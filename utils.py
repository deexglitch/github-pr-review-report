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