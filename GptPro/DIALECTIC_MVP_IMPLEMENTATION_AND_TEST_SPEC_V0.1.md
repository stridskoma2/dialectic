# Dialectic MVP Implementation and Test Specification

**Version:** 0.1

**Status:** Draft for implementation

**Date:** 2026-08-27

**Working product name:** Dialectic

## 1. Purpose

Build the smallest useful local supervisor that proves two cross-model workflows:

1. **Code Once:** one Codex driver performs one small coding task; one or more configured review agents review the resulting immutable diff in parallel; their structured findings are returned to the same Codex driver session once; the driver incorporates or rebuts them; the run then stops.
2. **Council Once:** one prompt is sent blindly to two or more configured model agents; each sees the other anonymized positions in one cross-examination round; a moderator creates a candidate conclusion; the participants cast structured ballots; the run then stops with unanimous, rough-consensus, contested, no-quorum, or timeout status.

This MVP proves orchestration, session continuation, structured cross-provider communication, concurrency, Git isolation, deterministic consensus calculation, and durable evidence. It deliberately does **not** implement continuous loops yet.

The design must make a later backward transition possible:

- Code mode: `REPAIR -> REVIEW`
- Council mode: `BALLOT -> DISCUSSION_ROUND`

Those transitions are not enabled in version 0.1.

Normative terms **MUST**, **SHOULD**, and **MAY** have their usual requirements meanings.

## 2. MVP product decisions

### 2.1 Fixed decisions

- The supervisor is a local Python application.
- Codex is the only writable driver supported by the MVP.
- Codex, Claude Code, and Grok Build are supported as reviewer and council-agent targets when their native CLIs are installed and authenticated.
- A reviewer entry named `@driver` resolves to the driver's runtime, model, effort, and authentication context, but starts a fresh independent session.
- The target repository's original working tree, current branch, and `main` branch are never modified by the supervisor.
- The controller, not an AI agent, owns Git branch creation, worktree creation, snapshot commits, final commits, state transitions, timeouts, and consensus calculation.
- Every model-facing output used for a control decision must validate against a controller-owned schema.
- Mutable runtime state is stored outside the target repository.
- The MVP never pushes, opens a pull request, merges, deploys, or deletes a worktree automatically.
- Provider credentials and authentication files are never copied into run artifacts.

### 2.2 Explicitly out of scope

The following are deferred until after the MVP:

- A second review after the driver repairs the code.
- Repeated code-review-repair cycles.
- More than one council cross-examination round.
- Automatic plan creation, plan review, plan DAGs, or multiple implementation steps.
- Deterministic target-project test/lint/type gates owned by the supervisor.
- Review arbitration, semantic deduplication, adjudicator models, or severity voting.
- Any writable driver other than Codex.
- Repository-wide reviewer exploration; MVP reviewers receive a bounded diff packet only.
- GitHub, GitLab, pull-request, issue, or CI integration.
- TUI, web UI, editor extension, ACP server, or background daemon.
- Crash resumption. Partial artifacts remain inspectable, but an interrupted run is not resumed.
- Automatic model/provider fallback.
- Distributed workers or execution on multiple machines.
- Token accounting or monetary budget enforcement beyond recording provider-reported values when available.
- Submodules, Git LFS, sparse checkouts, and repositories without Git.

## 3. User-visible commands

The product name is **Dialectic**. The primary executable is `dial`; the
long-form executable `dialectic` MUST be installed as an equivalent alias. Both
entry points MUST invoke the same application, accept the same arguments, emit
the same output, use the same state, and return the same exit codes. The MVP
does not distribute `dl`, although users MAY define it as a private shell alias.

In version 0.1, `code` and `council` each execute exactly one bounded cycle. The
command names remain suitable when configurable looping is added later.

```bash
# One coding, review, and repair pass
dial code \
  --config dialectic.yaml \
  --repo /path/to/repository \
  --task-file task.md

# One bounded council pass
dial council \
  --config dialectic.yaml \
  --prompt-file question.md

# Inspect a completed or failed run
dial status <run-id>

# The long-form executable is exactly equivalent
dialectic status <run-id>
```

The commands MUST stream concise phase/status messages to the terminal. Raw model event streams MAY be written to artifacts but MUST NOT flood the default terminal output.

