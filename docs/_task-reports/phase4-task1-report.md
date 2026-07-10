# Phase 4 Task 1 — `tools/publish-contracts.py` (TDD)

## Scope

Implemented `tools/publish-contracts.py` per `docs/PHASE4-DESIGN.md` §4, plus
`tests/test_publish_contracts.py`. TDD followed strictly: tests written and
run to failure first, tool implemented second, tests re-run to green third.

Files touched (both new, nothing else modified):
- `tools/publish-contracts.py`
- `tests/test_publish_contracts.py`

## Design decisions made (not fully pinned by the spec)

1. **Destination layout inside `contracts/` is flattened, not
   directory-mirrored.** `schemas/*.schema.json` → `contracts/<name>.schema.json`
   (no `schemas/` subdir), `docs/AAOS-SPEC.md` → `contracts/AAOS-SPEC.md`,
   `docs/COWORK-BOOTSTRAP.md` → `contracts/COWORK-BOOTSTRAP.md` (no `docs/`
   subdir), `agent-registry.json` → `contracts/agent-registry.json`.
   `templates/*` is the one exception, explicitly called out in both the
   task text and `PHASE4-DESIGN.md` §3 as landing under `contracts/templates/`.
   Basis: `PHASE4-DESIGN.md` §3's bootstrap-read list names files without
   their source-tree prefix ("`AAOS-SPEC.md`, `handoff.schema.json`,
   `event.schema.json`, `agent-registry.json`, `templates/handoff.template.json`")
   — flattened except templates. If the sibling task authoring
   `docs/COWORK-BOOTSTRAP.md` (task #9) assumes a different layout (e.g.
   `contracts/schemas/`), that doc and this script need to agree before the
   first live publish (task #10).

2. **No `--repo-root` CLI flag** (matches the task's literal CLI:
   `[--seam-root <dir>] [--source-commit <sha>]`). This script always
   publishes *this* repo (`_aaos_common.REPO_ROOT`), by design — per
   `PHASE4-DESIGN.md` §4 it is not meant to be pointed at arbitrary trees in
   production. Tests exercise fake/incomplete source trees by monkeypatching
   the loaded module's `REPO_ROOT` attribute before calling `main(argv)`
   (every path in the script is computed by reading the `REPO_ROOT` module
   global at call time, never captured once at import time, so this works
   without touching the documented CLI surface). Documented in both the
   script's module docstring and the test file's module docstring.

3. **`--source-commit` tests avoid a real git repo per test** (the task's
   offered choice) for everything except the three git-derivation behaviors
   themselves (clean-tree, dirty-tree, git-unavailable), which use real
   throwaway `git init` repos in `GitDerivationTests` — `git` is confirmed
   present in this dev environment (git 2.55.0.windows.2), so there was no
   reason to fake/skip that coverage.

4. **Check ordering in `main()`:** resolve `--seam-root` → derive
   `source_commit` (git or override) → validate `agent-registry.json` →
   check every required source file exists → copy everything → write
   `PUBLISHED.json` last. All four failure classes (bad seam-root, git
   failure, bad registry, missing file) abort before any file is copied;
   `PUBLISHED.json` is only ever written after every other copy in the plan
   has already succeeded.

5. **Malformed registry covers both invalid-JSON and schema-invalid
   content**, both exit 1. The task's "load the JSON yourself, don't just
   check it parses" instruction implied schema validation on top of a parse
   check; both paths are tested (`MalformedRegistryJsonTests`,
   `SchemaInvalidRegistryTests`).

## RED (tests written first, tool did not yet exist)

```
$ python -m unittest tests.test_publish_contracts -v
...
ERROR: test_idempotent_rerun_byte_identical_except_generated_at (...)
Traceback (most recent call last):
  File "C:\Users\Owner\AAOS\tests\test_publish_contracts.py", line 118, in setUp
    self._orig_repo_root = self.module.REPO_ROOT
                           ^^^^^^^^^^^^^^^^^^^^^
AttributeError: module '_aaos_tool_publish_contracts' has no attribute 'REPO_ROOT'
[... same AttributeError for all 12 tests via _tool_loader.load_tool,
     since tools/publish-contracts.py did not exist yet ...]

----------------------------------------------------------------------
Ran 12 tests in 0.004s

FAILED (errors=12)
```

## GREEN (after implementation)

```
$ python -m unittest tests.test_publish_contracts -v
test_clean_tree_no_dirty_suffix ... ok
test_dirty_tree_stamps_dirty_suffix ... ok
test_git_unavailable_not_a_repo_exit_2_publishes_nothing ... ok
test_exit_0_and_all_files_copied_byte_identical ... ok
test_idempotent_rerun_byte_identical_except_generated_at ... ok
test_published_json_shape ... ok
test_templates_all_land_under_contracts_templates ... ok
test_invalid_json_registry_exit_1_publishes_nothing ... ok
test_missing_bootstrap_doc_exit_2_publishes_nothing ... ok
test_rerunning_after_file_appears_just_works ... ok
test_schema_invalid_registry_exit_1_publishes_nothing ... ok
test_override_used_verbatim_no_git_call_no_dirty_suffix ... ok

----------------------------------------------------------------------
Ran 12 tests in 1.985s

OK
```

(The `warning: ... LF will be replaced by CRLF ...` lines seen on stdout
during the git-backed tests are Windows `core.autocrlf` chatter from `git
add`/`git commit` in the throwaway temp repos — cosmetic, not test failures;
both git-derivation tests still assert exact `source_commit`/`dirty`
correctness and pass.)

## Full-repo suite (after implementation)

```
$ python -m unittest discover -s tests -p "test_*.py" -v
[... 151 tests, all existing Phase 3 suites (test_aaos_common,
     test_derive_heartbeat, test_e2e_lifecycle, test_event_log_concurrency,
     test_project_notion, test_queue_scripts, test_validate) plus the new
     test_publish_contracts, all "ok" ...]

----------------------------------------------------------------------
Ran 151 tests in 14.592s

OK
```

`git status --porcelain` on the real repo after the full run: clean except
the two new files (`tools/publish-contracts.py`,
`tests/test_publish_contracts.py`) — confirms no test left stray writes in
the real repo tree.

## Coverage checklist against the task's required test list

- [x] Happy path: all files copied, byte-identical, `PUBLISHED.json` shape
- [x] `PUBLISHED.json` written last/only on full success (missing
      `COWORK-BOOTSTRAP.md` → exit 2, no `contracts/` dir at all)
- [x] Malformed `agent-registry.json` → exit 1, nothing published (both
      invalid-JSON and schema-invalid variants)
- [x] No `--source-commit` + git unavailable/not-a-repo → exit 2, nothing
      published
- [x] Dirty tree → `-dirty` suffix + `dirty: true` (real throwaway git repo)
- [x] Clean tree → no suffix + `dirty: false` (real throwaway git repo,
      beyond the minimum ask, for symmetry)
- [x] Idempotent re-run: byte-identical except `PUBLISHED.json.generated_at`
- [x] `templates/*` all land under `contracts/templates/`
- [x] Bonus: re-running after the missing file appears "just works" with no
      special-casing (explicit idempotent-recovery test)
- [x] Bonus: `--source-commit` override is verified to make zero git calls
      (asserted indirectly: the fake repo in that test is not a git repo at
      all, so any git call would have failed the run)

## Concerns

- Destination-layout decision #1 above (flattened `contracts/` vs. mirrored
  `contracts/schemas/`, `contracts/docs/`) is not literally pinned by
  `PHASE4-DESIGN.md` — it's inferred from the bootstrap doc's file-naming
  list. Whoever authors `docs/COWORK-BOOTSTRAP.md` (task #9) needs to read
  paths off this exact layout, or the two must be reconciled before task
  #10's live publish.
- Per instructions, this script was never run against the real seam root
  (`Claude_Workspace\AAOS\`) — only against temp dirs in tests. The real
  publish is task #10's job, blocked on task #9 writing
  `docs/COWORK-BOOTSTRAP.md` (currently absent from the repo, confirmed
  during this task — `publish-contracts.py` will correctly refuse with exit
  2 "missing required source file" if run for real before that file exists,
  which is the intended fail-closed behavior, not a bug).

---

STATUS: DONE
TESTS: 12 new tests (tests/test_publish_contracts.py), all green; full-repo suite 151/151 green (python -m unittest discover -s tests -p "test_*.py")
CONCERNS: contracts/ destination layout (flattened, not directory-mirrored) is inferred from PHASE4-DESIGN.md §3's naming, not explicitly pinned — reconcile with docs/COWORK-BOOTSTRAP.md (task #9) before the real publish (task #10); script correctly refuses to run against the real seam today since docs/COWORK-BOOTSTRAP.md doesn't exist yet (fail-closed, by design, not a defect).
