# AAOS Phase 3 — Coordination Layer: Implementation Design

**Status:** implementation-level design, produced by an opus-tier planning pass before any Phase 3 code was written. Implementers: read this in full before touching `tools/`. This is the single source of truth for Phase 3's function signatures, CLI contracts, exit codes, and test strategy — do not improvise around it; if something here conflicts with a schema or ADR, flag it rather than silently picking one.

## 0. Cross-cutting decisions that bind every script (read first)

**0.1 Runtime state root = the seam, not the git tree.** All tasks/handoffs/locks/events/heartbeat live under the OneDrive seam so Cowork (whose `reachable_paths`/`write_scope` cover only `…/Claude_Workspace/**` and `notion://**`, never `C:/Users/Owner/AAOS/**`) can participate. The canonical root is:

```
SEAM_ROOT = os.environ.get("AAOS_SEAM_ROOT",
    r"C:/Users/Owner/OneDrive - Oxford Capital Partners/Claude_Workspace/AAOS")
```

Every script also accepts `--seam-root` (overrides env, which overrides the default). This single env/flag is the primary testability lever for filesystem location: tests point `AAOS_SEAM_ROOT` at a `tmp_path`.

> **Known pre-existing Phase-2 drift, do not "fix" silently:** `examples/t-sample-001.json` (`history_ref`) and `examples/h-sample-001.json` / `templates/handoff.template.json` (`artifact_manifest[].path`) reference runtime paths under the **git tree** `C:/Users/Owner/AAOS/projects/…`. That location is unreachable by Cowork and is not where runtime state lives. Phase-3 scripts must resolve all runtime paths under `SEAM_ROOT`. Write-scope validation still passes either way because `claude-code`'s scope covers both trees. Leave the Phase-2 examples as-is (they passed Gate 2); just don't propagate their path convention into Phase 3 code.