## 4. Configuration contract

Example:

```yaml
version: 1

driver:
  runtime: codex
  model: ${CODEX_DRIVER_MODEL}
  effort: high

reviewers:
  - id: driver-self-review
    target: "@driver"
    lens: general-correctness

  - id: claude-correctness
    runtime: claude-code
    model: ${CLAUDE_REVIEW_MODEL}
    lens: correctness-and-edge-cases

  - id: grok-adversarial
    runtime: grok-build
    model: ${GROK_REVIEW_MODEL}
    lens: adversarial-and-operational

council:
  participants:
    - id: codex
      runtime: codex
      model: ${CODEX_COUNCIL_MODEL}
    - id: claude
      runtime: claude-code
      model: ${CLAUDE_COUNCIL_MODEL}
    - id: grok
      runtime: grok-build
      model: ${GROK_COUNCIL_MODEL}

  moderator:
    runtime: codex
    model: ${CODEX_COUNCIL_MODEL}

  consensus:
    max_dissenters: 1
    blocking_objection_prevents_consensus: true

limits:
  max_reviewers: 5
  max_council_participants: 5
  max_diff_bytes: 262144
  agent_turn_seconds: 300
  code_run_seconds: 1200
  council_run_seconds: 1200
  code_review_cycles: 1
  council_discussion_rounds: 1
```

Requirements:

- Environment-variable references MUST be expanded without persisting their values into normalized configuration artifacts.
- Secrets MUST be supplied through the native CLI authentication mechanism or environment, never inline in the configuration.
- The MVP MUST accept exactly `code_review_cycles: 1` and `council_discussion_rounds: 1`. Other values MUST fail validation with a message explaining that iteration is a post-MVP feature.
- Code mode MUST accept between one and five reviewers.
- Council mode MUST accept between two and five participants.
- Reviewer and participant IDs MUST be unique.
- No implicit model replacement or fallback is permitted.

## 5. Technical architecture

### 5.1 Stack

- Python 3.12+
- `asyncio.create_subprocess_exec` for native CLI execution
- Pydantic v2 for configuration and message schemas
- Typer for the CLI
- Rich for concise progress and final summaries
- `platformdirs` for the default state directory
- Git CLI for repository and worktree operations
- Pytest and pytest-asyncio for tests
- JSON and JSONL artifacts; no database in the MVP

Shell commands MUST be spawned as executable-plus-argument arrays. The implementation MUST NOT interpolate user prompts, repository paths, model names, or credentials into `shell=True` command strings.

### 5.2 Components

| Component | Responsibility |
|---|---|
| `ConfigLoader` | Load YAML, expand permitted environment references, validate limits and targets |
| `AgentRegistry` | Resolve runtime names and `@driver` into concrete agent targets |
| `AgentAdapter` | Invoke one native agent turn, continue a known session, parse its envelope, and return normalized metadata |
| `CodexAdapter` | Codex driver, reviewer, moderator, and council participant execution |
| `ClaudeAdapter` | Claude reviewer and council participant/moderator execution |
| `GrokAdapter` | Grok reviewer and council participant/moderator execution |
| `GitWorkspace` | Validate the repository, create an isolated branch/worktree, snapshot changes, and compute bounded diffs |
| `RunStore` | Atomically persist state, events, prompts, normalized reports, and summaries outside the repository |
| `CodeOnceOrchestrator` | Execute the one coding/review/repair state machine |
| `CouncilOnceOrchestrator` | Execute the one bounded council state machine |
| `ConsensusCalculator` | Calculate final council status from validated ballots without model judgment |
| `Redactor` | Remove known secret values and sensitive environment fields before artifact persistence |

### 5.3 Agent target

The internal selection unit is an agent target, not merely a model:

```python
class AgentTarget(BaseModel):
    runtime: Literal["codex", "claude-code", "grok-build"]
    model: str
    effort: str | None = None
```

The response MUST distinguish the requested target from what the native CLI reports actually ran:

```python
class AgentResponse(BaseModel):
    runtime: str
    requested_model: str
    actual_model: str | None
    session_id: str | None
    text: str
    structured_output: dict | None
    exit_code: int
    duration_ms: int
    usage: dict | None
```

