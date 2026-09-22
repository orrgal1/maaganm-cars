#!/usr/bin/env python3
"""Poll GitHub issues and issue comments for the car service repository."""

import argparse
import hashlib
import json
import math
import signal
import subprocess
import sys
import threading
import time
from typing import Any, Dict, Iterable, List, Tuple

REPOSITORY = "orrgal1/maaganm-cars"
DEFAULT_INTERVAL = 30.0


class FetchError(RuntimeError):
    """Raised when a complete GitHub snapshot cannot be fetched."""


def _flatten_pages(value: Any) -> List[Dict[str, Any]]:
    """Accept gh --slurp output, while also tolerating an un-slurped page."""
    if not isinstance(value, list):
        raise FetchError("GitHub returned a non-list response")
    pages: Iterable[Any] = value
    if value and all(isinstance(item, dict) for item in value):
        pages = (value,)
    items: List[Dict[str, Any]] = []
    for page in pages:
        if not isinstance(page, list):
            raise FetchError("GitHub returned malformed pagination data")
        if not all(isinstance(item, dict) for item in page):
            raise FetchError("GitHub returned a malformed item")
        items.extend(page)
    return items


def _gh_api(endpoint: str) -> List[Dict[str, Any]]:
    command = ["gh", "api", endpoint, "--paginate", "--slurp"]
    try:
        completed = subprocess.run(command, capture_output=True, text=True, timeout=30)
    except subprocess.TimeoutExpired as exc:
        raise FetchError("gh api timed out") from exc
    except OSError as exc:
        raise FetchError("unable to run gh api ({})".format(type(exc).__name__)) from exc
    if completed.returncode != 0:
        raise FetchError("gh api failed (exit {})".format(completed.returncode))
    try:
        return _flatten_pages(json.loads(completed.stdout))
    except (ValueError, TypeError) as exc:
        raise FetchError("GitHub returned invalid JSON") from exc


def fetch_snapshot() -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    """Fetch all issues and issue comments, excluding pull requests."""
    issues = _gh_api("repos/{}/issues?state=all&per_page=100".format(REPOSITORY))
    pull_request_numbers = {
        issue.get("number")
        for issue in issues
        if isinstance(issue.get("pull_request"), dict)
    }
    comments = _gh_api("repos/{}/issues/comments?per_page=100".format(REPOSITORY))
    filtered_comments = []
    for comment in comments:
        issue_url = comment.get("issue_url", "")
        try:
            issue_number = int(str(issue_url).rstrip("/").rsplit("/", 1)[-1])
        except (TypeError, ValueError):
            issue_number = None
        if issue_number in pull_request_numbers or comment.get("pull_request_url"):
            continue
        filtered_comments.append(comment)
    return [issue for issue in issues if not issue.get("pull_request")], filtered_comments


def _fingerprint(item: Dict[str, Any]) -> str:
    # Hash content without printing it; this catches edits even when a fixture's
    # updated_at is unchanged, while preserving only an in-memory dedup state.
    encoded = json.dumps(item, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _emit(prefix: str, action: str, item: Dict[str, Any], identifier: Any) -> None:
    fields = [
        prefix,
        "action={}".format(action),
        "id={}".format(identifier),
        "updated_at={}".format(item.get("updated_at", "unknown")),
    ]
    if prefix == "GITHUB_ISSUE_EVENT":
        fields.extend([
            "issue={}".format(item.get("number", identifier)),
            "state={}".format(item.get("state", "unknown")),
        ])
    else:
        fields.append("issue_url={}".format(item.get("issue_url", "unknown")))
    fields.append("url={}".format(item.get("html_url", item.get("url", "unknown"))))
    print(" ".join(fields), flush=True)


def _process_snapshot(
    issues: List[Dict[str, Any]],
    comments: List[Dict[str, Any]],
    seen_issues: Dict[Any, str],
    seen_comments: Dict[Any, str],
) -> None:
    for issue in issues:
        identifier = issue.get("id", issue.get("number"))
        if identifier is None:
            continue
        fingerprint = _fingerprint(issue)
        if identifier not in seen_issues:
            _emit("GITHUB_ISSUE_EVENT", "created", issue, identifier)
        elif seen_issues[identifier] != fingerprint:
            _emit("GITHUB_ISSUE_EVENT", "updated", issue, identifier)
        seen_issues[identifier] = fingerprint
    for comment in comments:
        identifier = comment.get("id")
        if identifier is None:
            continue
        fingerprint = _fingerprint(comment)
        if identifier not in seen_comments:
            _emit("GITHUB_COMMENT_EVENT", "created", comment, identifier)
        elif seen_comments[identifier] != fingerprint:
            _emit("GITHUB_COMMENT_EVENT", "updated", comment, identifier)
        seen_comments[identifier] = fingerprint
def _parse_args(argv: List[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Poll GitHub issue feedback")
    parser.add_argument("--interval", type=float, default=DEFAULT_INTERVAL,
                        help="seconds between polls (default: 30)")
    parser.add_argument("--once", action="store_true", help="fetch one snapshot and exit")
    args = parser.parse_args(argv)
    if not math.isfinite(args.interval) or args.interval <= 0:
        parser.error("--interval must be a finite positive number")
    return args


def main(argv: List[str] = None) -> int:
    args = _parse_args(sys.argv[1:] if argv is None else argv)
    seen_issues: Dict[Any, str] = {}
    seen_comments: Dict[Any, str] = {}
    stop_event = threading.Event()

    def stop(_signum: int, _frame: Any) -> None:
        stop_event.set()

    signal.signal(signal.SIGINT, stop)
    signal.signal(signal.SIGTERM, stop)
    first_success = False
    while not stop_event.is_set():
        try:
            issues, comments = fetch_snapshot()
        except FetchError as exc:
            print("GITHUB_POLLING_ERROR {}".format(str(exc).replace("\n", " ")),
                  file=sys.stderr, flush=True)
            if args.once:
                return 1
            stop_event.wait(args.interval)
            continue
        _process_snapshot(issues, comments, seen_issues, seen_comments)
        if not first_success:
            print("GITHUB_POLLING_STARTED repository={} interval={}s".format(
                REPOSITORY, args.interval), flush=True)
            first_success = True
        if args.once:
            print("GITHUB_POLLING_SNAPSHOT issues={} comments={}".format(
                len(issues), len(comments)), flush=True)
            return 0
        stop_event.wait(args.interval)
    return 0


if __name__ == "__main__":
    sys.exit(main())
