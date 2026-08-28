# Review: Dialectic MVP Implementation and Test Specification v0.2

**Reviewed document:** `DIALECTIC_MVP_IMPLEMENTATION_AND_TEST_SPEC_V0.2.md` (revision 0.2, 1288 lines)

**Prior review:** `DIALECTIC_MVP_SPEC_V0.1_REVIEW-Opus.md` (29 findings, 9 smaller notes, against revision 0.1)

**Review date:** 2026-08-27

**Review question:** did revision 0.2 resolve the v0.1 findings at the mechanism
level, and did the changes introduce new problems?

**Status:** advisory. Nothing in this file is normative until folded into the spec.

---

## 1. Summary verdict

Revision 0.2 resolves **all 29 findings and all 9 smaller notes** from the v0.1
review. Not deflected into caveats — resolved by specifying a mechanism, and in
almost every case backed by a named negative test. The document roughly doubled in
length and the test inventory grew from 46 rows to 86 (core 11 to 23, code 20 to
34, council 15 to 29).

It also caught a defect neither the Codex/Sol review nor this one found: reviewer-
local finding IDs collide across reports, so section CODE-08 now assigns a
controller-owned `finding_key` (`reviewer-a/001`) and CODE-021 tests two reviewers
both emitting local ID `F1`. That is a real bug fixed before implementation.

One change made while resolving a v0.1 finding is a regression, and it is the only
blocking item here: making `overall_vote` a *derived* value that the model must
compute, and failing the entire council run when the model computes it wrong
(N1). Five further findings are ordinary specification gaps.

The document is otherwise ready to implement.

Counts: 1 blocking regression, 5 substantive gaps, 3 smaller notes, 1 test-row
correction.

---

## 2. Disposition of the v0.1 findings

Every row was checked against the v0.2 text rather than assumed. "Resolved by"
cites where the fix lives.

