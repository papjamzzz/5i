# 5i — Re-Entry File
*Re-entry: 5i*

## What This Is
Multi-model AI synthesis engine. One prompt → up to 5 major AI models simultaneously → parallel responses + unified synthesis verdict. Live at creativekonsoles.com. Subscription product of Creative Konsoles.

## Re-Entry Phrase
"Re-entry: 5i"

## Current Status
LIVE on Railway. Subscription system active. Stripe + Resend + SQLite wired.

## File Structure
```
5i/
├── app.py              ← Flask, port 5562
├── templates/
│   ├── index.html      ← Main UI
│   └── konsole.html    ← Console mode
├── static/logo.png
├── requirements.txt
├── Makefile
├── launch.command
├── .env                ← API keys go here (gitignored)
├── .env.example
└── CLAUDE.md
```

## How to Run
```bash
cd ~/5i
make setup   # first time only
make run     # starts on http://127.0.0.1:5562
```

## Models Wired
| Key | Model | Provider | Env Var |
|-----|-------|----------|---------|
| gpt | GPT-4o | OpenAI | OPENAI_API_KEY |
| claude | Claude 3.5 Sonnet | Anthropic | ANTHROPIC_API_KEY |
| gemini | Gemini 1.5 Flash | Google | GOOGLE_API_KEY |
| grok | Grok 2 | xAI | GROK_API_KEY |
| mistral | Mistral Large | Mistral | MISTRAL_API_KEY |

## What's Built
- Full dark UI + Konsole Mode (opens as independent window)
- Parallel async API calls (asyncio.gather + aiohttp) — true parallel, not sequential
- Token-level streaming via /proxy/* routes (SSE)
- Per-model toggle buttons with color coding
- Synthesis pass — feeds all responses into Gemini-first judge model
- Plan status bar (Free trial / Base / Foundational)
- Subscription token gate — SQLite DB, Stripe webhook, Resend email
- 500-char input limit (frontend + backend)
- Mobile responsive — off-canvas drawer on small screens
- BYOK support — users can bring own API keys

## Subscription System
- Free: 5 trial syntheses (localStorage counter)
- Base Synthesis: $20/mo — 100 syntheses/month
- Foundational Synthesis: $89/mo — 1,000 syntheses/month
- Stripe webhook → token generated → Resend emails token to subscriber
- Token stored in browser localStorage, sent with each /ask request

## Railway Environment Variables
- OPENAI_API_KEY, ANTHROPIC_API_KEY, GOOGLE_API_KEY, GROK_API_KEY, MISTRAL_API_KEY
- STRIPE_WEBHOOK_SECRET, RESEND_API_KEY, FROM_EMAIL
- DB_PATH=/data/5i.db (Railway volume mounted at /data)

## Key Technical Decisions
- asyncio.gather for true parallel model calls
- Gemini-first synthesis judge (fastest)
- MAX_TOKENS=500 (model calls), MAX_TOKENS_SYNTH=900 (synthesis)
- SQLite at /data/5i.db (Railway persistent volume)
- hmac-based Stripe webhook signature verification (no stripe SDK)

## Port
5562

## GitHub
https://github.com/papjamzzz/5i — live, public

## Railway
https://web-production-94a13.up.railway.app

---
*Last updated: 2026-03-23*
