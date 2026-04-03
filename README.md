# PR Review MCP

A **VS Code extension** + **MCP server** + **CLI** for managing GitHub Pull
Request review comments directly from Copilot Chat or the terminal.

Fetch unresolved review threads → triage with the evaluator → fix the code
→ reply & resolve — all without leaving the editor.

## Features

### 16 MCP Tools (for Copilot Chat Agent mode)

| # | Tool | Description |
|---|------|-------------|
| 0a | `setup_review_session` | Initialise session — auto-detects repo + PR from git |
| 0b | `setup_github_access` | Show authentication setup instructions |
| 1 | `get_pr_overview` | High-level PR summary (title, state, files, reviews) |
| 2 | `list_review_comments` | All review comments grouped by file |
| 3 | `list_review_threads` | All threads (resolved + unresolved) via GraphQL |
| 4 | `get_unresolved_comments_summary` | Actionable summary of open review items |
| 5 | `get_file_diff` | Patch/diff for a specific file in the PR |
| 6 | `reply_to_comment` | Reply to a review comment |
| 7 | `update_comment` | Edit an existing review comment body |
| 8 | `resolve_thread` | Mark a thread as resolved |
| 9 | `unresolve_thread` | Re-open a resolved thread |
| 10 | `resolve_all_threads` | Bulk-resolve every unresolved thread |
| 11 | `post_pr_comment` | Post a top-level PR comment |
| 12 | `generate_fix_plan` | Structured fix plan from unresolved feedback |
| 13 | `batch_reply_and_resolve` | Reply + resolve multiple threads in one call |
| 14 | `evaluate_review_comments` | Triage threads: VALID / DISMISS / OPTIONAL |

### CLI (9 subcommands)

```bash
pr-review overview          # PR summary
pr-review comments          # all review comments
pr-review unresolved        # unresolved threads (numbered for dismiss)
pr-review evaluate          # triage with heuristic evaluator
pr-review fix-plan          # structured fix plan
pr-review diff <file>       # file diff from PR
pr-review dismiss N         # reply & resolve thread N by index
pr-review reply             # reply to a comment by ID
pr-review batch-resolve     # bulk reply & resolve (--evaluate for smart mode)
```

### Auto-Detection

Run from inside **any git checkout** — the CLI and MCP server automatically
detect:

1. **Repository** from `git remote get-url origin`
2. **PR number** from the current branch via GitHub API
3. **GitHub token** from `GITHUB_TOKEN` env or `~/.netrc`
4. **API endpoints** (github.com vs GitHub Enterprise) from the remote URL

No configuration needed for the common case.

### Evaluator (Triage Engine)

Heuristic rules classify each unresolved thread:

- **VALID** — Real issue, should fix the code
- **DISMISS** — Bot was wrong or concern doesn't apply
- **OPTIONAL** — Nice-to-have suggestion, not a bug

Rules prioritise: duplicates → real-bug patterns (division-by-zero,
security) → bot false-positive patterns → bot code suggestions → human
reviewer (default VALID) → bot fallback (OPTIONAL).

## Installation

### Option A: Install the `.vsix` (recommended for teammates)

```bash
code --install-extension pr-review-mcp-0.2.0.vsix
```

### Option B: Build from source

```bash
git clone <this-repo> ~/pr-review-mcp
cd ~/pr-review-mcp
make all           # npm install → compile → package .vsix
code --install-extension pr-review-mcp-0.2.0.vsix
```

### Option C: CLI only (no VS Code extension)

```bash
cd ~/pr-review-mcp/mcp_server
pip install -e .
pr-review --help
```

### Python dependencies

The MCP server needs Python 3.11+ with `mcp` and `httpx`:

```bash
pip install "mcp[cli]>=1.0.0" "httpx>=0.27.0"
```

The VS Code extension offers to install them automatically on first
activation if they're missing.

## Configuration

### VS Code Settings

Open VS Code Settings and search for **PR Review MCP**:

| Setting | Default | Description |
|---------|---------|-------------|
| `prReviewMcp.githubToken` | `""` | GitHub PAT with `repo` scope (falls back to `$GITHUB_TOKEN` / `~/.netrc`) |
| `prReviewMcp.githubApiBase` | `https://github.intel.com/api/v3` | REST API base URL |
| `prReviewMcp.githubGraphqlUrl` | `https://github.intel.com/api/graphql` | GraphQL endpoint |
| `prReviewMcp.pythonPath` | `python3` | Python interpreter path |