**0.2 Code lives in the git tree.** Scripts, schemas, and the canonical `agent-registry.json` stay at `REPO_ROOT = C:/Users/Owner/AAOS` (reuse `validate.py`'s `SCRIPT_DIR`/`REPO_ROOT`/`DEFAULT_REGISTRY_PATH` constants). Schemas: `REPO_ROOT/schemas`. Registry: `REPO_ROOT/agent-registry.json`.

**0.3 Injectable time & thresholds.** No function bakes wall-clock or the three thresholds as module constants inside logic. The mechanism:
- `now: datetime | None = None` on every time-dependent function, resolved by `_now(now)` → `now if now is not None else datetime.now().astimezone()` (timezone-aware, local offset, matching the ADR examples' `-04:00`).
- `ttl_minutes: int = 45`, `skew_minutes: int = 5`, `settle_seconds: float = 30.0` as keyword args with the real ADR-0003 defaults.
- `sleep: Callable[[float], None] = time.sleep` injected into `acquire_lease` — it *is* the settle mechanism and doubles as the deterministic seam for injecting a mid-settle conflict copy in the CONTESTED test.

All timestamps serialize via `dt.isoformat()`; parse via `datetime.fromisoformat()`. A naive datetime read from a file is treated as fail-closed ambiguous (`CorruptStateError`), never silently assumed UTC.

**0.4 Standard exit-code contract (every CLI script):**

| code | meaning |
|---|---|
| 0 | success, **including any idempotent no-op** |
| 1 | schema/validation or fail-closed content error |
| 2 | usage error (bad args, missing file, unreadable/corrupt registry) — matches `validate.py` |
| 3 | state-conflict / precondition unmet (task not queued, lease DENIED, write-once conflict) |
| 4 | CONTESTED / lease-lost (concurrency fail-closed) |
| 5 | event-commit failure (`emit_event` could neither append nor spool) → state left unchanged |

---

## 1. Shared library: `tools/_aaos_common.py`

Leading underscore keeps it out of the CLI namespace. Every other tool does `import _aaos_common as aaos` (siblings in `tools/`, same `sys.path` pattern as `tests/test_validate.py`).

### 1.1 Path-matching reuse — direction decided

**Lift `match_write_scope` (and its `posixpath.normpath` traversal-safe canonicalization) into `_aaos_common.py` as its single home, and change `validate.py` to import it from there.**

```python
# validate.py — replace the local def match_write_scope(...) body with:
from _aaos_common import match_write_scope
```

Rationale: the CLI tool should depend on the shared lib, not the reverse (no import cycle — `_aaos_common` imports nothing from `validate`); this is mechanical and already regression-guarded by `tests/test_validate.py::WriteScopeTraversalTests` (both the escape-rejected and resolves-inside-accepted cases) — re-run that suite as part of Phase 3's own TDD to prove zero behavior change. Zero divergent copies result. (Fallback if preferred: `_aaos_common` does `from validate import match_write_scope` and re-exports — either way there must be exactly one implementation.)

### 1.2 Result & error types

```python
class LeaseOutcome(enum.Enum):
    ACQUIRED = "acquired"
    DENIED   = "denied"       # task not leasable / live incumbent
    CONTESTED = "contested"   # settle-rescan found a foreign/conflict lock -> fail closed

@dataclasses.dataclass(frozen=True)
class LeaseResult:
    outcome: LeaseOutcome
    lock: dict | None = None
    expires: datetime | None = None
    reclaimed_prior: dict | None = None   # a prior EXPIRED lock we archived en route (caller emits LeaseExpired)
    conflict_paths: list[str] = ()        # offending filenames, on CONTESTED
    reason: str | None = None             # human-readable, for DENIED/CONTESTED

@dataclasses.dataclass(frozen=True)
class ReclaimDecision:
    reclaimable: bool
    classification: str        # "DeadAgent" | "ExpiredLapsed" | "" (not reclaimable)
    reason: str

class AaosError(Exception): ...
class UsageError(AaosError): ...            # -> exit 2
class StateConflictError(AaosError): ...    # -> exit 3
class LeaseLostError(AaosError): ...        # -> exit 4 (commit-time lease no longer ours)
class CorruptStateError(AaosError): ...     # -> exit 1, fail-closed (unparseable lock/task/expiry)
class EventCommitError(AaosError): ...      # -> exit 5 (append AND spool both failed)
```

### 1.3 Paths

```python
def seam_root(override: str | None = None) -> Path
def project_dir(project_id: str, *, root: str | None = None) -> Path      # <seam>/projects/<project_id>
def task_path(task_id, project_id, *, root=None) -> Path                  # .../tasks/<task_id>.json
def handoff_path(handoff_id, project_id, *, root=None) -> Path            # .../handoffs/<handoff_id>.json
def lock_path(task_id, project_id, *, root=None) -> Path                  # .../locks/<task_id>.lock
def lock_archive_dir(project_id, *, root=None) -> Path                    # .../locks/_archive/
def events_log_path(project_id, *, root=None) -> Path                     # .../events/aaos-events.ndjson
def event_spool_dir(project_id, *, root=None) -> Path                     # .../events/aaos-events.ndjson.pending/
def heartbeat_path(*, root=None) -> Path                                  # <seam>/heartbeat.json
def registry_path() -> Path                                              # REPO_ROOT/agent-registry.json (git tree)
```

Pure path construction; no I/O; no directory creation (that's `atomic_write`'s job). `project_id` validated against `^[a-z][a-z0-9-]*$`, rejected otherwise — a malformed project_id must never let a path escape the seam (same defensive-normalization lesson as Gate 2's traversal finding, applied to path *construction* not just glob-matching).

### 1.4 Atomic I/O

```python
def atomic_write(path: Path | str, content: str | bytes) -> None
```
The ADR-0002 idiom, verbatim: create parent dirs (`os.makedirs(parent, exist_ok=True)`); unique temp name `path + ".tmp-" + str(os.getpid())`; write bytes (UTF-8 if `str`); `flush()` + `os.fsync()`; `os.replace(tmp, path)`; on any exception, best-effort unlink the temp and re-raise. Never writes `path` in place.

```python
def load_json(path: Path | str) -> dict          # raises UsageError (missing) / CorruptStateError (unparseable)
def save_json(path: Path | str, obj: dict) -> None   # json.dumps(indent=2, ensure_ascii=False) -> atomic_write
```

### 1.5 Typed state loaders/savers

```python
def load_task(task_id, project_id, *, root=None) -> dict
def save_task(task: dict, *, root=None) -> None
def load_handoff(handoff_id, project_id, *, root=None) -> dict | None    # None if absent
def save_handoff(handoff: dict, *, root=None) -> None           # WRITE-ONCE: existing+different -> StateConflictError; identical -> silent no-op
def load_lock(task_id, project_id, *, root=None) -> dict | None # None if absent; CorruptStateError if present-but-unparseable
def load_heartbeat(*, root=None) -> dict | None                 # None if absent/unreadable
def load_registry() -> dict                                    # reuse validate.load_registry(registry_path())
```

`save_task`/`save_handoff` do **not** stamp `updated_at`/`posted_at` — callers set timestamps so the transaction controls ordering.

### 1.6 Validation reuse

```python
def validate_obj(obj: dict, schema_name: str, *, registry: dict | None = None) -> list[str]
```
Builds a `Draft202012Validator` from `REPO_ROOT/schemas/<schema>.schema.json` and calls `validate.validate_instance(obj, validator, schema_name, registry, registry_path())`. One validation implementation, reused in-process.

### 1.7 Lease acquisition — ADR-0003's 6-step protocol

```python
def acquire_lease(
    task_id: str, project_id: str, holder: str, session: str, *,
    now: datetime | None = None,
    ttl_minutes: int = 45, skew_minutes: int = 5, settle_seconds: float = 30.0,
    sleep: Callable[[float], None] = time.sleep,
    reclaim_expired: bool = True,
    root: str | None = None,
) -> LeaseResult
```

1. **Precheck.** `load_task`. If `state == "queued"` → proceed. Else if a `lease` exists and is **past `expires + skew_minutes`**: if `reclaim_expired`, `archive_lock` the incumbent, set `result.reclaimed_prior`, proceed. Else → `DENIED("expired lease present; run expire-leases")`. Else (live lease/non-queued) → `DENIED(f"task state={state}, lease live")`.
2. **Stage.** Compose the lock dict. `atomic_write` it to a uniquely-named sidecar `locks/<task_id>.lock.tmp-<holder>-<pid>`.
3. **Exclusive create.** `os.open(lock_path, O_CREAT|O_EXCL|O_WRONLY)`. Success → write, close, unlink sidecar. `FileExistsError` → load incumbent; past grace → archive + retry `O_EXCL` **once** (still collides → CONTESTED, archive sidecar); live → unlink sidecar, `DENIED("incumbent lease live")`.
4. **Settle.** `sleep(settle_seconds)`.
5. **Rescan.** CONTESTED if a foreign `<task_id>.lock` (different holder/session) or a OneDrive conflict-copy-shaped file (`<id>-<HOST>.lock`, `<id> (N).lock`, `<id>-<HOST> (N).lock`) exists → archive our own lock, return `CONTESTED(conflict_paths=[…])`.
6. **Confirm.** Return `ACQUIRED(lock=..., expires=..., reclaimed_prior=maybe)`.

`acquire_lease` **emits no events and does not mutate `task.json`** — the caller composes the transaction.

### 1.8 The commit transaction & the double-run backstop

```python
def commit_state_change(new_state_obj: dict, state_path: Path, event: dict, *, root=None) -> str
```
1. Stage `new_state_obj` to `state_path + ".tmp-<pid>"` (not yet in place).
2. `event_id = emit_event(**event)` — if `EventCommitError`, unlink the staged tmp and re-raise (operation fails, exit 5).
3. `os.replace(tmp, state_path)`.
4. Return `event_id`.

```python
def assert_lease_held(task: dict, holder: str, session: str, *, now=None, skew_minutes=5) -> None
```
Raises `LeaseLostError` (→ exit 4) unless `task.lease` exists, `holder`/`session` match, and it's not past `expires + skew`. **The double-run backstop** — `complete-task`, `fail-task`, `post-handoff` (when `--holder/--session` given) call this immediately before their `commit_state_change`.

### 1.9 Stale-lock reclamation — the resolved cross-document tension

```python
def reclaim_decision(lock: dict, heartbeat_status: str | None, *, now=None, skew_minutes=5) -> ReclaimDecision
def archive_lock(lock_file: Path, *, now=None) -> Path      # os.replace to locks/_archive/<task_id>.<compact-ts>.lock; never deletes
```

**Decision (flag for the reviewer — this reconciles wording that differs across `AAOS-SPEC.md` §10, ADR-0003, and ADR-0005):**
- **Primary trigger is hard expiry, heartbeat-independent:** `now > expires + skew_minutes` (no renewal) → **reclaimable**, unconditionally. Unparseable/missing expiry → not reclaimable, fail-closed `CorruptState`.
- **Heartbeat sets only the escalation classification, not reclaimability:** `missed`/`unknown`/unavailable → `classification="DeadAgent"` (sweep additionally emits `Escalated`); `fresh`/`stale` → `classification="ExpiredLapsed"` (routine, no escalation).
- **Why not literal "expiry AND missed-heartbeat":** the only liveness signal for `cowork` is a twice-daily/16-hour beacon. Gating reclamation on it would keep an *actually expired* lease un-reclaimable for up to 16h whenever the coarse beacon still reads `fresh`, wedging the queue and violating the sub-hour recovery goal. Lease **renewal** (bumping `expires`) is the correct fine-grained liveness proof; the coarse heartbeat is for classification/dashboards only. The double-run risk is closed by `assert_lease_held` at commit time. Textual authority: ADR-0003 — "reclaimable... past its grace period **with no renewal on record**."
- **Action item for this phase's review:** `AAOS-SPEC.md` §10 currently reads "requires both conditions" for reclamation — that wording should be corrected to match this resolved policy (expiry-primary, heartbeat-classifies) as part of Phase 3's doc-consistency pass, the same category of fix Phase 1's review made.

### 1.10 Event emission — ADR-0004 discipline, modeled on `~/.claude/maintenance/append-event-log.py`

```python
def emit_event(type: str, actor: str, project_id: str,
               refs: dict | None = None, detail: str | None = None,
               event_log_ref: str | None = None, *,
               now: datetime | None = None, root: str | None = None) -> str
```

1. **Drain first**: `drain_event_spool(project_id, root=root)` folds any parked events into the NDJSON, oldest-first, before this append.
2. **Build & validate**: `event_id = f"e-{_now(now).isoformat()}-{next(_EVENT_SEQ)}"` (matches `^e-[0-9TZ:.+-]+-[0-9]+$`). Run `validate_obj(event, "event")` — malformed → fail-closed `EventCommitError`, never appended.
3. **Append with ≤1 retry, then spool**: `_atomic_append_ndjson` = mtime-guarded read-all → append line → `os.replace` (atomic-write-rename, not in-place `O_APPEND`). Retry once on mtime-changed/lock; still failing → `spool_event(...)`, return `event_id` (spooling counts as durably committed).
4. `EventCommitError` **only if both append and spool fail**.

```python
def spool_event(event: dict, project_id, *, root=None) -> Path   # .../events/aaos-events.ndjson.pending/<ns>-<pid>.json
def drain_event_spool(project_id, *, root=None, attempts: int = 3) -> int   # replay oldest-first; stop at first still-locked; discard a malformed spooled file (don't wedge the queue); return count drained
```

**Drain is automatic** (head of every `emit_event`, plus `expire-leases.py` drains at startup as belt-and-suspenders) — safe to automate because the spool/NDJSON are files AAOS itself owns (unlike `memory/event_log.md`, see §3). `emit-event.py --drain-only` exists for a manual nudge but correctness never depends on a human running it.

---

## 2. The seven CLI scripts

Every script: `def main(argv=None) -> int` (testable in-process like `test_validate.py` drives `validate.main`), uses the §0.4 exit codes, `import _aaos_common as aaos`, and takes `--project` (default `ora-ops`), `--seam-root`, `--now <iso>` (test hook).

### 2.1 `create-task.py`
CLI: `--title <str> --accept <str>` (repeatable, ≥1) `[--id t-<slug> | --slug <slug>] [--owner <agent|unassigned>] [--priority {1,2,3}] [--input label=<l> ref=<r> kind=<k>]* [--suffix-timestamp]`.
ID scheme: caller-supplied slug, normalized (`--slug foo` → `t-foo`; `--suffix-timestamp` appends `-<utcYYYYMMDDHHMMSS>`), validated `^t-[a-z0-9-]+$`.
Writes `state="queued"`, `owner_agent` default `"unassigned"`, `priority` default 2, `created_at=updated_at=now`. `validate_obj` before write. Event: `TaskCreated`.
Idempotent: identical re-post of same id/content → exit 0 no-op, no re-emit. Different content, same id → exit 3.

### 2.2 `lease-task.py`
CLI: `<task-id> --holder <agent> --session <str> [--ttl-min 45] [--skew-min 5] [--settle-sec 30]`.
Calls `acquire_lease`. ACQUIRED → (emit `LeaseExpired` first if `reclaimed_prior`) → set `state="leased"`+`lease`, `commit_state_change` emitting `TaskLeased`, exit 0. DENIED → emit `LeaseDenied`, exit 3. CONTESTED → emit `LeaseContested`, exit 4.
**No escalation row, no Notion call** — Phase 3 emits the machine event only; the human-facing §10 escalation surface is Phase 4/5 (Problem-A boundary).
Idempotent: re-run with same holder+session on already-held lease → exit 0 no-op.

### 2.3 `complete-task.py`
CLI: `<task-id> --holder <agent> --session <str> --evidence label=<l> ref=<r>` (repeatable, **≥1 required**) `[--archive]`.
`assert_lease_held` (exit 4 if lost). Requires ≥1 `--evidence` (else exit 2) — enforces the schema's documented "evidence before done" business rule. Sets `state="done"`, drops `lease`, `commit_state_change` emitting `Completed`; `archive_lock` residual lock. `--archive` → second transition to `state="archived"`, emit `Archived`.
Archival is folded into this script (`--archive` flag) — this is how Gate 3's `completed → archived` is achieved without an 8th script. Archiving a non-completed task is out of scope (Phase 5).
Idempotent: already-done → skip re-emit; `--archive` still archives; already-archived → no-op.

### 2.4 `fail-task.py`
CLI: `<task-id> --holder <agent> --session <str> --reason <str> [--evidence label=… ref=…]*`.
`assert_lease_held`. Sets `state="failed"`, `commit_state_change` emitting `Failed`; `archive_lock` residual lock. If a prior `Failed` for the same task already exists in the NDJSON, additionally emit `Escalated` (trigger `DoubleFailure`, machine-log only, no Notion).
No task-level auto-retry (no schema field for it — would be scope-creep; the firm's "≤1 retry" canon lives at the I/O layer in `emit_event`/`acquire_lease`, not per-task).
Idempotent: already-failed → no-op.

### 2.5 `post-handoff.py`
CLI: `--id h-<slug> --task <task-id> --from <agent> --to <agent> --next-action <str> [--summary <str>|--summary-file <path>] [--artifact path=<p>]* [--blocker <str>]* [--suggested-next-owner <agent>] [--holder <agent> --session <str>]`.
Computes each artifact's `sha256`+`bytes` from disk. **Validates before writing (mandatory):** `validate_obj(handoff, "handoff", registry=...)` — schema + protocol_version + write_scope colonization guard (including the Gate-2 traversal fix). Any error → exit 1, write nothing. `save_handoff` is write-once (identical re-post = no-op; different content same id = exit 3). Emits `HandoffPosted`.
**Blockers rule (master-prompt-mandated):** non-empty `--blocker` → load the task, `assert_lease_held` if holder/session given, set `state="blocked"` + `blocked_reason`, emit `Blocked` in addition to `HandoffPosted`.

### 2.6 `expire-leases.py`
CLI: `[--dry-run] [--skew-min 5]`.
`--dry-run`: prints what would be reclaimed, **zero writes/archives/events**, exit 0.
Sweep: `drain_event_spool` first. For each lock: unparseable → emit `Escalated`/`CorruptState` marker, skip, don't steal. Else `reclaim_decision` (§1.9). Reclaimable + not dry-run: stage task `state="queued"` (drop lease) → emit `LeaseExpired` → rename → `archive_lock`. `classification=="DeadAgent"` → additionally emit `Escalated`.
This is the script staged (not registered) as the candidate scheduled task — fleet registration is deferred to Phase 4/5 supervised `create_scheduled_task`.

### 2.7 `emit-event.py`
CLI: `--type <EventType> --actor <agent|system> [--task t-…] [--handoff h-…] [--lock …] [--escalation …] [--detail <str>] [--event-log-ref <str>] [--drain-only]`.
Thin wrapper around `aaos.emit_event`. **Deliberately non-idempotent** — each call mints a new event and appends; dedup of *logical* duplicate events is the higher-level scripts' job (e.g. `complete-task` won't re-emit `Completed` on an already-done task). Do not "fix" this into a dedupe primitive.

---

## 3. Milestone-mirroring to `memory/event_log.md` — **manual for v1, not automatic**

No Phase-3 script calls `append-event-log.py` automatically. Reasons: (1) it lives outside this repo and appends to a shared, single-writer-enforced file AAOS doesn't own — exactly the file whose concurrent-append collisions already happened (3 in one day); a headless queue script joining that contention pool is the wrong move. (2) "Genuine milestone" is a judgment call a script can't safely make (not every `Completed` is project-level milestone-worthy). (3) The shared-checkout single-writer doctrine + "one fact, one home" both argue against an automated subsystem writing another subsystem's canonical human log.

**Documented manual ritual (goes in `docs/OPERATIONS.md`):** when a human/session judges an event milestone-worthy: (1) `python ~/.claude/maintenance/append-event-log.py --entry-file <entry.md>`, prose citing the specific `event_id`/`task_ref`; (2) `emit-event.py --type <...> --actor system --event-log-ref "<anchor>"` for the back-link. Realizes the by-reference linkage with zero automated writes to the shared file.

---

## 4. `derive-heartbeat.py` and `project-notion.py` — the MCP-access boundary

Plain Python scripts cannot call MCP tools (only an agent session can). Both scripts consume externally-fetched signal data / local seam files only; the actual Notion MCP read/write happens at a future agent-session call site, out of Phase-3 scope.

### 4.1 `derive-heartbeat.py`
CLI: `--signals <path|-> [--out <path>] [--now <iso>]`.
Input `signals.json` (produced by a future agent-session call site that alone can read Notion `last_edited_time` + run `fleet-status.py`):
```json
{ "agents": [
    {"agent_id":"cowork","last_run":"2026-07-09T06:00:00-04:00","source":["notion:35c6ecd8 last_edited_time","automation-health-monitor:beacon-table"],"threshold_hours":16},
    {"agent_id":"claude-code","last_run":"2026-07-09T14:20:00-04:00","source":["fleet-session-hook:session-state","fleet-status.py:dual-fleet-health"],"threshold_hours":16}
]}
```
Computes per agent: `last_run` null → `status="unknown"` (fail-closed). Else `delta=now-last_run`: `≤threshold`→`fresh`; `≤2×threshold`→`stale`; `>2×threshold`→`missed`. `source`/`threshold_hours` carried through from input (inherited, never invented). Registry agent absent from `signals.json` → `unknown`. Writes schema-valid `heartbeat.json` (`validate_obj` before `atomic_write`).

### 4.2 `project-notion.py`
CLI: `[--out <path>] [--now <iso>]`.
Reads only local files: every `tasks/<id>.json`, latest event per task from the NDJSON tail, `heartbeat.json`. Writes a Derived projection payload to `projects/<project>/notion-projection.json` (not one of the 5 contract schemas — Derived, like `contracts/PUBLISHED.json`): a `generation` block (`generated_at`, per-source freshness, `stale:true` if >7 days old per firm canon) + `rows[]` (one per task: id/title/state/owner/priority/lease/last_event/heartbeat_status). Touches Notion **not at all** — the actual Ops Hub page create/update is the future agent-session call site's job.

---

## 5. Test strategy for Gate 3's hardest criteria

Shared harness: `AAOS_SEAM_ROOT` → `tmp_path`, seed `projects/ora-ops/{tasks,handoffs,locks/_archive,events}`, fixture or git-tree registry. `settle_seconds=0` / injected `sleep` removes real waiting; injected `now`/`ttl`/`skew` remove real time dependence.

**Lock contention (two writers, one loses cleanly):**
- Sequential: task queued → acquire(holder=claude-code) → ACQUIRED; acquire(holder=cowork) → DENIED (O_EXCL collision, incumbent live).
- Genuine race (`threading`, chosen over multiprocessing — the `O_EXCL` syscall releases the GIL so threading races it identically, with far simpler shared-memory result collection): two threads released by a `threading.Barrier(2)`, each racing `acquire_lease` for a different holder; assert outcomes are exactly `{ACQUIRED, DENIED}`, never `{ACQUIRED, ACQUIRED}`.
- CONTESTED path: an injected `sleep` callback that plants a foreign conflict-copy lock mid-settle; assert CONTESTED, our own lock archived, task never left `leased`.

**End-to-end lifecycle — driven via `subprocess`, not in-process** (the E2E's value is proving the real invocation path/exit codes/process boundaries the production sweep will use): `create-task.py` → `lease-task.py` → `post-handoff.py` → `complete-task.py --archive`, fixed `--now` per step. Assert final `task.json` state `archived` with evidence and no lease; handoff file re-validates; NDJSON event sequence is exactly `TaskCreated, TaskLeased, HandoffPosted, Completed, Archived`, each line re-validated via `validate.py`.

**Stale-lock reclamation / lease-expiry (time-injected, no real waiting):** hand-write an expired lock + `leased` task + heartbeat status; run `expire-leases.py --now <fixed>`; assert archive+requeue+`LeaseExpired`(+`Escalated` if `missed`). `--dry-run` variant asserts zero changes. `reclaim_decision` gets its own pure unit-test table tabulating `(expiry vs now±skew) × heartbeat status` to pin the §1.9 policy against regression. Corrupt-lock case: sweep does not reclaim, emits `CorruptState`, leaves lock in place.

---

## 6. Explicitly OUT of Phase-3 scope (confirmed deferred, not built)

- `publish-contracts.ps1` (the `contracts/` mirror) — deploy/mirror step, not a coordination moving part. Phase 4.
- Actual scheduled-task registration of `expire-leases.py`/`derive-heartbeat.py` — staged + locally tested only; registration via supervised `create_scheduled_task` with in-turn confirmation.
- The §10 human-escalation Notion child page — Phase 3 emits machine events (`LeaseContested`, `Escalated`) only; Notion posting is Phase 4/5 (Problem-A boundary).
- The Notion write in `project-notion.py` and the Notion/`fleet-status.py` fetch in `derive-heartbeat.py` — both future agent-session call-site work.
- Automatic milestone mirroring to `memory/event_log.md` — deliberately manual (§3).
- An 8th generic "archive any in-flight task" script — Gate 3 archival is `complete-task.py --archive` only; the Phase-5 rollback drill covers non-completed archival.
- Task-level automatic retry / a `retry_count` field — no schema field for it; scope-creep.
- A dedicated `renew-lease` CLI — the primitive (`assert_lease_held`) supports it, but no 8th script is built now.
- `escalation.schema.json` as a sixth contract — escalations stay as `Escalated` events in Phase 3.

---

## Key files
- Task list / gates: `docs/2026-07-09_AAOS_MasterPrompt_v02.md` (Phase 3 + Gate 3)
- ADRs: `docs/adr/0002…0005-*.md`
- Schemas: `schemas/{agent-registry,task,handoff,event,heartbeat}.schema.json`
- Reuse targets: `tools/validate.py` (`match_write_scope`, `validate_instance`, `load_registry`), `~/.claude/maintenance/append-event-log.py` (spool/drain discipline to mirror in `emit_event`)
- Existing conventions: `templates/*.template.json`, `examples/{t,h}-sample-001.json`, `tests/test_validate.py` (in-process `main(argv)` test pattern)
