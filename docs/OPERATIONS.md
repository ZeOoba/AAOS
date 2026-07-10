# AAOS Operations Runbook

This is a human-facing quick-reference for operating the ORA Agent Coordination Layer (AAOS) day to day. Audience: Gabriel (or any human operator) sitting down to check on or intervene in the queue — not an agent. It assumes the mechanics already decided in the ADRs below and does not re-argue them; it only tells you where to look and what to click/edit/run, right now, for four recurring situations. If a step here seems to contradict an ADR, the ADR wins — file a correction rather than trusting this doc over the source.

All live coordination state lives on the OneDrive seam, under:

```
C:\Users\Owner\OneDrive - Oxford Capital Partners\Claude_Workspace\AAOS\projects\ora-ops\
  tasks\<task-id>.json           Source of Truth: task state
  handoffs\<handoff-id>.json     Source of Truth: write-once handoffs
  locks\<task-id>.lock           Ephemeral: active leases
  locks\_archive\                reclaimed/retired locks — never deleted
  events\aaos-events.ndjson      Source of Truth: append-only machine event log
```

Shorthand below: paths are given relative to `Claude_Workspace\AAOS\projects\ora-ops\` unless a full path is shown. The codebase (schemas, tools, docs — this file included) lives separately in the git repo at `C:\Users\Owner\AAOS\`, outside OneDrive; only `Claude_Workspace\AAOS\` is live runtime state.

## 1. Inspect the queue

**Where the tasks live.** Every task is one file: `tasks\<task-id>.json`. Task ids follow `t-[a-z0-9-]+`. Open the file directly in an editor to see its full state — there is no database to query.

**What `state` means.** Each task's `state` field is one of:

| state | meaning |
|---|---|
| `queued` | not leased, eligible for any agent to pick up |
| `leased` | an agent has claimed it but has not yet started active work |
| `in-progress` | actively being worked |
| `blocked` | stuck on something external — check `blocked_reason` on the task |
| `done` | complete; `evidence` array should be non-empty |
| `failed` | terminated unsuccessfully |
| `archived` | retired from the live view (see §3) |

`priority` is `1` (highest) to `3`. `owner_agent` is either an agent id or the literal `unassigned`.

**Spotting a stuck or stale task.** A task is suspect when its `state` is `leased` or `in-progress` but has clearly been sitting there too long. You cannot tell this from the task file alone — a task in `leased`/`in-progress` state carries a `lease` object (`holder`, `acquired`, `expires`, `session`), but the authoritative check is the matching lock file:

1. Open `tasks\<task-id>.json` and note its `lease.expires` timestamp (if the task has no `lease` object but its state is `leased`/`in-progress`, that mismatch is itself a red flag — treat it as stuck and investigate).
2. Cross-reference `locks\<task-id>.lock` — it carries the same `expires` field independently. If the lock file is missing entirely while the task still claims `leased`/`in-progress`, that is also a stuck/orphaned state.
3. Compare `expires` to the current time. Per ADR-0003, a lock isn't officially reclaimable until `now > expires + 5 minutes` (clock-skew grace) — don't treat a task as stuck the moment it ticks past `expires`; give it the 5-minute grace window first.
4. If it's past grace, that's a candidate for §2 below.

**Where to check the human-facing view.** For a dashboard rather than raw files, use the Notion Ops Hub (`32b6ecd8`) / Cowork Session Ledger — per ADR-0001, Notion is kept as a **read-only Derived mirror** of queue status, never the primary store. Use it to scan status at a glance; always treat the filesystem (`tasks\*.json` + `locks\*.lock`) as the source of truth if the two ever disagree, and treat a mirror that looks stale or frozen as a mirror problem, not a queue problem.

**Sanity-checking any file.** Before trusting or hand-editing any AAOS JSON file, validate it:

```
python C:\Users\Owner\AAOS\tools\validate.py <path-to-file>
```

This checks the file against its schema (`C:\Users\Owner\AAOS\schemas\*.schema.json`) and rejects a `protocol_version` whose major version doesn't match what's currently supported (see §4 and `MIGRATIONS.md`). Run it against a task, lock, handoff, event, or heartbeat file any time something looks off, and again after any manual edit described in this doc, before considering the edit done.

## 2. Break a stuck lock

Use this when a lock looks stuck — past its `expires` time, holder unresponsive — but the automated `expire-leases` sweep (a Phase 3 tool; not built yet as of this writing) hasn't run, or you don't trust it to run in time. This manual procedure is exactly what that sweep will eventually automate; until it exists, or when you need to intervene ahead of its normal cadence, do it by hand.

1. **Locate the lock.** `locks\<task-id>.lock` (JSON: `protocol_version`, `task_ref`, `holder`, `acquired`, `expires`, `session`).
2. **Confirm it's genuinely expired**, not just old-looking:
   - Check `expires` against the current time — it must be past `expires` **plus the 5-minute clock-skew grace** (ADR-0003) before it's reclaimable at all.
   - Check the holder's status: look up `heartbeat.json` (the derived heartbeat projection, one row per `agent_id`) and find the entry for `lock.holder`. Per ADR-0003, a lock is only reclaimable when the holder's heartbeat `status` is `missed` or `unknown` — or the lock is simply past grace with no `LeaseRenewed` event on record for it in `events\aaos-events.ndjson`. A `fresh` or `stale`-but-still-ticking holder is not grounds to steal the lock; when in doubt, this is the fail-closed case from ADR-0003 — escalate to a human rather than proceeding, which for you as the human means: wait, or contact the holder directly, don't reclaim yet.
3. **Move the lock file — never delete it.** Move (don't copy-then-delete; an actual move) `locks\<task-id>.lock` to:
   ```
   locks\_archive\<task-id>.<timestamp>.lock
   ```
   Use a sortable timestamp (e.g. the lock's own `acquired` value, or now, in `YYYYMMDDTHHMMSS` form) so `_archive` stays chronologically browsable.
4. **Reset the task's state.** Open `tasks\<task-id>.json` and set `state` back to `"queued"`. Do this using the atomic write-rename convention used everywhere else in AAOS — never edit the live file in place:
   - Write the modified JSON to `tasks\<task-id>.json.tmp`.
   - Rename `tasks\<task-id>.json.tmp` over `tasks\<task-id>.json` (an OS-level rename/replace, not a copy).
   - Also clear or drop the task's `lease` object as part of this same write, since it's stale.
5. **Validate the result:** `python C:\Users\Owner\AAOS\tools\validate.py "...\tasks\<task-id>.json"`.

This is a manual stand-in for the automated sweep's reclaim path (ADR-0003: move to `_archive`, reset `state` to `queued`, emit `LeaseExpired`). Because you're doing this by hand and `emit-event.py` tooling may not yet be available to you interactively, be aware the automated event trail may show a gap for this reclaim — that's an expected side effect of manual intervention pre-tooling, not something to paper over by hand-editing `aaos-events.ndjson` directly (never hand-edit the event log; see ADR-0004 and §3 below).

## 3. Archive a task

The general "never delete" rule applied to a task. Appropriate when a task is `done`, has been abandoned/superseded, or otherwise needs to come out of the active view without destroying its history.

1. **Set the task to its terminal archived state first.** Edit `tasks\<task-id>.json`, set `state` to `"archived"`, and write it via the same atomic write-rename convention as §2 (write `.tmp`, then rename over the live file). Do this before moving the file, so the last live-directory copy of the task correctly reflects its final state.
2. **Move the task file out of the live directory**, mirroring the same `_archive` convention already established for locks:
   ```
   tasks\_archive\<task-id>.json
   ```
   (Create `tasks\_archive\` if it doesn't exist yet — same rule as everywhere else: create directories before writing into them, never delete.)
3. **Move any related files with it, if they still exist:**
   - Any lock file at `locks\<task-id>.lock` still present → `locks\_archive\<task-id>.<timestamp>.lock` (per §2's move procedure — a `done`/`failed` task should normally have no live lock left, but check).
   - Any handoff(s) at `handoffs\<handoff-id>.json` that reference this `task-id` → `handoffs\_archive\<handoff-id>.json` (create `handoffs\_archive\` if it doesn't exist yet — same rule as `tasks\_archive\` in step 2). Handoffs are write-once, so this is a pure relocation, not an edit.
4. **Leave the event log alone.** Do not touch `events\aaos-events.ndjson`. Every event this task ever generated (`TaskLeased`, `LeaseRenewed`, `TaskCompleted`, etc.) stays exactly where it is, in place, forever — archiving a task removes it from the *live directories* only, never from history. If you ever need this task's full story after archiving it, it's `grep`-able out of `aaos-events.ndjson` by `task_ref`, not out of `tasks\_archive\`.
5. **Validate:** run `tools/validate.py` against the moved task file at its new `_archive` path to confirm it's still schema-valid after the edit.

## 4. Roll back a protocol version

Use this when a `protocol_version` bump (e.g. `1.0` → `2.0` on one of the five schemas) turns out to be wrong and needs undoing. Per `MIGRATIONS.md`, AAOS has **no automated migration or version-negotiation tooling by design** (see `docs/NON-GOALS.md`) — this is a fully manual, documented process, and rollback is no exception:

1. **Revert the schema in git.** In the `C:\Users\Owner\AAOS\` repo, revert the affected schema file's `protocol_version` `const` back to the prior version (e.g. `2.0` → `1.0`) — a normal git revert/edit + commit of `schemas\<name>.schema.json`.
2. **Revert the validator's supported-version check.** `tools/validate.py` was updated when the version was bumped forward (per `MIGRATIONS.md`'s own bump procedure, step 3); undo that change in the same commit or a follow-up so validation once again accepts the reverted major version and rejects the abandoned one.
3. **Add a dated entry to `MIGRATIONS.md`** describing the rollback: what was reverted, back to which version, and why the forward bump was wrong. This goes in the "Version history" section alongside the original bump entry — don't delete or edit the original entry, append the correction below it so the full story (bumped, then reverted, and why) stays intact.
4. **Manually reconcile any state files already written under the now-reverted version.** This is the step with no tooling and the one most likely to be skipped: if any `tasks\*.json`, `locks\*.lock`, `handoffs\*.json`, or event records were written while `protocol_version` was at the now-abandoned version, they need field-by-field reconciliation back to the shape the reverted (prior) version expects — by hand, per whatever the forward migration actually changed. There is no script that does this for you; the field-level transformation should be documented inline in the same `MIGRATIONS.md` rollback entry (per the "one-off migration note" convention in the bump procedure) so the next person doesn't have to reverse-engineer it. Run `tools/validate.py` against every reconciled file afterward to confirm it now passes under the reverted schema.

This is intentionally manual — it is the direct, accepted cost of the project's explicit Non-Goal against building protocol-negotiation/compatibility-matrix machinery. Don't attempt to script around it during a live rollback; the documented-manual-process is the design, not a placeholder for one.

---

This doc is the "how do I, right now" quick-reference only. For the full reasoning and mechanism detail behind these procedures — thresholds, fail-closed semantics, the OneDrive conflict-copy handling, and the event-log transaction ordering — see `docs/adr/0003-locking-model.md` and `docs/adr/0004-event-log-relationship.md`.
