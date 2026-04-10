<p align="center">
  <img src="static/logo.png" width="120" alt="5i logo"/>
</p>

# 5i — Five Intelligences

**One prompt. Five AI models. One synthesized verdict.**

5i sends your question to GPT-4o, Claude, Gemini, Grok, and DeepSeek simultaneously — in parallel, not sequence — then passes all five responses to a synthesis engine that identifies consensus, flags disagreements, and delivers a single authoritative verdict.

It's not a chatbot. It's a thinking machine.

---

## What It Does

- **Parallel execution** — all five models fire at once via async HTTP. No waiting in line.
- **Synthesis pass** — after responses arrive, a judge model reads all five and produces one unified answer: what they agreed on, what they disagreed on, and the final verdict.
- **Toggle any model** — disable individual models per query. Run 2 or 5, your call.
- **Graceful degradation** — missing API keys auto-disable that model. App runs with whatever you have.
- **500-char input cap** — keeps costs predictable.

---

## Models

| Key | Model | Provider | Env Var |
|-----|-------|----------|---------|
| gpt | GPT-4o | OpenAI | `OPENAI_API_KEY` |
| claude | Claude 3.5 Sonnet | Anthropic | `ANTHROPIC_API_KEY` |
| gemini | Gemini 1.5 Flash | Google | `GOOGLE_API_KEY` |
| grok | Grok 2 | xAI | `GROK_API_KEY` |
| deepseek | DeepSeek R1 | DeepSeek | `DEEPSEEK_API_KEY` |

---

## Setup

```bash
cd ~/5i
cp .env.example .env
# add your API keys to .env
make setup
make run
```

Opens at `http://127.0.0.1:5562`

---

## Stack

Python · Flask · aiohttp · asyncio · Vanilla JS · JetBrains Mono

No external UI frameworks. No databases. No tracking. Runs local.

---

## Part of Creative Konsoles

5i is the flagship product of [Creative Konsoles](https://creativekonsoles.com) — tools built using thought.

> "Not which AI is right. What the truth is when you ask all of them."

**[creativekonsoles.com](https://creativekonsoles.com)** &nbsp;·&nbsp; support@creativekonsoles.com

<!-- repo maintenance: 2026-04-10 -->
