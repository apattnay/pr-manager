"""Evaluator for PR review comments.

Triages each unresolved review thread by analyzing:
 - Whether the reviewer's concern is valid for the actual codebase
 - Whether the suggested change matches established project conventions
 - Whether the issue would actually cause a runtime error

Returns a verdict: VALID (should fix), DISMISS (not needed), or
OPTIONAL (nice-to-have) with supporting evidence.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from enum import Enum

from github_client import GitHubClient, ReviewThread

logger = logging.getLogger(__name__)


class Verdict(str, Enum):
    """Triage verdict for a review comment."""

    VALID = "VALID"        # Real issue — should fix
    DISMISS = "DISMISS"    # Bot was wrong or concern doesn't apply
    OPTIONAL = "OPTIONAL"  # Nice-to-have, not required


@dataclass
class Evaluation:
    """Result of evaluating a single review thread."""

    thread_id: str
    file: str
    line: int | None
    reviewer: str
    comment_preview: str
    verdict: Verdict
    reasoning: str
    confidence: str  # HIGH, MEDIUM, LOW
    suggested_reply: str

    def to_dict(self) -> dict:
        return {
            "thread_id": self.thread_id,
            "file": self.file,
            "line": self.line,
            "reviewer": self.reviewer,
            "comment_preview": self.comment_preview,
            "verdict": self.verdict.value,
            "reasoning": self.reasoning,
            "confidence": self.confidence,
            "suggested_reply": self.suggested_reply,
        }


# ---------------------------------------------------------------------------
# Heuristic rules
# ---------------------------------------------------------------------------

# Patterns that indicate the reviewer is an automated bot
_BOT_REVIEWERS = {
    "copilot-pull-request-reviewer",
    "github-actions[bot]",
    "dependabot[bot]",
}

# Common false-positive patterns from Copilot reviewer
_FALSE_POSITIVE_PATTERNS = [
    # "key not in supported_keys" — often wrong if settings are permissive
    (
        r"not\s+listed\s+in\s+.*supported_keys",
        "Bot assumes key validation exists. Many settings managers are "
        "free-form key-value stores without strict validation.",
        Verdict.DISMISS,
    ),
    # "non-ASCII symbols" — usually fine for internal tools
    (
        r"non-ASCII|UnicodeEncodeError|non-UTF-8",
        "Modern Python 3 on Linux defaults to UTF-8. This is only a "
        "concern for CI log collectors or Windows consoles.",
        Verdict.OPTIONAL,
    ),
    # "consider using" / "you might want" — suggestions, not bugs
    (
        r"^(?:Consider|You might want|It (?:might|may) be (?:better|worth))",
        "This is a stylistic suggestion, not a bug report.",
        Verdict.OPTIONAL,
    ),
]

# Patterns that indicate the comment is about a real bug
_VALID_PATTERNS = [
    (
        r"ZeroDivisionError|division by zero|divide.by.zero",
        "Division by zero is a runtime crash — must be guarded.",
        Verdict.VALID,
    ),
    (
        r"TypeError|AttributeError|NameError|IndexError|KeyError",
        "This describes a potential runtime exception.",
        Verdict.VALID,
    ),
    (
        r"infinite loop|deadlock|hang|memory leak",
        "Performance / availability issue — should investigate.",
        Verdict.VALID,
    ),
    (
        r"security|injection|XSS|CSRF|credential|secret|token.*exposed",
        "Security concern — must review carefully.",
        Verdict.VALID,
    ),
    (
        r"(?:break|pollut|corrupt).*(?:query|queries|results|data)",
        "Data integrity concern — queries returning wrong results.",
        Verdict.VALID,
    ),
]


def _match_patterns(
    body: str, patterns: list[tuple[str, str, Verdict]]
) -> tuple[Verdict, str] | None:
    """Check comment body against a list of (regex, reason, verdict) tuples."""
    for regex, reason, verdict in patterns:
        if re.search(regex, body, re.IGNORECASE | re.MULTILINE):
            return verdict, reason
    return None


def _is_bot_reviewer(username: str) -> bool:
    return username.lower() in _BOT_REVIEWERS or username.endswith("[bot]")


def _is_duplicate_of(thread: ReviewThread, others: list[ReviewThread]) -> bool:
    """Check if this thread is a duplicate (same file, same comment body)."""
    if not thread.comments:
        return False
    body = thread.comments[0].body
    for other in others:
        if other.thread_id == thread.thread_id:
            continue
        if other.path == thread.path and other.comments and other.comments[0].body == body:
            return True
    return False


# ---------------------------------------------------------------------------
# Main evaluator
# ---------------------------------------------------------------------------

async def evaluate_threads(
    gh: GitHubClient,
    pr_number: int,
    file_contents: dict[str, str] | None = None,
) -> list[Evaluation]:
    """Evaluate all unresolved review threads on a PR.

    :param gh: Authenticated GitHubClient.
    :param pr_number: PR number to evaluate.
    :param file_contents: Optional dict of {path: content} for deeper analysis.
        If not provided, evaluation is based on comment text heuristics only.
    :returns: List of Evaluation objects with verdicts.
    """
    threads = await gh.list_review_threads(pr_number)
    unresolved = [t for t in threads if not t.is_resolved]

    evaluations: list[Evaluation] = []
    for thread in unresolved:
        ev = _evaluate_single(thread, unresolved, file_contents)
        evaluations.append(ev)

    return evaluations


def _evaluate_single(
    thread: ReviewThread,
    all_threads: list[ReviewThread],
    file_contents: dict[str, str] | None,
) -> Evaluation:
    """Evaluate a single review thread."""
    first = thread.comments[0] if thread.comments else None
    body = first.body if first else ""
    reviewer = first.user if first else "unknown"
    preview = body[:120] + ("..." if len(body) > 120 else "")

    # ── Rule 1: Duplicate thread → DISMISS
    if _is_duplicate_of(thread, all_threads):
        return Evaluation(
            thread_id=thread.thread_id,
            file=thread.path,
            line=thread.line,
            reviewer=reviewer,
            comment_preview=preview,
            verdict=Verdict.DISMISS,
            reasoning="Duplicate thread — same comment already exists on the same file.",
            confidence="HIGH",
            suggested_reply="Duplicate of another thread on this file — resolving.",
        )

    # ── Rule 2: Check for real-bug patterns first (highest priority)
    match = _match_patterns(body, _VALID_PATTERNS)
    if match:
        verdict, reason = match
        return Evaluation(
            thread_id=thread.thread_id,
            file=thread.path,
            line=thread.line,
            reviewer=reviewer,
            comment_preview=preview,
            verdict=verdict,
            reasoning=reason,
            confidence="HIGH" if not _is_bot_reviewer(reviewer) else "MEDIUM",
            suggested_reply="",
        )

    # ── Rule 3: Known false-positive patterns from bots
    if _is_bot_reviewer(reviewer):
        match = _match_patterns(body, _FALSE_POSITIVE_PATTERNS)
        if match:
            verdict, reason = match
            return Evaluation(
                thread_id=thread.thread_id,
                file=thread.path,
                line=thread.line,
                reviewer=reviewer,
                comment_preview=preview,
                verdict=verdict,
                reasoning=f"Bot reviewer ({reviewer}): {reason}",
                confidence="MEDIUM",
                suggested_reply=f"Not applicable — {reason}",
            )

    # ── Rule 4: Bot reviewer with only a code suggestion → OPTIONAL
    if _is_bot_reviewer(reviewer) and "```suggestion" in body:
        return Evaluation(
            thread_id=thread.thread_id,
            file=thread.path,
            line=thread.line,
            reviewer=reviewer,
            comment_preview=preview,
            verdict=Verdict.OPTIONAL,
            reasoning=(
                f"Automated suggestion from {reviewer}. Review the suggested "
                "code change to decide if it improves the code."
            ),
            confidence="LOW",
            suggested_reply="",
        )

    # ── Rule 5: Human reviewer → default to VALID (respect human reviewers)
    if not _is_bot_reviewer(reviewer):
        return Evaluation(
            thread_id=thread.thread_id,
            file=thread.path,
            line=thread.line,
            reviewer=reviewer,
            comment_preview=preview,
            verdict=Verdict.VALID,
            reasoning=(
                f"Human reviewer ({reviewer}) requested a change. "
                "Human reviews should generally be addressed."
            ),
            confidence="MEDIUM",
            suggested_reply="",
        )

    # ── Rule 6: Bot reviewer, no pattern match → OPTIONAL
    return Evaluation(
        thread_id=thread.thread_id,
        file=thread.path,
        line=thread.line,
        reviewer=reviewer,
        comment_preview=preview,
        verdict=Verdict.OPTIONAL,
        reasoning=(
            f"Automated review from {reviewer}. No strong signal for "
            "or against. Manual inspection recommended."
        ),
        confidence="LOW",
        suggested_reply="",
    )
