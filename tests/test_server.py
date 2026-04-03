"""Tests for server.py — MCP tool functions and helpers."""

from __future__ import annotations

import json
import os
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from server import parse_pr_url, _comment_to_dict, _thread_to_dict
from github_client import ReviewComment, ReviewThread


# ── parse_pr_url ───────────────────────────────────────────────────────────


class TestParsePrUrl:
    def test_github_com_url(self):
        result = parse_pr_url("https://github.com/owner/repo/pull/123")
        assert result["owner"] == "owner"
        assert result["repo"] == "repo"
        assert result["pr_number"] == 123
        assert result["api_base"] == "https://api.github.com"
        assert result["graphql_url"] == "https://api.github.com/graphql"

    def test_ghes_url(self):
        result = parse_pr_url("https://github.intel.com/org/project/pull/456")
        assert result["owner"] == "org"
        assert result["repo"] == "project"
        assert result["pr_number"] == 456
        assert result["api_base"] == "https://github.intel.com/api/v3"
        assert result["graphql_url"] == "https://github.intel.com/api/graphql"

    def test_plain_number(self):
        result = parse_pr_url("42")
        assert result["pr_number"] == 42
        assert "owner" not in result

    def test_plain_number_with_whitespace(self):
        result = parse_pr_url("  42  ")
        assert result["pr_number"] == 42

    def test_invalid_input_raises(self):
        with pytest.raises(ValueError, match="Cannot parse"):
            parse_pr_url("not-a-url-or-number")

    def test_www_github_com(self):
        result = parse_pr_url("https://www.github.com/a/b/pull/1")
        assert result["api_base"] == "https://api.github.com"


# ── _comment_to_dict ───────────────────────────────────────────────────────


class TestCommentToDict:
    def test_filters_empty_values(self):
        c = ReviewComment(
            id=1, node_id="N1", path="a.py", line=10,
            original_line=10, side="RIGHT", body="Hello",
            user="dev1", state="COMMENTED",
            in_reply_to_id=None,  # Should be filtered
            created_at="",        # Should be filtered
            updated_at="",        # Should be filtered
            diff_hunk="",         # Should be filtered
            html_url="",          # Should be filtered
        )
        d = _comment_to_dict(c)
        assert "in_reply_to_id" not in d
        assert "created_at" not in d
        assert d["id"] == 1
        assert d["body"] == "Hello"


# ── _thread_to_dict ───────────────────────────────────────────────────────


class TestThreadToDict:
    def test_structure(self):
        t = ReviewThread(
            thread_id="PRT_1",
            is_resolved=False,
            path="a.py",
            line=5,
            comments=[
                ReviewComment(
                    id=1, node_id="N1", path="a.py", line=5,
                    original_line=5, side="RIGHT", body="Fix this",
                    user="dev", state="COMMENTED",
                ),
            ],
        )
        d = _thread_to_dict(t)
        assert d["thread_id"] == "PRT_1"
        assert d["is_resolved"] is False
        assert len(d["comments"]) == 1
        assert d["comments"][0]["body"] == "Fix this"


# ── Integration: MCP tool outputs return valid JSON ────────────────────────


