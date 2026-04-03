"""Tests for evaluator.py — heuristic triage engine."""

from __future__ import annotations

import pytest

from github_client import ReviewComment, ReviewThread
from evaluator import (
    Verdict,
    Evaluation,
    evaluate_threads,
    _evaluate_single,
    _is_bot_reviewer,
    _is_duplicate_of,
    _match_patterns,
    _VALID_PATTERNS,
    _FALSE_POSITIVE_PATTERNS,
)


# ── Helpers ────────────────────────────────────────────────────────────────


def _make_thread(
    thread_id: str = "PRT_1",
    path: str = "src/main.py",
    line: int | None = 10,
    is_resolved: bool = False,
    user: str = "copilot-pull-request-reviewer",
    body: str = "Some review comment",
) -> ReviewThread:
    """Build a ReviewThread with a single comment for testing."""
    comment = ReviewComment(
        id=1,
        node_id="PRRC_1",
        path=path,
        line=line,
        original_line=line,
        side="RIGHT",
        body=body,
        user=user,
        state="COMMENTED",
    )
    return ReviewThread(
        thread_id=thread_id,
        is_resolved=is_resolved,
        path=path,
        line=line,
        comments=[comment],
    )


# ── Bot detection ──────────────────────────────────────────────────────────


class TestBotDetection:
    def test_copilot_reviewer_is_bot(self):
        assert _is_bot_reviewer("copilot-pull-request-reviewer") is True

    def test_github_actions_is_bot(self):
        assert _is_bot_reviewer("github-actions[bot]") is True

    def test_dependabot_is_bot(self):
        assert _is_bot_reviewer("dependabot[bot]") is True

    def test_generic_bot_suffix(self):
        assert _is_bot_reviewer("my-custom-thing[bot]") is True

    def test_human_is_not_bot(self):
        assert _is_bot_reviewer("john-doe") is False

    def test_case_insensitive(self):
        assert _is_bot_reviewer("Copilot-Pull-Request-Reviewer") is True


# ── Duplicate detection ───────────────────────────────────────────────────


class TestDuplicateDetection:
    def test_same_file_same_body_is_duplicate(self):
        t1 = _make_thread(thread_id="PRT_1", body="Fix this")
        t2 = _make_thread(thread_id="PRT_2", body="Fix this")
        assert _is_duplicate_of(t2, [t1, t2]) is True

    def test_same_file_different_body_not_duplicate(self):
        t1 = _make_thread(thread_id="PRT_1", body="Fix this")
        t2 = _make_thread(thread_id="PRT_2", body="Different issue")
        assert _is_duplicate_of(t2, [t1, t2]) is False

    def test_different_file_same_body_not_duplicate(self):
        t1 = _make_thread(thread_id="PRT_1", path="a.py", body="Fix this")
        t2 = _make_thread(thread_id="PRT_2", path="b.py", body="Fix this")
        assert _is_duplicate_of(t2, [t1, t2]) is False

    def test_empty_comments_not_duplicate(self):
        t = ReviewThread(thread_id="PRT_1", is_resolved=False, path="a.py", line=1, comments=[])
        assert _is_duplicate_of(t, [t]) is False


# ── Pattern matching ──────────────────────────────────────────────────────


class TestPatternMatching:
    @pytest.mark.parametrize(
        "body,expected_verdict",
        [
            ("This could cause a ZeroDivisionError", Verdict.VALID),
            ("Potential TypeError when None is passed", Verdict.VALID),
            ("This is a security concern with token exposed", Verdict.VALID),
            ("Possible infinite loop in the scheduler", Verdict.VALID),
            ("May break queries and corrupt results", Verdict.VALID),
        ],
    )
    def test_valid_patterns(self, body, expected_verdict):
        match = _match_patterns(body, _VALID_PATTERNS)
        assert match is not None
        verdict, reason = match
        assert verdict == expected_verdict

    @pytest.mark.parametrize(
        "body,expected_verdict",
        [
            ("not listed in supported_keys mapping", Verdict.DISMISS),
            ("Consider using a list comprehension", Verdict.OPTIONAL),
            ("You might want to add error handling", Verdict.OPTIONAL),
        ],
    )
    def test_false_positive_patterns(self, body, expected_verdict):
        match = _match_patterns(body, _FALSE_POSITIVE_PATTERNS)
        assert match is not None
        verdict, _ = match
        assert verdict == expected_verdict

    def test_no_pattern_match_returns_none(self):
        assert _match_patterns("Just a plain comment", _VALID_PATTERNS) is None


