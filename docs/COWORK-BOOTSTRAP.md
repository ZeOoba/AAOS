# AAOS Cowork Bootstrap

## §0 — What this is

You are a Claude Cowork session joining Oxford Realty Advisors' Agent Coordination Layer (AAOS) as the `cowork` agent. This document is self-contained — everything you need is either stated here or a file at a path given here. **Do the steps below in order. Do not improvise around a step that fails — fail closed and report (§3), never guess or proceed on an assumption.**

## §1 — Constants

- **Seam root:** `C:/Users/Owner/OneDrive - Oxford Capital Partners/Claude_Workspace/AAOS/`
- **Project directory:** `<seam root>/projects/ora-ops/`, containing `tasks/`, `handoffs/`, `locks/` (with `locks/_archive/`), `events/aaos-events.ndjson`, `events/aaos-events.ndjson.pending/`
- **Contracts mirror:** `<seam root>/contracts/` (schemas under `contracts/schemas/`, everything else flat, plus `contracts/templates/`)
- **Git tree (may or may not be reachable from you — §2 finds out):** `C:/Users/Owner/AAOS/`
- **Your agent id:** `cowork`. **Your write_scope** (from `agent-registry.json`): `…/Claude_Workspace/**` and `notion://**` only — you may never write anywhere else, including the git tree even if you can read it.

## §2 — Capability-detection preamble (run first, every turn)

Three probes, in order. Record the outcome of each — you will state them in your §9 report.

**Probe S — Seam reachability (mandatory gate).** Read `<seam root>/contracts/PUBLISHED.json`. Not readable? Try `<seam root>/_seam-probe.md` as a fallback sentinel. **Neither readable → STOP at §3 now** ("not-found" or "permission" class, whichever applies). `PUBLISHED.json` readable but `protocol_version` is not `"1.0"` → **STOP at §3** ("malformed" class — protocol mismatch).

