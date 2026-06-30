"""MCP server that summarizes a public GitHub repository into a PDF report."""

from __future__ import annotations

import asyncio
import base64
import os
import re
from datetime import datetime, timezone
from pathlib import Path

import httpx
from dotenv import load_dotenv
from mcp.server.fastmcp import FastMCP
from ollama import Client as OllamaClient
from ollama import ResponseError as OllamaResponseError

load_dotenv(Path(__file__).resolve().parent / ".env")
from reportlab.lib import colors
from reportlab.lib.pagesizes import LETTER
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import inch
from reportlab.platypus import (
    Paragraph,
    SimpleDocTemplate,
    Spacer,
    Table,
    TableStyle,
)

GITHUB_API = "https://api.github.com"
OLLAMA_HOST = os.environ.get("OLLAMA_HOST", "http://localhost:11434")
OLLAMA_MODEL = os.environ.get("OLLAMA_MODEL", "llama3.2")
OLLAMA_NUM_CTX = int(os.environ.get("OLLAMA_NUM_CTX", "16384"))

MAX_FILES_TO_READ = 25
MAX_BYTES_PER_FILE = 40_000
MAX_TOTAL_BYTES = 400_000
COMMITS_TO_FETCH = 25

SOURCE_EXTENSIONS = {
    ".py", ".js", ".ts", ".tsx", ".jsx", ".mjs", ".cjs",
    ".go", ".rs", ".java", ".kt", ".kts", ".scala",
    ".c", ".h", ".cc", ".cpp", ".hpp",
    ".cs", ".rb", ".php", ".swift", ".m", ".mm",
    ".sh", ".bash", ".zsh",
    ".html", ".css", ".scss", ".vue", ".svelte",
    ".sql", ".graphql", ".proto",
    ".toml", ".yaml", ".yml", ".json",
    ".md", ".rst",
}

PRIORITY_FILENAMES = {
    "README.md", "README.rst", "README",
    "package.json", "pyproject.toml", "setup.py", "setup.cfg", "requirements.txt",
    "Cargo.toml", "go.mod", "pom.xml", "build.gradle", "build.gradle.kts",
    "Gemfile", "composer.json",
    "Dockerfile", "docker-compose.yml", "docker-compose.yaml",
    "Makefile", "CMakeLists.txt",
    "tsconfig.json", "next.config.js", "vite.config.ts", "vite.config.js",
}

NOISE_PATH_FRAGMENTS = (
    "node_modules/", "vendor/", "dist/", "build/", ".min.",
    "/test/", "/tests/", "__tests__/", "fixtures/", "snapshots/",
    ".lock", "package-lock.json", "yarn.lock", "pnpm-lock.yaml",
)


mcp = FastMCP("github-repo-summarizer")


# --- GitHub helpers -------------------------------------------------------


def parse_repo_url(url: str) -> tuple[str, str]:
    """Extract (owner, repo) from a GitHub repo URL or owner/repo shorthand."""
    url = url.strip().rstrip("/")
    if url.endswith(".git"):
        url = url[:-4]
    match = re.match(
        r"^(?:https?://(?:www\.)?github\.com/|git@github\.com:)([^/]+)/([^/]+?)/?$",
        url,
    )
    if match:
        return match.group(1), match.group(2)
    if "github.com" not in url and url.count("/") == 1:
        owner, repo = url.split("/")
        if owner and repo:
            return owner, repo
    raise ValueError(f"Could not parse GitHub repo URL: {url!r}")