An unavailable `actual_model` is recorded as `null`; it MUST NOT be invented.

### 5.4 Adapter interface

```python
class AgentAdapter(Protocol):
    async def preflight(self, target: AgentTarget) -> PreflightResult: ...
    async def start(self, request: AgentRequest) -> AgentResponse: ...
    async def resume(self, session_id: str, request: AgentRequest) -> AgentResponse: ...
```

`AgentRequest` includes the role, prompt, optional output schema, timeout, working directory, and access mode. Adapters translate this neutral request into the installed CLI's flags and output envelope.

The adapter MUST return a stable native session ID when later continuation is required. Code mode MUST fail before review if the Codex driver completed without a resumable session ID.

## 6. Durable run artifacts

The default root is the platform-specific user state directory returned by `platformdirs`, under `dialectic/runs/<run-id>/`.

Code run:

```text
<run-id>/
  run.json
  events.jsonl
  input/
    task.md
    config.redacted.json
  git/
    workspace.json
    initial.diff
    final.diff
  driver/
    initial.request.json
    initial.response.json
    repair.request.json          # only when findings exist
    repair.response.json         # only when findings exist
  reviews/
    manifest.json
    reviewer-a.json
    reviewer-b.json
  feedback.json
  summary.json
  summary.md
```

Council run:

```text
<run-id>/
  run.json
  events.jsonl
  input/
    prompt.md
    config.redacted.json
  council/
    aliases.json
    opening/
    cross-examination/
    candidate.json
    ballots/
  summary.json
  summary.md
```

Requirements:

- `run.json` MUST be written by temporary-file-plus-atomic-rename.
- `events.jsonl` MUST be append-only.
- Model prompts and responses MUST be retained for audit, after redaction.
- The alias map MAY identify which configured model corresponds to Participant A/B/C in local artifacts; model-facing prompts MUST use aliases rather than provider brand names.
- Authentication tokens, complete process environments, provider auth files, and unredacted secret-bearing command lines MUST NOT be persisted.

## 7. Code Once functional specification

### 7.1 Input

The task is one Markdown document containing:

- A required coding request.
- Optional acceptance criteria.
- Optional constraints.

There is no implementation-plan parsing in the MVP.

### 7.2 State machine

```text
CREATED
  -> PREFLIGHT
  -> WORKTREE_READY
  -> DRIVER_RUNNING
  -> DRIVER_SNAPSHOT_READY
  -> REVIEWERS_RUNNING
  -> REVIEWS_READY
  -> DRIVER_REPAIRING       (only when at least one finding exists)
  -> FINALIZED
```

Any phase may transition to a terminal failure status. There is no transition from `FINALIZED` back to review.

### 7.3 Detailed flow

#### CODE-01: Preflight

The controller MUST:

1. Resolve the repository to an absolute path.
2. Confirm it is a Git working tree.
3. Confirm the original working tree has no staged, unstaged, or untracked non-ignored files.
4. Record the original branch and base SHA.
5. Validate the configuration and limits.
6. Preflight the Codex driver and every distinct reviewer target.
7. Fail without creating a worktree if any required target is unavailable or unauthenticated.

#### CODE-02: Isolated worktree

The controller MUST create:

- Branch: `dialectic/<run-id>`
- Worktree: `<state-root>/worktrees/<run-id>`

The new branch starts at the recorded base SHA. The original repository remains untouched.

#### CODE-03: Initial driver turn

The Codex driver receives:

- The exact task document.
- The isolated worktree path.
- A statement that this is one bounded implementation pass.
- An instruction to implement the request, run whatever narrow checks it considers appropriate, summarize its work, and stop.

The driver is allowed to modify only the isolated worktree. The controller records the returned native session ID.

#### CODE-04: Initial snapshot

After the driver exits successfully, the controller MUST:

1. Confirm the worktree contains changes relative to the base SHA.
2. Fail as `NO_CHANGES` unless a future configuration explicitly permits no-change tasks; the MVP exposes no such option.
3. Commit all worktree changes using controller-owned per-command Git identity.
4. Record the resulting `review_sha`.
5. Generate the unified diff `base_sha..review_sha`.
6. Fail as `DIFF_TOO_LARGE` when the UTF-8 encoded review packet would exceed `max_diff_bytes`.

