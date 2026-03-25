try:
    from gevent import monkey; monkey.patch_all()
except ImportError:
    pass

import os
import asyncio
import aiohttp
import requests as req_lib
from flask import Flask, render_template, request, jsonify, Response, stream_with_context
from dotenv import load_dotenv
import time
import sqlite3
import uuid
import hmac
import hashlib
import json
from datetime import datetime, timedelta
import threading
from collections import defaultdict

load_dotenv()

app = Flask(__name__)

OPENAI_KEY    = os.getenv("OPENAI_API_KEY", "")
ANTHROPIC_KEY = os.getenv("ANTHROPIC_API_KEY", "")
GOOGLE_KEY    = os.getenv("GOOGLE_API_KEY", "")
GROK_KEY      = os.getenv("GROK_API_KEY", "")
MISTRAL_KEY   = os.getenv("MISTRAL_API_KEY", "")

STRIPE_WEBHOOK_SECRET = os.getenv("STRIPE_WEBHOOK_SECRET", "")
RESEND_API_KEY        = os.getenv("RESEND_API_KEY", "")
FROM_EMAIL            = os.getenv("FROM_EMAIL", "support@creativekonsoles.com")
DB_PATH               = os.getenv("DB_PATH", "/data/5i.db")

MAX_INPUT_CHARS = 500

# ── In-memory rate limiter ─────────────────────────────────────────────────────
_rl_lock     = threading.Lock()
_ip_log      = defaultdict(list)   # key → [timestamp, ...]

FREE_WINDOW  = 86400   # 24 h
FREE_LIMIT   = 5       # free (no-token) requests per IP per day
BURST_WINDOW = 60      # 1 min
BURST_LIMIT  = 8       # max requests per IP per minute, any tier


def _rl_check(ip, token):
    """Returns (allowed, reason). Burst-gates everyone; day-gates no-token users."""
    now = time.time()
    burst_key = f"b:{ip}"
    free_key  = f"f:{ip}"

    with _rl_lock:
        # Burst gate — everyone
        _ip_log[burst_key] = [t for t in _ip_log[burst_key] if now - t < BURST_WINDOW]
        if len(_ip_log[burst_key]) >= BURST_LIMIT:
            return False, "too_many_requests"
        _ip_log[burst_key].append(now)

        # Free-tier gate — no-token requests only
        if not token:
            _ip_log[free_key] = [t for t in _ip_log[free_key] if now - t < FREE_WINDOW]
            if len(_ip_log[free_key]) >= FREE_LIMIT:
                return False, "free_limit_reached"
            _ip_log[free_key].append(now)

    return True, "ok"

MODELS = {
    "gpt": {
        "label": "GPT-4o",
        "provider": "OpenAI",
        "color": "#10a37f",
        "enabled": lambda: bool(OPENAI_KEY),
    },
    "claude": {
        "label": "Claude 3.5",
        "provider": "Anthropic",
        "color": "#c96442",
        "enabled": lambda: bool(ANTHROPIC_KEY),
    },
    "gemini": {
        "label": "Gemini 1.5",
        "provider": "Google",
        "color": "#4285f4",
        "enabled": lambda: bool(GOOGLE_KEY),
    },
    "grok": {
        "label": "Grok 2",
        "provider": "xAI",
        "color": "#1da1f2",
        "enabled": lambda: bool(GROK_KEY),
    },
    "mistral": {
        "label": "Mistral Large",
        "provider": "Mistral",
        "color": "#f0a030",
        "enabled": lambda: bool(MISTRAL_KEY),
    },
}


# ── Database ──────────────────────────────────────────────────────────────────

def get_db():
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    with get_db() as db:
        db.execute("""
            CREATE TABLE IF NOT EXISTS subscribers (
                id                  INTEGER PRIMARY KEY AUTOINCREMENT,
                email               TEXT UNIQUE NOT NULL,
                token               TEXT UNIQUE NOT NULL,
                plan                TEXT NOT NULL,
                usage_count         INTEGER DEFAULT 0,
                monthly_limit       INTEGER NOT NULL,
                reset_date          TEXT NOT NULL,
                stripe_customer_id  TEXT,
                created_at          TEXT DEFAULT (datetime('now'))
            )
        """)
        db.commit()

