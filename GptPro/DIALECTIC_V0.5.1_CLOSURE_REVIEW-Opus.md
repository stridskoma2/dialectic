# Review: v0.5 Final Review Reconciliation and v0.5.1 Closure

**Reviewed document:** `DIALECTIC_V0.5_FINAL_REVIEW_AND_V0.5.1_CLOSURE.md` (440 lines)

**Also consulted:** `DIALECTIC_MVP_IMPLEMENTATION_AND_TEST_SPEC_V0.5.md`, to verify the
findings the closure accepts and the clauses the patch would change

**Review date:** 2026-08-28

**Review question:** are the eight dispositions right, is the patch text complete
enough to apply without a further round, and is the freeze gate sound?

**Status:** advisory.

---

## 1. Verdict

**Agree with the recommendation: patch, freeze, implement.** No v0.6 architecture
round is warranted, and the closure document is right that the remaining items are
contractual rather than structural.

One correction to make *before* applying the patch, not after: closure item 3.1
fixes the preflight-ordering bug for the code-mode driver and leaves the identical
bug in place for council mode and every packet-only role (C-1). It is the same
defect class the patch exists to close, so applying 3.1 as written would produce a
v0.5.1 that still cannot be implemented literally for two of the three role families.

Four smaller items follow. All five are patch-text corrections; none reopens a
settled decision, changes a workflow, or moves the 108-test count.

Counts: 1 must-fix-before-applying, 2 should-fix, 2 minor.

---

## 2. On the two reviews being reconciled

The closure document's characterization is accurate and I want to be direct about
it. Sol found four real defects I did not, and three of them are the same class —
a closed schema contradicting normative prose — that I did catch once, as R5-2.
Having found that pattern, I should have swept for it. Verified against v0.5:

- **Launch-plan evidence.** §5.4.1 step 3 says to record the resolved launcher path,
  spawned root executable, launch kind, and CLI version "in `run.json`". `RunRecord`
  (lines 626-640) declares none of those fields, and §6.1 says artifact-schema
  version 1 "rejects undeclared fields." Confirmed contradiction.
- **Concrete binding ordering.** CODE-01 step 10 preflights every target "as
  specified in section 5.4"; §5.4.1 requires each run to record a
  `CapabilityBindingArtifact` carrying `DynamicFilesystemIdentity` entries including
  role `isolated_worktree`; CODE-02 creates that worktree afterward. Confirmed
  contradiction.
- **Response-only audit.** The §6.1 binding table requires a `.response.json`
  holding an `AgentResponse` for every call, but `AgentResponse` has non-nullable
  `cli_version`, `exit_code`, and `captured_*_sha256` fields that a launch failure,
  timeout, or fail-fast peer cancellation cannot supply. Confirmed.
- **Run-storage bootstrap.** §5.2 step 1 has `create_run` persist `CREATED` before
  returning a handle; §6.2 requires the private directory or preflight fails; §6.3
  permits a no-record exit only for parser-level errors before a run ID exists. An
  unwritable state root satisfies none of the three. Confirmed.

On severity, Sol's P1 classification is the better one and the closure document is
right to adopt it. I rated my equivalents low-medium on the reasoning that an
implementer would fill the gap correctly within the hour. That is true and beside
the point: the question at an implementation gate is not whether a competent person
recovers, it is whether the document can be implemented literally. Where a closed
schema and a normative clause contradict each other, it cannot, and "the developer
will figure it out" is exactly the judgment this specification spent five revisions
removing from every other decision. I would not repeat that calibration.

The one disposition I would defend as written is the last: retaining
`canonical_instantiation_verified: Literal[True]`. The closure document's reasoning —
the artifact's existence is the proof, and failed construction is represented by run
failure evidence — is correct.

---

## 3. Findings on the patch

### C-1 — Gate B is defined only for the code-mode driver; council mode and packet-only roles keep the bug

**Refers to:** closure §3.1, Gate B; v0.5 `CouncilPhase`; v0.5 §5.4.4 and CODE-05
(neutral temporary CWD); `DynamicFilesystemIdentity.role`.

**Severity:** must fix before applying the patch.

Gate B's rule is right: "Run only after every referenced dynamic filesystem object
exists." Its placement is then given exactly once, for one case — "In code mode,
CODE-02 first creates the isolated branch and worktree" and "Concrete binding can be
the final required operation within `WORKTREE_SETUP` before transition to
`DRIVER_INITIAL`."

Two families are left without a home.

**Council mode has no `WORKTREE_SETUP` phase.** `CouncilPhase` is `PREFLIGHT`,
`OPENING_POSITIONS`, `CROSS_EXAMINATION`, `MODERATION`, `BALLOTS`, `REPORTING`.
Council participants and the moderator still require concrete bindings —
`DynamicFilesystemIdentity` includes `state_root`, `saved_auth_path`, `os_temp_root`,
and `outside_sentinel`, all of which apply to packet-only roles. The patch says "No
additional run phase is necessary," which is true for code mode and leaves council
mode with no named point at which Gate B runs.

