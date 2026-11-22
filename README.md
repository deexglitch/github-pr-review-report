# GitHub PR Review Report

A small ETL pipeline that checks whether merged pull requests in a `home-assistant` repository
went through code review and passed their required status checks before merging.

**Pipeline:** `extract.py` (raw JSON from GitHub) → `transform.py` (review/check logic) → `main.py` (report).

## Output

One row per merged PR in `data/report/<owner>__<repo>_pr_review_report.csv` (or `.parquet`):

| Column | Meaning |
|---|---|
| `PR_NUMBER` | Pull request number |
| `PR_TITLE` | Pull request title |
| `AUTHOR` | GitHub login of the author (empty if the account was deleted) |
| `MERGE_DATE` | Merge timestamp (UTC) |
| `CR_Passed` | At least one reviewer approved |
| `CHECKS_PASSED` | All required status checks passed before merge (empty = could not be determined) |

Each step also saves JSON: `data/extract/` (raw API data), `data/transform/` (per-PR verdicts, including
who approved, which checks failed/were missing, and how "required" was determined).

A 20-row example of the report is in `samples/sample_report.csv`.

## Setup

```powershell
# 1. Create and activate a virtual environment
python -m venv .venv
.venv\Scripts\Activate.ps1          # macOS/Linux: source .venv/bin/activate

# 2. Install dependencies
python -m pip install -r requirements.txt

# 3. Add your GitHub personal access token
Copy-Item .env.example .env         # macOS/Linux: cp .env.example .env
# then edit .env and set GITHUB_TOKEN=...
```

A fine-grained token with read-only access to public repositories is enough.
`.env` and `data/` must be listed in `.gitignore` (never commit your token).

## Run

```powershell
python main.py                                         # home-assistant/core, latest 50 merged PRs, CSV
python main.py --repo home-assistant/frontend --max-prs 100
python main.py --repo home-assistant/addons --max-prs 0 --format parquet   # 0 = all merged PRs
python main.py --skip-extract                          # re-run transform/report on saved raw JSON
```

| Option | Default | Description |
|---|---|---|
| `--repo` | `home-assistant/core` | Repository as `owner/name` |
| `--max-prs` | `50` | Max merged PRs to analyse (`0` = all) |
| `--format` | `csv` | `csv` or `parquet` |
| `--data-dir` | `data` | Base output folder |
| `--skip-extract` | off | Reuse previously saved raw JSON (saves API calls while iterating) |

Each PR costs about 3 API calls (reviews, check runs, commit status), so 50 PRs is roughly 150 calls
and a PAT's 5,000/hour limit is comfortable. The client sleeps automatically if a limit is hit.

## Tests

```powershell
python -m pip install pytest
python -m pytest
```

## How it decides

**Merged PRs.** The list endpoint has no "merged" filter, so closed PRs are listed (newest-updated first) and
those with `merged_at` set are kept.

**CR_Passed.** True when at least one reviewer's *latest* meaningful review is `APPROVED`.
- A later `CHANGES_REQUESTED` or a dismissal cancels an earlier approval.
- `COMMENTED` reviews don't change a verdict.
- Self-approvals by the PR author and reviews from deleted accounts are ignored.

**CHECKS_PASSED.** Evaluated on the PR's head commit (the last commit before merge), combining check runs and
legacy commit statuses. `success`, `neutral` and `skipped` count as passing; pending counts as not passed;
for re-run checks the latest run wins.

Which checks are *required* is resolved in this order:
1. Branch protection of the base branch (`required_status_checks`). Needs admin rights, usually unavailable.
2. Repository rulesets for the branch (readable on public repos).
3. **Fallback:** if neither is readable, all checks on the commit must have passed. If there are no checks at all,
   the value is left empty rather than guessed. The `checks_source` field in `data/transform/*.json`
   shows which method was used for each PR.

## Assumptions and limitations

