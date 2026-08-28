# Review Feedback: Dialectic MVP Implementation and Test Specification v0.1

**Reviewed document:** `DIALECTIC_MVP_IMPLEMENTATION_AND_TEST_SPEC_V0.1.md`  
**Review date:** 2026-08-27  
**Review type:** Read-only implementation, testability, control-contract, and safety review  
**Recommendation:** Revise before implementation

## 1. Overall assessment

The specification defines two coherent bounded workflows, keeps state-machine
authority in the controller, separates model judgment from deterministic control
decisions, and gives offline testing first-class status. The one-pass boundaries
are especially clear.

Five issues should be resolved before implementation begins because they can
cause ambiguous control decisions, false consensus, unintended code execution,
or incomplete cancellation. Five further issues should be resolved while
finalizing the contracts for Slice 0.

Priority meanings in this review:

- **P1:** Resolve before implementation. The current contract can produce an
  incorrect or unsafe result even if implemented literally.
- **P2:** Resolve before the corresponding vertical slice. Independent correct
  implementations could otherwise behave incompatibly.

## 2. P1 findings

### P1-01: Finding IDs are not globally unambiguous

**Affected clauses:** CODE-06 lines 395-422, CODE-08 lines 440-446, CODE-09
lines 457-469, and CODE-010 in the test table.

`ReviewFinding.id` is required to be unique only within a single report. The
combined repair packet then asks the driver to identify dispositions solely by
`finding_id`. Two valid reviewers can both emit an ID such as `F1`, leaving no
unambiguous way to satisfy the requirement that every supplied ID appear exactly
once.

Example:

```text
Reviewer A: finding id F1
Reviewer B: finding id F1
Driver disposition: finding_id F1
```

The disposition cannot identify which finding it addresses, and requiring two
`F1` dispositions would violate an ordinary uniqueness check.

**Required contract change:** The controller should assign a unique normalized
key when constructing the feedback packet. Preserve the reviewer-local ID for
audit, but use only the normalized key for repair dispositions.

One possible shape is:

```python
class NormalizedFinding(BaseModel):
    key: str                  # for example reviewer-a:F1
    reviewer_alias: str
    source_finding_id: str
    finding: ReviewFinding

class FindingDisposition(BaseModel):
    finding_key: str
    outcome: Literal["fixed", "rejected_with_evidence", "not_fixed"]
    explanation: str
```

**Tests to add:**

- Two reviewers return the same local finding ID.
- The feedback packet contains two distinct normalized keys.
- The repair report must cover each normalized key exactly once.
- A disposition that uses a local ID rather than a normalized key is rejected.

### P1-02: Consensus configuration can produce false consensus

**Affected clauses:** configuration lines 139-142 and COUNCIL-06 lines 606-618.

`max_dissenters` has no stated bound relative to the participant count. With
three participants and `max_dissenters: 3`, the rough-consensus threshold becomes
`A >= 0`; consequently, three reject votes and no blocking-objection flag would
qualify as rough consensus.

The outcome formula also always requires no blocking objection, regardless of
the value of `blocking_objection_prevents_consensus`. The configuration therefore
exposes a boolean that the normative calculation ignores. Finally, a unanimous
ballot also satisfies the rough-consensus inequality, but classification
precedence is not stated.

**Required contract change:**

- Validate `0 <= max_dissenters < N` after resolving the participant list.
- Either honor `blocking_objection_prevents_consensus` in the formula or require
  it to be `true` in version 0.1 and reject `false`.
- Make outcome evaluation ordered and mutually exclusive: overall wall-clock
  timeout, missing/invalid required participant artifact, unanimous, rough
  consensus, then contested.
- State whether at least one accept vote is independently required. The proposed
  bound above makes that true.

**Tests to add:**

- Negative `max_dissenters` is rejected.
- `max_dissenters >= N` is rejected.
- All participants reject and the result is never consensus.
- The blocking-objection option is tested for every accepted MVP value.
- A unanimous ballot is classified only as `UNANIMOUS`.

### P1-03: A neutral working directory does not enforce diff-only access

**Affected clauses:** CODE-05 lines 371-391 and security requirements lines
646-653.

