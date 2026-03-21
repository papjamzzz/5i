import os
import asyncio
import aiohttp
import requests as req_lib
from flask import Flask, render_template, request, jsonify, Response, stream_with_context
from dotenv import load_dotenv
import time

load_dotenv()

app = Flask(__name__)

OPENAI_KEY    = os.getenv("OPENAI_API_KEY", "")
ANTHROPIC_KEY = os.getenv("ANTHROPIC_API_KEY", "")
GOOGLE_KEY    = os.getenv("GOOGLE_API_KEY", "")
GROK_KEY      = os.getenv("GROK_API_KEY", "")
MISTRAL_KEY   = os.getenv("MISTRAL_API_KEY", "")

MAX_INPUT_CHARS = 500

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


# ── Individual model callers ──────────────────────────────────────────────────

async def call_openai(session, prompt):
    try:
        async with session.post(
            "https://api.openai.com/v1/chat/completions",
            headers={"Authorization": f"Bearer {OPENAI_KEY}", "Content-Type": "application/json"},
            json={"model": "gpt-4o", "messages": [{"role": "user", "content": prompt}], "max_tokens": 800},
            timeout=aiohttp.ClientTimeout(total=30)
        ) as r:
            data = await r.json()
            return data["choices"][0]["message"]["content"]
    except Exception as e:
        return f"Error: {str(e)}"


async def call_anthropic(session, prompt):
    try:
        async with session.post(
            "https://api.anthropic.com/v1/messages",
            headers={
                "x-api-key": ANTHROPIC_KEY,
                "anthropic-version": "2023-06-01",
                "Content-Type": "application/json"
            },
            json={"model": "claude-3-5-sonnet-20241022", "max_tokens": 800,
                  "messages": [{"role": "user", "content": prompt}]},
            timeout=aiohttp.ClientTimeout(total=30)
        ) as r:
            data = await r.json()
            return data["content"][0]["text"]
    except Exception as e:
        return f"Error: {str(e)}"


async def call_gemini(session, prompt):
    try:
        url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-1.5-flash:generateContent?key={GOOGLE_KEY}"
        async with session.post(
            url,
            headers={"Content-Type": "application/json"},
            json={"contents": [{"parts": [{"text": prompt}]}]},
            timeout=aiohttp.ClientTimeout(total=30)
        ) as r:
            data = await r.json()
            return data["candidates"][0]["content"]["parts"][0]["text"]
    except Exception as e:
        return f"Error: {str(e)}"


async def call_grok(session, prompt):
    try:
        async with session.post(
            "https://api.x.ai/v1/chat/completions",
            headers={"Authorization": f"Bearer {GROK_KEY}", "Content-Type": "application/json"},
            json={"model": "grok-4-1-fast", "messages": [{"role": "user", "content": prompt}], "max_tokens": 800},
            timeout=aiohttp.ClientTimeout(total=30)
        ) as r:
            data = await r.json()
            return data["choices"][0]["message"]["content"]
    except Exception as e:
        return f"Error: {str(e)}"


async def call_mistral(session, prompt):
    try:
        async with session.post(
            "https://api.mistral.ai/v1/chat/completions",
            headers={"Authorization": f"Bearer {MISTRAL_KEY}", "Content-Type": "application/json"},
            json={"model": "mistral-large-latest", "messages": [{"role": "user", "content": prompt}], "max_tokens": 800},
            timeout=aiohttp.ClientTimeout(total=30)
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

SYNTHESIS_PROMPT = """You are a neutral synthesis engine. Multiple AI models have answered the same question.
Your job is to produce ONE clear, unified answer by:
1. Identifying what all models agree on (core consensus)
2. Noting any meaningful disagreements or unique insights
3. Delivering a final synthesized answer that is more reliable than any single response

Original question: {question}

Model responses:
{responses}

Output format:
**Consensus:** (1-2 sentences — what all models agreed on)
**Key Disagreements:** (brief — only if meaningful, otherwise "None significant")
**Influence:** (which model(s) contributed most to the final answer and why)
**Verdict:** (the final synthesized answer — clear, direct, authoritative)"""


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
        question=question,
        responses=responses_text
    )

    # Use best available judge in priority order
    if OPENAI_KEY:
        return await call_openai(session, full_prompt)
    elif ANTHROPIC_KEY:
        return await call_anthropic(session, full_prompt)
    elif GOOGLE_KEY:
        return await call_gemini(session, full_prompt)
    else:
        return "Error: Need at least one API key to render a verdict."


