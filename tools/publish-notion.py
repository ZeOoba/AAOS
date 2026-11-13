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
                                    performs (only with --live) or prints
                                    without performing the actual POST.

FAIL-CLOSED DEFAULTS (docs/adr/0007-freeze.md, addendum 2026-11-12):
    * Dry-run is the default. A live POST needs ALL of: --live (live=True),
      AAOS_NOTION_TOKEN, AAOS_HASH_KEY (stripped; >= 16 non-whitespace
      chars) and -- while AAOS is
      frozen -- AAOS_UNFREEZE=1. publish() reads the token and key from the
      environment itself (there is no token parameter), and the generic
      NOTION_TOKEN is never read, so neither an ambient token nor a caller
      can turn a call into egress.
    * task_id and title are always replaced by an HMAC-SHA256 tag
      ("hmac:<16 hex>") keyed with AAOS_HASH_KEY; a dry run without the key
      uses an ephemeral random key. owner_agent passes verbatim only if it is
      a registered agent id or "unassigned"; state, heartbeat_status and
      priority only if they are schema values; anything else is tagged too.
      project must match PROJECT_ID_RE and appears in the page title verbatim
      only if it is in KNOWN_PROJECTS (else tagged), and generated_at / now must be
      tz-aware ISO-8601, or the call is refused (exit 2). There is no
      verbatim mode. The confidential-roster screen (A4a) is still unbuilt.
    * --in (and publish()'s payload_path) is refused if it is or lies under
      the live seam, like every other file argument while AAOS is frozen.

CLI:
    python tools/publish-notion.py [--project ora-ops] [--in <path>]
                                    [--parent-page-id <id>]
                                    [--dry-run | --live] [--now <iso>]

Env:
    AAOS_NOTION_TOKEN -- Notion internal integration token (required only
                      with --live; .strip()'d per the trailing-newline-secret
                      gotcha that produced an InvalidHeader 500 in
                      ora-cowork-watchdog on 2026-06-11)
    AAOS_HASH_KEY  -- HMAC key for the redacted cells (required with --live)
    AAOS_UNFREEZE  -- must be exactly "1" for --live while ADR-0007 stands

Exit codes:
    0   published successfully, or (default / --dry-run) printed the
        request body without sending it
    1   Notion API call failed (non-2xx response or network error) --
        the publish itself was attempted and did not succeed
    2   usage error (payload missing/malformed or under the live seam, bad
        --project/--now/generated_at, --live while frozen or without
        AAOS_NOTION_TOKEN/AAOS_HASH_KEY, --parent-page-id malformed)
"""
from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import os
import secrets
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
COLUMN_HEADERS = ("Task ID (hmac)", "Title (hmac)", "State", "Owner", "Priority", "Heartbeat")
# Free text that may name a confidential deal: always tagged, never published verbatim.
HASHED_COLUMNS = frozenset({"task_id", "title"})
TOKEN_ENV = "AAOS_NOTION_TOKEN"
HASH_KEY_ENV = "AAOS_HASH_KEY"
# Project ids that may appear verbatim in the page title; any other id is HMAC-tagged.
KNOWN_PROJECTS = frozenset({"ora-ops"})
MIN_HASH_KEY_LEN = 16
TAG_HEX = 16


def _task_states() -> frozenset:
    try:
        schema = json.loads(aaos.g_read_text(aaos.SCHEMA_DIR / "task.schema.json"))
        return frozenset(schema["properties"]["state"]["enum"])
    except (OSError, ValueError, KeyError, TypeError):
        return frozenset()  # fail closed: every state gets tagged


# Columns that pass verbatim only when their value is in the allowlist; anything else is tagged.
ALLOWED_VALUES = {
    "state": _task_states(),
    "heartbeat_status": frozenset({"fresh", "stale", "missed", "unknown"}),
    "priority": frozenset({1, 2, 3}),
}


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


def _tag(value, key: bytes) -> str | None:
    """Free-text cell -> "hmac:<16 hex>" keyed with `key`: stable for one key, so rows correlate
    across pages, but not confirmable by hashing a guessed deal name without the key.
    None/empty stays empty."""
    if value is None or value == "":
        return None
    return "hmac:" + hmac.new(key, str(value).encode("utf-8"), hashlib.sha256).hexdigest()[:TAG_HEX]


def _table_row(values) -> dict:
    return {"type": "table_row", "table_row": {"cells": [_cell(v) for v in values]}}


def _iso(value, what: str) -> str:
    try:
        return aaos._parse_iso_dt(value).isoformat()
    except aaos.CorruptStateError as exc:
        raise aaos.UsageError(f"{what} must be a tz-aware ISO-8601 datetime: {exc}") from exc


def _check_project(project) -> str:
    if not isinstance(project, str) or not aaos.PROJECT_ID_RE.fullmatch(project):
        raise aaos.UsageError(f"invalid project id {project!r} (must match {aaos.PROJECT_ID_RE.pattern!r})")
    return project


def _cell_value(col: str, value, key: bytes, known_agents: frozenset):
    if col in HASHED_COLUMNS:
        return _tag(value, key)
    if value is None:
        return None
    if col == "owner_agent":
        return value if (isinstance(value, str) and (value in known_agents or value == "unassigned")) else _tag(value, key)
    try:
        ok = not isinstance(value, bool) and value in ALLOWED_VALUES[col]
    except TypeError:  # unhashable (list/dict) -- not a schema value
        ok = False
    return value if ok else _tag(value, key)


def build_notion_page_payload(
    payload: dict,
    project: str,
    parent_page_id: str,
    now_iso: str,
    *,
    hash_key: bytes,
    known_agents: frozenset = frozenset(),
) -> dict:
    """PURE: project-notion.py's projection payload -> the Notion
    `POST /v1/pages` request body. No network call, no clock read.

    Raises aaos.UsageError if `payload` is missing the `generation`/`rows`
    keys project-notion.py always emits, if `project` is not a valid project
    id, or if `now_iso`/`generation.generated_at` is not tz-aware ISO-8601 --
    a malformed payload must never be silently published. Cells are redacted
    per the module docstring; `known_agents` defaults to empty, so an
    owner_agent is tagged unless the caller supplies the registry ids."""
    if not isinstance(payload, dict) or "generation" not in payload or "rows" not in payload:
        raise aaos.UsageError(
            "projection payload is missing required 'generation'/'rows' keys "
            "-- is this actually project-notion.py output?"
        )
    if not isinstance(hash_key, (bytes, bytearray)) or len(hash_key) < MIN_HASH_KEY_LEN:
        raise aaos.UsageError(f"hash_key must be at least {MIN_HASH_KEY_LEN} bytes")

    generation = payload["generation"]
    rows = payload["rows"]
    if not isinstance(generation, dict):
        raise aaos.UsageError("projection payload 'generation' is not an object")
    if not isinstance(rows, list):
        raise aaos.UsageError("projection payload 'rows' is not a list")

    project = _check_project(project)
    now_text = _iso(now_iso, "now")
    shown_project = project if project in KNOWN_PROJECTS else _tag(project, bytes(hash_key))
    title_text = f"AAOS Queue Status — {shown_project} — {now_text}"

    stale = bool(generation.get("stale"))
    skipped = generation.get("skipped") or []
    generated_at = (
        _iso(generation["generated_at"], "generation.generated_at")
        if "generated_at" in generation else "unknown"
    )

    summary_lines = [f"Generated: {generated_at}", f"Tasks: {len(rows)}"]
    if stale:
        summary_lines.append("⚠ STALE — newest input file is more than 7 days old.")
    if skipped:
        summary_lines.append(f"⚠ {len(skipped) if isinstance(skipped, list) else 1} file(s) skipped during projection (see generation.skipped).")

    table_rows = [_table_row(COLUMN_HEADERS)]
    for row in rows:
        if not isinstance(row, dict):
            raise aaos.UsageError("projection payload row is not an object")
        table_rows.append(_table_row(
            _cell_value(col, row.get(col), bytes(hash_key), known_agents) for col in ROW_COLUMNS
        ))

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


def _registered_agents() -> frozenset:
    try:
        registry = aaos.load_registry()
        return frozenset(a["id"] for a in registry.get("agents", []) if isinstance(a.get("id"), str))
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        return frozenset()  # fail closed: every owner gets tagged


def publish(
    payload_path: Path,
    *,
    project: str,
    parent_page_id: str,
    live: bool = False,
    now_iso: str,
) -> dict:
    """Read the projection payload, build the Notion request body, and (only if live) POST it.
    Returns the request body in all cases so dry-run callers can print it.

    Credentials come from the environment only (AAOS_NOTION_TOKEN, AAOS_HASH_KEY); there is no
    token parameter. Every live gate -- freeze sentinel, token, hash key -- is checked here, before
    the payload is read, so a programmatic caller is refused exactly like the CLI. Raises
    aaos.UsageError for anything wrong before the network call and PublishError for anything
    wrong during or after it."""
    _check_project(project)
    aaos.refuse_frozen_live_path(payload_path, "payload path (--in)")

    token = None
    # Surrounding whitespace is not key material; the key needs MIN_HASH_KEY_LEN non-whitespace chars.
    key_text = os.environ.get(HASH_KEY_ENV, "").strip()
    key_ok = sum(1 for ch in key_text if not ch.isspace()) >= MIN_HASH_KEY_LEN
    if live:
        if not aaos.unfreeze_enabled():
            raise aaos.UsageError(
                "AAOS is FROZEN (docs/adr/0007-freeze.md): --live publishing to Notion is refused. "
                f"Only a revival ADR may set {aaos.UNFREEZE_ENV}=1; dry-run (the default) still works."
            )
        token = os.environ.get(TOKEN_ENV, "").strip() or None
        if not token:
            raise aaos.UsageError(f"{TOKEN_ENV} is required (unset) for --live; NOTION_TOKEN is not read")
        if not key_ok:
            raise aaos.UsageError(
                f"{HASH_KEY_ENV} (at least {MIN_HASH_KEY_LEN} non-whitespace characters) is required for --live"
            )
    hash_key = key_text.encode("utf-8") if key_ok else secrets.token_bytes(32)

    if not payload_path.exists():
        raise aaos.UsageError(
            f"projection payload not found: {payload_path} "
            f"-- run tools/project-notion.py --project {project} first"
        )
    try:
        payload = aaos.load_json(payload_path)
    except aaos.CorruptStateError as exc:
        raise aaos.UsageError(f"projection payload is not valid JSON: {exc}") from exc

    body = build_notion_page_payload(
        payload, project, parent_page_id, now_iso, hash_key=hash_key, known_agents=_registered_agents()
    )

    if not live:
        return body

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
        "child page under the Notion Ops Hub (dry-run unless --live).",
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
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--dry-run", action="store_true",
        help="Build and print the Notion request body without sending it. This is the default; "
        "the flag is kept for compatibility.",
    )
    mode.add_argument(
        "--live", action="store_true",
        help=f"Actually POST to Notion. Requires {TOKEN_ENV}, {HASH_KEY_ENV} and, while AAOS is "
        "frozen (ADR-0007), AAOS_UNFREEZE=1.",
    )
    parser.add_argument(
        "--now", default=None,
        help="ISO-8601 timestamp for the page title (test hook; default: real clock).",
    )
    return parser


@aaos.freeze_exit
def main(argv=None) -> int:
    args = build_arg_parser().parse_args(argv)

    try:
        # Freeze sentinel (ADR-0007 addendum 2026-11-12): file arguments too, before any I/O.
        aaos.refuse_frozen_live_paths([(args.in_path, "--in")])
        _check_project(args.project)
        parent_page_id = _normalize_page_id(args.parent_page_id)
        if args.in_path:
            payload_path = Path(args.in_path)
        else:
            payload_path = aaos.project_dir(args.project, root=args.seam_root) / "notion-projection.json"
        now_iso = aaos.parse_cli_now(args.now).isoformat()
        body = publish(
            payload_path,
            project=args.project,
            parent_page_id=parent_page_id,
            live=args.live,
            now_iso=now_iso,
        )
    except aaos.UsageError as exc:
        print(f"USAGE ERROR: {exc}", file=sys.stderr)
        return 2
    except PublishError as exc:
        print(f"PUBLISH FAILED: {exc}", file=sys.stderr)
        return 1

    if not args.live:
        print(json.dumps(body, indent=2, ensure_ascii=False))
    else:
        print(f"PUBLISHED: Ops Hub child page under {parent_page_id} ({args.project})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