No AI agent may perform the snapshot commit.

#### CODE-05: Blind parallel reviews

Every reviewer receives the same immutable review packet:

- Task and acceptance criteria.
- Base SHA.
- Review SHA.
- Complete bounded unified diff.
- Its configured review lens.
- The controller-owned review schema.

Reviewers do not receive:

- The Codex implementation transcript or self-assessment.
- Other reviewer identities or outputs.
- Authentication or cost information.
- Writable repository access.

The MVP uses **diff-only reviews**. Reviewer processes run with a neutral temporary working directory and do not receive the target worktree path.

All reviewers MUST start concurrently. `@driver` MUST create a fresh Codex session and MUST NOT resume the driver session.

#### CODE-06: Review schema

```python
class ReviewFinding(BaseModel):
    id: str
    severity: Literal["critical", "major", "minor", "nit"]
    category: str
    file: str | None
    line: int | None
    claim: str
    evidence: str
    suggested_fix: str | None

class ReviewReport(BaseModel):
    schema_version: Literal[1]
    base_sha: str
    head_sha: str
    verdict: Literal["pass", "changes_requested"]
    summary: str
    findings: list[ReviewFinding]
```

Validation rules:

- `base_sha` and `head_sha` MUST exactly match the packet.
- `pass` requires an empty findings list.
- `changes_requested` requires at least one finding.
- Finding IDs MUST be unique within a report.
- Every finding MUST contain a concrete claim and evidence; evidence may explain why the diff itself demonstrates the concern.
- Malformed or schema-invalid output fails that reviewer. No heuristic acceptance and no model-powered format-repair retry are included in the MVP.

#### CODE-07: Review barrier

All configured reviewers are required. If any reviewer:

- Times out,
- Exits unsuccessfully,
- Returns malformed output,
- Reports a mismatched SHA, or
- Cannot be authenticated,

the run becomes `REVIEW_FAILED`. The driver repair turn is not invoked.

#### CODE-08: Feedback packet

If every reviewer passes with no findings, the controller skips repair and finalizes as `COMPLETED_NO_FINDINGS`.

If at least one finding exists, the controller creates one deterministic feedback packet:

- Reviewers are labeled Reviewer A, Reviewer B, and so forth.
- Findings are not semantically merged or deduplicated.
- Reports are ordered by reviewer alias; findings retain report order.
- All severities, including nits, are included.
- The packet identifies the reviewed SHA and states that no re-review will occur in this MVP.

#### CODE-09: One repair turn

The controller MUST resume the original Codex driver session and provide the feedback packet. The driver is instructed to:

1. Inspect every finding.
2. Modify the isolated worktree where appropriate.
3. Return one disposition for every finding.
4. Stop after this repair pass.

```python
class FindingDisposition(BaseModel):
    finding_id: str
    outcome: Literal["fixed", "rejected_with_evidence", "not_fixed"]
    explanation: str

class DriverRepairReport(BaseModel):
    schema_version: Literal[1]
    summary: str
    dispositions: list[FindingDisposition]
```

Every supplied finding ID MUST appear exactly once. Unknown IDs are invalid.

#### CODE-10: Finalization

After a valid repair response, the controller MUST:

1. Commit any new worktree changes as a second controller-owned commit.
2. Permit no-change repair turns when every finding was rebutted with evidence or explicitly left not fixed.
3. Record the final SHA and final diff against the original base SHA.
4. Create machine-readable and Markdown summaries.
5. Stop without launching reviewers again.
6. Leave the worktree and branch available for human inspection.

Possible success statuses:

- `COMPLETED_NO_FINDINGS`
- `COMPLETED_AFTER_REPAIR`
- `COMPLETED_WITH_REBUTTALS`
- `COMPLETED_WITH_UNRESOLVED_FINDINGS`

The summary MUST clearly state that the repaired code has not been re-reviewed.

## 8. Council Once functional specification

### 8.1 Input

Council mode accepts one prompt document. It MAY contain background evidence, options, constraints, or desired output format. The controller does not interpret its subject matter.

### 8.2 State machine

```text
CREATED
  -> PREFLIGHT
  -> OPENING_POSITIONS_RUNNING
  -> OPENING_POSITIONS_READY
  -> CROSS_EXAMINATION_RUNNING
  -> REVISIONS_READY
  -> MODERATOR_RUNNING
  -> CANDIDATE_READY
  -> BALLOTS_RUNNING
  -> FINALIZED
```