try:
    init_db()
except Exception:
    pass  # /data may not exist locally — ok, DB init retried on first use


def _check_and_reset(row):
    """Reset usage counter if billing period has rolled over. Returns current usage_count."""
    reset_date = datetime.fromisoformat(row["reset_date"])
    if datetime.utcnow() >= reset_date:
        next_reset = (reset_date + timedelta(days=30)).isoformat()
        with get_db() as db:
            db.execute("UPDATE subscribers SET usage_count=0, reset_date=? WHERE token=?",
                       (next_reset, row["token"]))
            db.commit()
        return 0
    return row["usage_count"]


def verify_token(token):
    """Returns (ok, reason, row_or_None)"""
    if not token:
        return False, "no_token", None
    try:
        with get_db() as db:
            row = db.execute("SELECT * FROM subscribers WHERE token=?", (token,)).fetchone()
    except Exception:
        return False, "db_error", None
    if not row:
        return False, "invalid_token", None
    usage = _check_and_reset(row)
    limit = row["monthly_limit"]
    if limit != -1 and usage >= limit:
        return False, "limit_reached", row
    return True, "ok", row


def increment_usage(token):
    try:
        with get_db() as db:
            db.execute("UPDATE subscribers SET usage_count=usage_count+1 WHERE token=?", (token,))
            db.commit()
    except Exception:
        pass


def send_token_email(to_email, token, plan_name):
    if not RESEND_API_KEY:
        print(f"[TOKEN] {to_email} → {token} ({plan_name})")  # fallback: log it
        return
    try:
        req_lib.post(
            "https://api.resend.com/emails",
            headers={"Authorization": f"Bearer {RESEND_API_KEY}", "Content-Type": "application/json"},
            json={
                "from": FROM_EMAIL,
                "to": [to_email],
                "subject": "Your 5i Access Token",
                "html": f"""
                <div style="font-family:monospace;background:#0B1210;color:#5EE88A;padding:32px;border-radius:8px;max-width:520px;">
                  <h2 style="color:#fff;margin:0 0 16px;">Welcome to 5i — {plan_name}</h2>
                  <p style="color:rgba(255,255,255,0.65);margin:0 0 12px;">Your access token:</p>
                  <pre style="background:#0F1A16;padding:16px;border-radius:4px;font-size:16px;color:#86F5B4;border:1px solid #2E8C52;word-break:break-all;">{token}</pre>
                  <p style="color:rgba(255,255,255,0.65);margin:16px 0 0;">Open <a href="https://web-production-94a13.up.railway.app" style="color:#5EE88A;">5i</a>, click the TOKEN field in the toolbar, and paste it in. It saves automatically.</p>
                  <p style="color:rgba(255,255,255,0.35);font-size:11px;margin:24px 0 0;">Keep this token private. It is tied to your subscription.</p>
                </div>
                """
            },
            timeout=10
        )
    except Exception as e:
        print(f"[EMAIL ERROR] {e}")


# ── Individual model callers ──────────────────────────────────────────────────

MODEL_TIMEOUT  = aiohttp.ClientTimeout(total=25)   # fail fast
SYNTH_TIMEOUT  = aiohttp.ClientTimeout(total=35)   # synthesis gets more headroom
MAX_TOKENS     = 500   # model calls — enough for most answers, faster generation
MAX_TOKENS_SYNTH = 900 # synthesis output — needs room to cover all 5 responses


async def call_openai(session, prompt, max_tokens=MAX_TOKENS):
    try:
        async with session.post(
            "https://api.openai.com/v1/chat/completions",
            headers={"Authorization": f"Bearer {OPENAI_KEY}", "Content-Type": "application/json"},
            json={"model": "gpt-4o", "messages": [{"role": "user", "content": prompt}], "max_tokens": max_tokens},
            timeout=MODEL_TIMEOUT
        ) as r:
            data = await r.json()
            return data["choices"][0]["message"]["content"]
    except Exception as e:
        return f"Error: {str(e)}"


