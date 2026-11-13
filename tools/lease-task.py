#!/usr/bin/env python3
"""
tools/lease-task.py — acquire a lease on a queued AAOS task.

Usage:
    python tools/lease-task.py <task-id> --holder <agent> --session <str>
        [--ttl-min 45] [--skew-min 5] [--settle-sec 30]
        [--project <id>] [--seam-root <path>] [--now <iso>]

See docs/PHASE3-DESIGN.md section 2.2. Calls aaos.acquire_lease (the
ADR-0003 six-step protocol lives entirely in _aaos_common.py; this script
is a thin CLI wrapper plus the caller-side transaction):

    ACQUIRED  -> (emit LeaseExpired first if reclaimed_prior) -> set
                 state="leased"+lease, commit_state_change emitting
                 TaskLeased, exit 0.
    DENIED    -> emit LeaseDenied, exit 3.
    CONTESTED -> emit LeaseContested, exit 4.

No Notion call, no human-escalation row here -- Phase 3 emits the machine
event only (Problem-A boundary, Phase 4/5).

Idempotent: re-run with the same holder+session on an already-held lease
-> exit 0 no-op (no re-emit).

Exit codes: see docs/PHASE3-DESIGN.md section 0.4.
    0 success (including idempotent no-op / ACQUIRED)
    1 schema/validation error
    2 usage error
    3 DENIED (state-conflict / precondition unmet)
    4 CONTESTED (concurrency fail-closed)
    5 event-commit failure
"""
from __future__ import annotations

import argparse
import sys

import _aaos_common as aaos


def _add_common_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--project", default="ora-ops")
    parser.add_argument("--seam-root", default=None)
    parser.add_argument("--now", default=None, help="ISO-8601 timestamp (test hook).")


def _resolve_now(now_str):
    return aaos.parse_cli_now(now_str)


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="lease-task.py", description="Acquire a lease on a queued AAOS task."
    )
    parser.add_argument("task_id")
    parser.add_argument("--holder", required=True)
    parser.add_argument("--session", required=True)
    parser.add_argument("--ttl-min", type=aaos.positive_int, default=45)
    parser.add_argument("--skew-min", type=aaos.nonneg_int, default=5)
    parser.add_argument("--settle-sec", type=aaos.finite_nonneg_float, default=30.0)
    _add_common_args(parser)
    return parser


@aaos.freeze_exit
def main(argv=None) -> int:
    args = build_arg_parser().parse_args(argv)

    try:
        resolved_now = _resolve_now(args.now)
        # The lock's `expires` is stamped BEFORE the settle sleep. A settle that lasts as long as the
        # lease would leave the holder asleep while its own lock is already reclaimable, and the
        # conflict path would then archive a legitimate reclaimer's live lock.
        if args.settle_sec >= args.ttl_min * 60:
            raise aaos.UsageError(
                f"--settle-sec ({args.settle_sec:g}) must be shorter than the lease it settles "
                f"(--ttl-min {args.ttl_min} = {args.ttl_min * 60} s)"
            )
        aaos.require_seam_reachable(args.seam_root)
        task = aaos.load_task(args.task_id, args.project, root=args.seam_root)
        lease = task.get("lease")
        if (
            task.get("state") == "leased"
            and lease
            and lease.get("holder") == args.holder
            and lease.get("session") == args.session
        ):
            print(f"IDEMPOTENT NO-OP: {args.task_id} already leased by {args.holder}/{args.session}")
            return 0

        result = aaos.acquire_lease(
            args.task_id, args.project, args.holder, args.session,
            now=resolved_now, ttl_minutes=args.ttl_min, skew_minutes=args.skew_min,
            settle_seconds=args.settle_sec, root=args.seam_root,
        )

        if result.outcome == aaos.LeaseOutcome.ACQUIRED:
            # F02: the canonical lock now exists and is ours. If anything
            # below fails (schema error, event-commit failure, anything), the
            # task stays queued -- so archive the lock this call created, or
            # the task is DENIED to everyone until the lock expires.
            try:
                if result.reclaimed_prior is not None:
                    aaos.emit_event(
                        type="LeaseExpired", actor="system", project_id=args.project,
                        refs={"task": args.task_id}, now=resolved_now, root=args.seam_root,
                    )

                # result.lock is the *lock file's* dict (protocol_version,
                # task_ref, holder, acquired, expires, session) -- the task
                # schema's "lease" object is narrower (additionalProperties:
                # false; only holder/acquired/expires/session), so project down
                # to exactly those four before embedding it in the task.
                new_task = dict(task)
                new_task["state"] = "leased"
                new_task["lease"] = {
                    "holder": result.lock["holder"],
                    "acquired": result.lock["acquired"],
                    "expires": result.lock["expires"],
                    "session": result.lock["session"],
                }
                new_task["updated_at"] = resolved_now.isoformat()

                errors = aaos.validate_obj(new_task, "task")
                if errors:
                    raise aaos.CorruptStateError(f"leased task failed schema validation: {errors}")

                lock_ref = str(aaos.lock_path(args.task_id, args.project, root=args.seam_root))
                event_id = aaos.commit_state_change(
                    new_task, aaos.task_path(args.task_id, args.project, root=args.seam_root),
                    {
                        "type": "TaskLeased", "actor": args.holder, "project_id": args.project,
                        "refs": {"task": args.task_id, "lock": lock_ref}, "now": resolved_now,
                    },
                    root=args.seam_root,
                )
            except BaseException:
                aaos.archive_lock_if_unchanged(
                    aaos.lock_path(args.task_id, args.project, root=args.seam_root),
                    result.lock, now=resolved_now,
                )
                raise
            print(f"ACQUIRED: {args.task_id} by {args.holder}/{args.session} (event {event_id})")
            return 0

        if result.outcome == aaos.LeaseOutcome.DENIED:
            aaos.emit_event(
                type="LeaseDenied", actor=args.holder, project_id=args.project,
                refs={"task": args.task_id}, detail=(result.reason or "")[:200],
                now=resolved_now, root=args.seam_root,
            )
            print(f"DENIED: {args.task_id}: {result.reason}", file=sys.stderr)
            return 3

        # CONTESTED
        aaos.emit_event(
            type="LeaseContested", actor=args.holder, project_id=args.project,
            refs={"task": args.task_id}, detail=(result.reason or "")[:200],
            now=resolved_now, root=args.seam_root,
        )
        print(f"CONTESTED: {args.task_id}: {result.reason}", file=sys.stderr)
        return 4

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
        return aaos.unexpected_error_exit(exc)


if __name__ == "__main__":
    sys.exit(main())
