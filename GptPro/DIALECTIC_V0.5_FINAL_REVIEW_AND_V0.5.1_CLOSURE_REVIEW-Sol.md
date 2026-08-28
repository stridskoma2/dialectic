# Review Feedback: Dialectic v0.5 Final Review Reconciliation and v0.5.1 Closure

**Reviewed document:** `DIALECTIC_V0.5_FINAL_REVIEW_AND_V0.5.1_CLOSURE.md`  
**Review date:** 2026-08-28  
**Reviewer:** Sol  
**Review type:** Read-only closure-patch and freeze-gate review  
**Recommendation:** Correct the closure instructions in place, apply them as v0.5.1, perform the promised diff-only verification, then freeze and implement

## 1. Verdict

The reconciliation is accurate: all findings accepted from the v0.5 reviews are
real, contractual, and solvable without changing either workflow, the authority
model, or the MVP scope. No v0.6 design round is warranted.

The proposed patch is not quite ready to apply literally. Its main remaining defect
is the same ordering class it intends to close: concrete binding is placed for the
code-mode driver but not for packet-only reviewers, council participants, or the
moderator. The turn-audit migration also needs to explicitly remove the old
`.response.json` contract and eliminate duplicated stream fields.

Those corrections are bounded edits to this closure document. After they are made,
write v0.5.1, run the specified diff-only gate, and begin Slice 0. The current file
is a patch recipe, not the patched specification itself, so it cannot by itself
receive `PASS_TO_IMPLEMENT`.

## 2. Eight-item closure check

| Item | Result | Evidence and required adjustment |
|---|---|---|
| 1. Generic preflight before workspace; concrete binding after dynamic objects exist | **PARTIAL** | Gate A is correct. Gate B is placed only at the end of code `WORKTREE_SETUP`; packet-only and council role placement remains undefined. See P1-01. |
| 2. Immutable target/launch evidence | **PASS** | `TargetPreflightArtifact` is separated from mutable `run.json`, closed, immutable, and available even when no turn launches. |
| 3. Failure-safe turn-attempt evidence | **PARTIAL** | `TurnAttemptArtifact` and `StreamCaptureResult` are the right contracts, but the old `.response.json` binding and duplicate stream fields are not explicitly removed. See P1-02. |
| 4. Secure run-storage bootstrap exception | **PARTIAL** | The private-at-creation sequence is sound, but atomic publication, collision behavior, and cleanup wording need exact correction. See P2-01. |
| 5. Bounded Windows reader teardown | **PASS** | Abortable queue waits, concurrent bounded joins, resource closure, and `PROCESS_CLEANUP_FAILED` classification close the hang path. |
| 6. Complete strict configuration schema | **PARTIAL** | The missing models are named, but mode-conditional top-level optionality is still left to implementer choice. See P2-02. |
| 7. `TurnPhase` versus `RunPhase` | **PASS** | The alias and `turn_phase` rename cleanly separate the two domains. |
| 8. Probe `unavailable` and `Literal[True]` semantics | **PASS** | The deterministic truth rule and fail-closed `unavailable` outcome are sufficient; retaining `Literal[True]` is correct. |

## 3. Required corrections before applying the closure patch

### P1-01: Gate B has no exact placement for packet-only and council roles

**Affected closure clauses:** section 3.1, especially lines 59-80; final-review
checklist item 1 around line 356.

The general rule at line 61 is correct: concrete binding runs only after every
referenced dynamic object exists. The placement text then specializes that rule
only for the code driver:

- CODE-02 creates its branch and worktree.
- Binding occurs at the end of `WORKTREE_SETUP`.
- The workflow transitions to `DRIVER_INITIAL`.

That does not place binding for the other role families:

- A packet-only reviewer uses a neutral private temporary CWD created for that
  reviewer, not the driver's writable worktree/profile.
- Council mode has no `WORKTREE_SETUP` phase.
- Council participants require bindings before their opening turns.
- The moderator starts later in `MODERATION` with its own fresh session and neutral
  role workspace.

The closure correctly keys a binding by role, alias, and access profile, which means
each of those bindings needs its own exact creation point. “Before the first native
turn” can also be read as before the first turn in the workflow rather than before
the first turn authorized by that binding.

**Smallest correction:** Replace the phase-specific generalization with this
normative rule:

