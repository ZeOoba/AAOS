#!/usr/bin/env python3
"""
tools/expire-leases.py — sweep locks/ and reclaim stale AAOS leases.

Usage:
    python tools/expire-leases.py [--dry-run] [--skew-min 5]
        [--project <id>] [--seam-root <path>] [--now <iso>]

See docs/PHASE3-DESIGN.md sections 1.9 and 2.6. --dry-run prints what
would be reclaimed with zero writes/archives/events, exit 0. Otherwise:
drain_event_spool first; for each lock file: unparseable -> emit
Escalated/CorruptState marker, skip, never steal. Else reclaim_decision
(hard-expiry-primary, heartbeat classifies only -- see
_aaos_common.reclaim_decision's docstring / PHASE3-DESIGN.md section
1.9). Reclaimable + not dry-run: stage task state="queued" (drop lease)
-> emit LeaseExpired -> rename -> archive_lock. classification ==
"DeadAgent" additionally emits Escalated.

This is the script staged (not registered) as the candidate scheduled
task -- fleet registration is deferred to Phase 4/5 supervised
create_scheduled_task, per docs/PHASE3-DESIGN.md section 2.6 and the
master prompt's Gate 3 exit criteria.

Lock identity note: each lock file's own `task_ref` field (written by
acquire_lease's lock_dict, see _aaos_common.py section 1.7) is used to
resolve which task a lock belongs to, rather than parsing the filename.
This correctly handles the canonical <task_id>.lock case; it is also
strictly safer than filename-parsing for any OneDrive conflict-copy-shaped
stray file (<id>-<HOST>.lock etc.) that might persist after a CONTESTED
race, since a lock whose task_ref doesn't resolve to a loadable task is
caught by the same fail-closed CorruptState path rather than silently
mis-attributed to the wrong task id.

Exit codes: see docs/PHASE3-DESIGN.md section 0.4. This is a best-effort
per-lock sweep; it returns 0 unless a genuine systemic failure (schema
validation on a reclaim, or an event-commit failure) propagates.
    0 success (sweep completed; dry-run always 0)
    1 schema/validation error on a reclaimed task
    5 event-commit failure
"""
from __future__ import annotations

import argparse
import sys
from datetime import datetime

import _aaos_common as aaos


def _add_common_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--project", default="ora-ops")
    parser.add_argument("--seam-root", default=None)
    parser.add_argument("--now", default=None, help="ISO-8601 timestamp (test hook).")


def _resolve_now(now_str):
    if now_str:
        try:
            return datetime.fromisoformat(now_str)
        except ValueError as exc:
            raise aaos.UsageError(f"--now: unparseable ISO-8601 datetime {now_str!r}: {exc}") from exc
    return datetime.now().astimezone()


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="expire-leases.py", description="Sweep locks/ and reclaim stale AAOS leases."
    )
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--skew-min", type=int, default=5)
    _add_common_args(parser)
    return parser


def _heartbeat_status_for(holder, heartbeat):
    if not heartbeat:
        return None
    for entry in heartbeat.get("agents", []):
        if entry.get("agent_id") == holder:
            return entry.get("status")
    return None


def _escalate_corrupt(project, root, resolved_now, entry, dry_run):
    if dry_run:
        print(f"[dry-run] would flag corrupt lock: {entry}")
        return
    aaos.emit_event(
        type="Escalated", actor="system", project_id=project,
        refs={"lock": str(entry)}, detail="CorruptState",
        now=resolved_now, root=root,
    )
    print(f"CORRUPT LOCK: {entry} -> Escalated, skipped (not reclaimed)")


def main(argv=None) -> int:
    args = build_arg_parser().parse_args(argv)
    resolved_now = _resolve_now(args.now)

    try:
        aaos.drain_event_spool(args.project, root=args.seam_root)

        locks_dir = aaos.project_dir(args.project, root=args.seam_root) / "locks"
        if not locks_dir.is_dir():
            print("no locks directory; nothing to sweep")
            return 0

        heartbeat = aaos.load_heartbeat(root=args.seam_root)

        for entry in sorted(p for p in locks_dir.iterdir() if p.is_file() and ".tmp-" not in p.name):
            try:
                lock = aaos.load_json(entry)
            except aaos.AaosError:
                _escalate_corrupt(args.project, args.seam_root, resolved_now, entry, args.dry_run)
                continue

            task_id = lock.get("task_ref") if isinstance(lock, dict) else None
            if not isinstance(task_id, str) or not task_id:
                _escalate_corrupt(args.project, args.seam_root, resolved_now, entry, args.dry_run)
                continue

            hb_status = _heartbeat_status_for(lock.get("holder"), heartbeat)
            decision = aaos.reclaim_decision(lock, hb_status, now=resolved_now, skew_minutes=args.skew_min)
            if not decision.reclaimable:
                continue

            if args.dry_run:
                print(
                    f"[dry-run] would reclaim {task_id} "
                    f"(lock={entry.name}, classification={decision.classification})"
                )
                continue

            try:
                task = aaos.load_task(task_id, args.project, root=args.seam_root)
            except aaos.AaosError:
                _escalate_corrupt(args.project, args.seam_root, resolved_now, entry, args.dry_run)
                continue

            new_task = dict(task)
            new_task["state"] = "queued"
            new_task.pop("lease", None)
            new_task["updated_at"] = resolved_now.isoformat()

            errors = aaos.validate_obj(new_task, "task")
            if errors:
                raise aaos.CorruptStateError(f"reclaimed task {task_id} failed schema validation: {errors}")

            aaos.commit_state_change(
                new_task, aaos.task_path(task_id, args.project, root=args.seam_root),
                {
                    "type": "LeaseExpired", "actor": "system", "project_id": args.project,
                    "refs": {"task": task_id, "lock": str(entry)}, "now": resolved_now,
                },
                root=args.seam_root,
            )
            aaos.archive_lock(entry, now=resolved_now)
            print(f"RECLAIMED: {task_id} (lock={entry.name}, classification={decision.classification})")

            if decision.classification == "DeadAgent":
                aaos.emit_event(
                    type="Escalated", actor="system", project_id=args.project,
                    refs={"task": task_id, "lock": str(entry)}, detail="DeadAgent",
                    now=resolved_now, root=args.seam_root,
                )
                print(f"ESCALATED: {task_id} DeadAgent")

        return 0

    except aaos.UsageError as exc:
        print(f"USAGE ERROR: {exc}", file=sys.stderr)
        return 2
    except aaos.CorruptStateError as exc:
        print(f"VALIDATION FAILED: {exc}", file=sys.stderr)
        return 1
    except aaos.StateConflictError as exc:
        print(f"STATE CONFLICT: {exc}", file=sys.stderr)
        return 3
    except aaos.LeaseLostError as exc:
        print(f"LEASE LOST: {exc}", file=sys.stderr)
        return 4
    except aaos.EventCommitError as exc:
        print(f"EVENT COMMIT FAILURE: {exc}", file=sys.stderr)
        return 5


if __name__ == "__main__":
    sys.exit(main())
