#!/usr/bin/env python3
"""
tools/complete-task.py — mark a leased AAOS task done (and optionally
archive it in the same invocation).

Usage:
    python tools/complete-task.py <task-id> --holder <agent> --session <str>
        --evidence label=<l> ref=<r> [--evidence label=<l> ref=<r> ...]
        [--archive]
        [--project <id>] [--seam-root <path>] [--now <iso>]

See docs/PHASE3-DESIGN.md section 2.3. assert_lease_held guards the
double-run backstop (exit 4 if lost). Requires >=1 --evidence (else exit
2) -- enforces the schema's documented "evidence before done" business
rule. Sets state="done", drops lease, commit_state_change emitting
Completed; archives the residual lock file. --archive performs a SECOND
transition to state="archived", emitting Archived -- this is how Gate 3's
completed -> archived is achieved without an eighth script.

Idempotency ordering matters here: an already-"done" or already-"archived"
task is checked for BEFORE assert_lease_held, because completion already
dropped the lease -- re-running complete-task on a finished task must not
fail with LeaseLostError just because there's nothing left to hold.

Exit codes: see docs/PHASE3-DESIGN.md section 0.4.
    0 success (including idempotent no-op)
    1 schema/validation error
    2 usage error (no --evidence before done)
    3 state-conflict
    4 lease lost
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


def _parse_kv_args(tokens, keys):
    if len(tokens) != len(keys):
        raise aaos.UsageError(f"expected {len(keys)} key=value token(s) {keys}, got {tokens!r}")
    result = {}
    for tok, expected_key in zip(tokens, keys):
        if "=" not in tok:
            raise aaos.UsageError(f"malformed key=value token (missing '='): {tok!r}")
        k, _, v = tok.partition("=")
        if k != expected_key:
            raise aaos.UsageError(f"expected key {expected_key!r}, got {k!r} in token {tok!r}")
        result[k] = v
    return result


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="complete-task.py", description="Mark a leased AAOS task done."
    )
    parser.add_argument("task_id")
    parser.add_argument("--holder", required=True)
    parser.add_argument("--session", required=True)
    parser.add_argument(
        "--evidence", nargs=2, action="append", default=None,
        metavar=("label=L", "ref=R"),
    )
    parser.add_argument("--archive", action="store_true")
    _add_common_args(parser)
    return parser


def _archive_task(task, args, resolved_now):
    """Second transition: done -> archived. No-op if already archived."""
    if task.get("state") == "archived":
        return None
    new_task = dict(task)
    new_task["state"] = "archived"
    new_task["updated_at"] = resolved_now.isoformat()
    errors = aaos.validate_obj(new_task, "task")
    if errors:
        raise aaos.CorruptStateError(f"archived task failed schema validation: {errors}")
    return aaos.commit_state_change(
        new_task, aaos.task_path(args.task_id, args.project, root=args.seam_root),
        {
            "type": "Archived", "actor": args.holder, "project_id": args.project,
            "refs": {"task": args.task_id}, "now": resolved_now,
        },
        root=args.seam_root,
    )


def main(argv=None) -> int:
    args = build_arg_parser().parse_args(argv)
    resolved_now = _resolve_now(args.now)

    try:
        aaos.require_seam_reachable(args.seam_root)
        task = aaos.load_task(args.task_id, args.project, root=args.seam_root)

        if task.get("state") == "archived":
            print(f"IDEMPOTENT NO-OP: {args.task_id} already archived")
            return 0

        if task.get("state") == "done":
            print(f"IDEMPOTENT NO-OP: {args.task_id} already done")
            if args.archive:
                event_id = _archive_task(task, args, resolved_now)
                print(f"ARCHIVED: {args.task_id} (event {event_id})")
            return 0

        aaos.assert_lease_held(task, args.holder, args.session, now=resolved_now)

        if not args.evidence:
            print(
                "USAGE ERROR: at least one --evidence label=<l> ref=<r> is required before done",
                file=sys.stderr,
            )
            return 2

        evidence = [_parse_kv_args(tok, ("label", "ref")) for tok in args.evidence]

        new_task = dict(task)
        new_task["state"] = "done"
        new_task.pop("lease", None)
        new_task["evidence"] = evidence
        new_task["updated_at"] = resolved_now.isoformat()

        errors = aaos.validate_obj(new_task, "task")
        if errors:
            raise aaos.CorruptStateError(f"completed task failed schema validation: {errors}")

        event_id = aaos.commit_state_change(
            new_task, aaos.task_path(args.task_id, args.project, root=args.seam_root),
            {
                "type": "Completed", "actor": args.holder, "project_id": args.project,
                "refs": {"task": args.task_id}, "now": resolved_now,
            },
            root=args.seam_root,
        )

        lock_file = aaos.lock_path(args.task_id, args.project, root=args.seam_root)
        if lock_file.exists():
            aaos.archive_lock(lock_file, now=resolved_now)

        print(f"COMPLETED: {args.task_id} (event {event_id})")

        if args.archive:
            archive_event_id = _archive_task(new_task, args, resolved_now)
            print(f"ARCHIVED: {args.task_id} (event {archive_event_id})")

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
