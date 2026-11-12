#!/usr/bin/env python3
"""
tools/project-notion.py — AAOS Phase 3 §4.2: build the local Derived
projection payload posted to the Notion Ops Hub by tools/publish-notion.py
(the call site Phase 3 §4.2/§6 deferred, now built).

This is a pure local-file compute script. It touches Notion not at all —
that's tools/publish-notion.py's job, kept separate so this script stays
testable without any network/token dependency. This script reads ONLY local seam files:
every tasks/<id>.json in the project, the NDJSON event log (latest event
per task, matched on refs.task), and heartbeat.json (absent -> every task's
heartbeat_status is "unknown").

CLI:
    python tools/project-notion.py [--project ora-ops] [--out <path>]
                                    [--now <iso>] [--seam-root <dir>]

Output shape (NOT one of the 5 contract schemas -- Derived, like
contracts/PUBLISHED.json, so no schema validation applies to it):

    {
      "generation": {
        "generated_at": "<now, iso>",
        "sources": {
          "tasks": {"path": "...", "file_count": N, "newest_mtime": iso|null},
          "events_log": {"path": "...", "exists": bool, "mtime": iso|null},
          "heartbeat": {"path": "...", "exists": bool, "mtime": iso|null}
        },
        "stale": bool,        # true iff the newest input file is >7 days
                               # older than `now` (firm staleness canon)
        "skipped": [{"file": "<name>.json", "reason": "<why>"}]
      },
      "rows": [
        {
          "task_id": "...", "title": "...", "state": "...",
          "owner_agent": "...", "priority": N,
          "lease": {"holder": "...", "expires": "..."} | null,
          "last_event": {"type": "...", "timestamp": "..."} | null,
          "heartbeat_status": "fresh" | "stale" | "missed" | "unknown"
        },
        ...
      ]
    }

A malformed task file (invalid JSON, or missing a field this script needs
to build its row) is skipped and recorded in generation.skipped with its
filename and the reason -- it never crashes the whole projection or is
silently dropped. heartbeat_status is looked up for the task's active
lease holder when a lease is present, else for its owner_agent; any lookup
that can't resolve to a known, fresh agent-in-heartbeat entry (heartbeat.json
absent, agent_id not present in it, or owner_agent == "unassigned" with no
lease) resolves to "unknown" -- fail-closed, never fabricated.

Exit codes:
    0   success, including an empty project (rows: [])
    2   usage error (e.g. the seam root cannot be resolved at all)
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

import _aaos_common as aaos  # noqa: E402  (import after sys.path manipulation, by design)

STALE_THRESHOLD = timedelta(days=7)
DEFAULT_PROJECT = "ora-ops"

REQUIRED_TASK_FIELDS = ("id", "title", "state", "owner_agent", "priority")


def _mtime_iso(path: Path):
    """Return the file's mtime as a tz-aware ISO string (UTC), or None if
    the path doesn't exist."""
    try:
        ts = path.stat().st_mtime
    except OSError:
        return None
    return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()


def _mtime_dt(path: Path):
    try:
        ts = path.stat().st_mtime
    except OSError:
        return None
    return datetime.fromtimestamp(ts, tz=timezone.utc)


def _load_tasks(tasks_dir: Path):
    """Return (rows_input, skipped, newest_mtime_dt, file_count).
    rows_input is a list of validated task dicts. skipped is a list of
    {"file", "reason"} for any task file that could not be used."""
    rows_input = []
    skipped = []
    newest = None
    file_count = 0

    if not tasks_dir.is_dir():
        return rows_input, skipped, newest, file_count

    for path in sorted(tasks_dir.glob("*.json")):
        file_count += 1
        mtime = _mtime_dt(path)
        if mtime is not None and (newest is None or mtime > newest):
            newest = mtime

        try:
            raw = path.read_text(encoding="utf-8")
        except OSError as exc:
            skipped.append({"file": path.name, "reason": f"could not read file: {exc}"})
            continue
        try:
            task = json.loads(raw)
        except json.JSONDecodeError as exc:
            skipped.append({"file": path.name, "reason": f"invalid JSON: {exc}"})
            continue
        if not isinstance(task, dict):
            skipped.append({"file": path.name, "reason": "top-level JSON value is not an object"})
            continue

        missing = [f for f in REQUIRED_TASK_FIELDS if f not in task]
        if missing:
            skipped.append(
                {"file": path.name, "reason": f"missing required field(s): {', '.join(missing)}"}
            )
            continue

        rows_input.append(task)

    return rows_input, skipped, newest, file_count


def _latest_event_per_task(project_id: str, root: str | None):
    """Return {task_id: {"type", "timestamp"}} for the newest event
    (by parsed timestamp) referencing each task_id, matched on refs.task.

    Reads via aaos.read_events (NDJSON + not-yet-drained pending events,
    deduped by event_id) rather than parsing the NDJSON file directly --
    see docs/PHASE3-DESIGN.md section 1.10 REVISED: a committed-but-still-
    pending event is a real event and must not be invisible to this
    projection just because no drain has folded it in yet. A malformed
    entry (missing/unparseable timestamp) is skipped -- it can't crash the
    projection."""
    latest: dict[str, tuple[datetime, dict]] = {}

    for event in aaos.read_events(project_id, root=root):
        if not isinstance(event, dict):
            continue
        task_id = event.get("refs", {}).get("task") if isinstance(event.get("refs"), dict) else None
        if not task_id:
            continue
        ts_raw = event.get("timestamp")
        if not isinstance(ts_raw, str):
            continue
        try:
            ts = aaos._parse_iso_dt(ts_raw)
        except aaos.CorruptStateError:
            continue

        current = latest.get(task_id)
        if current is None or ts > current[0]:
            latest[task_id] = (ts, {"type": event.get("type"), "timestamp": ts_raw})

    return {task_id: entry for task_id, (_, entry) in latest.items()}


