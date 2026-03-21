# 5i — Re-Entry File
*Re-entry: 5i*

## What This Is
Multi-model AI query engine. One prompt → up to 5 major AI models simultaneously → parallel responses side by side. Codename "5i" (Five Intelligences). Final name TBD.

## Re-Entry Phrase
"Re-entry: 5i"

## Current Status
Scaffold complete. UI built. Awaiting API keys to go live.

## File Structure
```
5i/
├── app.py              ← Flask, port 5561
├── templates/index.html
├── static/logo.png     ← Diffuse dark logo from Desktop
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
make run     # starts on http://127.0.0.1:5561
```

## Models Wired
| Key | Model | Provider | Env Var |
|-----|-------|----------|---------|
| gpt | GPT-4o | OpenAI | OPENAI_API_KEY |
| claude | Claude 3.5 Sonnet | Anthropic | ANTHROPIC_API_KEY |
| gemini | Gemini 1.5 Flash | Google | GOOGLE_API_KEY |
| grok | Grok 2 | xAI | GROK_API_KEY |
| deepseek | DeepSeek R1 | DeepSeek | DEEPSEEK_API_KEY |

## What's Done
- Full dark UI with diffuse logo treatment (background + header mark)
- Parallel async API calls (aiohttp) to all 5 models
- Per-model toggle buttons with color coding
- "The jury is deliberating…" loading state with 5-dot animation
- Verdict bar shows model count + response time
- 500-char input limit for cost control
- Enter to submit, Shift+Enter for newline
- Graceful error display per card if a model fails

## What's Next
- Add API keys to .env to go live
- Finalize product name (placeholder: 5i)
- Optional: synthesis pass — feed all 5 responses into one model for a unified verdict
- Optional: freemium gating (3 models free, 5 models paid)
- GitHub repo: papjamzzz/5i (not created yet)

## Key Technical Decisions
- asyncio.run() + aiohttp for true parallel calls (not sequential)
- 500 char limit baked into both frontend and backend
- Models auto-disable in UI if API key missing (no crashes)
- Logo is 32x32 RGBA PNG scaled to 600px in background with blur+brightness for diffuse effect

## Port
5561

## GitHub
Not created yet — pending name decision.

---
*Last updated: 2026-03-18*