For **github.com** (public), set:
```
prReviewMcp.githubApiBase    = https://api.github.com
prReviewMcp.githubGraphqlUrl = https://api.github.com/graphql
```

### Environment variables (all optional)

| Variable | Description |
|----------|-------------|
| `GITHUB_TOKEN` | GitHub PAT (falls back to `~/.netrc`) |
| `GITHUB_OWNER` | Repository owner (auto-detected from git remote) |
| `GITHUB_REPO` | Repository name (auto-detected from git remote) |
| `GITHUB_API_BASE` | REST API base URL |
| `GITHUB_GRAPHQL_URL` | GraphQL endpoint |

## Usage

### With Copilot Chat (Agent mode)

The extension activates on startup. In **Copilot Chat (Agent mode)**:

> *"Show me all unresolved review comments on PR #42"*
> *"Generate a fix plan for PR #42 and help me fix each item"*
> *"Reply 'Fixed' to comment 12345 and resolve the thread"*
> *"Evaluate the review comments — which ones should I actually fix?"*

### With the CLI

```bash
# Inside a git checkout with an open PR on the current branch:
pr-review overview              # auto-detects repo + PR
pr-review evaluate              # triage unresolved threads
pr-review unresolved            # see numbered list
pr-review dismiss 3             # reply & resolve thread #3

# Specify a PR number:
pr-review overview 2516

# Different repo entirely:
pr-review overview https://github.com/owner/repo/pull/123

# Bulk-resolve bot comments:
pr-review batch-resolve --evaluate
```

### Manual `.vscode/mcp.json` (without the extension)

```json
{
    "servers": {
        "pr-review-mcp": {
            "type": "stdio",
            "command": "python3",
            "args": ["/path/to/pr-review-mcp/mcp_server/server.py"],
            "env": {
                "GITHUB_TOKEN": "${env:GITHUB_TOKEN}",
                "GITHUB_API_BASE": "https://api.github.com",
                "GITHUB_GRAPHQL_URL": "https://api.github.com/graphql"
            }
        }
    }
}
```

## Typical Workflow

1. **Fetch** — `pr-review overview` or *"What are the open review items?"*
2. **Triage** — `pr-review evaluate` → see which threads need code changes
3. **Fix** — Copilot reads the fix plan, opens files, applies changes
4. **Reply** — `pr-review dismiss N` for invalid threads
5. **Resolve** — `pr-review batch-resolve --evaluate` for bulk cleanup
6. **Summarize** — *"Post a comment on PR #123 summarizing all changes"*

## Architecture

```
pr-review-mcp/
├── package.json           # VS Code extension manifest (v0.2.0)
├── tsconfig.json          # TypeScript config
├── Makefile               # Build commands (install, compile, package)
├── src/
│   └── extension.ts       # Extension entry — registers MCP, checks deps
├── mcp_server/
│   ├── __init__.py
│   ├── __main__.py        # python -m mcp_server
│   ├── pyproject.toml     # Python package (pr-review CLI entry point)
│   ├── server.py          # FastMCP server — 16 tools (stdio transport)
│   ├── cli.py             # CLI driver — 9 subcommands + auto-detection
│   ├── evaluator.py       # Heuristic triage engine (VALID/DISMISS/OPTIONAL)
│   └── github_client.py   # Async GitHub REST + GraphQL client (paginated)
├── .vscodeignore
├── .gitignore
├── LICENSE
└── README.md
```

## Development

```bash
npm install          # Install Node dependencies
npm run compile      # Compile TypeScript
npm run watch        # Watch mode
make package         # Build .vsix

# Python
cd mcp_server && pip install -e .   # Install CLI as 'pr-review'
```

## Distributing to Teammates

```bash
# Build
cd ~/pr-review-mcp
make all

# Share the .vsix file
cp pr-review-mcp-0.2.0.vsix /shared/path/

# Teammates install with:
code --install-extension /shared/path/pr-review-mcp-0.2.0.vsix
```
