# AAOS Operations Runbook

This is a human-facing quick-reference for operating the ORA Agent Coordination Layer (AAOS) day to day. Audience: Gabriel (or any human operator) sitting down to check on or intervene in the queue — not an agent. It assumes the mechanics already decided in the ADRs below and does not re-argue them; it only tells you where to look and what to click/edit/run, right now, for five recurring situations: four step-by-step procedures below (§1-4), plus a failure-mode index (§5) that maps every scenario this project's failure-injection tests exercise to one of those four procedures or to its own short recovery note. If a step here seems to contradict an ADR, the ADR wins — file a correction rather than trusting this doc over the source.

## Symptom to command

| You see | Check | Go to |
|---|---|---|
| A lease is past its `expires` and nothing has moved | `python C:\Users\Owner\AAOS\tools\expire-leases.py --dry-run` | section 2, then 5.1 |
| A task file was hand-edited or fails to parse | `python C:\Users\Owner\AAOS\tools\validate.py "C:\Users\Owner\OneDrive - Oxford Capital Partners\Claude_Workspace\AAOS\projects\ora-ops\tasks\<task-id>.json"` (`JSON PARSE ERROR` = corrupt) | 5.2 |
| `lease-task.py` exits `DENIED` (3) or `CONTESTED` (4) | Nothing: normal under contention; re-read the task state before retrying | 5.3 |
| Tools fail with a seam/usage error, or the seam folder is missing | Is `Claude_Workspace\AAOS` reachable? Is OneDrive sync paused? Is `--seam-root` right? | 5.4 |
| The Notion mirror looks old | Read `generation.stale` in `C:\Users\Owner\AAOS\tools\project-notion.py` output; do not eyeball timestamps | 5.5 |
| `protocol_version mismatch` from `validate.py` | Regenerate the file with current tooling; for a whole-schema bump see section 4 | 5.6, section 4 |
| Files accumulate in `events\aaos-events.ndjson.pending\` | `python C:\Users\Owner\AAOS\tools\emit-event.py --drain-only --project ora-ops`; if they keep accumulating, suspect the seam first | 5.7, 5.4 |
| A stray `<name>.tmp-<pid>` file sits next to a state file | `python C:\Users\Owner\AAOS\tools\validate.py <full path of the real file>`; `VALID` means the temp file is an abandoned write | 5.8 |
| `post-handoff.py` exits 1 with a write-scope message | Compare the artifact path with the sender's `write_scope` in `agent-registry.json` | 5.9 |
| You need to retire an in-flight (leased) task | Same procedure as a finished task | section 3, 5.10 |

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

Use this when a lock looks stuck — past its `expires` time, holder unresponsive — but the automated `expire-leases` sweep (built, and scheduled on the CC/MCP fleet as `ora-aaos-expire-leases` since 2026-07-10, cadence and cron: `list_scheduled_tasks` is the source of truth, not this doc; cadence history in docs/README.md) hasn't run yet, or you don't trust it to run in time. This manual procedure is what that sweep automates on its own cadence; use it to intervene ahead of that cadence, or if the scheduled sweep itself looks stuck.

1. **Locate the lock.** `locks\<task-id>.lock` (JSON: `protocol_version`, `task_ref`, `holder`, `acquired`, `expires`, `session`).
2. **Confirm it's genuinely expired**, not just old-looking:
   - Check `expires` against the current time — it must be past `expires` **plus the 5-minute clock-skew grace** (ADR-0003) before it's reclaimable at all.
   - Reclamation itself is gated on expiry alone — `now > expires + 5 minutes` with no `LeaseRenewed` event on record is sufficient, full stop; you do not need to also check the heartbeat before reclaiming. (**Renewal is specified but UNBUILT**: no tool in `tools/` renews a lease or emits `LeaseRenewed`, so today `expires` is always `acquired + ttl` and never moves; "no renewal on record" is therefore always true. A past-grace lock means either the job finished without releasing it, or the holder is gone — see ADR-0006 "Lease renewal".)
   - The heartbeat matters for a second, separate question — *how* to log the reclaim, not *whether* to do it. Look up `heartbeat.json` (the derived heartbeat projection, one row per `agent_id`) and find the entry for `lock.holder`: `missed`/`unknown` → this is a `DeadAgent`-class reclaim, worth an escalation note; `fresh`/`stale` → routine `ExpiredLapsed` reclaim, no escalation needed. Either way, proceed with the reclaim below — the classification only changes how loudly you flag it.
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

1. **Set the task to its terminal archived state first.** Edit `tasks\<task-id>.json`, set `state` to `"archived"`, and write it via the same atomic write-rename convention as §2 (write `.tmp`, then rename over the live file). Do this before moving the file, so the last live-directory copy of the task correctly reflects its final state. **If the task still carries a `lease` object** (you're archiving a `leased`/`in-progress` task, not just a `done` one — this is the Phase 5 "rollback drill" case), drop the `lease` field as part of this same write: an `archived` task must never carry a lease (same rule as `blocked`, per `task.schema.json`'s "lease present iff leased/in-progress" invariant).
2. **Move the task file out of the live directory**, mirroring the same `_archive` convention already established for locks:
   ```
   tasks\_archive\<task-id>.json
   ```
   (Create `tasks\_archive\` if it doesn't exist yet — same rule as everywhere else: create directories before writing into them, never delete.)
3. **Move any related files with it, if they still exist:**
   - Any lock file at `locks\<task-id>.lock` still present → `locks\_archive\<task-id>.<timestamp>.lock` (per §2's move procedure — a `done`/`failed` task should normally have no live lock left, but check).
   - Any handoff(s) at `handoffs\<handoff-id>.json` that reference this `task-id` → `handoffs\_archive\<handoff-id>.json` (create `handoffs\_archive\` if it doesn't exist yet — same rule as `tasks\_archive\` in step 2). Handoffs are write-once, so this is a pure relocation, not an edit.
4. **Leave the event log alone.** Do not touch `events\aaos-events.ndjson`. Every event this task ever generated (`TaskLeased`, `TaskCompleted`, etc.) stays exactly where it is, in place, forever — archiving a task removes it from the *live directories* only, never from history. If you ever need this task's full story after archiving it, it's `grep`-able out of `aaos-events.ndjson` by `task_ref`, not out of `tasks\_archive\`.
5. **Validate:** run `tools/validate.py` against the moved task file at its new `_archive` path to confirm it's still schema-valid after the edit.

## 4. Roll back a protocol version

Use this when a `protocol_version` bump (e.g. `1.0` → `2.0` on one of the five schemas) turns out to be wrong and needs undoing. Per `MIGRATIONS.md`, AAOS has **no automated migration or version-negotiation tooling by design** (see `docs/NON-GOALS.md`) — this is a fully manual, documented process, and rollback is no exception:

1. **Revert the schema in git.** In the `C:\Users\Owner\AAOS\` repo, revert the affected schema file's `protocol_version` `const` back to the prior version (e.g. `2.0` → `1.0`) — a normal git revert/edit + commit of `schemas\<name>.schema.json`.
2. **Revert the validator's supported-version check.** `tools/validate.py` was updated when the version was bumped forward (per `MIGRATIONS.md`'s own bump procedure, step 3); undo that change in the same commit or a follow-up so validation once again accepts the reverted major version and rejects the abandoned one.
3. **Add a dated entry to `MIGRATIONS.md`** describing the rollback: what was reverted, back to which version, and why the forward bump was wrong. This goes in the "Version history" section alongside the original bump entry — don't delete or edit the original entry, append the correction below it so the full story (bumped, then reverted, and why) stays intact.
4. **Manually reconcile any state files already written under the now-reverted version.** This is the step with no tooling and the one most likely to be skipped: if any `tasks\*.json`, `locks\*.lock`, `handoffs\*.json`, or event records were written while `protocol_version` was at the now-abandoned version, they need field-by-field reconciliation back to the shape the reverted (prior) version expects — by hand, per whatever the forward migration actually changed. There is no script that does this for you; the field-level transformation should be documented inline in the same `MIGRATIONS.md` rollback entry (per the "one-off migration note" convention in the bump procedure) so the next person doesn't have to reverse-engineer it. Run `tools/validate.py` against every reconciled file afterward to confirm it now passes under the reverted schema.

This is intentionally manual — it is the direct, accepted cost of the project's explicit Non-Goal against building protocol-negotiation/compatibility-matrix machinery. Don't attempt to script around it during a live rollback; the documented-manual-process is the design, not a placeholder for one.

## 5. Failure-mode quick reference

This project's Phase 5 failure-injection tests exercise ten specific ways AAOS can go wrong. Six of them are already fully handled by §1-4 above and just need a pointer; the other four don't have a home yet, so they get a real paragraph here.

1. **Agent dies mid-lease.** The lease simply ages past its `expires` timestamp with no renewal. This is §2, Break a stuck lock — go there directly.

2. **Corrupt/hand-mangled task file.** Look for this when a task file has been touched outside the normal tooling — a manual edit that left invalid JSON, a bad merge, a sync fault. Running
   ```
   python C:\Users\Owner\AAOS\tools\validate.py tasks\<task-id>.json
   ```
   against it prints `JSON PARSE ERROR: <path>: <detail>` and exits 1 — a distinct message shape from the enumerated schema-violation list you'd get on a well-formed-but-invalid file. Internally this is `_aaos_common.load_task`/`load_json` raising `CorruptStateError` (fail-closed) rather than returning a list of schema errors to weigh. Don't guess-fix the JSON by hand: a partially-repaired file you can't fully verify is worse than a file you know is broken, because you can no longer trust *any* field in it, including the ones that still look fine. Move it — same never-delete convention as everywhere else in this doc — to `tasks\_archive\<task-id>.corrupt-<timestamp>.json` (sortable `YYYYMMDDTHHMMSS` timestamp, same as §2's lock-archive convention), and note somewhere durable (your own record, the event log, wherever you're tracking the intervention) when and how you found it. If the task's work still needs doing, don't try to resurrect the original id — create a fresh task with `create-task.py` (`--title`, `--accept`, `--slug` or `--id`) and reference the corrupt file's archived path in the new task's `--input` or acceptance criteria so the context isn't lost. Critically, one corrupt task file never blocks operations on any other task: every AAOS tool opens the specific file it needs (`tasks\<task-id>.json`), never the whole `tasks\` directory, so `lease-task.py` or `complete-task.py` against any other task id proceeds completely normally while the corrupt one sits there flagged.

3. **Two writers race for one task.** This is self-resolving by design, not something to fix. `lease-task.py`'s lease acquisition guarantees exactly one caller lands on `ACQUIRED` (exit 0); the loser gets a clean `DENIED` (exit 3) or `CONTESTED` (exit 4) exit with its own event (`LeaseDenied`/`LeaseContested`) on record — never a crash, never a corrupted lock file. No manual recovery is needed. The only thing to know is that a `DENIED`/`CONTESTED` exit from `lease-task.py` is normal operation under contention, not a bug to chase — don't retry blind against the same task without first re-checking its `state` per §1.

4. **Seam unavailable.** This is what it looks like when the OneDrive-synced `Claude_Workspace\AAOS\` tree itself is unreachable — sync paused, the folder renamed or moved, or `--seam-root` pointed somewhere that doesn't exist. Every AAOS tool resolves paths from the seam root first (`--seam-root`, else the `AAOS_SEAM_ROOT` env var, else the built-in default under `Claude_Workspace\AAOS`) before doing anything else, so the failure shows up immediately and uniformly: the read fails, `_aaos_common` raises `UsageError`, and every CLI wrapper (`create-task.py`, `lease-task.py`, `post-handoff.py`, `complete-task.py`, `expire-leases.py`, `validate.py`) prints `USAGE ERROR: ...` to stderr and exits 2 — distinctly not a validation failure (exit 1) and not a schema problem. The correct human response is to fix the seam, not the command: resume OneDrive sync, restore the directory to its expected path, or check that `--seam-root` (or `AAOS_SEAM_ROOT`) wasn't passed wrong. Never work around a missing seam by pointing a tool at some other directory ad hoc, even temporarily — that silently forks the coordination state, since a task leased or completed against a stand-in directory is invisible to every other agent still reading the real seam, and reconciling the two afterward is exactly the manual, error-prone cleanup this design exists to avoid. Every AAOS tool already fails closed on this by design; there is no tool-side recovery action here, only an environment fix.

5. **Stale Notion mirror (>7 days).** Already covered in §1: the Notion Ops Hub / Cowork Session Ledger is a read-only Derived mirror, and one that looks stale or frozen is a mirror problem, not a queue problem — the filesystem stays the source of truth regardless. One thing to add: don't eyeball a timestamp to decide staleness — `tools/project-notion.py`'s own output stamps `generation.stale: true` directly when its newest input file is more than 7 days old, so check that flag rather than guessing from how old a row looks.

6. **Protocol version mismatch.** If a whole schema's `protocol_version` got bumped and needs undoing, that's §4, Roll back a protocol version — follow that procedure. The more common case is smaller: a single file carrying a stale or simply wrong `protocol_version` value. `tools/validate.py` catches this before it even runs full schema validation and rejects it with a distinct `protocol_version mismatch` message that names `docs/MIGRATIONS.md`. That's normal validator behavior doing its job, not a bug — and the fix is almost always to regenerate the file with the current tooling (`create-task.py`, `post-handoff.py`, etc. always stamp the current version) rather than hand-editing the file's `protocol_version` field just to make the rejection go away, which would leave the rest of the file's shape unverified against the version it now falsely claims.

7. **Event-log append failure.** `emit_event`'s durability guarantee doesn't run through the shared NDJSON directly — it commits first to a uniquely-named file under `events\aaos-events.ndjson.pending\` (`<time_ns>-<pid>-<uuid4hex>.json`), and only then makes a best-effort attempt to fold that file, and anything else pending, into `events\aaos-events.ndjson` itself. So if the shared-log append fails, or loses the race for the exclusive drain lock, the event isn't lost — it's durably parked in `…pending\`. The next successful `emit_event` call from any tool, or an explicit
   ```
   python C:\Users\Owner\AAOS\tools\emit-event.py --drain-only --project ora-ops
   ```
   folds all pending files (oldest-first, deduped by `event_id`) into the shared log. A handful of files sitting in `…pending\` at any moment is normal — drains happen constantly as a side effect of ordinary tool use and clear fast. What's actually worth acting on is files *accumulating* there: that usually means every drain attempt is failing. One exception: a single file that cannot be read (a permission or sharing-violation fault that does not clear) stays in `…pending\` on its own while other files fold normally, and each drain prints `WARNING: pending event file <name> could not be read ...; leaving it in pending/`; fix that file's access and it folds on the next drain. Otherwise it means every drain attempt is failing, which points to a deeper seam-write problem, not something specific to the event log — check §4 (Seam unavailable) above first. Also glance in `events\aaos-events.ndjson.pending\_corrupt\`: malformed pending files land there (never silently discarded) with a stderr warning naming the file. A file that does not decode or parse first stays in `…pending\` while its mtime is within ±10 minutes (`QUARANTINE_GRACE_SECONDS`) of now, so up to about 20 minutes for a file stamped in the future with a `may still be syncing` warning, in case OneDrive is still delivering it. Never hand-edit `aaos-events.ndjson` to paper over a gap; once the underlying write problem is resolved, a spooled file drains and folds in correctly on its own.

8. **Partial write / interrupted sync.** AAOS never writes a live state file in place — every write goes to a uniquely-named temp file first (`<real-filename>.tmp-<pid>`) and only lands on the real filename via an OS-level rename/replace. A crash, a killed process, or an interrupted OneDrive sync mid-write leaves that stray `.tmp-*` file sitting *next to* the real file, never in place of it — its mere existence is proof the atomic-write design worked, not evidence of damage. Confirm that before touching anything: run
   ```
   python C:\Users\Owner\AAOS\tools\validate.py <path-to-the-real-file>
   ```
   against the real (non-`.tmp`) file and confirm it still comes back `VALID` — that's the prior, fully-written version, untouched by the interrupted write. Once you've confirmed that, the stray `.tmp-*` file is safe to delete — it was never live state, just an abandoned write attempt that no reader ever saw. The one case to slow down on: if you have a specific reason to think the `.tmp-*` file might actually hold *newer* information than the real file (for instance, you're the one who killed the process mid-edit and remember what you were changing), don't guess-merge it back in — move it aside outside the live directories and reconcile the content by hand.

9. **Write-scope violation.** `tools/post-handoff.py` validates its handoff through the same machinery as `tools/validate.py`, which enforces that every `artifact_manifest[].path` in the handoff falls inside at least one of the `from_agent`'s `write_scope` globs declared in `agent-registry.json` (`C:\Users\Owner\AAOS\agent-registry.json` by default) — a rejection here is a distinct write-scope violation (exit 1), not a generic schema error. It usually means one of two things: either the artifact path genuinely belongs to a different agent's territory (the handoff itself is wrong — fix the artifact list, not the registry), or the acting agent's `write_scope` entry is stale or too narrow for a legitimate new location it should now be able to reach (the registry needs a deliberate, reviewed update, not a quick patch). Check which case it is before touching either file — never widen a `write_scope` glob casually just to make a specific error go away; it's the load-bearing ownership-boundary control between agents.

10. **Rollback drill — archiving an in-flight (leased) task.** This is §3, Archive a task — the same procedure, run against a task that happens to be `leased` or `in-progress` rather than `done`. §3 step 3 already covers it: "any lock file at `locks\<task-id>.lock` still present" is exactly what an actively-leased task has, and moving that lock to `locks\_archive\<task-id>.<timestamp>.lock` as part of the archive is what releases the lease. There is no separate "release the lease first" step to remember — archiving the lock file already accomplishes that.

---

**What CI proves, and what it does not.** The CI suite runs on Linux and (as an informational, non-required leg) on `windows-latest`, so it proves NTFS/Win32 and POSIX file semantics only. It does NOT prove behaviour on the OneDrive-synced seam: conflict copies, Files-On-Demand dehydration, cross-surface exclusive-create and hardlink behaviour in a synced folder are UNVERIFIED until the planned OneDrive contention soak (V1). Known defects in the lock/lease code are pinned by `@known_defect` tests (`tests/_known_defect.py`) in `tests/test_drain_lock_interleaving.py` and `tests/test_windows_fs_semantics.py`. They pass while the defect is present and emit a `KNOWN DEFECT still present` warning; when a fix makes every pinned invariant hold, the test FAILS until the decorator is removed. Unlike `unittest.expectedFailure`, an unrelated error (a renamed function, a changed signature, a hook that never fires) is a real failure, not a silent pass.

This doc is the "how do I, right now" quick-reference only. For the full reasoning and mechanism detail behind these procedures — thresholds, fail-closed semantics, the OneDrive conflict-copy handling, and the event-log transaction ordering — see `docs/adr/0003-locking-model.md` and `docs/adr/0004-event-log-relationship.md`. §5's ten-scenario mapping traces back to the failure-injection matrix in `docs/PHASE5-DESIGN.md`; that phase's own test suite is where each scenario is actually proven, not just asserted here.
