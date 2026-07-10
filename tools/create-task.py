#!/usr/bin/env python3
"""
tools/create-task.py — create a new AAOS task in the queue.

Usage:
    python tools/create-task.py --title <str> --accept <str> [--accept <str> ...]
        (--id t-<slug> | --slug <slug>)
        [--owner <agent|unassigned>] [--priority {1,2,3}]
        [--input label=<l> ref=<r> kind=<k>]*
        [--suffix-timestamp]
        [--project <id>] [--seam-root <path>] [--now <iso>]

See docs/PHASE3-DESIGN.md section 2.1 for the full contract: id scheme
(caller-supplied slug, normalized, validated ^t-[a-z0-9-]+$), validate_obj
before write, TaskCreated via commit_state_change, and idempotency
(identical re-post of same id/content -> exit 0 no-op, no re-emit;
different content, same id -> exit 3).

Note on the TaskCreated event's `actor`: create-task.py has no --actor
flag in its documented CLI (unlike lease-task.py/complete-task.py/
fail-task.py, which act on behalf of a --holder). The design does not
specify what to attribute an unattended task-creation event to, so this
script uses the literal "system" actor, the same convention
expire-leases.py's sweep-generated events use. Flagged as a design gap in
docs/_task-reports/phase3-task2-report.md rather than silently invented.

Exit codes: see docs/PHASE3-DESIGN.md section 0.4.
    0 success (including idempotent no-op)
    1 schema/validation error
    2 usage error (bad args, missing --accept, invalid id/slug)
    3 same id, different content (write conflict)
    5 event-commit failure
"""
from __future__ import annotations

import argparse
import re
import sys
from datetime import datetime, timezone

import _aaos_common as aaos

TASK_ID_RE = re.compile(r"^t-[a-z0-9-]+$")


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
        prog="create-task.py", description="Create a new AAOS task (queue)."
    )
    parser.add_argument("--title", required=True)
    parser.add_argument(
        "--accept", action="append", default=None, metavar="CRITERION",
        help="Acceptance criterion (repeatable; >=1 required).",
    )
    id_group = parser.add_mutually_exclusive_group(required=True)
    id_group.add_argument("--id", default=None, help="Full task id, e.g. t-foo.")
    id_group.add_argument("--slug", default=None, help="Slug; normalized to t-<slug>.")
    parser.add_argument("--owner", default="unassigned")
    parser.add_argument("--priority", type=int, choices=[1, 2, 3], default=2)
    parser.add_argument(
        "--input", nargs=3, action="append", default=None,
        metavar=("label=L", "ref=R", "kind=K"),
    )
    parser.add_argument("--suffix-timestamp", action="store_true")
    _add_common_args(parser)
    return parser


def _build_task_id(args, now) -> str:
    if args.id:
        raw = args.id
    else:
        raw = f"t-{args.slug.strip().lower()}"
    if args.suffix_timestamp:
        raw = f"{raw}-{now.astimezone(timezone.utc).strftime('%Y%m%d%H%M%S')}"
    if not TASK_ID_RE.match(raw):
        raise aaos.UsageError(f"invalid task id {raw!r}: must match {TASK_ID_RE.pattern!r}")
    return raw


def main(argv=None) -> int:
    args = build_arg_parser().parse_args(argv)
    resolved_now = _resolve_now(args.now)

    try:
        aaos.require_seam_reachable(args.seam_root)
        task_id = _build_task_id(args, resolved_now)

        if not args.accept:
            print(
                "USAGE ERROR: at least one --accept acceptance criterion is required",
                file=sys.stderr,
            )
            return 2

        inputs = [_parse_kv_args(tok, ("label", "ref", "kind")) for tok in (args.input or [])]

        now_iso = resolved_now.isoformat()
        task = {
            "protocol_version": "1.0",
            "id": task_id,
            "project_id": args.project,
            "title": args.title,
            "owner_agent": args.owner,
            "state": "queued",
            "priority": args.priority,
            "inputs": inputs,
            "acceptance_criteria": list(args.accept),
            "created_at": now_iso,
            "updated_at": now_iso,
        }

        task_path = aaos.task_path(task_id, args.project, root=args.seam_root)
        if task_path.exists():
            existing = aaos.load_json(task_path)
            content_fields = (
                "title", "owner_agent", "priority", "inputs",
                "acceptance_criteria", "project_id",
            )
            if all(existing.get(f) == task.get(f) for f in content_fields):
                print(f"IDEMPOTENT NO-OP: {task_id} already exists with identical content")
                return 0
            raise aaos.StateConflictError(
                f"task {task_id} already exists with different content"
            )

        errors = aaos.validate_obj(task, "task")
        if errors:
            raise aaos.CorruptStateError(f"task failed schema validation: {errors}")

        event_id = aaos.commit_state_change(
            task, task_path,
            {
                "type": "TaskCreated", "actor": "system", "project_id": args.project,
                "refs": {"task": task_id}, "now": resolved_now,
            },
            root=args.seam_root,
        )
        print(f"CREATED: {task_id} (event {event_id})")
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
