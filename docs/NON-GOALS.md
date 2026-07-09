# AAOS Non-Goals

These are binding — any design or code proposing to cross one of these lines routes to `V2-BACKLOG.md` with a one-line justification instead. Re-checked adversarially at every phase gate.

1. **Distributed/multi-machine execution.** All agents run on one Windows machine or Anthropic-hosted surfaces. No network transport, no remote workers, no containers.
   Tie-in: The Phase 0 capability map's integration seam is a single-machine OneDrive-synced folder (`Claude_Workspace\`), evidenced as reachable by both Code and Cowork on this one workstation (live two-way confirmation still pending, see `CAPABILITY-MAP.md` §3); there is no second machine in scope, and the machine itself has a hard 5.7GB RAM ceiling that rules out running distributed infrastructure locally.

2. **Message buses, queues-as-services, microservices.** The filesystem + Git + Notion are the transport. Full stop.
   Tie-in: ADR-0001 chose the shared-folder-as-bus architecture specifically because it extends five already-proven ORA mechanisms (chip-claims.log, append-event-log.py, fleet-session-hook.py's atomic write-rename, the existing probe/gate validators, Notion as a read-only mirror) rather than introducing new infrastructure this one-workstation environment has no operational need for.

3. **Plugin marketplace / dynamic capability negotiation.** The agent registry is a static, hand-edited JSON file with ~2-5 entries. Capability "discovery" is reading that file.
   Tie-in: capability map #1 (agent registry) is scored BUILD specifically because nothing existing offers this, and it is deliberately scoped down to the minimum — a tiny static roster (seeded with exactly 2 entries: claude-code, cowork) — rather than a general plugin system.

4. **Artifact quality-scoring engines.** The existing 16-point QC Gate and repo gates are the quality system. Do not build a parallel grader.
   Tie-in: capability map #10 identified a full, already-live governance/drift-detection/validator ecosystem (governor-drift-probe.py, doctrine-integrity-probe.py, the eos-*.py hook chain) — AAOS extends this validator philosophy for its own state files, it does not build a competing quality system.

5. **Protocol version negotiation machinery.** A single `protocol_version` field in every schema, plus a `MIGRATIONS.md`, is sufficient. No compatibility matrices.
   Tie-in: capability map #13 (protocol/schema versioning) is the smallest of the three BUILD items — genuinely novel to this environment, but intentionally trivial: one field plus one file.

6. **A second memory system.** Memory stays in `MEMORY.md` / `event_log.md` / session context. AAOS state files hold coordination state only (queue, leases, handoffs, heartbeats).
   Tie-in: capability map #8 scored memory persistence REUSE outright — AAOS's own state files (task.json, handoff.json, etc.) hold zero facts about the firm, deals, or tenants; only coordination metadata.

7. **A second scheduler.** The dual fleet (CC/MCP + LAMS/Cowork) remains the only scheduler. AAOS tasks that need recurrence are registered there via the supervised tool.
   Tie-in: capability map #9 scored the scheduler REUSE outright — the mature dual-fleet registry (6 CC/MCP + 18 LAMS/Cowork tasks live as of Phase 0) with its single-writer `create_scheduled_task` registration doctrine is used as-is; AAOS's lease-expiry sweep will be staged for registration there in Phase 3, never auto-registered, never given a competing scheduler of its own.

On vendor-agnosticism: it is achieved entirely by the contracts (the five JSON schemas plus `agent-registry.json`'s `write_scope` field), not by machinery. Any future agent that can read/write JSON at the seam and honor the five schemas can join by adding one registry entry with a `write_scope`. That is the whole extensibility story — nothing more is built to support it now.
