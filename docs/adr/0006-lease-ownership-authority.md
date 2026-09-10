# 0006. Lease ownership authority

## Status

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