- Branch protection / rulesets show the rules **as they are today**, not as they were at merge time.
- PRs are taken in "recently updated" order, so with `--max-prs` you get recently active merged PRs,
  not strictly the most recently merged.
- Only checks on the head commit are considered; admin overrides ("merged without waiting for checks") are not detected.
- Required code-owner approvals (CODEOWNERS) are not enforced: any independent approval counts as `CR_Passed`.
- An approval is not matched to the commit it was given on, so an approval given before later commits still counts.
- PRs merged into a branch with no readable rules (for example a stacked PR targeting a feature branch) use the
  fallback: every check on the commit must have passed. These rows show `all_checks_fallback` in
  `data/transform/*.json`.
- On `home-assistant/core` the required checks are read from repository rulesets (branch protection itself needs
  admin rights). On other repos both sources may be unreadable, so the fallback applies to all their PRs.

## Project structure

```
ai_utils/        markdown resources for AI tools (spec, API notes, standalone Jira prompt)
data/            generated output (git-ignored): extract/, transform/, report/
samples/         small example report committed for reviewers
tests/           unit tests for review/check logic
extract.py       BaseExtractor + GitHubExtractor
transform.py     BaseTransformer + GitHubTransformer + pure evaluate_* functions
main.py          CLI orchestration
utils.py         GitHubClient (auth, pagination, retries, rate limits), JSON I/O, report writer
```

## Extending to another integration (e.g. Jira)

`BaseExtractor` and `BaseTransformer` hold the shared workflow; a new integration only implements
`extract()` / `transform_item()`. A standalone prompt that rebuilds the entire project for Jira from an empty repo (structure, shared
base classes, rules, tests, docs) is in `ai_utils/03_jira_integration_prompt.md`.

## Design decisions (talking points)

| Decision | Why | Alternative I rejected |
|---|---|---|
| Separate extract / transform steps, raw JSON saved in between | Business rules can change and be re-run (`--skip-extract`) without new API calls; also what the assignment asks for | One script doing fetch + judge in one pass |
| List `state=closed` and filter `merged_at != null` | The PR list endpoint has no "merged" filter | Search API (`is:merged`): capped at 1,000 results and a stricter rate limit |
| Judge checks on `head.sha` | CI results attach to the PR's last commit; the merge commit is a new commit created at merge time | `merge_commit_sha` |
| Required checks from branch protection, then rulesets, then a fallback | The PR object doesn't say what was required; protection needs admin rights, rulesets are public | Assume all checks are required without trying to find out |
| `CHECKS_PASSED` can be empty (unknown) | No rules and no checks means no evidence, and a guess would be misleading | Defaulting to True or False |
| Missing / pending required check counts as failed | If a check was required it should have run and finished | Ignoring checks that never appeared |
| Latest review per reviewer decides | "Approved, then changes requested" is not an approved PR | Counting any approval ever given |
| Author's own approval ignored | Not an independent review | Counting it |
| Pure `evaluate_*` functions | Testable without network; easy to explain | Logic buried inside loops |
| `GitHubClient` class with retries, back-off, rate-limit waiting, lazy pagination | Hundreds of calls will hit transient errors and the hourly limit | Bare `requests.get` calls |
| Base classes `BaseExtractor` / `BaseTransformer` | Shared workflow once, integration-specific parts in subclasses (the bonus's OOP hint) | Copy-pasting the pipeline for Jira |
| Token only from `.env` | Never committed or logged | Hard-coding it |

## Git workflow and CI

- `main` always works. Each piece of work is done on a short-lived branch
  (`feature/...`, `docs/...`, `fix/...`) and merged through a pull request.
- Commit messages follow a simple convention: `feat:`, `fix:`, `test:`, `docs:`, `chore:`.
- GitHub Actions (`.github/workflows/tests.yml`) runs `python -m pytest` on every push to `main` and on
  every pull request. The tests are offline (no token needed), so CI needs no secrets.
- The real extraction is deliberately NOT run in CI: it would need a token stored as a secret and would
  depend on GitHub's rate limits.