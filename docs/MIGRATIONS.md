# AAOS Protocol Migrations

AAOS schemas carry a `protocol_version` field (currently `"1.0"` in all five schemas: `agent-registry`, `task`, `handoff`, `event`, `heartbeat`). Per the project's Non-Goals, there is no compatibility-matrix or negotiation machinery — this file is the entire versioning mechanism, plus the `protocol_version` field itself.

**Rule:** `tools/validate.py` rejects any AAOS file whose `protocol_version` major version does not match the current supported major version, with an error message pointing here.

**How to bump the protocol version:** when a schema needs a breaking change, (1) increment the major version (e.g. `1.0` → `2.0`) in the relevant schema's `const`, (2) add a dated entry below describing exactly what changed and why, (3) update `tools/validate.py`'s supported-version check, (4) if existing state files need to keep working, write a one-off migration note here describing the field-by-field transformation — there is no automated migration tooling in v1; this is a manual, documented process by design.

## Version history

### 1.0 — 2026-07-09 (initial)
Initial release of all five schemas (`agent-registry`, `task`, `handoff`, `event`, `heartbeat`) as part of AAOS Phase 1. No prior version exists; nothing to migrate from.
