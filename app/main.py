"""
DevLens - instant, judge-friendly audits of GitHub repositories.

Flow:
  1. Take a GitHub repo URL.
  2. Pull lightweight signal from the GitHub REST API (metadata, file tree, README).
  3. Send that signal to an LLM and ask for a strict JSON verdict.
  4. Serve the verdict to the frontend for a clean dashboard.

AI PROVIDER SWITCH (read this before demo day):
  - `AI_PROVIDER=groq`   -> uses Groq's OpenAI-compatible chat completions endpoint.
                            Fast + free, great for building/testing.
  - `AI_PROVIDER=gemini` -> uses Google's Gemini generateContent endpoint.
                            REQUIRED for the final hackathon submission.
  Both paths live in `call_ai()` below. Flipping the env var is the only
  change needed - no other code in this file references the provider.
"""

import json
import os
import re

import httpx
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException
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


async def fetch_repo_signal(owner: str, repo: str) -> dict:
    base = f"https://api.github.com/repos/{owner}/{repo}"
    async with httpx.AsyncClient(
        headers=gh_headers(), timeout=15, follow_redirects=True
    ) as client:
        repo_res = await client.get(base)
        if repo_res.status_code == 404:
            raise HTTPException(404, "Repository not found (is it public?).")
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

        readme_text = ""
        readme_res = await client.get(f"{base}/readme")
        if readme_res.status_code == 200:
            import base64

            content = readme_res.json().get("content", "")
            try:
                readme_text = base64.b64decode(content).decode("utf-8", errors="ignore")
            except Exception:
                readme_text = ""

    return {
        "name": meta.get("full_name"),
        "description": meta.get("description") or "",
        "language": meta.get("language") or "",
        "languages_url": meta.get("languages_url"),
        "stars": meta.get("stargazers_count", 0),
        "topics": meta.get("topics", []),
        "file_count": len(file_paths),
        "top_files": file_paths[:120],
        "has_dockerfile": any("dockerfile" in p.lower() for p in file_paths),
        "has_tests": any("test" in p.lower() for p in file_paths),
        "has_ci": any(p.startswith(".github/workflows") for p in file_paths),
        "readme_excerpt": readme_text[:4000],
    }


# ---------------------------------------------------------------------------
# AI layer
# ---------------------------------------------------------------------------

SYSTEM_PROMPT = """You are DevLens, an auditor that helps hackathon judges quickly assess \
a GitHub repository. You will be given structured signal about a repo (metadata, file \
list, README excerpt). Respond with ONLY a valid JSON object, no markdown fences, no \
prose, matching exactly this schema:

{
  "summary": "Exactly 2 sentences describing what the project does and how it's built.",
  "complexity_score": <integer 1-10, 1 = trivial, 10 = extremely complex>,
  "risk_level": "Safe" | "Moderate" | "Warning",
  "warnings": ["short warning strings, e.g. missing env vars, no tests, unclear setup"],
  "install_steps": ["ordered, concise, copy-pasteable steps to run the project locally"]
}

Rules:
- risk_level "Warning" if setup looks broken/unclear or secrets seem required with no \
example env file; "Moderate" if there are some gaps (no tests, no CI); "Safe" if it looks \
clean and well documented.
- warnings should be empty array if nothing notable.
- install_steps should be 3-7 steps, inferred from the file list / README (package.json, \
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
        raise HTTPException(502, f"Groq API error: {res.text}")

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
        raise HTTPException(502, f"Gemini API error: {res.text}")

    data = res.json()
    text = data["candidates"][0]["content"]["parts"][0]["text"]
    return json.loads(text)


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@app.get("/")
async def root():
    return FileResponse(os.path.join(STATIC_DIR, "index.html"))


@app.post("/api/analyze")
async def analyze(req: AnalyzeRequest):
    owner, repo = parse_repo_url(req.repo_url)
    signal = await fetch_repo_signal(owner, repo)
    verdict = await call_ai(signal)

    return {
        "repo": signal["name"],
        "description": signal["description"],
        "language": signal["language"],
        "stars": signal["stars"],
        **verdict,
    }


@app.get("/api/health")
async def health():
    return {"status": "ok", "provider": AI_PROVIDER}