| v0.1 finding | Status | Resolved by |
|---|---|---|
| F-B1 256 KB packet cannot travel on a Windows command line | Resolved | 5.1 forbids prompts in argv; 5.4.2 mandates stdin/ACP per runtime and caps argv elements at 4096 bytes; CORE-013 |
| F-B2 Executable resolution unspecified | Resolved | 5.4.1 requires `shutil.which()`, absolute path with extension, recorded path and CLI version; CORE-012 |
| F-C1 `ROUGH_CONSENSUS` on unanimous rejection | Resolved | Section 4 line 180 requires `0 <= max_dissenters < N`; COUNCIL-06 rule 2 adds `A >= 1`; COUNCIL-016/017/018 |
| F-C2 Dead `blocking_objection_prevents_consensus` key | Resolved | Key deleted; section 4 states blocking objections always prevent consensus with no switch |
| F-C3 Mixed dispositions have no terminal status | Resolved | CODE-10 step 6 ordered precedence rule; CODE-022 |
| F-C4 "Original repository remains untouched" is false | Resolved | Section 2.1 rewritten to the precise claim; CODE-02 states the intentional metadata additions; section 10 requires cleanup commands in output and README; CODE-018 compares status bytes |
| F-C5 "No heuristic acceptance" vs Grok extraction | Resolved | 5.4.4 whole-text-then-single-fence rule; CORE-018 covers zero and two fences |
| F-C6 Redaction over-broad, collides with audit | Resolved | Section 4 declares model/effort/runtime/lens/ID non-secret and retains resolved values; 6.2 sets an 8-character floor on allowlisted names; CORE-004/006 |
| F-C7 CORE-002 has no detection rule | Resolved | Strict schema with `extra="forbid"` and no credential field at all; CORE-002 rewritten to reject an unknown `api_key` without entropy heuristics |
| F-U1 No status enum, no exit codes | Resolved (see N5) | Section 6.3 separates `RunStatus`, `CodeOutcome`, `ConsensusOutcome`, `FailureKind`, plus an exit-code table and a distinct `dial status` mapping |
| F-U2 Code mode has no timeout status | Resolved | `TIMED_OUT` applies to both modes; section 9 fixes wall-clock start and terminal precedence; CODE-030 |
| F-U3 Diff generation not pinned | Resolved | CODE-04 step 7 pins the full command with `-c` overrides and `LC_ALL=C`; CODE-027 |
| F-U4 `max_diff_bytes` measures a per-reviewer quantity | Resolved | `max_diff_bytes` now bounds the diff alone; separate `max_packet_bytes` with `PACKET_TOO_LARGE`; CODE-031, COUNCIL-029 |
| F-U5 No schema for run artifacts | Resolved | Section 6.1 defines seven versioned models with `artifact_schema_version` and `tool_version`; CORE-015 |
| F-U6 No run-id format | Resolved | Section 6 grammar with regex and 50-bit suffix; validation before path joining; CORE-014, CORE-021 |
| F-U7 Reviewer config schema only shown by example | Resolved | 5.3 adds `ReviewerSpec`/`ParticipantSpec`/`ModeratorSpec` with `extra="forbid"`; lens bounded by `max_lens_chars`; unsupported effort fails preflight rather than being dropped |
| F-U8 Council has no session-ID guard | Resolved | 5.4.5 and COUNCIL-02 fail as `NO_QUORUM` before the moderator starts; COUNCIL-024 |
| F-U9 Asymmetric validation strictness | Resolved | COUNCIL-04 validates proposition IDs and supporting aliases; COUNCIL-05 requires exact ID-set equality; COUNCIL-020/021 |
| F-U10 Proposition votes collected but never used | Resolved, with a regression | COUNCIL-05 now derives `overall_vote` from proposition votes — see N1 |
| F-U11 Cross-examination ledger composition undefined | Resolved | COUNCIL-03 includes the participant's own position and tells it its own alias; COUNCIL-025 |
| F-U12 A `fixed` disposition is never verified | Resolved | CODE-10 step 2 fails as `REPAIR_FAILED` on an empty aggregate repair diff; CODE-023 |
| F-U13 Native CLIs load global config | Resolved | 5.4.3 names the per-CLI isolation flags, records effective flags, and states plainly what is not confined; section 2.1 and section 10 record the residual risk; post-MVP item 11 |
| F-U14 Child process trees survive termination | Resolved | Section 9 specifies process groups and Job Objects; `ProcessSupervisor` component; `PROCESS_CLEANUP_FAILED`; CORE-007/008/019 with grandchild sentinels |
| F-U15 No repository-level locking | Resolved | `RepositoryLock` component, CODE-01 step 4, lock path in section 7.3; CODE-029 |
| F-U16 Worktree lacks gitignored files | Resolved | CODE-03 warns the driver explicitly and excludes environment repair from the task; CODE-032 |
| F-U17 No rate-limit note | Resolved | Section 2.2, CODE-07, and section 9 state fail-closed behavior; post-MVP item 12 |
| F-T1 MUSTs with no test | Resolved | CORE-016 (counts/IDs/bounds), COUNCIL-026, CODE-028 (missing session ID), CODE-030 (code wall clock), CODE-022 (mixed dispositions), CODE-006 (worktree-path sentinel) |
| F-T2 CORE-009 is a wall-clock assertion | Resolved | CORE-009 now asserts recorded interval overlap and explicitly uses no wall-clock ratio |
| F-T3 CORE-011 needs a comparison rule | Resolved | CORE-011 normalizes run IDs, timestamps, and paths before comparing |

The nine smaller notes are also resolved: `confidence` carries `Field(ge=0.0,
le=1.0)`; CODE-04 step 2 is reduced to the plain `NO_CHANGES` condition; the
snapshot-before-validation ordering is now stated and reported; a finding naming a
path outside the diff is explicitly passed through (CODE-033); CORE-010 decides the
model-mismatch question and introduces `MODEL_MISMATCH`; CODE-003 asserts on
operation type rather than session-ID inequality; macOS is explicitly excluded from
the definition of done; and Slice 2's exit criterion is marked an opt-in manual
verification with fixtures as the CI gate.

---

## 3. Blocking regression

### N1 — A derived `overall_vote` lets one model's bookkeeping slip destroy the whole council run

**Refers to:** COUNCIL-05 line 912 (coherence rule) and line 913 (invalid ballots
fail as `NO_QUORUM`); COUNCIL-06 lines 917-921; test COUNCIL-023.

**Severity:** blocking.

Resolving F-U10 by validating ballot coherence was the right instinct, but the
implementation makes `overall_vote` a total function of `proposition_votes` and
`blocking_objection`:

> The overall vote is a checked summary of proposition votes: any blocking
> objection or proposition rejection requires `reject`; otherwise any proposition
> abstention requires `abstain`; otherwise all propositions accept and the overall
> vote must be `accept`.

and then fails a non-conforming ballot as `NO_QUORUM`. Two consequences follow.

**First, the model is asked to compute a value the controller already owns every
input for, and the run dies when it gets it wrong.** A participant that accepts
every proposition but writes `overall_vote: "abstain"` — out of hedging, or simple
inattention — produces an invalid ballot, which fails the participant phase, which
fails the run. All participants' opening positions, revisions, and the moderator's
candidate are discarded, and the user gets `FAILED`/`NO_QUORUM` rather than any
consensus outcome. This is a likely failure mode, not an exotic one: it is exactly
the kind of derived-field bookkeeping that models are weakest at, applied at the
last step of the most expensive workflow in the product.

**Second, it contradicts the specification's own organizing principle.** Section
2.1 states that the controller, not an AI agent, owns consensus calculation, and
COUNCIL-06 repeats that the controller calculates the outcome without model
judgment. Requiring the model to perform the first step of that calculation, and
treating its arithmetic as load-bearing, moves a deterministic computation back
across the boundary the whole design exists to hold.

There is also a semantic side effect worth deciding deliberately rather than
inheriting: because `overall_vote` is now fully determined by per-proposition
votes, `UNANIMOUS` requires every participant to accept **every** proposition. A
participant that endorses the answer but rejects one minor proposition is forced to
`reject` overall. With `N = 3` and `max_dissenters: 1`, two participants each
rejecting one *different* minor proposition yields `A = 1` and `CONTESTED`. The
practical bar for consensus is considerably higher in 0.2 than in 0.1, and nothing
in the document says so.

**Recommended fix.**

1. The controller derives `overall_vote` from `proposition_votes` and
   `blocking_objection` using the rule already written at line 912. This is a pure
   function of validated inputs and belongs in `ConsensusCalculator`.
2. Keep the model's `overall_vote` only as an advisory self-report, or remove it
   from `CouncilBallot` entirely. If retained, a mismatch between the reported and
   derived value is recorded as an event and surfaced in the report — it is not a
   ballot validity failure.
3. Reserve `NO_QUORUM` for what its name says: a required participant failed to
   produce a usable artifact. Per-proposition votes that are complete, unique, and
   in range are a usable artifact.
4. Keep the genuine consistency checks, which do not depend on model arithmetic:
   exact proposition-ID set equality, `blocking_objection=true` requiring non-empty
   evidence, and `blocking_objection=false` requiring null evidence.
5. State the raised consensus bar explicitly in COUNCIL-06, since it is now a
   product decision rather than an implementation detail.

**Tests to change.** COUNCIL-023 currently asserts that a disagreeing overall vote
invalidates the ballot. Restate it: the derived overall vote governs, the reported
value is recorded, and the run continues to a normal outcome. Add a case where one
participant reports an incoherent overall vote and the run still finalizes with the
outcome computed from proposition votes.

---

## 4. Substantive gaps

### N2 — No `max_propositions`, so the moderator sets the difficulty of the vote it does not participate in

**Refers to:** COUNCIL-04 validation paragraph; section 4 limits table.

**Severity:** medium.

Revision 0.2 bounds reviewers, participants, input bytes, diff bytes, packet bytes,
lens characters, and every timeout — with a hard ceiling table so that no
configuration can produce an unbounded local run. Proposition count is the one
quantity left free, and under the N1 coherence rule it is the quantity that
determines how hard consensus is to reach: `UNANIMOUS` requires unanimous
acceptance of every proposition, so a moderator that emits twenty propositions makes
`CONTESTED` close to certain.

That is an unstated coupling between a model's stylistic choice and the product's
headline outcome, in a document that otherwise removes exactly this kind of
non-determinism.

**Recommended fix.** Add `max_propositions` to `limits` with a hard ceiling
(1..20 is a reasonable range, defaulting near 8), validate the moderator artifact
against it, and fail as `MODERATOR_FAILED` on overflow. State the coupling between
proposition count and consensus difficulty in COUNCIL-04 so the bound reads as a
product decision rather than a size guard.

**Test to add:** moderator returns more than `max_propositions` propositions →
`MODERATOR_FAILED`, no ballots run.

### N3 — A driver-added binary fails as `UNSUPPORTED_REPOSITORY`