There is no MVP transition from `BALLOTS_RUNNING` to another discussion round.

### 8.3 Detailed flow

#### COUNCIL-01: Preflight

The controller MUST validate and preflight all participants and the moderator before any model is invoked. All configured participants are required for quorum in the MVP.

#### COUNCIL-02: Blind opening positions

Participants start concurrently in fresh sessions. Every participant receives exactly the same user prompt and opening-position schema. No participant sees another participant's identity or response.

```python
class CouncilClaim(BaseModel):
    statement: str
    evidence: str | None
    assumption: str | None

class OpeningPosition(BaseModel):
    schema_version: Literal[1]
    conclusion: str
    claims: list[CouncilClaim]
    uncertainties: list[str]
    confidence: float  # 0.0 through 1.0; recorded but never vote-weighted
```

#### COUNCIL-03: One cross-examination round

The controller creates an anonymized position ledger containing Participant A/B/C positions in deterministic alias order.

Each participant's original session is resumed concurrently with:

- The original user prompt.
- The complete anonymized opening-position ledger.
- An instruction to identify the strongest opposing argument, identify unsupported assumptions, state what changed its view, and submit a revised conclusion.

```python
class CouncilRevision(BaseModel):
    schema_version: Literal[1]
    strongest_opposing_claim: str
    critique: str
    changed_mind: bool
    change_reason: str | None
    revised_conclusion: str
    remaining_objections: list[str]
```

#### COUNCIL-04: Candidate conclusion

The moderator starts a fresh non-voting session. It receives:

- The original prompt.
- All anonymized opening positions.
- All anonymized revisions.
- A requirement to create a concise candidate conclusion divided into independently ratifiable propositions.

```python
class CandidateProposition(BaseModel):
    id: str
    statement: str
    rationale: str
    supporting_participants: list[str]
    known_objections: list[str]

class CandidateConclusion(BaseModel):
    schema_version: Literal[1]
    answer: str
    propositions: list[CandidateProposition]
    unresolved_questions: list[str]
```

The moderator does not vote. If the same underlying model is also a participant, the moderator still uses a separate fresh session.

#### COUNCIL-05: Final ballots

Each participant resumes its own session and receives the candidate. Ballots run concurrently.

```python
class PropositionVote(BaseModel):
    proposition_id: str
    vote: Literal["accept", "reject", "abstain"]
    reason: str

class CouncilBallot(BaseModel):
    schema_version: Literal[1]
    proposition_votes: list[PropositionVote]
    overall_vote: Literal["accept", "reject", "abstain"]
    blocking_objection: bool
    blocking_objection_evidence: str | None
    minority_report: str | None
```

Every candidate proposition MUST receive exactly one vote from every participant.

#### COUNCIL-06: Deterministic outcome

Let `N` be the number of configured participants and `A` the number of overall `accept` votes.

- `UNANIMOUS`: `A == N`, no abstentions, and no blocking objection.
- `ROUGH_CONSENSUS`: `A >= N - max_dissenters` and no blocking objection.
- `CONTESTED`: valid ballots exist from all participants, but neither consensus rule passes.
- `NO_QUORUM`: any required participant failed in any participant phase or returned an invalid artifact.
- `TIMEOUT`: the overall council wall clock expired.

For the normal three-participant configuration with `max_dissenters: 1`, two accept votes can produce rough consensus only when nobody raises a blocking objection.

Confidence values MUST NOT affect the vote calculation. The supervisor MUST NOT claim that consensus proves factual correctness.

#### COUNCIL-07: Stop and report

The controller writes and displays:

- Outcome status.
- Candidate answer.
- Per-proposition vote matrix.
- Supporting rationale.
- Every minority report.
- Every blocking objection.
- Unresolved questions.
- Participant runtime/model identities for the user-facing audit, while preserving anonymization within model-to-model prompts.

The run then stops. A contested result is a valid completed product outcome, not an execution failure.

## 9. Timeouts, cancellation, and failures

