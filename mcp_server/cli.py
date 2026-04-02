#!/usr/bin/env python3
"""CLI driver for the PR Review MCP server.

Designed as a **companion tool** for any git project.  It auto-detects
the repository and current PR from your working directory so you can
jump straight into reviewing without any configuration.

Usage::

    # Inside a git checkout that has an open PR on the current branch:
    pr-review overview          # auto-detects repo + PR
    pr-review evaluate          # triage unresolved threads

    # Specify a PR number (repo still auto-detected from git remote):
    pr-review overview 2516

    # Working with a *different* repo — provide the full URL:
    pr-review overview https://github.com/owner/repo/pull/123

    # Other commands
    pr-review unresolved
    pr-review fix-plan
    pr-review diff src/main.py
    pr-review reply --comment-id 12345 --body "Fixed in abc123"
    pr-review batch-resolve --evaluate

Environment variables (all optional — auto-detected when possible):
    GITHUB_TOKEN        GitHub PAT with repo scope (falls back to ~/.netrc)
    GITHUB_OWNER        Repo owner — auto-detected from git remote
    GITHUB_REPO         Repo name  — auto-detected from git remote
    GITHUB_API_BASE     REST API base URL
    GITHUB_GRAPHQL_URL  GraphQL endpoint
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import subprocess
import sys
import textwrap
from typing import Any

# Ensure sibling imports work when run as a script
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from github_client import GitHubClient  # noqa: E402
from evaluator import evaluate_threads, Verdict  # noqa: E402


# ---------------------------------------------------------------------------
# Git helpers — auto-detect everything from the current working directory
# ---------------------------------------------------------------------------

def _detect_remote() -> tuple[str, str, str]:
    """Detect owner, repo, and host from git remote.

    :returns: (owner, repo, host) — any may be empty on failure.
    """
    try:
        url = subprocess.check_output(
            ["git", "remote", "get-url", "origin"],
            text=True, stderr=subprocess.DEVNULL,
        ).strip()
        m = re.search(
            r"(?:https?://(?P<host>[^/]+)/|git@(?P<sshhost>[^:]+):)"
            r"(?P<owner>[^/]+)/(?P<repo>[^/]+?)(?:\.git)?$",
            url,
        )
        if m:
            host = m.group("host") or m.group("sshhost") or ""
            return m.group("owner"), m.group("repo"), host
    except Exception:
        pass
    return "", "", ""


def _detect_branch() -> str:
    """Return the name of the current git branch, or ``""``."""
    try:
        return subprocess.check_output(
            ["git", "rev-parse", "--abbrev-ref", "HEAD"],
            text=True, stderr=subprocess.DEVNULL,
        ).strip()
    except Exception:
        return ""


def _resolve_token(api_host: str = "") -> str:
    """Find a GitHub token from env or ~/.netrc."""
    token = os.environ.get("GITHUB_TOKEN", "")
    if token:
        return token

    netrc_path = os.path.expanduser("~/.netrc")
    if os.path.isfile(netrc_path):
        try:
            import netrc as netrc_mod
            hosts_to_try = ["github.com", "api.github.com"]
            if api_host:
                hosts_to_try.insert(0, api_host)
            nrc = netrc_mod.netrc(netrc_path)
            for host in hosts_to_try:
                auth = nrc.authenticators(host)
                if auth and auth[2]:
                    return auth[2]
        except Exception:
            pass
    return ""


def _api_endpoints(host: str) -> tuple[str, str]:
    """Derive REST + GraphQL URLs from the git remote host."""
    if not host or host in ("github.com", "www.github.com"):
        return "https://api.github.com", "https://api.github.com/graphql"
    return f"https://{host}/api/v3", f"https://{host}/api/graphql"


def _parse_pr_ref(value: str) -> dict[str, Any]:
    """Parse a PR URL or plain number.

    :returns: Dict with ``pr_number`` (int) and optionally
        ``owner``, ``repo``, ``api_base``, ``graphql_url``.
    """
    value = value.strip()
    m = re.match(
        r"https?://(?P<host>[^/]+)/(?P<owner>[^/]+)/(?P<repo>[^/]+)"
        r"/pull/(?P<pr>\d+)",
        value,
    )
    if m:
        host = m.group("host")
        api_base, graphql_url = _api_endpoints(host)
        return {
            "owner": m.group("owner"),
            "repo": m.group("repo"),
            "pr_number": int(m.group("pr")),
            "api_base": api_base,
            "graphql_url": graphql_url,
        }
    if value.isdigit():
        return {"pr_number": int(value)}
    raise ValueError(
        f"Cannot parse '{value}' as a PR URL or number.\n"
        "Expected formats:\n"
        "  • https://github.com/owner/repo/pull/123\n"
        "  • https://github.intel.com/org/repo/pull/456\n"
        "  • 123"
    )


# ---------------------------------------------------------------------------
# Client factory — builds a GitHubClient with full auto-detection
# ---------------------------------------------------------------------------

# Module-level cache so auto-detected PR is reused across calls
_resolved_context: dict[str, Any] = {}


def _resolve_context(pr_ref: str | None = None) -> dict[str, Any]:
    """Resolve owner, repo, api_base, graphql_url, and pr_number.

    Resolution order:
      1. If *pr_ref* is a full URL → everything comes from the URL.
      2. If *pr_ref* is a plain number → repo from env / git remote.
      3. If *pr_ref* is None → repo from env / git remote, PR from
         current branch auto-detection via GitHub API.
    """
    if _resolved_context:
        return _resolved_context

    ctx: dict[str, Any] = {}

    # --- Owner / repo / host ---
    if pr_ref and not pr_ref.isdigit():
        try:
            parsed = _parse_pr_ref(pr_ref)
            ctx.update(parsed)
            _resolved_context.update(ctx)
            return ctx
        except ValueError:
            raise

    # Detect from env / git remote
    auto_owner, auto_repo, auto_host = _detect_remote()
    ctx["owner"] = os.environ.get("GITHUB_OWNER", auto_owner)
    ctx["repo"] = os.environ.get("GITHUB_REPO", auto_repo)
    api_base, graphql_url = _api_endpoints(auto_host)
    ctx["api_base"] = os.environ.get("GITHUB_API_BASE", api_base)
    ctx["graphql_url"] = os.environ.get("GITHUB_GRAPHQL_URL", graphql_url)

    if not ctx["owner"] or not ctx["repo"]:
        _print_no_repo_help()
        sys.exit(1)

    # --- PR number ---
    if pr_ref and pr_ref.strip().isdigit():
        ctx["pr_number"] = int(pr_ref.strip())
    # else: auto-detect from current branch (deferred to _auto_detect_pr)

    _resolved_context.update(ctx)
    return ctx


async def _auto_detect_pr(gh: GitHubClient) -> int | None:
    """Try to find an open PR for the current git branch."""
    branch = _detect_branch()
    if not branch or branch in ("HEAD", "main", "master"):
        return None
    try:
        prs = await gh.list_prs_for_branch(branch, state="open")
        if prs:
            return prs[0]["number"]
    except Exception:
        pass
    return None


def make_client(ctx: dict[str, Any] | None = None) -> GitHubClient:
    """Build a GitHubClient from the resolved context."""
    ctx = ctx or _resolved_context
    token = _resolve_token(ctx.get("api_base", ""))
    if not token:
        _print_no_token_help()
        sys.exit(1)
    return GitHubClient(
        token=token,
        owner=ctx["owner"],
        repo=ctx["repo"],
        api_base=ctx.get("api_base", "https://api.github.com"),
        graphql_url=ctx.get("graphql_url", "https://api.github.com/graphql"),
    )


async def resolve_pr_number(args: argparse.Namespace) -> int:
    """Get the PR number — from args, context cache, or auto-detection.

    Prints a friendly message and exits if nothing can be resolved.
    """
    ctx = _resolve_context(getattr(args, "pr_ref", None))

    if "pr_number" in ctx:
        return ctx["pr_number"]

    # Auto-detect from current branch
    gh = make_client(ctx)
    pr_num = await _auto_detect_pr(gh)
    if pr_num:
        print(f"Auto-detected PR #{pr_num} from current branch '{_detect_branch()}'.\n")
        ctx["pr_number"] = pr_num
        _resolved_context["pr_number"] = pr_num
        return pr_num

    _print_no_pr_help()
    sys.exit(1)


# ---------------------------------------------------------------------------
# Help messages
# ---------------------------------------------------------------------------

def _print_no_repo_help() -> None:
    print(
        "ERROR: Cannot detect repository.\n"
        "\n"
        "Run this command from inside a git checkout, or provide a full PR URL:\n"
        "\n"
        "  pr-review overview https://github.com/owner/repo/pull/123\n"
        "  pr-review overview https://github.intel.com/org/repo/pull/456\n"
        "\n"
        "Alternatively, set environment variables:\n"
        "  export GITHUB_OWNER=owner\n"
        "  export GITHUB_REPO=repo\n",
        file=sys.stderr,
    )


def _print_no_token_help() -> None:
    print(
        "ERROR: No GitHub token found.\n"
        "\n"
        "Set up authentication using one of these methods:\n"
        "\n"
        "  1. Environment variable:\n"
        '     export GITHUB_TOKEN="ghp_xxxxxxxxxxxx"\n'
        "\n"
        "  2. ~/.netrc file (recommended):\n"
        "     machine github.com\n"
        "       login your-username\n"
        "       password ghp_xxxxxxxxxxxx\n"
        "\n"
        "     Then: chmod 600 ~/.netrc\n"
        "\n"
        "  Create a token at: GitHub → Settings → Developer Settings\n"
        "  → Personal Access Tokens → Generate new token (classic)\n"
        "  → Select the 'repo' scope.\n",
        file=sys.stderr,
    )


def _print_no_pr_help() -> None:
    branch = _detect_branch()
    owner, repo, _ = _detect_remote()
    repo_str = f"{owner}/{repo}" if owner and repo else "<owner>/<repo>"

    print(
        "ERROR: Cannot determine which PR to review.\n"
        "\n"
        f"  Current branch: {branch or '(not in a git repo)'}\n"
        f"  Repository:     {repo_str}\n"
        "\n"
        "No open PR was found for this branch. Provide the PR explicitly:\n"
        "\n"
        f"  pr-review overview 2516\n"
        f"  pr-review overview https://github.com/{repo_str}/pull/2516\n"
        "\n"
        "Or switch to the branch that has an open PR:\n"
        f"  git checkout <your-pr-branch>\n"
        f"  pr-review overview\n",
        file=sys.stderr,
    )


# ---------------------------------------------------------------------------
# Output helpers
# ---------------------------------------------------------------------------

def _json_print(data: Any) -> None:
    print(json.dumps(data, indent=2))


def _table_print(
    rows: list[dict], columns: list[str], widths: dict[str, int] | None = None
) -> None:
    """Print a simple ASCII table."""
    widths = widths or {}
    col_widths = {c: max(len(c), widths.get(c, 0)) for c in columns}
    for row in rows:
        for c in columns:
            val = str(row.get(c, ""))
            col_widths[c] = max(col_widths[c], min(len(val), 80))

    header = " | ".join(c.ljust(col_widths[c]) for c in columns)
    sep = "-+-".join("-" * col_widths[c] for c in columns)
    print(header)
    print(sep)
    for row in rows:
        line = " | ".join(
            str(row.get(c, ""))[:col_widths[c]].ljust(col_widths[c])
            for c in columns
        )
        print(line)


# ---------------------------------------------------------------------------
# Subcommands
# ---------------------------------------------------------------------------

async def cmd_overview(args: argparse.Namespace) -> None:
    """PR overview: title, state, author, files, reviews."""
    pr_number = await resolve_pr_number(args)
    gh = make_client()
    pr = await gh.get_pr(pr_number)
    reviews = await gh.list_reviews(pr_number)
    files = await gh.get_pr_files(pr_number)
    threads = await gh.list_review_threads(pr_number)
    resolved = sum(1 for t in threads if t.is_resolved)
    unresolved = sum(1 for t in threads if not t.is_resolved)

    print(f"PR #{pr['number']}: {pr['title']}")
    print(f"  State:    {pr['state']}")
    print(f"  Author:   {pr['user']['login']}")
    print(f"  Branch:   {pr['head']['ref']} → {pr['base']['ref']}")
    print(f"  Files:    {len(files)} changed")
    print(f"  Reviews:  {len(reviews)}")
    print(f"  Threads:  {resolved} resolved, {unresolved} unresolved")
    if args.json:
        _json_print({
            "number": pr["number"], "title": pr["title"], "state": pr["state"],
            "author": pr["user"]["login"], "files": len(files),
            "reviews": len(reviews), "resolved": resolved, "unresolved": unresolved,
        })


async def cmd_unresolved(args: argparse.Namespace) -> None:
    """List unresolved review threads."""
    pr_number = await resolve_pr_number(args)
    gh = make_client()
    threads = await gh.list_review_threads(pr_number)
    unresolved = [t for t in threads if not t.is_resolved]

    if not unresolved:
        print(f"No unresolved threads on PR #{pr_number}.")
        return

    print(f"{len(unresolved)} unresolved thread(s) on PR #{pr_number}:\n")
    rows = []
    for t in unresolved:
        first = t.comments[0] if t.comments else None
        rows.append({
            "file": f"{t.path}:{t.line or '?'}",
            "reviewer": (first.user if first else "?")[:20],
            "comment": (
                first.body[:60] + "..." if first and len(first.body) > 60
                else (first.body if first else "")
            ),
            "thread_id": t.thread_id,
            "comment_id": first.id if first else "",
        })

    if args.json:
        _json_print(rows)
    else:
        _table_print(rows, ["file", "reviewer", "comment"])


async def cmd_evaluate(args: argparse.Namespace) -> None:
    """Evaluate unresolved threads — triage before fixing."""
    pr_number = await resolve_pr_number(args)
    gh = make_client()
    evaluations = await evaluate_threads(gh, pr_number)

    if not evaluations:
        print(f"No unresolved threads to evaluate on PR #{pr_number}.")
        return

    valid = [e for e in evaluations if e.verdict == Verdict.VALID]
    dismiss = [e for e in evaluations if e.verdict == Verdict.DISMISS]
    optional = [e for e in evaluations if e.verdict == Verdict.OPTIONAL]

    print(f"Evaluation of {len(evaluations)} unresolved thread(s) on PR #{pr_number}:\n")
    print(f"  VALID (should fix):      {len(valid)}")
    print(f"  DISMISS (not needed):    {len(dismiss)}")
    print(f"  OPTIONAL (nice-to-have): {len(optional)}")
    print()

    for ev in evaluations:
        icon = {"VALID": "⚠️ ", "DISMISS": "❌", "OPTIONAL": "💡"}[ev.verdict.value]
        print(f"{icon} [{ev.verdict.value}] {ev.file}:{ev.line or '?'}")
        print(f"   Reviewer:   {ev.reviewer}")
        print(f"   Confidence: {ev.confidence}")
        print(f"   Comment:    {ev.comment_preview}")
        print(f"   Reasoning:  {ev.reasoning}")
        if ev.suggested_reply:
            print(f"   Reply hint: {ev.suggested_reply}")
        print()

    if args.json:
        _json_print([e.to_dict() for e in evaluations])


async def cmd_fix_plan(args: argparse.Namespace) -> None:
    """Generate a fix plan from unresolved threads."""
    pr_number = await resolve_pr_number(args)
    gh = make_client()
    threads = await gh.list_review_threads(pr_number)
    unresolved = [t for t in threads if not t.is_resolved]

    plan = []
    for t in unresolved:
        first = t.comments[0] if t.comments else None
        last = t.comments[-1] if t.comments else None
        plan.append({
            "thread_id": t.thread_id,
            "file": t.path,
            "line": t.line,
            "reviewer": first.user if first else "unknown",
            "request": first.body if first else "",
            "latest_reply": last.body if last and last != first else None,
            "diff_hunk": first.diff_hunk if first else "",
            "comment_id": first.id if first else None,
        })
    plan.sort(key=lambda x: (x["file"], x["line"] or 0))

    if args.json:
        _json_print(plan)
    else:
        for i, item in enumerate(plan, 1):
            print(f"{i}. {item['file']}:{item['line'] or '?'}")
            print(f"   Reviewer: {item['reviewer']}")
            print(f"   Request:  {item['request'][:100]}...")
            print(f"   Thread:   {item['thread_id']}")
            print()


async def cmd_diff(args: argparse.Namespace) -> None:
    """Show the diff for a specific file in the PR."""
    pr_number = await resolve_pr_number(args)
    gh = make_client()
    files = await gh.get_pr_files(pr_number)
    for f in files:
        if f["filename"] == args.file_path:
            print(f.get("patch", "(binary file)"))
            return
    print(f"File '{args.file_path}' not in PR #{pr_number} changed files.",
          file=sys.stderr)
    sys.exit(1)


async def cmd_reply(args: argparse.Namespace) -> None:
    """Reply to a comment and optionally resolve its thread."""
    pr_number = await resolve_pr_number(args)
    gh = make_client()
    reply = await gh.reply_to_review_comment(pr_number, args.comment_id, args.body)
    print(f"Replied to comment {args.comment_id}: {reply.html_url}")

    if args.resolve and args.thread_id:
        ok = await gh.resolve_thread(args.thread_id)
        print(f"Thread {'resolved' if ok else 'FAILED to resolve'}: {args.thread_id}")


async def cmd_batch_resolve(args: argparse.Namespace) -> None:
    """Reply to all unresolved threads and resolve them."""
    pr_number = await resolve_pr_number(args)
    gh = make_client()
    threads = await gh.list_review_threads(pr_number)
    unresolved = [t for t in threads if not t.is_resolved]

    if not unresolved:
        print("No unresolved threads.")
        return

    # If --evaluate, triage first
    if args.evaluate:
        evaluations = await evaluate_threads(gh, pr_number)
        eval_map = {e.thread_id: e for e in evaluations}
    else:
        eval_map = {}

    replied = 0
    resolved = 0
    skipped = 0

    for t in unresolved:
        first = t.comments[0] if t.comments else None
        cid = first.id if first else None
        ev = eval_map.get(t.thread_id)

        # Skip VALID threads (those need actual code fixes)
        if ev and ev.verdict == Verdict.VALID and not args.force:
            print(f"  SKIP (VALID — needs fix) {t.path}:{t.line}")
            skipped += 1
            continue

        # Determine reply message
        if ev and ev.suggested_reply:
            msg = ev.suggested_reply
        else:
            msg = args.message or "Acknowledged — reviewed and addressed."

        # Reply
        if cid:
            try:
                await gh.reply_to_review_comment(pr_number, cid, msg)
                replied += 1
                print(f"  Replied: {t.path}:{t.line}")
            except Exception as e:
                print(f"  Reply FAILED: {t.path}:{t.line}: {e}", file=sys.stderr)

        # Resolve
        try:
            ok = await gh.resolve_thread(t.thread_id)
            if ok:
                resolved += 1
        except Exception as e:
            print(f"  Resolve FAILED: {t.thread_id}: {e}", file=sys.stderr)

    print(f"\nDone: replied={replied}, resolved={resolved}, skipped={skipped}")


async def cmd_comments(args: argparse.Namespace) -> None:
    """List all review comments grouped by file."""
    pr_number = await resolve_pr_number(args)
    gh = make_client()
    comments = await gh.list_review_comments(pr_number)
    by_path: dict[str, list] = {}
    for c in comments:
        by_path.setdefault(c.path, []).append({
            "id": c.id, "line": c.line, "user": c.user,
            "body": c.body[:100] + ("..." if len(c.body) > 100 else ""),
        })

    if args.json:
        _json_print(by_path)
    else:
        for path, cmts in by_path.items():
            print(f"\n{path} ({len(cmts)} comment{'s' if len(cmts) != 1 else ''}):")
            for c in cmts:
                print(f"  [{c['id']}] L{c['line'] or '?'} {c['user']}: {c['body'][:70]}")


# ---------------------------------------------------------------------------
# Argument parser
# ---------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="pr-review",
        description="CLI for PR Review MCP — companion tool for any git project.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=textwrap.dedent("""\
            Auto-detection:
              Run from inside a git checkout — the repo and open PR are
              detected automatically.  No configuration needed.

            Examples:
              pr-review overview                              # auto-detect everything
              pr-review overview 2516                         # specify PR number
              pr-review overview https://github.com/o/r/pull/42  # different repo
              pr-review evaluate                              # triage review comments
              pr-review unresolved --json                     # machine-readable
              pr-review batch-resolve --evaluate              # smart bulk resolve
              pr-review diff src/main.py                      # file diff from PR
        """),
    )
    parser.add_argument("--json", action="store_true", help="Output as JSON")
    sub = parser.add_subparsers(dest="command", required=True)

    # Each subcommand takes an optional pr_ref (URL or number)
    def _add_pr_ref(p: argparse.ArgumentParser) -> None:
        p.add_argument(
            "pr_ref", nargs="?", default=None,
            help=(
                "PR number or full URL (optional — auto-detected from "
                "current branch if omitted)"
            ),
        )

    # overview
    p = sub.add_parser("overview", help="PR overview")
    _add_pr_ref(p)

    # comments
    p = sub.add_parser("comments", help="List all review comments")
    _add_pr_ref(p)

    # unresolved
    p = sub.add_parser("unresolved", help="List unresolved threads")
    _add_pr_ref(p)

    # evaluate
    p = sub.add_parser("evaluate", help="Evaluate/triage unresolved threads")
    _add_pr_ref(p)

    # fix-plan
    p = sub.add_parser("fix-plan", help="Generate fix plan")
    _add_pr_ref(p)

    # diff
    p = sub.add_parser("diff", help="Show file diff from PR")
    _add_pr_ref(p)
    p.add_argument("file_path", help="Path of file in the PR")

    # reply
    p = sub.add_parser("reply", help="Reply to a review comment")
    _add_pr_ref(p)
    p.add_argument("--comment-id", type=int, required=True)
    p.add_argument("--thread-id", type=str, help="Thread node ID (for --resolve)")
    p.add_argument("--body", type=str, required=True, help="Reply text")
    p.add_argument("--resolve", action="store_true", help="Also resolve the thread")

    # batch-resolve
    p = sub.add_parser("batch-resolve", help="Reply & resolve all unresolved threads")
    _add_pr_ref(p)
    p.add_argument("--message", type=str, default="Acknowledged — reviewed and addressed.")
    p.add_argument("--evaluate", action="store_true",
                   help="Triage first — skip VALID threads (those need actual fixes)")
    p.add_argument("--force", action="store_true",
                   help="With --evaluate, also resolve VALID threads")

    return parser


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

COMMANDS = {
    "overview": cmd_overview,
    "comments": cmd_comments,
    "unresolved": cmd_unresolved,
    "evaluate": cmd_evaluate,
    "fix-plan": cmd_fix_plan,
    "diff": cmd_diff,
    "reply": cmd_reply,
    "batch-resolve": cmd_batch_resolve,
}


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()
    handler = COMMANDS.get(args.command)
    if not handler:
        parser.print_help()
        sys.exit(1)
    asyncio.run(handler(args))


if __name__ == "__main__":
    main()
