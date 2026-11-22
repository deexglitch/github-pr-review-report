""" transforms the raw github data into 1 verdict per pr

layout:
constants
pure functions -> only looks at inputs and returns a result, can unit test this
"""
from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any

import pandas as pd

from utils import load_json, save_json

logger = logging.getLogger(__name__)

#constants
#gh has APPROVED, CHANGES_REQUESTED, COMMENTED, DISMISSED, PENDING
#but commented is just a comment, doesnt do anything
#pending is an unsubmitted draft
VERDICT_STATES = {"APPROVED", "CHANGES_REQUESTED", "DISMISSED"}

#success = pass, neutral = nothing to fail, skipped = failed/cancelled/action_required etc
PASSING_CONCLUSIONS = {"success", "neutral", "skipped"}

#big business rules for code rev
def evaluate_reviews(reviews: list[dict[str, Any]], author: str | None) -> tuple[bool, list[str]]:
    """Return (CR_Passed, approvers): passed if at least one reviewer's latest verdict is APPROVED.

    - The latest verdict per reviewer wins ("approved, then changes requested" is not a pass).
    - The author's own approval and reviews from deleted users are ignored.
    """
    latest: dict[str, str] = {}  # reviewer login -> most recent verdict

    # Oldest first, so later reviews overwrite earlier ones. submitted_at is an ISO-8601 string,
    # which sorts chronologically; `or ""` covers drafts that have no timestamp.
    for review in sorted(reviews, key=lambda r: r.get("submitted_at") or ""):
        login = (review.get("user") or {}).get("login")  # user is null for deleted accounts
        state = (review.get("state") or "").upper()
        if not login or login == author or state not in VERDICT_STATES:
            continue
        latest[login] = state

    approvers = sorted(user for user, state in latest.items() if state == "APPROVED")
    return bool(approvers), approvers


def _check_outcomes(check_runs: list[dict[str, Any]], commit_status: dict[str, Any] | None) -> dict[str, str]:
    """Merge check runs and legacy commit statuses into {name: 'pass' | 'fail' | 'pending'}."""
    outcomes: dict[str, str] = {}

    # Sorted by id so that for re-runs of the same check the latest run wins.
    for run in sorted(check_runs, key=lambda r: r.get("id", 0)):
        if run.get("status") != "completed":
            outcomes[run["name"]] = "pending"
        elif run.get("conclusion") in PASSING_CONCLUSIONS:
            outcomes[run["name"]] = "pass"
        else:
            outcomes[run["name"]] = "fail"

    for status in (commit_status or {}).get("statuses", []):
        # success -> pass, pending -> pending, anything else (failure, error, ...) -> fail
        outcomes[status["context"]] = {"success": "pass", "pending": "pending"}.get(status.get("state"), "fail")

    return outcomes


def evaluate_checks(
    check_runs: list[dict[str, Any]],
    commit_status: dict[str, Any] | None,
    required: dict[str, Any] | None,
) -> tuple[bool | None, dict[str, Any]]:
    """Return (CHECKS_PASSED, details). CHECKS_PASSED is True, False, or None (cannot tell).

    - Required checks known: each one must be present and passing. A missing one counts as failed.
    - Required checks unknown: fall back to "every check on the commit passed". With no checks
      at all the result is None, because no evidence is not the same as passed.
    """
    outcomes = _check_outcomes(check_runs, commit_status)
    contexts = (required or {}).get("contexts")  # None = unknown

    # Saved in the transform JSON so each verdict can be audited.
    details: dict[str, Any] = {
        "checks_source": (required or {}).get("source", "unavailable"),
        "failing_checks": [],
        "missing_required_checks": [],
    }

    #when we know the required list
    if contexts is not None:
        details["missing_required_checks"] = sorted(c for c in contexts if c not in outcomes)
        details["failing_checks"] = sorted(c for c in contexts if outcomes.get(c) in ("fail", "pending"))
        passed = not details["missing_required_checks"] and not details["failing_checks"]
        return passed, details

    #fallback, we dont know 
    details["checks_source"] = "all_checks_fallback"
    if not outcomes:
        return None, details #no checks existed
    details["failing_checks"] = sorted(name for name, outcome in outcomes.items() if outcome != "pass")
    return not details["failing_checks"], details