def _heartbeat_status_lookup(heartbeat_doc):
    """Return a callable agent_id -> status, "unknown" if unresolved."""
    statuses: dict[str, str] = {}
    if isinstance(heartbeat_doc, dict):
        for agent in heartbeat_doc.get("agents", []):
            if isinstance(agent, dict) and isinstance(agent.get("agent_id"), str):
                statuses[agent["agent_id"]] = agent.get("status", "unknown")

    def _lookup(agent_id):
        if not agent_id or agent_id == "unassigned":
            return "unknown"
        return statuses.get(agent_id, "unknown")

    return _lookup


def _build_rows(tasks_input, last_events, heartbeat_lookup):
    rows = []
    for task in tasks_input:
        lease = task.get("lease")
        lease_out = None
        holder = None
        if isinstance(lease, dict):
            holder = lease.get("holder")
            lease_out = {"holder": holder, "expires": lease.get("expires")}

        effective_agent = holder if holder else task.get("owner_agent")

        rows.append({
            "task_id": task["id"],
            "title": task.get("title"),
            "state": task.get("state"),
            "owner_agent": task.get("owner_agent"),
            "priority": task.get("priority"),
            "lease": lease_out,
            "last_event": last_events.get(task["id"]),
            "heartbeat_status": heartbeat_lookup(effective_agent),
        })
    return rows


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="project-notion.py",
        description="Build the local Derived Notion-projection payload for a project "
        "(pure local-file compute -- never touches Notion itself).",
    )
    parser.add_argument("--project", default=DEFAULT_PROJECT, help=f"Project id (default: {DEFAULT_PROJECT}).")
    parser.add_argument(
        "--out", default=None,
        help="Output path (default: <seam-root>/projects/<project>/notion-projection.json).",
    )
    parser.add_argument(
        "--now", default=None,
        help="ISO-8601 timestamp to use as 'now' (test hook; default: real clock).",
    )
    parser.add_argument(
        "--seam-root", default=None,
        help="Override the seam root directory (default: $AAOS_SEAM_ROOT or the "
        "built-in default).",
    )
    return parser


def main(argv=None) -> int:
    args = build_arg_parser().parse_args(argv)

    # Freeze sentinel (ADR-0007 addendum 2026-11-12): every file-path argument, not only the seam
    # root, is refused if it is or lies under the live seam -- before any read or write.
    try:
        aaos.refuse_frozen_live_paths([(args.out, "--out")])
    except aaos.UsageError as exc:
        print(f"USAGE ERROR: {exc}")
        return 2

    if args.now:
        try:
            now = aaos._parse_iso_dt(args.now)
        except aaos.CorruptStateError as exc:
            print(f"USAGE ERROR: invalid --now value: {exc}")
            return 2
    else:
        now = aaos._now(None)

    try:
        # Gate-5 review Minor #3: every input this tool reads (tasks/,
        # events log, heartbeat.json) is seam-rooted regardless of --out,
        # so a phantom seam must fail closed here, not auto-vivify an
        # empty (and misleadingly stale:false) projection at exit 0.
        aaos.require_seam_reachable(args.seam_root)
        project_dir = aaos.project_dir(args.project, root=args.seam_root)
    except aaos.UsageError as exc:
        print(f"USAGE ERROR: {exc}")
        return 2

    tasks_dir = project_dir / "tasks"
    events_path = aaos.events_log_path(args.project, root=args.seam_root)
    hb_path = aaos.heartbeat_path(root=args.seam_root)

    tasks_input, skipped, newest_task_mtime, task_file_count = _load_tasks(tasks_dir)
    last_events = _latest_event_per_task(args.project, args.seam_root)
    heartbeat_doc = aaos.load_heartbeat(root=args.seam_root)
    heartbeat_lookup = _heartbeat_status_lookup(heartbeat_doc)

    rows = _build_rows(tasks_input, last_events, heartbeat_lookup)

    events_mtime_dt = _mtime_dt(events_path)
    heartbeat_mtime_dt = _mtime_dt(hb_path)

    newest_overall = None
    for candidate in (newest_task_mtime, events_mtime_dt, heartbeat_mtime_dt):
        if candidate is not None and (newest_overall is None or candidate > newest_overall):
            newest_overall = candidate

    stale = bool(newest_overall is not None and (now - newest_overall) > STALE_THRESHOLD)

    generation = {
        "generated_at": now.isoformat(),
        "sources": {
            "tasks": {
                "path": str(tasks_dir),
                "file_count": task_file_count,
                "newest_mtime": newest_task_mtime.isoformat() if newest_task_mtime else None,
            },
            "events_log": {
                "path": str(events_path),
                "exists": events_path.exists(),
                "mtime": events_mtime_dt.isoformat() if events_mtime_dt else None,
            },
            "heartbeat": {
                "path": str(hb_path),
                "exists": hb_path.exists(),
                "mtime": heartbeat_mtime_dt.isoformat() if heartbeat_mtime_dt else None,
            },
        },
        "stale": stale,
        "skipped": skipped,
    }

    payload = {"generation": generation, "rows": rows}

    out_path = Path(args.out) if args.out else (project_dir / "notion-projection.json")
    aaos.save_json(out_path, payload)
    print(str(out_path))
    return 0


if __name__ == "__main__":
    sys.exit(main())