1. Gate B runs separately for every `(role, alias, access_profile)` immediately
   before that binding's first authorized native turn and after all dynamic
   filesystem objects referenced by that binding have been created and opened for
   stable identity.
2. For the code driver, this is the final operation in `WORKTREE_SETUP` before
   `DRIVER_INITIAL`.
3. For each packet-only reviewer, create its neutral private role directory and bind
   it before that reviewer is admitted to the `REVIEWERS` launch barrier.
4. For council participants, create neutral role directories and bindings before
   any opening participant launches. A binding failure launches none of the opening
   cohort.
5. For the moderator, create and bind its neutral role directory before the fresh
   moderator launch in `MODERATION`.
6. A resume revalidates the referenced binding identities/policy before launch; it
   may reuse the immutable binding only if all remain unchanged.

For any Gate B failure, use `FAILED/PREFLIGHT_FAILED`, launch no member of the
affected phase, cancel none because the barrier has not opened, and retain the
already valid run/workspace artifacts. The original no-branch/worktree guarantee
continues to apply only to Gate A failure.

Update final-review checklist item 1 to say “for that role and before that role's
authorized native turn.” This is the one correction that must be made before the
closure instructions are applied.

### P1-02: The response-to-attempt schema migration is incomplete

**Affected closure clauses:** section 3.3; v0.5 artifact trees and schema table;
final-review checklist items 3 and 7.

The four-file request/attempt/stdout/stderr layout is a better contract because
failed launches and cancelled peers cannot fabricate a normalized provider
response. However, the closure never explicitly removes:

- every `<turn-phase>.response.json` entry from the code and council artifact trees;
- the `.response.json -> AgentResponse` schema-binding row;
- prose saying every native call has a request, response, stdout, and stderr set;
- the statement that captured/persisted stream hashes live in `AgentResponse`.

There is also duplicated authority. `StreamCaptureResult` newly owns accepted and
persisted byte counts, hashes, truncation, guard removal, and termination
causality, while the existing `AgentResponse` still owns `stdout_bytes`,
`stderr_bytes`, and four captured/persisted stream hashes. Two closed records can
therefore disagree about the same stream.

**Smallest correction:** State explicitly that v0.5.1:

1. Removes `.response.json` from both artifact trees and deletes its binding-table
   row.
2. Replaces every “request/response/streams” requirement and test assertion with
   “request/attempt/streams.”
3. Embeds `AgentResponse | None` only inside `TurnAttemptArtifact`.
4. Removes the six stream count/hash fields from `AgentResponse`; stream capture is
   supervisor evidence and has one source of truth in the two
   `StreamCaptureResult` values.
5. Defines each referenced artifact hash as SHA-256 of the exact persisted artifact
   bytes, or otherwise names one canonical serialization. A verifier must not have
   to guess what the reference hashes.
6. Defines empty stream files/results for launch failure, so the four-file layout is
   total rather than conditional.

Drop “or equivalent” from final-review checklist item 3. The purpose of this closure
gate is to verify one exact artifact contract, not accept an unspecified substitute.

### P2-01: Bootstrap publication and cleanup need exact filesystem semantics

**Affected closure clauses:** section 3.4 and CORE-017 update.

The private-at-creation bootstrap correctly acknowledges that a run record cannot
be promised when secure storage itself is unavailable. Three details remain:

- The unpublished directory must be a sibling beneath the same state/run root as
  the final directory; otherwise a cross-volume “rename” is not an atomic publish.
- Publication must be no-replace. If the final run ID already exists, the service
  must not overwrite it. It may generate another candidate under a small fixed
  collision-attempt bound or fail bootstrap with exit 2.
- After step 4, the unpublished directory is not empty: it contains `run.json`.
  The current promise to remove an “unpublished empty temporary directory” does not
  describe that failure path.

Specify an exact sibling temporary-name grammar, no-replace atomic publication, and
bounded collision handling. On failure, best-effort remove the exact verified
unpublished directory and its controller-written non-sensitive `CREATED` record.
If removal itself fails, the defensible guarantee is **no published final run
record and no sensitive content in the unpublished directory**, not that no durable
filesystem entry can possibly remain.

CORE-017's expected result must be **replaced**, not merely strengthened: this is a
bootstrap exit-2/no-published-record path, not `PREFLIGHT_FAILED` on an existing run.
Rename the test-table column to “Added or corrected assertion.”

