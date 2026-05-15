#!/usr/bin/env python3
"""GitHub Actions entry point — process a single PR close event.

Reads PR context from env (set by the workflow), looks up the matching S3 knowledge record,
and calls the shared update_record logic (which writes S3 + posts to Slack thread).

Idempotent: re-runs are safe — update_record's slack_notified guard prevents duplicate posts.
"""

import logging
import os
import sys

from feedback_lib import (
    find_s3_record,
    get_review_comments,
    update_record,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)


def main() -> int:
    pr_number = os.environ.get("PR_NUMBER", "").strip()
    pr_merged = os.environ.get("PR_MERGED", "").strip().lower() == "true"

    if not pr_number:
        logger.error("PR_NUMBER env var missing")
        return 2

    outcome = "PR_MERGED" if pr_merged else "PR_CLOSED"
    logger.info(f"Processing PR #{pr_number} outcome={outcome}")

    key, record = find_s3_record(pr_number)
    if not record:
        logger.info(f"No S3 record for PR #{pr_number} — not orbit-managed, exiting")
        return 0

    if (
        record.get("outcome") == outcome
        and record.get("slack_notified") is True
        and bool(record.get("feedback_valence"))
    ):
        logger.info(f"PR #{pr_number} already fully processed — nothing to do")
        return 0

    comments = get_review_comments(int(pr_number))
    update_record(key, record, outcome, comments)
    return 0


if __name__ == "__main__":
    sys.exit(main())
