# Standalone prompt: reproduce the whole project for Jira

**How to use:** open an empty folder in an AI coding agent (Claude Code, Cursor, etc.) and paste
everything below the line. The prompt does not depend on any existing code: it describes the full
project (structure, shared design, rules, outputs, tests, docs) so the agent can rebuild it from scratch
for Jira. The same prompt works for other integrations by changing section 3 (the "integration spec").

---

# ROLE
You are a senior Python data engineer. Build, test and document a small ETL application from an empty
repository. Work autonomously: when something is ambiguous, choose the most reasonable option, write
the assumption in `README.md`, and continue. Do not invent API behaviour: verify every endpoint,
parameter and pagination style against the official documentation before relying on it.

# 1. GOAL
Extract the list of tickets from a Jira Cloud project using an **API key (API token)**, analyse each
ticket, and produce a CSV or Parquet report. The application mirrors an existing GitHub pipeline that
reports whether merged pull requests passed code review and required status checks; the Jira version
reports the equivalent for tickets (section 3). The architecture must make a *second integration*
(GitHub, or any other) addable by writing new subclasses only.

# 2. MANDATORY PROJECT STRUCTURE
```
ai_utils/        markdown resources for AI tools (copy of this prompt, API notes, project spec)
data/            generated output, git-ignored: extract/  transform/  report/
tests/           pytest unit tests
extract.py       raw data retrieval
transform.py     analysis / business rules
main.py          orchestration + CLI
utils.py         shared helpers (optional but expected here)
requirements.txt, .env.example, .gitignore, README.md
```
Each pipeline step must write its output (JSON) into its own subfolder of `data/`.

# 3. INTEGRATION SPEC: JIRA

**Authentication.** Jira Cloud basic auth: `Authorization: Basic base64(email:api_token)`.
Read `JIRA_BASE_URL` (e.g. `https://yourcompany.atlassian.net`), `JIRA_EMAIL` and `JIRA_API_TOKEN`
from environment variables loaded from `.env` with python-dotenv. Never hard-code or log them.
Provide `.env.example` with placeholders; `.env` must be in `.gitignore`.

**Extraction.**
- Use the Jira Cloud REST API v3 JQL search. Check the current docs: the older `/rest/api/3/search`
  was deprecated in favour of `/rest/api/3/search/jql`, which paginates with `nextPageToken`
  instead of `startAt`. Implement the pagination style the docs specify today.
- CLI input: `--project KEY` (required unless `--jql` is given) and optional `--jql`; `--max-tickets N`
  (0 = all). Default JQL when only a project is given: `project = KEY ORDER BY created DESC`.
- Request only the fields needed (key, summary, reporter, status, resolutiondate, created) and expand
  the **changelog** so status transitions are available. If the search endpoint does not return the full
  changelog, fetch it per ticket from the issue changelog endpoint.
- Save the raw response records untouched to `data/extract/<project>_tickets.json`.

**Report (one row per ticket)**, columns in this order:

| Column | Source / meaning |
|---|---|
| `TICKET_KEY` | issue key, e.g. `ABC-123` |
| `TITLE` | summary |
| `REPORTER` | reporter display name (empty if the account was deleted) |
| `RESOLVED_DATE` | `resolutiondate` (UTC) |
| `CR_Passed` | the ticket passed through a **review** status before being resolved |
| `CHECKS_PASSED` | the ticket passed through a **QA/verification** status before being resolved |

Rules (implement as pure, unit-testable functions, separate from any I/O):
- Only tickets whose status category is **Done** and that have a `resolutiondate` are reported.
- `CR_Passed`: the changelog shows a transition *into* any configured review status
  (default `In Review, Code Review`) at or before the resolution time. Make the names configurable
  via `--review-statuses`, because every Jira workflow differs. Case-insensitive match.
- `CHECKS_PASSED`: same logic with `--qa-statuses` (default `QA, Testing, In QA`).
- If no status in the configured list exists in the project's workflow, the value is **empty (unknown)**,
  not False. Use a nullable boolean type so unknown stays distinct from False.
