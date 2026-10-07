# 0002. Transport mechanics: atomic write-rename, two-tree layout, project namespacing

## Status

Accepted

## Context

Every AAOS state file must survive partial writes and OneDrive sync faults — the system must fail closed and never assume a write is fine. Paths must never require a migration when a second project is added later. The codebase must be git-versioned without putting `.git` inside the OneDrive-synced tree, per ADR-0001.

## Decision

**Atomic write idiom.** Every state-file write uses the exact idiom already implemented and verified in production at `~/.claude/hooks/fleet-session-hook.py`: write to `path + ".tmp"`, then atomically rename it into place via `os.replace(tmp, path)`, which is atomic on a single NTFS volume. No AAOS tool ever writes a state file in place. Directories are created before writing if they do not already exist.

**Two-tree layout.** The codebase and the runtime state are deliberately split across two trees — one git-versioned and outside OneDrive, one OneDrive-synced and never git-tracked:

```
Git repo (Source of Truth for code + contracts) — C:\Users\Owner\AAOS\  (outside OneDrive, not synced):
  schemas/{agent-registry,task,handoff,event,heartbeat}.schema.json
  tools/  validate.py, create-task.py, lease-task.py, complete-task.py,
          fail-task.py, post-handoff.py, expire-leases.py, emit-event.py,
          derive-heartbeat.py, project-notion.py, publish-contracts.py
  tests/
  templates/{task,handoff}.template.json, {adr,gate-review,BUILD-STATE}.template.md
  agent-registry.json            (canonical static roster, 2 entries)
  docs/   AAOS-SPEC.md, CAPABILITY-MAP.md, NON-GOALS.md, V2-BACKLOG.md,
          OPERATIONS.md, COWORK-BOOTSTRAP.md, MIGRATIONS.md, adr/0001..0006-*.md
  .gitignore   (excludes any accidental runtime state; secrets scan clean)

Seam (OneDrive-synced runtime, NO git) — Claude_Workspace\AAOS\:
  contracts/                       (DERIVED mirror, stamped)
    schemas/*.json, agent-registry.json, AAOS-SPEC.md, COWORK-BOOTSTRAP.md,
    templates/*, PUBLISHED.json {source_commit, generated_at, protocol_version}
  projects/ora-ops/
    tasks/<task-id>.json           (Source of Truth: task state)
    handoffs/<handoff-id>.json     (Source of Truth: write-once handoffs)
    locks/<task-id>.lock           (Ephemeral: leases)
    locks/_archive/                (reclaimed locks — never deleted)
    events/aaos-events.ndjson      (Source of Truth: append-only event log)
  heartbeat.json                   (Derived: projection of the health chain)
  state/BUILD-STATE.md             (build continuity)
```

`publish-contracts.py` is the single writer (originally sketched as `.ps1`; built in Python, see PHASE4-DESIGN.md) of `contracts/` — a deploy/mirror step that reuses the existing "brain mirror" deploy idiom already used elsewhere in ORA's stack. The docs originally staged during Phase 0 under `Claude_Workspace\AAOS\docs\` are relocated to the git tree as canonical; the OneDrive seam keeps only the published mirror, never the editable source.

**Project namespace.** All queue, handoff, lock, and event paths carry a `projects/<project-id>/` path segment. Exactly one project is seeded now: id `ora-ops`, name "ORA Cross-Surface Operations" — the inaugural home for the real Code↔Cowork operational handoffs that Phase 0 already documented as live friction, such as a cross-fleet scheduled-task toggle that Gabriel decided on but that got stuck because the Code session's bound MCP tool could only reach one of the two fleets. No multi-project machinery is built now; the path convention alone means a second project needs no migration later.

## Consequences

Contracts exist in two places by design: git is the editable home, and the OneDrive seam is a stamped, read-only Derived mirror. This is an explicit Source-of-Truth-to-Derived relationship, not a duplicated fact — "one fact, one home" is preserved because only one of the two copies is ever hand-edited. `heartbeat.json` and `state/` sit above `projects/` in the seam tree because they are agent-scoped or build-scoped, not task-scoped.
