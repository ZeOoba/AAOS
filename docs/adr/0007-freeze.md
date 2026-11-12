# ADR-0007: Freeze AAOS

- **Status:** Accepted (Gabriel, 2026-11-05, maintenance-reduction bundle 1)
- **Supersedes in effect:** AMD-0003, AMD-0004, AMD-0006 are **suspended by this ADR**. They are not rejected; they resume only on revival.

## Context
- The live seam (`Claude_Workspace\AAOS\projects\ora-ops\`) shows no task, lock or event activity after 2026-08-16. The last cross-surface handoff was 2026-07-10.
- Both scheduled callers (`ora-aaos-expire-leases`, `ora-aaos-publish-contracts`) have been disabled since September 2026 (last runs 2026-09-14 and 2026-09-22).
- Even so, the repo took about 60 commits in the 90 days to 2026-11-05 (ADR and amendment loops, review passes, Python-version chasing), and CI failed 7 of its last 12 runs.

## Decision
- The repo is FROZEN. No amendments, refactors, doc refreshes or dependency chasing.
- CI runs only on manual `workflow_dispatch`.
- Every non-`main` remote branch is deleted. Their head SHAs are recorded below so any of them can be restored with `git push origin <sha>:refs/heads/<name>`.
- Both scheduled tasks are retired (registry entry deleted, spec folder archived) in `~/.claude`.

## Revival trigger
A real cross-surface task, meaning work that must move between Claude Code and Cowork and cannot be done on one surface, shows up at least twice in 30 days. Revival is a new ADR that un-freezes, re-registers the tasks via `register-scheduled-task`, and re-enables push/PR CI.

## Deleted branches (restorable by SHA)
| Branch | Head SHA | Last commit |
|---|---|---|
| `amd/0003-locality-routing-settle` | `98985bf20cbb1527f48b7de2e8d8d8e6dc9cc3f2` | 2026-10-17 |
| `amd/0004-drain-fold-validation` | `9e6aa967334533f7556779692890515e3d74a204` | 2026-10-18 |
| `amd/0006-drain-lock-primitive` | `848005aea2f1418be51586c82793e71df4eac919` | 2026-10-17 |
| `chore/ignore-claude-worktrees` | `b7b11157b3264e484ce2293b6674f675315da663` | 2026-09-18 |
| `ci/add-github-actions` | `02bbd4d88cb5c4ab2b1f65d709d529603f170b24` | 2026-09-10 |
| `ci/validate-and-contract-drift` | `e1284b119d927906377a70d01a9c51f6cebca966` | 2026-10-29 |
| `claude/aaos-optimization-plan-27d345` | `1ee9cef90621c3b94fbb48acedac1b2760849ffe` | 2026-10-17 |
| `claude/code-review-52b518` | `0679d46d09ad7d22987fbfb0f00fcf3d993fb4a4` | 2026-11-03 |
| `claude/iso-normaliser-follow-3.13.17` | `aab4140d223a94b6cefadefec7996fcfb271533d` | 2026-11-03 |
| `claude/pin-ci-python-3.13.14` | `99ae3bca0f9d92e91638ecad5da5e95270d20557` | 2026-10-29 |
| `claude/plan-s16-a1c-a2-specs-d2c350` | `0dd6feac1c644bbd724dd2a318eec9d35b6970d8` | 2026-10-18 |
| `docs/adr-0003-0006-review-fixes` | `f60cf79d229ee6cc25992d571fa2692038b42ac9` | 2026-10-18 |
| `docs/architecture-diagram-refresh` | `0ea6412b9cf50ef9f58d8e287fee863414fad6a8` | 2026-08-21 |
| `docs/cowork-bootstrap-r1` | `8a6c0e9f5c788366d1ca02468fbdf0e3d7fa1546` | 2026-10-17 |
| `docs/diagram-refresh-20261028` | `e35428c54a234f7abd702df0522fe876e98182c4` | 2026-10-28 |
| `docs/ratify-amd-0003-0004-0006` | `abb569ca91f9c4d22dc97fa48b982e42c2fe0e34` | 2026-10-18 |
| `docs/readme-diagram-verified` | `8cfea7f288265aaa3f1032925c94e86cd6f390d9` | 2026-09-18 |
| `feat/a0-5-cli-input-validators` | `b4162ac91f177b6efbe9f64027e82a24fbd4a9e2` | 2026-10-17 |
| `feat/a0a-windows-ci-leg` | `54076ca72914f2d04b7252fe26db4605ee31067b` | 2026-10-17 |
| `feat/a0b-characterization-tests` | `343d5dd54210ca12d6431437823e1728607d5b87` | 2026-10-17 |
| `feat/c0-doc-drift` | `74406d17b126621ac13e1b65c8b0069624337e94` | 2026-10-17 |
| `feature/notion-post-wire` | `1dde297d200368f982e9f03ee7e7035871be1619` | 2026-07-26 |
| `fix/drain-grace-followup` | `55e5b725c618a5219a597ebeb48d2033d716a196` | 2026-10-18 |
| `fix/iso-lowercase-t` | `1bdace7ce1f256c703127442460943fc74ec7782` | 2026-10-18 |
| `fix/iso-py310-normaliser` | `2156460608d851464e533c28bda58d430778c81f` | 2026-10-18 |
| `fix/iso-z-portability` | `a5b96c9d043ec0066e75abd9c1faaebcbe34c49d` | 2026-10-18 |
| `fix/lifecycle-hardening-2026-10-03` | `2c5a8fddbb7d0fc9db7a65e5e599c0d128125670` | 2026-10-03 |
| `fix/registry-cowork-r1` | `53d07abf155517040151a87f061f62133f5424cb` | 2026-10-18 |
| `fix/review-findings-a0x` | `eb8fef0d3ddb33f1cfc12f160e21fd6c857553c1` | 2026-10-17 |
| `fix/transient-error-never-quarantines` | `9a34860c3b03fa5e8b5bff719a1b0aa709e177ed` | 2026-10-17 |
| `worktree-oi265-lease-ownership-spec` | `598ef5148611466df9dc3e3978d107ead8924d9b` | 2026-09-10 |

## Addendum (2026-11-05, same day): what actually happened
- **Branches.** The 31 branches in the table above were deleted (exact name and SHA match). Two branches pushed after this ADR was written were **kept**, unmerged: `fix/t1-drain-non-object-ndjson` (a674e56eb79ac02099ecdd9fe664260c55b5e06a) and `fix/t2-sweep-survives-drain-failure` (bf0b8cf6145398eb49fb9c01b4baf4b169a89f9b). They were opened as AAOS#38/#39 by a concurrent eval-plan session and closed unmerged under this freeze, on Gabriel's ruling. They still exist on the remote and need no restore.
- **Scheduled tasks.** Retirement of `ora-aaos-expire-leases` and `ora-aaos-publish-contracts` is **ratified but not yet executed**. The MCP delete gate refused `delete_scheduled_task` (store-scan timeout; tracked as OI-396 in `~/.claude` memory). Both remain registered with `enabled:false` and are listed in `~/.claude/maintenance/_state/retire-pending.json`, so no resume can re-enable them. Their spec folders are not yet archived.

## Addendum (2026-11-12): freeze enforced in code; Notion egress fails closed
An egress and write-path audit found three live paths the freeze did not close. `tools/publish-notion.py` sent task titles verbatim to the Notion Ops Hub. It also POSTed by default whenever an ambient `NOTION_TOKEN` was set. And no code enforced the freeze, so every seam CLI still fell back to the live OneDrive seam. An adversarial review of the first fix (e2fa2c5) found more gaps, and the rules below are the result of both rounds. This change closes paths and adds no features.
- **Freeze sentinel.** `_aaos_common.refuse_frozen_live_path()` raises `UsageError` with a `FROZEN` message pointing here for any path that is, or lies under, the live seam `LIVE_SEAM_ROOT`. The only exception is `AAOS_UNFREEZE` set to exactly `1`.
  - **Routes covered.** `seam_root()` applies it to the built-in default, `AAOS_SEAM_ROOT` and `--seam-root`. Every path builder and `require_seam_reachable()` go through `seam_root()`.
  - **File arguments covered.** Every CLI also applies it, before any read or write, to each file argument: `--out` (derive-heartbeat, project-notion), `--in` (publish-notion), `--signals`, and post-handoff's `--summary-file`, `--artifact` and `--registry`. In validate.py it covers the positional path, `--schema-dir` and `--registry`.
  - **Spellings covered.** It compares paths by containment, not equality. Before comparing, it strips the `\\?\`, `\\.\` and `\\?\UNC\` prefixes, maps an admin share on any host (`\\host\C$\...`) to `C:\...`, and removes trailing dots, trailing spaces and NTFS stream suffixes. Case is ignored on Windows.
  - **Shares refused.** While frozen, any other path that starts with `\\` or `//` (a UNC share or device path) is refused outright.
  - **Aliases.** Junctions, symlinks, 8.3 short names and subst drives are caught by `os.path.realpath`. That opens a read-only handle on the path given and, for an alias, on its target, which may be the live seam. It runs only when the string checks above did not already recognise the path.
  - **What still works.** Paths outside the live seam, such as the test suite's tmp seams or a copy of the seam, are unaffected.
- **publish-notion fails closed.**
  - **Dry run by default.** `--dry-run` remains as a no-op and cannot be combined with `--live`.
  - **Live gates.** A live POST requires `--live`, `AAOS_NOTION_TOKEN`, `AAOS_HASH_KEY` (at least 16 characters) and, while this ADR stands, `AAOS_UNFREEZE=1`.
  - **Credentials from the environment only.** `publish()` reads the token and key itself and has no token parameter, so a caller cannot pass in the generic `NOTION_TOKEN`, which is never read.
  - **Redaction.** `task_id` and `title` are always replaced by an HMAC-SHA256 tag (`hmac:<16 hex>`) keyed with `AAOS_HASH_KEY`. A dry run without the key uses a random one-time key.
  - **Allowlists.** `owner_agent` appears as-is only if it is a registered agent id or `unassigned`. `state`, `heartbeat_status` and `priority` appear as-is only if they are values the schema allows. Anything else is replaced by an HMAC tag.
  - **Validated fields.** `project` must match `PROJECT_ID_RE`, and `generated_at` and `now` must be timezone-aware ISO-8601. Otherwise the call exits 2.
  - **Deferred.** The ratified A4a confidentiality screen (match the payload against the shared roster and exit 2 on a hit) is still unbuilt and waits for revival.
- **Revival.** The revival ADR removes the sentinel or sets `AAOS_UNFREEZE=1` on the operating machine. Setting it for any other reason breaks this ADR.
