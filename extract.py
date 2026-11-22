""" this acts as our claw machine to retrieve raw merged pr data from gh and save it as JSON

extract.py only collects data. no other logic for if a PR "passed a review" -> will do that in transform.py
made this choice to keep raw data so that if we need to change business rules later and rerun transform withput calling the API again

core idea with github:
a repo has a PR that has 3 fields:
    base.ref - branch it was merge into
        branch protection rules => checks are required here
    reviews - each review has a reviewer + state (APPROVED,.. etc)
    head.sha - the PR's last commit
        check runs - results form github actions
        commit statuses - results from the older "status" API
"""
from __future__ import annotations

import logging
from abc import ABC, abstractmethod   # tools for defining an abstract base class (a "contract")
from pathlib import Path
from typing import Any, Iterator

from utils import GitHubClient, save_json

logger = logging.getLogger(__name__)

#abstracted base class workflow for any integration (just makes it easier to reuse)
class BaseExtractor(ABC):
    """defines the contract that any extractor should follow
    
    uses OOP since we fetch items and save as JSON for any integration we use
    however, how these items are fetched changes so this class accounts for the shared part
    so it only runs onec and each integration implements its extraction method
    kind of uses capstone principles
    """

    def __init__(self, output_dir: Path) -> None:
        self.output_dir = Path(output_dir)  # folder where the raw JSON will be written

    @property
    @abstractmethod
    def output_filename(self) -> str:
        """File name (inside output_dir) for the raw JSON. Each integration chooses its own."""

    @abstractmethod
    def extract(self) -> list[dict[str, Any]]:
        """Fetch and return one raw record per item (a PR, a Jira ticket, ...)."""

    def run(self) -> Path:
        """The shared workflow (the same for all integrations): fetch, save, report the path."""
        items = self.extract()
        path = save_json(items, self.output_dir / self.output_filename)
        logger.info("Saved %d raw records -> %s", len(items), path)
        return path


