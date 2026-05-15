#!/usr/bin/env python3
"""Shared logic for feedback ingestion: GitHub PR lookup, S3 record I/O, Slack notification.

Used by both the cronjob (ingest.py) and the GitHub Actions per-PR handler (handle_pr_event.py).
"""

import os
import json
import logging
import re
import boto3
import httpx
from datetime import datetime, timezone

logger = logging.getLogger(__name__)

GITHUB_TOKEN    = os.environ["GITHUB_TOKEN"]
GITHUB_REPO     = os.environ["GITHUB_REPO"]
S3_BUCKET       = os.environ.get("S3_KNOWLEDGE_BUCKET", "orbit-knowledge-store-952893849914")
S3_PREFIX       = os.environ.get("S3_KNOWLEDGE_PREFIX", "fixes/")
SLACK_BOT_TOKEN = os.environ.get("SLACK_BOT_TOKEN", "")

s3 = boto3.client("s3", region_name="us-east-1")

VALENCE_BY_OUTCOME = {
    "PR_MERGED": "positive",
    "PR_CLOSED": "negative",
    "DISMISSED": "dismissed",
}

_MERGE_AUTO_HEADER = re.compile(r"^Merge pull request #\d+ from .+$")


def gh_headers():
    return {"Authorization": f"token {GITHUB_TOKEN}", "Accept": "application/vnd.github.v3+json"}


def _post_slack_message(channel, text, thread_ts=None):
    """POST to chat.postMessage. Returns True on ok, False otherwise. Never raises."""
    payload = {"channel": channel, "text": text}
    if thread_ts:
        payload["thread_ts"] = thread_ts
    headers = {
        "Authorization": f"Bearer {SLACK_BOT_TOKEN}",
        "Content-Type": "application/json; charset=utf-8",
    }
    try:
        with httpx.Client(timeout=30.0) as client:
            resp = client.post("https://slack.com/api/chat.postMessage", headers=headers, json=payload)
        body = resp.json()
        if body.get("ok"):
            return True
        logger.warning(f"Slack post failed: error={body.get('error')} channel={channel} thread_ts={thread_ts}")
        return False
    except httpx.HTTPError as e:
        logger.warning(f"Slack post HTTP error: {e}")
        return False


def post_slack_outcome(record):
    """Post PR outcome to original alert thread; fall back to top-level channel message on failure."""
    if not SLACK_BOT_TOKEN:
        logger.warning("SLACK_BOT_TOKEN unset — skipping Slack notification")
        return False

    channel   = record.get("slack_channel_id")
    thread_ts = record.get("slack_thread_ts")
    pr_url    = record.get("pr_url", "")
    pr_number = record.get("pr_number", "")
    outcome   = record.get("outcome", "")

    if not channel:
        logger.warning(f"Record missing slack_channel_id (pr={pr_number}) — skipping Slack notification")
        return False

    reason = record.get("human_correction_text", "").strip()
    reason_line = f"\n>_{reason}_" if reason else ""

    if outcome == "PR_MERGED":
        primary_text = f":white_check_mark: PR merged — fix accepted. <{pr_url}|View PR>{reason_line}"
    elif outcome == "PR_CLOSED":
        primary_text = f":x: PR closed without merge. <{pr_url}|View PR>{reason_line}"
    else:
        primary_text = f"PR outcome: {outcome}. <{pr_url}|View PR>"

    if thread_ts and _post_slack_message(channel, primary_text, thread_ts=thread_ts):
        logger.info(f"Slack notified (thread) channel={channel} thread_ts={thread_ts} pr={pr_number}")
        return True

    fallback_text = (
        f":warning: Could not post PR outcome to the original alert thread for "
        f"<{pr_url}|PR #{pr_number}> (outcome: {outcome}). "
        f"Please review the Slack notification configuration in jobs/feedback-ingestion."
    )
    if _post_slack_message(channel, fallback_text):
        logger.warning(f"Slack notified (fallback top-level) channel={channel} pr={pr_number}")
        return True

    logger.error(f"Slack notification fully failed channel={channel} pr={pr_number}")
    return False