- Every subprocess turn has an individual timeout.
- Each workflow has one overall wall-clock timeout.
- When a timeout expires, the supervisor MUST terminate the process, wait briefly, and force-kill it if necessary.
- Ctrl+C MUST initiate the same cancellation path and mark the run `CANCELLED`.
- Partial artifacts MUST remain available.
- The supervisor performs no automatic provider retry in the MVP.
- Failure messages MUST name the phase and configured target but MUST NOT expose credentials or complete environment contents.
- A failed code run never merges, pushes, or copies partial code into the original working tree.

## 10. Security and safety requirements

- Only the Codex driver receives the isolated writable worktree path.
- Reviewers receive a diff packet and neutral temporary CWD, not repository access.
- Council agents receive only the supplied prompt and controller-produced discussion artifacts.
- Model output is data. It MUST NOT be executed as a shell command by the supervisor.
- Target-repository files MUST NOT be treated as supervisor configuration unless explicitly named by the user as the configuration file.
- No repository hooks, MCP definitions, skills, or model instruction files are loaded by the supervisor itself.
- Git commits use argument arrays and a controller-local identity; global Git configuration is not modified.
- There is no automatic cleanup because preserving the branch/worktree is safer and more auditable for the MVP.
- The final terminal output MUST give the isolated worktree path and explain that the original repository is unchanged.

## 11. Test strategy

### 11.1 Test layers

1. **Unit tests:** pure schemas, configuration, state transitions, consensus rules, redaction, and prompt construction.
2. **Subprocess contract tests:** local fake executables emit recorded Codex/Claude/Grok-style envelopes and capture invocation arguments.
3. **Git integration tests:** temporary real Git repositories and real Git worktrees; model behavior remains scripted.
4. **Offline end-to-end tests:** complete workflows with scripted adapters; mandatory in CI.
5. **Live smoke tests:** opt-in, authenticated, cost-bearing tests against installed CLIs; excluded from normal CI.

All mandatory tests MUST run without network access and without provider credentials.

### 11.2 Scripted adapter

Tests use a `ScriptedAgentAdapter` with queued responses and optional callbacks that modify the isolated worktree. It records:

- Start versus resume operation.
- Session ID.
- Prompt hash and complete prompt in test memory.
- Target and role.
- Start and end timestamps.
- Requested schema.

This is the primary means of proving exact call counts and absence of accidental loops.

### 11.3 Core tests

| ID | Test | Expected result |
|---|---|---|
| CORE-001 | Valid configuration loads | Normalized targets and limits match input |
| CORE-002 | Inline secret-like config field supplied | Validation rejects it |
| CORE-003 | Unsupported review/discussion cycle count | Validation rejects values other than one |
| CORE-004 | Environment reference expansion | Runtime value is used but redacted config retains only reference/name |
| CORE-005 | Atomic run-state update interrupted before rename | Previous valid `run.json` remains readable |
| CORE-006 | Known secret appears in response fixture | Persisted artifact contains redaction marker, not secret |
| CORE-007 | Agent timeout | Child is terminated and phase records timeout |
| CORE-008 | Ctrl+C simulation | Children stop and run becomes `CANCELLED` |
| CORE-009 | Two delayed parallel invocations | Elapsed time approximates maximum delay, not sum of delays |
| CORE-010 | Requested and actual model differ | Both are recorded; no silent normalization |
| CORE-011 | Invoke matching command through `dial` and `dialectic` | Arguments, output, state effects, and exit codes are equivalent |

### 11.4 Code Once tests