**Probe G — Contracts source.** Try reading `C:/Users/Owner/AAOS/schemas/task.schema.json` (the git tree) as a plain file. Succeeds → `CONTRACTS_SOURCE = git-tree` (read your contracts from there for the rest of this turn — it's the freshest copy). Fails → `CONTRACTS_SOURCE = seam-mirror` (read from `<seam root>/contracts/` instead). If `CONTRACTS_SOURCE` resolves to `seam-mirror` but `<seam root>/contracts/schemas/` is missing or its files don't parse → **STOP at §3** ("malformed" — no usable schemas anywhere).

**Probe X — Execution.** Attempt this exact command as a real subprocess/shell invocation, if you have any facility to run one:
```
python "C:/Users/Owner/AAOS/tools/validate.py" "<seam root>/contracts/agent-registry.json" --schema agent-registry
```
Exit code `0` → `MODE = FULL-EXEC`. Anything else — non-zero exit, an error, or no shell/Python facility available to you at all — → `MODE = SAFE-BY-HAND`. **Do not guess your own mode from general knowledge of what Cowork sessions can do — actually attempt the command and observe the real result.** If you have no way to attempt it at all (no code-execution tool exposed to you in this session), that itself means `MODE = SAFE-BY-HAND` — record that plainly rather than skipping the probe.

You now have `MODE` and `CONTRACTS_SOURCE`. State both explicitly in your §9 report — a human reading your report must be able to tell which path you took without re-deriving it.

## §3 — Seam unreadable → stop, fail closed

Three failure classes. On any of them: **write nothing, take no protocol action, stop, and report** using the template below.

- **not-found** — the expected path simply doesn't exist (seam not synced yet, or `contracts/` never published).
- **permission** — the path exists but reading it errors.
- **malformed** — the path reads but doesn't parse as JSON / valid content, or a `protocol_version` doesn't match `"1.0"`, or a specific task/handoff you were told to act on won't parse.

**Report template (state this to the human/session that invoked you — never send it externally):**
> `AAOS STOP (fail-closed). Could not proceed: <path> — <not-found | permission-denied | malformed: <detail>>. Expected: <what you were trying to read/do>. I have written nothing and taken no protocol action. Resolve the seam/contracts and re-invoke.`

## §4 — Read the contracts

From `CONTRACTS_SOURCE` (§2), read:
- `AAOS-SPEC.md` — the model. Read it before doing anything else below; it explains the state machine, the schemas, and why the rules in §7–§8 exist, not just what they are.
- `schemas/handoff.schema.json` and `schemas/event.schema.json` — the exact shapes you will write against in §7.
- `agent-registry.json` — confirms your own `write_scope` and every valid agent id (so `to_agent`/`from_agent` values you write are real).
- `templates/handoff.template.json` — a filled, schema-valid example handoff to pattern-match against.

Any of these missing/unparseable under your resolved `CONTRACTS_SOURCE` → §3 stop.

## §5 — Read the queue

List `<seam root>/projects/ora-ops/tasks/`. Read each task; find the one addressed to you — `owner_agent:"cowork"` — or read `<seam root>/projects/ora-ops/handoffs/` for one with `to_agent:"cowork"`. Read that handoff in full, and read any file its `artifact_manifest[].path` points to. **If the specific task/handoff you were invoked to act on is missing or malformed → §3 stop**, naming exactly which file.

## §6 — Execute the turn (branch on `MODE`)

- **`MODE = FULL-EXEC`:** run the real tools, exactly as the Code adapter does, with `--holder cowork --session <your-cowork-session-id>`: `lease-task.py` → do the work → `post-handoff.py` and/or `complete-task.py`/`fail-task.py`. Full parity with Code; §7–§8 below do not apply to you in this mode.
- **`MODE = SAFE-BY-HAND`:** proceed to §7 only. Do not attempt any operation not listed there.

## §7 — SAFE-BY-HAND write recipes

**Governing rule, memorize it exactly:** *you may READ anything; you may WRITE only by creating a brand-new file at an id/name you invent that nobody else will ever write to; you may never edit an existing shared file in place, and you may never create a file at a predictable, contested name (a lock).* Every new file you write, you self-check field-by-field against its schema before treating the write as final — get the shape right by hand since no validator is available to you in this mode.

**Recipe 1 — Post a return handoff.** Write `<seam root>/projects/ora-ops/handoffs/h-<your-fresh-slug>.json` (pick a slug nobody else has used — include a random suffix if unsure) with exactly these fields (`handoff.schema.json`'s `required` set, `additionalProperties:false` — no extra fields):
```json
{
  "protocol_version": "1.0",
  "id": "h-<your-fresh-slug>",
  "project_id": "ora-ops",
  "task_ref": "<the task id you're responding to>",
  "from_agent": "cowork",
  "to_agent": "<usually claude-code>",
  "artifact_manifest": [],
  "context_summary": "<<=300 words, pointer-first: what you read and concluded, not embedded data>",
  "required_next_action": "<what the receiver should do next>",
  "blockers": [],
  "posted_at": "<ISO-8601 now, e.g. 2026-07-10T14:00:00-04:00>"
}
```
**Keep `artifact_manifest` empty and `blockers` empty, always, in this mode** — see §8 for why. `id` must match `^h-[a-z0-9-]+$`. Never write to a `handoffs/<id>.json` path that already exists (write-once — pick a new id instead).

**Recipe 2 — Emit the paired event (spool file only).** Write ONE new file at `<seam root>/projects/ora-ops/events/aaos-events.ndjson.pending/<sortable-timestamp>-cowork-<random>.json` (e.g. `20260710T140005123456-cowork-a1b2.json` — the point is a unique, sortable name; do not overwrite an existing spool file) containing exactly one JSON object:
```json
{
  "protocol_version": "1.0",
  "event_id": "e-<ISO now>-<random digits>",
  "type": "HandoffPosted",
  "actor": "cowork",
  "timestamp": "<same ISO now>",
  "project_id": "ora-ops",
  "refs": {"task": "<task id>", "handoff": "<the handoff id from Recipe 1>"}
}
```
`event_id` must match `^e-[0-9TZ:.+-]+-[0-9]+$` — a real ISO timestamp followed by `-` and digits only (no letters) for the tail. `type` must be one of the fixed set in `event.schema.json` (`TaskCreated, TaskLeased, LeaseRenewed, LeaseExpired, LeaseDenied, LeaseContested, HandoffPosted, Blocked, Completed, Failed, HeartbeatMissed, Escalated, Archived`) — you will almost always use `HandoffPosted`. **Never open `aaos-events.ndjson` itself and never append a line to it.** Dropping this file in `…pending/` *is* the durable, committed record — a Code session's next drain folds it into the shared log under a lock you don't need to worry about.

**Recipe 3 — Create a brand-new task (rare in v1, but safe).** Same governing rule: pick a fresh `t-<slug>` id nobody else has used, write it once to `tasks/<id>.json` per `task.schema.json` with `state:"queued"`, `owner_agent` set appropriately, `acceptance_criteria` non-empty. Pair it with a `TaskCreated` spool event (Recipe 2's shape, `type:"TaskCreated"`, `refs:{"task": "<id>"}`).

## §8 — What you must NOT do by hand (one honest reason each)

- **Create or edit a `.lock` file.** Exactly-one-winner leasing needs an OS-level exclusive-create you don't have — a hand-written lock is last-write-wins, which is precisely the double-lease corruption AAOS exists to prevent.
- **Edit an existing `tasks/*.json` in place** (including to set it `leased`, `done`, `failed`, or `blocked`). It's a shared file with a single legitimate writer at a time (whoever holds its lease); your edit could silently clobber a concurrent legitimate writer. This is also why Recipe 1 always leaves `blockers: []` — setting a blocker is defined as forcing the referenced task to `state:"blocked"`, which is exactly this forbidden shared-file mutation.
- **Attempt `lease-task`, `complete-task`, `fail-task`, or `expire-leases` "by hand."** Each requires either the exclusive-create primitive above, or `assert_lease_held` (a check you cannot have legitimately satisfied without having run the real lease-acquire code), or is a fleet-wide concurrency-critical sweep that is Code's/the scheduler's job, never an individual turn's.
- **Append to `aaos-events.ndjson` directly**, even if it looks like "just adding a line" — two writers appending near-simultaneously can silently clobber each other's line (this was a real bug found and fixed in Phase 3). The spool file (Recipe 2) is the only safe path.
- **Edit `_seam-probe.md` in place.** It's a shared file too. Your Recipe-1 return handoff, referencing that you read it, *is* the machine-checkable proof that the seam works both ways — a human or Code session transcribes that confirmation elsewhere; you don't edit the probe file yourself.
- **List an artifact path outside `…/Claude_Workspace/**` or `notion://**`** in any manifest — that's outside your `write_scope` and would fail the colonization guard if a validator ever checked it (none does in this mode, so the discipline is on you).

**If your assigned work genuinely requires one of the above:** post a handoff back to `claude-code` (Recipe 1) describing exactly what needs doing and why you can't do it yourself, and stop there. That is the *correct* outcome in this mode, not a failure to work around.

## §9 — Report back

End every turn stating, plainly: the resolved `MODE` and `CONTRACTS_SOURCE`; what you read; the exact paths of any new files you wrote; and any §3 stop or §8 deferral, with the reason. This report is internal — read by the human or the next session — never an external send.
