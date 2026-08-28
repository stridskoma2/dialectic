# Review Feedback: Dialectic MVP Implementation and Test Specification v0.5

**Reviewed document:** `DIALECTIC_MVP_IMPLEMENTATION_AND_TEST_SPEC_V0.5.md`  
**Review date:** 2026-08-28  
**Reviewer:** Sol  
**Review type:** Read-only implementation-gate, testability, control-contract, and safety review  
**Recommendation:** Make one narrow v0.5.1 contract/schema patch, then begin implementation

## 1. Overall assessment

Version 0.5 substantively closes all eight findings from my v0.4 review. The two
workflows are stable; I see no reason for another product or architecture redesign.
In particular, v0.5 now gives the service lifecycle authority, bounds named-file
acquisition, makes Windows stream handoff bounded under a stalled event loop,
records every model turn, bounds scratch traversal, rejects hard-linked product
files, pins changed-leaf enumeration, separates generic capability evidence from
per-run construction evidence, and scopes the native-process `AgentResponse`
honestly.

The declared mandatory test count is also correct: **30 core + 46 Code Once + 32
Council Once = 108**.

I would not freeze the document as the literal implementation baseline quite yet.
Two clauses still cannot be implemented simultaneously, and one unavoidable
bootstrap failure needs an explicit exception. These are local ordering/schema
corrections, not a request for v0.6 or any workflow expansion. Once the three
findings below are patched, I am comfortable proceeding directly to Slice 0.

Priority meanings:

- **P1:** Resolve in the specification before implementation; current normative
  clauses require incompatible outputs or ordering.
- **P2:** Resolve in the Slice 0 contract; the failure is unavoidable and needs one
  deterministic public behavior.

## 2. Disposition of the v0.4 findings

| Prior finding | v0.5 disposition | Evidence |
|---|---|---|
| P1-01: service boundary versus pre-load `CREATED` | **Closed** | `create_run`, bounded CLI acquisition, `fail_invalid_input`, service-side parsing, and service-owned terminal persistence are explicit at lines 291-299 and 897. CORE-026 exercises the boundary. |
| P1-02: unbounded Windows callback bridge | **Closed** | Lines 465-471 specify cumulative reader-side accounting, a byte-bounded handoff, at most one wake-up plus one terminal notification, and one-shot overflow. CORE-028 stalls the event loop and tests the bound. |
| P1-03: missing reviewer/council request-response evidence | **Closed in layout; see P1-02 below for two schema omissions** | The `turns/<role>/<alias>/<phase>` layout, bindings, hashes, and field/value-equivalence rules at lines 523-599 and 750-827 cover all driver, reviewer, participant, and moderator calls. |
| P1-04: scratch bytes do not bound traversal | **Closed** | Entry, depth, and cleanup-time limits are configured at lines 178-186; lines 419-421 define iterative bound-plus-one traversal and bounded no-follow cleanup. |
| P1-05: multi-link product files outside the integrity proof | **Closed** | The native cross-boundary hard-link probe is required at line 457; candidate files must be single-link regular files before staging at line 1001; CODE-045 and LIVE-CODE-002 cover both construction and enforcement. |
| P2-01: changed-leaf set undefined | **Closed** | Lines 992-1001 pin the three NUL-delimited commands, raw-byte union/deduplication, bound-plus-one stop, ordering, decoding, rename/deletion behavior, and pre-stage file checks. |
| P2-02: generic versus run-specific attestation undefined | **Mechanism closed; see P1-01 below for phase placement** | Lines 400-405 and schemas at 707-749 split the expensive generic behavior attestation from the exact concrete run binding and define invalidation. |
| P2-03: future transport claim conflicts with process-specific response | **Closed** | Line 337 now limits transport neutrality to workflow semantics and requires a new artifact-schema version for a future direct API adapter. |

## 3. Remaining findings

### P1-01: The concrete capability binding is ordered before its dynamic worktree exists

**Affected clauses:** service/capability lifecycle at lines 291-299 and 400-405;
CODE-01 at lines 943-956; CODE-02 at lines 962-971.

The generic-versus-concrete split is the right design, but the state-machine order
does not currently provide a legal point at which to create the concrete record:

1. `CapabilityBindingArtifact` must record the exact concrete profile and stable
   dynamic filesystem identities for the run, including the isolated worktree.
2. CODE-01 step 11 preflights every target “as specified in section 5.4.”
3. CODE-01 step 12 says any preflight failure occurs without creating a branch or
   worktree.
4. The isolated worktree is not created until CODE-02.

The generic native probe can use fixture-owned sentinels and can run in CODE-01.
The run-specific binding cannot record or verify the identity of a directory that
does not exist. Hashing the planned spelling would not satisfy
`DynamicFilesystemIdentity`, and creating the worktree early would contradict the
current CODE-01 no-worktree-on-failure guarantee.

**Required contract change:** Split target readiness into two named gates:

1. **Generic target preflight in CODE-01:** executable/version/authentication,
   managed-policy inspection, and cached-or-fresh generic capability attestation.
   Failure here still creates no branch/worktree.
2. **Concrete binding after CODE-02 and before DRIVER_INITIAL:** open and verify the
   actual worktree/common-Git/original/state/auth/temp identities; instantiate and
   byte-compare the complete concrete profile; persist
   `CapabilityBindingArtifact`; then permit the first native turn.

Define the terminal behavior if the second gate fails. The simplest rule is
`FAILED/PREFLIGHT_FAILED`, retain and report the already-created branch/worktree,
and launch no model. Adjust CODE-01 step 12 so its no-worktree guarantee applies
only to the generic gate.

**Tests to strengthen:**

- Assert that no concrete binding is emitted before its referenced filesystem
  objects exist and have stable identities.
- Make concrete substitution omit, reorder, add, or weaken one dynamic rule after
  worktree creation; the model must not launch.
- Make concrete binding fail after CODE-02; verify the specified failure kind and
  branch/worktree retention/reporting behavior.
- Verify that every turn references the exact binding that authorized its dynamic
  paths.

### P1-02: Closed artifact schemas cannot carry two pieces of evidence required by the prose

**Affected clauses:** target preflight at lines 388-398; `AgentResponse` at lines
342-371; bounded output at lines 463-471; artifact schemas/bindings at lines
605-827.

There are two direct schema contradictions.

First, preflight step 3 requires the resolved launcher path, spawned root
executable, launch kind, and CLI version to be recorded in `run.json`. The closed
`RunRecord` schema at lines 626-640 contains none of those fields, and line 796
forbids undeclared fields. Later successful `AgentResponse` records contain the
values, but that does not satisfy the explicit `run.json` requirement and cannot
audit a target that passes preflight but never receives a turn.

Second, line 469 says structured truncation metadata lives in the enclosing
versioned turn artifact. The only bound response schema is `AgentResponse`, and it
has stream byte/hash fields but no stdout/stderr truncation flags, overflow stream,
termination reason, or persisted-length metadata. The marker in the text file is a
useful diagnostic, but it is not the promised structured evidence.

**Required contract change:**

- Either add a closed target/launch-plan collection to `RunRecord`, or preferably
  add a separately versioned `audit/targets/<target-id>.json` artifact and change
  preflight step 3 plus the binding table to point to it. The latter keeps mutable
  run state separate from immutable target evidence.
- Add explicit stream-result fields to the versioned turn response/attempt
  artifact, at minimum `stdout_truncated`, `stderr_truncated`, the triggering
  stream or `null`, captured and persisted byte counts, and a closed termination
  reason. If an attempted invocation can fail before an `AgentResponse` exists,
  define a versioned `TurnAttemptArtifact` union rather than inventing native
  response values.
- State which hash/count covers the accepted bounded prefix, which covers the
  redacted persisted file, and how the discarded credential guard is represented.

**Tests to strengthen:**

- Validate and reload target evidence for a run that fails after preflight but
  before its first model turn.
- Make CORE-027 assert the structured truncation/termination fields as well as the
  marker and hashes.
- Walk failed, timed-out, cancelled, overflowed, and fail-fast-cancelled turns and
  prove that each attempted invocation has one schema-valid audit outcome without
  fabricated provider data.

### P2-01: Secure run-directory bootstrap failure cannot persist the record currently promised

**Affected clauses:** `create_run` at line 293; private-directory requirement at
line 836; `PREFLIGHT_FAILED` mapping at line 877; CORE-017.