def github_client() -> httpx.AsyncClient:
    headers = {
        "Accept": "application/vnd.github+json",
        "User-Agent": "github-repo-summarizer-mcp",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    token = os.environ.get("GITHUB_TOKEN")
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return httpx.AsyncClient(headers=headers, timeout=30.0)


async def fetch_repo_meta(client: httpx.AsyncClient, owner: str, repo: str) -> dict:
    r = await client.get(f"{GITHUB_API}/repos/{owner}/{repo}")
    if r.status_code == 404:
        raise ValueError(f"Repository {owner}/{repo} not found or is private.")
    r.raise_for_status()
    return r.json()


async def fetch_commits(client: httpx.AsyncClient, owner: str, repo: str, limit: int) -> list[dict]:
    r = await client.get(
        f"{GITHUB_API}/repos/{owner}/{repo}/commits",
        params={"per_page": limit},
    )
    r.raise_for_status()
    return r.json()


async def fetch_tree(client: httpx.AsyncClient, owner: str, repo: str, branch: str) -> dict:
    r = await client.get(
        f"{GITHUB_API}/repos/{owner}/{repo}/git/trees/{branch}",
        params={"recursive": "1"},
    )
    r.raise_for_status()
    return r.json()


async def fetch_file_content(
    client: httpx.AsyncClient, owner: str, repo: str, path: str, ref: str
) -> str | None:
    r = await client.get(
        f"{GITHUB_API}/repos/{owner}/{repo}/contents/{path}",
        params={"ref": ref},
    )
    if r.status_code != 200:
        return None
    data = r.json()
    if data.get("encoding") != "base64" or "content" not in data:
        return None
    try:
        return base64.b64decode(data["content"]).decode("utf-8", errors="replace")
    except Exception:
        return None


def select_files(tree_entries: list[dict]) -> list[dict]:
    """Pick a representative set of source files within size budgets."""
    priority: list[dict] = []
    others: list[dict] = []

    for entry in tree_entries:
        if entry.get("type") != "blob":
            continue
        path = entry["path"]
        size = entry.get("size", 0) or 0
        if size == 0 or size > MAX_BYTES_PER_FILE:
            continue
        lower_path = path.lower()
        if any(frag in lower_path for frag in NOISE_PATH_FRAGMENTS):
            continue

        name = path.rsplit("/", 1)[-1]
        ext = Path(name).suffix.lower()
        is_priority = name in PRIORITY_FILENAMES or (
            "/" not in path and name.lower().startswith("readme")
        )
        is_source = ext in SOURCE_EXTENSIONS
        if not (is_priority or is_source):
            continue

        record = {"path": path, "size": size}
        (priority if is_priority else others).append(record)

    priority.sort(key=lambda e: e["path"])
    others.sort(key=lambda e: e["size"])

    selected: list[dict] = []
    total = 0
    for record in priority + others:
        if len(selected) >= MAX_FILES_TO_READ:
            break
        if total + record["size"] > MAX_TOTAL_BYTES:
            continue
        selected.append(record)
        total += record["size"]
    return selected


# --- LLM analysis ---------------------------------------------------------


def build_corpus(files_with_content: list[dict]) -> str:
    parts = []
    for f in files_with_content:
        parts.append(f"=== FILE: {f['path']} ===")
        parts.append(f["content"])
        parts.append("")
    return "\n".join(parts)


def call_llm(prompt: str, system: str) -> str:
    client = OllamaClient(host=OLLAMA_HOST)
    try:
        response = client.chat(
            model=OLLAMA_MODEL,
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": prompt},
            ],
            options={"num_ctx": OLLAMA_NUM_CTX, "temperature": 0.2},
        )
    except OllamaResponseError as e:
        if e.status_code == 404:
            raise RuntimeError(
                f"Ollama model {OLLAMA_MODEL!r} is not pulled. "
                f"Run: ollama pull {OLLAMA_MODEL}"
            ) from e
        raise RuntimeError(f"Ollama error ({e.status_code}): {e.error}") from e
    except (httpx.ConnectError, ConnectionError) as e:
        raise RuntimeError(
            f"Could not connect to Ollama at {OLLAMA_HOST}. "
            "Is `ollama serve` running?"
        ) from e
    return (response.get("message") or {}).get("content", "").strip()


def generate_analysis(repo_meta: dict, corpus: str) -> tuple[str, str]:
    descriptor = (
        f"Repository: {repo_meta.get('full_name')}\n"
        f"Description: {repo_meta.get('description') or '(none)'}\n"
        f"Primary language: {repo_meta.get('language') or 'unknown'}\n"
        f"Stars: {repo_meta.get('stargazers_count')}, "
        f"Forks: {repo_meta.get('forks_count')}\n"
        f"Default branch: {repo_meta.get('default_branch')}\n"
    )

    summary_system = (
        "You are a senior engineer writing a concise, accurate technical summary "
        "of a codebase for a PDF report. Write in plain prose. Be specific — refer "
        "to actual files, frameworks, and patterns you can see. Avoid filler. "
        "Target 250-400 words. Do not use Markdown headings; use short paragraphs."
    )
    summary_prompt = (
        f"{descriptor}\n"
        "Below is a representative sample of source files from this repository. "
        "Write a technical summary covering: what the project does, its overall "
        "architecture, the major modules or components, and the tech stack.\n\n"
        f"{corpus}"
    )
    summary = call_llm(summary_prompt, summary_system)

    improvements_system = (
        "You are a senior engineer doing a code review. Identify concrete, "
        "actionable improvements grounded in the actual code shown. Cover code "
        "quality, structure, testing, security, performance, and developer "
        "experience as relevant. Output a numbered list of 5-10 items; each item "
        "should be a short bold-style headline followed by 1-3 sentences of "
        "justification referencing specific files where possible. Use the format "
        "'1. **Headline.** Explanation...' on a single line per item."
    )
    improvements_prompt = (
        f"{descriptor}\n"
        "Review the following source files and suggest improvements.\n\n"
        f"{corpus}"
    )
    improvements = call_llm(improvements_prompt, improvements_system)

    return summary, improvements