**Packet-only roles have per-turn neutral directories whose creation time is
unspecified.** v0.5 requires reviewers to run "with a neutral private temporary
directory" (CODE-05) and packet-only Codex roles to use "a neutral temporary CWD"
(§5.4.4), but nowhere states when those are created. A reviewer's first native turn
is in the `REVIEWERS` phase, long after `WORKTREE_SETUP`. Binding a reviewer at
`WORKTREE_SETUP` would reference a directory that does not exist yet — the precise
failure Gate B was introduced to eliminate, reproduced one level down.

The patch's own rule that bindings are "keyed by **role, alias, and access
profile**" makes this sharper, not softer: it means there is a distinct binding per
alias, so each one needs its own defined "after its objects exist" point.

**Recommended correction.** Replace the single placement sentence with a rule
expressed in terms of the objects rather than one phase:

1. Gate B for a target runs immediately before that target's first native turn, and
   after every dynamic filesystem object its binding references has been created and
   its stable identity obtained.
2. For the code-mode driver that point is the end of `WORKTREE_SETUP`, since the
   isolated worktree is its last dependency.
3. For packet-only roles it is the point at which that role's neutral private
   directory is created — which v0.5 should also state explicitly, since it is
   currently assumed rather than specified. Creating it as part of the same
   operation that binds the role is the simplest fix.
4. In council mode Gate B therefore runs inside `OPENING_POSITIONS` for participants
   and `MODERATION` for the moderator, before the first launch in each.

Keep the patch's existing guarantee that a Gate A failure creates no branch or
worktree, and extend the Gate B failure rule — `FAILED`/`PREFLIGHT_FAILED`, no model
launched, artifacts retained — to a Gate B failure in any phase, not only after
CODE-02.

The closure prompt's checklist item 1 should then read "concrete role/access binding
occurs only after every referenced dynamic filesystem object exists **for that role**
and before that role's authorized native turn," so the verification catches this.

### C-2 — The `.response.json` binding's fate is unstated, and stream evidence is now duplicated

**Refers to:** closure §3.3; v0.5 §6.1 binding table and the paragraph following it;
`AgentResponse` fields.

**Severity:** should fix.

§3.3 gives a four-file layout containing `.request.json`, `.attempt.json`,
`.stdout.txt`, and `.stderr.txt` — no `.response.json` — and embeds
`agent_response: AgentResponse | None` inside the attempt artifact. The intent is
clear, but the patch never says that the `turns/<role>/<alias>/<phase>.response.json`
row is deleted from the binding table. §6.1's rule is "Every JSON filename has one
unambiguous schema binding," so a stale row is not a cosmetic leftover; it is a
second binding for a file that no longer exists, and an implementer could reasonably
write both files.

Two pieces of v0.5 prose depend on that row and also need editing: the binding-table
note that "bounded raw streams remain adjacent text files," and the closing paragraph
defining `captured_*_sha256` and `persisted_*_sha256` "in `AgentResponse`."

That last one raises the substantive half. `StreamCaptureResult` now carries
configured cap, accepted-prefix byte count, captured-prefix SHA-256, guard bytes
discarded, truncation flag, persisted byte count, persisted SHA-256, and whether the
stream triggered termination. `AgentResponse` already carries `stdout_bytes`,
`stderr_bytes`, `captured_stdout_sha256`, `captured_stderr_sha256`,
`persisted_stdout_sha256`, and `persisted_stderr_sha256`. After the patch, a
successful turn records the same four values twice, in two nested artifacts, with no
stated precedence and no stated equality invariant.

**Recommended correction.** State that `.response.json` is removed and its binding
row deleted; move the two dependent prose sentences to `TurnAttemptArtifact` and
`StreamCaptureResult`; and resolve the duplication by removing the six stream fields
from `AgentResponse` in favor of `StreamCaptureResult` — `AgentResponse` is described
as "the normalized successful native response," and stream capture is a supervisor
fact, not a provider fact. If they are kept in both, add a validator requiring
equality, so the two cannot drift.

### C-3 — CORE-017 needs a corrected expectation, not an added assertion

**Refers to:** closure §4 test table and §5 freeze gate; v0.5 CORE-017 (line 1437).

**Severity:** should fix.

The §4 table is headed "Added assertion," and for most rows that is accurate. For
CORE-017 it is not. The existing expected result is:

> Private run-directory permissions cannot be established | **Preflight fails
> without launching a model**

Under §3.4, that outcome no longer occurs. Bootstrap failure now happens *before*
`create_run` returns a handle — therefore before preflight — and produces exit 2
with no durable run record at all. The row's expectation is contradicted by the
patch, not extended by it.

CORE-030 is a milder version of the same thing: adding "Generic preflight precedes
workspace; concrete role binding follows workspace" changes what the row asserts
about ordering, which is a semantic amendment rather than a strengthening.

This matters because of how the freeze gate is worded. §5 step 2 says "Confirm
Markdown/schema consistency and the **unchanged 108-test inventory**." A count-only
check passes while a row still states an outcome the specification no longer
produces — and CORE-017 sits directly on the bootstrap path the patch exists to
define.

