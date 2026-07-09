# Phase 3, Task 3 — `derive-heartbeat.py` + `project-notion.py`

## Files touched
- `C:\Users\Owner\AAOS\tools\derive-heartbeat.py` (new)
- `C:\Users\Owner\AAOS\tools\project-notion.py` (new)
- `C:\Users\Owner\AAOS\tests\test_derive_heartbeat.py` (new, written first)
- `C:\Users\Owner\AAOS\tests\test_project_notion.py` (new, written first)

No file outside this set was modified. Neither script makes any Notion/MCP
call — both read local files only, per design §6.

## TDD evidence

**RED** — both test modules imported before either script existed; running
them failed at module load with `FileNotFoundError` on the hyphenated
script paths (`tools/derive-heartbeat.py`, `tools/project-notion.py`),
i.e. every test in both files was unrunnable/failing at collection time:

```
File "C:\Users\Owner\AAOS\tests\test_derive_heartbeat.py", line 50, in <module>
    dh = _load_hyphenated_module("derive_heartbeat", "derive-heartbeat.py")
...
FileNotFoundError: [Errno 2] No such file or directory: 'C:\\Users\\Owner\\AAOS\\tools\\derive-heartbeat.py'
```

**GREEN** — after implementing both scripts, full repo suite (4 test
modules: `test_aaos_common`, `test_validate`, `test_derive_heartbeat`,
`test_project_notion`):

```
$ python -m unittest discover -s tests -p "test_*.py"
.................................................................................
----------------------------------------------------------------------
Ran 81 tests in 1.028s

OK
```

37 new tests (19 `test_derive_heartbeat`, 18 `test_project_notion`); the
prior 44 (`test_aaos_common` + `test_validate`) still pass unchanged —
zero regression. Also spot-checked both scripts as real standalone `python
tools/<script>.py` subprocess invocations (not just in-process), confirming
argparse wiring and real atomic writes work outside the test harness.

## Design decisions made where the brief asked for judgment

1. **`threshold_hours` for a registry agent with no signal at all**
   (absent from `signals.json`): carried as `0` (a plain number, satisfies
   the schema's `"type": "number"` requirement). Its `status` is always
   `"unknown"` in this case, per the schema's own documented consumer note
   ("only missed/unknown... " — the numeric value is moot to every
   consumer once status is unknown), so `0` is a safe, non-misleading
   placeholder rather than an invented duration.

2. **`active: false` registry agent → forced `"unknown"`**: interpreted
   literally — the `status` field is always forced to `"unknown"`
   regardless of what `signals.json` reports for that agent, since an
   inactive agent isn't part of the live fleet `expire-leases.py` should
   reason about. `last_run` / `source` / `threshold_hours` are still
   carried through from the observed signal (or the registry/absent-agent
   fallback) verbatim — only `status` is overridden — so the projection
   stays an honest audit trail rather than blanking real observed data.

3. **Signals entries for an `agent_id` not present in the registry at
   all**: still surfaced (computed normally), appended after all registry
   agents. The registry is treated as the *expected* set (driving the
   "registry agent absent from signals → unknown" rule), not an exhaustive
   filter that silently drops a real observed signal — that would be its
   own kind of fabrication-by-omission. Not explicitly required by the
   test list, but exercised implicitly by keeping registry cross-reference
   additive rather than filtering.

4. **Boundary semantics**: `delta <= threshold_hours` → fresh;
   `delta <= 2*threshold_hours` → stale; else missed — i.e. both boundary
   values (exactly 1x and exactly 2x threshold) land on the "less severe"
   side. Pinned by four dedicated boundary tests (1x/2x, exact and
   just-over).

5. **`registry_path()`/`load_registry()` have no seam-root or CLI
   override** (per `_aaos_common`'s existing, unmodified contract — always
   `REPO_ROOT/agent-registry.json`). `derive-heartbeat.py` therefore takes
   no `--registry` flag, matching the design's literal CLI contract
   (`--signals`, `--out`, `--now`, `--seam-root` only). Registry-dependent
   test scenarios (inactive agent, absent-from-signals) use
   `unittest.mock.patch.object(aaos, "load_registry", ...)` rather than a
   fixture file, since that's the only override lever the shared library
   actually exposes.

6. **`project-notion.py` output shape** (not schema-validated — Derived,
   like `PUBLISHED.json`): `generation.sources` records path/exists/mtime
   for `tasks` (plus `file_count`/`newest_mtime`), `events_log`, and
   `heartbeat`; `generation.stale` is `true` iff the newest mtime across
   all present sources is more than 7 days older than `--now`; with zero
   input files present (fully empty project, no heartbeat), `stale` is
   `false` (nothing to measure staleness against, not a false "true").
   `heartbeat_status` is resolved for the task's lease holder if a lease
   is present, else the task's `owner_agent`; `"unassigned"` or an
   agent_id absent from `heartbeat.json` both resolve to `"unknown"`,
   fail-closed.

7. **Malformed task file** (invalid JSON, non-object top level, or missing
   any of `id`/`title`/`state`/`owner_agent`/`priority`) → skipped, with
   `{"file": <name>, "reason": <detail>}` appended to
   `generation.skipped`; its filesystem mtime still contributes to the
   staleness computation (disk freshness is a property of the file
   regardless of parse success) but it never appears in `rows[]` and never
   crashes the run. NDJSON event-log lines that are malformed are silently
   skipped during latest-event selection (not tracked in `skipped[]` —
   that field is scoped to task files per the brief's wording).

## Status

DONE

Full suite: 81/81 passed (`python -m unittest discover -s tests -p "test_*.py"`), no regressions, zero forbidden files touched, no `.tmp-*` residue.

Concerns: none blocking. Two judgment calls worth a second pair of eyes at
review time — (1) the `threshold_hours: 0` placeholder for no-signal
registry agents (design explicitly invited an alternative choice here),
and (2) surfacing signals-only agents not present in the registry, which
the brief didn't explicitly ask for but which I judged was the safer
default over silently dropping real data.
