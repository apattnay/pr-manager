"""GitHub REST / GraphQL client for PR review operations.

Wraps the GitHub API to provide high-level helpers consumed by the MCP
server tools.  Requires a personal-access token (classic or fine-grained)
with at least the ``repo`` scope.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import Any

import httpx

logger = logging.getLogger(__name__)

_GITHUB_API = "https://api.github.com"
_GITHUB_GRAPHQL = "https://api.github.com/graphql"


# ---------------------------------------------------------------------------
# Data containers
# ---------------------------------------------------------------------------
@dataclass
class ReviewComment:
    """A single review comment on a PR."""

    id: int
    node_id: str
    path: str
    line: int | None
    original_line: int | None
    side: str
    body: str
    user: str
    state: str  # e.g. "COMMENTED", "CHANGES_REQUESTED", …
    in_reply_to_id: int | None = None
    created_at: str = ""
    updated_at: str = ""
    diff_hunk: str = ""
    html_url: str = ""
    pull_request_review_id: int | None = None


@dataclass
class ReviewThread:
    """A conversation thread (top-level comment + replies)."""

    thread_id: str  # GraphQL node id of the thread
    is_resolved: bool
    path: str
    line: int | None
    comments: list[ReviewComment] = field(default_factory=list)


@dataclass
class Review:
    """A PR review (approved / changes-requested / commented)."""

    id: int
    node_id: str
    user: str
    state: str
    body: str
    submitted_at: str
    html_url: str


# ---------------------------------------------------------------------------
# Client
# ---------------------------------------------------------------------------
class GitHubClient:
    """Thin async wrapper around the GitHub API for PR review ops.

    :param token: GitHub personal access token.
    :param owner: Repository owner (org or user).
    :param repo: Repository name.
    :param api_base: Base URL for GitHub REST API.
    :param graphql_url: URL for GitHub GraphQL API.
    """

    def __init__(
        self,
        token: str,
        owner: str,
        repo: str,
        *,
        api_base: str = _GITHUB_API,
        graphql_url: str = _GITHUB_GRAPHQL,
    ) -> None:
        self._token = token
        self._owner = owner
        self._repo = repo
        self._api_base = api_base.rstrip("/")
        self._graphql_url = graphql_url
        self._headers = {
            "Authorization": f"token {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }

    # -- helpers -------------------------------------------------------------
    def _url(self, path: str) -> str:
        return f"{self._api_base}/repos/{self._owner}/{self._repo}/{path}"

    async def _get(self, path: str, **params: Any) -> Any:
        async with httpx.AsyncClient(headers=self._headers, timeout=30) as c:
            resp = await c.get(self._url(path), params=params)
            resp.raise_for_status()
            return resp.json()

    async def _post(self, path: str, payload: dict) -> Any:
        async with httpx.AsyncClient(headers=self._headers, timeout=30) as c:
            resp = await c.post(self._url(path), json=payload)
            resp.raise_for_status()
            return resp.json()

    async def _patch(self, path: str, payload: dict) -> Any:
        async with httpx.AsyncClient(headers=self._headers, timeout=30) as c:
            resp = await c.patch(self._url(path), json=payload)
            resp.raise_for_status()
            return resp.json()

    async def _delete(self, path: str) -> None:
        async with httpx.AsyncClient(headers=self._headers, timeout=30) as c:
            resp = await c.delete(self._url(path))
            resp.raise_for_status()

    async def _graphql(self, query: str, variables: dict | None = None) -> Any:
        payload: dict[str, Any] = {"query": query}
        if variables:
            payload["variables"] = variables
        async with httpx.AsyncClient(headers=self._headers, timeout=30) as c:
            resp = await c.post(self._graphql_url, json=payload)
            resp.raise_for_status()
            data = resp.json()
            if "errors" in data:
                raise RuntimeError(
                    f"GraphQL errors: {json.dumps(data['errors'], indent=2)}"
                )
            return data["data"]

    # ── PR metadata ─────────────────────────────────────────────────────────
    async def get_pr(self, pr_number: int) -> dict:
        """Return the full PR object."""
        return await self._get(f"pulls/{pr_number}")

    async def list_prs_for_branch(
        self, head_branch: str, state: str = "open"
    ) -> list[dict]:
        """List PRs whose head matches *head_branch*.

        :param head_branch: Branch name (e.g. ``feature/foo``).
            The API requires ``owner:branch`` format — this method
            adds the owner prefix automatically.
        :param state: PR state filter (``open``, ``closed``, ``all``).
        :returns: List of PR dicts (may be empty).
        """
        head_param = f"{self._owner}:{head_branch}"
        return await self._get("pulls", state=state, head=head_param)

    async def get_pr_files(self, pr_number: int) -> list[dict]:
        """Return the list of changed files in a PR."""
        return await self._get(f"pulls/{pr_number}/files")

    # ── Reviews ─────────────────────────────────────────────────────────────
    async def list_reviews(self, pr_number: int) -> list[Review]:
        """List all reviews on a PR."""
        raw = await self._get(f"pulls/{pr_number}/reviews")
        return [
            Review(
                id=r["id"],
                node_id=r["node_id"],
                user=r["user"]["login"],
                state=r["state"],
                body=r.get("body", ""),
                submitted_at=r.get("submitted_at", ""),
                html_url=r.get("html_url", ""),
            )
            for r in raw
        ]

    # ── Review comments (REST) ──────────────────────────────────────────────
    async def list_review_comments(self, pr_number: int) -> list[ReviewComment]:
        """Return *all* review comments on a PR (paginated)."""
        page = 1
        all_comments: list[ReviewComment] = []
        while True:
            raw = await self._get(
                f"pulls/{pr_number}/comments", per_page=100, page=page
            )
            if not raw:
                break
            for c in raw:
                all_comments.append(self._parse_comment(c))
            page += 1
        return all_comments

    async def get_review_comment(self, comment_id: int) -> ReviewComment:
        """Fetch a single review comment by id."""
        raw = await self._get(f"pulls/comments/{comment_id}")
        return self._parse_comment(raw)

    async def reply_to_review_comment(
        self, pr_number: int, comment_id: int, body: str
    ) -> ReviewComment:
        """Reply to a review comment (creates a new comment in the thread)."""
        raw = await self._post(
            f"pulls/{pr_number}/comments/{comment_id}/replies",
            {"body": body},
        )
        return self._parse_comment(raw)

    async def update_review_comment(
        self, comment_id: int, body: str
    ) -> ReviewComment:
        """Edit the body of an existing review comment."""
        raw = await self._patch(
            f"pulls/comments/{comment_id}",
            {"body": body},
        )
        return self._parse_comment(raw)

    async def delete_review_comment(self, comment_id: int) -> None:
        """Delete a review comment."""
        await self._delete(f"pulls/comments/{comment_id}")

    # ── Issue comments (top-level PR conversation) ──────────────────────────
    async def list_issue_comments(self, pr_number: int) -> list[dict]:
        """List top-level (non-review) comments on a PR."""
        return await self._get(f"issues/{pr_number}/comments")

    async def create_issue_comment(self, pr_number: int, body: str) -> dict:
        """Post a new top-level comment on the PR."""
        return await self._post(f"issues/{pr_number}/comments", {"body": body})

    # ── Resolve / unresolve threads (GraphQL) ───────────────────────────────
    async def list_review_threads(self, pr_number: int) -> list[ReviewThread]:
        """Return all review threads using GraphQL (resolved + unresolved)."""
        query = """
        query($owner: String!, $repo: String!, $pr: Int!, $cursor: String) {
          repository(owner: $owner, name: $repo) {
            pullRequest(number: $pr) {
              reviewThreads(first: 100, after: $cursor) {
                pageInfo { hasNextPage endCursor }
                nodes {
                  id
                  isResolved
                  path
                  line
                  comments(first: 100) {
                    nodes {
                      id
                      databaseId
                      body
                      author { login }
                      createdAt
                      updatedAt
                      path
                      line: originalLine
                      diffHunk
                      url
                    }
                  }
                }
              }
            }
          }
        }
        """
        threads: list[ReviewThread] = []
        cursor: str | None = None
        while True:
            data = await self._graphql(
                query,
                {
                    "owner": self._owner,
                    "repo": self._repo,
                    "pr": pr_number,
                    "cursor": cursor,
                },
            )
            page = data["repository"]["pullRequest"]["reviewThreads"]
            for node in page["nodes"]:
                comments = [
                    ReviewComment(
                        id=cm["databaseId"] or 0,
                        node_id=cm["id"],
                        path=cm.get("path", ""),
                        line=cm.get("line"),
                        original_line=cm.get("line"),
                        side="RIGHT",
                        body=cm["body"],
                        user=cm["author"]["login"] if cm.get("author") else "",
                        state="COMMENTED",
                        created_at=cm.get("createdAt", ""),
                        updated_at=cm.get("updatedAt", ""),
                        diff_hunk=cm.get("diffHunk", ""),
                        html_url=cm.get("url", ""),
                    )
                    for cm in node["comments"]["nodes"]
                ]
                threads.append(
                    ReviewThread(
                        thread_id=node["id"],
                        is_resolved=node["isResolved"],
                        path=node.get("path", ""),
                        line=node.get("line"),
                        comments=comments,
                    )
                )
            if page["pageInfo"]["hasNextPage"]:
                cursor = page["pageInfo"]["endCursor"]
            else:
                break
        return threads

    async def resolve_thread(self, thread_node_id: str) -> bool:
        """Mark a review thread as resolved (GraphQL mutation)."""
        mutation = """
        mutation($threadId: ID!) {
          resolveReviewThread(input: {threadId: $threadId}) {
            thread { id isResolved }
          }
        }
        """
        data = await self._graphql(mutation, {"threadId": thread_node_id})
        return data["resolveReviewThread"]["thread"]["isResolved"]

    async def unresolve_thread(self, thread_node_id: str) -> bool:
        """Mark a review thread as unresolved (GraphQL mutation)."""
        mutation = """
        mutation($threadId: ID!) {
          unresolveReviewThread(input: {threadId: $threadId}) {
            thread { id isResolved }
          }
        }
        """
        data = await self._graphql(mutation, {"threadId": thread_node_id})
        return not data["unresolveReviewThread"]["thread"]["isResolved"]

    # ── private helpers ─────────────────────────────────────────────────────
    @staticmethod
    def _parse_comment(raw: dict) -> ReviewComment:
        return ReviewComment(
            id=raw["id"],
            node_id=raw.get("node_id", ""),
            path=raw.get("path", ""),
            line=raw.get("line"),
            original_line=raw.get("original_line"),
            side=raw.get("side", "RIGHT"),
            body=raw.get("body", ""),
            user=raw["user"]["login"],
            state=raw.get("state", ""),
            in_reply_to_id=raw.get("in_reply_to_id"),
            created_at=raw.get("created_at", ""),
            updated_at=raw.get("updated_at", ""),
            diff_hunk=raw.get("diff_hunk", ""),
            html_url=raw.get("html_url", ""),
            pull_request_review_id=raw.get("pull_request_review_id"),
        )
