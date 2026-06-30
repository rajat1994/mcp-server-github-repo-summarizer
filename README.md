# github-repo-summarizer

An [MCP](https://modelcontextprotocol.io) server that turns any public GitHub
repository into a PDF report containing a codebase summary, the most recent
commits, and suggested improvements — generated locally by an
[Ollama](https://ollama.com) model.

## What it does

The server exposes a single MCP tool:

```
summarize_github_repo(repo_url: str, output_dir: str = ".") -> str
```

Given a GitHub URL (e.g. `https://github.com/owner/repo`, `owner/repo`
shorthand, or a `git@github.com:owner/repo.git` SSH URL), it:

1. Fetches repository metadata, the latest 25 commits, and the recursive file
   tree from the public GitHub API.
2. Picks a representative set of source files (up to 25 files, each ≤ 40 KB,
   total ≤ 400 KB), prioritising entry points and manifests
   (`README`, `pyproject.toml`, `package.json`, `Dockerfile`, etc.) and
   skipping noise (`node_modules`, `dist`, lockfiles, snapshot folders).
3. Sends that corpus to a local Ollama model in two passes — one for the
   codebase summary, one for improvement suggestions.
4. Renders a PDF (via ReportLab) and returns the absolute path.

### The PDF contains

| Section | What's in it |
| --- | --- |
| Title block | Repo full name, generation timestamp (UTC), link to the repo |
| Metadata table | Description, primary language, default branch, stars, forks, open issues, license, created/last-pushed dates |
| Codebase Summary | 250–400 word LLM-written prose summary of architecture, modules, tech stack |
| Recent Commits | Table of the latest 25 commits (SHA, author, date, message) |
| Suggested Improvements | Numbered list of 5–10 actionable items grounded in the actual code |

## Requirements

- **Python ≥ 3.11**
- **[uv](https://docs.astral.sh/uv/)** for dependency management
- **[Ollama](https://ollama.com)** running locally with at least one model pulled
- An MCP-aware client (e.g. Claude Desktop)

## Installation

```bash
# 1. Clone or copy this folder
cd github-repo-summarizer

# 2. Install dependencies into a project-local .venv
uv sync

# 3. Configure environment
cp .env.example .env
# then edit .env to taste (see "Configuration" below)

# 4. Pull at least one Ollama model
ollama pull llama3.2          # small, fast
# or
ollama pull qwen2.5-coder:7b  # better for code analysis, ~4.7 GB
```

Make sure the Ollama server is running:

```bash
ollama serve          # in a terminal
# or just launch the Ollama.app — it starts the server automatically
```

## Configuration

All settings come from environment variables, which can live in a `.env` file
next to `main.py` (loaded automatically at startup).

| Variable | Default | Purpose |
| --- | --- | --- |
| `OLLAMA_MODEL` | `llama3.2` | Tag of the Ollama model to use. Must be pulled. |
| `OLLAMA_HOST` | `http://localhost:11434` | Ollama HTTP endpoint. |
| `OLLAMA_NUM_CTX` | `16384` | Context window in tokens. Bump for larger repos; lower if memory-constrained. |

Example `.env`:

```ini
OLLAMA_MODEL=qwen2.5-coder:7b
OLLAMA_HOST=http://localhost:11434
OLLAMA_NUM_CTX=32768
```

### Choosing a model

Local LLMs vary widely in code-comprehension quality. Suggestions:

- **`llama3.2`** — 3 B parameters, fast, decent general summaries.
- **`llama3.1:8b`** — bigger, better narrative quality.
- **`qwen2.5-coder:7b`** — recommended for code-heavy repos; trained for code.
- **`codellama:13b`** — older but solid for code review-style output.

Make sure the model's native context window is ≥ `OLLAMA_NUM_CTX`; otherwise
the corpus will be silently truncated.

## Using it with Claude Desktop

Add the server to `~/Library/Application Support/Claude/claude_desktop_config.json`
(macOS) — adjust the absolute paths to wherever you cloned the project:

```json
{
  "mcpServers": {
    "github-repo-summarizer": {
      "command": "/absolute/path/to/uv",
      "args": [
        "run",
        "--directory",
        "/absolute/path/to/github-repo-summarizer",
        "--frozen",
        "--with",
        "mcp[cli]",
        "mcp",
        "run",
        "/absolute/path/to/github-repo-summarizer/main.py"
      ]
    }
  }
}
```

Notes:

- Use the **absolute path to `uv`** (`which uv`). Claude Desktop is a GUI app
  and doesn't inherit your shell's `PATH`.
- `--directory` must point at the **project directory**, not the script
  itself. uv `chdir`s into it.
- Fully quit Claude Desktop (⌘Q on macOS) and reopen it after editing config.
  Just closing the window won't reload MCP servers.

Once connected, ask Claude to use it:

> Use github-repo-summarizer to summarize https://github.com/anthropics/anthropic-sdk-python and save the PDF to my Desktop.

Claude will call `summarize_github_repo(repo_url=..., output_dir=...)` and
return the path to the generated PDF.

## Using it from any other MCP client

The server speaks MCP over stdio. Any client that can launch a subprocess and
exchange JSON-RPC over stdin/stdout will work. The launch command is:

```bash
uv run --directory /path/to/github-repo-summarizer --frozen \
       --with mcp[cli] mcp run /path/to/github-repo-summarizer/main.py
```

For quick local testing:

```bash
uv run python main.py
```

…and pipe an MCP `initialize` request to stdin to confirm it responds.

## Output

PDFs are written to `<output_dir>/<owner>-<repo>-summary.pdf`. `output_dir`
defaults to the current working directory of the server process (which, when
launched by Claude Desktop, may not be where you expect — pass an explicit
path when invoking the tool).

## Limitations

- **Public repos only.** Private repos would need authenticated GitHub access;
  this server doesn't ship that.
- **File budget.** The server reads up to 25 files (≤ 40 KB each, ≤ 400 KB
  total). Very large monorepos will be sampled, not exhaustively analysed.
- **LLM quality is bounded by the model.** A 3 B parameter model will produce
  shallower summaries than an 8 B+ model. Pick accordingly.
- **No streaming.** PDFs are produced after both LLM calls complete; expect
  tens of seconds to a couple of minutes per repo, dominated by the model's
  generation speed.
- **PDF only.** No HTML/Markdown output mode (yet).

## Troubleshooting

**Server shows "failed" in Claude Desktop developer settings.**
Check `~/Library/Logs/Claude/mcp-server-github-repo-summarizer.log` for the
real error. The two most common causes:

- `--directory` value missing or pointing at a file → uv exits with
  `Not a directory`. Fix the path in `claude_desktop_config.json`.
- `uv` not found → use the absolute path to `uv` in `command`.

**`Could not connect to Ollama at http://localhost:11434`.**
Run `ollama serve` (or open Ollama.app).

**`Ollama model 'X' is not pulled`.**
Run `ollama pull X`.

**Truncated or empty summary.**
The corpus exceeded the model's context window. Lower `OLLAMA_NUM_CTX` only
hurts; instead, switch to a model with a larger native context, or shrink
`MAX_TOTAL_BYTES` in `main.py`.

## Project layout

```
github-repo-summarizer/
├── main.py            # MCP server + GitHub/Ollama/PDF logic
├── pyproject.toml     # Project metadata and dependencies
├── .env               # Local config (gitignored)
├── .env.example       # Template
└── README.md
```

## License

Not specified. Add one before redistributing.
