"""MCP server for GitHub PR review management.

Provides tools to:
 - List and inspect PR reviews & review comments
 - Fetch unresolved review threads with full context
 - Reply to review comments
 - Resolve / unresolve review threads
 - Get file diffs to support fixing review feedback
 - Create summary reports of open review items

Requires environment variables:
  GITHUB_TOKEN        — GitHub PAT (repo scope)
  GITHUB_OWNER        — Repository owner / org  (optional — auto-detected from git remote)
  GITHUB_REPO         — Repository name          (optional — auto-detected from git remote)
  GITHUB_API_BASE     — REST API base URL        (default: https://api.github.com)
  GITHUB_GRAPHQL_URL  — GraphQL endpoint         (default: https://api.github.com/graphql)
"""

from __future__ import annotations

import json
import logging
import os
import re
import subprocess
from dataclasses import asdict

from mcp.server.fastmcp import FastMCP

from github_client import GitHubClient, ReviewComment, ReviewThread
from evaluator import evaluate_threads  # noqa: E402

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Bootstrap
# ---------------------------------------------------------------------------

mcp = FastMCP(
    "pr-review-mcp",
    instructions=(
        "GitHub PR Review MCP — fetch PR comments, reviews, threads; "
        "reply to & resolve review conversations; get file diffs for fixing.\n"
        "\n"
        "IMPORTANT STARTUP FLOW:\n"
        "1. When the user asks to review a PR, ALWAYS call `setup_review_session` first.\n"
        "2. If the user provides a PR URL or number, pass it directly.\n"
        "3. If the user does NOT provide a PR reference, call `setup_review_session` \n"
        "   with an empty string — it will return a prompt with examples that you \n"
        "   should show to the user.\n"
        "4. Once the session is set up, use any other tool freely.\n"
        "\n"
        "Accepted PR reference formats:\n"
        "  - Full URL:  https://github.com/owner/repo/pull/123\n"
        "  - GHES URL:  https://github.intel.com/owner/repo/pull/123\n"
        "  - PR number: 123 (needs GITHUB_OWNER/GITHUB_REPO env or git remote)\n"
        "\n"
        "If the token is not configured, call `setup_github_access` to show the \n"
        "user how to authenticate."
    ),
)

# Session state — populated by setup_review_session or env vars
_session: dict[str, str] = {}


def _detect_remote() -> tuple[str, str]:
    """Try to detect owner/repo from the git remote in cwd."""
    try:
        url = subprocess.check_output(
            ["git", "remote", "get-url", "origin"],
            text=True,
            stderr=subprocess.DEVNULL,
        ).strip()
        m = re.search(r"[:/]([^/]+)/([^/]+?)(?:\.git)?$", url)
        if m:
            return m.group(1), m.group(2)
    except Exception:
        pass
    return "", ""


def _detect_branch() -> str:
    """Return the name of the current git branch, or ``""``."""
    try:
        return subprocess.check_output(
            ["git", "rev-parse", "--abbrev-ref", "HEAD"],
            text=True,
            stderr=subprocess.DEVNULL,
        ).strip()
    except Exception:
        return ""


def parse_pr_url(url_or_number: str) -> dict[str, str | int]:
    """Parse a GitHub PR URL or plain number into components.

    Accepts:
      - ``https://github.com/owner/repo/pull/123``
      - ``https://github.intel.com/owner/repo/pull/123``
      - ``https://<any-ghes>/owner/repo/pull/123``
      - ``123``  (plain PR number — needs owner/repo from env or git remote)

    :param url_or_number: Full PR URL or just a PR number.
    :returns: Dict with keys: owner, repo, pr_number, api_base, graphql_url.
    :raises ValueError: If the input cannot be parsed.
    """
    url_or_number = url_or_number.strip()

    # Try matching a full URL first
    m = re.match(
        r"https?://(?P<host>[^/]+)/(?P<owner>[^/]+)/(?P<repo>[^/]+)/pull/(?P<pr>\d+)",
        url_or_number,
    )
    if m:
        host = m.group("host")
        owner = m.group("owner")
        repo = m.group("repo")
        pr_number = int(m.group("pr"))

        # Determine API endpoints from host
        if host in ("github.com", "www.github.com"):
            api_base = "https://api.github.com"
            graphql_url = "https://api.github.com/graphql"
        else:
            # GitHub Enterprise Server
            api_base = f"https://{host}/api/v3"
            graphql_url = f"https://{host}/api/graphql"

        return {
            "owner": owner,
            "repo": repo,
            "pr_number": pr_number,
            "api_base": api_base,
            "graphql_url": graphql_url,
        }

    # Try plain number
    if url_or_number.isdigit():
        return {"pr_number": int(url_or_number)}

    raise ValueError(
        f"Cannot parse \'{url_or_number}\' as a PR URL or number. "
        "Expected: https://github.com/owner/repo/pull/123  or just  123"
    )