The spec treats a neutral temporary CWD and omission of the target worktree path
as though they remove repository access. They do not form an access boundary. A
native CLI can potentially enumerate readable filesystem paths or load user-level
configuration, MCP servers, hooks, skills, memories, plugins, or tools. Council
agents have the same issue.

This matters because the spec makes stronger claims than prompt-level blindness:
reviewers are said to receive only a bounded packet, and only the driver is said
to receive the writable worktree path.

**Required product decision:** Choose and specify one of these contracts:

1. **Capability-enforced isolation:** Run non-driver agents in an OS sandbox or
   equivalent environment that exposes only the packet directory and required
   authentication channel. Disable runtime customizations and tools not required
   for packet analysis.
2. **Trusted-local-process boundary:** State explicitly that the supervisor does
   not provide repository context but does not prevent a configured native CLI
   from discovering locally readable data. Recast the current security language
   as prompt isolation rather than filesystem isolation.

If capability enforcement remains the claim, the adapter requirements must name
the relevant safe-mode, configuration-isolation, MCP, tool, filesystem, and
network policy for each runtime. CWD alone is insufficient.

**Tests to add:**

- Place a sentinel outside the packet directory and instruct a scripted or
  sandbox-probe reviewer to read it; access must fail under the enforced model.
- Verify that user and project MCP/customization fixtures are not loaded.
- Verify that reviewers and council agents receive no writable path.

### P1-04: Controller-owned Git operations may still execute repository hooks

**Affected clauses:** CODE-02, CODE-04 line 364, CODE-10 line 475, and security
requirements lines 652-654.

Using executable-plus-argument arrays prevents shell interpolation, but it does
not stop Git from invoking repository-configured hooks during controller-owned
operations. A snapshot or final commit can therefore execute target-controlled
code with the supervisor process's filesystem and environment access. This
contradicts the statement that the supervisor does not load repository hooks.

**Required contract change:** Every controller Git command that could invoke
hooks should explicitly disable them with a controller-owned empty hooks path,
without changing global or target-repository configuration. The Git wrapper
should also avoid external diff/text-conversion execution when constructing the
review packet.

For example, the policy can require the equivalent of a per-invocation:

```text
git -c core.hooksPath=<controller-owned-empty-directory> ...
```

**Tests to add:**

- A target repository contains pre-commit, commit-msg, post-commit, and
  post-checkout sentinel hooks.
- Worktree creation and both controller commits complete without any sentinel
  being written.
- Global Git configuration remains unchanged.

### P1-05: Timeout and Ctrl+C semantics do not require process-tree termination

**Affected clauses:** timeout requirements lines 635-644 and CORE-007,
CORE-008, and COUNCIL-013.

Terminating or killing the direct `asyncio` subprocess is not sufficient when a
native agent CLI has spawned shell commands or other descendants. Those children
can survive the CLI process, continue consuming resources, and continue changing
the isolated worktree after the run is marked timed out or cancelled.

**Required contract change:** Define process-tree ownership and cleanup for both
supported operating-system families:

- Start each agent turn in a dedicated process group on Unix-like systems.
- Use a Windows Job Object or an equivalent tree-owned mechanism on Windows.
- On timeout or cancellation, send graceful termination to the tree, wait for a
  bounded grace period, force-kill the tree, and await complete reaping before
  recording the terminal state.
- Specify what happens if cleanup itself fails and preserve that fact in the run
  artifacts.

**Tests to add:**

- A fake CLI spawns a grandchild that would write a delayed sentinel.
- Agent timeout prevents the sentinel and leaves no descendant running.
- Ctrl+C follows the same path.
- Overall workflow timeout cancels every concurrent participant process tree.

## 3. P2 findings

### P2-01: Codex packet-only roles need an explicit non-repository invocation

**Affected clauses:** AgentAdapter lines 225-236, CODE-05 lines 389-391, and all
Codex council phases.

Current Codex non-interactive mode normally requires a Git repository. A neutral
temporary directory used for an `@driver` review or Codex council role will fail
unless the adapter either initializes a disposable Git repository or passes the
documented skip-repository-check option.

**Required contract change:** Specify the same deterministic policy for every
Codex non-driver role. A disposable empty Git repository provides a conservative
default; using `--skip-git-repo-check` is also viable if the chosen isolation
contract makes that explicit.

