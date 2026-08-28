# Dialectic v0.5 Final Review Reconciliation and v0.5.1 Closure

**Date:** 2026-08-28  
**Reviewed specification:** `DIALECTIC_MVP_IMPLEMENTATION_AND_TEST_SPEC_V0.5.md`  
**Inputs:** Independent Sol and Opus reviews of specification revision 0.5  
**Recommendation:** Apply one narrow v0.5.1 contract/schema patch, freeze the specification, and begin implementation

## 1. Final verdict

This should be the last review cycle.

Both reviewers agree that Dialectic's workflows, architecture, MCP boundary, and MVP scope are finished. The remaining findings are genuine but purely contractual. They do not justify a product redesign, a v0.6 architecture round, or any expansion of the MVP.

Revision 0.5.1 should close the remaining ordering and schema gaps while preserving all settled decisions:

- **Code Once:** one Codex implementation, parallel independent reviews, one feedback delivery to the same driver session, one repair opportunity, then stop without re-review.
- **Council Once:** blind openings, one anonymized cross-examination round, a fresh moderator, final ballots, controller-derived consensus, then stop.
- Native CLI-first execution through `dial`, with `dialectic` as the equivalent long alias.
- Codex as the only writable MVP driver.
- Native Codex, Claude Code, and Grok Build adapters.
- MCP deferred until after a native alpha or beta and retained only as a thin northbound ingress over `DialecticService`.
- Gemini retained as a future independent `AgentAdapter`.
- No Claw Orchestrator dependency.
- No continuous loop, second review, extra council round, API transport, daemon, or background job ownership in the MVP.

The independent reviews differ mainly on severity. Opus calls the remaining items low or low-medium; Sol classifies two as P1 because the current normative clauses cannot all be implemented literally. Sol's implementation-gate classification is the safer one: patch the specification before treating it as the baseline.

## 2. Finding dispositions

| Finding | Disposition | Required resolution |
|---|---|---|
| Concrete capability binding is ordered before the isolated worktree exists | Accept | Split generic target preflight from concrete post-workspace binding |
| Launch-plan and truncation evidence lack fields in the closed schemas | Accept | Add immutable target evidence and a failure-safe turn-attempt artifact |
| Secure run-directory bootstrap cannot always persist `CREATED` | Accept | Define the unavoidable pre-record bootstrap failure |
| Windows reader threads have no bounded join | Accept | Make queue waits abortable, bound reader teardown, and classify failure |
| `DialecticConfig` is referenced but undefined | Accept | Define the complete strict top-level configuration schema |
| `phase` denotes two unrelated vocabularies | Accept | Rename the request field to `turn_phase` and define `TurnPhase` |
| `CapabilityProbeResult.observed="unavailable"` has no stated result | Accept as tiny clarification | State that `unavailable` always fails the probe |
| `canonical_instantiation_verified: Literal[True]` may look accidental | No functional change needed | Retain it; existing prose already says the artifact is emitted only after successful proof |

## 3. Exact v0.5.1 closure patch

### 3.1 Split generic preflight from concrete binding

Define two distinct readiness gates.

#### Gate A: generic target preflight

Run during CODE-01, before creating a branch or worktree:

- Resolve and fingerprint the executable and spawned root.
- Capture the native CLI version and launch kind.
- Verify authentication without persisting credential material.
- Inspect managed policy and backend/elevation state.
- Validate or refresh the generic `CapabilityAttestationArtifact` using fixture-owned typed sentinels.

Failure remains `PREFLIGHT_FAILED` and creates no branch or worktree.

#### Gate B: concrete role binding

Run only after every referenced dynamic filesystem object exists:

- In code mode, CODE-02 first creates the isolated branch and worktree.
- Open and obtain stable identities for the actual worktree, Git common directory, original worktree, state root, saved-auth paths, temporary roots, and any required sentinel locations.
- Instantiate the exact role/access-specific permission template.
- Byte-compare the complete concrete rule set to the canonical template substitution.
- Persist the `CapabilityBindingArtifact` before the first native turn.

