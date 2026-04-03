"""Tests for github_client.py — GitHubClient async wrapper."""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from github_client import GitHubClient, ReviewComment, ReviewThread, Review


# ── Helpers ────────────────────────────────────────────────────────────────


def _mock_response(data, status_code=200):
    """Create a mock httpx.Response."""
    resp = MagicMock(spec=httpx.Response)
    resp.status_code = status_code
    resp.json.return_value = data
    resp.raise_for_status = MagicMock()
    if status_code >= 400:
        resp.raise_for_status.side_effect = httpx.HTTPStatusError(
            f"{status_code}", request=MagicMock(), response=resp
        )
    return resp


def _make_client(**overrides):
    """Build a GitHubClient with defaults."""
    defaults = {
        "token": "ghp_test_token",
        "owner": "test-owner",
        "repo": "test-repo",
    }
    defaults.update(overrides)
    return GitHubClient(**defaults)


# ── Construction ───────────────────────────────────────────────────────────


class TestGitHubClientInit:
    def test_default_endpoints(self):
        gh = _make_client()
        assert "api.github.com" in gh._api_base
        assert "api.github.com/graphql" in gh._graphql_url

    def test_custom_endpoints(self):
        gh = _make_client(
            api_base="https://github.intel.com/api/v3",
            graphql_url="https://github.intel.com/api/graphql",
        )
        assert "github.intel.com/api/v3" in gh._api_base
        assert "github.intel.com/api/graphql" in gh._graphql_url

    def test_trailing_slash_stripped(self):
        gh = _make_client(api_base="https://api.github.com/")
        assert not gh._api_base.endswith("/")

    def test_auth_header_set(self):
        gh = _make_client(token="ghp_abc123")
        assert gh._headers["Authorization"] == "token ghp_abc123"


# ── HTTP client lifecycle ──────────────────────────────────────────────────


class TestHttpLifecycle:
    @pytest.mark.asyncio
    async def test_ensure_http_creates_client(self):
        gh = _make_client()
        assert gh._http is None
        client = await gh._ensure_http()
        assert client is not None
        await gh.close()

    @pytest.mark.asyncio
    async def test_close_idempotent(self):
        gh = _make_client()
        await gh._ensure_http()
        await gh.close()
        await gh.close()  # Should not raise
        assert gh._http is None

    @pytest.mark.asyncio
    async def test_reuses_existing_client(self):
        gh = _make_client()
        c1 = await gh._ensure_http()
        c2 = await gh._ensure_http()
        assert c1 is c2
        await gh.close()


# ── URL building ───────────────────────────────────────────────────────────


class TestUrlBuilding:
    def test_url_includes_owner_repo(self):
        gh = _make_client(owner="acme", repo="widgets")
        url = gh._url("pulls/42")
        assert url == "https://api.github.com/repos/acme/widgets/pulls/42"


# ── REST GET with pagination ──────────────────────────────────────────────


class TestGetPrFiles:
    @pytest.mark.asyncio
    async def test_single_page(self, raw_file):
        gh = _make_client()
        gh._http = AsyncMock()
        gh._http.is_closed = False
        gh._http.get = AsyncMock(
            return_value=_mock_response([raw_file])
        )
        files = await gh.get_pr_files(42)
        assert len(files) == 1
        assert files[0]["filename"] == "src/main.py"

    @pytest.mark.asyncio
    async def test_two_pages(self):
        gh = _make_client()
        gh._http = AsyncMock()
        gh._http.is_closed = False
        page1 = [{"filename": f"f{i}.py", "status": "added", "changes": 1} for i in range(100)]
        page2 = [{"filename": "last.py", "status": "added", "changes": 1}]
        gh._http.get = AsyncMock(
            side_effect=[_mock_response(page1), _mock_response(page2)]
        )
        files = await gh.get_pr_files(42)
        assert len(files) == 101

    @pytest.mark.asyncio
    async def test_empty_pr(self):
        gh = _make_client()
        gh._http = AsyncMock()
        gh._http.is_closed = False
        gh._http.get = AsyncMock(return_value=_mock_response([]))
        files = await gh.get_pr_files(42)
        assert files == []


# ── Reviews ────────────────────────────────────────────────────────────────


class TestListReviews:
    @pytest.mark.asyncio
    async def test_parses_reviews(self, raw_review):
        gh = _make_client()
        gh._http = AsyncMock()
        gh._http.is_closed = False
        gh._http.get = AsyncMock(return_value=_mock_response([raw_review]))
        reviews = await gh.list_reviews(42)
        assert len(reviews) == 1
        assert isinstance(reviews[0], Review)
        assert reviews[0].user == "reviewer1"
        assert reviews[0].state == "CHANGES_REQUESTED"


# ── Review comments ────────────────────────────────────────────────────────


class TestListReviewComments:
    @pytest.mark.asyncio
    async def test_parses_comments(self, raw_comment):
        gh = _make_client()
        gh._http = AsyncMock()
        gh._http.is_closed = False
        # First call returns data, second returns empty (pagination stop)
        gh._http.get = AsyncMock(
            side_effect=[_mock_response([raw_comment]), _mock_response([])]
        )
        comments = await gh.list_review_comments(42)
        assert len(comments) == 1
        assert isinstance(comments[0], ReviewComment)
        assert comments[0].path == "src/main.py"
        assert comments[0].user == "copilot-pull-request-reviewer"

    @pytest.mark.asyncio
    async def test_reply_to_comment(self, raw_comment):
        gh = _make_client()
        gh._http = AsyncMock()
        gh._http.is_closed = False
        gh._http.post = AsyncMock(return_value=_mock_response(raw_comment))
        reply = await gh.reply_to_review_comment(42, 2001, "Fixed!")
        assert isinstance(reply, ReviewComment)

    @pytest.mark.asyncio
    async def test_update_comment(self, raw_comment):
        gh = _make_client()
        gh._http = AsyncMock()
        gh._http.is_closed = False
        gh._http.patch = AsyncMock(return_value=_mock_response(raw_comment))
        updated = await gh.update_review_comment(2001, "Updated body")
        assert isinstance(updated, ReviewComment)


