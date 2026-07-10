#!/usr/bin/env python3
"""
tools/emit-event.py — thin CLI wrapper around _aaos_common.emit_event.

Usage:
    python tools/emit-event.py --type <EventType> --actor <agent|system>
        [--task t-...] [--handoff h-...] [--lock <str>] [--escalation <str>]
        [--detail <str>] [--event-log-ref <str>]
        [--drain-only]
        [--project <id>] [--seam-root <path>] [--now <iso>]

See docs/PHASE3-DESIGN.md section 2.7. This script is DELIBERATELY
NON-IDEMPOTENT: every call mints and appends a new event (aaos.emit_event
always allocates a fresh, monotonically-sequenced event_id). Dedup of
*logical* duplicate events is the higher-level scripts' job (e.g.
complete-task.py won't re-emit Completed on an already-done task) --
do NOT "fix" this into a dedupe primitive.

Concern flagged in docs/_task-reports/phase3-task2-report.md: because
_aaos_common.emit_event() itself raises EventCommitError (-> exit 5, per
this script's exception cascade) both when append+spool both fail AND
when the constructed event fails schema validation (e.g. an unrecognized
--type), an invalid --type here surfaces as exit 5, not the "1 =
schema/validation error" bucket the exit-code table's general wording
might suggest. This is the already-committed Task 1 behavior of
_aaos_common.py (see its emit_event docstring/implementation) and is
deliberately NOT pre-validated/duplicated here.

Exit codes: see docs/PHASE3-DESIGN.md section 0.4.
    0 success (drain-only or emit; never idempotent-skipped)
    2 usage error (missing --type/--actor when not --drain-only; or the
      seam root itself is unreachable -- Phase 5 scenario #4, see
      aaos.require_seam_reachable)
    5 event-commit failure (includes: constructed event fails schema validation)
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
        prog="emit-event.py",
        description=(
            "Thin wrapper around _aaos_common.emit_event. Deliberately "
            "non-idempotent: each invocation mints and appends a new event."
        ),
    )
    parser.add_argument("--type", default=None)
    parser.add_argument("--actor", default=None)
    parser.add_argument("--task", default=None)
    parser.add_argument("--handoff", default=None)
    parser.add_argument("--lock", default=None)
    parser.add_argument("--escalation", default=None)
    parser.add_argument("--detail", default=None)
    parser.add_argument("--event-log-ref", default=None)
    parser.add_argument("--drain-only", action="store_true")
    _add_common_args(parser)
    return parser


def main(argv=None) -> int:
    args = build_arg_parser().parse_args(argv)
    resolved_now = _resolve_now(args.now)

    try:
        aaos.require_seam_reachable(args.seam_root)
        if args.drain_only:
            n = aaos.drain_event_spool(args.project, root=args.seam_root, now=resolved_now)
            print(f"DRAINED: {n} spooled event(s)")
            return 0

        if not args.type or not args.actor:
            print("USAGE ERROR: --type and --actor are required (unless --drain-only)", file=sys.stderr)
            return 2

        refs = {}
        if args.task:
            refs["task"] = args.task
        if args.handoff:
            refs["handoff"] = args.handoff
        if args.lock:
            refs["lock"] = args.lock
        if args.escalation:
            refs["escalation"] = args.escalation

        event_id = aaos.emit_event(
            type=args.type, actor=args.actor, project_id=args.project,
            refs=refs, detail=args.detail, event_log_ref=args.event_log_ref,
            now=resolved_now, root=args.seam_root,
        )
        print(f"EMITTED: {event_id}")
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
