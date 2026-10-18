#!/usr/bin/env python3
"""
tools/publish-notion.py — the "future agent-session call site" that
docs/PHASE3-DESIGN.md section 4.2/6 deferred: publishes the Derived
projection payload project-notion.py already computes and writes locally
(notion-projection.json) as a dated child page under the Notion Ops Hub
(page id 32b6ecd8-9e59-81f2-b329-df3fd0e25062 -- see memory/tech.md),
following the EXISTING Ops Hub child-page convention (Daily Brief,
Automation Health, Outbox QC, EOD Action Ledger, ...) per
docs/CAPABILITY-MAP.md item 3: "REUSE -- target the existing Ops Hub
child-page convention ... do not create a parallel Notion database."

This script is READ-ONLY against the local projection payload and
WRITE-ONLY against Notion. It never touches tasks/, locks/, or the event
log -- Notion stays exactly what ADR-0001 requires: a Derived mirror, not
a source of truth, and a publish failure here must never be treated as a
queue failure by any caller.

Stdlib-only HTTP (urllib.request/urllib.error), matching AAOS's existing
"Python 3, stdlib + one external dep (jsonschema)" design -- this script
adds no new dependency.

Design split (same house style as reconcile-noi.ts's classifier/wrapper
split and publish-contracts.py's plan/execute split):
    build_notion_page_payload()  -- PURE: projection dict -> Notion API
                                    request body. No network, no clock
                                    read (now_iso is a parameter).
    publish()                     -- thin IO wrapper: reads the payload
                                    file, calls the pure builder, then
                                    performs (or, with --dry-run, prints
                                    without performing) the actual POST.

CLI:
    python tools/publish-notion.py [--project ora-ops] [--in <path>]
                                    [--parent-page-id <id>] [--dry-run]
                                    [--now <iso>]

Env:
    NOTION_TOKEN   -- Notion internal integration token (required unless
                      --dry-run; .strip()'d per the trailing-newline-secret
                      gotcha that produced an InvalidHeader 500 in
                      ora-cowork-watchdog on 2026-06-11 -- same failure
                      mode, same fix, applied here preemptively)

Exit codes:
    0   published successfully, or --dry-run printed the request body
        without sending it
    1   Notion API call failed (non-2xx response or network error) --
        the publish itself was attempted and did not succeed
    2   usage error (payload file missing/malformed, NOTION_TOKEN unset
        and --dry-run not given, --parent-page-id malformed)
"""
from __future__ import annotations

import argparse
import json
import sys
import urllib.error
import urllib.request
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

import _aaos_common as aaos  # noqa: E402  (import after sys.path manipulation, by design)

NOTION_API = "https://api.notion.com/v1/pages"
NOTION_VERSION = "2022-06-28"

# Operations Hub (parent) page -- memory/tech.md, dashed per the
# reference_notion_query_requires_dashed_uuid gotcha (Notion's API rejects
# a bare 32-hex-char id on some endpoints; dashed is always accepted).
DEFAULT_PARENT_PAGE_ID = "32b6ecd8-9e59-81f2-b329-df3fd0e25062"

ROW_COLUMNS = ("task_id", "title", "state", "owner_agent", "priority", "heartbeat_status")
COLUMN_HEADERS = ("Task ID", "Title", "State", "Owner", "Priority", "Heartbeat")


class PublishError(aaos.AaosError):
    """Notion API call was attempted and did not succeed (network error or
    non-2xx response). Distinct from aaos.UsageError, which means the
    call was never attempted."""


def _normalize_page_id(raw: str) -> str:
    """Accept either a dashed or bare 32-hex-char page id; always return
    dashed (some Notion API surfaces are picky about the dashed form --
    see memory/reference_notion_query_requires_dashed_uuid.md)."""
    s = raw.replace("-", "").strip().lower()
    if len(s) != 32 or not all(c in "0123456789abcdef" for c in s):
        raise aaos.UsageError(f"--parent-page-id must be a 32-hex-char Notion id, got {raw!r}")
    return f"{s[0:8]}-{s[8:12]}-{s[12:16]}-{s[16:20]}-{s[20:32]}"


def _cell(value) -> list[dict]:
    text = "" if value is None else str(value)
    return [{"type": "text", "text": {"content": text}}]


def _table_row(values) -> dict:
    return {"type": "table_row", "table_row": {"cells": [_cell(v) for v in values]}}


