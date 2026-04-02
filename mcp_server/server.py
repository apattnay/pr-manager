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

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Bootstrap
# ---------------------------------------------------------------------------

mcp = FastMCP(
    "pr-review-mcp",
    instructions=(
        "GitHub PR Review MCP — fetch PR comments, reviews, threads; "
        "reply to & resolve review conversations; get file diffs for fixing."
    ),
)


def _detect_remote() -> tuple[str, str]:
    """Try to detect owner/repo from the git remote in cwd."""
    try:
        url = subprocess.check_output(
            ["git", "remote", "get-url", "origin"],
            text=True,
            stderr=subprocess.DEVNULL,
        ).strip()
        # Handle HTTPS and SSH remotes
        m = re.search(r"[:/]([^/]+)/([^/]+?)(?:\.git)?$", url)
        if m:
            return m.group(1), m.group(2)
    except Exception:
        pass
    return "", ""


def _client() -> GitHubClient:
    """Build a GitHubClient from env vars (+ optional git-remote fallback)."""
    token = os.environ.get("GITHUB_TOKEN", "")
    if not token:
        raise RuntimeError(
            "GITHUB_TOKEN environment variable is required. "
            "Set it to a GitHub PAT with 'repo' scope."
        )
    auto_owner, auto_repo = _detect_remote()
    owner = os.environ.get("GITHUB_OWNER", auto_owner)
    repo = os.environ.get("GITHUB_REPO", auto_repo)
    if not owner or not repo:
        raise RuntimeError(
            "Cannot determine repository. Set GITHUB_OWNER and GITHUB_REPO "
            "environment variables, or run from inside a git checkout."
        )
    return GitHubClient(
        token=token,
        owner=owner,
        repo=repo,
        api_base=os.environ.get("GITHUB_API_BASE", "https://api.github.com"),
        graphql_url=os.environ.get(
            "GITHUB_GRAPHQL_URL", "https://api.github.com/graphql"
        ),
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


# ---------------------------------------------------------------------------
# Entrypoint
# ---------------------------------------------------------------------------

def main() -> None:
    """Run the MCP server (stdio transport)."""
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
