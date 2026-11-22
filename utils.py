"""Shared helpers used by every step of this pipeline

4 small, generic helpers:
setup_logging
save_json
load_json
write_report

a HTTP layer that communicates with Github
GitHubAPIError
GithubClient

design decision: 
decided to separate utils.py since extract.py a transform.py need to only contain business logic
for example: "what is a merged PR", "what counts as passed"
Plumbing like HTTP, retries, file I/O live in this file so it can be
reused by other integrations w/o copy pasting.
"""

from __future__ import annotations
# makes all type hints lazy strings, can write 'str | None' and 'list[dict]'
#in annotations w/o worrying about the python version

import json
import logging
import time 
from pathlib import Path #path objs safer than string paths (windows and linux compatible)
from typing import Any, Iterator #only used for type hints (docs for readers/IDEs)

import pandas as pd #used to write the final CSV/Parquet report
import requests #HTTP lib used to call Github REST API

#a module level logger, __name__ is utils here so log lines can be traced back to this file
logger = logging.getLogger(__name__) 

API_ROOT = "https://api.github.com"
MAX_RATE_LIMIT_WAITS = 10 #safety net for sleeping through a rate-limit window for 1 request

def setup_logging(level: int = logging.INFO) -> None:
    """config how log msgs look
    
    used logging for timestamp and severity level (INFO / WARNING/ ERROR), and verbosity can be changed in 1 place
    """
    logging.basicConfig(
        level=level, 
        format="%(asctime)s | %(levelname)-7s | %(message)s", #padded to 7 chars so cols line up
        datefmt="%H:%M:%S"
    )

def save_json(data: Any, path: Path) -> Path:
    """write data to path as JSON and return the path
    
    for persistent output at every pipeline step
    """
    path = Path(path) #accepts both str and Path from callers
    path.parent.mkdir(parents=True, exist_ok=True) #exist_ok -> dont raise an error if there
    with path.open("w", encoding="utf-8") as fh: #windows default encoding is cp1252, fails on emojis
        json.dump(data, fh, indent=2, ensure_ascii=False) #indent 2 for readable file, ensure_ascii for keeping real chars instead of esc them as \uXXXX
    return path

def load_json(path: Path) -> Any:
    """reads a Json file written by save_json"""
    with Path(path).open("r", encoding="utf-8") as fh:
        return json.load(fh)


def write_report(df: pd.DataFrame, path: Path, fmt: str = "csv") -> Path:
    """write final report as a CSV or parquet"""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    if fmt == "csv":
        df.to_csv(path, index=False) #index doesnt write pandas internal 0,1,2 row nos as an xtra column
    elif fmt == "parquet":
        df.to_parquet(path, index=False) #needs pyarrow package
    else:
        raise ValueError(f"Unsupported report format: {fmt!r}")
    return path


#github HTTP client

class GitHubAPIError(Exception):
    """raised when a request fails after retries fail too. this type lets callers catch errors w/o also catching unrelated bugs"""