The binding should be keyed by **role, alias, and access profile**, not only by model target. An `@driver` reviewer uses the driver's runtime/model but a fresh packet-only session and a different permission profile.

Every `TurnAttemptArtifact` must reference the exact capability-binding hash that authorized the turn. Resumed calls may reuse the binding only while every bound identity and policy remains unchanged.

If concrete binding fails after CODE-02:

- Set `FAILED/PREFLIGHT_FAILED`.
- Launch no model.
- Retain and report the already-created Dialectic branch/worktree.
- Do not weaken CODE-01's guarantee: generic-gate failures still create neither.

No additional run phase is necessary. Concrete binding can be the final required operation within `WORKTREE_SETUP` before transition to `DRIVER_INITIAL`.

### 3.2 Add immutable target-preflight evidence

Do not add process-launch fields to mutable `run.json`. Add a separately versioned artifact such as:

```text
audit/targets/<role>/<alias>.json
```

Bind it to a closed `TargetPreflightArtifact` containing at least:

- Role and alias.
- Requested target and resolved model alias when known.
- Resolved launcher path and file identity/digest.
- Spawned root executable and file identity/digest.
- Launch kind.
- CLI version.
- Platform/backend/elevation state.
- Adapter fixture and test versions.
- Generic capability-attestation hash.
- Credential environment **names** and denied saved-auth path hashes, never values or contents.
- Canonical non-secret launch-plan hash.

Change section 5.4.1 step 3 to point to this artifact rather than claiming these fields are recorded in `run.json`.

This preserves target evidence even when a target passes preflight but no model turn is ultimately launched.

### 3.3 Replace response-only audit with a turn-attempt contract

Not every attempted invocation can produce a valid `AgentResponse`. Launch failure, timeout, cancellation, overflow, protocol failure, and fail-fast peer cancellation must not require fabricated provider data.

Use this general layout:

```text
turns/<role>/<alias>/<turn-phase>.request.json
turns/<role>/<alias>/<turn-phase>.attempt.json
turns/<role>/<alias>/<turn-phase>.stdout.txt
turns/<role>/<alias>/<turn-phase>.stderr.txt
```

`TurnAttemptArtifact` should contain:

- `artifact_schema_version` and `tool_version`.
- Role, alias, `turn_phase`, operation (`start` or `resume`), and attempt number.
- Request artifact hash.
- Target-preflight artifact hash.
- Capability-binding artifact hash.
- Process start/end timestamps and duration when applicable.
- `agent_response: AgentResponse | None`.
- Closed `termination_reason`, for example:
  - `completed`
  - `nonzero_exit`
  - `launch_failed`
  - `protocol_failed`
  - `timeout`
  - `cancelled`
  - `peer_cancelled`
  - `stdout_overflow`
  - `stderr_overflow`
  - `process_cleanup_failed`
  - `reader_cleanup_failed`
- Resulting `failure_kind` or `null`.
- `process_cleanup_confirmed` and `reader_cleanup_confirmed`.
- Bounded diagnostic or `null`, after redaction.
- Separate `StreamCaptureResult` values for stdout and stderr.

Each `StreamCaptureResult` should contain:

- Configured cap.
- Pre-redaction accepted-prefix byte count.
- Captured-prefix SHA-256.
- Credential-guard bytes discarded.
- Truncation flag.
- Persisted redacted byte count.
- Persisted file SHA-256.
- Whether this stream triggered termination.

`AgentResponse` remains the normalized successful native response and must not be invented when native/provider metadata is unavailable.

### 3.4 Define the secure run-storage bootstrap exception

`create_run` cannot promise a persisted `CREATED` record when secure storage itself cannot be established.

Define this exact sequence:

1. Generate a candidate run ID.
2. Create an unpublished temporary run directory with private permissions **at creation**:
   - POSIX: restrictive creation mode and verification.
   - Windows: explicit private security descriptor/DACL at creation and verification.
3. Write no sensitive content until private permissions have been verified.
4. Atomically write the explicit-null `CREATED` record inside it.
5. Atomically publish/rename the directory to the final run path.
6. Return the opaque `RunHandle` only after publication succeeds.