def get_labeled_prs():
    """Fetch all open + closed PRs with the sre-auto-fix label."""
    api = f"https://api.github.com/repos/{GITHUB_REPO}/pulls"
    prs = []
    with httpx.Client(timeout=30.0) as client:
        for state in ("open", "closed"):
            resp = client.get(api, headers=gh_headers(), params={
                "state": state, "per_page": 100,
            })
            if resp.status_code == 200:
                for pr in resp.json():
                    labels = [l["name"] for l in pr.get("labels", [])]
                    if "sre-auto-fix" in labels:
                        prs.append(pr)
    return prs


def _is_human(actor: dict) -> bool:
    user = actor.get("user") or actor
    return user.get("type", "User") != "Bot" and not user.get("login", "").endswith("[bot]")


def get_review_comments(pr_number):
    """Get human-authored comments from a PR — formal reviews + issue comments."""
    comments = []
    with httpx.Client(timeout=30.0) as client:
        resp = client.get(
            f"https://api.github.com/repos/{GITHUB_REPO}/pulls/{pr_number}/reviews",
            headers=gh_headers()
        )
        if resp.status_code == 200:
            comments += [
                r.get("body", "") for r in resp.json()
                if r.get("body", "").strip() and _is_human(r)
            ]

        resp = client.get(
            f"https://api.github.com/repos/{GITHUB_REPO}/issues/{pr_number}/comments",
            headers=gh_headers()
        )
        if resp.status_code == 200:
            comments += [
                c.get("body", "") for c in resp.json()
                if c.get("body", "").strip() and _is_human(c)
            ]

    return comments


def get_merge_commit_message(pr_number) -> str:
    """Return custom merge-commit body (the message users type in GitHub's merge dialog).

    Strips GitHub's auto-generated `Merge pull request #N from ...` header line so only
    the human-authored body remains. Returns empty string if PR is unmerged, the API call
    fails, or no body exists.
    """
    try:
        with httpx.Client(timeout=30.0) as client:
            pr_resp = client.get(
                f"https://api.github.com/repos/{GITHUB_REPO}/pulls/{pr_number}",
                headers=gh_headers(),
            )
            if pr_resp.status_code != 200:
                return ""
            sha = pr_resp.json().get("merge_commit_sha")
            if not sha:
                return ""
            commit_resp = client.get(
                f"https://api.github.com/repos/{GITHUB_REPO}/commits/{sha}",
                headers=gh_headers(),
            )
            if commit_resp.status_code != 200:
                return ""
            message = commit_resp.json().get("commit", {}).get("message", "")
    except httpx.HTTPError as e:
        logger.warning(f"Merge commit fetch failed pr={pr_number}: {e}")
        return ""

    lines = message.splitlines()
    if lines and _MERGE_AUTO_HEADER.match(lines[0]):
        lines = lines[1:]
    return "\n".join(lines).strip()


def find_s3_record(pr_number):
    """Search S3 for a record matching the given PR number."""
    try:
        paginator = s3.get_paginator("list_objects_v2")
        for page in paginator.paginate(Bucket=S3_BUCKET, Prefix=S3_PREFIX):
            for obj in page.get("Contents", []):
                if obj["Key"].endswith(f"/{pr_number}.json"):
                    body = s3.get_object(Bucket=S3_BUCKET, Key=obj["Key"])["Body"].read()
                    return obj["Key"], json.loads(body)
    except Exception as e:
        logger.error(f"S3 search error: {e}")
    return None, None


def update_record(key, record, outcome, comments):
    """Update S3 record with outcome and human feedback. Post Slack on first transition."""
    record["outcome"] = outcome
    feedback_parts = [c for c in comments if c and c.strip()]
    if outcome == "PR_MERGED":
        merge_body = get_merge_commit_message(record.get("pr_number", ""))
        if merge_body:
            feedback_parts.append(merge_body)

    record["human_correction_text"] = " | ".join(feedback_parts)
    record["feedback_valence"] = VALENCE_BY_OUTCOME.get(outcome, "")
    record["agent_learning"] = record["human_correction_text"]
    record["last_updated"] = datetime.now(timezone.utc).isoformat()
    s3.put_object(
        Bucket=S3_BUCKET, Key=key,
        Body=json.dumps(record, indent=2),
        ContentType="application/json",
    )
    logger.info(f"Updated {key} -> outcome={outcome}, feedback_parts={len(feedback_parts)}")

    if not record.get("slack_notified") and post_slack_outcome(record):
        record["slack_notified"] = True
        s3.put_object(
            Bucket=S3_BUCKET, Key=key,
            Body=json.dumps(record, indent=2),
            ContentType="application/json",
        )
