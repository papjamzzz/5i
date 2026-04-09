try:
    from gevent import monkey; monkey.patch_all()
except ImportError:
    pass

import sentry_sdk
from sentry_sdk.integrations.flask import FlaskIntegration

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

sentry_sdk.init(
    dsn=os.getenv("SENTRY_DSN", ""),
    integrations=[FlaskIntegration()],
    traces_sample_rate=0.1,
    send_default_pii=False,
)

app = Flask(__name__)

OPENAI_KEY    = os.getenv("OPENAI_API_KEY", "")
ANTHROPIC_KEY = os.getenv("ANTHROPIC_API_KEY", "")
GOOGLE_KEY    = os.getenv("GOOGLE_API_KEY", "")
GROK_KEY      = os.getenv("GROK_API_KEY", "")
MISTRAL_KEY   = os.getenv("MISTRAL_API_KEY", "")

STRIPE_WEBHOOK_SECRET = os.getenv("STRIPE_WEBHOOK_SECRET", "")
STRIPE_SECRET_KEY     = os.getenv("STRIPE_SECRET_KEY", "")
RESEND_API_KEY        = os.getenv("RESEND_API_KEY", "")
FROM_EMAIL            = os.getenv("FROM_EMAIL", "support@creativekonsoles.com")
DB_PATH               = os.getenv("DB_PATH", "/data/5i.db")
KALSHI_API_KEY        = os.getenv("KALSHI_API_KEY", "")

MAX_INPUT_CHARS = 2000

# ── In-memory rate limiter ─────────────────────────────────────────────────────
_rl_lock     = threading.Lock()
_ip_log      = defaultdict(list)   # key → [timestamp, ...]

