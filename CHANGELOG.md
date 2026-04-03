# Changelog

All notable changes to the **PR Review MCP** extension will be documented here.

## [1.0.0] — 2025-04-03

### Added
- **71 unit tests** covering GitHub client, evaluator, and MCP server tools.
- **Retry with exponential back-off** (3 attempts) for transient server errors (5xx)
  and network timeouts in the GitHub API client.
- **Rate-limit detection** — automatically pauses and retries when the GitHub API
  returns `403` with `X-RateLimit-Remaining: 0`.
- **CI/CD pipeline** via GitHub Actions: Python tests (3.11–3.13), Ruff lint,
  TypeScript compile, `.vsix` build.
- `CHANGELOG.md` — this file.
- Extension icon (`icon.png`).
- Root `pyproject.toml` with pytest configuration.

### Changed
- Bumped version to **1.0.0** for marketplace release.
- Default API URLs changed from GitHub Enterprise to **github.com** (public).
- Extension now uses the standard `vscode.lm.registerMcpServerDefinitionProvider`
  API — no `mcp.json` file management needed.
- `_get`, `_post`, `_patch`, `_delete` in `github_client.py` now route through
  a unified `_request()` method with retry/rate-limit support.
- README rewritten for marketplace quality.

### Fixed
- Pagination bug in `list_review_comments` — was missing a page-termination check
  when the response list was non-empty but < 100 items.
- `httpx.AsyncClient` is now reused across requests (shared instance) instead of
  creating a new client per call.

### Security
- Removed hardcoded GitHub token from `.vscode/settings.json`.
- Added `.vscode/settings.json`, `.env`, and `*.log` to `.gitignore`.
- Token resolution chain: VS Code settings → `GITHUB_TOKEN` env → `~/.netrc`.

## [0.3.0] — 2025-04-03

### Changed
- Extension rewritten to use `vscode.lm.registerMcpServerDefinitionProvider`
  instead of writing `mcp.json` files into the workspace.
- Removed `ensureWorkspaceMcpJson()` — MCP server registration is fully automatic.
- Default API endpoints changed to `https://api.github.com`.

## [0.2.0] — 2025-04-02

### Added
- CLI driver (`pr-review`) with 9 subcommands and full auto-detection.
- Evaluator / triage engine: VALID, DISMISS, OPTIONAL verdicts.
- `batch_reply_and_resolve` tool for bulk operations.
- `setup_review_session` tool with auto-detection from git remote + current branch.
- `setup_github_access` tool with step-by-step authentication guide.

### Fixed
- REST pagination for `get_pr_files` and `list_reviews` (> 100 items).
- `pyproject.toml` entry point corrected to `cli:main`.

## [0.1.0] — 2025-04-02

### Added
- Initial MCP server with 14 tools for PR review management.
- GitHub REST + GraphQL client (`github_client.py`).
- VS Code extension with `onStartupFinished` activation.
- Python dependency checking on extension activation.
