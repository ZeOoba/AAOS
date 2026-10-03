<!-- STAGED FILE — NOT AUTO-LOADED, NOT INSTALLED. -->
<!-- The existence of this file changes no session's behavior. Gabriel pastes -->
<!-- the relevant block into a Code session or a skill at his own discretion. -->
<!-- Per Hard Constraint: nothing is silently installed. -->

# AAOS Code Adapter

How a Claude Code session joins the ORA Agent Coordination Layer as the `claude-code` agent. This page is pointer-first — mechanics live in `docs/AAOS-SPEC.md`, exit codes in `docs/PHASE3-DESIGN.md` §0.4, human-recovery procedures in `docs/OPERATIONS.md`. Read those, not a re-explanation here.

## Preconditions
- You are a `claude-code` session on the ORA workstation.
- Tools: `C:/Users/Owner/AAOS/tools/*.py` (interpreter: `python`). Live seam: `C:/Users/Owner/OneDrive - Oxford Capital Partners/Claude_Workspace/AAOS/projects/ora-ops/`.
- Every mutating call carries `--holder claude-code --session <your-session-id>` (a stable per-session string — e.g. this session's id).

## The turn
- **Check the queue** — list and read `…/projects/ora-ops/tasks/*.json`; pick the lowest-`priority` task with `state:"queued"` addressed to you (or `owner_agent:"unassigned"`). No dedicated list command by design — the human-facing view is the Notion mirror (`tools/project-notion.py`'s output), which is Derived, not authoritative.
- **Lease** — `python tools/lease-task.py <task-id> --holder claude-code --session <s>`. Exit 0 = ACQUIRED, proceed. Exit 3 = DENIED (someone/something holds it — move on). **Exit 4 = CONTESTED — stop; this is already flagged via a `LeaseContested` event, do not retry blind.**
- **Execute** — satisfy every item in `task.acceptance_criteria`; fetch `task.inputs[]` by their `ref` pointers (never embedded data).
- **Hand off** (if passing work to another agent) — `python tools/post-handoff.py --id h-<slug> --task <task-id> --from claude-code --to <agent> --next-action "…" [--summary "…" | --summary-file <path>] [--artifact path=<p>]* [--holder claude-code --session <s>]`. Every `--artifact` path must fall inside claude-code's `write_scope` (`agent-registry.json`) or validation fails (the colonization guard). A `--blocker <str>` (repeatable) forces the task to `state:"blocked"`; it requires `--holder`/`--session` of the current lease and a `leased`/`in-progress` task, and a rejected blocker writes nothing (exit 2/3/4).
- **Complete** — `python tools/complete-task.py <task-id> --holder claude-code --session <s> --evidence label=<l> ref=<r> [--evidence …]* [--archive]`. At least one `--evidence` is required (schema's "evidence before done" rule, enforced here, not by the schema). `--archive` folds the `done→archived` transition in — this is how a full lifecycle ends without an 8th script.
- **Fail** — `python tools/fail-task.py <task-id> --holder claude-code --session <s> --reason "…" [--evidence …]*`.

## Fail-closed reminders
- Never hand-edit `…/events/aaos-events.ndjson` or any `.lock` file directly — every mutation goes through a tool.
- A DENIED/CONTESTED/lease-lost (exit 3/4) result is a stop, not a retry loop — the ≤1-retry canon already lives inside the tools (`emit_event`'s append/spool retry).
- Lease TTL is 45 minutes, not renewable via a dedicated CLI in v1 — a job that will overrun should be re-scoped into a smaller task rather than raced against expiry.
- Every "done" claim carries the completing command's own stdout as evidence — never assert success from memory.

## Escalation / recovery
Breaking a stuck lock, archiving a task, or rolling back a protocol version → `docs/OPERATIONS.md` §2–4. Do not improvise a manual fix for something a documented procedure covers.