If bootstrap cannot complete:

- Exit 2 with one bounded controller-formatted diagnostic.
- Launch no model and perform no repository work.
- Create no durable run record.
- Best-effort remove any unpublished empty temporary directory.
- Never write task, prompt, configuration, credential-derived, or model content into an unverified directory.

Once `create_run` returns a handle, every subsequent failure retains the existing persisted-state requirement.

### 3.5 Bound Windows reader teardown

Amend the Windows stream bridge as follows:

- Reader-side byte accounting remains authoritative before enqueue.
- A queue-capacity wait must also observe a supervisor-owned cancellation event.
- Overflow, forced termination, cancellation, fail-fast peer cancellation, and turn teardown set that event.
- A blocked producer wakes without requiring the event loop to free capacity first.
- After process termination, the consumer continues draining/discarding bounded queued data until both readers signal completion.
- Reader joins are bounded by `graceful_kill_seconds` and occur concurrently.
- A reader that does not terminate, or a pipe/thread handle that cannot be closed within the bound, produces `FAILED/PROCESS_CLEANUP_FAILED`.

Extend the `PROCESS_CLEANUP_FAILED` trigger to cover:

- Platform-owned process-unit cleanup.
- Reserved turn-workspace cleanup.
- Reader-thread termination.
- Pipe/thread/job/process handle closure.

This turns CORE-028 failures into deterministic test failures rather than hung tests.

### 3.6 Define the complete configuration schema

Add strict Pydantic models for:

- `DriverSpec`, with `runtime: Literal["codex"]`.
- `ConsensusSpec`.
- `CouncilSpec`.
- `LimitsSpec`, explicitly declaring every field and default/bound from section 4.
- `DialecticConfig`, composing `version`, driver/reviewers, council, and limits.

All use:

```python
model_config = ConfigDict(strict=True, extra="forbid")
```

The schema must also define command-mode requiredness. A combined configuration may contain both code and council sections, but service-side mode validation must require only the sections used by the selected command and must not preflight unused targets.

For `RedactedConfigArtifact`, replace the ambiguous standalone boolean with—or derive it from—a bounded list:

```python
redacted_field_paths: list[str]
```

Use canonical JSON-pointer-style paths in deterministic order. An empty list means no normalized configuration value was changed by known-value redaction. If `redaction_applied` is retained, a validator must require:

```text
redaction_applied == bool(redacted_field_paths)
```

### 3.7 Disambiguate turn phase

Define:

```python
TurnPhase = Literal[
    "initial", "repair", "review", "opening",
    "cross-examination", "candidate", "ballot",
]
```

Rename:

```python
AgentRequestArtifact.phase
```

to:

```python
AgentRequestArtifact.turn_phase
```

Use the same field in `TurnAttemptArtifact`. Keep `RunRecord.phase` and `EventRecord.phase` as `RunPhase | None`.

### 3.8 Clarify probe outcomes

Define `CapabilityProbeResult.passed` deterministically:

```text
passed =
    (expected == "allow" and observed == "allowed") or
    (expected == "deny" and observed == "denied")
```

`observed == "unavailable"` always implies `passed == false` and therefore cannot authorize a capability attestation or binding.

Retain:

```python
canonical_instantiation_verified: Literal[True]
```

The binding artifact exists only after successful canonical construction proof. Failed construction is represented by the run failure/event evidence, not by a negative binding artifact.

## 4. Test updates without increasing the count

Keep the mandatory suite at **108 tests: 30 core, 46 Code Once, and 32 Council Once**. Strengthen existing rows:

| Existing test | Added assertion |
|---|---|
| CORE-004 / CORE-016 | Complete `DialecticConfig` shape, command-mode requiredness, normalized audit configuration, and redacted field paths |
| CORE-017 | Unwritable state root, failed POSIX mode verification, failed Windows DACL verification, no durable record, no sensitive unpublished content |
| CORE-026 | Reader-thread cleanup has a deterministic `PROCESS_CLEANUP_FAILED` trigger |
| CORE-027 | Structured truncation flags, guard count, accepted/persisted byte counts and hashes, and termination reason |
| CORE-028 | Stalled loop, abortable producer wait, bounded reader join, simultaneous stream flood, and exact resource closure |
| CORE-030 | Generic preflight precedes workspace; concrete role binding follows workspace and references stable identities |
| CODE-001 | Every driver/reviewer attempt references exact target and capability evidence |
| CODE-040 | Concrete driver profile omission/addition/reordering/weakening after worktree creation prevents model launch |
| COUNCIL-001 | Every participant/moderator attempt has schema-valid request/attempt/stream evidence and binding reference |

Also add a failure-path artifact walk to the most relevant existing core rows covering launch failure, timeout, cancellation, overflow, protocol failure, and fail-fast cancellation without fabricated `AgentResponse` data.

## 5. Freeze gate

After these edits:

1. Verify the v0.5-to-v0.5.1 diff against this closure list.
2. Confirm Markdown/schema consistency and the unchanged 108-test inventory.
3. Freeze the specification.
4. Begin Slice 0.

Do **not** request another broad architecture review unless the patch changes the workflow, authority model, threat boundary, or MVP scope. A short diff-only closure check is sufficient; it must not become a new findings hunt.

The implementation gate is therefore:

> Apply this v0.5.1 patch, verify the diff, freeze Dialectic's MVP specification, and implement. No v0.6 design round is warranted.

## 6. Prompt for the final Claude and Codex review

Use the following **identical prompt** in separate Claude and Codex sessions. Do not give either reviewer the other reviewer's output before both have finished.

Preferably attach:

1. `DIALECTIC_MVP_IMPLEMENTATION_AND_TEST_SPEC_V0.5.md`
2. `DIALECTIC_MVP_IMPLEMENTATION_AND_TEST_SPEC_V0.5.1.md`
3. `DIALECTIC_V0.5_FINAL_REVIEW_AND_V0.5.1_CLOSURE.md`

If only v0.5.1 is available, remove the sentence requiring a literal v0.5-to-v0.5.1 diff but retain the closure checklist.

### Copy/paste prompt

