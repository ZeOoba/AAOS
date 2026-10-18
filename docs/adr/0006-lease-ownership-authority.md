# 0006. Lease ownership authority

## Status

**Amended 2026-10-17 by AMD-0006 (PROPOSED, pending ratification; merging without the ratification record at the end of the Amendment section ratifies nothing).** See the Amendment section at the end of this file.

**Accepted** — D3's thresholds ratified by Gabriel in-turn, 2026-09-10. Implementation may
proceed against this ADR.

Nothing here is implemented yet. D1, D2 and D4 are design decisions rather than ratified
constants; D3 introduces a fourth threshold to a set AAOS-SPEC.md §7 fixes by ratification,
which is what required sign-off.

Supersedes nothing. Amends ADR-0003 (Locking and leasing model) §"Acquire protocol" and
AAOS-SPEC.md §7. Tracked as OI-265 in ORA's open-items tracker (outside this repo); see
also OI-264 and OI-266.

## Context

AAOS's single guarantee is that two agents never both hold a task's lease. Three defects
against that guarantee were found on 2026-09-10, and **two independent attempts to fix the
principal one were adversarially refuted**, each closing one interleaving and opening
another. That pattern — not any individual bug — is what this ADR responds to.

### What was found

- **OI-264 (fixed, merged in #4).** `_exclusive_create` created the lock with `O_EXCL` and
  wrote its JSON as a separate syscall, so the lock was observable as zero bytes. Three
  independent readers each misread that state. Fixed by publishing the lock via `os.link`
  from an already-complete sidecar, making the create genuinely atomic as
  AAOS-SPEC.md §7 step 3 always claimed it was.

- **OI-265 (open).** `acquire_lease` step 1 archives an incumbent lock and takes the lease
  on the strength of the **task's** `lease` field, without reading the lock. Two callers
  both ACQUIRE.

- **OI-266 (open).** `archive_lock`'s `OSError` escapes `acquire_lease` outside the
  `AaosError` hierarchy, and sidecars leak into a directory the sweep filters by name.

### Why two fixes failed

Attempt 1 read the incumbent lock and honoured its expiry. Refuted: `archive_lock` moves
whatever is *at the path*, not the file that was read and judged, so the read→archive
window still let two callers ACQUIRE. It also removed the only unwedge path, making a lock
with a far-future `expires` permanently unleasable with no event emitted.

Attempt 2 added an atomic take (`os.rename` to a private name, then byte verification).
Refuted: the take *vacates the canonical lock path* while verifying, and `os.link` cannot
distinguish "hole" from "free", so a third caller arriving in that gap acquires — and the
restoring caller then archives *its* live lock. It also left the real wedge untouched,
because `lease-task.py` copies the lock's `expires` into the task's lease, so the
production wedge state has both far-future and step 1 never reaches the new liveness rule
at all. The test asserting otherwise used a state `lease-task.py` cannot produce.

Both attempts were locally green, and attempt 2 shipped a mutation harness that reported
"all 8 mutants killed" while a build with a demonstrated double-acquire passed it — because
every mutant targeted the code its author was thinking about.

### The single cause underneath all of it

**Two artifacts answer "who holds this lease?", they are written by different processes at
different times, and they are guaranteed to disagree.**

| | `locks/<task-id>.lock` | `task.lease` |
|---|---|---|
| Written by | `acquire_lease`, inside the protocol | `lease-task.py`, *after* `acquire_lease` returns |
| Written when | at step 3, before the settle | after step 6, ≥30s later |
| Arbitrates acquisition | **yes** — `O_EXCL`/`os.link` collision is the decision | no |
| Read to authorize reclaim | no (until attempt 1) | **yes** — `acquire_lease` step 1 |
| Read by the double-run backstop | no | **yes** — `assert_lease_held` |

The lock file is the *authority*: its atomic create is what actually decides who wins.
`task.lease` is a *derived projection* of that decision, published a settle window later.

Every defect above is the same category error — **a decision that must be made from the
authority was made from the projection**:

- Step 1 authorizes reclamation from `task.lease`. For ≥30 seconds on *every* acquisition,
  that field is stale or absent while a live lock exists. That is OI-265.
- `assert_lease_held` — which AAOS-SPEC.md §7 explicitly names as the mechanism that closes
  the double-run risk ("the double-run risk … is closed instead by the commit-time lease
  check every mutating script runs") — **also reads `task.lease`, never the lock file.**
  So the designated backstop against two holders consults the artifact that cannot
  distinguish them. This is the finding that matters most in this ADR: the safety net has
  the same defect as the thing it was meant to catch.

  Demonstrated — run `python docs/adr/0006-evidence-backstop-blind-spot.py` from the repo
  root. With the lock file naming `cowork/sess-B` and `task.lease` still naming
  `claude-code/sess-A` (the ordinary state for a full settle window after every
  acquisition), `assert_lease_held(task, "claude-code", "sess-A")` **passes**, clearing
  sess-A to commit against a task the lock says it lost.
- The wedge propagates *because* the projection inherits the authority's bad data:
  `lease-task.py` copies `result.lock["expires"]` into `task["lease"]["expires"]`.

Patching interleavings cannot fix this, because the disagreement window is a designed
feature of the write ordering, not a race. It is open on every single acquisition.

## Decision

### D1 — The lock file is the sole authority for lease ownership

`locks/<task-id>.lock` is authoritative. `task.lease` is a derived projection maintained
for reporting and for humans reading task files.

**No decision that affects correctness may be made from `task.lease`.** Specifically:

- `acquire_lease` step 1 MUST NOT authorize reclamation from `task.lease`. It may use it as
  a cheap negative pre-filter (skip work when the task is plainly not leasable), but the
  reclaim decision is made from the lock.
- `assert_lease_held` MUST verify against the lock file, not `task.lease`.
- `reclaim_decision` (used by `expire-leases.py`) MUST apply the same rule as
  `acquire_lease`. Today they can disagree; after this change one implementation serves
  both.

### D2 — Ownership transfers in exactly one atomic operation

Reclaiming is not read-then-archive-then-create. It is a single **compare-and-swap on the
lock path**:

> Replace the lock file with mine **if and only if** its current content is the content I
> judged expired; otherwise I lose.

Two properties are required and neither attempt had both:

1. **The lock path is never vacated for a lock we do not intend to reclaim.** Attempt 2
   violated this and created a new double-acquire. Verification happens *before* the path
   changes, not after.
2. **The swap is atomic against a concurrent `os.link` create.** A losing caller observes
   either the old lock or the new one, never absence.

The concrete mechanism is deliberately left to implementation, but it must be demonstrated
against the property, not argued for in a comment. `os.replace` of a staged sidecar onto
the lock path is atomic and never leaves a hole, but is unconditional — so the "if and only
if" must come from something else (e.g. taking a separate exclusive reclaim token via the
same `O_EXCL` primitive the codebase already trusts, and holding it across read→judge→swap,
with the token itself subject to expiry so it cannot wedge).

> **Mechanism amended by AMD-0006 section A (PROPOSED): a token-serialized swap, not a compare-and-swap.** The "single compare-and-swap" and "exactly one atomic operation" wording above is relaxed to an approximation with bounded, documented and detected residual windows; the filesystem offers no native CAS on a path, and AMD-0006 does not call its mechanism one.

### D3 — No lock may assert liveness indefinitely

A lock whose stated `expires` is further ahead than any admissible lease is not believed.
This closes the wedge that AAOS-SPEC.md §7 already rejects in principle ("wedging the
queue"), and must hold in `acquire_lease` **and** `reclaim_decision` — otherwise the
acquirer and the sweep disagree and the task is unleasable while the sweep stays silent,
which is exactly the state attempt 2 left in place.

**Ratified values (Gabriel, in-turn, 2026-09-10):**

| Constant | Value | Derivation |
|---|---|---|
| `LEASE_TTL_CAP` | **45 minutes** | The ratified TTL itself. No caller may request a lease longer than the lease length the protocol ratified — otherwise "the TTL is 45 minutes" states nothing. `docs/CODE-ADAPTER.md` already directs that a job which will overrun be re-scoped into a smaller task "rather than raced against expiry", so nothing legitimate needs more. |
| `LEASE_EXPIRY_CEILING` | **90 minutes (2 × ratified TTL)** | A lock claiming an expiry further ahead than this is not believed. |

Deriving the ceiling rather than picking it:

- Renewal is **specified but not implemented** — there is no renew tool and no
  `LeaseRenewed` emitter in `tools/`, though ADR-0003 conditions reclamation on "no
  renewal on record". So today `expires` is always `acquired + ttl` and is never bumped.
  Should renewal ship, a renewal sets `expires = renewal_time + ttl`, so the bound below
  holds either way; this derivation does not depend on renewal staying unimplemented.
- From the writer's clock, a legitimate lock therefore satisfies
  `expires ≤ write_time + LEASE_TTL_CAP`.
- The writer's clock may lead the reader's by up to the ratified 5-minute skew, so from a
  reader's view a legitimate lock satisfies `expires ≤ now + 45 + 5 = 50 minutes`.
- **50 minutes is the floor, not the answer.** The consequences are asymmetric: judging a
  live lock bogus is a *steal* (a double-acquire — the defect this ADR exists to close),
  while believing a bogus lock too long is a *bounded wait*. AAOS-SPEC.md §7 fixes the
  direction for exactly this trade — "erring toward waiting longer rather than reclaiming
  too early" — so the ceiling takes margin, not the minimum.
- **90 minutes** gives 40 minutes of headroom over the floor (8× the skew allowance) while
  bounding a wedge to ~90 minutes rather than 24 hours or forever. Expressed as 2× the
  ratified TTL it is a derived quantity, not a fourth independent magic number, which is
  the property §7 demands of every threshold it fixes.

Two constraints this encodes, both learned from attempt 2:

- The ceiling MUST sit strictly above the `--ttl-min` cap with skew margin between them.
  Attempt 2 set them to the same number (1440/1440), making a maximum-length lease
  stealable after one second of backward drift — converting an anti-wedge rule into a
  double-acquire. Here the gap is 45 minutes.
- The comparison is strict (`now + CEILING < expires`), so a lock sitting exactly at the
  ceiling is believed.

This changes a threshold set AAOS-SPEC.md §7 fixes by ratification (TTL 45 min, skew
5 min, settle 30 s), so it requires matching edits to AAOS-SPEC.md §7, ADR-0003 and
`docs/CODE-ADAPTER.md` as part of implementation — see Consequences.

*Note on authorship of these numbers: Gabriel ratified D3; the specific values above were
derived by Claude from the existing ratified constants using the reasoning shown. Changing
either is a one-line edit plus the threshold-doc updates — the derivation, not the
numbers, is the part that should survive review.*

### D4 — Verification obligations

These are part of the decision, not advice, because the failure that produced this ADR was
a verification failure as much as a design one.

- **Every call site that decides ownership carries its own mutation coverage.** Attempt 2's
  harness covered step 1 and the liveness predicate; reverting step 3 to its pre-change
  form produced a live double-acquire that the harness still reported clean. A harness
  scoped to the author's attention is worse than none, because it converts a blind spot
  into a number.
- **Concurrency tests force interleavings; they do not race for them.** A probabilistic
  guard on this protocol is how these defects survived.
- **Fixtures must be states the system can actually reach.** Attempt 2's anti-wedge test
  passed against an unfixed wedge because its fixture (task lease expired, lock far-future)
  cannot be produced by `lease-task.py`. Where a test constructs state by hand, it states
  which code path produces that state in production.

## Consequences

### Conforming changes required

| File | Change |
|---|---|
| `tools/_aaos_common.py` | `acquire_lease` steps 1 and 3 decide from the lock; ownership transfer becomes the D2 CAS; one liveness rule shared with `reclaim_decision`; `reclaimed_prior` reports the lock actually archived |
| `tools/_aaos_common.py` | `assert_lease_held` verifies against the lock file (D1) — this is a behaviour change to the double-run backstop and needs its own tests |
| `tools/expire-leases.py` | `reclaim_decision` honours the D3 ceiling; sweep enumeration accounts for any new intermediate filenames rather than filtering by an ad-hoc name fragment |
| `tools/lease-task.py` | `--ttl-min` capped at `LEASE_TTL_CAP` (45), strictly below the 90-minute ceiling; a catch-all so no non-`AaosError` escapes as a bare traceback |
| `docs/AAOS-SPEC.md` §7 | State D1 explicitly; correct the `assert_lease_held` sentence, which currently claims a protection that artifact cannot provide; document `LEASE_EXPIRY_CEILING` as a derived fourth threshold |
| `docs/adr/0003-locking-model.md` | Cross-reference this ADR at the acquire protocol |
| `docs/CODE-ADAPTER.md` | Note that `--ttl-min` is now capped, reinforcing its existing "re-scope rather than race expiry" guidance |

### Accepted costs

- `assert_lease_held` gains a filesystem read on every mutating operation. Against a
  45-minute lease and a 30-second settle, immaterial.
- Reclamation gets slower and may DENY where it previously proceeded. Correct: the previous
  behaviour proceeded by not checking.

### Explicitly out of scope

- The 45-minute TTL, 5-minute skew and 30-second settle are **not** revisited. They are
  ratified and nothing found here bears on them.
- Whether `task.lease` should exist at all. Making the lock the sole authority (D1) is
  sufficient; deleting the projection is a larger change with no demonstrated defect
  behind it.
- OI-266's sidecar leak, except where D2's mechanism subsumes it.
- **Lease renewal.** Surfaced while deriving D3: renewal is specified in three places —
  AAOS-SPEC.md §7 and §"Lease expiry is 45 minutes, renewable", and ADR-0003 §32 ("The
  lease is renewable: the holder can request a renewal before expiry, emitting a
  `LeaseRenewed` event") — and **is not implemented**. There is no renew tool and no
  `LeaseRenewed` emitter in `tools/`. This is load-bearing beyond documentation drift:
  ADR-0003's reclamation rule conditions reclaim on "no renewal on record", a clause that
  can never be false today, and a long-running holder has no way to keep a lease it is
  actively using. Filed separately; not fixed here. D3's derivation is stated to hold
  either way, so implementing renewal later does not invalidate the ceiling.

### If this is not done

The known state is: two callers can both ACQUIRE (OI-265, demonstrated); the designated
backstop cannot detect it (`assert_lease_held` reads the projection); a bogus expiry wedges
a task silently; and a stale intermediate file in `locks/` can cause the sweep to destroy a
live holder's lease (OI-266, demonstrated). AAOS currently has one documented live
Code↔Cowork round-trip and no observed contention incident, so the realised risk today is
low — but the guarantee the system exists to provide is not currently held.

## Verification

This ADR is satisfied when, in addition to the suite being green:

1. A forced three-caller interleaving across the reclaim path produces at most one
   `ACQUIRED` (the interleaving that refuted attempt 2).
2. The wedge state **as produced by `lease-task.py`** — lock and task lease both
   far-future — is reclaimable by `acquire_lease` and reported by `expire-leases.py`.
3. Reverting any single ownership-deciding call site to its pre-change form fails the
   suite. Demonstrated by mutation, per D4.
4. `assert_lease_held` rejects a holder whose `task.lease` says they hold the lease while
   the lock file says otherwise. This test fails against today's code.

## Amendment — 2026-10-17 (AMD-0006: ownership-transfer requirements for lease and drain locks)

**PROPOSED. RED, docs-only. Not in force until the ratification record at the end of this section is filled in; merging without it ratifies nothing.** It changes no code. **D2 is amended** (see section A, which states plainly what changes); **D1, D3, D4 and every ratified constant** (TTL 45 minutes, skew 5 minutes, settle 30 seconds, `LEASE_TTL_CAP` 45, `LEASE_EXPIRY_CEILING` 90) are **unchanged**. This amendment states **requirements and prohibitions**. The concrete mechanism is deliberately left to the A1a implementation PR and its design note, to be demonstrated against these requirements by forced-interleaving tests, because two reviews refuted mechanism-level text written here (a token protocol whose creation and release recreated the defects it was meant to close). Sources: audit findings M01, M02, M04, M11, M32, M33 and two independent adversarial reviews. Companions: AMD-0003, AMD-0004. Where the hardened plan (PR #13) disagrees with this text, this text governs; see the plan's "Superseded by the amendments" section.

### A. What D2 becomes

D2 decided an "if and only if" compare-and-swap and left the mechanism open. **Terminology: the amended mechanism is a token-serialized swap, not a compare-and-swap, and this amendment does not call it one.** A local filesystem offers no native compare-and-swap on a path, and `archive_lock_if_unchanged` is not one (its docstring says so). D2 is therefore **amended**: the "if and only if" becomes a requirement to *approximate* it, with every residual window **bounded, documented and detected**, plus the commit-time backstop of D1. This relaxes D2's literal guarantee, and Verification #1 ("at most one `ACQUIRED` across a forced three-caller interleaving") stays required: the implementation must meet it for every interleaving it claims to close and must *report* the stall windows it cannot close, rather than assert them away. D2's two properties stay as the target: the lock path is not vacated for a lock we do not intend to reclaim; the swap is atomic against a concurrent `os.link` create.

### B. Requirements for every ownership-deciding operation on a lock file

These apply to lease locks and to the drain lock (AMD-0004), and to every mutator of an existing lock file. The known sites, to be re-derived by a grep in the implementing PR and kept in a test, are: the acquirer's reclaim and the collision archive in `acquire_lease`; the CONTESTED archive; the `acquire_lease` rollback on an exception; the sidecar archive in `acquire_lease` (`archive_lock(sidecar)` after a persisting collision, `_aaos_common.py` ~:928); `archive_lock_after_commit` (complete-task, fail-task, post-handoff); the `lease-task` rollback; the `expire-leases` reclaim archive (`archive_lock`, `expire-leases.py` ~:197) and its orphan-lock archive (`archive_lock_if_unchanged`, `expire-leases.py` ~:166); `_restore_archived_lock`; and the release of the drain lock.

**Cowork is not a reason to skip any of these.** A claim that a window is "moot for Cowork" is valid only while AMD-0003 rule R1 is in force **and enforced**: R1 is honour-system until the bootstrap is edited and the mirror republished, and it lifts when its lift condition is met. Any such claim MUST cite R1's enforcement state, and no requirement below is waived on the strength of it.

1. **No unconditional action by path.** An archive, replace, rename or unlink of a lock file on an ownership path MUST be preceded by a verification that the file's content is the content that was judged, and MUST be serialized against other mutators of that lock. `archive_lock_if_unchanged` MUST NOT be used as if it were a compare-and-swap.
2. **Helper files are published complete and released by content.** Any lock-side helper file (a reclaim token, a swap sidecar) MUST be published complete, never created and then written (the zero-byte window already caused a steal under drain-lock contention), and any release of a helper MUST verify, by content, that it is still the releaser's own. A stalled holder that aborts MUST NOT remove its successor's helper. In particular a reclaim token MUST NOT be created with a bare `O_EXCL` create followed by a write (`_exclusive_create`), which recreates the zero-byte hazard of OI-264 and M32: it is published through a complete, fsynced sidecar and `os.link`, as `_exclusive_publish` does (`_aaos_common.py` ~:1120).
3. **Residual windows are documented, bounded and detected.** For each verify-then-act window the implementation cannot close, the design note states the window, its bound, and how it is detected. "Detected by the settle-and-rescan" is **not sufficient** for an overwrite that lands after the victim's settle ends (the settle may be 0), and the sweep's and release paths have the same verify-then-act shape as the acquirer's swap. The commit-time `assert_lease_held` (D1) is the backstop that must catch a lost lease before a state commit. **Settle-and-rescan covers only the first settle window after an acquisition** (30 seconds as ratified, and possibly 0); an overwrite after that is invisible to it. So `assert_lease_held` reading the lock file (D1, plan item A6b) is the real backstop and a **hard prerequisite**: no design note may claim a residual window is bounded or acceptable, and no residual may be recorded as "detected", until `assert_lease_held` verifies against the lock file.
4. **Conflict copies of helper files are signals.** A conflict-shaped copy of a helper file indicates two parties held it; it MUST be detected (consistent with ADR-0003's rule that any conflict copy is CONTESTED), not silently ignored.
5. **Intermediate file names.** New helper files introduced by this amendment (the reclaim token and the swap file) MUST use the existing `.tmp-` naming convention (for example `<lock>.tmp-<purpose>-<nonce>`, where the nonce is random per use and not the process id: a pid is reused, so a `.tmp-swap-<pid>` name can collide with a stale helper of an earlier process with the same pid). This does not amend ADR-0003 Decision step 2: the existing staging sidecar `<task-id>.lock.tmp-<agent>-<pid>` keeps its name, and its pid-reuse exposure is left as it is, because the sweep today skips any name containing `.tmp-`. Names that do not (`<lock>.reclaim-token`, `<lock>.swap-<pid>`, as in a first draft) are loaded as locks by the current sweep and can requeue a live lease (reproduced). ADR-0006's own Consequences row also requires that sweep enumeration "accounts for any new intermediate filenames rather than filtering by an ad-hoc name fragment": the sweep moves to an **allowlist** (a canonical lock name or a conflict-copy shape, using a task-independent pattern) one PR *before* any code creates new helper names, with a test that feeds it helper and stale-helper names. The sweep's existing filter is `".tmp-" not in p.name` (`expire-leases.py` ~:115). The allowlist **cannot reuse `_matches_conflict_shape(filename, task_id)` as written**: it needs a task id, and the sweep has none when it enumerates (the task id comes from the lock's own content, `task_ref`, only after the file is loaded, which is exactly what the allowlist must decide before). The pattern is therefore task-id-agnostic and anchored over the whole name: the task-id charset of `schemas/task.schema.json` (`t-[a-z0-9-]+`), then the conflict-copy suffix grammar of `_CONFLICT_SUFFIX_RE`, then `.lock`, and no `.tmp-` segment (the implementing PR fixes the exact expression and tests it). **Files the sweep ignores MUST be listed by the health check**, never skipped silently. Orphaned helper files are listed by the health check after a bounded age and archived, never deleted.
6. **`os.link` exclusive create is atomic where hardlinks are supported.** Where the filesystem refuses hardlinks, `_exclusive_link` falls back to `_exclusive_create`, which is non-atomic: the lock exists, empty, between the create and the write (audit M32, pinned by a `@known_defect` test; the OI-264 zero-byte window returns on such a filesystem). `_exclusive_publish` has **no fallback**: an `OSError` from `os.link` propagates out of the drain-lock acquire. The design note decides whether the lock-side fallback is acceptable and records it, and a row in the conforming-changes table (section C) tracks it. **OPEN: either make hardlink-refusing filesystems a supported-and-fixed case (the conforming change in section C), or record M32 as an explicit non-goal of this amendment; the author has not decided.**
7. **A failed swap fails closed.** If the `os.replace` of the swap fails (on Windows a sharing violation or `PermissionError` from a concurrent reader is the routine cause), the caller MUST release its token and **lose** (report DENIED or escalate), never proceed as if the swap had happened and never leave the token behind. A bounded retry is allowed before losing; the bound is an elapsed-time budget recorded in the design note, and every retry re-verifies that the lock content is still the content that was judged.

### C. Extended conforming-changes table

| Call site | Required change | Audit |
|---|---|---|
| Every mutator listed in section B | Satisfy B.1-B.4 | M01, M02 |
| `tools/expire-leases.py` reclaim | After loading the task, **re-match** `task.lease` to the judged lock; skip if they no longer correspond | M02 |
| `tools/expire-leases.py` enumeration | Allowlist (B.5) | M02 |
| `lease-task.py` idempotent no-op | A same-holder, same-session lease that is **expired** is not a no-op success; it falls through to normal acquisition | M04 |
| `acquire_lease` and `reclaim_decision` | A lock whose `expires` is missing, naive or unparseable is treated as corrupt and **escalated**, never silently skipped | M11 |
| `_release_drain_lock` and the drain-lock acquire | Satisfy B (AMD-0004 section A) | M01 |
| `_exclusive_link` fallback and `_exclusive_publish` on a hardlink-refusing filesystem | Remove the non-atomic `_exclusive_create` fallback or fence it, and define `_exclusive_publish`'s behaviour on `OSError`; **or** record M32 as an explicit non-goal (B.6, OPEN) | M32 |
| Tests | A characterization test for a far-future-TTL lease as `lease-task.py` produces it; the `@known_defect` tests (M01 steal and release, M02 sweep, M31, M32) become ordinary regression tests in the PRs that fix them | M33, M31, M32 |

### D. Recorded non-change (M03)

D3 stays as ratified. The 5-minute skew is **not** turned into a floor on foreign-lease judging (ballot Q4, option A); the CLI rejects only invalid and absurd values (AMD-0003 section B). A floor would be a separate, explicit D3 amendment.

## Consequences of this amendment

- `tools/_aaos_common.py`: the A1a PR implements B with a design note and forced-interleaving tests (including the stall windows it cannot close); the A1b and A6 PRs adopt it at the drain lock and the lease paths. `assert_lease_held` still moves to the lock file under D1.
- ADR-0006 "Verification" gains: **5.** the drain-lock steal and release pass the same forced three-caller interleaving as the lease reclaim path; **6.** each residual window is exercised by a forced interleaving and its outcome is recorded in the design note.
- No new threshold is introduced and no ratified number changes.

### Ratification record
**Cross-PR ratification order: ratify PR #20 (AMD-0006) first, then PR #19 (AMD-0004), then PR #18 (AMD-0003).** AMD-0004 depends on this amendment for the drain-lock primitive, and AMD-0003 depends on this one and cites AMD-0004 section C for the `.part` rename; any other order leaves a ratified text citing an unratified one.

Pending. Completed by a docs commit on Gabriel's instruction (PR #20 is already merged, and merging it ratified nothing): that commit replaces this paragraph, not the ordering note above it, with `Ratified by Gabriel, <date>, scope: <sections>, PR #20`, and replaces the PROPOSED banner at the top of this file's Status section. Code PRs cite that commit.
