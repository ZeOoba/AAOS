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
"DeadAgent" additionally emits Escalated. A reclaimable (expired) lock
whose task is NOT leased/in-progress is an orphan: archived (only if its
content is unchanged since it was read), reported as ORPHAN LOCK, no event.

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
    2 usage error (Phase 5, scenario #4: seam root itself unreachable --
      see aaos.require_seam_reachable; previously this silently returned 0
      "no locks directory; nothing to sweep" even when the whole seam was
      gone, masking a broken seam as a clean empty sweep)
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
        aaos.require_seam_reachable(args.seam_root)
        if not args.dry_run:
            # Gated behind dry-run (Finding 4, adversarial review): draining
            # is a real write (folds spooled events into the NDJSON) --
            # unconditional drain violated --dry-run's "zero writes" promise
            # whenever a prior failure had left events spooled.
            aaos.drain_event_spool(args.project, root=args.seam_root, now=resolved_now)

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

            # Pre-check (Finding 3a, adversarial review): reclaim_decision
            # was computed from the LOCK file alone. Between then and this
            # load, the task's lease can legitimately have gone away for
            # more than one reason -- a fully-concurrent sweep already won
            # the race and reclaimed it (task -> queued, lease dropped), or
            # post-handoff.py transitioned it to "blocked" (which also
            # drops the lease, per task.schema.json's "lease present iff
            # leased/in-progress" invariant). Either way, proceeding here
            # would emit a redundant/incorrect LeaseExpired for a lease
            # that's already gone -- skip silently: no event, no state
            # churn. Don't assert which of the (possibly several) causes
            # applies; we haven't verified that, only that the lease itself
            # is no longer live.
            if task.get("state") not in ("leased", "in-progress") or "lease" not in task:
                # F02: the lock we read is expired past grace (reclaimable
                # above) yet its task holds no lease -- e.g. a lease-task that
                # crashed between creating the lock and committing the task.
                # Left in place it DENIES the task to every caller until a
                # human intervenes. Archive it, but only if it is still the
                # exact lock we judged: a lease-task may have replaced it with
                # a fresh live lock since (archive_lock_if_unchanged).
                archived = aaos.archive_lock_if_unchanged(entry, lock, now=resolved_now)
                if archived is not None:
                    print(
                        f"ORPHAN LOCK: {entry.name} expired but task {task_id} holds no lease "
                        f"(state={task.get('state')!r}); archived to {archived.name}, no event emitted"
                    )
                    continue
                print(
                    f"SKIP: {task_id} lease no longer live (state={task.get('state')!r}); "
                    "reclaimed or transitioned by another operation since this lock was read"
                )
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
            try:
                aaos.archive_lock(entry, now=resolved_now)
            except OSError as exc:
                # Finding 3b/3c (adversarial review): the reclaim transaction
                # above already committed (task -> queued, LeaseExpired
                # emitted) -- that's the correctness-bearing step and it's
                # done. If a concurrent sweep (or anything else) already
                # moved/removed this exact lock file before we got here,
                # there is nothing left to archive; that is not a failure of
                # THIS reclaim, so it must not crash the sweep, must not
                # re-emit LeaseExpired, and must not skip the DeadAgent
                # escalation below.
                print(
                    f"NOTE: lock {entry.name} already moved/removed "
                    f"(concurrent sweep?) before archive could run ({exc}); "
                    "reclaim already committed, continuing"
                )
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
    except Exception as exc:
        # Finding 3d (adversarial review): the six explicit except clauses
        # above only ever covered aaos.AaosError subclasses. Any OTHER
        # exception (this Finding's own repro used a raw FileNotFoundError,
        # but the class of bug is general) propagated straight out of
        # main(), which means a scheduled run of this sweep would crash
        # with a raw traceback instead of exiting cleanly non-zero. A
        # best-effort sweep must never do that -- print a clear one-line
        # message (no bare traceback dump) and fail the run visibly.
        print(f"UNEXPECTED ERROR: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