# ── Single thread evaluation ──────────────────────────────────────────────


class TestEvaluateSingle:
    def test_duplicate_returns_dismiss(self):
        t1 = _make_thread(thread_id="PRT_1", body="Same comment")
        t2 = _make_thread(thread_id="PRT_2", body="Same comment")
        ev = _evaluate_single(t2, [t1, t2], None)
        assert ev.verdict == Verdict.DISMISS
        assert "Duplicate" in ev.reasoning

    def test_real_bug_from_bot_returns_valid_medium_confidence(self):
        t = _make_thread(
            user="copilot-pull-request-reviewer",
            body="This will cause a ZeroDivisionError on line 42",
        )
        ev = _evaluate_single(t, [t], None)
        assert ev.verdict == Verdict.VALID
        assert ev.confidence == "MEDIUM"  # Bot reviewer → medium

    def test_real_bug_from_human_returns_valid_high_confidence(self):
        t = _make_thread(
            user="senior-dev",
            body="This will cause a ZeroDivisionError on line 42",
        )
        ev = _evaluate_single(t, [t], None)
        assert ev.verdict == Verdict.VALID
        assert ev.confidence == "HIGH"

    def test_bot_false_positive_returns_dismiss(self):
        t = _make_thread(
            user="copilot-pull-request-reviewer",
            body="key 'foo' not listed in supported_keys",
        )
        ev = _evaluate_single(t, [t], None)
        assert ev.verdict == Verdict.DISMISS

    def test_bot_code_suggestion_returns_optional(self):
        t = _make_thread(
            user="copilot-pull-request-reviewer",
            body="Here's a better approach:\n```suggestion\nnew_code()\n```",
        )
        ev = _evaluate_single(t, [t], None)
        assert ev.verdict == Verdict.OPTIONAL

    def test_human_reviewer_defaults_to_valid(self):
        t = _make_thread(
            user="team-lead",
            body="Please rename this variable for clarity",
        )
        ev = _evaluate_single(t, [t], None)
        assert ev.verdict == Verdict.VALID
        assert "Human reviewer" in ev.reasoning

    def test_bot_no_pattern_match_returns_optional(self):
        t = _make_thread(
            user="copilot-pull-request-reviewer",
            body="This is a generic observation about the code.",
        )
        ev = _evaluate_single(t, [t], None)
        assert ev.verdict == Verdict.OPTIONAL
        assert ev.confidence == "LOW"

    def test_evaluation_to_dict(self):
        t = _make_thread(user="human", body="Fix the bug")
        ev = _evaluate_single(t, [t], None)
        d = ev.to_dict()
        assert "thread_id" in d
        assert "verdict" in d
        assert d["verdict"] in ("VALID", "DISMISS", "OPTIONAL")

    def test_comment_preview_truncated(self):
        long_body = "x" * 200
        t = _make_thread(user="bot[bot]", body=long_body)
        ev = _evaluate_single(t, [t], None)
        assert len(ev.comment_preview) <= 123  # 120 + "..."


# ── Priority ordering ─────────────────────────────────────────────────────


class TestRulePriority:
    def test_valid_pattern_beats_false_positive(self):
        """A comment matching both VALID and FALSE_POSITIVE patterns
        should be classified VALID (Rule 2 before Rule 3)."""
        t = _make_thread(
            user="copilot-pull-request-reviewer",
            body="ZeroDivisionError not listed in supported_keys",
        )
        ev = _evaluate_single(t, [t], None)
        assert ev.verdict == Verdict.VALID

    def test_duplicate_beats_everything(self):
        """Duplicate rule (Rule 1) fires before any pattern matching."""
        t1 = _make_thread(
            thread_id="PRT_1",
            user="copilot-pull-request-reviewer",
            body="ZeroDivisionError here",
        )
        t2 = _make_thread(
            thread_id="PRT_2",
            user="copilot-pull-request-reviewer",
            body="ZeroDivisionError here",
        )
        ev = _evaluate_single(t2, [t1, t2], None)
        assert ev.verdict == Verdict.DISMISS
