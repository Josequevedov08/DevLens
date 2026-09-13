"""
DevLens - instant, judge-friendly audits of GitHub repositories.

Flow:
  1. Take a GitHub repo URL.
  2. Pull rich signal from the GitHub REST API (metadata, file tree, README,
     languages, contributors, license, activity, dependency count).
  3. Send that signal to an LLM and ask for a strict JSON verdict.
  4. Serve the verdict + raw signal to the frontend for a judge-style dashboard.

AI PROVIDER SWITCH (read this before demo day):
  - `AI_PROVIDER=groq`   -> uses Groq's OpenAI-compatible chat completions endpoint.
                            Fast + free, great for building/testing.
  - `AI_PROVIDER=gemini` -> uses Google's Gemini generateContent endpoint.
                            REQUIRED for the final hackathon submission.
  Both paths live in `call_ai()` below. Flipping the env var is the only
  change needed - no other code in this file references the provider.
"""

import base64
import json
import os
import re
import time
from datetime import datetime, timezone

import httpx
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

load_dotenv()

GITHUB_TOKEN = os.getenv("GITHUB_TOKEN", "")
GROQ_API_KEY = os.getenv("GROQ_API_KEY", "")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "")
AI_PROVIDER = os.getenv("AI_PROVIDER", "groq").lower()

GROQ_URL = "https://api.groq.com/openai/v1/chat/completions"
GROQ_MODEL = "openai/gpt-oss-120b"

# Standard generative language endpoint - swap model name only if Google renames it.
GEMINI_URL = (
    "https://generativelanguage.googleapis.com/v1beta/models/"
    "gemini-2.5-flash:generateContent"
)

app = FastAPI(title="DevLens")

STATIC_DIR = os.path.join(os.path.dirname(__file__), "static")
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


class AnalyzeRequest(BaseModel):
    repo_url: str


# ---------------------------------------------------------------------------
# Security: basic headers + a lightweight in-memory rate limiter.
#
# This is a hackathon MVP behind a single process, so an in-memory limiter is
# enough to stop accidental abuse / runaway AI-key spend. If DevLens ever runs
# with multiple workers or instances, swap this for a shared store (Redis).
# ---------------------------------------------------------------------------

RATE_LIMIT_MAX_REQUESTS = 10
RATE_LIMIT_WINDOW_SECONDS = 60
_request_log: dict[str, list[float]] = {}


def check_rate_limit(client_ip: str) -> None:
    now = time.time()
    window_start = now - RATE_LIMIT_WINDOW_SECONDS
    recent = [t for t in _request_log.get(client_ip, []) if t > window_start]
    if len(recent) >= RATE_LIMIT_MAX_REQUESTS:
        raise HTTPException(429, "Too many requests. Please wait a minute and try again.")
    recent.append(now)
    _request_log[client_ip] = recent


@app.middleware("http")
async def security_headers(request: Request, call_next):
    response = await call_next(request)
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
    response.headers["Permissions-Policy"] = "geolocation=(), microphone=(), camera=()"
    return response


# ---------------------------------------------------------------------------
# GitHub ingestion
# ---------------------------------------------------------------------------

def parse_repo_url(url: str) -> tuple[str, str]:
    match = re.search(r"github\.com/([^/]+)/([^/#?]+)", url.strip())
    if not match:
        raise HTTPException(400, "That doesn't look like a valid GitHub repo URL.")
    owner, repo = match.group(1), match.group(2)
    return owner, repo.removesuffix(".git")


def gh_headers() -> dict:
    headers = {"Accept": "application/vnd.github+json"}
    if GITHUB_TOKEN:
        headers["Authorization"] = f"Bearer {GITHUB_TOKEN}"
    return headers


def decode_b64(content: str) -> str:
    try:
        return base64.b64decode(content).decode("utf-8", errors="ignore")
    except Exception:
        return ""


async def fetch_file_raw(client: httpx.AsyncClient, base: str, path: str) -> str | None:
    res = await client.get(f"{base}/contents/{path}")
    if res.status_code != 200:
        return None
    data = res.json()
    if data.get("encoding") == "base64":
        return decode_b64(data.get("content", ""))
    return None


def days_since(iso_timestamp: str | None) -> int | None:
    if not iso_timestamp:
        return None
    try:
        dt = datetime.fromisoformat(iso_timestamp.replace("Z", "+00:00"))
        return (datetime.now(timezone.utc) - dt).days
    except Exception:
        return None