#this class will do the heavy lifting 
class GitHubClient:
    """acts as a wrapper around requests that adds anything github will need:
        -auth header (personal access token)
        -pagination (gh returns lists in pages)
        -retries w backoff for temp fails (5xx, network errors)
        -auto waiting when rate limit exhausted

        token, session and settings are a shared state that each req needs.
        this class keeps it in 1 obj that extract.py can receive
    """


    def __init__(
        self,
        token: str | None,
        base_url: str = API_ROOT,
        max_retries: int = 5,
        timeout: int = 30,
    ) -> None:
        self.base_url = base_url.rstrip("/") #strips a trialing "/"
        # f"{base_url}{path}" never makes "//"
        self.max_retries = max_retries
        self.timeout = timeout

        # A Session reuses an underlying TCP/TLS connection between requests 
        # (much faster than opening a new connection for each of the hundreds of calls we make)
        # lets us set headers once for all of them.
        self.session = requests.Session()
        self.session.headers.update(
            {
                # Asks GitHub for its standard JSON format.
                "Accept": "application/vnd.github+json",
                # Pins the API version so GitHub changing defaults later can't break this script.
                "X-GitHub-Api-Version": "2022-11-28",
                # GitHub rejects requests with no User-Agent; identifying the app is good practice.
                "User-Agent": "github-pr-review-report",
            }
        )
        if token:
            # "Bearer <token>" is how GitHub expects a PAT. The token is read from .env by
            # main.py and is never hard-coded or logged anywhere.
            self.session.headers["Authorization"] = f"Bearer {token}"
        else:
            # Without a token the limit is 60 requests/hour instead of 5,000/hour.
            logger.warning("No token supplied: limited to 60 requests/hour.")  

    #public methods
    def get(self, path: str, params: dict | None = None, optional: bool = False) -> Any | None:
            """GET one JSON resource (not a paginated list).
    
            optional=True means "this data might legitimately be unavailable" on 401/403/404/409/422
            return None instead of raising.
            Example: branch protection settings need admin rights, so for an outsider a 403/404 is *expected* and must not crash the script.
            """
            resp = self._request(path, params, optional)
            # _request returns None only in the optional case above.
            return None if resp is None else resp.json()

    def paginate(
            self,
            path: str,
            params: dict | None = None,
            items_key: str | None = None,
            optional: bool = False,
        ) -> Iterator[Any]:
            """Yield every item of a paginated list endpoint, page style.
    
            It is a generator (it uses `yield`): the next page is only requested when the caller
            asks for more items. 
            WHY? when we only need 50 merged PRs we stop early and never download the remaining page to save API calls.
    
            items_key: some endpoints return the list wrapped in an object
            For example:
            /check-runs returns {"total_count": 3, "check_runs": [...]}, so we pass items_key="check_runs".
            Endpoints like /pulls return a bare list, so items_key stays None.
            """
            
            params = {"per_page": 100, **(params or {})} #100 is ghs max -> fewest possible requests
            # `{**a, **b}` merges two dicts so caller's params can override per_page if they really want to.
            url: str | None = path
            while url:  # loop until GitHub stops saying there exists a next page
                resp = self._request(url, params, optional)
                if resp is None:   #req not found/allowed
                    return
                body = resp.json()
                # yield from : hands over each item one by one to the caller.
                yield from (body[items_key] if items_key else body)
                # GitHub puts the address of the next page in the HTTP `Link` header.
                # `resp.links` is requests' parsed version: {"next": {"url": "...", "rel": "next"}, ...}.
                # On the last page there is no "next" key -> .get returns None -> loop ends.
                url = resp.links.get("next", {}).get("url")
                # The "next" URL already contains ?per_page=100&page=2..., so sending `params` again
                # would duplicate/override them therefore clear it.
                params = None


    #internals, not meant to be called from outside class
    def _request(self, path: str, params: dict | None, optional: bool) -> requests.Response | None:
        """perform 1 GET request with retries + rate limit handling"""
        #path is either relative or full url
        url = path if path.startswith("http") else f"{self.base_url}{path}"
        errors = 0 #limited by max_retries
        rate_waits = 0 #count of how long it slept for a rate limit (limited by MAX_RATE_LIMIT_WAITS)

        while True:
            #send request
            try:
                resp = self.session.get(url, params=params, timeout=self.timeout)
            except requests.RequestException as exc:
                # Network-level problem (DNS failure, connection reset, timeout...).
                errors += 1
                if errors > self.max_retries:
                    raise GitHubAPIError(f"Network error for {url}: {exc}") from exc
                self._backoff(errors, f"network error ({exc})")
                continue  # try again

            #success
            if resp.ok:
                return resp

            # rate-limited response = 403, same as "no permission"
            # if we treated it as "no permission" we would silently lose data. So we first make sure it isn't a rate limit.
            if self._is_rate_limited(resp) and rate_waits < MAX_RATE_LIMIT_WAITS:
                rate_waits += 1
                self._sleep_until_reset(resp)
                continue  # retry the same request after waking up

            # temp server error so retry with backoff
            if resp.status_code >= 500 and errors < self.max_retries:
                errors += 1
                self._backoff(errors, f"HTTP {resp.status_code}")
                continue

            # 401 no token / 403 no permission / 404 not found or hidden / 409 conflict /
            # 422 e.g. "No commit found for SHA" (commit from a deleted fork).
            if optional and resp.status_code in (401, 403, 404, 409, 422):
                logger.debug("Optional GET %s -> %s", url, resp.status_code)
                return None

            # else error
            raise GitHubAPIError(f"GET {url} failed: {resp.status_code} {resp.text[:200]}")

    # the function doesn't use `self`, it's just grouped with the class
    @staticmethod
    def _is_rate_limited(resp: requests.Response) -> bool:
        """whether a failed response means 'you hit the rate limit' or not

        check all of gh signals for rate limit.
        
        """
        if resp.status_code == 429: #too many reqs
            return True
        if resp.status_code != 403: # rate limits only codes are 429/403
            return False
        # Primary limit: the hourly quota is used up
        if resp.headers.get("X-RateLimit-Remaining") == "0":
            return True
        # Secondary (abuse) limit: GitHub tells us how long to wait via Retry-After.
        if "Retry-After" in resp.headers:
            return True
        # Last resort: the error message text mentions it.
        return "rate limit" in resp.text.lower()

    @staticmethod
    def _sleep_until_reset(resp: requests.Response) -> None:
        """Sleep for as long as GitHub says we must wait."""
        retry_after = resp.headers.get("Retry-After")
        if retry_after and retry_after.isdigit():
            # Secondary limit: GitHub gives the wait time in seconds directly.
            wait = int(retry_after)
        else:
            # Primary limit: X-RateLimit-Reset is the *Unix timestamp* at which the quota resets,
            # so the wait is (reset time - now). `or 0` guards against an empty header.
            reset = int(resp.headers.get("X-RateLimit-Reset", "0") or 0)
            wait = reset - time.time()
        # Clamp to [1, 3700] seconds: at least 1 (never a zero/negative sleep if the clock is
        # slightly off), at most ~1 hour (GitHub's window) + margin so a bad header can't make
        # us sleep for days. The extra +1 is a safety margin because reset is whole seconds.
        wait = max(1, min(wait, 3700)) + 1
        logger.warning("Rate limited. Sleeping %.0f seconds...", wait)
        time.sleep(wait)

    @staticmethod
    def _backoff(attempt: int, reason: str) -> None:
        """Exponential back-off: wait 2s, 4s, 8s, 16s, then 30s (capped).

        WHY exponential: if the server is struggling, hammering it every second makes things
        worse; waiting longer after each failure gives it time to recover.
        """
        wait = min(2 ** attempt, 30)
        logger.warning("Request failed (%s). Retry in %ss...", reason, wait)
        time.sleep(wait)
    