| ID | Test | Expected result |
|---|---|---|
| CODE-001 | Happy path: two reviewers return findings | One initial driver start, two parallel fresh reviews, one driver resume, two commits at most, then stop |
| CODE-002 | All reviewers pass | No driver repair call; status `COMPLETED_NO_FINDINGS` |
| CODE-003 | `@driver` reviewer | Same target/model as driver but different fresh session ID |
| CODE-004 | Reviewer concurrency | All reviewer start timestamps precede first reviewer completion |
| CODE-005 | Immutable review packet | Every reviewer receives identical task, base SHA, review SHA, and diff hash |
| CODE-006 | Reviewer sees driver transcript sentinel | Sentinel is absent from reviewer prompt |
| CODE-007 | One reviewer fails | Status `REVIEW_FAILED`; driver resume count is zero |
| CODE-008 | One reviewer returns invalid JSON/schema | Status `REVIEW_FAILED`; invalid output retained for diagnosis |
| CODE-009 | Reviewer returns mismatched SHA | Report rejected and run fails closed |
| CODE-010 | Driver repair feedback | Every normalized finding ID and no provider identity appears in repair packet |
| CODE-011 | Driver omits a disposition | Repair report rejected; run becomes `REPAIR_FAILED` |
| CODE-012 | Driver fixes findings | New changes committed; status `COMPLETED_AFTER_REPAIR` |
| CODE-013 | Driver rebuts every finding without edits | No second commit required; status `COMPLETED_WITH_REBUTTALS` |
| CODE-014 | Driver leaves finding `not_fixed` | Status `COMPLETED_WITH_UNRESOLVED_FINDINGS` and summary highlights it |
| CODE-015 | Driver produces no initial changes | Status `NO_CHANGES`; no reviewers run |
| CODE-016 | Diff exceeds configured bound | Status `DIFF_TOO_LARGE`; no reviewers run |
| CODE-017 | Original repository is dirty | Preflight fails before worktree creation |
| CODE-018 | Full happy-path Git integration | Original branch/HEAD unchanged; isolated branch contains final code |
| CODE-019 | Exact call-count guard | No second review call exists after repair |
| CODE-020 | Failure after driver changes | Partial isolated worktree is preserved and reported |

### 11.5 Council Once tests

| ID | Test | Expected result |
|---|---|---|
| COUNCIL-001 | Three valid participants | Three blind starts, three resumes for cross-examination, one fresh moderator, three resumes for ballots |
| COUNCIL-002 | Blindness | No opening prompt contains another participant response or identity |
| COUNCIL-003 | Anonymized cross-examination | Participants see A/B/C aliases and no provider brands |
| COUNCIL-004 | Participant changes its mind | Revision records `changed_mind=true` and reason |
| COUNCIL-005 | Moderator isolation | Moderator uses a fresh session and produces no ballot |
| COUNCIL-006 | Candidate proposition coverage | Every final ballot covers every proposition exactly once |
| COUNCIL-007 | Three of three accept | Status `UNANIMOUS` |
| COUNCIL-008 | Two accept, one rejects, no blocker | Status `ROUGH_CONSENSUS` with minority report |
| COUNCIL-009 | Two accept, one raises blocker | Status `CONTESTED` |
| COUNCIL-010 | One participant abstains and threshold fails | Status `CONTESTED` |
| COUNCIL-011 | Participant fails during opening/cross-exam/ballot | Status `NO_QUORUM`; partial artifacts retained |
| COUNCIL-012 | Moderator fails | Execution failure `MODERATOR_FAILED`; no ballots run |
| COUNCIL-013 | Overall wall clock expires | Status `TIMEOUT`; active children terminated |
| COUNCIL-014 | Exact round-count guard | No participant receives a second cross-examination prompt after ballots |
| COUNCIL-015 | User-facing report | Contains answer, vote matrix, dissent, blockers, unresolved questions, and actual identities |

### 11.6 Adapter contract tests

Each native adapter MUST have fixture-based tests covering:

- Successful first turn parsing.
- Native session ID extraction.
- Resume invocation with that exact ID.
- Requested model forwarding.
- Non-zero exit.
- Missing/invalid envelope.
- Structured payload extraction.
- Timeout and process termination.
- Provider-reported actual model and usage when present.
- Prompts containing spaces, quotes, newlines, Unicode, `$()`, and backticks without shell execution.

Recorded envelopes MUST be version-labeled so a future CLI-output change produces an explicit fixture update rather than silent parsing drift.

### 11.7 Live smoke tests

Live tests are opt-in and MUST require an explicit environment flag.

```bash
pytest -q                         # mandatory offline suite
pytest -q -m integration          # mandatory local Git/subprocess suite
DIALECTIC_LIVE=1 pytest -q -m live
```

Live code smoke fixture:

- A temporary tiny Python package.
- Task: add one simple pure function and unit tests.
- Codex is the driver.
- At least `@driver` and one available external reviewer are configured.
- The test passes whether reviewers pass or request changes, but requires the correct corresponding terminal path and valid artifacts.
- A separate scripted-reviewer integration test, not a live model, guarantees exercise of the repair path.

