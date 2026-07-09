# 0005. Heartbeat derivation — zero new monitoring emitters

## Status

Accepted

## Context

The master prompt's Non-Goals explicitly forbid building a second monitoring/heartbeat emitter. `heartbeat.json` must therefore be a thin, read-only projection of the health-monitoring chain that already exists and runs in production: the `ora-cowork-heartbeat` scheduled task, which fires twice daily and updates a Notion page's `last_edited_time`, watched by a 16-hour-threshold GitHub Actions watchdog; the `automation-health-monitor` skill (version 1.6), which maintains a producer-consumer "beacon" table and an ORPHANED-BEACON detection rule; `registry-membership-probe.py`, a daily fleet-drift detector; `fleet-status.py`, the canonical dual-fleet health reporter; and `fleet-session-hook.py`, which tracks live Claude Code session state.

## Decision

A tool named `derive-heartbeat.py`, run on the same cadence as the lease-expiry sweep, reads and reformats these existing signals — it never pings anything, never writes to any monitoring system, and never creates a new beacon of its own. For the `claude-code` agent, its source is `fleet-session-hook.py`'s session state file modification time combined with `fleet-status.py`'s dual-fleet health output. For the `cowork` agent, its source is the Notion heartbeat page's `last_edited_time`, read only via the Notion MCP connector, combined with the `automation-health-monitor` beacon table. The `status` field is computed by comparing the observed `last_run` against the upstream system's own existing threshold — 16 hours for the Cowork watchdog canary — and this threshold is always inherited from the upstream signal, never independently invented by AAOS. If an upstream signal cannot be read at all, the agent's status is set to `unknown` and flagged; `unknown` is explicitly never treated as equivalent to fresh, which is a fail-closed default.

## Consequences

The existing health-monitoring chain remains the single source of truth for liveness; `heartbeat.json` is purely a stamped, derived mirror of it, consumed by `expire-leases.py` to decide whether to reclaim a dead agent's leases, and by `project-notion.py` for the human-facing dashboard. Because thresholds are inherited rather than hardcoded, a future change to the upstream watchdog's threshold is honored automatically without any AAOS code change.