**Tests to add:** A Codex adapter contract fixture starts and resumes successfully
from the exact neutral CWD shape used by reviewers and council agents.

### P2-02: Candidate proposition and ballot identity rules are incomplete

**Affected clauses:** COUNCIL-04 lines 559-583 and COUNCIL-05 lines 585-604.

`CandidateConclusion` permits an empty proposition list and duplicate proposition
IDs. Empty propositions can still be followed by overall accept votes and a
reported unanimous result. Duplicate IDs make the exactly-once ballot requirement
ambiguous.

The ballot schema also does not state whether blocking-objection evidence is
required when the flag is true, or whether an overall accept vote may coexist
with rejection of every proposition.

**Required contract change:**

- Require at least one candidate proposition.
- Require unique, non-empty proposition IDs.
- Validate participant references against the configured alias set.
- Require every ballot's proposition-ID set to equal the candidate set exactly,
  with no duplicates or unknown IDs.
- Require non-empty blocking-objection evidence when the flag is true and define
  whether evidence is allowed when it is false.
- Define the permitted relationship between proposition votes and `overall_vote`,
  or state explicitly that overall voting is independent.

**Tests to add:** Empty candidates, duplicate proposition IDs, duplicate ballot
votes, unknown IDs, omitted IDs, and inconsistent blocking evidence.

### P2-03: A normal unified diff is not complete for binary changes

**Affected clauses:** CODE-04 lines 358-369, CODE-05 lines 371-389, and
CODE-016.

A normal unified diff replaces binary contents with a marker. A reviewer can
therefore approve a packet without seeing the contents of every committed change,
despite the specification calling the packet complete. The same ambiguity applies
to repositories using unsupported submodules, Git LFS, custom filters, or sparse
checkout: listing them as out of scope does not say whether preflight rejects
them.

**Required contract change:** For the smallest fail-closed MVP, reject binary
changes and reject unsupported repository features during preflight or snapshot.
Alternatively, require a binary patch format and count the complete encoded patch
against `max_diff_bytes`.

Also clarify whether `max_diff_bytes` measures only the diff or the complete
review packet, because CODE-04 uses both descriptions.

**Tests to add:** Binary addition/modification, unsupported submodule/LFS/sparse
repository, and a task whose text makes the whole packet exceed the bound while
the diff alone does not.

### P2-04: Code success-status precedence is not exhaustive

**Affected clauses:** CODE-10 lines 471-489 and CODE-012 through CODE-014.

The listed statuses cover simple examples but do not determine mixed cases:

- Some findings are fixed and others are rebutted.
- Some findings are fixed and at least one is `not_fixed`.
- The driver claims `fixed` but produces no worktree change.
- The driver changes files while rejecting every finding.

**Required contract change:** Define an ordered truth table based on validated
dispositions and actual Git changes. One reasonable precedence is:

1. Any `not_fixed` disposition: `COMPLETED_WITH_UNRESOLVED_FINDINGS`.
2. Otherwise, any post-review Git change: `COMPLETED_AFTER_REPAIR`.
3. Otherwise, every finding is `rejected_with_evidence`:
   `COMPLETED_WITH_REBUTTALS`.
4. A `fixed` disposition with no corresponding final-state change is invalid
   unless the schema explicitly supports an already-fixed explanation.

Mixed rebuttals should remain visible in the summary even when the terminal
status is `COMPLETED_AFTER_REPAIR`.

**Tests to add:** A table-driven test for every meaningful disposition/change
combination.

### P2-05: CLI exit codes and status lookup behavior are undefined

**Affected clauses:** user-visible commands lines 64-94, failure semantics, and
COUNCIL-07 line 633.

The spec requires `dial` and `dialectic` to return identical exit codes but never
defines the semantic exit-code contract. Automation cannot reliably distinguish
a valid contested council result from invalid input, no quorum, timeout,
cancellation, a missing run ID, or corrupted state.

**Required contract change:** Publish a stable table covering at least:

- All successful Code Once statuses.
- `UNANIMOUS`, `ROUGH_CONSENSUS`, and valid `CONTESTED` outcomes.
- Configuration/input/preflight failures.
- Provider, review, repair, and moderator failures.
- `NO_QUORUM`, `TIMEOUT`, and `CANCELLED`.
- `dial status` for unknown, incomplete, and corrupt run records.

Because the spec declares contested to be a valid product outcome, it should use
the same success exit class as unanimous and rough-consensus outcomes. Ctrl+C may
use the conventional platform-appropriate interrupt code, but the exact choice
must be tested.

## 4. Additional contract clarifications

These are not classified as implementation blockers, but settling them in Slice
0 will prevent avoidable format and compatibility drift.

### 4.1 Version the controller-owned artifact schemas

The model-facing schemas are shown, but `run.json`, `workspace.json`,
`manifest.json`, `feedback.json`, `summary.json`, and `events.jsonl` do not have
minimum required fields or schema versions. `dial status` depends directly on
those formats. Define their versioned Pydantic models, terminal-state invariants,
and allowed partial-state shapes before implementing RunStore.

The artifact tree should also name where raw invalid provider output is retained,
because CODE-008 requires it to remain diagnosable.

### 4.2 Fully specify configurable-limit validation

The absolute product limits are stated, but the meaning and validation of
`max_reviewers`, `max_council_participants`, byte limits, and timeout values are
not. Require positive bounded values and state whether configured maxima may be
lower than the product maxima. Validate actual reviewer and participant counts
against both applicable limits.

### 4.3 Define run-ID syntax and safe lookup

The run ID is used in a Git ref, filesystem paths, and a user-supplied `status`
argument, but its format is unspecified. Define a controller-generated,
collision-resistant, Git-ref-safe and path-component-safe grammar. `dial status`
must reject values outside that grammar rather than joining arbitrary user input
to the state root.

### 4.4 Qualify the original-repository guarantee

Creating a linked worktree and `dialectic/<run-id>` branch necessarily updates
the repository's shared Git metadata. Replace broad statements that the original
repository is "untouched" with the exact guarantee already implied elsewhere:
the original working-tree files, checked-out branch, original HEAD, index, and
`main` ref remain unchanged, while a new branch and linked-worktree metadata are
created intentionally.

### 4.5 State the limits of redaction and protect artifact permissions

Known-value redaction cannot guarantee removal of secrets embedded in arbitrary
task text, source diffs, or model prose. Document that limitation, create the
state directory with user-only permissions where the platform permits, and avoid
claiming that arbitrary sensitive content is always removed. Tests should cover
permissions and the exact known-secret classes the Redactor promises to remove.

## 5. Provider feasibility check

The core native-session premise is currently feasible:

- OpenAI documents Codex non-interactive JSONL, schema-constrained final output,
  session IDs, and `exec resume`. It also documents the Git-repository requirement
  relevant to P2-01: <https://learn.chatgpt.com/docs/non-interactive-mode>
- Anthropic documents Claude Code print mode, JSON-schema output, session IDs,
  resume, and safe/customization controls:
  <https://code.claude.com/docs/en/cli-usage>
- xAI documents Grok Build headless JSON output and session resume:
  <https://docs.x.ai/build/cli/headless-scripting>

Local inspection during this review found Codex CLI `0.150.0-alpha.8` and Claude
Code `2.1.177`. The installed Codex resume command accepts `--output-schema`, so
the repair, cross-examination, and ballot turns are not blocked on that feature.
Grok Build was not installed locally; its feasibility assessment above is based
on the current official documentation rather than a local smoke test.

Adapter fixtures should record the CLI version they represent, as the spec
already requires, and live smoke tests should verify the exact installed version
before claiming support.

## 6. Recommended revision gate

Before starting Slice 0:

1. Resolve P1-01 through P1-05 as explicit normative text.
2. Resolve the consensus and terminal-status truth tables.
3. Choose whether reviewer/council isolation is capability-enforced or a trusted
   local-process limitation, then make every security claim match that choice.
4. Define the versioned controller artifact schemas and exit-code table.
5. Add the proposed negative and collision cases to the test inventory.

After those changes, the document will be sufficiently deterministic to support
independent implementation without leaving safety-critical behavior to adapter
or orchestrator interpretation.
