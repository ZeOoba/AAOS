#!/usr/bin/env python3
"""
tools/fail-task.py — mark a leased AAOS task failed.

Usage:
    python tools/fail-task.py <task-id> --holder <agent> --session <str>
        --reason <str> [--evidence label=<l> ref=<r>]*
        [--project <id>] [--seam-root <path>] [--now <iso>]

See docs/PHASE3-DESIGN.md section 2.4. assert_lease_held guards the
double-run backstop. Sets state="failed", commit_state_change emitting
Failed; archives the residual lock file. If a prior Failed for the same
task already exists in the NDJSON, additionally emits Escalated (trigger
DoubleFailure, machine-log only, no Notion). No task-level auto-retry
(no schema field for it -- would be scope-creep).

Note on --reason: the task schema has no failure-reason field (unlike the
"blocked" state, which does have blocked_reason) -- there is nothing to
attach it to on the task object itself. This script places --reason in
the Failed event's `detail` field (maxLength 200, truncated defensively),
which is exactly the schema's stated purpose for that field ("why
LeaseContested, etc."). Flagged in docs/_task-reports/phase3-task2-report.md
as a design-gap interpretation, not a silent invention.

Idempotent: already-failed -> no-op (checked before assert_lease_held, for
the same reason as complete-task.py: a finished task has already dropped
its lease).

Exit codes: see docs/PHASE3-DESIGN.md section 0.4.
    0 success (including idempotent no-op)
    1 schema/validation error
    2 usage error
    4 lease lost
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
        prog="fail-task.py", description="Mark a leased AAOS task failed."
    )
    parser.add_argument("task_id")
    parser.add_argument("--holder", required=True)
    parser.add_argument("--session", required=True)
    parser.add_argument("--reason", required=True)
    parser.add_argument(
        "--evidence", nargs=2, action="append", default=None,
        metavar=("label=L", "ref=R"),
    )
    _add_common_args(parser)
    return parser


def _prior_failed_exists(project, task_id, root) -> bool:
    """Uses aaos.read_events (NDJSON + not-yet-drained pending events,
    deduped by event_id -- see docs/PHASE3-DESIGN.md section 1.10 REVISED)
    so a still-pending prior Failed event isn't missed by this scan even if
    it hasn't been folded into the NDJSON yet."""
    for rec in aaos.read_events(project, root=root):
        if rec.get("type") == "Failed" and rec.get("refs", {}).get("task") == task_id:
            return True
    return False


@aaos.freeze_exit
def main(argv=None) -> int:
    args = build_arg_parser().parse_args(argv)

    try:
        resolved_now = _resolve_now(args.now)
        aaos.require_seam_reachable(args.seam_root)
        task = aaos.load_task(args.task_id, args.project, root=args.seam_root)

        if task.get("state") == "failed":
            print(f"IDEMPOTENT NO-OP: {args.task_id} already failed")
            return 0

        aaos.assert_lease_held(task, args.holder, args.session, now=resolved_now)

        prior_failed = _prior_failed_exists(args.project, args.task_id, args.seam_root)

        new_task = dict(task)
        new_task["state"] = "failed"
        new_task.pop("lease", None)
        if args.evidence:
            new_task["evidence"] = [_parse_kv_args(tok, ("label", "ref")) for tok in args.evidence]
        new_task["updated_at"] = resolved_now.isoformat()

        errors = aaos.validate_obj(new_task, "task")
        if errors:
            raise aaos.CorruptStateError(f"failed task failed schema validation: {errors}")

        event_id = aaos.commit_state_change(
            new_task, aaos.task_path(args.task_id, args.project, root=args.seam_root),
            {
                "type": "Failed", "actor": args.holder, "project_id": args.project,
                "refs": {"task": args.task_id}, "detail": args.reason[:200], "now": resolved_now,
            },
            root=args.seam_root,
        )

        aaos.archive_lock_after_commit(
            aaos.lock_path(args.task_id, args.project, root=args.seam_root), now=resolved_now
        )

        print(f"FAILED: {args.task_id} (event {event_id})")

        if prior_failed:
            esc_id = aaos.emit_event(
                type="Escalated", actor="system", project_id=args.project,
                refs={"task": args.task_id}, detail="DoubleFailure",
                now=resolved_now, root=args.seam_root,
            )
            print(f"ESCALATED: {args.task_id} DoubleFailure (event {esc_id})")

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
        return aaos.unexpected_error_exit(exc)


if __name__ == "__main__":
    sys.exit(main())