```text
Perform a final, read-only implementation-gate review of the attached Dialectic MVP specification revision 0.5.1.

This is a narrowly scoped closure verification, not another architecture or product-design review.

Context and authority:

- DIALECTIC_MVP_IMPLEMENTATION_AND_TEST_SPEC_V0.5.1.md is the candidate implementation baseline.
- DIALECTIC_MVP_IMPLEMENTATION_AND_TEST_SPEC_V0.5.md is the prior baseline.
- DIALECTIC_V0.5_FINAL_REVIEW_AND_V0.5.1_CLOSURE.md is the authoritative closure checklist for this review.
- Review the documents; do not edit code, repositories, or files.
- Work independently. You have not been given the other reviewer's output and must not assume what another model concluded.

Settled product decisions are out of scope and must not be reopened:

- Product name Dialectic; primary CLI `dial`; equivalent long alias `dialectic`.
- Code Once performs exactly one Codex implementation, parallel independent reviews, one feedback delivery to the same driver session, one repair opportunity, and then stops without re-review.
- Council Once performs blind openings, exactly one anonymized cross-examination round, a fresh moderator, final ballots, controller-derived consensus, and then stops.
- Codex is the only writable MVP driver.
- Native Codex, Claude Code, and Grok Build adapters are the MVP runtimes.
- MCP remains deferred until after native alpha/beta and is only a future thin northbound ingress over DialecticService.
- Gemini remains a future independent AgentAdapter.
- Claw Orchestrator is not an MVP dependency.
- Continuous loops, second review, extra council rounds, API transport, daemons, background execution, crash resumption, and additional providers remain post-MVP.
- The mandatory offline test inventory remains 30 core + 46 Code Once + 32 Council Once = 108.

Your primary task is to verify that revision 0.5.1 implements all eight closure items completely and consistently:

1. Generic target preflight occurs before worktree creation, while concrete role/access binding occurs only after every referenced dynamic filesystem object exists and before the authorized native turn.
2. Immutable target/launch-plan evidence has a closed schema and no longer relies on undeclared RunRecord fields.
3. Every attempted invocation has a closed TurnAttemptArtifact or equivalent, including failed/timeout/cancelled/overflow/fail-fast attempts, without fabricated AgentResponse/provider data.
4. Secure run-storage bootstrap has an explicit no-record failure before create_run returns, while all failures after a RunHandle exists remain durably persisted.
5. Windows reader queue waits are abortable; reader/handle teardown is time-bounded; failure maps deterministically to PROCESS_CLEANUP_FAILED.
6. DriverSpec, ConsensusSpec, CouncilSpec, LimitsSpec, and DialecticConfig form a complete strict top-level configuration contract, including command-mode requiredness and redaction audit paths.
7. TurnPhase/turn_phase is distinct from RunPhase/phase everywhere in schemas, paths, prose, and tests.
8. Capability probe `unavailable` behavior and Literal[True] binding semantics are explicit and deterministic.

Also verify:

- Every affected artifact-tree path has exactly one schema binding.
- Every schema reference resolves to a defined type.
- Target evidence, capability bindings, requests, attempts, streams, normalized model outputs, and summaries have an unambiguous relationship.
- Every turn references the exact target-preflight and concrete-binding evidence that authorized it.
- No ordering cycle remains among generic preflight, workspace creation, concrete binding, and first model launch.
- Failure kind, terminal status, exit code, artifact retention, branch/worktree retention, and cleanup precedence agree across all affected sections.
- The strengthened existing tests actually assert the new contracts while the enumerated test count remains exactly 108.
- The v0.5.1 patch introduced no regression into Code Once, Council Once, session continuation, Git isolation, packet blindness, deterministic consensus, or the deferred MCP design.

Do not report:

- Optional enhancements, style preferences, naming alternatives, post-MVP ideas, additional providers, UI suggestions, or general hardening wish lists.
- A finding merely because a different design could also work.
- Pre-existing accepted MVP limitations unless v0.5.1 accidentally contradicts them.
- Low-value prose polish that cannot cause two conforming implementations to behave differently.
- Provider-documentation drift unless v0.5.1 changed the relevant provider contract.

Blocking threshold:

Report a blocker only if the specification still contains an internal contradiction, undefined required behavior, unbounded operation within a claimed bound, missing schema needed for a required artifact, impossible test oracle, or safety/control invariant that a literal implementation could violate.

For every blocker, provide:

- Stable finding ID.
- Priority P0 or P1 only.
- Exact affected sections/clauses.
- The conflicting requirements or concrete failure trace.
- Why a literal conforming implementation cannot safely choose one behavior.
- The smallest specification correction.
- The existing test row(s) that should be strengthened; do not increase the 108-test count unless logically unavoidable.

Required output format:

# Verdict

One exact token on its own line:

PASS_TO_IMPLEMENT

or

BLOCKED

# Closure checklist

A table with one row for each of the eight closure items and columns:

Item | PASS/FAIL | Evidence

# Blocking findings

If the verdict is PASS_TO_IMPLEMENT, write exactly:

None.

If BLOCKED, list only qualifying P0/P1 findings using the required fields above.

# Scope and count confirmation

Confirm whether:

- Settled MVP scope remained unchanged.
- MCP/Gemini/Claw deferral remained unchanged.
- The enumerated mandatory tests remain exactly 30 core, 46 Code Once, and 32 Council Once: 108 total.

Be decisive. If all eight closure items pass and no qualifying blocker exists, return PASS_TO_IMPLEMENT and stop. Do not manufacture another revision cycle.
```

### How to use the two results

- If both return `PASS_TO_IMPLEMENT`: freeze v0.5.1 and begin Slice 0.
- If one returns `PASS_TO_IMPLEMENT` and the other reports a blocker: verify only the claimed clause/failure trace. Do not reopen the entire specification.
- If both independently report the same blocker: make the smallest possible correction and perform a focused check of that correction only.
- Do not merge reviewer suggestions automatically merely because they are well written; the blocking threshold in the prompt remains controlling.