Live council smoke fixture:

- A harmless architecture question with two or three available targets.
- The test requires valid positions, revisions, candidate, ballots, and one documented terminal outcome.
- It does not require the models to agree.

## 12. Definition of done

The MVP is complete only when all of the following are true:

### Code Once

- A real Codex driver can modify an isolated Git worktree for one simple task.
- Between one and five configured review targets can review the same immutable bounded diff concurrently.
- `@driver` produces a fresh independent review session.
- Valid findings are returned once to the original Codex driver session.
- The driver produces a disposition for every finding and may modify the code once.
- The controller commits the final isolated state and stops without re-reviewing.
- The original repository remains unchanged.
- The summary explicitly states whether repair occurred and that repaired code was not re-reviewed.

### Council Once

- Between two and five participants receive one identical blind prompt concurrently.
- They perform exactly one anonymized cross-examination round using their original sessions.
- A fresh non-voting moderator creates independently votable propositions.
- Participants cast complete structured ballots.
- The controller deterministically reports `UNANIMOUS`, `ROUGH_CONSENSUS`, `CONTESTED`, `NO_QUORUM`, or `TIMEOUT` and stops.
- Dissent and blocking objections remain visible in the result.

### Engineering

- All mandatory tests pass offline on Windows and Linux.
- No test or implementation path uses shell interpolation.
- Credentials are absent from persisted fixtures and run artifacts.
- Exact call-count tests prove neither workflow loops.
- A short README documents installation, native CLI prerequisites, configuration, commands, artifact locations, cost warning, and MVP limitations.

## 13. Agile implementation slices

### Slice 0: Skeleton and contracts

Deliver:

- Python package and CLI skeleton.
- Pydantic configuration and artifact schemas.
- `AgentAdapter` protocol and scripted adapter.
- Atomic `RunStore` and redaction.
- Core unit tests.

Exit criterion: configuration, schemas, state persistence, and scripted single turns work without Git or native agents.

### Slice 1: Offline Code Once vertical slice

Deliver:

- Git preflight and isolated worktree.
- Scripted Codex driver.
- Parallel scripted reviewers.
- Feedback construction.
- One scripted driver resume.
- Final commits and summary.

Exit criterion: CODE-001 through CODE-020 pass with no native AI CLI installed.

### Slice 2: Native agent adapters

Deliver:

- Codex adapter supporting start, fresh review, structured output, and resume.
- Claude adapter supporting fresh review/council turn, structured output, and resume.
- Grok adapter supporting fresh review/council turn, JSON extraction/validation, and resume.
- Version-labeled adapter fixtures and preflight diagnostics.
- Opt-in live code smoke test.

Exit criterion: a real simple Codex task can flow through available real reviewers and stop correctly.

### Slice 3: Offline Council Once vertical slice

Deliver:

- Blind opening fan-out.
- Alias/position ledger.
- One session-resumed cross-examination round.
- Fresh moderator candidate.
- Final ballots and deterministic consensus.
- Council report.

Exit criterion: COUNCIL-001 through COUNCIL-015 pass offline.

### Slice 4: MVP release hardening

Deliver:

- Live council smoke test.
- Windows and Linux verification.
- Cancellation/timeout polish.
- README and example configuration/task/prompt files.
- Packaging and version `0.1.0`.

Exit criterion: the complete definition of done is satisfied.

## 14. First post-MVP increments

These are intentionally ordered but not part of version 0.1:

1. Add one `REPAIR -> REVIEW` transition, invalidating previous approvals and reviewing the new SHA.
2. Generalize that transition to a configured maximum repair count.
3. Add one `BALLOT -> CROSS_EXAMINATION` transition using the prior objections.
4. Generalize council debate to a maximum round count and early-consensus stop.
5. Add deterministic target-project verification commands before model review.
6. Give reviewers read-only repository exploration in isolated sandboxes.
7. Add plan review and a machine-readable multi-step plan.
8. Permit any supported adapter to act as the writable driver.
9. Add SQLite-backed recovery and resumable interrupted runs.
10. Add a terminal UI after the headless state machines are stable.

This ordering preserves the MVP's two proven vertical flows and adds looping as a state-machine extension rather than a rewrite.