#github extractor implementation
class GitHubExtractor(BaseExtractor):
    """Fetches merged PRs of one repository plus everything needed to judge them."""

    def __init__(
        self,
        client: GitHubClient, # passed in rather than created here -> easy to replace with a fake in tests
        repo: str, # "owner/name"
        max_prs: int | None = None,
        output_dir: Path = Path("data/extract"),
    ) -> None:
        super().__init__(output_dir)   # let the base class store output_dir
        self.client = client
        self.repo = repo
        # `0 or None` -> None, so both 0 and None mean "no limit". main.py documents --max-prs 0 = all.
        self.max_prs = max_prs or None
        # Cache: branch name -> required-checks result
        # many PRs target the same branch, and the answer is the same for all of them
        # therefore Asking GitHub once per branch instead of once per PR saves API calls.
        self._required_cache: dict[str, dict[str, Any]] = {}

    @property
    def output_filename(self) -> str:
        # "home-assistant/core" -> "home-assistant__core_pull_requests.json"
        # The "/" can't be used in a file name, so it is replaced with "__".
        return f"{self.repo.replace('/', '__')}_pull_requests.json"

    #main flow endpoints
    def extract(self) -> list[dict[str, Any]]:
        """For each merged PR collect its details and return the list of raw records."""
        records: list[dict[str, Any]] = []
        for pr in self._iter_merged_pull_requests():
            # Progress log, e.g. "PR #193540 (3/50)". len(records)+1 = this PR's position.
            logger.info(
                "Fetching details for PR #%s (%d/%s)", pr["number"], len(records) + 1, self.max_prs or "all"
            )
            records.append(self._collect_pr_details(pr))
        return records


    def _iter_merged_pull_requests(self) -> Iterator[dict[str, Any]]:
        """Yield merged PRs only, stopping once max_prs have been found.

        NB API fact: GET /pulls has NO "merged" filter. `state` only accepts open/closed/all.
        A PR can be closed because it was merged OR because it was rejected. So we ask for
        closed PRs and keep those whose `merged_at` timestamp is set (it is null if never merged).
        (The Search API `is:pr is:merged` is an alternative, but it caps at 1000 results and has
        a much stricter rate limit, so listing + filtering is more robust.)
        """
        found = 0
        pulls = self.client.paginate(
            f"/repos/{self.repo}/pulls",
            params={
                "state": "closed",
                # sort by last-updated, newest first for recent PRs 1st
                # Merging a PR updates it, so recently merged PRs are near the top. Caveat (documented in the README): a
                # comment on an old PR also bumps it, so it is "recently active", not strictly
                # "recently merged".
                "sort": "updated",
                "direction": "desc",
            },
        )
        for pr in pulls:
            if not pr.get("merged_at"):
                continue  # closed without merging -> not part of our report
            yield pr
            found += 1
            # Stop early. Because `paginate` is lazy, no further pages are downloaded.
            if self.max_prs and found >= self.max_prs:
                return


    def _collect_pr_details(self, pr: dict[str, Any]) -> dict[str, Any]:
        """Fetch everything needed to judge one PR and bundle it into one raw record."""
        number = pr["number"]
        # WHY head.sha and not merge_commit_sha: CI checks run against the PR's own last commit
        # (head). The merge commit is a *new* commit created at merge time (and for squash/rebase
        # merges it differs from anything the PR checks ran on). "Checks passed before the merge"
        # therefore means the checks attached to the head commit.
        head_sha = pr["head"]["sha"]
        base_branch = pr["base"]["ref"]  # the branch the PR was merged INTO; rules are defined per branch

        # All reviews of this PR (paginated, so we collect them into a list).
        reviews = list(self.client.paginate(f"/repos/{self.repo}/pulls/{number}/reviews"))

        # Check runs of the head commit. The response is {"total_count": N, "check_runs": [...]},
        # hence items_key="check_runs". optional=True because the commit can be unreachable
        # (e.g. the contributor's fork was deleted) and we don't want one odd PR to crash the run.
        check_runs = list(
            self.client.paginate(
                f"/repos/{self.repo}/commits/{head_sha}/check-runs",
                items_key="check_runs",
                optional=True,
            )
        )

        # The "combined status" of the legacy Status API (older CI systems report here instead of
        # as check runs). It returns the latest status per context under "statuses".
        commit_status = self.client.get(
            f"/repos/{self.repo}/commits/{head_sha}/status",
            params={"per_page": 100},
            optional=True,
        )

        # One record = the PR plus all its related entities, stored exactly as GitHub returned
        # them ("raw"). transform.py will interpret them.
        return {
            "pull_request": pr,
            "reviews": reviews,
            "check_runs": check_runs,
            "commit_status": commit_status,
            "required_checks": self._get_required_checks(base_branch),
        }

    #checks
    def _get_required_checks(self, branch: str) -> dict[str, Any]:
        """Work out which status checks the base branch requires (cached per branch).

        The PR object does NOT say which checks were required, so we look at the branch's rules.
        There are two places GitHub stores them, so we try both:

          1. Branch protection (classic) -> needs ADMIN rights on the repo. As an outsider we
             usually get 403/404, which is why the request is `optional`.
          2. Repository rulesets (newer) -> readable on public repos without special rights.

        If neither is readable we return contexts=None, meaning "unknown". transform.py then uses
        a documented fallback ("all checks on the commit must have passed").

        Returned shape: {"source": <where we got it>, "contexts": [<check names>] or None}
        """
        # Cache hit: we already asked GitHub about this branch.
        if branch in self._required_cache:
            return self._required_cache[branch]

        # Default = "unknown" until one of the two sources answers.
        result: dict[str, Any] = {"source": "unavailable", "contexts": None}

        # ---- Source 1: classic branch protection ----------------------------------------
        protection = self.client.get(
            f"/repos/{self.repo}/branches/{branch}/protection/required_status_checks",
            optional=True,   # 401/403/404 are *expected* for non-admins -> returns None
        )
        if protection is not None:
            # GitHub exposes the names in two fields: the legacy `contexts` list (strings) and the
            # newer `checks` list (objects with "context" and "app_id"). We combine both.
            contexts = list(protection.get("contexts", []))
            contexts += [c["context"] for c in protection.get("checks", []) if c.get("context")]
            # set() removes duplicates (the two lists often overlap); sorted() makes the output
            # deterministic, so the saved JSON doesn't change between runs.
            result = {"source": "branch_protection", "contexts": sorted(set(contexts))}
        else:
            # ---- Source 2: repository rulesets --------------------------------------------
            # Returns a list of rules that apply to this branch (or an empty list).
            # `or []` turns the None of a failed optional call into an empty list.
            rules = self.client.get(f"/repos/{self.repo}/rules/branches/{branch}", optional=True) or []
            contexts = []
            found_rule = False  # distinguishes "rule exists but lists no checks" from "no such rule"
            for rule in rules:
                if rule.get("type") == "required_status_checks":
                    found_rule = True
                    params = rule.get("parameters", {})
                    contexts += [c["context"] for c in params.get("required_status_checks", [])]
            if found_rule:
                result = {"source": "repository_rules", "contexts": sorted(set(contexts))}

        logger.info(
            "Required checks for '%s': source=%s, count=%s",
            branch,
            result["source"],
            None if result["contexts"] is None else len(result["contexts"]),
        )
        self._required_cache[branch] = result  # remember for the next PR on the same branch
        return result