# --- PDF rendering --------------------------------------------------------


def _escape(text: str) -> str:
    safe = text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    return re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", safe)


def text_to_paragraphs(text: str, body_style: ParagraphStyle) -> list:
    flowables = []
    for raw_block in re.split(r"\n\s*\n", text):
        block = raw_block.strip()
        if not block:
            continue
        safe = _escape(block).replace("\n", "<br/>")
        flowables.append(Paragraph(safe, body_style))
        flowables.append(Spacer(1, 6))
    return flowables


def build_pdf(
    output_path: Path,
    repo_meta: dict,
    commits: list[dict],
    summary: str,
    improvements: str,
) -> None:
    doc = SimpleDocTemplate(
        str(output_path),
        pagesize=LETTER,
        leftMargin=0.75 * inch,
        rightMargin=0.75 * inch,
        topMargin=0.75 * inch,
        bottomMargin=0.75 * inch,
        title=f"{repo_meta.get('full_name')} — Codebase Summary",
        author="github-repo-summarizer",
    )

    base = getSampleStyleSheet()
    styles = {
        "title": ParagraphStyle(
            "title", parent=base["Title"], fontSize=22, leading=26, spaceAfter=10
        ),
        "h1": ParagraphStyle(
            "h1", parent=base["Heading1"], fontSize=15, leading=19,
            spaceBefore=16, spaceAfter=8, textColor=colors.HexColor("#1f2937"),
        ),
        "body": ParagraphStyle(
            "body", parent=base["BodyText"], fontSize=10.5, leading=15,
        ),
        "meta": ParagraphStyle(
            "meta", parent=base["BodyText"], fontSize=9.5, leading=13,
            textColor=colors.HexColor("#4b5563"),
        ),
    }

    story: list = []

    title_text = f"{repo_meta.get('full_name', 'Repository')} — Codebase Summary"
    story.append(Paragraph(_escape(title_text), styles["title"]))
    generated = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    html_url = repo_meta.get("html_url") or ""
    story.append(Paragraph(
        f"Generated {generated} &middot; "
        f"<a href='{html_url}' color='#2563eb'>{_escape(html_url)}</a>",
        styles["meta"],
    ))
    story.append(Spacer(1, 14))

    license_obj = repo_meta.get("license") or {}
    meta_rows = [
        ["Description", repo_meta.get("description") or "—"],
        ["Primary language", repo_meta.get("language") or "—"],
        ["Default branch", repo_meta.get("default_branch") or "—"],
        ["Stars", str(repo_meta.get("stargazers_count", 0))],
        ["Forks", str(repo_meta.get("forks_count", 0))],
        ["Open issues", str(repo_meta.get("open_issues_count", 0))],
        ["License", license_obj.get("spdx_id") or "—"],
        ["Created", (repo_meta.get("created_at") or "")[:10] or "—"],
        ["Last pushed", (repo_meta.get("pushed_at") or "")[:10] or "—"],
    ]
    meta_rows = [[Paragraph(_escape(str(k)), styles["body"]),
                  Paragraph(_escape(str(v)), styles["body"])] for k, v in meta_rows]
    meta_table = Table(meta_rows, colWidths=[1.5 * inch, 5.25 * inch])
    meta_table.setStyle(TableStyle([
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("BOX", (0, 0), (-1, -1), 0.5, colors.HexColor("#d1d5db")),
        ("INNERGRID", (0, 0), (-1, -1), 0.25, colors.HexColor("#e5e7eb")),
        ("BACKGROUND", (0, 0), (0, -1), colors.HexColor("#f9fafb")),
        ("LEFTPADDING", (0, 0), (-1, -1), 6),
        ("RIGHTPADDING", (0, 0), (-1, -1), 6),
        ("TOPPADDING", (0, 0), (-1, -1), 4),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
    ]))
    story.append(meta_table)

    story.append(Paragraph("Codebase Summary", styles["h1"]))
    story.extend(text_to_paragraphs(summary, styles["body"]))

    story.append(Paragraph("Recent Commits", styles["h1"]))
    header = [
        Paragraph("<b>SHA</b>", styles["body"]),
        Paragraph("<b>Author</b>", styles["body"]),
        Paragraph("<b>Date</b>", styles["body"]),
        Paragraph("<b>Message</b>", styles["body"]),
    ]
    rows = [header]
    for c in commits:
        sha = (c.get("sha") or "")[:7]
        commit_obj = c.get("commit") or {}
        author_obj = commit_obj.get("author") or {}
        author = author_obj.get("name") or "—"
        date = (author_obj.get("date") or "")[:10]
        message = (commit_obj.get("message") or "").split("\n", 1)[0][:140]
        rows.append([
            Paragraph(_escape(sha), styles["body"]),
            Paragraph(_escape(author), styles["body"]),
            Paragraph(_escape(date), styles["body"]),
            Paragraph(_escape(message), styles["body"]),
        ])
    commits_table = Table(
        rows, colWidths=[0.7 * inch, 1.3 * inch, 0.9 * inch, 3.85 * inch],
        repeatRows=1,
    )
    commits_table.setStyle(TableStyle([
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#f3f4f6")),
        ("INNERGRID", (0, 0), (-1, -1), 0.25, colors.HexColor("#e5e7eb")),
        ("BOX", (0, 0), (-1, -1), 0.5, colors.HexColor("#d1d5db")),
        ("LEFTPADDING", (0, 0), (-1, -1), 5),
        ("RIGHTPADDING", (0, 0), (-1, -1), 5),
        ("TOPPADDING", (0, 0), (-1, -1), 3),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
    ]))
    story.append(commits_table)

    story.append(Paragraph("Suggested Improvements", styles["h1"]))
    story.extend(text_to_paragraphs(improvements, styles["body"]))

    doc.build(story)


