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
| claude | Claude Sonnet 4.5 | Anthropic | ANTHROPIC_API_KEY |
| gemini | Gemini 2.5 Flash | Google | GOOGLE_API_KEY |
| grok | Grok 3 Mini | xAI | GROK_API_KEY |
| mistral | Mistral Small | Mistral | MISTRAL_API_KEY |
| gemma | Gemma 4 | Google | GEMMA_API_KEY (falls back to GOOGLE_API_KEY) |
| deepseek | DeepSeek R1 | DeepSeek | DEEPSEEK_API_KEY (currently benched — enabled: False in app.py) |

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
- Free: 3 trial syntheses (localStorage counter — see FREE_COUNT_KEY in index.html)
- Base Synthesis: $18/mo — 100 syntheses/month
- Foundational Synthesis: $88/mo — 1,000 syntheses/month
- Stripe webhook → token generated → Resend emails token to subscriber
- Token stored in browser localStorage, sent with each /ask request

## Railway Environment Variables
- OPENAI_API_KEY, ANTHROPIC_API_KEY, GOOGLE_API_KEY, GROK_API_KEY, MISTRAL_API_KEY
- STRIPE_SECRET_KEY, STRIPE_WEBHOOK_SECRET (required — webhook now fails closed without it), RESEND_API_KEY, FROM_EMAIL
- DB_PATH=/data/5i.db (Railway volume mounted at /data)
- ADMIN_KEY — required for /admin/issue-token and /kalshi-fusion/order (both fail closed if unset, 2026-07-31 security pass)
- OWNER_TOKEN — optional unlimited-use bypass token for verify_token(); the old hardcoded value was rotated out of source, must be set fresh if this bypass is still wanted

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

## Last Session
2026-07-08 — V2 UI rebuilt from scratch on Fable 5 (V1 archived at branch `archive/v1-ui` + tag `v1-ui`). Machined-rack aesthetic per locked brand system: neutral charcoal chassis, silver hairlines, vivid green signal only. All 103 element IDs + JS contract preserved verbatim — zero logic changes. Revived the built-but-unwired RESEARCH toggle in the dev toolbar. Fixed: /verdict endpoint deadlocked locally (gevent monkey.patch_all now gated to RAILWAY_ENVIRONMENT only — local pip install of requirements.txt had introduced gevent which deadlocks aiohttp under the dev server; prod unchanged, verified working before+after). E2E verified: 5-model synthesis, verdict, gauges, tabs, light theme, mobile drawer. New Mistral key wired (old one dead since ~7/8 09:00).