async def call_anthropic(session, prompt, max_tokens=MAX_TOKENS):
    try:
        async with session.post(
            "https://api.anthropic.com/v1/messages",
            headers={
                "x-api-key": ANTHROPIC_KEY,
                "anthropic-version": "2023-06-01",
                "Content-Type": "application/json"
            },
            json={"model": "claude-3-5-sonnet-20241022", "max_tokens": max_tokens,
                  "messages": [{"role": "user", "content": prompt}]},
            timeout=MODEL_TIMEOUT
        ) as r:
            data = await r.json()
            return data["content"][0]["text"]
    except Exception as e:
        return f"Error: {str(e)}"


async def call_gemini(session, prompt, max_tokens=MAX_TOKENS):
    try:
        url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-1.5-flash:generateContent?key={GOOGLE_KEY}"
        async with session.post(
            url,
            headers={"Content-Type": "application/json"},
            json={
                "contents": [{"parts": [{"text": prompt}]}],
                "generationConfig": {"maxOutputTokens": max_tokens}
            },
            timeout=MODEL_TIMEOUT
        ) as r:
            data = await r.json()
            return data["candidates"][0]["content"]["parts"][0]["text"]
    except Exception as e:
        return f"Error: {str(e)}"


async def call_grok(session, prompt, max_tokens=MAX_TOKENS):
    try:
        async with session.post(
            "https://api.x.ai/v1/chat/completions",
            headers={"Authorization": f"Bearer {GROK_KEY}", "Content-Type": "application/json"},
            json={"model": "grok-4-1-fast", "messages": [{"role": "user", "content": prompt}], "max_tokens": max_tokens},
            timeout=MODEL_TIMEOUT
        ) as r:
            data = await r.json()
            return data["choices"][0]["message"]["content"]
    except Exception as e:
        return f"Error: {str(e)}"


async def call_mistral(session, prompt, max_tokens=MAX_TOKENS):
    try:
        async with session.post(
            "https://api.mistral.ai/v1/chat/completions",
            headers={"Authorization": f"Bearer {MISTRAL_KEY}", "Content-Type": "application/json"},
            json={"model": "mistral-large-latest", "messages": [{"role": "user", "content": prompt}], "max_tokens": max_tokens},
            timeout=MODEL_TIMEOUT
        ) as r:
            data = await r.json()
            return data["choices"][0]["message"]["content"]
    except Exception as e:
        return f"Error: {str(e)}"


CALLERS = {
    "gpt":      call_openai,
    "claude":   call_anthropic,
    "gemini":   call_gemini,
    "grok":     call_grok,
    "mistral":  call_mistral,
}


# ── Synthesis / Judge pass ────────────────────────────────────────────────────

SYNTHESIS_PROMPT = """You are a neutral synthesis engine. Your function is to aggregate outputs from multiple independent AI models into a single coherent analysis — without preference, ranking, or bias toward any source.

You will be given {n} independent AI responses to the same prompt.

Your task:
1. Preserve all materially distinct insights across all responses
2. Do NOT prefer, rank, or bias toward any model
3. Identify areas of consensus and divergence with precision
4. Resolve redundancy by merging overlapping points — do not repeat them
5. Clearly distinguish between:
   — Shared conclusions (agreement across models)
   — Divergent perspectives (meaningful disagreement between models)
   — Unique insights (present in only one response)

Output Requirements:
— Do NOT mention model names
— Do NOT evaluate which response is "better"
— Do NOT discard minority viewpoints unless clearly erroneous
— Do NOT infer agreement unless it is explicitly present across responses
— Do NOT merge statements that differ in meaning even if they sound similar
— Maintain technical precision suitable for software developers and researchers
— Explicitly flag uncertainty or ambiguity where it exists

Structure your output as:

**Unified Synthesis** — a clean, coherent answer to the original question
**Consensus Points** — what all or most responses agreed on (bullet list)
**Divergences** — meaningful disagreements or contrasting positions (bullet list)
**Unique Contributions** — notable insights that appeared in only one response
**Open Questions** — unresolved uncertainty or gaps across all responses (omit if none)

Style: concise, information-dense, no filler language, preserve technical terminology.

Original question: {question}

Model responses:
{responses}

Begin synthesis."""