**Refers to:** CODE-04 step 6 (line 630); test CODE-024.

**Severity:** medium.

The failure kind names the wrong subject. The repository is supported and passed
preflight; what is unsupported is a change the driver made during this run. A user
who sees `UNSUPPORTED_REPOSITORY` after a successful preflight will go looking for a
repository problem that does not exist, and the distinction matters operationally:
`UNSUPPORTED_REPOSITORY` at preflight means "never run Dialectic here",
while the same kind at snapshot means "this run's diff contained a binary."

**Recommended fix.** Add `UNSUPPORTED_CHANGE` to `FailureKind` and use it for
CODE-04 steps 3 and 6 — binary additions or modifications, and paths newly matched
by a clean/process filter. Keep `UNSUPPORTED_REPOSITORY` for the CODE-01 preflight
rejections (sparse checkout, submodules, LFS, pre-existing filtered paths).
Update CODE-024 and CODE-034 accordingly.

### N4 — Staging scope is ambiguous about ignored files

**Refers to:** CODE-04 step 3 (line 629); compare CODE-01 step 5 (line 588).

**Severity:** medium.

Preflight is precise — "no staged, unstaged, or untracked non-ignored files."
CODE-04 step 3 says only "every staged, unstaged, and untracked path that would be
included," leaving the ignore rules implicit at the one point where the driver has
just created files.

It matters because of a live interaction with two other requirements. CODE-03
instructs the driver to run whatever narrow checks it considers appropriate, which
in a Python repository generates `__pycache__` and `.pyc` files. In a repository
whose `.gitignore` does not cover them, those become untracked binary additions,
which CODE-04 step 6 rejects — so the run fails on an artifact of the check the
driver was told to run, reported (per N3) as an unsupported *repository*.

**Recommended fix.** Say "untracked non-ignored" in CODE-04 step 3, matching
preflight. Then decide the residual case deliberately: either state that a
repository whose ignore rules do not cover its own build artifacts will fail this
way, or have CODE-03 instruct the driver not to commit build output. The former is
simpler and consistent with the fail-closed posture.

**Test to add:** driver's check run produces ignored bytecode → those paths are
excluded from the snapshot and the run completes normally.

### N5 — Five of sixteen `FailureKind` values have no normative trigger

**Refers to:** section 6.3 `FailureKind` (line 524); CODE-04 opening sentence
(line 624).

**Severity:** medium.

This is the residue of F-U1. The enum is now canonical and exhaustive, but
`INVALID_INPUT`, `PREFLIGHT_FAILED`, `STATE_CORRUPT`, and `INTERNAL_ERROR` appear
nowhere else in the document, and `DRIVER_FAILED` appears only in the enum and in
test row CODE-028. An implementer knows the values exist but not when to emit them,
which is how v0.1's `REPAIR_FAILED` and `MODERATOR_FAILED` came to live only in test
cells.

`DRIVER_FAILED` is the sharpest case. CODE-04 opens "After the driver exits
successfully" and never writes the else-branch: a non-zero driver exit, a driver
timeout, and a driver whose envelope fails to parse are all unhandled in prose, and
only the missing-session-ID case is covered by a test.

**Recommended fix.** Give every `FailureKind` a one-line trigger definition in
section 6.3, and add the missing normative sentence to CODE-03/CODE-04: a driver
that exits non-zero, times out, returns an unparseable envelope, or returns no
resumable session ID fails as `DRIVER_FAILED` before any reviewer starts.

**Tests to add:** driver exits non-zero; driver turn times out. Both →
`DRIVER_FAILED`, zero reviewer invocations, worktree preserved.

### N6 — `phase` is the one field that did not get a type

**Refers to:** `RunRecord.phase` (line 429); `EventRecord.phase` (line 445);
state-machine diagrams in sections 7.2 and 8.2.

**Severity:** low-medium.

Everything around it became a `Literal`, but `phase: str` stayed free-form — and
phase is load-bearing: section 6.3 says `TIMED_OUT` and `CANCELLED` have no product
outcome and that "their phase identifies where work stopped," so phase is the only
machine-readable record of where a timed-out or cancelled run died.

There is also a terminology collision. `CREATED` and `FINALIZED` appear both as
phases in the section 7.2 and 8.2 diagrams and as members of `RunStatus`, so the
same token means two things depending on which field holds it.

