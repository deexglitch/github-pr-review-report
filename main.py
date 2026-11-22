"""Entry point: runs EXTRACT -> TRANSFORM -> REPORT.

Usage: python main.py --repo home-assistant/core --max-prs 50

Orchestration only; the business logic lives in extract.py and transform.py.
"""
from __future__ import annotations

import argparse
import logging
import os
import sys
from pathlib import Path

from dotenv import load_dotenv

from extract import GitHubExtractor
from transform import GitHubTransformer
from utils import GitHubClient, setup_logging, write_report

logger = logging.getLogger("main")


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Report whether merged PRs passed code review and required checks.")
    p.add_argument("--repo", default="home-assistant/core", help="owner/name (default: %(default)s)")
    p.add_argument("--max-prs", type=int, default=50, help="Max merged PRs to analyse, 0 = all (default: %(default)s)")
    p.add_argument("--format", choices=["csv", "parquet"], default="csv", help="Report format (default: %(default)s)")
    p.add_argument("--data-dir", default="data", help="Base output folder (default: %(default)s)")
    p.add_argument("--skip-extract", action="store_true", help="Reuse the previously saved raw JSON")
    return p.parse_args()


def main() -> int:
    """Run the pipeline. Returns the process exit code (0 = success, 1 = error)."""
    setup_logging()
    load_dotenv()  # loads GITHUB_TOKEN from .env so it never lives in the source code
    args = parse_args()

    token = os.getenv("GITHUB_TOKEN")
    if not token:
        logger.error("GITHUB_TOKEN is not set. Add it to your .env file (see .env.example).")
        return 1
    # Warn rather than fail, so the script can still be tried on other repos.
    if not args.repo.startswith("home-assistant/"):
        logger.warning("The assignment targets the home-assistant organisation; got %s", args.repo)

    data_dir = Path(args.data_dir)
    extractor = GitHubExtractor(GitHubClient(token), args.repo, args.max_prs, output_dir=data_dir / "extract")
    transformer = GitHubTransformer(args.repo, output_dir=data_dir / "transform")

    # 1. Extract (or reuse the saved raw JSON, so tweaking the rules costs no API calls)
    raw_path = data_dir / "extract" / extractor.output_filename
    if args.skip_extract:
        if not raw_path.exists():
            logger.error("--skip-extract used but %s does not exist.", raw_path)
            return 1
        logger.info("Skipping extract, reusing %s", raw_path)
    else:
        raw_path = extractor.run()

    # 2. Transform
    records = transformer.run(raw_path)

    # 3. Report
    df = transformer.to_report_frame(records)
    report_name = f"{args.repo.replace('/', '__')}_pr_review_report.{args.format}"
    report_path = write_report(df, data_dir / "report" / report_name, args.format)

    logger.info("Report written: %s (%d PRs)", report_path, len(df))
    if len(df):
        logger.info(
            "CR_Passed=True: %d | CHECKS_PASSED=True: %d | CHECKS_PASSED unknown: %d",
            int(df["CR_Passed"].sum()),
            int(df["CHECKS_PASSED"].sum()),  # sum() skips unknown (<NA>) values
            int(df["CHECKS_PASSED"].isna().sum()),
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())