async def synthesize(session, question, results):
    """Feed all model responses into the best available judge model."""
    responses_text = "\n\n".join(
        f"[{MODELS[k]['label']} / {MODELS[k]['provider']}]:\n{v}"
        for k, v in results.items()
        if not v.startswith("Error:")
    )

    if not responses_text:
        return "Error: No valid model responses to synthesize."

    full_prompt = SYNTHESIS_PROMPT.format(
        n=len(results),
        question=question,
        responses=responses_text
    )

    # Synthesis judge priority: fastest first (Gemini Flash > Mistral > GPT > Claude)
    if GOOGLE_KEY:
        return await call_gemini(session, full_prompt, max_tokens=MAX_TOKENS_SYNTH)
    elif MISTRAL_KEY:
        return await call_mistral(session, full_prompt, max_tokens=MAX_TOKENS_SYNTH)
    elif OPENAI_KEY:
        return await call_openai(session, full_prompt, max_tokens=MAX_TOKENS_SYNTH)
    elif ANTHROPIC_KEY:
        return await call_anthropic(session, full_prompt, max_tokens=MAX_TOKENS_SYNTH)
    else:
        return "Error: Need at least one API key to render a verdict."


# ── Async orchestration ───────────────────────────────────────────────────────

async def query_all(prompt, selected):
    async with aiohttp.ClientSession() as session:
        tasks = [CALLERS[k](session, prompt) for k in selected]
        responses = await asyncio.gather(*tasks, return_exceptions=True)
        return {
            k: (r if isinstance(r, str) else f"Error: {str(r)}")
            for k, r in zip(selected, responses)
        }


async def query_all_with_verdict(prompt, selected):
    async with aiohttp.ClientSession() as session:
        tasks = [CALLERS[k](session, prompt) for k in selected]
        responses = await asyncio.gather(*tasks, return_exceptions=True)
        results = {
            k: (r if isinstance(r, str) else f"Error: {str(r)}")
            for k, r in zip(selected, responses)
        }
        verdict = await synthesize(session, prompt, results)
        return results, verdict


# ── Routes ────────────────────────────────────────────────────────────────────

@app.route("/")
def index():
    models_info = {
        k: {"label": v["label"], "provider": v["provider"],
            "color": v["color"], "enabled": v["enabled"]()}
        for k, v in MODELS.items()
    }
    return render_template("index.html", models=models_info, max_chars=MAX_INPUT_CHARS)


@app.route("/ask", methods=["POST"])
def ask():
    data         = request.json
    token        = data.get("token", "").strip()
    prompt       = data.get("prompt", "").strip()[:MAX_INPUT_CHARS]
    want_verdict = data.get("verdict", False)
    selected     = [k for k in data.get("models", list(MODELS.keys()))
                    if k in MODELS and MODELS[k]["enabled"]()]

    ip = (request.headers.get("X-Forwarded-For", "") or request.remote_addr or "").split(",")[0].strip()

    # Rate limiting — burst gate for everyone, day gate for no-token users
    allowed, rl_reason = _rl_check(ip, token)
    if not allowed:
        return jsonify({"error": f"Rate limit: {rl_reason}"}), 429

    # Token gate
    use_token = False
    if token:
        ok, reason, _ = verify_token(token)
        if not ok:
            return jsonify({"error": f"Access denied: {reason}"}), 403
        use_token = True

    if not prompt:
        return jsonify({"error": "Empty prompt"}), 400
    if not selected:
        return jsonify({"error": "No models available — add API keys to .env"}), 400

    start = time.time()

    if want_verdict:
        results, verdict = asyncio.run(query_all_with_verdict(prompt, selected))
    else:
        results = asyncio.run(query_all(prompt, selected))
        verdict = None

    if use_token:
        increment_usage(token)

    elapsed = round(time.time() - start, 1)
    return jsonify({"results": results, "verdict": verdict, "elapsed": elapsed, "prompt": prompt})


@app.route("/verdict", methods=["POST"])
def render_verdict():
    """Standalone verdict endpoint — call after already having model results."""
    data = request.json
    prompt = data.get("prompt", "")
    results = data.get("results", {})

    if not results:
        return jsonify({"error": "No results to synthesize"}), 400

    async def _synth():
        async with aiohttp.ClientSession() as session:
            return await synthesize(session, prompt, results)

    start = time.time()
    verdict = asyncio.run(_synth())
    elapsed = round(time.time() - start, 1)
    return jsonify({"verdict": verdict, "elapsed": elapsed})