#base class for shared workflow
class BaseTransformer(ABC):
    """Shared transform workflow. A subclass sets the column lists and implements transform_item."""

    #attrs a subclass can override for its own report
    REPORT_COLUMNS: list[str] = [] #cols in what order 
    BOOLEAN_COLUMNS: list[str] = [] #cols for bool values, unknown is allowed
    DATE_COLUMNS: list[str] = [] #cols for timestamps

    def __init__(self, output_dir: Path) -> None:
        self.output_dir = Path(output_dir)

    @property
    @abstractmethod
    def output_filename(self) -> str:
        ... #placeholder body

    @abstractmethod
    def transform_item(self, raw: dict[str, Any]) -> dict[str, Any]:
        """Turn one raw record into a flat dict containing at least REPORT_COLUMNS."""

    def transform(self, raw_items: list[dict[str, Any]]) -> list[dict[str, Any]]:
        #do the transform_item for every record
        return [self.transform_item(raw) for raw in raw_items]

    def run(self, input_path: Path) -> list[dict[str, Any]]:
        """Load raw JSON, transform it, save the result as JSON and return the records."""
        records = self.transform(load_json(input_path))
        path = save_json(records, self.output_dir / self.output_filename)
        logger.info("Saved %d analysed records -> %s", len(records), path)
        return records

    def to_report_frame(self, records: list[dict[str, Any]]) -> pd.DataFrame:
        """Build the report table. `columns=` keeps only the report columns, in order."""
        df = pd.DataFrame(records, columns=self.REPORT_COLUMNS)
        for col in self.BOOLEAN_COLUMNS:
            # Nullable boolean, so an unknown result (None) stays empty instead of becoming True/False.
            df[col] = df[col].astype("boolean")
        for col in self.DATE_COLUMNS:
            df[col] = pd.to_datetime(df[col], utc=True)
        return df


#github implementation
class GitHubTransformer(BaseTransformer):
    # Exactly the columns the assignment asks for, in the order it lists them.
    REPORT_COLUMNS = ["PR_NUMBER", "PR_TITLE", "AUTHOR", "MERGE_DATE", "CR_Passed", "CHECKS_PASSED"]
    BOOLEAN_COLUMNS = ["CR_Passed", "CHECKS_PASSED"]
    DATE_COLUMNS = ["MERGE_DATE"]

    def __init__(self, repo: str, output_dir: Path = Path("data/transform")) -> None:
        super().__init__(output_dir)
        self.repo = repo

    @property
    def output_filename(self) -> str:
        return f"{self.repo.replace('/', '__')}_pr_analysis.json"

    def transform_item(self, raw: dict[str, Any]) -> dict[str, Any]:
        """Apply the business rules to one raw record and return one flat row."""
        pr = raw["pull_request"]
        # Author login. `user` is null when the account was deleted -> AUTHOR stays None (empty in CSV).
        author = (pr.get("user") or {}).get("login")

        # `.get(..., [])` / `.get(...)` keep us safe if a key is missing from an older extract file.
        cr_passed, approvers = evaluate_reviews(raw.get("reviews", []), author)
        checks_passed, details = evaluate_checks(
            raw.get("check_runs", []), raw.get("commit_status"), raw.get("required_checks")
        )
        return {
            # ---- the six columns the assignment requires ----
            "PR_NUMBER": pr["number"],
            "PR_TITLE": pr.get("title"),
            "AUTHOR": author,
            "MERGE_DATE": pr.get("merged_at"),
            "CR_Passed": cr_passed,
            "CHECKS_PASSED": checks_passed,
            # ---- extra audit info: saved in the transform JSON, NOT in the CSV report ----
            "approved_by": approvers,
            **details,  # unpacks checks_source / failing_checks / missing_required_checks into this dict
        }