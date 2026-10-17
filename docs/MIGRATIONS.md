# AAOS Protocol Migrations

AAOS schemas carry a `protocol_version` field (currently `"1.0"` in all five schemas: `agent-registry`, `task`, `handoff`, `event`, `heartbeat`). Per the project's Non-Goals, there is no compatibility-matrix or negotiation machinery — this file is the entire versioning mechanism, plus the `protocol_version` field itself.

**Rule:** `tools/validate.py` rejects any AAOS file whose `protocol_version` major version does not match the current supported major version, with an error message pointing here.

**How to bump the protocol version:** when a schema needs a breaking change, (1) increment the major version (e.g. `1.0` → `2.0`) in the relevant schema's `const`, (2) add a dated entry below describing exactly what changed and why, (3) update `tools/validate.py`'s supported-version check, (4) if existing state files need to keep working, write a one-off migration note here describing the field-by-field transformation — there is no automated migration tooling in v1; this is a manual, documented process by design.

## Version history

### 1.0 — 2026-07-09 (initial)
Initial release of all five schemas (`agent-registry`, `task`, `handoff`, `event`, `heartbeat`) as part of AAOS Phase 1. No prior version exists; nothing to migrate from.

## CLI behaviour changes (no protocol_version bump)

### 2026-10-17 -- A0.5: CLI input validation (audit M03, M43 behaviour part)
The queue CLIs now reject invalid inputs with exit 2 *before* any lock, lease, event or state is touched:

- `--now` must carry a UTC offset in all seven queue CLIs (`lease-task`, `expire-leases`, `complete-task`, `create-task`, `emit-event`, `fail-task`, `post-handoff`). A naive value (e.g. `2026-07-09T12:00:00`) now exits 2 with `USAGE ERROR: --now: ...`; previously it was accepted and compared against timezone-aware datetimes. `expire-leases` also now reports a bad `--now` as exit 2 instead of an uncaught exception.
- `lease-task --ttl-min` must be an integer >= 1; `lease-task` and `expire-leases` `--skew-min` an integer >= 0; `lease-task --settle-sec` a finite number >= 0 (NaN and Inf rejected). A negative `--skew-min` previously let a contender reclaim a still-live foreign lease.
- The ratified defaults are unchanged (45-minute TTL, 5-minute skew, 30s settle) and are defaults, not floors: `--settle-sec 0` and `--skew-min 0` remain valid.
- Callers to check: any recipe or scheduled-task command line that passes a naive `--now` or a zero/negative value. The scheduled `ora-aaos-expire-leases` sweep runs `expire-leases.py --project ora-ops` with defaults only, so it is unaffected; verify with one supervised run after merge.
