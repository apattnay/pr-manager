"""Shared fixtures for PR Review MCP tests."""

from __future__ import annotations

import sys
import os
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

# Ensure mcp_server/ is importable
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "mcp_server"))


# ── Raw GitHub API response factories ──────────────────────────────────────


@pytest.fixture()
def raw_pr():
    """Minimal GitHub PR response dict."""
    return {
        "number": 42,
        "title": "Add feature X",
        "state": "open",
        "user": {"login": "author1"},
        "base": {"ref": "main"},
        "head": {"ref": "feature-x"},
        "mergeable": True,
    }


@pytest.fixture()
def raw_review():
    """Minimal GitHub Review response dict."""
    return {
        "id": 1001,
        "node_id": "PRR_kwDOtest",
        "user": {"login": "reviewer1"},
        "state": "CHANGES_REQUESTED",
        "body": "Please fix the import",
        "submitted_at": "2024-01-15T10:00:00Z",
        "html_url": "https://github.com/o/r/pull/42#pullrequestreview-1001",
    }


@pytest.fixture()
def raw_comment():
    """Minimal GitHub review comment response dict."""
    return {
        "id": 2001,
        "node_id": "PRRC_kwDOtest",
        "path": "src/main.py",
        "line": 10,
        "original_line": 10,
        "side": "RIGHT",
        "body": "Consider using `from module import call`",
        "user": {"login": "copilot-pull-request-reviewer"},
        "state": "COMMENTED",
        "in_reply_to_id": None,
        "created_at": "2024-01-15T10:00:00Z",
        "updated_at": "2024-01-15T10:00:00Z",
        "diff_hunk": "@@ -8,3 +8,4 @@\n+import os",
        "html_url": "https://github.com/o/r/pull/42#discussion_r2001",
        "pull_request_review_id": 1001,
    }


@pytest.fixture()
def raw_file():
    """Minimal changed file response dict."""
    return {
        "filename": "src/main.py",
        "status": "modified",
        "changes": 5,
        "patch": "@@ -1,3 +1,5 @@\n-old line\n+new line\n+added line",
    }


@pytest.fixture()
def graphql_threads_response():
    """GraphQL reviewThreads response (1 resolved, 1 unresolved)."""
    return {
        "repository": {
            "pullRequest": {
                "reviewThreads": {
                    "pageInfo": {"hasNextPage": False, "endCursor": None},
                    "nodes": [
                        {
                            "id": "PRT_resolved",
                            "isResolved": True,
                            "path": "src/old.py",
                            "line": 5,
                            "comments": {
                                "nodes": [
                                    {
                                        "id": "PRRC_1",
                                        "databaseId": 3001,
                                        "body": "Looks good",
                                        "author": {"login": "human-dev"},
                                        "createdAt": "2024-01-14T09:00:00Z",
                                        "updatedAt": "2024-01-14T09:00:00Z",
                                        "path": "src/old.py",
                                        "line": 5,
                                        "diffHunk": "@@ hunk",
                                        "url": "https://github.com/o/r/pull/42#r3001",
                                    }
                                ]
                            },
                        },
                        {
                            "id": "PRT_unresolved",
                            "isResolved": False,
                            "path": "src/main.py",
                            "line": 10,
                            "comments": {
                                "nodes": [
                                    {
                                        "id": "PRRC_2",
                                        "databaseId": 3002,
                                        "body": "`call` used but not imported",
                                        "author": {"login": "copilot-pull-request-reviewer"},
                                        "createdAt": "2024-01-15T10:00:00Z",
                                        "updatedAt": "2024-01-15T10:00:00Z",
                                        "path": "src/main.py",
                                        "line": 10,
                                        "diffHunk": "@@ -8,3 +8,4 @@",
                                        "url": "https://github.com/o/r/pull/42#r3002",
                                    }
                                ]
                            },
                        },
                    ],
                }
            }
        }
    }


@pytest.fixture()
def mock_http_client():
    """A mock httpx.AsyncClient that can be preconfigured per test."""
    client = AsyncMock()
    client.is_closed = False
    return client