# ── Streaming proxy routes — keep API keys off the browser ──────────────────

def _stream_proxy(upstream_url, upstream_headers, upstream_body):
    # Eagerly open the connection so we can check status before streaming
    try:
        r = req_lib.post(upstream_url, headers=upstream_headers,
                         json=upstream_body, stream=True, timeout=90)
    except Exception as e:
        return jsonify({"error": str(e)}), 502

    if not r.ok:
        err = r.text[:400]
        r.close()
        return jsonify({"error": f"Upstream {r.status_code}: {err}"}), r.status_code

    def generate(resp):
        try:
            for chunk in resp.iter_content(chunk_size=None):
                if chunk:
                    yield chunk
        except Exception as e:
            yield f"data: {{\"_err\": \"{str(e)}\"}}\n\n".encode()
        finally:
            resp.close()

    return Response(stream_with_context(generate(r)), content_type='text/event-stream',
                    headers={'X-Accel-Buffering': 'no', 'Cache-Control': 'no-cache'})


@app.route('/proxy/claude', methods=['POST'])
def proxy_claude():
    d = request.json
    key = d.get('apiKey') or ANTHROPIC_KEY
    if not key:
        return jsonify({"error": "No Anthropic API key — add one via BYOK or set ANTHROPIC_API_KEY on the server"}), 503
    return _stream_proxy(
        'https://api.anthropic.com/v1/messages',
        {'x-api-key': key, 'anthropic-version': '2023-06-01', 'content-type': 'application/json'},
        {'model': 'claude-haiku-4-5-20251001', 'max_tokens': d.get('maxTokens', 1500),
         'stream': True, 'system': d.get('sysPrompt', ''),
         'messages': [{'role': 'user', 'content': d.get('userPrompt', '')}]}
    )


@app.route('/proxy/gpt', methods=['POST'])
def proxy_gpt():
    d = request.json
    key = d.get('apiKey') or OPENAI_KEY
    if not key:
        return jsonify({"error": "No OpenAI API key — add one via BYOK or set OPENAI_API_KEY on the server"}), 503
    return _stream_proxy(
        'https://api.openai.com/v1/chat/completions',
        {'Authorization': f'Bearer {key}', 'Content-Type': 'application/json'},
        {'model': 'gpt-4o', 'max_tokens': d.get('maxTokens', 1500), 'stream': True,
         'messages': [{'role': 'system', 'content': d.get('sysPrompt', '')},
                      {'role': 'user', 'content': d.get('userPrompt', '')}]}
    )


@app.route('/proxy/gemini', methods=['POST'])
def proxy_gemini():
    d = request.json
    key = d.get('apiKey') or GOOGLE_KEY
    if not key:
        return jsonify({"error": "No Google API key — add one via BYOK or set GOOGLE_API_KEY on the server"}), 503
    url = f'https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash:streamGenerateContent?alt=sse&key={key}'
    return _stream_proxy(
        url,
        {'Content-Type': 'application/json'},
        {'system_instruction': {'parts': [{'text': d.get('sysPrompt', '')}]},
         'contents': [{'role': 'user', 'parts': [{'text': d.get('userPrompt', '')}]}],
         'generationConfig': {'maxOutputTokens': d.get('maxTokens', 1500), 'temperature': 0.7}}
    )


@app.route('/proxy/mistral', methods=['POST'])
def proxy_mistral():
    d = request.json
    key = d.get('apiKey') or MISTRAL_KEY
    if not key:
        return jsonify({"error": "No Mistral API key — add one via BYOK or set MISTRAL_API_KEY on the server"}), 503
    return _stream_proxy(
        'https://api.mistral.ai/v1/chat/completions',
        {'Authorization': f'Bearer {key}', 'Content-Type': 'application/json'},
        {'model': 'mistral-small-latest', 'max_tokens': d.get('maxTokens', 1500), 'stream': True,
         'messages': [{'role': 'system', 'content': d.get('sysPrompt', '')},
                      {'role': 'user', 'content': d.get('userPrompt', '')}]}
    )


