"""
DevLens - instant audits of GitHub repositories.

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


# --- Deterministic README link extraction (no AI guessing involved) --------

VIDEO_LINK_RE = re.compile(
    r"https?://(?:www\.)?(?:youtube\.com/watch\?v=[\w-]+|youtu\.be/[\w-]+|"
    r"loom\.com/share/[\w-]+)"
)
DEMO_LINK_RE = re.compile(
    r'https?://[^\s\)\]"\'>]+\.(?:vercel\.app|netlify\.app|herokuapp\.com|'
    r'onrender\.com|github\.io|pages\.dev|streamlit\.app|railway\.app)'
    r'[^\s\)\]"\'>]*',
    re.IGNORECASE,
)

# README badges that are NOT the project's own site (solidarity banners, funding
# links, coverage badges, etc.) but happen to live on one of the DEMO_LINK_RE
# hosting domains and would otherwise be misread as "the project's live demo".
DEMO_LINK_DENYLIST = (
    "standwithukraine", "opencollective.com", "buymeacoffee.com",
    "patreon.com", "codecov.io", "coveralls.io", "shields.io",
)


def extract_readme_links(readme_text: str, owner: str = "") -> dict:
    video_match = VIDEO_LINK_RE.search(readme_text)

    demo_url = None
    for match in DEMO_LINK_RE.finditer(readme_text):
        candidate = match.group(0)
        low = candidate.lower()
        if any(bad in low for bad in DEMO_LINK_DENYLIST):
            continue
        # A github.io link is only trustworthy as "this project's site" when it's
        # hosted under the repo owner's own github.io domain; other people's
        # github.io pages linked from a README (badges, credits, etc.) are not.
        if "github.io" in low and owner and f"{owner.lower()}.github.io" not in low:
            continue
        demo_url = candidate
        break

    return {
        "video_url": video_match.group(0) if video_match else None,
        "demo_url": demo_url,
    }


# --- Deterministic security scan (regex-based, not AI-guessed) -------------
#
# This is a best-effort scan of the file tree plus a handful of likely entry
# files. It is not a substitute for a real secret-scanning tool, but it turns
# "the AI thinks env vars might be missing" into a concrete, checkable finding.

ENTRY_FILE_CANDIDATES = [
    "main.py", "app.py", "app/main.py", "manage.py",
    "index.js", "index.ts", "src/index.js", "src/index.ts",
    "server.js", "src/main.tsx", "src/main.jsx", "src/App.tsx", "src/App.jsx",
]

ENV_USAGE_PATTERNS = [
    re.compile(r'os\.getenv\(\s*["\']([A-Z0-9_]+)["\']'),
    re.compile(r'os\.environ(?:\.get)?\(?\[?\s*["\']([A-Z0-9_]+)["\']'),
    re.compile(r'process\.env\.([A-Z0-9_]+)'),
    re.compile(r'process\.env\[\s*["\']([A-Z0-9_]+)["\']\s*\]'),
    re.compile(r'import\.meta\.env\.([A-Z0-9_]+)'),
]

SECRET_PATTERNS = [
    ("AWS access key", re.compile(r"AKIA[0-9A-Z]{16}")),
    ("Google API key", re.compile(r"AIza[0-9A-Za-z\-_]{35}")),
    ("Stripe live key", re.compile(r"sk_live_[0-9A-Za-z]{16,}")),
    ("Slack token", re.compile(r"xox[baprs]-[0-9A-Za-z-]{10,}")),
    ("Private key block", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----")),
]


async def scan_security(
    client: httpx.AsyncClient, base: str, file_paths: list[str]
) -> dict:
    # Only a *root-level* .env is treated as a real leak risk. A .env nested under
    # a tests/fixtures/examples-style directory is very commonly a harmless test
    # fixture (e.g. for exercising dotenv-loading code), not a secret - so it is
    # reported separately, at low severity, instead of raising a false alarm.
    has_committed_env_file = ".env" in file_paths
    nested_env_files = [
        p for p in file_paths
        if p != ".env" and (p.endswith("/.env") or "/.env." in p)
    ]

    env_example_keys: set[str] = set()
    example_path = next(
        (p for p in file_paths if p.lower() in (".env.example", ".env.sample")), None
    )
    if example_path:
        content = await fetch_file_raw(client, base, example_path)
        if content:
            for line in content.splitlines():
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    env_example_keys.add(line.split("=", 1)[0].strip())

    candidates = [p for p in ENTRY_FILE_CANDIDATES if p in file_paths][:2]
    used_env_vars: set[str] = set()
    potential_secrets: list[dict] = []
    for path in candidates:
        content = await fetch_file_raw(client, base, path)
        if not content:
            continue
        for pattern in ENV_USAGE_PATTERNS:
            used_env_vars.update(pattern.findall(content))
        for name, pattern in SECRET_PATTERNS:
            if pattern.search(content):
                potential_secrets.append({"file": path, "type": name})

    undocumented_env_vars = sorted(v for v in used_env_vars if v not in env_example_keys)

    return {
        "has_committed_env_file": has_committed_env_file,
        "nested_env_files": nested_env_files[:3],
        "scanned_files": candidates,
        "undocumented_env_vars": undocumented_env_vars,
        "potential_secrets": potential_secrets,
    }


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

        readme_text, top_languages, contributors_count, dependency_info, security = (
            await asyncio.gather(
                get_readme(),
                get_languages(),
                get_contributors_count(),
                get_dependency_info(),
                scan_security(client, base, file_paths),
            )
        )

    readme_links = extract_readme_links(readme_text, owner=owner)
    # GitHub's own "homepage" field (the link shown next to a repo's description on
    # GitHub) is a structured, author-set value, more reliable than anything scraped
    # out of README prose, so it takes priority as the demo link when present.
    homepage = (meta.get("homepage") or "").strip() or None
    demo_url = homepage or readme_links["demo_url"]
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
        "homepage_url": homepage,
        "demo_url": demo_url,
        "video_url": readme_links["video_url"],
        "has_committed_env_file": security["has_committed_env_file"],
        "nested_env_files": security["nested_env_files"],
        "undocumented_env_vars": security["undocumented_env_vars"],
        "potential_secrets": security["potential_secrets"],
        "security_scanned_files": security["scanned_files"],
    }


# ---------------------------------------------------------------------------
# Judge score: a deterministic 0-100 rollup, computed in code (not by the AI)
# so judges get one comparable number that is always derived the same way.
# ---------------------------------------------------------------------------

SEVERITY_PENALTY = {"high": 15, "medium": 7, "low": 3}


def compute_judge_score(signal: dict, verdict: dict) -> tuple[int, list[dict]]:
    """Returns (score, breakdown) where breakdown lists every line item that went
    into the score, so the UI can show judges exactly why one repo outscored
    another instead of presenting the number as a black box."""
    breakdown = [{"label": "Base score", "points": 40}]
    score = 40

    if signal.get("license"):
        breakdown.append({"label": "Open-source license present", "points": 15})
        score += 15
    if signal.get("has_tests"):
        breakdown.append({"label": "Automated tests present", "points": 10})
        score += 10
    if signal.get("has_ci"):
        breakdown.append({"label": "CI pipeline configured", "points": 10})
        score += 10
    if signal.get("has_dockerfile"):
        breakdown.append({"label": "Dockerfile present", "points": 5})
        score += 5
    if signal.get("demo_url"):
        breakdown.append({"label": "Live demo or website linked", "points": 10})
        score += 10

    days = signal.get("days_since_last_commit")
    if days is not None:
        if days <= 30:
            breakdown.append({"label": "Committed in the last 30 days", "points": 10})
            score += 10
        elif days <= 180:
            breakdown.append({"label": "Committed in the last 6 months", "points": 5})
            score += 5

    for warning in verdict.get("warnings", []):
        severity = warning.get("severity", "medium") if isinstance(warning, dict) else "medium"
        text = warning.get("text", "warning") if isinstance(warning, dict) else str(warning)
        penalty = SEVERITY_PENALTY.get(severity, 7)
        breakdown.append({"label": f"Warning ({severity}): {text}", "points": -penalty})
        score -= penalty

    if signal.get("has_committed_env_file"):
        breakdown.append({"label": "Root .env file committed", "points": -20})
        score -= 20
    elif signal.get("nested_env_files"):
        breakdown.append({"label": "Nested .env file found (likely a test fixture)", "points": -5})
        score -= 5
    if signal.get("potential_secrets"):
        breakdown.append({"label": "Possible secret key pattern found", "points": -15})
        score -= 15
    if signal.get("undocumented_env_vars") and not signal.get("has_env_example"):
        breakdown.append({"label": "Undocumented environment variables", "points": -5})
        score -= 5

    return max(0, min(100, round(score))), breakdown


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
  "complexity_factors": [
    {"label": "Codebase size", "note": "one short clause on file count / repo scale"},
    {"label": "Dependencies", "note": "one short clause on dependency_count and what they imply"},
    {"label": "Architecture", "note": "one short clause on structure: monolith, services, infra"},
    {"label": "Tech stack diversity", "note": "one short clause on language/framework spread"},
    {"label": "Setup effort", "note": "one short clause on how much work install/config takes"}
  ],
  "risk_level": "Safe" | "Moderate" | "Warning",
  "tech_stack": ["short tags for the languages/frameworks/services actually used, max 6"],
  "highlights": ["short positive callouts a judge would want to know, max 5"],
  "warnings": [
    {"severity": "high" | "medium" | "low", "text": "short, specific warning"}
  ],
  "install_steps": ["ordered, concise, copy-pasteable steps to run the project locally"]
}

Rules:
- "complexity_score" must be derived from exactly the 5 dimensions listed in
  "complexity_factors" (codebase size, dependency count, architecture, tech stack
  diversity, setup effort). Always return all 5 factors, each with a short, specific note
  grounded in the provided signal, never a generic placeholder.
- Use the provided signal (license presence, contributors_count, days_since_last_commit,
  has_tests, has_ci, has_dockerfile, has_env_example, dependency_count) to justify your
  warnings and highlights instead of guessing.
- The signal also includes results of a real, code-level scan (not a guess):
  has_committed_env_file, nested_env_files, potential_secrets, undocumented_env_vars,
  demo_url, video_url. Do NOT restate these as your own warnings or highlights (the app
  already surfaces them separately); use them only to inform your overall risk_level and
  summary. Note that nested_env_files (a .env found inside a subdirectory, e.g. under
  tests/) is usually a harmless test fixture, not a real secret leak, so treat it as
  minor; has_committed_env_file (a .env at the repository root) is the real signal.
- risk_level "Warning" if setup looks broken/unclear, secrets seem required with no example
  env file, has_committed_env_file is true, potential_secrets is non-empty, or the repo
  looks abandoned/undocumented; "Moderate" if there are some gaps (no tests, no CI, stale
  activity); "Safe" if it looks clean, documented, and maintained.
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

    # Verified, code-level findings from our own scan take priority over the AI's
    # warnings (which are asked not to duplicate these) and are labeled distinctly
    # in the response so the frontend can mark them as "Verified" rather than AI-guessed.
    scan_warnings = []
    if signal["has_committed_env_file"]:
        scan_warnings.append({
            "severity": "high",
            "text": "A .env file is committed at the repository root. If it contains "
                    "real secrets, rotate them immediately.",
            "source": "scan",
        })
    elif signal["nested_env_files"]:
        scan_warnings.append({
            "severity": "low",
            "text": f"Found {signal['nested_env_files'][0]}. Likely a test fixture, but "
                    f"worth a manual check for anything sensitive.",
            "source": "scan",
        })
    for secret in signal["potential_secrets"]:
        scan_warnings.append({
            "severity": "high",
            "text": f"Possible {secret['type']} found in {secret['file']}.",
            "source": "scan",
        })
    if signal["undocumented_env_vars"]:
        preview = ", ".join(signal["undocumented_env_vars"][:5])
        scan_warnings.append({
            "severity": "medium",
            "text": f"Environment variables used in code but not documented in "
                    f".env.example: {preview}.",
            "source": "scan",
        })

    ai_warnings = [
        {**w, "source": "ai"} if isinstance(w, dict) else {"severity": "medium", "text": w, "source": "ai"}
        for w in verdict.get("warnings", [])
    ]
    verdict["warnings"] = scan_warnings + ai_warnings

    judge_score, judge_score_breakdown = compute_judge_score(signal, verdict)

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
        "created_at": signal["created_at"],
        "has_tests": signal["has_tests"],
        "has_ci": signal["has_ci"],
        "has_dockerfile": signal["has_dockerfile"],
        "homepage_url": signal["homepage_url"],
        "demo_url": signal["demo_url"],
        "video_url": signal["video_url"],
        "judge_score": judge_score,
        "judge_score_breakdown": judge_score_breakdown,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        **verdict,
    }


@app.get("/api/health")
async def health():
    return {"status": "ok", "provider": AI_PROVIDER}