**Recommended fix.** Define `CodePhase` and `CouncilPhase` as `Literal` types drawn
from the two state-machine diagrams, and label those diagrams as phase diagrams.
Rename the overlapping tokens (for example `PHASE_CREATED` or, better, drop
`CREATED` from the phase diagrams since `RunStatus` already covers pre-start), so
that no token is valid in both fields.

---

## 5. Smaller notes

- **`UNANIMOUS`'s "and `B` is false" is now unreachable-redundant.** COUNCIL-06 rule
  1 (line 921) guards against a blocking objection, but line 910 requires
  `blocking_objection=true` to carry `overall_vote="reject"`, so any blocking
  objection already forces `A < N`. Harmless, but the rule reads as though the two
  conditions were independent. Either drop the clause or add a sentence noting it is
  a defensive restatement.
- **The controller-reserved turn directory has no name.** Section 5.4.2 (lines
  318-320) requires a "collision-checked, controller-reserved subdirectory of the
  isolated worktree" and states that "a task MUST NOT use the reserved directory for
  product output" — a rule the driver cannot follow, since the document never says
  what the directory is called. Pin the name (for example `.dialectic-turn/`), state
  it in the driver prompt, and have CODE-04 assert its absence before staging.
- **The lock key's canonicalization is undefined.** Section 7.3 (line 597) keys the
  lock on `sha256(canonical-git-common-dir)` without saying what canonical means.
  On Windows the same repository reached as `C:\repo` and `c:\repo`, or through a
  junction or subst drive, must produce one key or the lock silently fails to
  exclude. Specify the normalization (resolve symlinks and junctions, then case-fold
  on Windows) and add a test that two spellings of one repository collide on the
  lock.

Non-blocking observation on the pinned diff: `-c core.quotePath=true` (line 636)
is deterministic, which is the goal, but it renders non-ASCII filenames as octal
escapes in the packet the reviewer reads. `quotePath=false` with the already-fixed
`LC_ALL=C` and UTF-8 handling is equally deterministic and legible. Worth a
deliberate choice rather than inheriting the Git default.

---

## 6. Test-row correction

**COUNCIL-010** reads "One participant abstains and threshold fails | Status
`FINALIZED`, outcome `CONTESTED`." Under the documented example configuration —
`N = 3`, `max_dissenters: 1` — one abstention gives `A = 2`, and
`2 >= 3 - 1` satisfies COUNCIL-06 rule 2, so the outcome is `ROUGH_CONSENSUS`, not
`CONTESTED`. The row is only correct at `max_dissenters: 0`.

**Recommended fix.** Name the configuration in the row, and add its complement: one
abstention at `max_dissenters: 1` yields `ROUGH_CONSENSUS`. The pair pins the
abstention-counts-as-dissent semantics, which is currently implicit.

---

## 7. Suggested order of work

1. **N1** — it changes `CouncilBallot`, `ConsensusCalculator`, COUNCIL-05,
   COUNCIL-06, and two test rows, and it is the only item that can lose a user's
   completed council run.
2. **N2** — small, and it interacts with N1's consensus semantics; decide both
   together.
3. **N5, N3** — the failure-kind cleanup; both are enum and prose work with no
   design consequences.
4. **N4, N6** — precision fixes in Slice 0 and Slice 1 territory.
5. Smaller notes and the COUNCIL-010 correction, in place.

None of these blocks starting Slice 0. N1 should be settled before Slice 3 begins.

---

## 8. Finding index

| ID | Severity | Title |
|---|---|---|
| N1 | blocking | Derived `overall_vote` lets one model's slip destroy the council run |
| N2 | medium | No `max_propositions`; moderator sets consensus difficulty |
| N3 | medium | Driver-added binary fails as `UNSUPPORTED_REPOSITORY` |
| N4 | medium | Staging scope ambiguous about ignored files |
| N5 | medium | Five `FailureKind` values have no normative trigger |
| N6 | low-medium | `phase` is untyped; `CREATED`/`FINALIZED` collide with `RunStatus` |
| N7 | low | `UNANIMOUS`'s blocking-objection clause is unreachable-redundant |
| N8 | low | Reserved turn directory has no name |
| N9 | low | Lock key canonicalization undefined |
| N10 | low | COUNCIL-010 is incorrect at the documented `max_dissenters` |
