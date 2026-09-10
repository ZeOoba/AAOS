# 0006. Lease ownership authority

## Status

**Proposed** — not accepted. This revises a protocol whose thresholds ADR-0003 and
AAOS-SPEC.md §7 fix by ratification, so it needs Gabriel's sign-off before any code
changes against it. Nothing in this ADR has been implemented.

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

### D3 — No lock may assert liveness indefinitely

A lock whose stated `expires` is further ahead than any admissible lease is not believed.
This closes the wedge that AAOS-SPEC.md §7 already rejects in principle ("wedging the
queue"), and must hold in `acquire_lease` **and** `reclaim_decision` — otherwise the
acquirer and the sweep disagree and the task is unleasable while the sweep stays silent,
which is exactly the state attempt 2 left in place.

Two constraints on the ceiling, both learned from attempt 2:

- It MUST carry clock-skew margin above the maximum admissible TTL. Attempt 2 set the
  ceiling and the CLI's `--ttl-min` cap to the same number, making a maximum-length lease
  stealable after one second of backward drift — converting an anti-wedge rule into a
  double-acquire.
- Introducing it changes a ratified threshold set (AAOS-SPEC.md §7 fixes three: TTL 45 min,
  skew 5 min, settle 30 s). A fourth threshold requires the same ratification and a
  matching edit to AAOS-SPEC.md §7, ADR-0003, and `docs/CODE-ADAPTER.md`. **A ceiling far
  above the ratified 45-minute TTL only bounds the wedge; a ceiling near it prevents one.**

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
| `tools/lease-task.py` | `--ttl-min` bounded strictly below the D3 ceiling; a catch-all so no non-`AaosError` escapes as a bare traceback |
| `docs/AAOS-SPEC.md` §7 | State D1 explicitly; correct the `assert_lease_held` sentence, which currently claims a protection that artifact cannot provide; document the fourth threshold |
| `docs/adr/0003-locking-model.md` | Cross-reference this ADR at the acquire protocol |

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