async def fetch_repo_signal(owner: str, repo: str) -> dict:
    base = f"https://api.github.com/repos/{owner}/{repo}"
    async with httpx.AsyncClient(
        headers=gh_headers(), timeout=15, follow_redirects=True
    ) as client:
        repo_res = await client.get(base)
        if repo_res.status_code == 404:
            raise HTTPException(404, "Repository not found (is it public?).")
        if repo_res.status_code == 403 and repo_res.headers.get("x-ratelimit-remaining") == "0":
            raise HTTPException(
                429,
                "GitHub API rate limit reached for this server. Add a GITHUB_TOKEN to "
                ".env to raise the limit from 60 to 5,000 requests/hour, then try again.",
            )
        if repo_res.status_code != 200:
            raise HTTPException(502, "GitHub API error while fetching repo metadata.")
        meta = repo_res.json()

        default_branch = meta.get("default_branch", "main")

        tree_res = await client.get(
            f"{base}/git/trees/{default_branch}", params={"recursive": "1"}
        )
        file_paths: list[str] = []
        if tree_res.status_code == 200:
            tree = tree_res.json().get("tree", [])
            file_paths = [item["path"] for item in tree if item.get("type") == "blob"]

        # --- Everything below is independent, so fetch it concurrently. ---
        import asyncio

        async def get_readme():
            res = await client.get(f"{base}/readme")
            if res.status_code == 200:
                return decode_b64(res.json().get("content", ""))
            return ""

        async def get_languages():
            url = meta.get("languages_url")
            if not url:
                return []
            res = await client.get(url)
            if res.status_code != 200:
                return []
            langs = res.json()
            total = sum(langs.values()) or 1
            ranked = sorted(langs.items(), key=lambda kv: kv[1], reverse=True)[:4]
            return [{"name": n, "pct": round(v / total * 100)} for n, v in ranked]

        async def get_contributors_count():
            res = await client.get(
                f"{base}/contributors", params={"per_page": 1, "anon": "true"}
            )
            if res.status_code != 200:
                return None
            link = res.headers.get("link", "")
            match = re.search(r'page=(\d+)>;\s*rel="last"', link)
            if match:
                return int(match.group(1))
            return len(res.json())

        async def get_dependency_info():
            if "package.json" in file_paths:
                content = await fetch_file_raw(client, base, "package.json")
                if content:
                    try:
                        pkg = json.loads(content)
                        count = len(pkg.get("dependencies", {})) + len(
                            pkg.get("devDependencies", {})
                        )
                        return {"count": count, "source": "package.json"}
                    except Exception:
                        pass
            if "requirements.txt" in file_paths:
                content = await fetch_file_raw(client, base, "requirements.txt")
                if content:
                    lines = [
                        ln for ln in content.splitlines()
                        if ln.strip() and not ln.strip().startswith("#")
                    ]
                    return {"count": len(lines), "source": "requirements.txt"}
            return {"count": None, "source": None}

        readme_text, top_languages, contributors_count, dependency_info = (
            await asyncio.gather(
                get_readme(),
                get_languages(),
                get_contributors_count(),
                get_dependency_info(),
            )
        )

    license_info = meta.get("license") or {}

    return {
        "name": meta.get("full_name"),
        "html_url": meta.get("html_url"),
        "avatar_url": (meta.get("owner") or {}).get("avatar_url"),
        "description": meta.get("description") or "",
        "language": meta.get("language") or "",
        "stars": meta.get("stargazers_count", 0),
        "forks": meta.get("forks_count", 0),
        "open_issues": meta.get("open_issues_count", 0),
        "watchers": meta.get("subscribers_count", 0),
        "license": license_info.get("spdx_id") if license_info else None,
        "topics": meta.get("topics", []),
        "created_at": meta.get("created_at"),
        "pushed_at": meta.get("pushed_at"),
        "days_since_last_commit": days_since(meta.get("pushed_at")),
        "contributors_count": contributors_count,
        "top_languages": top_languages,
        "dependency_count": dependency_info["count"],
        "dependency_source": dependency_info["source"],
        "file_count": len(file_paths),
        "top_files": file_paths[:120],
        "has_dockerfile": any("dockerfile" in p.lower() for p in file_paths),
        "has_tests": any("test" in p.lower() for p in file_paths),
        "has_ci": any(p.startswith(".github/workflows") for p in file_paths),
        "has_env_example": any(
            p.lower() in (".env.example", ".env.sample") for p in file_paths
        ),
        "readme_excerpt": readme_text[:4000],
    }


# ---------------------------------------------------------------------------
# AI layer
# ---------------------------------------------------------------------------