class TestToolOutputFormat:
    """Ensure tool functions return parseable JSON strings."""

    @pytest.mark.asyncio
    @patch("server._client")
    async def test_get_pr_overview_returns_json(self, mock_client_fn):
        from server import get_pr_overview

        mock_gh = AsyncMock()
        mock_gh.get_pr.return_value = {
            "number": 42, "title": "Test PR", "state": "open",
            "user": {"login": "dev1"},
            "base": {"ref": "main"}, "head": {"ref": "feature"},
            "mergeable": True,
        }
        mock_gh.list_reviews.return_value = []
        mock_gh.get_pr_files.return_value = []
        mock_client_fn.return_value = mock_gh

        result = await get_pr_overview(42)
        data = json.loads(result)
        assert data["number"] == 42
        assert data["title"] == "Test PR"

    @pytest.mark.asyncio
    @patch("server._client")
    async def test_get_unresolved_summary_returns_json(self, mock_client_fn):
        from server import get_unresolved_comments_summary
        from github_client import ReviewThread, ReviewComment

        mock_gh = AsyncMock()
        thread = ReviewThread(
            thread_id="PRT_1", is_resolved=False,
            path="a.py", line=10,
            comments=[
                ReviewComment(
                    id=1, node_id="N1", path="a.py", line=10,
                    original_line=10, side="RIGHT",
                    body="Fix the import", user="reviewer1",
                    state="COMMENTED", html_url="https://example.com",
                )
            ],
        )
        mock_gh.list_review_threads.return_value = [thread]
        mock_client_fn.return_value = mock_gh

        result = await get_unresolved_comments_summary(42)
        data = json.loads(result)
        assert len(data) == 1
        assert data[0]["file"] == "a.py"
        assert data[0]["reviewer"] == "reviewer1"

    @pytest.mark.asyncio
    @patch("server._client")
    async def test_get_file_diff_found(self, mock_client_fn):
        from server import get_file_diff

        mock_gh = AsyncMock()
        mock_gh.get_pr_files.return_value = [
            {"filename": "src/main.py", "patch": "+new line"},
        ]
        mock_client_fn.return_value = mock_gh

        result = await get_file_diff(42, "src/main.py")
        assert "+new line" in result

    @pytest.mark.asyncio
    @patch("server._client")
    async def test_get_file_diff_not_found(self, mock_client_fn):
        from server import get_file_diff

        mock_gh = AsyncMock()
        mock_gh.get_pr_files.return_value = []
        mock_client_fn.return_value = mock_gh

        result = await get_file_diff(42, "nonexistent.py")
        assert "not found" in result

    @pytest.mark.asyncio
    @patch("server._client")
    async def test_resolve_thread_ok(self, mock_client_fn):
        from server import resolve_thread

        mock_gh = AsyncMock()
        mock_gh.resolve_thread.return_value = True
        mock_client_fn.return_value = mock_gh

        result = await resolve_thread("PRT_1")
        assert "✅" in result

    @pytest.mark.asyncio
    @patch("server._client")
    async def test_resolve_thread_fail(self, mock_client_fn):
        from server import resolve_thread

        mock_gh = AsyncMock()
        mock_gh.resolve_thread.return_value = False
        mock_client_fn.return_value = mock_gh

        result = await resolve_thread("PRT_1")
        assert "⚠️" in result

    @pytest.mark.asyncio
    @patch("server._client")
    async def test_resolve_all_threads(self, mock_client_fn):
        from server import resolve_all_threads
        from github_client import ReviewThread

        mock_gh = AsyncMock()
        mock_gh.list_review_threads.return_value = [
            ReviewThread(thread_id="T1", is_resolved=False, path="a.py", line=1, comments=[]),
            ReviewThread(thread_id="T2", is_resolved=True, path="b.py", line=2, comments=[]),
        ]
        mock_gh.resolve_thread.return_value = True
        mock_client_fn.return_value = mock_gh

        result = await resolve_all_threads(42)
        data = json.loads(result)
        assert data["resolved_count"] == 1
        assert data["total_unresolved"] == 1

    @pytest.mark.asyncio
    @patch("server._client")
    async def test_post_pr_comment(self, mock_client_fn):
        from server import post_pr_comment

        mock_gh = AsyncMock()
        mock_gh.create_issue_comment.return_value = {
            "id": 5001, "html_url": "https://example.com", "body": "Done",
        }
        mock_client_fn.return_value = mock_gh

        result = await post_pr_comment(42, "Done")
        data = json.loads(result)
        assert data["id"] == 5001

    @pytest.mark.asyncio
    @patch("server._client")
    async def test_batch_reply_and_resolve(self, mock_client_fn):
        from server import batch_reply_and_resolve

        mock_gh = AsyncMock()
        mock_gh.reply_to_review_comment.return_value = ReviewComment(
            id=1, node_id="N", path="", line=None, original_line=None,
            side="RIGHT", body="ok", user="me", state="COMMENTED",
        )
        mock_gh.resolve_thread.return_value = True
        mock_client_fn.return_value = mock_gh

        items = json.dumps([
            {"comment_id": 1, "thread_id": "T1", "reply": "Fixed"},
        ])
        result = await batch_reply_and_resolve(42, items)
        data = json.loads(result)
        assert len(data) == 1
        assert data[0]["reply_status"] == "ok"
        assert data[0]["resolve_status"] == "ok"