@app.route('/proxy/grok', methods=['POST'])
def proxy_grok():
    d = request.json
    key = d.get('apiKey') or GROK_KEY
    if not key:
        return jsonify({"error": "No xAI API key — add one via BYOK or set GROK_API_KEY on the server"}), 503
    return _stream_proxy(
        'https://api.x.ai/v1/chat/completions',
        {'Authorization': f'Bearer {key}', 'Content-Type': 'application/json'},
        {'model': 'grok-3-mini-beta', 'max_tokens': d.get('maxTokens', 1500), 'stream': True,
         'messages': [{'role': 'system', 'content': d.get('sysPrompt', '')},
                      {'role': 'user', 'content': d.get('userPrompt', '')}]}
    )


@app.route("/verify-token", methods=["POST"])
def verify_token_endpoint():
    token = (request.json or {}).get("token", "").strip()
    ok, reason, row = verify_token(token)
    if not ok:
        return jsonify({"valid": False, "reason": reason}), 200
    return jsonify({
        "valid":       True,
        "plan":        row["plan"],
        "usage_count": row["usage_count"],
        "monthly_limit": row["monthly_limit"],
        "reset_date":  row["reset_date"]
    }), 200


@app.route("/webhook/stripe", methods=["POST"])
def stripe_webhook():
    payload    = request.get_data()
    sig_header = request.headers.get("Stripe-Signature", "")

    if STRIPE_WEBHOOK_SECRET:
        try:
            parts    = dict(p.split("=", 1) for p in sig_header.split(","))
            ts       = parts.get("t", "")
            v1       = parts.get("v1", "")
            signed   = ts.encode() + b"." + payload
            expected = hmac.new(STRIPE_WEBHOOK_SECRET.encode(), signed, hashlib.sha256).hexdigest()
            if not hmac.compare_digest(expected, v1):
                return jsonify({"error": "invalid signature"}), 400
        except Exception:
            return jsonify({"error": "signature error"}), 400

    event = request.get_json(force=True)
    if not event or event.get("type") != "checkout.session.completed":
        return jsonify({"ok": True}), 200

    obj         = event["data"]["object"]
    email       = obj.get("customer_details", {}).get("email", "")
    customer_id = obj.get("customer", "")
    metadata    = obj.get("metadata", {})
    plan_key    = metadata.get("plan", "base")

    if plan_key == "foundational":
        monthly_limit = 1000
        plan_name     = "Foundational Synthesis"
    else:
        monthly_limit = 100
        plan_name     = "Base Synthesis"

    token      = str(uuid.uuid4())
    reset_date = (datetime.utcnow() + timedelta(days=30)).isoformat()

    try:
        with get_db() as db:
            db.execute("""
                INSERT INTO subscribers (email, token, plan, usage_count, monthly_limit, reset_date, stripe_customer_id)
                VALUES (?, ?, ?, 0, ?, ?, ?)
                ON CONFLICT(email) DO UPDATE SET
                    token=excluded.token, plan=excluded.plan,
                    monthly_limit=excluded.monthly_limit,
                    reset_date=excluded.reset_date,
                    stripe_customer_id=excluded.stripe_customer_id,
                    usage_count=0
            """, (email, token, plan_key, monthly_limit, reset_date, customer_id))
            db.commit()
    except Exception as e:
        return jsonify({"error": str(e)}), 500

    send_token_email(email, token, plan_name)
    return jsonify({"ok": True}), 200


@app.route('/guide')
def guide():
    return render_template('guide.html')


@app.route('/konsole')
def konsole():
    return render_template('konsole.html')


@app.route('/health')
def health():
    return jsonify({
        "status": "ok",
        "keys": {
            "openai":    bool(OPENAI_KEY),
            "anthropic": bool(ANTHROPIC_KEY),
            "google":    bool(GOOGLE_KEY),
            "grok":      bool(GROK_KEY),
            "mistral":   bool(MISTRAL_KEY),
        }
    })


if __name__ == "__main__":
    port = int(os.getenv("PORT", 5562))
    host = "0.0.0.0" if os.getenv("PORT") else "127.0.0.1"
    app.run(host=host, port=port, debug=not os.getenv("PORT"))
