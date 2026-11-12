#!/usr/bin/env python3
"""
tools/derive-heartbeat.py — AAOS Phase 3 §4.1: derive heartbeat.json from an
externally-fetched signals payload.

This is a pure local-file compute script. It never calls Notion or any MCP
tool — per docs/PHASE3-DESIGN.md §4 and §6, and the zero-new-emitters
doctrine in docs/adr/0005-heartbeat-derivation.md, the actual Notion /
fleet-status.py fetch that produces the `signals.json` input happens at a
future agent-session call site, out of Phase-3 scope. This script's only
job is: read that already-fetched signal data, apply the freshness-banding
math, cross-reference the agent registry, validate, and write
heartbeat.json atomically.

CLI:
    python tools/derive-heartbeat.py --signals <path|-> [--out <path>]
                                      [--now <iso>] [--seam-root <dir>]

Input shape (docs/PHASE3-DESIGN.md §4.1):
    {"agents": [
        {"agent_id": "cowork", "last_run": "2026-07-09T06:00:00-04:00",
         "source": ["notion:35c6ecd8 last_edited_time", "..."],
         "threshold_hours": 16},
        ...
    ]}

Status banding (delta = now - last_run, in hours):
    last_run null/missing  -> "unknown"          (fail-closed)
    delta <= threshold_hours       -> "fresh"
    delta <= 2 * threshold_hours   -> "stale"
    delta >  2 * threshold_hours   -> "missed"
`source` and `threshold_hours` are always carried through from the input
verbatim — inherited, never invented.

Registry cross-reference (via _aaos_common.load_registry(), which always
reads the git-tree REPO_ROOT/agent-registry.json — see design §0.2; there
is deliberately no --registry override lever here, matching load_registry's
own signature):
  - A registered agent absent from signals.json is included with
    status="unknown", last_run=null, source carried from that agent's own
    `heartbeat_source` registry field (never invented), and
    threshold_hours=0 (see the design-choice note below — there is no
    threshold to inherit for an agent we observed nothing about, and the
    schema requires the field regardless; "unknown" status makes the
    numeric value moot to every consumer per the heartbeat schema's own
    documented consumer note).
  - A registered agent with `"active": false` is always forced to
    status="unknown", regardless of what signals.json says about it (an
    inactive agent is not part of the live fleet expire-leases.py should
    reason about). Its last_run/source/threshold_hours are still carried
    through from the observed signal when one exists (audit trail, not
    fabrication) or from the registry/default-0 fallback when it doesn't.
  - A signals.json entry for an agent_id that is NOT in the registry at all
    is still surfaced (computed normally) rather than silently dropped —
    refusing to report a real observed signal would itself be a kind of
    fabrication-by-omission. The registry is the *expected* set (per the
    "registry agent absent from signals" rule above), not an exhaustive
    filter on what may appear.

Exit codes:
    0   success (heartbeat.json written; the printed line is its path)
    1   assembled document failed schema validation — nothing written
    2   usage error: --signals unreadable/missing, or its content is not
        well-formed per the shape above (never fabricate a value for a
        malformed field — refuse instead)
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

import _aaos_common as aaos  # noqa: E402  (import after sys.path manipulation, by design)

# threshold_hours representation for a registry agent that has no signal at
# all to inherit from (see module docstring's "Registry cross-reference").
NO_SIGNAL_THRESHOLD_HOURS = 0


def _read_signals_text(signals_arg: str) -> str:
    if signals_arg == "-":
        return sys.stdin.read()
    path = Path(signals_arg)
    try:
        return aaos.g_read_text(path)
    except OSError as exc:
        raise aaos.UsageError(f"could not read --signals file '{path}': {exc}") from exc


def _parse_signals(raw_text: str) -> dict:
    """Parse and validate the signals payload shape. Returns a dict keyed by
    agent_id -> {"agent_id", "last_run_raw", "last_run_dt", "source",
    "threshold_hours"}, preserving input order. Raises UsageError on any
    malformed field — this function never fabricates a value."""
    try:
        data = json.loads(raw_text)
    except json.JSONDecodeError as exc:
        raise aaos.UsageError(f"--signals content is not valid JSON: {exc}") from exc

    if not isinstance(data, dict) or not isinstance(data.get("agents"), list):
        raise aaos.UsageError(
            "--signals content must be a JSON object with an 'agents' array"
        )

    by_id: dict[str, dict] = {}
    for i, entry in enumerate(data["agents"]):
        if not isinstance(entry, dict):
            raise aaos.UsageError(f"--signals agents[{i}] is not an object")

        agent_id = entry.get("agent_id")
        if not isinstance(agent_id, str) or not agent_id:
            raise aaos.UsageError(f"--signals agents[{i}] missing a valid 'agent_id'")

        source = entry.get("source")
        if not isinstance(source, list) or not source or not all(
            isinstance(s, str) for s in source
        ):
            raise aaos.UsageError(
                f"--signals agents[{i}] ('{agent_id}') 'source' must be a "
                "non-empty array of strings"
            )

        threshold_hours = entry.get("threshold_hours")
        if not isinstance(threshold_hours, (int, float)) or isinstance(threshold_hours, bool):
            raise aaos.UsageError(
                f"--signals agents[{i}] ('{agent_id}') 'threshold_hours' must be a number"
            )

        last_run_raw = entry.get("last_run", None)
        last_run_dt = None
        if last_run_raw is not None:
            if not isinstance(last_run_raw, str):
                raise aaos.UsageError(
                    f"--signals agents[{i}] ('{agent_id}') 'last_run' must be a string or null"
                )
            try:
                last_run_dt = aaos._parse_iso_dt(last_run_raw)
            except aaos.CorruptStateError as exc:
                raise aaos.UsageError(
                    f"--signals agents[{i}] ('{agent_id}') 'last_run' is unparseable: {exc}"
                ) from exc

        by_id[agent_id] = {
            "agent_id": agent_id,
            "last_run_raw": last_run_raw,
            "last_run_dt": last_run_dt,
            "source": list(source),
            "threshold_hours": threshold_hours,
        }
    return by_id


def _status_for(last_run_dt, threshold_hours, now: datetime) -> str:
    if last_run_dt is None:
        return "unknown"
    delta_hours = (now - last_run_dt).total_seconds() / 3600.0
    if delta_hours < 0:
        # last_run reported in the future relative to now (clock skew) --
        # never fabricate a worse status than the data actually supports.
        return "fresh"
    if delta_hours <= threshold_hours:
        return "fresh"
    if delta_hours <= 2 * threshold_hours:
        return "stale"
    return "missed"


def _registry_heartbeat_source(reg_agent: dict, agent_id: str) -> list[str]:
    source = reg_agent.get("heartbeat_source")
    if isinstance(source, list) and source and all(isinstance(s, str) for s in source):
        return source
    # Defensive fallback only: the schema requires a non-empty `source`
    # array, and a registry entry should always declare heartbeat_source,
    # but refuse to leave the field unpopulated if it somehow doesn't.
    return [f"agent-registry.json:{agent_id} (no heartbeat_source declared)"]


def _build_heartbeat(signals_by_id: dict, registry: dict, now: datetime) -> dict:
    agents_out: list[dict] = []
    seen: set[str] = set()

    for reg_agent in registry.get("agents", []):
        if not isinstance(reg_agent, dict):
            continue
        agent_id = reg_agent.get("id")
        if not isinstance(agent_id, str) or not agent_id:
            continue
        seen.add(agent_id)
        active = reg_agent.get("active", True)
        sig = signals_by_id.get(agent_id)

        if sig is not None:
            entry = {
                "agent_id": agent_id,
                "last_run": sig["last_run_raw"],
                "source": sig["source"],
                "status": _status_for(sig["last_run_dt"], sig["threshold_hours"], now),
                "threshold_hours": sig["threshold_hours"],
            }
        else:
            entry = {
                "agent_id": agent_id,
                "last_run": None,
                "source": _registry_heartbeat_source(reg_agent, agent_id),
                "status": "unknown",
                "threshold_hours": NO_SIGNAL_THRESHOLD_HOURS,
            }

        if not active:
            # Design §4.1 / task-instructions interpretation: an inactive
            # registry agent is reported unknown regardless of what signals
            # say -- it is not part of the live fleet expire-leases.py
            # should reason about. Observed last_run/source/threshold_hours
            # are still carried through verbatim above for audit purposes;
            # only `status` is forced here.
            entry["status"] = "unknown"

        agents_out.append(entry)

    for agent_id, sig in signals_by_id.items():
        if agent_id in seen:
            continue
        agents_out.append({
            "agent_id": agent_id,
            "last_run": sig["last_run_raw"],
            "source": sig["source"],
            "status": _status_for(sig["last_run_dt"], sig["threshold_hours"], now),
            "threshold_hours": sig["threshold_hours"],
        })

    return {
        "protocol_version": "1.0",
        "derived_at": now.isoformat(),
        "agents": agents_out,
    }


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="derive-heartbeat.py",
        description="Derive heartbeat.json from an externally-fetched signals payload "
        "(pure local-file compute -- never calls Notion/MCP itself).",
    )
    parser.add_argument(
        "--signals", required=True,
        help="Path to the signals JSON file, or '-' to read it from stdin.",
    )
    parser.add_argument(
        "--out", default=None,
        help="Output path (default: <seam-root>/heartbeat.json).",
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


@aaos.freeze_exit
def main(argv=None) -> int:
    args = build_arg_parser().parse_args(argv)

    # Freeze sentinel (ADR-0007 addendum 2026-11-12): every file-path argument, not only the seam
    # root, is refused if it is or lies under the live seam -- before any read or write.
    try:
        aaos.refuse_frozen_live_paths([(args.signals, "--signals"), (args.out, "--out")])
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
        raw_text = _read_signals_text(args.signals)
        signals_by_id = _parse_signals(raw_text)
    except aaos.UsageError as exc:
        print(f"USAGE ERROR: {exc}")
        return 2

    try:
        registry = aaos.load_registry()
    except Exception as exc:  # noqa: BLE001 -- any registry-load failure is a usage error
        print(f"USAGE ERROR: could not load agent registry: {exc}")
        return 2

    heartbeat = _build_heartbeat(signals_by_id, registry, now)

    errors = aaos.validate_obj(heartbeat, "heartbeat")
    if errors:
        print("SCHEMA VALIDATION FAILED, nothing written:")
        for e in errors:
            print(f"  {e}")
        return 1

    if args.out:
        out_path = Path(args.out)
    else:
        # Gate-5 review Minor #3: this tool's INPUT (--signals) is never
        # seam-rooted, so only gate the seam here -- at the point we're
        # about to default to writing INTO the seam's heartbeat.json. A
        # phantom seam must fail closed rather than auto-vivify a stray
        # heartbeat.json in a brand-new parallel tree at exit 0.
        try:
            aaos.require_seam_reachable(args.seam_root)
        except aaos.UsageError as exc:
            print(f"USAGE ERROR: {exc}")
            return 2
        out_path = aaos.heartbeat_path(root=args.seam_root)
    aaos.save_json(out_path, heartbeat)
    print(str(out_path))
    return 0


if __name__ == "__main__":
    sys.exit(main())