SYSTEM_PROMPT = """You are DevLens, an auditor that helps a hackathon judge quickly form \
a verdict on a GitHub repository without reading the code. You will be given structured \
signal about a repo: metadata, activity stats, license, languages, dependency count, file \
list, and a README excerpt. Respond with ONLY a valid JSON object, no markdown fences, no \
prose, matching exactly this schema:

{
  "summary": "Exactly 2 sentences describing what the project does and how it's built.",
  "complexity_score": <integer 1-10, 1 = trivial, 10 = extremely complex>,
  "risk_level": "Safe" | "Moderate" | "Warning",
  "tech_stack": ["short tags for the languages/frameworks/services actually used, max 6"],
  "highlights": ["short positive callouts a judge would want to know, max 5"],
  "warnings": [
    {"severity": "high" | "medium" | "low", "text": "short, specific warning"}
  ],
  "install_steps": ["ordered, concise, copy-pasteable steps to run the project locally"]
}

Rules:
- Use the provided signal (license presence, contributors_count, days_since_last_commit,
  has_tests, has_ci, has_dockerfile, has_env_example, dependency_count) to justify your
  warnings and highlights instead of guessing.
- risk_level "Warning" if setup looks broken/unclear, secrets seem required with no example
  env file, or the repo looks abandoned/undocumented; "Moderate" if there are some gaps (no
  tests, no CI, stale activity); "Safe" if it looks clean, documented, and maintained.
- "warnings" severity: "high" for things that would block a judge from running it (missing
  env vars with no example, broken/unclear install path), "medium" for real gaps (no tests,
  no CI, no license), "low" for minor nitpicks. Empty array if nothing notable.
- "highlights" should be genuine positives (e.g. "Has CI pipeline", "MIT licensed",
  "Active in the last week", "Includes tests"). Empty array if genuinely nothing stands out.
- install_steps should be 3-7 steps, inferred from the file list / README (package.json,
  requirements.txt, Dockerfile, etc.).
"""


def build_user_prompt(signal: dict) -> str:
    return json.dumps(signal, indent=2)


async def call_ai(signal: dict) -> dict:
    if AI_PROVIDER == "gemini":
        return await call_gemini(signal)
    return await call_groq(signal)


async def call_groq(signal: dict) -> dict:
    if not GROQ_API_KEY:
        raise HTTPException(500, "GROQ_API_KEY is not set on the server.")

    payload = {
        "model": GROQ_MODEL,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": build_user_prompt(signal)},
        ],
        "temperature": 0.3,
        "response_format": {"type": "json_object"},
    }
    headers = {"Authorization": f"Bearer {GROQ_API_KEY}"}

    async with httpx.AsyncClient(timeout=30) as client:
        res = await client.post(GROQ_URL, json=payload, headers=headers)
    if res.status_code != 200:
        raise HTTPException(502, "The AI provider (Groq) returned an error.")

    content = res.json()["choices"][0]["message"]["content"]
    return json.loads(content)


async def call_gemini(signal: dict) -> dict:
    """
    Mandatory final-submission provider. Swap AI_PROVIDER=gemini in .env to use this.
    Uses the standard generative language `generateContent` endpoint with a JSON
    response mime type so the model returns clean JSON, same schema as call_groq.
    """
    if not GEMINI_API_KEY:
        raise HTTPException(500, "GEMINI_API_KEY is not set on the server.")

    payload = {
        "contents": [
            {
                "role": "user",
                "parts": [{"text": build_user_prompt(signal)}],
            }
        ],
        "systemInstruction": {"parts": [{"text": SYSTEM_PROMPT}]},
        "generationConfig": {
            "temperature": 0.3,
            "responseMimeType": "application/json",
        },
    }
    params = {"key": GEMINI_API_KEY}

    async with httpx.AsyncClient(timeout=30) as client:
        res = await client.post(GEMINI_URL, json=payload, params=params)
    if res.status_code != 200:
        raise HTTPException(502, "The AI provider (Gemini) returned an error.")

    data = res.json()
    text = data["candidates"][0]["content"]["parts"][0]["text"]
    return json.loads(text)


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@app.get("/")
async def root():
    return FileResponse(os.path.join(STATIC_DIR, "index.html"))


@app.get("/privacy")
async def privacy():
    return FileResponse(os.path.join(STATIC_DIR, "privacy.html"))


@app.get("/terms")
async def terms():
    return FileResponse(os.path.join(STATIC_DIR, "terms.html"))


@app.get("/cookies")
async def cookies():
    return FileResponse(os.path.join(STATIC_DIR, "cookies.html"))


@app.post("/api/analyze")
async def analyze(req: AnalyzeRequest, request: Request):
    client_ip = request.client.host if request.client else "unknown"
    check_rate_limit(client_ip)

    owner, repo = parse_repo_url(req.repo_url)
    signal = await fetch_repo_signal(owner, repo)
    verdict = await call_ai(signal)

    return {
        "repo": signal["name"],
        "html_url": signal["html_url"],
        "avatar_url": signal["avatar_url"],
        "description": signal["description"],
        "language": signal["language"],
        "topics": signal["topics"],
        "stars": signal["stars"],
        "forks": signal["forks"],
        "open_issues": signal["open_issues"],
        "license": signal["license"],
        "contributors_count": signal["contributors_count"],
        "top_languages": signal["top_languages"],
        "dependency_count": signal["dependency_count"],
        "dependency_source": signal["dependency_source"],
        "days_since_last_commit": signal["days_since_last_commit"],
        "has_tests": signal["has_tests"],
        "has_ci": signal["has_ci"],
        "has_dockerfile": signal["has_dockerfile"],
        "generated_at": datetime.now(timezone.utc).isoformat(),
        **verdict,
    }


@app.get("/api/health")
async def health():
    return {"status": "ok", "provider": AI_PROVIDER}