# ── Async orchestration ───────────────────────────────────────────────────────

async def query_all(prompt, selected):
    async with aiohttp.ClientSession() as session:
        tasks = {k: asyncio.create_task(CALLERS[k](session, prompt)) for k in selected}
        results = {k: await t for k, t in tasks.items()}
        return results


async def query_all_with_verdict(prompt, selected):
    async with aiohttp.ClientSession() as session:
        tasks = {k: asyncio.create_task(CALLERS[k](session, prompt)) for k in selected}
        results = {k: await t for k, t in tasks.items()}
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
    data = request.json
    prompt = data.get("prompt", "").strip()[:MAX_INPUT_CHARS]
    want_verdict = data.get("verdict", False)
    selected = [k for k in data.get("models", list(MODELS.keys()))
                if k in MODELS and MODELS[k]["enabled"]()]

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
    def generate():
        try:
            with req_lib.post(upstream_url, headers=upstream_headers,
                              json=upstream_body, stream=True, timeout=60) as r:
                for chunk in r.iter_content(chunk_size=None):
                    if chunk:
                        yield chunk
        except Exception as e:
            yield f"data: {{\"error\": \"{str(e)}\"}}\n\n".encode()
    return Response(stream_with_context(generate()), content_type='text/event-stream')


@app.route('/proxy/claude', methods=['POST'])
def proxy_claude():
    d = request.json
    return _stream_proxy(
        'https://api.anthropic.com/v1/messages',
        {'x-api-key': ANTHROPIC_KEY, 'anthropic-version': '2023-06-01',
         'content-type': 'application/json'},
        {'model': 'claude-3-5-haiku-20241022', 'max_tokens': d.get('maxTokens', 600),
         'stream': True, 'system': d.get('sysPrompt', ''),
         'messages': [{'role': 'user', 'content': d.get('userPrompt', '')}]}
    )


@app.route('/proxy/gpt', methods=['POST'])
def proxy_gpt():
    d = request.json
    return _stream_proxy(
        'https://api.openai.com/v1/chat/completions',
        {'Authorization': f'Bearer {OPENAI_KEY}', 'Content-Type': 'application/json'},
        {'model': 'gpt-4o', 'max_tokens': d.get('maxTokens', 600), 'stream': True,
         'messages': [{'role': 'system', 'content': d.get('sysPrompt', '')},
                      {'role': 'user', 'content': d.get('userPrompt', '')}]}
    )


@app.route('/proxy/gemini', methods=['POST'])
def proxy_gemini():
    d = request.json
    url = f'https://generativelanguage.googleapis.com/v1beta/models/gemini-2.0-flash:streamGenerateContent?alt=sse&key={GOOGLE_KEY}'
    return _stream_proxy(
        url,
        {'Content-Type': 'application/json'},
        {'system_instruction': {'parts': [{'text': d.get('sysPrompt', '')}]},
         'contents': [{'role': 'user', 'parts': [{'text': d.get('userPrompt', '')}]}],
         'generationConfig': {'maxOutputTokens': d.get('maxTokens', 600), 'temperature': 0.7}}
    )


@app.route('/proxy/mistral', methods=['POST'])
def proxy_mistral():
    d = request.json
    return _stream_proxy(
        'https://api.mistral.ai/v1/chat/completions',
        {'Authorization': f'Bearer {MISTRAL_KEY}', 'Content-Type': 'application/json'},
        {'model': 'mistral-small-latest', 'max_tokens': d.get('maxTokens', 600), 'stream': True,
         'messages': [{'role': 'system', 'content': d.get('sysPrompt', '')},
                      {'role': 'user', 'content': d.get('userPrompt', '')}]}
    )


@app.route('/proxy/grok', methods=['POST'])
def proxy_grok():
    d = request.json
    return _stream_proxy(
        'https://api.x.ai/v1/chat/completions',
        {'Authorization': f'Bearer {GROK_KEY}', 'Content-Type': 'application/json'},
        {'model': 'grok-3-mini-beta', 'max_tokens': d.get('maxTokens', 600), 'stream': True,
         'messages': [{'role': 'system', 'content': d.get('sysPrompt', '')},
                      {'role': 'user', 'content': d.get('userPrompt', '')}]}
    )


if __name__ == "__main__":
    port = int(os.getenv("PORT", 5562))
    host = "0.0.0.0" if os.getenv("PORT") else "127.0.0.1"
    app.run(host=host, port=port, debug=not os.getenv("PORT"))