The ordinary lifecycle is now coherent: once the service has persisted `CREATED`,
every later input failure can be written through the service. There is one
unavoidable exception. If the state root is unavailable, the run directory cannot
be created, or its private mode/DACL cannot be established, the service has nowhere
safe to atomically persist either `CREATED` or terminal `PREFLIGHT_FAILED`.

The specification currently promises both a persisted `CREATED` record after every
valid mode and a persisted preflight classification for private-directory setup.
No implementation can guarantee durable state on storage that it has just proved
unusable or unsafe.

**Required contract change:** Define a pre-record bootstrap failure. A safe rule is:

- `create_run` first creates and verifies a private temporary run directory, writes
  the explicit-null `CREATED` record, and atomically publishes the directory.
- If that bootstrap cannot complete, return exit 2 with one bounded
  controller-formatted diagnostic, launch no model, leave no insecure partial run
  directory, and explicitly create no durable run record.
- Once `create_run` returns a handle, every subsequent failure must retain the
  existing persisted behavior.

CORE-017 should cover unwritable state root, failed POSIX mode verification, and
failed Windows DACL verification, including cleanup of any unpublished temporary
directory.

## 4. Provider and protocol validation

The Codex portions remain aligned with current primary documentation:

- Permission profiles are beta and do not compose with `sandbox_mode` or
  `--sandbox`; the older sandbox wins if both are loaded.
- Named permission profiles support explicit filesystem rules, narrower carve-outs,
  and native Windows as well as Linux/WSL.
- Non-interactive mode supports stdin prompts, `--ignore-user-config`,
  `--ignore-rules`, JSONL, output schemas, and last-message output.

References checked:

- [OpenAI Codex permission profiles](https://learn.chatgpt.com/docs/permissions)
- [OpenAI Codex non-interactive mode](https://learn.chatgpt.com/docs/non-interactive-mode)

The installed local CLI is still `codex-cli 0.150.0-alpha.8`; its current
`codex exec --help` exposes repeatable `-c`, stdin prompt `-`,
`--ignore-user-config`, `--ignore-rules`, `--output-schema`, and
`--output-last-message`. This is supporting evidence only. The specification is
correct to require versioned fixtures and pinned-native release evidence because
the permission-profile feature is explicitly beta.

I did not run live, authenticated provider calls during this specification review.
Those remain opt-in implementation/release evidence under section 11.7.

## 5. Confirmed strengths to retain

- The workflows remain deliberately bounded: one implementation/review/repair pass
  and one opening/cross-examination/moderation/ballot pass.
- CLI, service, orchestrator, Git, and future-ingress authority are now separated
  cleanly.
- The named-file acquisition rule is hard-bounded before config-derived limits are
  known and rejects blocking/special inputs.
- The Windows Job Object and stream-reader contracts are explicit enough to drive
  adversarial platform tests.
- Scratch and candidate changes are bounded before Git validation, with honest
  best-effort qualifications where no filesystem quota exists.
- The exact raw changed-leaf union closes the untracked-directory and duplicate-path
  ambiguity.
- Hard-link enforcement is tested both as offline construction and pinned-native
  behavior, while the validator independently rejects multiply linked candidates.
- Model-facing control decisions remain strict, versioned, bounded, and fail-closed.
- MCP remains a post-alpha/beta northbound ingress rather than a participant tool or
  hidden second orchestrator.

## 6. Final implementation gate

This is very close. I recommend a small v0.5.1 edit that does only the following:

1. Place generic target preflight before worktree creation and concrete capability
   binding after it, with explicit failure behavior.
2. Add schema-bound target launch evidence and structured stream
   truncation/termination evidence.
3. Define the no-record exception for failure to establish secure run storage.

After those edits, proceed to implementation. I would not request another
independent architecture review unless that patch changes the workflow, authority
model, or threat boundary.

## 7. Finding index

| ID | Priority | Title |
|---|---|---|
| P1-01 | P1 | Concrete capability binding is ordered before the worktree exists |
| P1-02 | P1 | Required launch/truncation evidence has no fields in the closed schemas |
| P2-01 | P2 | Secure run-storage bootstrap needs an explicit no-record failure |
