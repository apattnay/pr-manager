# PR Review MCP

A **VS Code extension** + **MCP server** for managing GitHub Pull Request
review comments directly from Copilot Chat.

Fetch unresolved review threads → understand what reviewers asked →
fix the code → reply & resolve — all without leaving the editor.

## Features (13 MCP Tools)

| Tool | Description |
|------|-------------|
| `get_pr_overview` | High-level PR summary (title, state, files, reviews) |
| `list_review_comments` | All review comments grouped by file |
| `list_review_threads` | All threads (resolved + unresolved) via GraphQL |
| `get_unresolved_comments_summary` | Actionable summary of open review items |
| `generate_fix_plan` | Structured fix plan from unresolved feedback |
| `get_file_diff` | Patch/diff for a specific file in the PR |
| `reply_to_comment` | Reply to a review comment |
| `update_comment` | Edit an existing review comment body |
| `resolve_thread` | Mark a thread as resolved |
| `unresolve_thread` | Re-open a resolved thread |
| `resolve_all_threads` | Bulk-resolve every unresolved thread |
| `post_pr_comment` | Post a top-level PR comment |
| `batch_reply_and_resolve` | Reply + resolve multiple threads in one call |

## Installation

### Option A: Install the `.vsix` (recommended for teammates)

```bash
code --install-extension pr-review-mcp-0.1.0.vsix
```

### Option B: Build from source

```bash
git clone <this-repo> ~/pr-review-mcp
cd ~/pr-review-mcp
make all           # npm install → compile → package .vsix
code --install-extension pr-review-mcp-0.1.0.vsix
```

### Python dependencies

The extension bundles the MCP server Python code but needs a Python 3.11+
interpreter with `mcp` and `httpx` installed:

```bash
pip install "mcp[cli]>=1.0.0" "httpx>=0.27.0"
```

Or use the built-in prompt: the extension will offer to install them on
first activation if they're missing.

## Configuration

Open VS Code Settings and search for **PR Review MCP**:

| Setting | Default | Description |
|---------|---------|-------------|
| `prReviewMcp.githubToken` | `""` | GitHub PAT with `repo` scope (falls back to `$GITHUB_TOKEN`) |
| `prReviewMcp.githubApiBase` | `https://github.intel.com/api/v3` | REST API base URL |
| `prReviewMcp.githubGraphqlUrl` | `https://github.intel.com/api/graphql` | GraphQL endpoint |
| `prReviewMcp.pythonPath` | `python3` | Python interpreter path |

For **github.com** (public), set:
```
prReviewMcp.githubApiBase    = https://api.github.com
prReviewMcp.githubGraphqlUrl = https://api.github.com/graphql
```

## Usage with Copilot Chat

### Option 1: Extension auto-registers (after install)

The extension activates on startup. In **Copilot Chat (Agent mode)**:

> *"Show me all unresolved review comments on PR #42"*
> *"Generate a fix plan for PR #42 and help me fix each item"*
> *"Reply 'Fixed' to comment 12345 and resolve the thread"*

### Option 2: Manual `.vscode/mcp.json` (without the extension)

If you prefer not to install the extension, add this to your project's
`.vscode/mcp.json`:

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

1. **Fetch** — *"What are the open review items on PR #123?"*
2. **Plan** — *"Generate a fix plan for PR #123"*
3. **Fix** — Copilot reads the plan, opens files, and applies changes
4. **Reply** — *"Reply to comment 456 with 'Fixed — refactored per suggestion'"*
5. **Resolve** — *"Resolve all threads on PR #123"*
6. **Summarize** — *"Post a comment on PR #123 summarizing all changes"*

## Architecture

```
pr-review-mcp/
├── package.json           # VS Code extension manifest
├── tsconfig.json          # TypeScript config
├── src/
│   └── extension.ts       # Extension entry — registers MCP, checks deps
├── mcp_server/
│   ├── __init__.py
│   ├── __main__.py        # python -m mcp_server
│   ├── server.py          # FastMCP server — all 13 tools
│   ├── github_client.py   # Async GitHub REST + GraphQL client
│   └── pyproject.toml     # Python package metadata
├── Makefile               # Build commands (install, compile, package)
├── .vscodeignore          # Files excluded from .vsix
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
```

## Distributing to Teammates

```bash
# Build
cd ~/pr-review-mcp
make all

# Share the .vsix file
cp pr-review-mcp-0.1.0.vsix /shared/path/

# Teammates install with:
code --install-extension /shared/path/pr-review-mcp-0.1.0.vsix
```