**Recommended correction.** Retitle the §4 column "Added or corrected assertion,"
rewrite CORE-017's expected result to match §3.4 (no durable record, exit 2, no
sensitive content in an unpublished directory, best-effort temp removal), and change
freeze-gate step 2 to confirm that every row the patch touches states an outcome the
patched specification actually produces — not merely that the count is 108.

### C-4 — Mode-conditional configuration sections have no stated optionality

**Refers to:** closure §3.6.

**Severity:** minor.

The patch requires `DialecticConfig` to compose "`version`, driver/reviewers,
council, and limits" under `strict=True, extra="forbid"`, and separately requires
that "service-side mode validation must require only the sections used by the
selected command."

Those two rules interact and the patch does not say how. If `driver`, `reviewers`,
and `council` are required fields, a council-only configuration fails schema
validation before mode validation is ever reached. If they are `| None`, the schema
admits a configuration with no sections at all, and something must reject it. Both
are defensible; two implementers will choose differently, which is the condition
this specification treats as a defect everywhere else.

**Recommended correction.** State that `driver`, `reviewers`, and `council` are
declared optional (`| None`) in `DialecticConfig`, that `version` and `limits` are
required, and that mode requiredness is enforced by service-side validation after
schema validation — with the failure classified `INVALID_INPUT`.

### C-5 — The bootstrap publish step needs two constraints

**Refers to:** closure §3.4, steps 2 and 5.

**Severity:** minor.

The sequence creates an "unpublished temporary run directory" and then "atomically
publish/rename the directory to the final run path." Two conditions are needed for
that rename to be atomic and to fail predictably:

- The temporary directory must be on the same volume as the runs root — otherwise
  the rename is a copy on both platforms and is not atomic. Making it a sibling
  under the state root satisfies this; the patch implies it but does not say it.
- The final path may already exist. Directory rename onto an existing directory
  fails on both platforms, and run-ID collision is negligible but a leftover
  directory from an interrupted publish is not. The patch should say that an
  existing final path is a bootstrap failure under the same exit-2 no-record rule,
  rather than leaving it to `os.replace` semantics.

---

## 4. On the freeze gate and the review prompt

The prompt in §6 is the strongest part of the document and I would change almost
nothing. Three things it gets right that are easy to get wrong:

- **A closed verdict token.** `PASS_TO_IMPLEMENT` or `BLOCKED`, with "Be decisive"
  and "Do not manufacture another revision cycle." A reviewer asked an open question
  will always find something; a reviewer asked a closed one has to justify the
  finding against a threshold.
- **An explicit non-goals list.** Ruling out optional enhancements, naming
  alternatives, "a different design would also work," and prose polish removes the
  large majority of what makes late-stage reviews unproductive.
- **A blocking threshold stated as a property, not a feeling** — internal
  contradiction, undefined required behavior, unbounded operation within a claimed
  bound, missing schema for a required artifact, impossible test oracle, violable
  invariant. Every genuine finding across the last three rounds fits one of those,
  which is good evidence the threshold is calibrated rather than arbitrary.

Two notes.

**One loophole to close.** Checklist item 3 says "Every attempted invocation has a
closed `TurnAttemptArtifact` **or equivalent**." In a checklist meant to be verified
literally, "or equivalent" lets a reviewer pass a partial implementation. Drop the
phrase.

**One thing to be aware of rather than fix.** The instructions say "Work
independently. You have not been given the other reviewer's output," while attaching
this closure document — which contains both reviewers' reconciled findings. That is
the right trade for a closure check: the eight items are the point, and anchoring
both reviewers to them is deliberate. Just do not read a double `PASS_TO_IMPLEMENT`
as two independent confirmations that nothing else exists; it is two confirmations
that the eight items landed. The broader "Also verify" list is what carries any
residual coverage, and it is appropriately scoped.

The "How to use the two results" rules are well judged, particularly "Do not merge
reviewer suggestions automatically merely because they are well written." That is
the correct instinct at a freeze gate, and it is the failure mode a reconciliation
document is most exposed to.

---

## 5. Recommended sequence

1. Fold **C-1** into closure §3.1 before the patch is written, and amend prompt
   checklist item 1 to match.
2. Fold **C-2** and **C-3** into §3.3, §4, and §5.
3. Apply **C-4** and **C-5** as one-line clarifications in §3.6 and §3.4.
4. Write v0.5.1, run the two-reviewer gate, freeze, start Slice 0.

If C-1 is not folded in beforehand, expect at least one `BLOCKED` return, since the
council-mode binding gap is exactly what checklist item 1 asks the reviewers to
verify.

---

## 6. Finding index

| ID | Severity | Title |
|---|---|---|
| C-1 | must fix before applying | Gate B undefined for council mode and packet-only roles |
| C-2 | should fix | `.response.json` fate unstated; duplicated stream evidence |
| C-3 | should fix | CORE-017 needs a corrected expectation, not an addition |
| C-4 | minor | Mode-conditional config sections have no stated optionality |
| C-5 | minor | Bootstrap publish needs same-volume and existing-path rules |
