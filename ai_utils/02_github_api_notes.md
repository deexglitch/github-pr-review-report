# GitHub REST API notes

Base URL `https://api.github.com`. Headers on every request:
`Authorization: Bearer <PAT>`, `Accept: application/vnd.github+json`, `X-GitHub-Api-Version: 2022-11-28`.

## Entity relationships
```
Repository
 └─ Pull request ── base.ref (target branch) ── branch protection / rulesets (required checks)
     ├─ Reviews (one per submission, each with a user + state)
     └─ head.sha (last commit)
         ├─ Check runs     (GitHub Actions and other apps)
         └─ Commit statuses (legacy status API)
```

## Endpoints used
| Purpose | Endpoint |
|---|---|
| List PRs | `GET /repos/{repo}/pulls?state=closed&sort=updated&direction=desc` |
| Reviews | `GET /repos/{repo}/pulls/{n}/reviews` |
| Check runs | `GET /repos/{repo}/commits/{sha}/check-runs` (response wrapped in `check_runs`) |
| Commit statuses | `GET /repos/{repo}/commits/{sha}/status` |
| Required checks (protection) | `GET /repos/{repo}/branches/{branch}/protection/required_status_checks` |
| Required checks (rulesets) | `GET /repos/{repo}/rules/branches/{branch}` |

## Gotchas
- There is no "merged" filter on the PR list: use `state=closed` and keep `merged_at != null`.
- Pagination: `per_page` max 100, follow the `Link: rel="next"` header.
- Rate limits: 60/h unauthenticated, 5,000/h with a PAT. Watch `X-RateLimit-Remaining` / `X-RateLimit-Reset`;
  secondary limits send `Retry-After`.
- Branch protection needs admin rights; outsiders get 401/403/404 -> treat as "unknown", don't crash.
- Branch protection / rulesets reflect the rules **today**, not at merge time.
- Review states: `APPROVED`, `CHANGES_REQUESTED`, `COMMENTED`, `DISMISSED`, `PENDING`.
- `pull.user` can be `null` for deleted accounts.
- Check-run conclusions: `success, failure, neutral, cancelled, skipped, timed_out, action_required, stale`.