def _resolve_token() -> str:
    """Find the GitHub token from env, .netrc, or VS Code setting."""
    token = os.environ.get("GITHUB_TOKEN", "")
    if token:
        return token

    # Try .netrc
    netrc_path = os.path.expanduser("~/.netrc")
    if os.path.isfile(netrc_path):
        try:
            import netrc as netrc_mod

            hosts_to_try = ["github.com", "api.github.com"]
            # Add session host if available
            if "api_base" in _session:
                from urllib.parse import urlparse
                parsed = urlparse(_session["api_base"])
                if parsed.hostname:
                    hosts_to_try.insert(0, parsed.hostname)

            nrc = netrc_mod.netrc(netrc_path)
            for host in hosts_to_try:
                auth = nrc.authenticators(host)
                if auth and auth[2]:
                    return auth[2]
        except Exception:
            pass

    return ""


def _client() -> GitHubClient:
    """Build a GitHubClient from session state, env vars, or git remote."""
    token = _resolve_token()
    if not token:
        raise RuntimeError(
            "No GitHub token found. Set it via one of:\n"
            "  1. GITHUB_TOKEN environment variable\n"
            "  2. ~/.netrc entry (machine github.com login <user> password <token>)\n"
            "  3. VS Code setting: prReviewMcp.githubToken\n"
            "\n"
            "Call the setup_github_access tool for detailed instructions."
        )

    # Session takes priority, then env, then git remote
    auto_owner, auto_repo = _detect_remote()
    owner = _session.get("owner") or os.environ.get("GITHUB_OWNER", auto_owner)
    repo = _session.get("repo") or os.environ.get("GITHUB_REPO", auto_repo)
    if not owner or not repo:
        raise RuntimeError(
            "Cannot determine repository. Either:\n"
            "  - Call setup_review_session with a full PR URL, e.g.:\n"
            "    https://github.com/owner/repo/pull/123\n"
            "  - Set GITHUB_OWNER + GITHUB_REPO environment variables\n"
            "  - Run from inside a git checkout"
        )

    api_base = (
        _session.get("api_base")
        or os.environ.get("GITHUB_API_BASE", "https://api.github.com")
    )
    graphql_url = (
        _session.get("graphql_url")
        or os.environ.get("GITHUB_GRAPHQL_URL", "https://api.github.com/graphql")
    )

    return GitHubClient(
        token=token,
        owner=owner,
        repo=repo,
        api_base=api_base,
        graphql_url=graphql_url,
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _comment_to_dict(c: ReviewComment) -> dict:
    d = asdict(c)
    return {k: v for k, v in d.items() if v not in (None, "", 0)}


def _thread_to_dict(t: ReviewThread) -> dict:
    return {
        "thread_id": t.thread_id,
        "is_resolved": t.is_resolved,
        "path": t.path,
        "line": t.line,
        "comments": [_comment_to_dict(c) for c in t.comments],
    }


# ═══════════════════════════════════════════════════════════════════════════
# MCP Tools
# ═══════════════════════════════════════════════════════════════════════════


# ── 0a. Session initialisation ────────────────────────────────────────────
@mcp.tool()
async def setup_review_session(pr_url_or_number: str = "") -> str:
    """Initialise the review session with a PR URL or number.

    CALL THIS FIRST before using any other tool.  Accepts either:
      - A full GitHub PR URL, e.g. ``https://github.com/owner/repo/pull/123``
      - A GitHub Enterprise URL, e.g. ``https://github.intel.com/org/repo/pull/456``
      - Just a PR number, e.g. ``123`` (requires GITHUB_OWNER/GITHUB_REPO env
        or that the server runs inside a git checkout)
      - Empty string or omitted — returns a prompt asking the user for the PR URL.

    When a full URL is provided, the owner, repo, and correct API endpoints
    are extracted automatically — no extra environment variables needed.

    :param pr_url_or_number: Full PR URL or just the PR number (optional).
    :returns: JSON confirming the session context, or a prompt asking for the PR.
    """
    # If no PR reference provided, try auto-detection first
    if not pr_url_or_number or not pr_url_or_number.strip():
        # Try to auto-detect: repo from git remote + PR from current branch
        auto_owner, auto_repo = _detect_remote()
        owner = os.environ.get("GITHUB_OWNER", auto_owner)
        repo = os.environ.get("GITHUB_REPO", auto_repo)
        branch = _detect_branch()
        token = _resolve_token()

        if owner and repo and branch and token and branch not in ("HEAD", "main", "master"):
            # We have enough to try auto-detection
            from urllib.parse import urlparse
            api_base = _session.get("api_base") or os.environ.get(
                "GITHUB_API_BASE", "https://api.github.com"
            )
            graphql_url = _session.get("graphql_url") or os.environ.get(
                "GITHUB_GRAPHQL_URL", "https://api.github.com/graphql"
            )
            try:
                gh = GitHubClient(
                    token=token, owner=owner, repo=repo,
                    api_base=api_base, graphql_url=graphql_url,
                )
                prs = await gh.list_prs_for_branch(branch, state="open")
                if prs:
                    pr = prs[0]
                    _session["owner"] = owner
                    _session["repo"] = repo
                    _session["api_base"] = api_base
                    _session["graphql_url"] = graphql_url
                    return json.dumps({
                        "status": "ok",
                        "auto_detected": True,
                        "pr_number": pr["number"],
                        "title": pr["title"],
                        "state": pr["state"],
                        "author": pr["user"]["login"],
                        "owner": owner,
                        "repo": repo,
                        "branch": branch,
                        "api_base": api_base,
                        "message": (
                            f"Auto-detected PR #{pr['number']} from current "
                            f"branch '{branch}' on {owner}/{repo}."
                        ),
                    }, indent=2)
            except Exception:
                pass  # Fall through to the prompt below

        # Could not auto-detect — ask the user for input
        return json.dumps({
            "status": "needs_input",
            "message": (
                "Please provide a PR URL or number to get started.\n"
                "\n"
                "Examples:\n"
                "  • https://github.com/owner/repo/pull/123\n"
                "  • https://github.intel.com/org/repo/pull/456\n"
                "  • 123  (if GITHUB_OWNER/GITHUB_REPO are set or you're in a git checkout)\n"
                "\n"
                "Paste the full PR URL from your browser for the easiest setup — "
                "the repository, owner, and API endpoints will be detected automatically."
            ),
        }, indent=2)

    parsed = parse_pr_url(pr_url_or_number)
    pr_number = parsed["pr_number"]

    # Store session-level overrides
    for key in ("owner", "repo", "api_base", "graphql_url"):
        if key in parsed:
            _session[key] = str(parsed[key])

    # Verify the token is available
    token = _resolve_token()
    if not token:
        return json.dumps({
            "status": "error",
            "message": (
                "Session context set, but no GitHub token found. "
                "Call setup_github_access for instructions."
            ),
            "session": {**_session, "pr_number": pr_number},
        }, indent=2)

    # Verify connectivity by fetching the PR
    try:
        gh = _client()
        pr = await gh.get_pr(pr_number)
    except Exception as exc:
        return json.dumps({
            "status": "error",
            "message": f"Token found but API call failed: {exc}",
            "session": {**_session, "pr_number": pr_number},
        }, indent=2)

    return json.dumps({
        "status": "ok",
        "pr_number": pr_number,
        "title": pr["title"],
        "state": pr["state"],
        "author": pr["user"]["login"],
        "owner": _session.get("owner", ""),
        "repo": _session.get("repo", ""),
        "api_base": _session.get("api_base", ""),
    }, indent=2)


# ── 0b. Authentication guidance ──────────────────────────────────────────
@mcp.tool()
async def setup_github_access() -> str:
    """Show how to configure GitHub authentication for this MCP server.

    Returns step-by-step instructions for setting up a GitHub Personal
    Access Token (PAT) so the PR review tools can access the GitHub API.

    No parameters required — call this when the user asks how to set up
    access or when a token-related error occurs.

    :returns: Markdown-formatted setup guide.
    """
    # Check current state
    token = _resolve_token()
    has_token = bool(token)

    guide_lines = [
        "# GitHub Access Setup\n",
    ]

    if has_token:
        guide_lines.append(
            "\u2705 **Token found** — authentication is already configured.\n"
        )
    else:
        guide_lines.append(
            "\u26a0\ufe0f **No token detected** — follow one of the methods below.\n"
        )

    guide_lines.extend([
        "## Method 1: Environment Variable (recommended for CLI)\n",
        "```bash",
        'export GITHUB_TOKEN="ghp_xxxxxxxxxxxxxxxxxxxx"',
        "```\n",
        "Add to your `~/.bashrc` or `~/.zshrc` so it persists across sessions.\n",
        "",
        "## Method 2: ~/.netrc File (recommended for automation)\n",
        "Add an entry to `~/.netrc` (create the file if it doesn't exist):\n",
        "```",
        "# For github.com",
        "machine github.com",
        "  login your-username",
        "  password ghp_xxxxxxxxxxxxxxxxxxxx",
        "",
        "# For GitHub Enterprise (e.g. github.intel.com)",
        "machine github.intel.com",
        "  login your-username",
        "  password ghp_xxxxxxxxxxxxxxxxxxxx",
        "```\n",
        "Then secure the file: `chmod 600 ~/.netrc`\n",
        "",
        "## Method 3: VS Code Setting\n",
        "Open VS Code Settings and set:\n",
        "  **PR Review MCP \u2192 GitHub Token** (`prReviewMcp.githubToken`)\n",
        "",
        "## Creating a Token\n",
        "1. Go to **GitHub \u2192 Settings \u2192 Developer Settings \u2192 Personal Access Tokens**",
        "2. Click **Generate new token (classic)**",
        "3. Select the **`repo`** scope (full control of private repositories)",
        "4. Copy the token and store it using one of the methods above\n",
        "",
        "### GitHub Enterprise (GHES)\n",
        "For `github.intel.com` or other GHES instances:",
        "  - URL: `https://github.intel.com/settings/tokens`",
        "  - The token works with both REST and GraphQL APIs",
        "  - API base is auto-detected from the PR URL you provide\n",
    ])

    return "\n".join(guide_lines)


# ── 1. PR overview ─────────────────────────────────────────────────────────
@mcp.tool()
async def get_pr_overview(pr_number: int) -> str:
    """Get a high-level overview of a PR: title, state, author, changed files, and review status.

    :param pr_number: The pull request number.
    :returns: JSON summary of the PR.
    """
    gh = _client()
    pr = await gh.get_pr(pr_number)
    reviews = await gh.list_reviews(pr_number)
    files = await gh.get_pr_files(pr_number)

    summary = {
        "number": pr["number"],
        "title": pr["title"],
        "state": pr["state"],
        "author": pr["user"]["login"],
        "base": pr["base"]["ref"],
        "head": pr["head"]["ref"],
        "mergeable": pr.get("mergeable"),
        "changed_files": [
            {"filename": f["filename"], "status": f["status"], "changes": f["changes"]}
            for f in files
        ],
        "reviews": [
            {"user": r.user, "state": r.state, "submitted_at": r.submitted_at}
            for r in reviews
        ],
    }
    return json.dumps(summary, indent=2)


# ── 2. List all review comments ───────────────────────────────────────────
@mcp.tool()
async def list_review_comments(pr_number: int) -> str:
    """List every review comment on a PR, grouped by file path.

    :param pr_number: The pull request number.
    :returns: JSON object mapping file paths to arrays of comment objects.
    """
    gh = _client()
    comments = await gh.list_review_comments(pr_number)

    by_path: dict[str, list[dict]] = {}
    for c in comments:
        by_path.setdefault(c.path, []).append(_comment_to_dict(c))
    return json.dumps(by_path, indent=2)


# ── 3. List review threads (resolved + unresolved) ────────────────────────
@mcp.tool()
async def list_review_threads(
    pr_number: int,
    only_unresolved: bool = False,
) -> str:
    """List review threads on a PR via GraphQL.

    Each thread contains the original comment plus replies, and a flag
    indicating whether it has been resolved.

    :param pr_number: The pull request number.
    :param only_unresolved: If True, return only unresolved threads.
    :returns: JSON array of thread objects.
    """
    gh = _client()
    threads = await gh.list_review_threads(pr_number)
    if only_unresolved:
        threads = [t for t in threads if not t.is_resolved]
    return json.dumps([_thread_to_dict(t) for t in threads], indent=2)


# ── 4. Get unresolved comments summary ────────────────────────────────────
@mcp.tool()
async def get_unresolved_comments_summary(pr_number: int) -> str:
    """Get a concise actionable summary of all unresolved review threads.

    Returns one entry per thread with: file, line, reviewer, request summary.

    :param pr_number: The pull request number.
    :returns: JSON array of summary objects ready for triage / fixing.
    """
    gh = _client()
    threads = await gh.list_review_threads(pr_number)
    unresolved = [t for t in threads if not t.is_resolved]

    items = []
    for t in unresolved:
        first = t.comments[0] if t.comments else None
        items.append(
            {
                "thread_id": t.thread_id,
                "file": t.path,
                "line": t.line,
                "reviewer": first.user if first else "unknown",
                "comment": first.body if first else "",
                "replies": len(t.comments) - 1,
                "url": first.html_url if first else "",
            }
        )
    return json.dumps(items, indent=2)


# ── 5. Get file diff context ──────────────────────────────────────────────
@mcp.tool()
async def get_file_diff(pr_number: int, file_path: str) -> str:
    """Return the patch/diff for a specific file in the PR.

    Useful for understanding the context around a review comment before
    making a fix.

    :param pr_number: The pull request number.
    :param file_path: Path of the file whose diff is needed.
    :returns: The unified-diff patch for the file, or an error message.
    """
    gh = _client()
    files = await gh.get_pr_files(pr_number)
    for f in files:
        if f["filename"] == file_path:
            return f.get("patch", "(binary file — no text diff)")
    return f"File '{file_path}' not found in PR #{pr_number} changed files."


# ── 6. Reply to a review comment ──────────────────────────────────────────
@mcp.tool()
async def reply_to_comment(
    pr_number: int,
    comment_id: int,
    body: str,
) -> str:
    """Post a reply to an existing review comment.

    Use this after making a code fix to acknowledge the reviewer's
    feedback, e.g. "Fixed in <commit>".

    :param pr_number: The pull request number.
    :param comment_id: The ID of the review comment to reply to.
    :param body: The reply text (Markdown supported).
    :returns: JSON of the newly created reply comment.
    """
    gh = _client()
    reply = await gh.reply_to_review_comment(pr_number, comment_id, body)
    return json.dumps(_comment_to_dict(reply), indent=2)


# ── 7. Update a review comment ────────────────────────────────────────────
@mcp.tool()
async def update_comment(comment_id: int, new_body: str) -> str:
    """Edit the body of an existing review comment.

    Useful for adding a note like "✅ Resolved" to your own comments.

    :param comment_id: The ID of the comment to edit.
    :param new_body: The new Markdown body for the comment.
    :returns: JSON of the updated comment.
    """
    gh = _client()
    updated = await gh.update_review_comment(comment_id, new_body)
    return json.dumps(_comment_to_dict(updated), indent=2)


# ── 8. Resolve a review thread ────────────────────────────────────────────
@mcp.tool()
async def resolve_thread(thread_id: str) -> str:
    """Mark a review thread as resolved.

    :param thread_id: The GraphQL node ID of the thread (from list_review_threads).
    :returns: Confirmation message.
    """
    gh = _client()
    ok = await gh.resolve_thread(thread_id)
    if ok:
        return f"✅ Thread {thread_id} is now resolved."
    return f"⚠️ Failed to resolve thread {thread_id}."


# ── 9. Unresolve a review thread ──────────────────────────────────────────
@mcp.tool()
async def unresolve_thread(thread_id: str) -> str:
    """Re-open a previously resolved review thread.

    :param thread_id: The GraphQL node ID of the thread.
    :returns: Confirmation message.
    """
    gh = _client()
    ok = await gh.unresolve_thread(thread_id)
    if ok:
        return f"↩️ Thread {thread_id} is now unresolved."
    return f"⚠️ Failed to unresolve thread {thread_id}."


# ── 10. Bulk-resolve threads ──────────────────────────────────────────────
@mcp.tool()
async def resolve_all_threads(pr_number: int) -> str:
    """Resolve every unresolved review thread on a PR.

    :param pr_number: The pull request number.
    :returns: Summary of resolved threads.
    """
    gh = _client()
    threads = await gh.list_review_threads(pr_number)
    unresolved = [t for t in threads if not t.is_resolved]
    results = []
    for t in unresolved:
        try:
            await gh.resolve_thread(t.thread_id)
            results.append({"thread_id": t.thread_id, "status": "resolved"})
        except Exception as exc:
            results.append({"thread_id": t.thread_id, "status": f"error: {exc}"})
    return json.dumps(
        {
            "resolved_count": sum(1 for r in results if r["status"] == "resolved"),
            "total_unresolved": len(unresolved),
            "details": results,
        },
        indent=2,
    )


# ── 11. Post a top-level PR comment ───────────────────────────────────────
@mcp.tool()
async def post_pr_comment(pr_number: int, body: str) -> str:
    """Post a new top-level comment on the PR (not a review comment).

    Good for posting a summary of all changes made in response to review.

    :param pr_number: The pull request number.
    :param body: Comment body (Markdown).
    :returns: JSON of the created comment.
    """
    gh = _client()
    result = await gh.create_issue_comment(pr_number, body)
    return json.dumps(
        {"id": result["id"], "url": result["html_url"], "body": result["body"]},
        indent=2,
    )


# ── 12. Generate a fix plan from unresolved comments ──────────────────────
@mcp.tool()
async def generate_fix_plan(pr_number: int) -> str:
    """Analyze all unresolved review threads and produce a structured fix plan.

    For each unresolved thread, returns:
    - The file and line to change
    - The reviewer's request
    - The relevant diff hunk for context

    :param pr_number: The pull request number.
    :returns: JSON array of fix-plan items, ordered by file path.
    """
    gh = _client()
    threads = await gh.list_review_threads(pr_number)
    unresolved = [t for t in threads if not t.is_resolved]

    plan = []
    for t in unresolved:
        first = t.comments[0] if t.comments else None
        last = t.comments[-1] if t.comments else None
        plan.append(
            {
                "thread_id": t.thread_id,
                "file": t.path,
                "line": t.line,
                "reviewer": first.user if first else "unknown",
                "original_request": first.body if first else "",
                "latest_comment": last.body if last and last != first else None,
                "diff_hunk": first.diff_hunk if first else "",
                "comment_id": first.id if first else None,
                "url": first.html_url if first else "",
            }
        )
    plan.sort(key=lambda x: (x["file"], x["line"] or 0))
    return json.dumps(plan, indent=2)


# ── 13. Batch reply and resolve ───────────────────────────────────────────
@mcp.tool()
async def batch_reply_and_resolve(
    pr_number: int,
    items: str,
) -> str:
    """Reply to multiple review comments and resolve their threads in one call.

    *items* is a JSON string — an array of objects, each with:
      - ``comment_id`` (int): The review comment to reply to.
      - ``thread_id`` (str): The GraphQL thread node ID to resolve.
      - ``reply`` (str): The reply body text.

    :param pr_number: The pull request number.
    :param items: JSON array string of {comment_id, thread_id, reply} objects.
    :returns: JSON summary of results.
    """
    gh = _client()
    entries = json.loads(items)
    results = []
    for entry in entries:
        cid = entry["comment_id"]
        tid = entry["thread_id"]
        reply_body = entry["reply"]
        result: dict = {"comment_id": cid, "thread_id": tid}
        try:
            await gh.reply_to_review_comment(pr_number, cid, reply_body)
            result["reply_status"] = "ok"
        except Exception as exc:
            result["reply_status"] = f"error: {exc}"
        try:
            await gh.resolve_thread(tid)
            result["resolve_status"] = "ok"
        except Exception as exc:
            result["resolve_status"] = f"error: {exc}"
        results.append(result)
    return json.dumps(results, indent=2)




# ── 14. Evaluate review comments (triage) ─────────────────────────────────────
@mcp.tool()
async def evaluate_review_comments(pr_number: int) -> str:
    """Evaluate all unresolved review threads and triage them.

    Analyzes each unresolved thread using heuristic rules to determine
    whether the reviewer's concern is valid, should be dismissed, or is
    optional. This helps prioritise which review comments actually need
    code changes vs. which can be resolved with a reply.

    Verdicts:
      - VALID   — Real issue, should fix the code.
      - DISMISS — Bot was wrong or concern does not apply.
      - OPTIONAL — Nice-to-have suggestion, not a bug.

    :param pr_number: The pull request number.
    :returns: JSON array of evaluation objects with verdicts and reasoning.
    """
    gh = _client()
    evaluations = await evaluate_threads(gh, pr_number)
    return json.dumps([e.to_dict() for e in evaluations], indent=2)
# ---------------------------------------------------------------------------
# Entrypoint
# ---------------------------------------------------------------------------

def main() -> None:
    """Run the MCP server (stdio transport)."""
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
