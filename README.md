# PR Review MCP

[![CI](https://github.com/apattnay/pr-manager/actions/workflows/ci.yml/badge.svg)](https://github.com/apattnay/pr-manager/actions)
[![VS Code](https://img.shields.io/badge/VS%20Code-1.99%2B-blue?logo=visualstudiocode)](https://code.visualstudio.com/)
[![Python](https://img.shields.io/badge/Python-3.11%2B-blue?logo=python)](https://python.org)
[![License: MIT](https://img.shields.io/badge/License-MIT-green.svg)](LICENSE)

**Manage GitHub Pull Request reviews from Copilot Chat — fetch, triage, fix, reply, and resolve review comments without leaving the editor.**

PR Review MCP is a **VS Code extension** that bundles an **MCP server** (Model Context Protocol) and a **CLI tool**, giving you three ways to manage PR reviews:

| Interface | Best for |
|-----------|----------|
| **Copilot Chat** (Agent mode) | Conversational review workflow |
| **CLI** (`pr-review`) | Scripting and terminal-first developers |
| **MCP tools** | Any MCP-compatible client (Copilot, Claude Desktop, etc.) |

## Features

### 16 MCP Tools

| Tool | What it does |
|------|-------------|
| `setup_review_session` | Initialize session — auto-detects repo + PR from git |
| `setup_github_access` | Show authentication setup instructions |
| `get_pr_overview` | High-level PR summary (title, state, files, reviews) |
| `list_review_comments` | All review comments grouped by file |
| `list_review_threads` | All threads (resolved + unresolved) via GraphQL |
| `get_unresolved_comments_summary` | Actionable summary of open review items |
| `get_file_diff` | Patch/diff for a specific file in the PR |
| `reply_to_comment` | Reply to a review comment |
| `update_comment` | Edit an existing review comment body |
| `resolve_thread` | Mark a thread as resolved |
| `unresolve_thread` | Re-open a resolved thread |
| `resolve_all_threads` | Bulk-resolve every unresolved thread |
| `post_pr_comment` | Post a top-level PR comment |
| `generate_fix_plan` | Structured fix plan from unresolved feedback |
| `batch_reply_and_resolve` | Reply + resolve multiple threads in one call |
| `evaluate_review_comments` | Triage threads: VALID / DISMISS / OPTIONAL |

### Smart Triage Engine

The built-in evaluator classifies each unresolved thread:

- **VALID** — Real issue, should fix the code
- **DISMISS** — Bot was wrong or concern does not apply
- **OPTIONAL** — Nice-to-have suggestion, not a bug

Priority rules: duplicates > real-bug patterns (division-by-zero, security, runtime errors) > bot false-positive patterns > bot code suggestions > human reviewer (default VALID) > bot fallback (OPTIONAL).

### Auto-Detection

Run from inside any git checkout — everything is auto-detected:

1. **Repository** from `git remote get-url origin`
2. **PR number** from the current branch via GitHub API
3. **GitHub token** from VS Code settings, `GITHUB_TOKEN` env, or `~/.netrc`
4. **API endpoints** (github.com vs GitHub Enterprise) from the remote URL

Zero configuration needed for the common case.

### Production-Ready

- Retry with exponential back-off for transient server errors (5xx) and network timeouts
- Rate-limit detection — automatically pauses when GitHub returns 403
- Paginated API calls — handles PRs with 100+ files and reviews
- 71 unit tests with CI running on Python 3.11, 3.12, and 3.13

## Installation

### From VS Code Marketplace

Search for **"PR Review MCP"** in the Extensions panel, or run:

```bash
code --install-extension apattnay.pr-review-mcp
```

### From .vsix file

```bash
code --install-extension pr-review-mcp-1.0.0.vsix
```

### CLI only (no VS Code)

```bash
cd pr-review-mcp/mcp_server
pip install -e .
pr-review --help
```

### Python Dependencies

The MCP server needs Python 3.11+ with `mcp` and `httpx`:

```bash
pip install "mcp[cli]>=1.0.0" "httpx>=0.27.0"
```

The extension offers to install them automatically on first activation.

## Configuration

### VS Code Settings

| Setting | Default | Description |
|---------|---------|-------------|
| `prReviewMcp.githubToken` | `""` | GitHub PAT with `repo` scope |
| `prReviewMcp.githubApiBase` | `https://api.github.com` | REST API base URL |
| `prReviewMcp.githubGraphqlUrl` | `https://api.github.com/graphql` | GraphQL endpoint |
| `prReviewMcp.pythonPath` | `python3` | Python interpreter path |

For GitHub Enterprise (e.g. `github.intel.com`), set:

```
prReviewMcp.githubApiBase    = https://github.intel.com/api/v3
prReviewMcp.githubGraphqlUrl = https://github.intel.com/api/graphql
```

### Environment Variables (all optional)

| Variable | Description |
|----------|-------------|
| `GITHUB_TOKEN` | GitHub PAT (falls back to `~/.netrc`) |
| `GITHUB_OWNER` | Repository owner (auto-detected from git remote) |
| `GITHUB_REPO` | Repository name (auto-detected from git remote) |
| `GITHUB_API_BASE` | REST API base URL |
| `GITHUB_GRAPHQL_URL` | GraphQL endpoint |

## Usage

### With Copilot Chat (Agent Mode)

The extension activates on startup. In Copilot Chat:

- "Show me all unresolved review comments on PR #42"
- "Generate a fix plan for PR #42 and help me fix each item"
- "Evaluate the review comments — which ones should I actually fix?"
- "Reply Fixed to comment 12345 and resolve the thread"
- "Post a summary comment on PR #42 with all the changes I made"

### With the CLI

```bash
pr-review overview              # auto-detects repo + PR
pr-review evaluate              # triage unresolved threads
pr-review unresolved            # see numbered list
pr-review dismiss 3             # reply and resolve thread #3
pr-review overview 2516         # specify a PR number
pr-review batch-resolve --evaluate  # bulk-resolve bot comments
```

### Typical Workflow

1. **Fetch** — `pr-review overview` or "What are the open review items?"
2. **Triage** — `pr-review evaluate` to see which threads need code changes
3. **Fix** — Copilot reads the fix plan, opens files, applies changes
4. **Reply** — `pr-review dismiss N` for invalid threads
5. **Resolve** — `pr-review batch-resolve --evaluate` for bulk cleanup
6. **Summarize** — "Post a comment on PR #123 summarizing all changes"

## Architecture

```
pr-review-mcp/
  package.json           # VS Code extension manifest
  src/extension.ts       # Extension — registers MCP server provider
  mcp_server/
    server.py            # FastMCP server — 16 tools (stdio transport)
    cli.py               # CLI — 9 subcommands + auto-detection
    evaluator.py         # Heuristic triage engine
    github_client.py     # Async GitHub REST + GraphQL client
    pyproject.toml       # Python package (pr-review CLI entry point)
  tests/                 # 71 unit tests (pytest + pytest-asyncio)
  .github/workflows/     # CI: test + lint + build
  CHANGELOG.md
```

## Contributing

```bash
git clone https://github.com/apattnay/pr-manager.git
cd pr-review-mcp
npm install && npm run compile
pip install -e mcp_server/
pip install pytest pytest-asyncio
python -m pytest tests/ -v
make all
```

## License

[MIT](LICENSE)
