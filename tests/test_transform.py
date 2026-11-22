"""Unit tests for the business rules in transform.py.

WHY test these: the rules (evaluate_reviews / evaluate_checks) are where a mistake would make the
report WRONG, and they are pure functions, so they run instantly with no network or files.
Each test covers ONE rule or edge case; read the names as a list of what the code guarantees.

Run with:  python -m pytest
"""
from transform import GitHubTransformer, evaluate_checks, evaluate_reviews


# --------------------------------------------------------------------------- #
# Helpers: build fake API objects so each test stays short and readable
# --------------------------------------------------------------------------- #
def review(user, state, ts):
    """A fake review. user=None simulates a deleted account (GitHub returns `user: null`)."""
    return {"user": {"login": user} if user else None, "state": state, "submitted_at": ts}


def run(name, conclusion="success", status="completed", id_=1):
    """A fake check run. A higher id_ means a later run (used to test re-runs)."""
    return {"id": id_, "name": name, "status": status, "conclusion": conclusion}


# --------------------------------------------------------------------------- #
# Code review rules.  Convention in these tests: PR author = "alice", reviewer = "bob".
# --------------------------------------------------------------------------- #
def test_simple_approval():
    # Happy path: one independent approval -> passed, and bob is listed as approver.
    assert evaluate_reviews([review("bob", "APPROVED", "2025-01-01")], "alice") == (True, ["bob"])


def test_no_reviews():
    # A PR merged with no reviews at all must NOT pass.
    assert evaluate_reviews([], "alice") == (False, [])


def test_changes_requested_after_approval_cancels_it():
    # The reviewer's LATEST verdict wins: approve (Jan 1) then request changes (Jan 2) = not passed.
    reviews = [review("bob", "APPROVED", "2025-01-01"), review("bob", "CHANGES_REQUESTED", "2025-01-02")]
    assert evaluate_reviews(reviews, "alice")[0] is False


def test_comment_after_approval_keeps_approval():
    # A plain comment is not a verdict, so it must not erase an earlier approval.
    reviews = [review("bob", "APPROVED", "2025-01-01"), review("bob", "COMMENTED", "2025-01-02")]
    assert evaluate_reviews(reviews, "alice")[0] is True


def test_dismissed_approval_does_not_count():
    # A dismissed review is withdrawn, so it can't make the PR "approved".
    assert evaluate_reviews([review("bob", "DISMISSED", "2025-01-01")], "alice")[0] is False


def test_self_approval_and_deleted_user_ignored():
    # The author approving their own PR doesn't count, and neither does a deleted user's review.
    reviews = [review("alice", "APPROVED", "2025-01-01"), review(None, "APPROVED", "2025-01-02")]
    assert evaluate_reviews(reviews, "alice")[0] is False


def test_out_of_order_input_is_sorted_by_time():
    # The input list order must not matter: we sort by submitted_at, so the Jan 2 "changes
    # requested" is still the latest even though it appears first in the list.
    reviews = [review("bob", "CHANGES_REQUESTED", "2025-01-02"), review("bob", "APPROVED", "2025-01-01")]
    assert evaluate_reviews(reviews, "alice")[0] is False


# --------------------------------------------------------------------------- #
# Status check rules
# --------------------------------------------------------------------------- #
def test_required_all_pass():
    # Only "ci" is required; "lint" is extra. ci passed -> True, and the source is recorded.
    passed, d = evaluate_checks([run("ci"), run("lint")], None, {"source": "branch_protection", "contexts": ["ci"]})
    assert passed is True and d["checks_source"] == "branch_protection"


def test_required_missing_fails():
    # "ci" is required but never reported -> counts as failed, and is listed as missing.
    passed, d = evaluate_checks([run("lint")], None, {"source": "branch_protection", "contexts": ["ci"]})
    assert passed is False and d["missing_required_checks"] == ["ci"]


def test_required_failing_or_pending_fails():
    req = {"source": "repository_rules", "contexts": ["ci"]}
    # A failed required check -> False.
    assert evaluate_checks([run("ci", "failure")], None, req)[0] is False
    # A required check still running (no conclusion yet) -> not passed.
    assert evaluate_checks([run("ci", None, "in_progress")], None, req)[0] is False


def test_skipped_and_neutral_count_as_pass():
    # contexts=None -> fallback mode (all checks count). skipped/neutral are not failures.
    runs = [run("a", "skipped"), run("b", "neutral", id_=2)]
    assert evaluate_checks(runs, None, {"contexts": None})[0] is True


def test_fallback_without_required_list():
    # Required list unknown -> every check counts; "lint" failed so the PR fails,
    # and the fallback is reported so the verdict can be audited.
    runs = [run("ci"), run("lint", "failure", id_=2)]
    passed, d = evaluate_checks(runs, None, {"source": "unavailable", "contexts": None})
    assert passed is False
    assert d["checks_source"] == "all_checks_fallback"
    assert d["failing_checks"] == ["lint"]


def test_no_checks_at_all_is_unknown():
    # No rules readable AND no checks: we return None ("unknown"), never a guessed True.
    assert evaluate_checks([], None, {"source": "unavailable", "contexts": None})[0] is None


def test_rerun_latest_wins():
    # "ci" failed first (id 1) then passed on re-run (id 2) -> the latest run decides.
    runs = [run("ci", "failure", id_=1), run("ci", "success", id_=2)]
    assert evaluate_checks(runs, None, {"contexts": ["ci"]})[0] is True


def test_legacy_commit_status_counts():
    # Checks reported through the old Status API (not check runs) must be honoured too.
    status = {"statuses": [{"context": "ci/legacy", "state": "failure"}]}
    assert evaluate_checks([], status, {"contexts": ["ci/legacy"]})[0] is False


def test_empty_required_list_passes():
    # Branch rules exist but require no checks -> nothing can have failed -> True.
    assert evaluate_checks([], None, {"source": "repository_rules", "contexts": []})[0] is True


# --------------------------------------------------------------------------- #
# One full record through GitHubTransformer (reviews + checks + author handling together)
# --------------------------------------------------------------------------- #
def test_transform_item_handles_deleted_author():
    raw = {
        # "user": None = deleted author -> AUTHOR must be None, and nothing may crash.
        "pull_request": {"number": 7, "title": "Fix", "user": None, "merged_at": "2025-01-03T00:00:00Z"},
        "reviews": [review("bob", "APPROVED", "2025-01-02")],
        "check_runs": [run("ci")],
        "commit_status": None,
        "required_checks": {"source": "branch_protection", "contexts": ["ci"]},
    }
    row = GitHubTransformer("home-assistant/core").transform_item(raw)
    assert row["AUTHOR"] is None
    assert row["CR_Passed"] is True
    assert row["CHECKS_PASSED"] is True