# --- MCP tool -------------------------------------------------------------


@mcp.tool()
async def summarize_github_repo(repo_url: str, output_dir: str = ".") -> str:
    """Generate a PDF report summarizing a public GitHub repository.

    The PDF contains the repository's metadata, an LLM-written technical summary
    of the codebase, the most recent commits, and a list of suggested improvements.

    Args:
        repo_url: A public GitHub repository URL (e.g. https://github.com/owner/repo)
            or "owner/repo" shorthand.
        output_dir: Directory where the PDF will be written. Defaults to the
            current working directory.

    Returns:
        The absolute path of the generated PDF file.
    """
    owner, repo = parse_repo_url(repo_url)

    async with github_client() as client:
        repo_meta = await fetch_repo_meta(client, owner, repo)
        branch = repo_meta.get("default_branch") or "main"
        commits = await fetch_commits(client, owner, repo, COMMITS_TO_FETCH)
        tree = await fetch_tree(client, owner, repo, branch)
        picks = select_files(tree.get("tree", []))

        files_with_content: list[dict] = []
        for pick in picks:
            content = await fetch_file_content(client, owner, repo, pick["path"], branch)
            if not content:
                continue
            if len(content) > MAX_BYTES_PER_FILE:
                content = content[:MAX_BYTES_PER_FILE] + "\n... [truncated]"
            files_with_content.append({"path": pick["path"], "content": content})

    if not files_with_content:
        raise RuntimeError(
            f"No readable source files found in {owner}/{repo}; cannot summarize."
        )

    corpus = build_corpus(files_with_content)
    summary, improvements = await asyncio.to_thread(generate_analysis, repo_meta, corpus)

    out_dir = Path(output_dir).expanduser().resolve()
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / f"{owner}-{repo}-summary.pdf"
    build_pdf(out_path, repo_meta, commits, summary, improvements)

    return str(out_path)


def main() -> None:
    mcp.run()


if __name__ == "__main__":
    main()
