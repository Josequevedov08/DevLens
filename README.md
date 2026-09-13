# DevLens

**Instant audits for any GitHub repository.**

DevLens takes a single GitHub repo link and turns it into a clean, one-page dashboard: a
2-sentence summary of what the project actually does, a complexity score, a risk rating
(Safe / Moderate / Warning), the key setup warnings, and a copy-pasteable install guide,
all generated in seconds by AI.

---

## Inspiration

Hackathon judges see dozens of repos in a single afternoon. Most of that time is spent
just figuring out what a project even does and whether it actually runs, before any real
judging can happen. We built DevLens to compress that discovery phase from minutes to
seconds, so judges can spend their limited time evaluating ideas instead of untangling
READMEs and `requirements.txt` files.

## What it does

1. **Paste a GitHub URL.** Any public repository.
2. **DevLens reads the repo.** It pulls metadata, file tree, README, languages,
   contributors, license, and dependency count straight from the GitHub API, no cloning
   required.
3. **AI forms a verdict.** That signal is handed to an LLM which returns a structured
   verdict: summary, complexity score, risk level, tech stack, highlights, warnings, and
   install steps.
4. **Judge-friendly dashboard.** The frontend renders it all as clean, color-coded cards,
   readable in under 10 seconds, with a direct link back to the source repository.

### Dashboard includes
- **Judge verdict banner** stating the risk rating up front.
- **Project summary**, exactly two sentences, no fluff.
- **Complexity score**, a 1 to 10 gauge that shifts from green to red as complexity rises.
- **Repo stats**: stars, forks, open issues, contributors, license, last commit activity,
  and dependency count.
- **Highlights and warnings**, positive signals and risk signals shown side by side, each
  warning tagged with a severity (high, medium, low) so real blockers stand out.
- **Quick install guide**, an interactive numbered stepper with a copy button per step.

## Architecture

DevLens is intentionally a lightweight monolith, built to be fast to ship and easy to
demo within a hackathon's time constraints.

```
Browser (Tailwind + vanilla JS)
        |  fetch("/api/analyze")
        v
FastAPI backend (app/main.py)
        |  1. Parse the GitHub URL
        |  2. Pull metadata, file tree, README, languages, contributors, license,
        |     dependency count via the GitHub REST API
        |  3. Send that signal to an LLM with a strict JSON schema prompt
        v
Google Gemini (generateContent API)
        |  returns structured JSON verdict
        v
Dashboard renders the verdict as cards and badges
```

**Stack:**
- **Backend:** FastAPI (serves both the JSON API and the static frontend)
- **Frontend:** Single `index.html` with Tailwind CSS (CDN) and vanilla JavaScript, no
  build step, no framework overhead
- **AI:** Google Gemini (`gemini-2.5-flash`) for the final verdict generation, satisfying
  the hackathon's official AI requirement. Groq was used during development for fast,
  free iteration on the prompt and error handling before the final swap to Gemini.
- **Data source:** GitHub REST API (public, no cloning, no auth required for public repos)

## Running locally

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
GITHUB_TOKEN=strongly_recommended_see_note_below
```

> **About `GITHUB_TOKEN`:** DevLens pulls a lot of signal per analysis (metadata, file
> tree, README, languages, contributors, license, dependency file), which adds up to
> several GitHub API calls per repo. Unauthenticated requests are capped at 60 per hour,
> which is easy to exhaust during a demo. A free
> [personal access token](https://github.com/settings/tokens) (no scopes needed for
> public repos) raises that to 5,000 per hour.

Run the server:

```bash
uvicorn app.main:app --reload
```

Open **http://localhost:8000** and paste in any public GitHub repo URL.

> Want to iterate faster during development? Set `AI_PROVIDER=groq` and add a
> [`GROQ_API_KEY`](https://console.groq.com/keys) instead, same code path, same JSON
> schema, just a faster and free provider for local testing. Flip it back to `gemini`
> before submitting.

## Security

- API keys are never hardcoded and are loaded exclusively via `python-dotenv` from a
  local `.env` file (excluded from version control via `.gitignore`). See
  `.env.example` for the full list of required variables.
- Errors returned to the client never leak upstream provider responses or keys.
- `/api/analyze` is rate-limited per IP (in-memory, 10 requests per minute) to protect
  against runaway AI-key spend.
- Security response headers (`X-Content-Type-Options`, `X-Frame-Options`,
  `Referrer-Policy`, `Permissions-Policy`) are set on every response.
- DevLens does not use cookies, accounts, or any persistent storage. See the in-app
  [Privacy Policy](app/static/privacy.html), [Terms of Service](app/static/terms.html),
  and [Cookie Policy](app/static/cookies.html), also linked in the app's footer.

## What's next

- Cache repeated analyses to cut down on API calls
- Support private repos via OAuth
- Add a shareable or exportable judge report (PDF or link)
- Score trends across multiple submissions for the same team

## License

MIT

## Changelog

### v1, Initial MVP
- FastAPI backend and a Groq/Gemini-switchable AI layer (`AI_PROVIDER` env var).
- Single-page Tailwind dashboard: summary, complexity bar, warnings list, install guide.
- `.env` / `.env.example`, `.gitignore`, Devpost-ready README.

### v2, Judge dashboard revamp
- Complexity gauge now color-codes green to amber to red by score instead of a flat bar.
- Warnings carry a severity (high, medium, low) and render as bordered, colored alert
  rows instead of a flat list, plus a separate highlights panel for positive signals.
- Install guide became an interactive numbered stepper with a per-step copy button.
- Signal pulled from GitHub expanded significantly: license, stars, forks, open issues,
  contributors count, top languages, dependency count, last-commit recency, and a direct
  link back to the repository.
- Added `/privacy`, `/terms`, `/cookies` pages linked from the footer.
- Added basic hardening: per-IP rate limiting on `/api/analyze` and security response
  headers.

### v3, Visual and content pass
- Full visual redesign to a clean, light, professional look inspired by the Arka design
  system: Inter typeface, soft-bordered white cards, a single blue accent color, and
  hand-drawn line icons in place of emoji throughout the app.
- Removed every emoji and em dash from the UI, README, and legal pages for a more
  professional tone.
- Added an explicit "Back to DevLens" link on every legal page.
- Added a "How this score is calculated" panel under the complexity gauge, listing the
  5 factors (codebase size, dependencies, architecture, tech stack diversity, setup
  effort) the AI is now required to justify with a per-repo note.
- Fixed the install guide's code blocks, which used a black terminal look that clashed
  with the rest of the light UI, to match the page's card style.
- Dropped "judge-friendly" from the tagline.
