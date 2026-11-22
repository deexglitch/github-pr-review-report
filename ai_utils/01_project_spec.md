# Project spec (prompt used to drive the AI assistant)

## Goal
Build a small Python ETL that reports, for every merged pull request in one GitHub repository
under the `home-assistant` organisation, whether it **passed code review** and whether
**all required status checks passed** before merging.

## Requirements
- Authenticate with a GitHub personal access token (PAT) read from `GITHUB_TOKEN` (`.env`).
- Fetch merged PRs from a single repository (`--repo owner/name`, `--max-prs N`).
- Output a CSV or Parquet report with one row per PR:
  `PR_NUMBER, PR_TITLE, AUTHOR, MERGE_DATE, CR_Passed, CHECKS_PASSED`.
- Save intermediate output of each step as JSON (`data/extract`, `data/transform`, `data/report`).

## Required structure
```
ai_utils/     markdown resources for AI tools (this folder)
extract.py    retrieves raw PR data from the API
transform.py  computes CR_Passed / CHECKS_PASSED
main.py       orchestrates extract -> transform -> report
utils.py      shared helpers (HTTP client, logging, JSON I/O, report writer)
```

## Design rules
- Integration-specific logic lives in subclasses of `BaseExtractor` / `BaseTransformer`;
  the shared workflow (save JSON, load JSON, build the report frame) lives in the base classes.
- Business rules are pure functions (`evaluate_reviews`, `evaluate_checks`) so they are unit-testable.
- The HTTP client handles pagination (Link header), retries on 5xx/network errors and rate limits.
- Never hard-code or log the token.

## Business rules
**CR_Passed**: at least one reviewer's *latest* verdict is `APPROVED`.
Only `APPROVED`, `CHANGES_REQUESTED`, `DISMISSED` change a verdict; `COMMENTED` and `PENDING` are ignored.
Approvals by the PR author and reviews from deleted users are ignored.

**CHECKS_PASSED**: every *required* check on the PR's head commit exists and passed
(`success`, `neutral`, `skipped` count as pass; pending counts as not passed).
Required checks come from branch protection, then repository rulesets. If neither is readable,
fall back to "all checks on the commit passed"; with no checks at all the value is empty (unknown).

## Edge cases to handle
Null author (deleted user), no reviews, no checks, re-run checks (latest wins), legacy commit
statuses, closed-but-unmerged PRs, unreachable head commits, rate limits, pagination.