FREE_WINDOW  = 86400   # 24 h
FREE_LIMIT   = 99999   # owner: effectively unlimited
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
        db.execute("""
            CREATE TABLE IF NOT EXISTS fusion_signals (
                id               INTEGER PRIMARY KEY AUTOINCREMENT,
                ticker           TEXT,
                title            TEXT,
                market_price     INTEGER,
                ai_consensus     INTEGER,
                alpha_gap        INTEGER,
                signal_strength  TEXT,
                trade_decision   TEXT,
                trade_side       TEXT,
                verdict          TEXT,
                confidence       INTEGER,
                synth_signal     TEXT,
                contracts        INTEGER DEFAULT 0,
                order_id         TEXT,
                outcome          TEXT DEFAULT 'pending',
                created_at       TEXT DEFAULT (datetime('now'))
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


def issue_prorated_refund(customer_id, period_start_ts, period_end_ts, plan_key):
    """Calculate unused days, find latest charge, issue prorated refund. Returns refund amount in cents or 0."""
    if not STRIPE_SECRET_KEY:
        print(f"[REFUND] No STRIPE_SECRET_KEY — skipping refund for {customer_id}")
        return 0
    try:
        now_ts = int(datetime.utcnow().timestamp())
        total_secs     = max(period_end_ts - period_start_ts, 1)
        remaining_secs = max(period_end_ts - now_ts, 0)
        prorate_ratio  = remaining_secs / total_secs

        plan_price_cents = 8800 if plan_key == "foundational" else 1800
        refund_cents = int(plan_price_cents * prorate_ratio)

        if refund_cents < 50:  # Stripe minimum is 50 cents
            return 0

        # Get most recent charge for this customer
        charges_resp = req_lib.get(
            f"https://api.stripe.com/v1/charges?customer={customer_id}&limit=1",
            headers={"Authorization": f"Bearer {STRIPE_SECRET_KEY}"},
            timeout=10
        )
        charges = charges_resp.json().get("data", [])
        if not charges:
            return 0

        charge_id = charges[0]["id"]
        refund_resp = req_lib.post(
            "https://api.stripe.com/v1/refunds",
            headers={"Authorization": f"Bearer {STRIPE_SECRET_KEY}",
                     "Content-Type": "application/x-www-form-urlencoded"},
            data={"charge": charge_id, "amount": refund_cents},
            timeout=10
        )
        if refund_resp.ok:
            print(f"[REFUND] Issued ${refund_cents/100:.2f} to {customer_id}")
            return refund_cents
        else:
            print(f"[REFUND ERROR] {refund_resp.text[:200]}")
            return 0
    except Exception as e:
        print(f"[REFUND ERROR] {e}")
        return 0


def send_cancellation_email(to_email, refund_cents):
    if not RESEND_API_KEY:
        print(f"[CANCEL EMAIL] {to_email} — refund ${refund_cents/100:.2f}")
        return
    refund_line = (f"<p style='color:rgba(255,255,255,0.65);margin:12px 0 0;'>A prorated refund of <strong style='color:#fff;'>${refund_cents/100:.2f}</strong> has been issued to your original payment method. Allow 5–10 business days.</p>"
                   if refund_cents > 0 else "")
    try:
        req_lib.post(
            "https://api.resend.com/emails",
            headers={"Authorization": f"Bearer {RESEND_API_KEY}", "Content-Type": "application/json"},
            json={
                "from": FROM_EMAIL,
                "to": [to_email],
                "subject": "Your 5i subscription has been cancelled",
                "html": f"""
                <div style="font-family:monospace;background:#0B1210;color:#5EE88A;padding:32px;border-radius:8px;max-width:520px;">
                  <h2 style="color:#fff;margin:0 0 16px;">See you around.</h2>
                  <p style="color:rgba(255,255,255,0.65);margin:0 0 12px;">Your 5i subscription has been cancelled and your access token has been deactivated.</p>
                  {refund_line}
                  <p style="color:rgba(255,255,255,0.65);margin:16px 0 0;">Whenever you're ready to come back, we'll be here — <a href="https://creativekonsoles.com/#pricing" style="color:#5EE88A;">creativekonsoles.com</a>.</p>
                  <p style="color:rgba(255,255,255,0.35);font-size:11px;margin:24px 0 0;">Creative Konsoles · support@creativekonsoles.com</p>
                </div>
                """
            },
            timeout=10
        )
    except Exception as e:
        print(f"[CANCEL EMAIL ERROR] {e}")


# ── Individual model callers ──────────────────────────────────────────────────

MODEL_TIMEOUT  = aiohttp.ClientTimeout(total=25)   # fail fast
SYNTH_TIMEOUT  = aiohttp.ClientTimeout(total=35)   # synthesis gets more headroom
MAX_TOKENS     = 500   # model calls — enough for most answers, faster generation
MAX_TOKENS_SYNTH = 900 # synthesis output — needs room to cover all 5 responses

# Injected into every model call to keep responses tight and comparable
RESPONSE_SYSTEM = "Be concise. Answer in 3 sentences or fewer. Do not pad, repeat, or add filler."


async def call_openai(session, prompt, max_tokens=MAX_TOKENS, system=RESPONSE_SYSTEM):
    try:
        messages = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": prompt})
        async with session.post(
            "https://api.openai.com/v1/chat/completions",
            headers={"Authorization": f"Bearer {OPENAI_KEY}", "Content-Type": "application/json"},
            json={"model": "gpt-4o", "messages": messages, "max_tokens": max_tokens},
            timeout=MODEL_TIMEOUT
        ) as r:
            data = await r.json()
            return data["choices"][0]["message"]["content"]
    except Exception as e:
        return f"Error: {str(e)}"


async def call_anthropic(session, prompt, max_tokens=MAX_TOKENS, system=RESPONSE_SYSTEM):
    try:
        body = {
            "model": "claude-3-5-sonnet-20240620",
            "max_tokens": max_tokens,
            "messages": [{"role": "user", "content": prompt}]
        }
        if system:
            body["system"] = system
        async with session.post(
            "https://api.anthropic.com/v1/messages",
            headers={
                "x-api-key": ANTHROPIC_KEY,
                "anthropic-version": "2023-06-01",
                "Content-Type": "application/json"
            },
            json=body,
            timeout=MODEL_TIMEOUT
        ) as r:
            data = await r.json()
            if "content" not in data:
                err = data.get("error", {}).get("message", str(data))
                return f"Error: {err}"
            return data["content"][0]["text"]
    except Exception as e:
        return f"Error: {str(e)}"


async def call_gemini(session, prompt, max_tokens=MAX_TOKENS, system=RESPONSE_SYSTEM):
    try:
        url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-2.0-flash:generateContent?key={GOOGLE_KEY}"
        body = {
            "contents": [{"parts": [{"text": prompt}]}],
            "generationConfig": {"maxOutputTokens": max_tokens}
        }
        if system:
            body["system_instruction"] = {"parts": [{"text": system}]}
        async with session.post(
            url,
            headers={"Content-Type": "application/json"},
            json=body,
            timeout=MODEL_TIMEOUT
        ) as r:
            data = await r.json()
            if "candidates" not in data:
                block = data.get("promptFeedback", {}).get("blockReason", "no candidates returned")
                return f"Error: {block}"
            candidate = data["candidates"][0]
            # Handle safety-blocked or empty candidates
            if candidate.get("finishReason") in ("SAFETY", "RECITATION", "OTHER"):
                return f"Error: blocked ({candidate.get('finishReason')})"
            parts = candidate.get("content", {}).get("parts", [])
            if not parts:
                return f"Error: empty Gemini response"
            return parts[0]["text"]
    except Exception as e:
        return f"Error: {str(e)}"


async def call_grok(session, prompt, max_tokens=MAX_TOKENS, system=RESPONSE_SYSTEM):
    try:
        messages = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": prompt})
        async with session.post(
            "https://api.x.ai/v1/chat/completions",
            headers={"Authorization": f"Bearer {GROK_KEY}", "Content-Type": "application/json"},
            json={"model": "grok-3-mini-beta", "messages": messages, "max_tokens": max_tokens},
            timeout=MODEL_TIMEOUT
        ) as r:
            data = await r.json()
            choices = data.get("choices") or []
            if not choices:
                return f"Error: {data.get('error', {}).get('message', str(data))}"
            return choices[0]["message"]["content"]
    except Exception as e:
        return f"Error: {str(e)}"


async def call_mistral(session, prompt, max_tokens=MAX_TOKENS, system=RESPONSE_SYSTEM):
    try:
        messages = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": prompt})
        async with session.post(
            "https://api.mistral.ai/v1/chat/completions",
            headers={"Authorization": f"Bearer {MISTRAL_KEY}", "Content-Type": "application/json"},
            json={"model": "mistral-large-latest", "messages": messages, "max_tokens": max_tokens},
            timeout=MODEL_TIMEOUT
        ) as r:
            data = await r.json()
            choices = data.get("choices") or []
            if not choices:
                return f"Error: {data.get('message', str(data))}"
            return choices[0]["message"]["content"]
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

SYNTHESIS_PROMPT = """You are a verdict engine — not a summarizer. Your job is to read {n} independent AI responses to the same question and produce a single authoritative verdict with a confidence score.

Original question: {question}

Model responses:
{responses}

Your output MUST be valid JSON and nothing else. No markdown, no preamble, no explanation outside the JSON.

Return exactly this structure:

{{
  "verdict": "YES" | "NO" | "UNCERTAIN",
  "confidence": <integer 0-100>,
  "signal": "<one punchy sentence — the single most important takeaway, written as a direct statement>",
  "consensus": ["<point 1>", "<point 2>", "<point 3 max>"],
  "dissent": ["<meaningful disagreement 1>", "<meaningful disagreement 2 if exists>"],
  "edge": "<one sentence — what a smart person should do or watch for given this verdict>",
  "flag": "HIGH_DISAGREEMENT" | "LOW_CONFIDENCE" | "STRONG_CONSENSUS" | "MIXED" | "CLEAR"
}}

Scoring rules:
- confidence 80-100: strong consensus, models mostly agree on direction
- confidence 50-79: mixed signals, lean one way but notable dissent
- confidence 0-49: models meaningfully disagree — verdict is UNCERTAIN
- flag HIGH_DISAGREEMENT if models contradict each other on the core question
- flag STRONG_CONSENSUS if 4+ models align on the same conclusion
- flag LOW_CONFIDENCE if the question cannot be answered with the available information
- dissent array may be empty [] if there is no meaningful disagreement
- Keep every string concise — signal and edge max 20 words each

Begin."""


async def synthesize(session, question, results):
    """Feed all model responses into the judge model — returns structured verdict dict."""
    import json as _json
    import re as _re

    responses_text = "\n\n".join(
        f"[{MODELS[k]['label']}]:\n{v}"
        for k, v in results.items()
        if not v.startswith("Error:")
    )

    if not responses_text:
        return {"error": "No valid model responses to synthesize."}

    full_prompt = SYNTHESIS_PROMPT.format(
        n=len(results),
        question=question,
        responses=responses_text
    )

    # Judge priority: Gemini Flash > Claude > GPT > Mistral
    # Cascade — if the top judge fails, fall through to the next available model
    raw = None
    judges = []
    if GOOGLE_KEY:
        judges.append(("gemini", call_gemini))
    if ANTHROPIC_KEY:
        judges.append(("claude", call_anthropic))
    if OPENAI_KEY:
        judges.append(("gpt", call_openai))
    if MISTRAL_KEY:
        judges.append(("mistral", call_mistral))

    if not judges:
        return {"error": "No API key available for synthesis."}

    for judge_name, judge_fn in judges:
        candidate = await judge_fn(session, full_prompt, max_tokens=MAX_TOKENS_SYNTH, system=None)
        if not candidate.startswith("Error:"):
            raw = candidate
            break
        print(f"[SYNTH] Judge {judge_name} failed: {candidate[:80]} — trying next")

    if raw is None:
        return {
            "verdict": "UNCERTAIN",
            "confidence": 0,
            "signal": "All judge models failed — check API keys.",
            "consensus": [],
            "dissent": [],
            "edge": "",
            "flag": "LOW_CONFIDENCE"
        }

    # Parse JSON — strip markdown fences if present
    try:
        cleaned = _re.sub(r"```(?:json)?\s*|\s*```", "", raw).strip()
        return _json.loads(cleaned)
    except Exception:
        # Fallback: return raw text wrapped so UI doesn't break
        return {
            "verdict": "UNCERTAIN",
            "confidence": 0,
            "signal": raw[:200] if raw else "Synthesis failed.",
            "consensus": [],
            "dissent": [],
            "edge": "",
            "flag": "LOW_CONFIDENCE"
        }


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
    url = f'https://generativelanguage.googleapis.com/v1beta/models/gemini-2.0-flash:streamGenerateContent?alt=sse&key={key}'
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

    event      = request.get_json(force=True)
    event_type = (event or {}).get("type", "")

    if not event:
        return jsonify({"ok": True}), 200

    # ── Cancellation — prorate refund + deactivate token ──
    if event_type == "customer.subscription.deleted":
        sub         = event["data"]["object"]
        customer_id = sub.get("customer", "")
        period_start = sub.get("current_period_start", 0)
        period_end   = sub.get("current_period_end", 0)

        try:
            with get_db() as db:
                row = db.execute("SELECT * FROM subscribers WHERE stripe_customer_id=?",
                                 (customer_id,)).fetchone()
            if row:
                refund_cents = issue_prorated_refund(
                    customer_id, period_start, period_end, row["plan"])
                with get_db() as db:
                    db.execute("UPDATE subscribers SET monthly_limit=0 WHERE stripe_customer_id=?",
                               (customer_id,))
                    db.commit()
                send_cancellation_email(row["email"], refund_cents)
        except Exception as e:
            print(f"[CANCEL ERROR] {e}")
        return jsonify({"ok": True}), 200

    if event_type != "checkout.session.completed":
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


# ── Kalshi Fusion ─────────────────────────────────────────────────────────────

KALSHI_BASE     = "https://api.elections.kalshi.com/trade-api/v2"
KALSHI_ALT_BASE = "https://trading-api.kalshi.com/trade-api/v2"

KALSHI_FUSION_PROMPT = """You are a prediction market analyst. Be precise, direct, and calibrated.

Market question: "{title}"
Current market price: {price}¢ YES (implies {price}% probability of YES)
Days until resolution: {days}
Category: {category}

Your job: assess whether this market is MISPRICED relative to the current {price}¢ price.

Consider: base rates, current evidence, time remaining, relevant context, and what the crowd typically gets wrong.

Respond in EXACTLY this format (three lines, nothing else):
PROBABILITY: [your integer estimate 0-100 that this resolves YES]
EDGE: [one sentence — why the market price is wrong, or why it's fair]
RISK: [one sentence — the single biggest thing that could make you wrong]"""


def _fetch_kalshi_markets(limit=25):
    """Fetch open markets from Kalshi. Returns list of market dicts or []."""
    headers = {"accept": "application/json"}
    if KALSHI_API_KEY:
        headers["Authorization"] = f"Bearer {KALSHI_API_KEY}"

    params = {"limit": limit, "status": "open"}

    for base in (KALSHI_BASE, KALSHI_ALT_BASE):
        try:
            r = req_lib.get(f"{base}/markets", headers=headers, params=params, timeout=8)
            if r.ok:
                data = r.json()
                markets_raw = data.get("markets", [])
                out = []
                for m in markets_raw:
                    yes_bid = m.get("yes_bid", 0) or 0
                    yes_ask = m.get("yes_ask", 0) or 0
                    mid = round((yes_bid + yes_ask) / 2) if (yes_bid or yes_ask) else 50
                    title = m.get("title") or m.get("subtitle") or ""
                    if not title:
                        continue
                    close_time = m.get("close_time") or m.get("expected_expiration_time") or ""
                    try:
                        close_dt = datetime.fromisoformat(close_time.replace("Z", ""))
                        days = max(0, (close_dt - datetime.utcnow()).days)
                    except Exception:
                        days = 0
                    out.append({
                        "ticker":       m.get("ticker", ""),
                        "event_ticker": m.get("event_ticker", ""),
                        "title":        title,
                        "price":        mid,
                        "yes_bid":      yes_bid,
                        "yes_ask":      yes_ask,
                        "volume":       m.get("volume", 0) or 0,
                        "category":     m.get("category", "General"),
                        "days":         days,
                        "close_time":   close_time,
                    })
                return out
        except Exception:
            continue
    return []


def _parse_model_probability(text):
    """Extract PROBABILITY: N from model response. Returns int or None."""
    import re as _re
    m = _re.search(r"PROBABILITY:\s*(\d+)", text, _re.IGNORECASE)
    if m:
        val = int(m.group(1))
        return max(0, min(100, val))
    # fallback: any bare integer 0-100 on its own line
    for line in text.splitlines():
        m2 = _re.match(r"^\s*(\d{1,3})\s*$", line.strip())
        if m2:
            val = int(m2.group(1))
            if 0 <= val <= 100:
                return val
    return None


@app.route("/kalshi-fusion/order", methods=["POST"])
def kalshi_fusion_order():
    """Place a Kalshi order directly from the fusion signal."""
    if not KALSHI_API_KEY:
        return jsonify({"error": "No KALSHI_API_KEY configured"}), 503

    data    = request.json
    ticker  = (data.get("ticker") or "").strip().upper()
    side    = (data.get("side") or "").lower()   # "yes" or "no"
    count   = int(data.get("count", 5))
    price_c = int(data.get("price_cents", 50))   # limit price in cents

    if not ticker:
        return jsonify({"error": "ticker required"}), 400
    if side not in ("yes", "no"):
        return jsonify({"error": "side must be yes or no"}), 400
    count = max(1, min(50, count))

    headers = {
        "Authorization": f"Bearer {KALSHI_API_KEY}",
        "Content-Type":  "application/json",
        "accept":        "application/json",
    }

    order_body = {
        "ticker":          ticker,
        "client_order_id": str(uuid.uuid4()),
        "action":          "buy",
        "type":            "limit",
        "side":            side,
        "count":           count,
        "price":           price_c,
    }

    try:
        r = req_lib.post(f"{KALSHI_BASE}/orders", headers=headers,
                         json=order_body, timeout=10)
        if r.ok:
            resp  = r.json()
            order = resp.get("order", resp)
            return jsonify({
                "ok":       True,
                "order_id": order.get("order_id", order.get("id", "")),
                "status":   order.get("status", "submitted"),
                "ticker":   ticker,
                "side":     side,
                "count":    count,
                "price":    price_c,
            })
        try:
            err_body = r.json()
            err_msg  = err_body.get("error", err_body.get("detail", r.text[:300]))
        except Exception:
            err_msg = r.text[:300]
        return jsonify({"error": f"{r.status_code}: {err_msg}"}), 502
    except Exception as e:
        return jsonify({"error": str(e)}), 502


@app.route("/kalshi-fusion/log", methods=["POST"])
def kalshi_fusion_log():
    """Save a fusion signal to the trade log."""
    d = request.json or {}
    try:
        with get_db() as db:
            db.execute("""
                INSERT INTO fusion_signals
                  (ticker, title, market_price, ai_consensus, alpha_gap,
                   signal_strength, trade_decision, trade_side,
                   verdict, confidence, synth_signal, contracts, order_id)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
            """, (
                d.get("ticker", ""),
                d.get("title", "")[:300],
                d.get("market_price"),
                d.get("ai_consensus"),
                d.get("alpha_gap"),
                d.get("signal_strength", ""),
                d.get("trade_decision", ""),
                d.get("trade_side", ""),
                d.get("verdict", ""),
                d.get("confidence"),
                d.get("synth_signal", "")[:300],
                d.get("contracts", 0),
                d.get("order_id", ""),
            ))
            db.commit()
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/kalshi-fusion/history")
def kalshi_fusion_history():
    """Return recent fusion signals."""
    try:
        with get_db() as db:
            rows = db.execute("""
                SELECT * FROM fusion_signals
                ORDER BY created_at DESC LIMIT 50
            """).fetchall()
        return jsonify({"signals": [dict(r) for r in rows]})
    except Exception as e:
        return jsonify({"signals": [], "error": str(e)})


@app.route("/kalshi-fusion/balance")
def kalshi_fusion_balance():
    """Fetch Kalshi account balance."""
    if not KALSHI_API_KEY:
        return jsonify({"balance": None})
    headers = {"Authorization": f"Bearer {KALSHI_API_KEY}", "accept": "application/json"}
    for base in (KALSHI_BASE, KALSHI_ALT_BASE):
        try:
            r = req_lib.get(f"{base}/portfolio/balance", headers=headers, timeout=6)
            if r.ok:
                d = r.json()
                balance = d.get("balance", d.get("available_balance", 0))
                return jsonify({"balance": balance})  # in cents
        except Exception:
            continue
    return jsonify({"balance": None})


@app.route("/kalshi-fusion")
def kalshi_fusion():
    models_info = {
        k: {"label": v["label"], "provider": v["provider"],
            "color": v["color"], "enabled": v["enabled"]()}
        for k, v in MODELS.items()
    }
    return render_template("kalshi_fusion.html",
                           models=models_info,
                           has_kalshi_key=bool(KALSHI_API_KEY))


@app.route("/kalshi-fusion/markets")
def kalshi_fusion_markets():
    markets = _fetch_kalshi_markets(25)
    return jsonify({"markets": markets, "count": len(markets)})


@app.route("/kalshi-fusion/analyze", methods=["POST"])
def kalshi_fusion_analyze():
    data     = request.json
    title    = (data.get("title") or "").strip()[:400]
    price    = int(data.get("price", 50))
    days     = int(data.get("days", 7))
    category = (data.get("category") or "General").strip()[:50]
    selected = [k for k in data.get("models", list(MODELS.keys()))
                if k in MODELS and MODELS[k]["enabled"]()]

    ip = (request.headers.get("X-Forwarded-For", "") or request.remote_addr or "").split(",")[0].strip()
    token = (data.get("token") or "").strip()

    allowed, rl_reason = _rl_check(ip, token)
    if not allowed:
        return jsonify({"error": f"Rate limit: {rl_reason}"}), 429

    if token:
        ok, reason, _ = verify_token(token)
        if not ok:
            return jsonify({"error": f"Access denied: {reason}"}), 403

    if not title:
        return jsonify({"error": "Market question required"}), 400
    if not selected:
        return jsonify({"error": "No models available"}), 400

    price = max(1, min(99, price))
    prompt = KALSHI_FUSION_PROMPT.format(
        title=title, price=price, days=days, category=category
    )

    start = time.time()
    results, verdict = asyncio.run(query_all_with_verdict(prompt, selected))
    elapsed = round(time.time() - start, 1)

    # Parse individual model probabilities + edge/risk
    import re as _re
    model_probs = {}
    model_details = {}
    for k, text in results.items():
        if not text.startswith("Error:"):
            p = _parse_model_probability(text)
            if p is not None:
                model_probs[k] = p
            edge_m = _re.search(r"EDGE:\s*(.+)", text, _re.IGNORECASE)
            risk_m = _re.search(r"RISK:\s*(.+)", text, _re.IGNORECASE)
            model_details[k] = {
                "edge": edge_m.group(1).strip() if edge_m else "",
                "risk": risk_m.group(1).strip() if risk_m else "",
            }

    # AI consensus = median of parsed probabilities
    if model_probs:
        probs_sorted = sorted(model_probs.values())
        n = len(probs_sorted)
        if n % 2 == 0:
            ai_consensus = round((probs_sorted[n//2 - 1] + probs_sorted[n//2]) / 2)
        else:
            ai_consensus = probs_sorted[n//2]
    else:
        ai_consensus = None

    alpha_gap = abs(ai_consensus - price) if ai_consensus is not None else None
    if alpha_gap is not None:
        if alpha_gap >= 20:
            signal_strength = "STRONG"
        elif alpha_gap >= 10:
            signal_strength = "MODERATE"
        else:
            signal_strength = "WEAK"
        signal_direction = "OVER" if ai_consensus > price else "UNDER"
        # TRADE/SKIP decision: trade if gap >= 10 and models agree on direction
        prob_values = list(model_probs.values())
        models_above = sum(1 for p in prob_values if p > price)
        models_below = sum(1 for p in prob_values if p < price)
        direction_agreement = max(models_above, models_below) / len(prob_values) if prob_values else 0
        trade_decision = "TRADE" if alpha_gap >= 10 and direction_agreement >= 0.6 else "SKIP"
        trade_side = ("BUY YES" if signal_direction == "OVER" else "BUY NO") if trade_decision == "TRADE" else None
    else:
        signal_strength = "UNKNOWN"
        signal_direction = None
        trade_decision = "SKIP"
        trade_side = None

    if token:
        increment_usage(token)

    return jsonify({
        "results":           results,
        "verdict":           verdict,
        "model_probs":       model_probs,
        "model_details":     model_details,
        "ai_consensus":      ai_consensus,
        "market_price":      price,
        "alpha_gap":         alpha_gap,
        "signal_strength":   signal_strength,
        "signal_direction":  signal_direction,
        "trade_decision":    trade_decision,
        "trade_side":        trade_side,
        "elapsed":           elapsed,
    })


if __name__ == "__main__":
    port = int(os.getenv("PORT", 5562))
    host = "0.0.0.0" if os.getenv("PORT") else "127.0.0.1"
    app.run(host=host, port=port, debug=not os.getenv("PORT"))
