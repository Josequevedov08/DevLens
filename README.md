# 🔍 DevLens

**Instant, judge-friendly audits for any GitHub repository.**

DevLens takes a single GitHub repo link and turns it into a clean, one-page dashboard: a
2-sentence summary of what the project actually does, a complexity score, a risk badge
(Safe / Moderate / Warning), the key setup warnings, and a copy-pasteable install guide —
all generated in seconds by AI.

---

## 💡 Inspiration

Hackathon judges see dozens of repos in a single afternoon. Most of that time is spent
just figuring out *what a project even does* and *whether it actually runs* — before any
real judging can happen. We built DevLens to compress that discovery phase from minutes
to seconds, so judges can spend their limited time evaluating ideas instead of untangling
READMEs and `requirements.txt` files.

## ⚙️ What it does

1. **Paste a GitHub URL.** Any public repository.
2. **DevLens reads the repo.** It pulls the metadata, file tree, and README straight from
   the GitHub API — no cloning required.
3. **AI forms a verdict.** That signal is handed to an LLM which returns a structured
   verdict: summary, complexity score, risk level, warnings, and install steps.
4. **Judge-friendly dashboard.** The frontend renders it all as clean cards and badges —
   readable in under 10 seconds.

### Dashboard includes
- 📝 **Project Summary** — exactly two sentences, no fluff.
- 📊 **Complexity Score** — a 1–10 gauge of how involved the codebase is.
- 🚦 **Risk Badge** — Safe, Moderate, or Warning at a glance.
- ⚠️ **Key Warnings** — missing env vars, no tests, unclear setup, etc.
- 🚀 **Quick Install Guide** — ordered, copy-pasteable steps to run it locally.

## 🏗️ Architecture

DevLens is intentionally a lightweight monolith — built to be fast to ship and easy to
demo within a hackathon's time constraints.

```
Browser (Tailwind + vanilla JS)
        │  fetch("/api/analyze")
        ▼
FastAPI backend (app/main.py)
        │  1. Parse the GitHub URL
        │  2. Pull metadata + file tree + README via the GitHub REST API
        │  3. Send that signal to an LLM with a strict JSON schema prompt
        ▼
Google Gemini (generateContent API)
        │  returns structured JSON verdict
        ▼
Dashboard renders the verdict as cards + badges
```

**Stack:**
- **Backend:** FastAPI (serves both the JSON API and the static frontend)
- **Frontend:** Single `index.html` with Tailwind CSS (CDN) + vanilla JavaScript — no
  build step, no framework overhead
- **AI:** Google Gemini (`gemini-2.5-flash`) for the final verdict generation, satisfying
  the hackathon's official AI requirement. Groq was used during development for fast,
  free iteration on the prompt and error handling before the final swap to Gemini.
- **Data source:** GitHub REST API (public, no cloning, no auth required for public repos)

## 🚀 Running locally

### Prerequisites
- Python 3.10+
- A [Google Gemini API key](https://aistudio.google.com/apikey) (free tier works)

### Setup

```bash
git clone https://github.com/Josequevedov08/DevLens.git
cd DevLens
pip install -r requirements.txt
```

Copy the example environment file and fill in your key:

```bash
cp .env.example .env
```

```env
AI_PROVIDER=gemini
GEMINI_API_KEY=your_key_here
GITHUB_TOKEN=optional_but_avoids_rate_limits
```

Run the server:

```bash
uvicorn app.main:app --reload
```

Open **http://localhost:8000** and paste in any public GitHub repo URL.

> Want to iterate faster during development? Set `AI_PROVIDER=groq` and add a
> [`GROQ_API_KEY`](https://console.groq.com/keys) instead — same code path, same JSON
> schema, just a faster/free provider for local testing. Flip it back to `gemini` before
> submitting.

## 🔒 Security

API keys are never hardcoded and are loaded exclusively via `python-dotenv` from a local
`.env` file (excluded from version control via `.gitignore`). See `.env.example` for the
full list of required variables.

## 🧭 What's next

- Cache repeated analyses to cut down on API calls
- Support private repos via OAuth
- Add a shareable/exportable judge report (PDF or link)
- Score trends across multiple submissions for the same team

## 📄 License

MIT