### P2-02: Mode-conditional configuration sections need explicit types and failure behavior

**Affected closure clauses:** section 3.6.

The closure says the complete model composes driver/reviewers and council while
requiring only the section used by the selected command. It does not say whether
those top-level fields are required or nullable. If all are required, a council-only
configuration fails before mode validation. If all are optional, a completely
empty workflow configuration reaches mode validation. Both readings are currently
conforming.

Specify:

- `version` and `limits` are required.
- `driver`, `reviewers`, and `council` are explicitly nullable/optional at the
  structural schema layer.
- Service-side mode validation requires non-null `driver` plus reviewers for
  `code`, and non-null council for `council`; missing active-mode sections are
  `INVALID_INPUT`.
- Present unused sections still validate structurally but their targets are not
  resolved, authenticated, probed, or bound for the selected command.
- The persisted redacted configuration is revalidated against its declared
  artifact schema after known-value substitution; `redacted_field_paths` and any
  retained boolean must agree exactly.

### P2-03: The test updates must amend obsolete expectations, not only add assertions

**Affected closure clauses:** section 4 and freeze-gate step 2.

CORE-017 currently expects private-directory setup to fail during preflight. The
new bootstrap rule deliberately moves that failure before a `RunHandle` exists, so
the old expectation is false. Other rows change terminology from response to
attempt and must be rewritten consistently rather than supplemented with both.

The closure should require a semantic audit of every touched test row:

- CORE-017: exit 2, no published run, no model/repository work, no sensitive
  unpublished content, bounded/best-effort bootstrap cleanup.
- CORE-027/028: one authoritative `StreamCaptureResult` per stream, abortable
  producer, bounded concurrent joins, and deterministic cleanup precedence.
- CORE-030: Gate A ordering plus per-role Gate B placement.
- CODE-001 and COUNCIL-001: request/attempt/stream sets and exact target/binding
  references, with no `.response.json` expectation.

The count remains **30 core + 46 Code Once + 32 Council Once = 108**. “Unchanged
inventory” must mean unchanged IDs/count, not unchanged obsolete expected results.

## 4. Provider and scope validation

No provider-contract change in this closure introduces documentation drift. Current
official OpenAI documentation still states that Codex permission profiles are beta,
are supported on native Windows and Linux/WSL, and do not compose with legacy
`sandbox_mode`/`--sandbox`. Current non-interactive documentation still supports
`--ignore-user-config`, `--ignore-rules`, stdin-oriented automation, JSONL, output
schemas, and session resume.

References checked:

- [OpenAI Codex permission profiles](https://learn.chatgpt.com/docs/permissions)
- [OpenAI Codex non-interactive mode](https://learn.chatgpt.com/docs/non-interactive-mode)

The settled scope remains unchanged:

- Code Once and Council Once retain exactly one bounded pass.
- Codex remains the only writable MVP driver.
- Codex, Claude Code, and Grok Build remain the three native MVP runtimes.
- MCP remains a post-alpha/beta thin ingress over `DialecticService`.
- Gemini remains a separate future adapter, and Claw remains out of scope.
- No daemon, background owner, API transport, retry loop, second review, or extra
  council round has entered the MVP.

## 5. Final closure sequence

1. Amend this closure document for P1-01, P1-02, and the three precise P2 items.
2. Apply the amended instructions once to produce
   `DIALECTIC_MVP_IMPLEMENTATION_AND_TEST_SPEC_V0.5.1.md`.
3. Compare v0.5 to v0.5.1 against the amended eight-item checklist and verify the
   exact 108-row inventory.
4. If the diff-only check passes, freeze the specification and begin Slice 0.

No further broad architecture review is justified. The next review should be only
the literal v0.5-to-v0.5.1 closure check described by this document.

## 6. Finding index

| ID | Priority | Title |
|---|---|---|
| P1-01 | P1 | Gate B placement is incomplete for packet-only and council roles |
| P1-02 | P1 | Attempt migration leaves stale response bindings and duplicate stream authority |
| P2-01 | P2 | Bootstrap publication/collision/cleanup semantics need precision |
| P2-02 | P2 | Mode-conditional configuration sections need explicit optionality |
| P2-03 | P2 | Test rows must replace obsolete expectations, not only add assertions |