- Edge cases: null reporter, no changelog, reopened tickets (use the *last* resolution), custom status
  names, tickets resolved without any workflow transitions, pagination across many pages.

# 4. SHARED ARCHITECTURE (this is the part that makes more integrations possible)
Use OOP so that everything integration-independent is written once.
- `utils.py`:
  - `setup_logging`, `save_json`, `load_json`, `write_report(df, path, fmt)` for CSV or Parquet.
  - `BaseHTTPClient`: wraps `requests.Session` with timeouts, retry with exponential back-off on
    network errors and 5xx, rate-limit handling (HTTP 429 and `Retry-After`; sleep and retry, with a
    cap on total waits), lazy pagination generator, and an `optional=True` mode that returns None for
    expected 401/403/404 instead of raising. Subclasses supply auth headers and pagination rules.
  - `JiraClient(BaseHTTPClient)`: Jira auth and token-based pagination.
- `extract.py`: abstract `BaseExtractor` with `output_filename` (property), abstract `extract()`
  and a concrete `run()` that calls `extract()` and saves JSON. `JiraExtractor(BaseExtractor)`.
  Inject the client through the constructor so tests can pass a fake.
- `transform.py`: abstract `BaseTransformer` with `REPORT_COLUMNS`, `BOOLEAN_COLUMNS`, `DATE_COLUMNS`,
  abstract `transform_item(raw) -> dict`, and concrete `transform()`, `run(input_path)`
  (load raw JSON, transform, save analysed JSON) and `to_report_frame(records)` (select and order the
  columns, nullable `boolean` dtype, UTC datetimes). `JiraTransformer(BaseTransformer)`.
- `main.py`: argparse CLI with `--source` (registry dict mapping `"jira"` to its extractor and
  transformer classes, so a new integration needs no change to the flow), `--format csv|parquet`,
  `--data-dir`, `--skip-extract` (reuse saved raw JSON). Return proper exit codes. `main.py` contains
  no business logic.

# 5. QUALITY REQUIREMENTS
- Python 3.10+. Type hints everywhere. Small functions. No global mutable state.
- **Comment the code thoroughly**: module docstrings stating purpose, and inline comments explaining
  *why* each non-obvious decision was made (the owner must be able to explain every line in an interview).
- Log progress with `logging`, not `print`. Never log secrets.
- Handle errors explicitly; fail with a clear message on missing credentials.

# 6. TESTS
`pytest` with `pytest.ini` setting `pythonpath = .`. Unit-test every business rule from section 3 with
fake data (no network): happy path, no changelog, null reporter, reopened ticket, unknown workflow names,
case-insensitive status matching, out-of-order changelog entries. Add one test that runs a full
record through `JiraTransformer.transform_item`. Add a pipeline test using a fake client class.

# 7. DOCUMENTATION
- `README.md`: purpose, setup (venv, install, `.env`), run examples, output columns, how each rule is
  decided, assumptions and limitations, project structure, and how to add another integration.
- `ai_utils/`: save this prompt, a short `api_notes.md` (endpoints used, auth, pagination, rate limits,
  gotchas you verified) and a `project_spec.md` summarising the rules.

# 8. DEFINITION OF DONE (verify each point yourself and report the results)
1. `python -m pytest` passes.
2. `python main.py --source jira --project ABC --max-tickets 20` creates, in order:
   `data/extract/*.json`, `data/transform/*.json` and `data/report/*.csv`
   (the report has exactly the six columns of section 3).
3. `--format parquet` and `--skip-extract` both work.
4. No secret appears in the repo, in logs or in saved JSON; `git status` shows no `.env`.
5. Adding a new integration would require only new `Extractor`/`Transformer` subclasses and one
   registry entry (explain how in the README).
Finish with a short summary of what you built, the assumptions you made, and anything you could not verify.
