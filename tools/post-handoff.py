#!/usr/bin/env python3
"""
tools/post-handoff.py — post an AAOS handoff (task -> task, agent -> agent).

Usage:
    python tools/post-handoff.py --id h-<slug> --task <task-id>
        --from <agent> --to <agent> --next-action <str>
        [--summary <str> | --summary-file <path>]
        [--artifact path=<p>]* [--blocker <str>]*
        [--suggested-next-owner <agent>]
        [--holder <agent> --session <str>]
        [--project <id>] [--seam-root <path>] [--now <iso>]

See docs/PHASE3-DESIGN.md section 2.5. Computes each artifact's sha256 +
bytes from disk (a listed artifact that doesn't exist on disk is a usage
error, exit 2 -- the manifest hashes exist so the receiver can verify).
Validates BEFORE writing (mandatory): validate_obj(handoff, "handoff")
runs the schema check + protocol_version check + write_scope colonization
guard; any error -> exit 1, write nothing. save_handoff is write-once
(identical re-post = no-op, no re-emit; different content same id = exit
3). Non-empty --blocker -> load the task, assert_lease_held if
--holder/--session given, set state="blocked" + blocked_reason, emit
Blocked in addition to HandoffPosted.

Registry note: this script always validates against the REAL repo-root
agent-registry.json -- aaos.validate_obj("handoff", registry=None) loads
it via aaos.load_registry(). There is no --registry override here,
per the literal CLI spec in PHASE3-DESIGN.md section 2.5.

Exit codes: see docs/PHASE3-DESIGN.md section 0.4.
    0 success (including idempotent no-op)
    1 validation/write-scope failure (nothing written)
    2 usage error (artifact missing on disk, --summary-file missing)
    3 write-once conflict
    4 lease lost (only when --holder/--session given with a blocker)
    5 event-commit failure
"""
from __future__ import annotations

import argparse
import hashlib
import sys
from datetime import datetime
from pathlib import Path

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
        prog="post-handoff.py", description="Post an AAOS handoff."
    )
    parser.add_argument("--id", required=True)
    parser.add_argument("--task", required=True, dest="task_ref")
    parser.add_argument("--from", required=True, dest="from_agent")
    parser.add_argument("--to", required=True, dest="to_agent")
    parser.add_argument("--next-action", required=True, dest="next_action")
    summary_group = parser.add_mutually_exclusive_group()
    summary_group.add_argument("--summary", default=None)
    summary_group.add_argument("--summary-file", default=None)
    parser.add_argument(
        "--artifact", nargs=1, action="append", default=None, metavar="path=P",
    )
    parser.add_argument("--blocker", action="append", default=None)
    parser.add_argument("--suggested-next-owner", default=None)
    parser.add_argument("--holder", default=None)
    parser.add_argument("--session", default=None)
    _add_common_args(parser)
    return parser


def _hash_artifact(path_str: str) -> dict:
    path = Path(path_str)
    if not path.is_file():
        raise aaos.UsageError(
            f"artifact not found on disk, cannot compute manifest hash: {path_str}"
        )
    data = path.read_bytes()
    return {"path": path_str, "sha256": hashlib.sha256(data).hexdigest(), "bytes": len(data)}


def main(argv=None) -> int:
    args = build_arg_parser().parse_args(argv)
    resolved_now = _resolve_now(args.now)

    try:
        aaos.require_seam_reachable(args.seam_root)
        summary = args.summary
        if summary is None and args.summary_file:
            summary_path = Path(args.summary_file)
            if not summary_path.is_file():
                raise aaos.UsageError(f"--summary-file not found: {args.summary_file}")
            summary = summary_path.read_text(encoding="utf-8")
        if summary is None:
            summary = ""

        artifact_manifest = [
            _hash_artifact(_parse_kv_args(tok, ("path",))["path"])
            for tok in (args.artifact or [])
        ]

        handoff = {
            "protocol_version": "1.0",
            "id": args.id,
            "project_id": args.project,
            "task_ref": args.task_ref,
            "from_agent": args.from_agent,
            "to_agent": args.to_agent,
            "artifact_manifest": artifact_manifest,
            "context_summary": summary,
            "required_next_action": args.next_action,
            "blockers": list(args.blocker) if args.blocker else [],
            "posted_at": resolved_now.isoformat(),
        }
        if args.suggested_next_owner:
            handoff["suggested_next_owner"] = args.suggested_next_owner

        errors = aaos.validate_obj(handoff, "handoff")
        if errors:
            raise aaos.CorruptStateError(f"handoff failed validation: {errors}")

        existing = aaos.load_handoff(args.id, args.project, root=args.seam_root)
        is_noop = existing is not None and existing == handoff

        if not is_noop:
            aaos.save_handoff(handoff, root=args.seam_root)
            event_id = aaos.emit_event(
                type="HandoffPosted", actor=args.from_agent, project_id=args.project,
                refs={"task": args.task_ref, "handoff": args.id},
                now=resolved_now, root=args.seam_root,
            )
            print(f"POSTED: {args.id} (event {event_id})")
        else:
            print(f"IDEMPOTENT NO-OP: {args.id} already posted with identical content")

        if args.blocker:
            task = aaos.load_task(args.task_ref, args.project, root=args.seam_root)
            if args.holder and args.session:
                aaos.assert_lease_held(task, args.holder, args.session, now=resolved_now)

            blocked_reason = "; ".join(args.blocker)
            if task.get("state") != "blocked" or task.get("blocked_reason") != blocked_reason:
                new_task = dict(task)
                new_task["state"] = "blocked"
                new_task["blocked_reason"] = blocked_reason
                new_task["updated_at"] = resolved_now.isoformat()
                # Gate-5 review Issue 1: task.schema.json's own invariant is
                # "lease present iff state is leased or in-progress" -- a
                # blocked task must not carry a lease. Drop it here so a
                # dead agent's lock on a now-blocked task is not left
                # unreclaimable forever (expire-leases.py only re-examines
                # locks whose task is leased/in-progress).
                new_task.pop("lease", None)

                errors = aaos.validate_obj(new_task, "task")
                if errors:
                    raise aaos.CorruptStateError(f"blocked task failed schema validation: {errors}")

                blocked_event_id = aaos.commit_state_change(
                    new_task, aaos.task_path(args.task_ref, args.project, root=args.seam_root),
                    {
                        "type": "Blocked", "actor": args.from_agent, "project_id": args.project,
                        "refs": {"task": args.task_ref, "handoff": args.id}, "now": resolved_now,
                    },
                    root=args.seam_root,
                )

                lock_file = aaos.lock_path(args.task_ref, args.project, root=args.seam_root)
                if lock_file.exists():
                    aaos.archive_lock(lock_file, now=resolved_now)

                print(f"BLOCKED: {args.task_ref} (event {blocked_event_id})")

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