# ── GraphQL threads ────────────────────────────────────────────────────────


class TestListReviewThreads:
    @pytest.mark.asyncio
    async def test_parses_threads(self, graphql_threads_response):
        gh = _make_client()
        gh._http = AsyncMock()
        gh._http.is_closed = False
        gh._http.post = AsyncMock(
            return_value=_mock_response({"data": graphql_threads_response})
        )
        threads = await gh.list_review_threads(42)
        assert len(threads) == 2
        assert isinstance(threads[0], ReviewThread)
        assert threads[0].is_resolved is True
        assert threads[1].is_resolved is False
        assert threads[1].comments[0].user == "copilot-pull-request-reviewer"

    @pytest.mark.asyncio
    async def test_graphql_pagination(self):
        """Two pages of threads are stitched together."""
        gh = _make_client()
        gh._http = AsyncMock()
        gh._http.is_closed = False

        page1 = {
            "data": {
                "repository": {
                    "pullRequest": {
                        "reviewThreads": {
                            "pageInfo": {"hasNextPage": True, "endCursor": "cursor1"},
                            "nodes": [
                                {
                                    "id": "PRT_1",
                                    "isResolved": False,
                                    "path": "a.py",
                                    "line": 1,
                                    "comments": {"nodes": []},
                                }
                            ],
                        }
                    }
                }
            }
        }
        page2 = {
            "data": {
                "repository": {
                    "pullRequest": {
                        "reviewThreads": {
                            "pageInfo": {"hasNextPage": False, "endCursor": None},
                            "nodes": [
                                {
                                    "id": "PRT_2",
                                    "isResolved": True,
                                    "path": "b.py",
                                    "line": 2,
                                    "comments": {"nodes": []},
                                }
                            ],
                        }
                    }
                }
            }
        }
        gh._http.post = AsyncMock(
            side_effect=[_mock_response(page1), _mock_response(page2)]
        )
        threads = await gh.list_review_threads(42)
        assert len(threads) == 2
        assert threads[0].thread_id == "PRT_1"
        assert threads[1].thread_id == "PRT_2"


# ── Resolve / unresolve ───────────────────────────────────────────────────


class TestResolveThread:
    @pytest.mark.asyncio
    async def test_resolve_returns_true(self):
        gh = _make_client()
        gh._http = AsyncMock()
        gh._http.is_closed = False
        gh._http.post = AsyncMock(
            return_value=_mock_response(
                {"data": {"resolveReviewThread": {"thread": {"id": "T1", "isResolved": True}}}}
            )
        )
        assert await gh.resolve_thread("T1") is True

    @pytest.mark.asyncio
    async def test_unresolve_returns_true(self):
        gh = _make_client()
        gh._http = AsyncMock()
        gh._http.is_closed = False
        gh._http.post = AsyncMock(
            return_value=_mock_response(
                {"data": {"unresolveReviewThread": {"thread": {"id": "T1", "isResolved": False}}}}
            )
        )
        assert await gh.unresolve_thread("T1") is True


# ── GraphQL error handling ─────────────────────────────────────────────────


class TestGraphQLErrors:
    @pytest.mark.asyncio
    async def test_graphql_errors_raise_runtime(self):
        gh = _make_client()
        gh._http = AsyncMock()
        gh._http.is_closed = False
        gh._http.post = AsyncMock(
            return_value=_mock_response(
                {"errors": [{"message": "Bad query"}]}
            )
        )
        with pytest.raises(RuntimeError, match="GraphQL errors"):
            await gh._graphql("{ bad }")


# ── _parse_comment ─────────────────────────────────────────────────────────


class TestParseComment:
    def test_parses_all_fields(self, raw_comment):
        rc = GitHubClient._parse_comment(raw_comment)
        assert rc.id == 2001
        assert rc.path == "src/main.py"
        assert rc.line == 10
        assert rc.user == "copilot-pull-request-reviewer"
        assert rc.diff_hunk.startswith("@@")

    def test_handles_missing_optional_fields(self):
        minimal = {
            "id": 1,
            "user": {"login": "someone"},
        }
        rc = GitHubClient._parse_comment(minimal)
        assert rc.id == 1
        assert rc.path == ""
        assert rc.line is None
        assert rc.in_reply_to_id is None


# ── HTTP error propagation ─────────────────────────────────────────────────


class TestHttpErrors:
    @pytest.mark.asyncio
    async def test_404_raises(self):
        gh = _make_client()
        gh._http = AsyncMock()
        gh._http.is_closed = False
        gh._http.get = AsyncMock(return_value=_mock_response({}, status_code=404))
        with pytest.raises(httpx.HTTPStatusError):
            await gh.get_pr(9999)

    @pytest.mark.asyncio
    async def test_403_rate_limit_non_ratelimit(self):
        """403 without rate-limit headers is raised immediately (no retry)."""
        gh = _make_client()
        gh._http = AsyncMock()
        gh._http.is_closed = False
        resp = _mock_response({"message": "Forbidden"}, status_code=403)
        resp.status_code = 403
        resp.headers = {"X-RateLimit-Remaining": "10"}  # Not rate-limited
        gh._http.get = AsyncMock(return_value=resp)
        with pytest.raises(httpx.HTTPStatusError):
            await gh.get_pr(42)
