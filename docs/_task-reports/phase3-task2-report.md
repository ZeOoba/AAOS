# Phase 3, Task 2 — Queue CLI Scripts: Implementation Report

**Status: DONE**

## Scope

Implemented the seven CLI scripts specified in `docs/PHASE3-DESIGN.md` §2.1–§2.7, each a thin composition over the already-built, already-tested `tools/_aaos_common.py` (Task 1, 26 tests, committed as `8413e3e`):

- `tools/create-task.py`
- `tools/lease-task.py`
- `tools/complete-task.py`
- `tools/fail-task.py`
- `tools/post-handoff.py`
- `tools/expire-leases.py`
- `tools/emit-event.py`

Each exposes `def main(argv=None) -> int`, is import-safe (all `argparse` construction happens inside `build_arg_parser()`, called only from `main()`), uses the §0.4 exit-code table, and calls `import _aaos_common as aaos`. No changes were made to `tools/_aaos_common.py`, `tools/validate.py`, any schema, or `agent-registry.json` — `git diff --stat HEAD` against those paths is empty; every change in this task is a new, untracked file.

## TDD evidence

### RED (before any tools/*.py existed)

```
$ python -m unittest tests.test_queue_scripts tests.test_e2e_lifecycle -v
...
AttributeError: module '_aaos_tool_post_handoff' has no attribute 'main'
...
AssertionError: 2 != 0 : ...python.exe: can't open file 'C:\\Users\\Owner\\AAOS\\tools\\create-task.py': [Errno 2] No such file or directory
----------------------------------------------------------------------
Ran 38 tests in 0.684s

FAILED (failures=2, errors=36)
```
36 collection-time errors (tools/*.py don't exist yet, so `_tool_loader.load_tool` produces a module with no `main`) + 2 genuine failures (the CONTESTED CLI test observed `None` instead of an exit code; the subprocess-driven E2E test hit `[Errno 2] No such file`). This confirms every test was wired to the not-yet-built implementation, not vacuously passing.

### GREEN (after implementation)

```
$ python -m unittest tests.test_queue_scripts tests.test_e2e_lifecycle -v
...
Ran 38 tests in 5.842s

OK
```

### Full repo suite (pre-existing + new)

```
$ python -m unittest discover -s tests -p "test_*.py" -v
...
Ran 119 tests in 6.978s

OK
```
(119 = 44 pre-existing (`test_aaos_common.py` 26 + `test_validate.py` 18) + 38 new from this task + 37 from a concurrent session's Phase-3 work on `derive-heartbeat.py`/`project-notion.py` — see "Concurrent work observed" below. `git status --porcelain` shows only untracked additions, zero modified tracked files.)

## Test files

- `tests/_tool_loader.py` — `importlib.util`-based loader for the hyphenated `tools/*.py` filenames (can't be `import`ed directly), caching loaded modules in `sys.modules` under a synthetic name.
- `tests/test_queue_scripts.py` — 37 in-process tests, one class per failure/success mode per script (happy path, DENIED/CONTESTED, lost-lease, missing-evidence, write-scope, idempotency-with-event-count-assertions, double-failure escalation, expire-leases reclaim/dry-run/corrupt-lock, emit-event non-idempotency and `--drain-only`).
- `tests/test_e2e_lifecycle.py` — 1 subprocess-driven test: `create-task → lease-task → post-handoff → complete-task --archive`, asserting exit codes, final task state (`archived`, evidence present, no lease), the handoff and task files and the NDJSON log all re-validating via `tools/validate.py` run as a subprocess, and the exact event sequence `TaskCreated, TaskLeased, HandoffPosted, Completed, Archived`.

**Registry choice for write-scope tests:** the REAL repo-root `agent-registry.json` (not a fixture) — `post-handoff.py` has no `--registry` override on its CLI per the literal design spec, and `aaos.validate_obj("handoff", registry=None)` always loads it via `aaos.load_registry()`. Tests needing a handoff to pass write-scope validation place their artifact file in a temp directory created *inside* the git tree (`tempfile.mkdtemp(dir=REPO_ROOT)`, cleaned up in `tearDown`) so it falls under claude-code's registered `"C:/Users/Owner/AAOS/**"` write_scope; tests needing a write-scope *violation* place the artifact under the per-test OS-tmp seam root instead.

## Design decisions / gaps worth flagging

These are places where §2 didn't fully specify a mechanism and I made a call rather than guessing silently:

1. **`create-task.py`'s `TaskCreated` actor.** The CLI signature in §2.1 has no `--actor`/`--holder` flag (unlike lease/complete/fail, which act on behalf of `--holder`). Used the literal `"system"` actor, the same convention `expire-leases.py`'s sweep-generated events already use elsewhere in the design. Not schema-invalid, but worth confirming against intent.

2. **`fail-task.py`'s `--reason`.** `task.schema.json` has no failure-reason field (unlike `blocked_reason` for the `blocked` state) — there's nowhere on the task object to put it. Placed it in the `Failed` event's `detail` field (maxLength 200, truncated defensively), matching that field's documented purpose ("human audit... why LeaseContested, etc."). If a `failed_reason` task field was actually intended, that's a schema gap from Phase 2, not something this task should silently patch.

3. **Lock-dict vs. task `lease` shape mismatch (a real cross-document bug, caught by the tests, not worked around).** `acquire_lease`'s returned `result.lock` (and the physical `.lock` file) carries `protocol_version` and `task_ref` in addition to `holder`/`acquired`/`expires`/`session` — but `task.schema.json`'s `lease` sub-object has `additionalProperties: false` and only permits those last four keys. Assigning `result.lock` directly to `task["lease"]` fails schema validation (caught immediately by `LeaseTaskHappyPathTests`, RED → fixed). `lease-task.py` now explicitly projects the lock dict down to the four schema-permitted fields before embedding it. This is a genuine mismatch between the already-committed lock-file shape (`_aaos_common.acquire_lease`, Task 1) and the already-committed `task.schema.json` (Phase 2) — flagging rather than touching either committed artifact.

4. **`emit-event.py` / `_aaos_common.emit_event`'s exit-5 surface is broader than §1.10 step 4's stated contract.** §1.10 step 4 says "`EventCommitError` only if both append and spool fail." The actual, already-tested implementation also raises `EventCommitError` immediately when the constructed event fails schema validation (e.g., an unrecognized `--type`) — before any append is attempted. Since every script's exit-code cascade maps `EventCommitError → 5`, an invalid `--type` on `emit-event.py` surfaces as exit 5, not the "1 = schema/validation error" bucket the general exit-code table's wording might suggest. Per instructions, this is reported rather than patched around (e.g., by pre-validating `--type` against the schema enum inside `emit-event.py`, which would duplicate validation logic the library already owns).

5. **`expire-leases.py` lock→task-id resolution.** §2.6 doesn't specify how a lock file maps to a task id. Used the lock's own `task_ref` field (present in every lock `acquire_lease` writes) rather than parsing the filename — this is strictly safer for OneDrive conflict-copy-shaped stray files (`<id>-<HOST>.lock` etc.) than filename-parsing would be, and funnels a lock with a missing/unresolvable `task_ref`, or one whose referenced task file is itself missing/corrupt, into the same fail-closed `Escalated`/`CorruptState` path as an unparseable lock — an extension of §2.6's explicit "unparseable → skip, don't steal" rule to two adjacent failure modes it didn't enumerate, not a deviation from it.

## Concurrent work observed (informational, not a defect)

While this task was in progress, `tools/derive-heartbeat.py`, `tools/project-notion.py`, `tests/test_derive_heartbeat.py`, `tests/test_project_notion.py`, and `docs/_task-reports/phase3-task3-report.md` appeared in the same shared working tree (file timestamps ~19:03–19:06 vs. this task's ~19:05–19:12+), evidently from a concurrent session working the design's §4 scripts. No collision occurred: `git diff --stat HEAD` against every shared/tracked file (`_aaos_common.py`, `validate.py`, schemas, `agent-registry.json`) is empty, both sessions' new files are disjoint, and the full 119-test suite is green with both bodies of work present. Noting this per the shared-checkout single-writer caution in standing doctrine — this repo isn't on that doctrine's named list (`~/.claude`, `ora-skills`, `oxford-command-center`), but the same risk class applies to any concurrently-edited git tree, and it's worth Gabriel's awareness that Phase 3 tasks are being worked in parallel on one checkout rather than isolated worktrees.

## Files touched

New (this task only):
- `tools/create-task.py`, `tools/lease-task.py`, `tools/complete-task.py`, `tools/fail-task.py`, `tools/post-handoff.py`, `tools/expire-leases.py`, `tools/emit-event.py`
- `tests/_tool_loader.py`, `tests/test_queue_scripts.py`, `tests/test_e2e_lifecycle.py`

Read only, not modified: `tools/_aaos_common.py`, `tools/validate.py`, `schemas/*.json`, `templates/*.json`, `examples/*.json`, `agent-registry.json`, `docs/PHASE3-DESIGN.md`, `docs/2026-07-09_AAOS_MasterPrompt_v02.md`.

No writes outside `C:\Users\Owner\AAOS\` (no seam writes — the seam root for every test run is a per-test temp directory via `AAOS_SEAM_ROOT`, never the real OneDrive seam). No `git commit` was run, per instructions.