def build_notion_page_payload(payload: dict, project: str, parent_page_id: str, now_iso: str) -> dict:
    """PURE: project-notion.py's projection payload -> the Notion
    `POST /v1/pages` request body. No network call, no clock read.

    Raises aaos.UsageError if `payload` is missing the `generation`/`rows`
    keys project-notion.py always emits -- a malformed payload must never
    be silently published as an empty/misleading page."""
    if not isinstance(payload, dict) or "generation" not in payload or "rows" not in payload:
        raise aaos.UsageError(
            "projection payload is missing required 'generation'/'rows' keys "
            "-- is this actually project-notion.py output?"
        )

    generation = payload["generation"]
    rows = payload["rows"]
    if not isinstance(rows, list):
        raise aaos.UsageError("projection payload 'rows' is not a list")

    title_text = f"AAOS Queue Status — {project} — {now_iso}"

    stale = bool(generation.get("stale"))
    skipped = generation.get("skipped") or []
    generated_at = generation.get("generated_at", "unknown")

    summary_lines = [f"Generated: {generated_at}", f"Tasks: {len(rows)}"]
    if stale:
        summary_lines.append("⚠ STALE — newest input file is more than 7 days old.")
    if skipped:
        summary_lines.append(f"⚠ {len(skipped)} file(s) skipped during projection (see generation.skipped).")

    table_rows = [_table_row(COLUMN_HEADERS)]
    for row in rows:
        table_rows.append(_table_row(row.get(col) for col in ROW_COLUMNS))

    children = [
        {
            "type": "paragraph",
            "paragraph": {"rich_text": [{"type": "text", "text": {"content": line}}]},
        }
        for line in summary_lines
    ]
    children.append(
        {
            "type": "table",
            "table": {
                "table_width": len(ROW_COLUMNS),
                "has_column_header": True,
                "has_row_header": False,
                "children": table_rows,
            },
        }
    )

    return {
        "parent": {"page_id": parent_page_id},
        "properties": {
            "title": {"title": [{"type": "text", "text": {"content": title_text}}]},
        },
        "children": children,
    }


def publish(
    payload_path: Path,
    *,
    project: str,
    parent_page_id: str,
    token: str | None,
    dry_run: bool,
    now_iso: str,
) -> dict:
    """Read the projection payload, build the Notion request body, and
    (unless dry_run) POST it. Returns the request body in all cases so
    --dry-run callers can print it. Raises aaos.UsageError for anything
    wrong before the network call, PublishError for anything wrong during
    or after it."""
    if not payload_path.exists():
        raise aaos.UsageError(
            f"projection payload not found: {payload_path} "
            f"-- run tools/project-notion.py --project {project} first"
        )
    try:
        payload = aaos.load_json(payload_path)
    except aaos.CorruptStateError as exc:
        raise aaos.UsageError(f"projection payload is not valid JSON: {exc}") from exc

    body = build_notion_page_payload(payload, project, parent_page_id, now_iso)

    if dry_run:
        return body

    if not token:
        raise aaos.UsageError("NOTION_TOKEN is required (unset) unless --dry-run is given")

    request = urllib.request.Request(
        NOTION_API,
        data=json.dumps(body).encode("utf-8"),
        method="POST",
        headers={
            "Authorization": f"Bearer {token}",
            "Notion-Version": NOTION_VERSION,
            "Content-Type": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as resp:
            resp.read()
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")[:500]
        raise PublishError(f"Notion API returned {exc.code}: {detail}") from exc
    except urllib.error.URLError as exc:
        raise PublishError(f"Notion API call failed: {exc.reason}") from exc

    return body


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="publish-notion.py",
        description="Publish the local Notion projection payload as a dated "
        "child page under the Notion Ops Hub.",
    )
    parser.add_argument("--project", default="ora-ops", help="Project id (default: ora-ops).")
    parser.add_argument(
        "--in", dest="in_path", default=None,
        help="Path to the projection payload (default: <seam-root>/projects/<project>/notion-projection.json, "
        "matching project-notion.py's own default --out).",
    )
    parser.add_argument(
        "--parent-page-id", default=DEFAULT_PARENT_PAGE_ID,
        help=f"Notion Ops Hub parent page id (default: {DEFAULT_PARENT_PAGE_ID}).",
    )
    parser.add_argument(
        "--seam-root", default=None,
        help="Seam root override, used only to derive the default --in path.",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Build and print the Notion request body without sending it (no NOTION_TOKEN needed).",
    )
    parser.add_argument(
        "--now", default=None,
        help="ISO-8601 timestamp for the page title (test hook; default: real clock).",
    )
    return parser


def main(argv=None) -> int:
    import os

    args = build_arg_parser().parse_args(argv)

    try:
        parent_page_id = _normalize_page_id(args.parent_page_id)
    except aaos.UsageError as exc:
        print(f"USAGE ERROR: {exc}", file=sys.stderr)
        return 2

    if args.in_path:
        payload_path = Path(args.in_path)
    else:
        try:
            project_dir = aaos.project_dir(args.project, root=args.seam_root)
        except aaos.UsageError as exc:
            print(f"USAGE ERROR: {exc}", file=sys.stderr)
            return 2
        payload_path = project_dir / "notion-projection.json"

    try:
        now_iso = aaos.parse_cli_now(args.now).isoformat()
    except aaos.UsageError as exc:
        print(f"USAGE ERROR: {exc}", file=sys.stderr)
        return 2

    token = os.environ.get("NOTION_TOKEN", "").strip() or None

    try:
        body = publish(
            payload_path,
            project=args.project,
            parent_page_id=parent_page_id,
            token=token,
            dry_run=args.dry_run,
            now_iso=now_iso,
        )
    except aaos.UsageError as exc:
        print(f"USAGE ERROR: {exc}", file=sys.stderr)
        return 2
    except PublishError as exc:
        print(f"PUBLISH FAILED: {exc}", file=sys.stderr)
        return 1

    if args.dry_run:
        print(json.dumps(body, indent=2, ensure_ascii=False))
    else:
        print(f"PUBLISHED: Ops Hub child page under {parent_page_id} ({args.project})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
