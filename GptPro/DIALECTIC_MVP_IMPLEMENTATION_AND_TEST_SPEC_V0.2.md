# Dialectic MVP Implementation and Test Specification

**Specification revision:** 0.2

**Target product version:** 0.1.0

**Status:** Implementation candidate after Codex and Claude review reconciliation

**Date:** 2026-08-27

**Working product name:** Dialectic

**Alternative names retained:** VerityLoop, OmniPilot

## 1. Purpose

Build the smallest useful local supervisor that proves two cross-model workflows:

1. **Code Once:** one Codex driver performs one small coding task; one or more configured review agents review the resulting immutable diff in parallel; their structured findings are returned to the same Codex driver session once; the driver incorporates, rebuts, or leaves them unresolved; the run then stops.
2. **Council Once:** one prompt is sent blindly to two or more configured model agents; each sees the anonymized positions in one cross-examination round; a moderator creates a candidate conclusion; the participants cast structured ballots; the controller calculates unanimous, rough-consensus, or contested outcome; the run then stops.

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
- No tracked content in the target repository's checked-out working tree, its index, its checked-out branch, its original `HEAD`, any pre-existing branch, or `main` is modified by the supervisor. A code run intentionally adds a `dialectic/<run-id>` branch, linked-worktree metadata, commits, and Git objects to the repository's shared Git database.
- The controller, not an AI agent, owns Git branch creation, worktree creation, snapshot commits, final commits, state transitions, timeouts, and consensus calculation.
- Every model-facing output used for a control decision must validate against a controller-owned schema.
- Mutable runtime state is stored outside the target repository.
- The MVP never pushes, opens a pull request, merges, deploys, or deletes a worktree automatically.
- Provider credentials and authentication files are never copied into run artifacts.
- Code mode takes an exclusive advisory lock per target Git common directory. Concurrent code runs against one repository fail before worktree creation; council runs do not require this lock.
- Windows 11 and Linux are release platforms. macOS may work but is not part of the v0.1.0 definition of done.
- Reviewer and council packet isolation is a context-minimization contract on a trusted local machine, not an OS confidentiality boundary. Configured native CLIs execute with the user's operating-system identity and may retain access granted by user or managed configuration.

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
- Capability-enforced OS/container isolation for reviewer and council processes.
- GitHub, GitLab, pull-request, issue, or CI integration.
- TUI, web UI, editor extension, ACP server, or background daemon.
- Crash resumption. Partial artifacts remain inspectable, but an interrupted run is not resumed.
- Automatic model/provider fallback.
- Automatic retry after rate limits, quota exhaustion, transient provider errors, or malformed output.
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

limits:
  max_reviewers: 5
  max_council_participants: 5
  max_input_bytes: 65536
  max_diff_bytes: 262144
  max_packet_bytes: 393216
  max_lens_chars: 4096
  preflight_seconds: 30
  agent_turn_seconds: 300
  code_run_seconds: 1200
  council_run_seconds: 1200
  graceful_kill_seconds: 5
  code_review_cycles: 1
  council_discussion_rounds: 1
```

Requirements:

- Model, effort, runtime, lens, and ID fields are non-secret. Environment references in those fields MUST be expanded and the resolved values MUST be retained in the normalized audit configuration.
- The strict configuration schema exposes no credential, token, API-key, or auth-file field. Unknown fields are rejected. Credentials MUST be supplied through the native CLI's existing authentication mechanism or inherited environment, never through Dialectic configuration.
- The MVP MUST accept exactly `code_review_cycles: 1` and `council_discussion_rounds: 1`. Other values MUST fail validation with a message explaining that iteration is a post-MVP feature.
- Code mode MUST accept between one and five reviewers and MUST reject a count above the configured `max_reviewers`.
- Council mode MUST accept between two and five participants and MUST reject a count above the configured `max_council_participants`.
- Reviewer and participant IDs MUST be unique within their respective lists and match `[a-z][a-z0-9-]{0,31}`.
- After environment expansion, every configured model selector MUST be between 1 and 128 characters and match `[A-Za-z0-9][A-Za-z0-9._:/@+\[\]-]{0,127}`. An adapter MAY narrow that allowlist for a specific native CLI but MUST NOT accept shell metacharacters or quoting/control characters.
- A reviewer with `target: "@driver"` MUST NOT also specify `runtime`, `model`, or `effort`. A concrete reviewer MUST specify `runtime` and `model` and MUST NOT specify `target`.
- `lens` is model-facing free text between 1 and `max_lens_chars` characters; it is not a file path or enum.
- Every byte and timeout limit MUST be positive and within the hard ceilings in the following table.
- Every outbound `AgentRequest` prompt, including council ledgers, candidates, and ballots, MUST fit `max_packet_bytes` before that phase launches any participant. Overflow fails as `PACKET_TOO_LARGE` without launching a partial phase.
- After participant resolution, consensus MUST satisfy `0 <= max_dissenters < N`, where `N` is the participant count. Blocking objections always prevent consensus in the MVP; there is no configuration switch for this behavior.
- No implicit model replacement or fallback is permitted. A documented provider alias may resolve to its canonical model and is not a fallback. Each adapter records the requested selector and canonical resolution when available; a known non-equivalent `actual_model` fails as `MODEL_MISMATCH`. An unavailable actual-model field remains `null` and is not invented.

Hard validation ceilings prevent accidental unbounded local runs:

| Field | Allowed value |
|---|---:|
| `max_reviewers` | 1..5 |
| `max_council_participants` | 2..5 |
| `max_input_bytes` | 1..262144 |
| `max_diff_bytes` | 1..1048576 |
| `max_packet_bytes` | 1..1572864 |
| `max_lens_chars` | 1..8192 |
| `preflight_seconds` | 1..300 |
| `agent_turn_seconds` | 1..3600 |
| `code_run_seconds`, `council_run_seconds` | 1..14400 |
| `graceful_kill_seconds` | 1..30 |

## 5. Technical architecture

### 5.1 Stack

- Python 3.12+
- `asyncio.create_subprocess_exec` for native CLI execution
- Pydantic v2 for configuration and message schemas
- Typer for the CLI
- Rich for concise progress and final summaries
- `platformdirs` for the default state directory
- `filelock` for a cross-platform per-repository advisory lock
- `pywin32` on Windows for Job Object process-tree ownership
- Git CLI for repository and worktree operations
- Pytest and pytest-asyncio for tests
- JSON and JSONL artifacts; no database in the MVP

Commands MUST be spawned as executable-plus-argument arrays with `shell=False`. The implementation MUST NOT interpolate user prompts, repository paths, model names, or credentials into command strings. Prompts, diffs, schemas, and other unbounded model input MUST NOT be placed in argv.

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
| `ProcessSupervisor` | Own each native CLI process tree, enforce timeouts, and reap descendants |
| `RepositoryLock` | Prevent concurrent code runs against the same Git common directory |
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

class ReviewerSpec(BaseModel):
    id: str
    target: Literal["@driver"] | None = None
    runtime: Literal["codex", "claude-code", "grok-build"] | None = None
    model: str | None = None
    effort: str | None = None
    lens: str

class ParticipantSpec(BaseModel):
    id: str
    runtime: Literal["codex", "claude-code", "grok-build"]
    model: str
    effort: str | None = None

class ModeratorSpec(BaseModel):
    runtime: Literal["codex", "claude-code", "grok-build"]
    model: str
    effort: str | None = None
```

All models use `extra="forbid"`. `ReviewerSpec` enforces the `@driver` versus concrete-target exclusive-or rule from section 4. Runtime-specific effort values are validated by the corresponding adapter; an unsupported value fails preflight rather than being silently dropped.

The response MUST distinguish the requested target from what the native CLI reports actually ran:

```python
class AgentResponse(BaseModel):
    runtime: str
    requested_model: str
    resolved_requested_model: str | None
    actual_model: str | None
    session_id: str | None
    resolved_executable: str
    cli_version: str
    prompt_transport: Literal["stdin", "acp-stdio"]
    effective_safety_flags: list[str]
    text: str
    structured_output: dict | None
    exit_code: int
    duration_ms: int
    usage: dict | None
```

An unavailable `actual_model` or canonical alias resolution is recorded as `null`; it MUST NOT be invented. Adapter fixtures define accepted alias-to-canonical equivalence for their labeled CLI version.

### 5.4 Adapter interface

```python
class AgentAdapter(Protocol):
    async def preflight(self, target: AgentTarget) -> PreflightResult: ...
    async def start(self, request: AgentRequest) -> AgentResponse: ...
    async def resume(self, session_id: str, request: AgentRequest) -> AgentResponse: ...
```

`AgentRequest` includes the role, prompt, optional output schema, timeout, working directory, and access mode. Adapters translate this neutral request into the installed CLI's flags and output envelope.

#### 5.4.1 Executable and capability preflight

For every distinct runtime target, preflight MUST:

1. Resolve the executable with `shutil.which()` and fail with the configured target ID when resolution returns `None`.
2. Convert the result to an absolute path, including `.exe`, `.cmd`, or other extension on Windows.
3. Record the absolute path and reported CLI version in `run.json`.
4. Verify authentication without printing or persisting credential material.
5. Verify that the installed version supports every flag and protocol the adapter requires.

All later invocations MUST use the recorded absolute executable. Prompts are never argv elements, which also prevents user content from being reinterpreted by a Windows `.cmd` shim through `cmd.exe`.

#### 5.4.2 Prompt transport

- Codex requests MUST pass `-` as the prompt argument and stream the complete UTF-8 prompt over stdin for both `exec` and `exec resume`.
- Claude requests MUST use print mode and stream the complete packet over stdin. The argv prompt, if required by the installed CLI, is a fixed controller-owned sentence directing Claude to process stdin.
- Grok requests MUST use ACP stdio with update checks, memory, web search, planning, subagents, and built-in tools disabled through supported flags. The adapter speaks JSON-RPC over stdin/stdout, creates a session with no filesystem, terminal, or MCP client capabilities, and sends prompt text in `session/prompt`. A council participant's ACP process and session remain alive through its three turns.
- No argv element may exceed 4096 UTF-8 bytes. Runtime adapters MUST apply the configured model-selector rule and MUST validate any native session ID placed in argv against `[A-Za-z0-9][A-Za-z0-9._:-]{0,255}`. Nonconforming values are rejected rather than escaped heuristically.
- A model process's working directory MUST be supplied through the subprocess `cwd` parameter, not a CLI path argument. Schema and output-file arguments, when required, MUST be controller-generated relative names containing no `..` component. Packet-only roles place them in the private neutral CWD. Driver roles place them in a collision-checked, controller-reserved subdirectory of the isolated worktree and remove that directory before inspecting code changes. This keeps task text and user-selected filesystem paths out of a Windows `.cmd` command line.

The controller writes JSON Schemas to temporary files only when a CLI requires a schema path. It copies any audit-required content into the private run artifacts, then deletes the temporary turn directory in a `finally` block before Git inspection. A task MUST NOT use the reserved directory for product output.

#### 5.4.3 Driver and packet-only runtime policy

The Codex driver MUST run with the isolated worktree as its process CWD, `workspace-write` sandboxing, a non-interactive never-ask approval policy, and command network access disabled. It MUST NOT use `danger-full-access`, `--yolo`, or an approval path that can expand the writable boundary. The adapter MUST ignore user config and exec-policy rules, preserve repository instruction discovery, and fail preflight when the installed CLI or managed policy cannot provide the required effective profile. The required Codex profile protects the linked worktree's `.git` pointer and resolved Git directory as read-only to model-generated commands, leaving Git metadata changes to the controller.

Packet-only Codex roles MUST use a neutral temporary CWD, `--skip-git-repo-check`, `--sandbox read-only`, `--ignore-user-config`, and `--ignore-rules`. Packet-only Claude roles MUST use `--safe-mode`, an empty built-in tool set, and no MCP configuration. Grok ACP roles MUST disable update checks, memory, web search, planning, subagents, and tools and advertise no filesystem, terminal, or MCP capabilities. Equivalent current flags may replace these only through an adapter fixture and specification update.

These controls reduce accidental discovery and customization, but Dialectic does not claim to confine the native process itself. Managed policy, provider behavior, inherited environment, or a compromised executable may still access data available to the user's OS account. The target repository path MUST never appear in a packet-only prompt, argv, environment override, or packet artifact.

#### 5.4.4 Output extraction

Each adapter first parses its versioned native envelope. When the runtime exposes a schema-validated structured field, that field is the only accepted payload. Otherwise the adapter applies this deterministic rule to the final assistant text:

1. Parse the complete text as JSON.
2. If that fails, accept exactly one fenced block tagged `json` and parse its complete contents.
3. Zero or multiple `json` fences, trailing non-whitespace inside the fence, or any schema failure fails the turn.

No other substring search, prose stripping, model-powered repair, or retry is permitted. Raw stdout and stderr are retained in the agent's artifact directory after known-secret redaction.

#### 5.4.5 Session continuation

The adapter MUST return a stable native session ID whenever later continuation is required. Code mode fails before review if the Codex driver completes without one. Council mode fails as `NO_QUORUM` immediately after opening positions if any participant lacks one; the moderator never starts. Resume calls MUST use the exact recorded session and operation type `resume`, except Grok ACP, which continues the exact live ACP session with another `session/prompt` request.

## 6. Durable run artifacts

The default root is the platform-specific user state directory returned by `platformdirs`, under `dialectic/runs/<run-id>/`.

The controller generates run IDs in UTC with this grammar:

```text
YYYYMMDDTHHMMSSZ-<10 lowercase RFC 4648 base32 characters>
example: 20260827T142355Z-k7m2q4v5wx
regex:   ^[0-9]{8}T[0-9]{6}Z-[a-z2-7]{10}$
```

The random suffix contains 50 bits. The value is safe as one path component and one Git-ref component. `dial status` MUST validate the complete argument against the grammar before joining it to a path; it MUST NOT normalize or accept traversal-like alternatives.

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
    initial.stdout.txt
    initial.stderr.txt
    repair.request.json          # only when findings exist
    repair.response.json         # only when findings exist
    repair.stdout.txt            # only when findings exist
    repair.stderr.txt            # only when findings exist
  reviews/
    manifest.json
    reviewer-a.json
    reviewer-b.json
    raw/
      reviewer-a.stdout.txt
      reviewer-a.stderr.txt
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
      participant-a.json
      participant-b.json
    cross-examination/
      participant-a.json
      participant-b.json
    candidate.json
    ballots/
      participant-a.json
      participant-b.json
    raw/
  summary.json
  summary.md
```

### 6.1 Versioned artifact contracts

Every controller-owned JSON object MUST include `artifact_schema_version: 1` and `tool_version`. Minimum models are:

```python
class RunRecord(BaseModel):
    artifact_schema_version: Literal[1]
    tool_version: str
    run_id: str
    mode: Literal["code", "council"]
    status: Literal["CREATED", "RUNNING", "FINALIZED", "FAILED", "TIMED_OUT", "CANCELLED"]
    phase: str
    code_outcome: str | None
    consensus_outcome: str | None
    failure_kind: str | None
    failure_detail: str | None
    created_at: datetime
    updated_at: datetime
    started_model_work_at: datetime | None
    completed_at: datetime | None

class EventRecord(BaseModel):
    artifact_schema_version: Literal[1]
    tool_version: str
    sequence: int
    timestamp: datetime
    run_id: str
    phase: str
    event_type: str
    payload: dict

class WorkspaceRecord(BaseModel):
    artifact_schema_version: Literal[1]
    tool_version: str
    repo_common_dir: str
    original_worktree: str
    original_branch: str | None
    base_sha: str
    dialectic_branch: str | None
    dialectic_worktree: str | None
    review_sha: str | None
    final_sha: str | None

class ReviewManifest(BaseModel):
    artifact_schema_version: Literal[1]
    tool_version: str
    base_sha: str
    review_sha: str
    diff_sha256: str
    reviewer_aliases: list[str]
    reports: list[str]

class FeedbackArtifact(BaseModel):
    artifact_schema_version: Literal[1]
    tool_version: str
    review_sha: str
    findings: list[dict]

class SummaryRecord(BaseModel):
    artifact_schema_version: Literal[1]
    tool_version: str
    run_id: str
    mode: Literal["code", "council"]
    status: str
    outcome: str | None
    failure_kind: str | None
    unresolved_items: list[str]
    artifact_paths: dict[str, str]

class AliasMapArtifact(BaseModel):
    artifact_schema_version: Literal[1]
    tool_version: str
    aliases: dict[str, AgentTarget]
```

Models MAY add fields, but existing fields cannot change meaning within artifact schema version 1. Partial runs may omit only fields typed as nullable. All timestamps are timezone-aware UTC and serialize as RFC 3339; event sequence numbers start at 1 and increase without gaps. Summary artifact paths are relative to the run directory. `events.jsonl` uses one `EventRecord` per line. `aliases.json` is the only council artifact that maps participant aliases to actual runtimes/models; model-facing artifacts never contain that mapping.

### 6.2 Persistence and redaction requirements

- `run.json` MUST be written by temporary-file-plus-atomic-rename.
- `events.jsonl` MUST be append-only.
- Model prompts and responses MUST be retained for audit, after redaction.
- The alias map MUST identify which configured target corresponds to Participant A/B/C in local artifacts; model-facing prompts MUST use aliases rather than provider brand names.
- Authentication tokens, complete process environments, provider auth files, and unredacted secret-bearing command lines MUST NOT be persisted.
- On POSIX, run directories and temporary packet directories MUST be mode `0700` and files `0600`. On Windows, the controller MUST apply a DACL limited to the current user and required system principals. Failure to establish a private artifact directory fails preflight.
- Known-value redaction applies only to non-empty values of at least eight characters from the adapter-maintained credential environment-name allowlist. It does not redact ordinary non-secret model, effort, runtime, ID, or lens values.
- Redaction is defense in depth, not a promise to discover arbitrary secrets embedded in source, task text, or model prose. Users must treat the run directory as sensitive.

### 6.3 Run status, product outcome, and exit codes

Execution status and product result are separate:

```python
RunStatus = Literal["CREATED", "RUNNING", "FINALIZED", "FAILED", "TIMED_OUT", "CANCELLED"]

CodeOutcome = Literal[
    "COMPLETED_NO_FINDINGS",
    "COMPLETED_AFTER_REPAIR",
    "COMPLETED_WITH_REBUTTALS",
    "COMPLETED_WITH_UNRESOLVED_FINDINGS",
]

ConsensusOutcome = Literal["UNANIMOUS", "ROUGH_CONSENSUS", "CONTESTED"]

FailureKind = Literal[
    "INVALID_INPUT", "PREFLIGHT_FAILED", "REPOSITORY_BUSY",
    "UNSUPPORTED_REPOSITORY", "DRIVER_FAILED", "NO_CHANGES",
    "DIFF_TOO_LARGE", "PACKET_TOO_LARGE", "MODEL_MISMATCH",
    "REVIEW_FAILED", "REPAIR_FAILED", "NO_QUORUM",
    "MODERATOR_FAILED", "PROCESS_CLEANUP_FAILED", "STATE_CORRUPT",
    "INTERNAL_ERROR",
]
```

- `FINALIZED` requires exactly one mode-appropriate product outcome and no failure kind.
- `FAILED` requires one failure kind and no product outcome.
- `TIMED_OUT` and `CANCELLED` have no product outcome. Their phase identifies where work stopped.
- `CREATED` and `RUNNING` are non-terminal.
- A valid `CONTESTED` council decision and a code result containing unresolved findings are completed product outcomes, not orchestration failures.

Process exit codes are stable:

| Command result | Exit code |
|---|---:|
| `FINALIZED`, regardless of product outcome | 0 |
| Invalid input/config, preflight failure, unsupported repository, repository busy, or unknown run ID | 2 |
| Agent/workflow/internal/state/process-cleanup failure | 3 |
| `TIMED_OUT` | 4 |
| `CANCELLED` by Ctrl+C | 130 |

`dial status <run-id>` returns 0 when a syntactically valid, readable record is displayed, including non-terminal or failed runs; 2 for an invalid or unknown ID; and 3 for a corrupt or unreadable record. The `dial` and `dialectic` entry points use this identical mapping.

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
2. Resolve and record the Git common directory.
3. Create one controller-owned empty hooks directory under the private run directory.
4. Acquire the repository advisory lock or set status `FAILED` with failure kind `REPOSITORY_BUSY`, naming the holding run ID when available.
5. Confirm it is a non-bare Git working tree whose original working tree has no staged, unstaged, or untracked non-ignored files.
6. Reject sparse checkout, any mode-`160000` entry from `git ls-files -s`, and any tracked path for which `git check-attr filter` returns a value other than `unspecified`. This rejects submodules, Git LFS, and custom clean/smudge-filtered content as `UNSUPPORTED_REPOSITORY` without requiring Git LFS to be installed.
7. Validate the input byte bound, configuration, counts, limits, IDs, and consensus settings.
8. Record the original branch, original `HEAD`, base SHA, `main` SHA when present, and byte-for-byte `git status --porcelain=v1 -z` result.
9. Resolve and preflight the Codex driver and every distinct reviewer target as specified in section 5.4.
10. Fail without creating a branch or worktree if any check fails.

Preflight has its own timeout and does not consume the workflow wall clock.

The lock path is `<state-root>/locks/<sha256(canonical-git-common-dir)>.lock`; a user-private metadata sidecar contains the holding run ID. The controller holds the OS-backed lock for the complete code run and releases it on every normal or exceptional exit. A stale sidecar without an acquired OS lock does not block a new run and is replaced.

#### CODE-02: Isolated worktree

The controller MUST create:

- Branch: `dialectic/<run-id>`
- Worktree: `<state-root>/worktrees/<run-id>`

The new branch starts at the recorded base SHA. Every Git operation that can invoke hooks, including worktree creation, checkout, and commit, MUST use the equivalent of `git -c core.hooksPath=<controller-empty-hooks-dir> ...` without modifying repository, global, or system configuration.

The operation intentionally changes the repository's shared Git metadata by adding the Dialectic branch, linked-worktree entry, commits, and objects. It MUST NOT change the original checked-out files, index, current branch, original `HEAD`, any pre-existing branch, or `main`.

#### CODE-03: Initial driver turn

The Codex driver receives:

- The exact task document.
- The isolated worktree path.
- A statement that this is one bounded implementation pass.
- An instruction to implement the request, run whatever narrow checks it considers appropriate, summarize its work, and stop.
- A warning that the linked worktree is a fresh checkout and does not contain ignored local artifacts such as `.venv`, `node_modules`, build caches, or `.env`; repairing that environment is not part of the coding task.

The driver is allowed to modify only the isolated worktree. The controller records the returned native session ID.

#### CODE-04: Initial snapshot

After the driver exits successfully, the controller MUST:

1. Confirm the worktree contains changes relative to the base SHA.
2. Fail as `NO_CHANGES` when the worktree is unchanged relative to the base SHA.
3. Enumerate every staged, unstaged, and untracked path that would be included, using NUL-delimited Git output. Before staging, query the effective `filter` attribute for that complete path set with the NUL-safe equivalent of `git check-attr -z --stdin filter`; reject any value other than `unspecified` as `UNSUPPORTED_REPOSITORY`. This repeats the preflight protection for paths newly created by the driver and MUST occur before a clean or process filter can run.
4. Commit all worktree changes using controller-owned per-command Git identity.
5. Record the resulting `review_sha`.
6. Inspect `git diff --numstat base_sha..review_sha` under the same no-external-diff and no-textconv policy used below; reject any binary addition or modification as `UNSUPPORTED_REPOSITORY`.
7. Generate exactly one common diff with behavior equivalent to:

   ```text
   git -c core.hooksPath=<empty> -c core.autocrlf=false \
       -c core.quotePath=true -c diff.external= -c diff.algorithm=histogram \
       -c diff.indentHeuristic=false -c color.ui=false \
       --no-pager diff --no-color --no-ext-diff --no-textconv \
       --no-renames --full-index --src-prefix=a/ --dst-prefix=b/ --unified=3 \
       <base_sha>..<review_sha> --
   ```

8. Run diff inspection and generation with `LC_ALL=C`, then store the exact UTF-8 diff and its SHA-256 hash.
9. Set status `FAILED` with failure kind `DIFF_TOO_LARGE` when the diff alone exceeds `max_diff_bytes`.
10. Construct each reviewer packet and set status `FAILED` with failure kind `PACKET_TOO_LARGE` before launching any reviewer if any encoded packet exceeds `max_packet_bytes`.

No AI agent may perform the snapshot commit. Because the snapshot precedes size and binary validation, a rejected run may retain that snapshot commit on its isolated branch; the final report MUST state this.

#### CODE-05: Blind parallel reviews

Every reviewer receives the same immutable packet core:

- Task and acceptance criteria.
- Base SHA.
- Review SHA.
- Complete bounded unified diff.
- The controller-owned review schema.

The controller then adds only that reviewer's configured lens. It records both the common-core hash and complete per-reviewer packet hash.

Reviewers do not receive:

- The Codex implementation transcript or self-assessment.
- Other reviewer identities or outputs.
- Authentication or cost information.
- The target repository or worktree path.
- Writable repository access supplied by Dialectic.

The MVP uses **diff-only reviews**. Reviewer processes run with a neutral private temporary directory and the packet-only runtime policy from section 5.4. This is prompt/context isolation, not an OS sandbox guarantee.

All reviewers MUST start concurrently. `@driver` MUST create a fresh Codex session and MUST NOT resume the driver session.

#### CODE-06: Review schema

```python
class ReviewFinding(BaseModel):
    id: str  # reviewer-local, 1..64 characters
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
- Finding IDs MUST be non-empty and no longer than 64 characters; a non-null line number MUST be at least 1.
- Every finding MUST contain a concrete claim and evidence; evidence may explain why the diff itself demonstrates the concern.
- A finding may name a file outside the supplied diff when it explains a cross-cutting impact; Dialectic passes it through and does not attempt repository-backed path validation.
- Malformed or schema-invalid output fails that reviewer. Only section 5.4.4's deterministic extraction is allowed; no model-powered format-repair retry is included.

#### CODE-07: Review barrier

All configured reviewers are required. If any reviewer:

- Times out,
- Exits unsuccessfully,
- Returns malformed output,
- Reports a mismatched SHA, or
- Reports a known model different from the requested model, or
- Cannot be authenticated,

the run becomes `FAILED` with failure kind `REVIEW_FAILED` or `MODEL_MISMATCH`. The driver repair turn is not invoked. Rate limits, quota exhaustion, and transient provider failures are failures in the MVP; completed turns are retained but not reused by an automatic retry.

#### CODE-08: Feedback packet

If every reviewer passes with no findings, the controller skips repair and sets run status `FINALIZED` with code outcome `COMPLETED_NO_FINDINGS`.

If at least one finding exists, the controller creates one deterministic feedback packet:

- Reviewers are labeled Reviewer A, Reviewer B, and so forth.
- Findings are not semantically merged or deduplicated.
- Reports are ordered by reviewer alias; findings retain report order.
- All severities, including nits, are included.
- The packet identifies the reviewed SHA and states that no re-review will occur in this MVP.

Reviewer-local finding IDs are audit metadata only. The controller assigns a globally unique deterministic key based on alias and report order:

```python
class NormalizedFinding(BaseModel):
    finding_key: str          # for example reviewer-a/001
    reviewer_alias: str
    source_finding_id: str
    finding: ReviewFinding
```

Duplicate local IDs across reports therefore remain unambiguous. The repair prompt refers only to `finding_key`; it does not expose provider or model identity.

#### CODE-09: One repair turn

The controller MUST resume the original Codex driver session and provide the feedback packet. The driver is instructed to:

1. Inspect every finding.
2. Modify the isolated worktree where appropriate.
3. Return one disposition for every finding.
4. Stop after this repair pass.

```python
class FindingDisposition(BaseModel):
    finding_key: str
    outcome: Literal["fixed", "rejected_with_evidence", "not_fixed"]
    explanation: str

class DriverRepairReport(BaseModel):
    schema_version: Literal[1]
    summary: str
    dispositions: list[FindingDisposition]
```

Every supplied normalized finding key MUST appear exactly once. A reviewer-local ID, duplicate key, omitted key, or unknown key is invalid.

#### CODE-10: Finalization

After a valid repair response, the controller MUST:

1. Compare the worktree with `review_sha` after the repair turn.
2. Set status `FAILED` with failure kind `REPAIR_FAILED` if any disposition says `fixed` but the aggregate repair diff is empty. The MVP does not attempt to prove a semantic one-to-one relationship between individual findings and hunks.
3. Commit any new changes as a second controller-owned commit with hooks disabled.
4. Permit an empty repair diff only when every disposition is `rejected_with_evidence` or `not_fixed`.
5. Record the final SHA and final diff against the original base SHA.
6. Select exactly one `CodeOutcome` with this ordered rule:

   1. Any `not_fixed` disposition: `COMPLETED_WITH_UNRESOLVED_FINDINGS`.
   2. Otherwise, any post-review Git change: `COMPLETED_AFTER_REPAIR`.
   3. Otherwise, the non-empty disposition set is entirely `rejected_with_evidence`: `COMPLETED_WITH_REBUTTALS`.
   4. An empty finding set, handled before repair: `COMPLETED_NO_FINDINGS`.

7. Preserve rebuttals in the summary even when another disposition gives the run a higher-precedence outcome.
8. Set run status `FINALIZED`, create machine-readable and Markdown summaries, and stop without launching reviewers again.
9. Leave the worktree and branch available for human inspection.

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

The controller MUST validate counts, unique IDs, consensus bounds, input byte limits, and all participant/moderator targets before any model is invoked. It then resolves executables, capabilities, versions, and authentication as specified in section 5.4. All configured participants are required for quorum in the MVP.

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
    confidence: float = Field(ge=0.0, le=1.0)  # recorded, never vote-weighted
```

If any participant completes without a stable resumable session ID, the controller fails immediately with failure kind `NO_QUORUM`; no cross-examination or moderator turn starts.

#### COUNCIL-03: One cross-examination round

The controller creates an anonymized position ledger containing every position, including the receiving participant's own position, in deterministic alias order. Each participant is told its own alias so it can distinguish self-critique from critique of peers; no participant receives the alias-to-runtime/model map.

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

Validation requires one or more propositions, unique non-empty proposition IDs matching `[a-z][a-z0-9-]{0,31}`, non-empty statements and rationales, and `supporting_participants` containing only known participant aliases without duplicates. Unknown aliases invalidate the moderator artifact and fail the run with `MODERATOR_FAILED`.

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

For each ballot:

- The proposition-ID set MUST equal the candidate-ID set exactly; duplicates, omissions, and unknown IDs are invalid.
- `blocking_objection=true` requires non-empty evidence and requires `overall_vote="reject"`.
- `blocking_objection=false` requires `blocking_objection_evidence=null`.
- The overall vote is a checked summary of proposition votes: any blocking objection or proposition rejection requires `reject`; otherwise any proposition abstention requires `abstain`; otherwise all propositions accept and the overall vote must be `accept`.
- Invalid ballots fail the participant phase as `NO_QUORUM`; Dialectic does not repair or reinterpret them.

#### COUNCIL-06: Deterministic outcome

Let `N` be the number of configured participants, `A` the number of overall `accept` votes, and `B` indicate that any ballot contains a blocking objection. Configuration has already established `0 <= max_dissenters < N`.

Only after every required ballot validates, the controller evaluates these mutually exclusive rules in order:

1. `UNANIMOUS` when `A == N` and `B` is false.
2. `ROUGH_CONSENSUS` when `A >= 1`, `A >= N - max_dissenters`, and `B` is false.
3. `CONTESTED` otherwise.

Participant failure or invalid output produces run status `FAILED` with failure kind `NO_QUORUM`; moderator failure produces `MODERATOR_FAILED`; overall expiry produces `TIMED_OUT`. None of these is a `ConsensusOutcome`.

For the normal three-participant configuration with `max_dissenters: 1`, two accept votes can produce rough consensus only when nobody raises a blocking objection.

Per-proposition votes determine and validate each participant's overall vote as specified in COUNCIL-05. Consensus arithmetic then uses only those validated overall votes and blocking-objection flags. Confidence values MUST NOT affect the vote calculation. The supervisor MUST NOT claim that consensus proves factual correctness.

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

The controller sets status `FINALIZED` with exactly one `ConsensusOutcome` and stops. A contested result is a valid completed product outcome, not an execution failure.

## 9. Timeouts, cancellation, and failures

- Preflight has its own timeout. The workflow wall clock begins immediately before the first model invocation, not when the run record is created.
- Every agent turn has an individual timeout, and each workflow has one overall wall-clock timeout.
- Every native invocation MUST be owned as one process tree. On POSIX, the controller starts a new session/process group and signals the group. On Windows, it assigns the process to a Job Object configured to terminate all members when closed.
- On timeout or cancellation, the controller requests graceful termination of the entire tree, waits at most `graceful_kill_seconds`, force-terminates the tree, and awaits complete reaping before recording a terminal state.
- If tree cleanup cannot be confirmed, the run becomes `FAILED` with `PROCESS_CLEANUP_FAILED`; the trigger and surviving-process diagnostics are retained without credentials.
- Ctrl+C initiates the same tree-cleanup path and normally records `CANCELLED`.
- Terminal precedence for competing events is: process-cleanup failure, explicit cancellation, overall timeout, individual-turn timeout/phase failure.
- Partial artifacts MUST remain available.
- The supervisor performs no automatic provider retry in the MVP.
- Rate limits, quota exhaustion, transient transport errors, and malformed provider output therefore fail the affected required turn. The report MUST make clear that this is deliberate fail-closed MVP behavior.
- Failure messages MUST name the phase and configured target but MUST NOT expose credentials or complete environment contents.
- A failed code run never merges, pushes, or copies partial code into the original working tree.

## 10. Security and safety requirements

- Only the Codex driver receives the isolated writable worktree path.
- Dialectic supplies reviewers only a diff packet and neutral temporary CWD, and supplies council agents only the user prompt plus controller-produced discussion artifacts.
- Packet-only adapters use the customization/tool restrictions in section 5.4.3 and record their effective flags. Dialectic does not claim that CWD or prompt isolation prevents a trusted native process, managed policy, inherited environment, or compromised executable from discovering other locally readable data.
- Model output is data. It MUST NOT be executed as a shell command by the supervisor.
- Target-repository files MUST NOT be treated as supervisor configuration unless explicitly named by the user as the configuration file.
- The Codex driver intentionally receives normal repository context. Packet-only agents do not receive project instructions from Dialectic. Native user or managed configuration is disabled where current supported flags allow it; any residual configuration risk is recorded rather than hidden.
- Every controller-owned Git command uses per-invocation `core.hooksPath=<empty>`, `core.fsmonitor=false`, and `core.pager=cat`. Commits additionally use `commit.gpgSign=false`; diff generation disables external diff and text-conversion commands. Global, system, and repository Git configuration are not modified.
- Before each controller staging operation, every path to be staged is checked for an effective clean/process filter; a filtered path is rejected before Git can invoke that filter.
- Git commands use argument arrays and a controller-local commit identity.
- There is no automatic cleanup because preserving the branch/worktree is safer and more auditable for the MVP.
- The final terminal output MUST give the isolated worktree and branch, state exactly which original repository properties remained unchanged, and state that shared Git metadata and objects were added.
- The final output and README MUST provide explicit cleanup commands using the recorded paths: `git worktree remove <path>`, `git branch -D dialectic/<run-id>`, and `git worktree prune`. Dialectic does not run them automatically.

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
| CORE-002 | Unknown credential-like field such as `api_key` supplied | Strict schema rejects the unknown field without entropy heuristics |
| CORE-003 | Unsupported review/discussion cycle count | Validation rejects values other than one |
| CORE-004 | Non-secret model environment reference expansion | Resolved model value is retained in normalized audit configuration |
| CORE-005 | Atomic run-state update interrupted before rename | Previous valid `run.json` remains readable |
| CORE-006 | Known allowlisted secret of at least eight characters appears in fixture | Persisted artifact contains a redaction marker; ordinary short words remain intact |
| CORE-007 | Agent timeout with delayed grandchild sentinel | Entire process tree is reaped and sentinel is never written |
| CORE-008 | Ctrl+C with concurrent trees and delayed grandchildren | Every tree is reaped and run becomes `CANCELLED` |
| CORE-009 | Two delayed parallel invocations | Recorded invocation intervals overlap; no wall-clock ratio assertion is used |
| CORE-010 | CLI reports a documented alias resolution, then a known non-equivalent model | Alias case is accepted and canonicalized; non-equivalent case fails as `MODEL_MISMATCH`; all names are recorded |
| CORE-011 | Invoke matching fixture through `dial` and `dialectic` | After normalizing run IDs, timestamps, and paths: artifact tree shape, summary, status, and exit code are identical |
| CORE-012 | Resolve executable available only as a Windows `.cmd` shim | `shutil.which()` result is absolute, recorded, and used for invocation |
| CORE-013 | Deliver a 200 KiB prompt containing Windows/POSIX metacharacters | Prompt travels by stdin/ACP; no argv element exceeds 4096 bytes; nothing is shell-expanded |
| CORE-014 | Invalid or traversal-shaped run ID passed to `dial status` | Rejected before path joining with exit code 2 |
| CORE-015 | Controller artifact lacks or mismatches `artifact_schema_version` | Artifact validation fails explicitly |
| CORE-016 | Count, ID, model-selector, lens, limit, or timeout boundary violated | Configuration fails with the exact field and bound |
| CORE-017 | Private run-directory permissions cannot be established | Preflight fails without launching a model |
| CORE-018 | Output is whole JSON, one `json` fence, zero fences, or two fences | First two parse deterministically; latter two fail and raw output is retained |
| CORE-019 | Tree termination cannot be confirmed after timeout or cancellation | Status is `FAILED`, failure kind is `PROCESS_CLEANUP_FAILED`, and trigger is retained |
| CORE-020 | CLI does not report an actual model | `actual_model=null` is recorded and the turn is not failed solely for absence |
| CORE-021 | Well-formed but unknown run ID passed to `dial status` | Clear not-found message and exit code 2 |
| CORE-022 | `dial status` reads valid `RUNNING`, `FINALIZED`, and `FAILED` records | Each record is displayed faithfully and lookup exits 0 |
| CORE-023 | `dial status` reads truncated, malformed, or schema-invalid `run.json` | Reports corrupt state without guessing and exits 3 |

### 11.4 Code Once tests

| ID | Test | Expected result |
|---|---|---|
| CODE-001 | Happy path: two reviewers return findings | One driver start, two parallel fresh reviews, one driver resume, two commits at most, then stop |
| CODE-002 | All reviewers pass | No repair call; status `FINALIZED`, outcome `COMPLETED_NO_FINDINGS` |
| CODE-003 | `@driver` reviewer | Same target/model as driver, operation `start` rather than `resume`, and a different session ID |
| CODE-004 | Reviewer concurrency | All reviewer start timestamps precede first reviewer completion |
| CODE-005 | Immutable review core | Every reviewer receives identical task, base SHA, review SHA, and diff hash; only lens and packet hash differ |
| CODE-006 | Driver transcript and worktree-path sentinels | Both are absent from reviewer prompt, argv, and packet artifact |
| CODE-007 | One reviewer fails | Status `FAILED`, kind `REVIEW_FAILED`; driver resume count is zero |
| CODE-008 | One reviewer returns invalid JSON/schema | Status `FAILED`, kind `REVIEW_FAILED`; raw output retained |
| CODE-009 | Reviewer returns mismatched SHA | Report rejected and run fails closed |
| CODE-010 | Driver repair feedback | Every normalized finding key and no provider identity appears in repair packet |
| CODE-011 | Driver omits, duplicates, or invents a disposition key | Report rejected; status `FAILED`, kind `REPAIR_FAILED` |
| CODE-012 | Driver fixes findings and edits worktree | New changes committed; outcome `COMPLETED_AFTER_REPAIR` |
| CODE-013 | Driver rebuts every finding without edits | No second commit; outcome `COMPLETED_WITH_REBUTTALS` |
| CODE-014 | Driver leaves any finding `not_fixed` | Outcome `COMPLETED_WITH_UNRESOLVED_FINDINGS`; summary highlights every unresolved key |
| CODE-015 | Driver produces no initial changes | Status `FAILED`, kind `NO_CHANGES`; no reviewers run |
| CODE-016 | Diff exceeds configured bound | Status `FAILED`, kind `DIFF_TOO_LARGE`; snapshot remains and no reviewers run |
| CODE-017 | Original repository is dirty | Preflight fails before worktree creation |
| CODE-018 | Full happy-path Git integration | Original files, index, branch, `HEAD`, pre-existing refs, `main`, and status bytes match baseline; Dialectic branch contains final code |
| CODE-019 | Exact call-count guard | No second review call exists after repair |
| CODE-020 | Failure after driver changes | Partial isolated worktree is preserved and reported |
| CODE-021 | Two reviewers both use local finding ID `F1` | Feedback assigns distinct `reviewer-a/001` and `reviewer-b/001` keys; both require dispositions |
| CODE-022 | Mixed `fixed`, rebutted, and `not_fixed` dispositions | Outcome is `COMPLETED_WITH_UNRESOLVED_FINDINGS`; rebuttal and edits remain visible |
| CODE-023 | Driver claims any finding `fixed` but makes no aggregate repair change | Status `FAILED`, kind `REPAIR_FAILED` |
| CODE-024 | Driver adds or modifies a binary file | Snapshot is preserved; status `FAILED`, kind `UNSUPPORTED_REPOSITORY`; no reviewer runs |
| CODE-025 | Repository uses sparse checkout, tracked submodule, Git LFS path, or tracked clean/smudge filter | Preflight rejects the repository before worktree creation |
| CODE-026 | Repository contains checkout/commit hook sentinels | Worktree creation and both controller commits execute no repository hook; global/repository config is unchanged |
| CODE-027 | External-diff, textconv, fsmonitor, or commit-signing sentinels are configured | Review diff is deterministic and no external command executes |
| CODE-028 | Initial Codex turn returns no resumable session ID | Status `FAILED`, kind `DRIVER_FAILED`; no reviewer runs |
| CODE-029 | Second code run targets a repository whose lock is held | It fails as `REPOSITORY_BUSY` and names the holding run; first run is unaffected |
| CODE-030 | Code workflow wall clock expires during concurrent reviews | All process trees are reaped; status `TIMED_OUT`; no repair starts |
| CODE-031 | Diff fits `max_diff_bytes` but one lens makes its packet exceed `max_packet_bytes` | Status `FAILED`, kind `PACKET_TOO_LARGE`; no reviewer starts |
| CODE-032 | Fresh linked worktree lacks an ignored environment sentinel | Driver prompt warns about absent ignored artifacts and does not ask it to repair environment setup |
| CODE-033 | Reviewer finding names a path outside the diff | Structurally valid report is accepted and finding passes through unchanged |
| CODE-034 | Driver adds a path newly matched by a clean/process filter | Run fails as `UNSUPPORTED_REPOSITORY` before staging; filter sentinel never executes |

### 11.5 Council Once tests

| ID | Test | Expected result |
|---|---|---|
| COUNCIL-001 | Three valid participants | Three blind starts, three resumes for cross-examination, one fresh moderator, three resumes for ballots |
| COUNCIL-002 | Blindness | No opening prompt contains another participant response or identity |
| COUNCIL-003 | Anonymized cross-examination | Participants see A/B/C aliases and no provider brands |
| COUNCIL-004 | Participant changes its mind | Revision records `changed_mind=true` and reason |
| COUNCIL-005 | Moderator isolation | Moderator uses a fresh session and produces no ballot |
| COUNCIL-006 | Candidate proposition coverage | Every final ballot covers every proposition exactly once |
| COUNCIL-007 | Three of three accept | Status `FINALIZED`, outcome `UNANIMOUS` |
| COUNCIL-008 | Two accept, one rejects, no blocker | Status `FINALIZED`, outcome `ROUGH_CONSENSUS`, minority report retained |
| COUNCIL-009 | Two accept, one raises a valid blocker | Status `FINALIZED`, outcome `CONTESTED` |
| COUNCIL-010 | One participant abstains and threshold fails | Status `FINALIZED`, outcome `CONTESTED` |
| COUNCIL-011 | Participant fails during opening/cross-exam/ballot | Status `FAILED`, kind `NO_QUORUM`; partial artifacts retained |
| COUNCIL-012 | Moderator fails | Status `FAILED`, kind `MODERATOR_FAILED`; no ballots run |
| COUNCIL-013 | Overall wall clock expires | Status `TIMED_OUT`; all active process trees reaped |
| COUNCIL-014 | Exact round-count guard | No participant receives a second cross-examination prompt after ballots |
| COUNCIL-015 | User-facing report | Contains answer, vote matrix, dissent, blockers, unresolved questions, and actual identities |
| COUNCIL-016 | `max_dissenters` is negative | Configuration is rejected |
| COUNCIL-017 | `max_dissenters >= N` | Configuration is rejected with both values named |
| COUNCIL-018 | Every participant rejects | Outcome is `CONTESTED`, never consensus |
| COUNCIL-019 | Every participant accepts and rough threshold also passes | Ordered evaluation returns only `UNANIMOUS` |
| COUNCIL-020 | Candidate has zero propositions, duplicate IDs, empty IDs, or unknown supporting alias | Moderator artifact fails as `MODERATOR_FAILED` |
| COUNCIL-021 | Ballot duplicates, omits, or invents a proposition ID | Participant phase fails as `NO_QUORUM` |
| COUNCIL-022 | Blocking flag/evidence/overall vote are inconsistent | Ballot is rejected as invalid |
| COUNCIL-023 | Proposition votes and overall vote disagree with deterministic summary rule | Ballot is rejected as invalid |
| COUNCIL-024 | Opening participant lacks a resumable session ID | Immediate `NO_QUORUM`; no cross-examination or moderator call |
| COUNCIL-025 | Cross-examination ledger delivered to Participant B | It contains all positions including B's, identifies B's own alias, and contains no runtime/model map |
| COUNCIL-026 | Participant count outside 2..5 or participant IDs duplicate/invalid | Configuration fails before model invocation |
| COUNCIL-027 | Council wall clock expires with several active agents | Every participant/moderator process tree is reaped before `TIMED_OUT` is persisted |
| COUNCIL-028 | Confidence is outside 0.0..1.0 | Opening position is schema-invalid and run fails as `NO_QUORUM` |
| COUNCIL-029 | Cross-examination or ballot packet exceeds `max_packet_bytes` | Status `FAILED`, kind `PACKET_TOO_LARGE`; no participant in that phase starts |

### 11.6 Adapter contract tests

Each native adapter MUST have fixture-based tests covering:

- Executable resolution, absolute-path reuse, CLI-version capture, and authentication preflight.
- Successful first turn parsing.
- Native session ID extraction.
- Resume invocation with that exact ID.
- Requested model forwarding.
- Non-zero exit.
- Missing/invalid envelope.
- Native structured-field extraction and deterministic whole-text/single-fence fallback where applicable.
- Prompt delivery over stdin or ACP with no unbounded argv content.
- Required packet-only safety/customization flags and the absence of the target repository path.
- Timeout and complete process-tree termination.
- Provider-reported actual model and usage when present.
- Prompts containing spaces, quotes, newlines, Unicode, `$()`, backticks, `&`, `|`, `^`, `%`, `<`, and `>` without shell or `.cmd` reinterpretation.
- Unsafe model selectors or native session IDs are rejected before invocation; all CLI file arguments are safe relative controller names.
- The exact neutral CWD and session-continuation shape used by packet-only roles.

Codex driver fixtures MUST cover stdin prompt `-`, ignored user config/rules, no-network `workspace-write`, never-ask approval behavior, protected Git metadata, and absence of any full-access bypass. Codex packet fixtures MUST cover stdin prompt `-`, `--skip-git-repo-check`, `--ignore-user-config`, `--ignore-rules`, and read-only sandboxing. Claude fixtures MUST cover piped stdin, `--safe-mode`, disabled tools/MCP, schema output, and resume. Grok fixtures MUST cover ACP initialization with empty capabilities, session creation, repeated `session/prompt`, and process teardown.

Recorded envelopes MUST be version-labeled so a future CLI-output change produces an explicit fixture update rather than silent parsing drift. A new unrecognized CLI version does not silently inherit a fixture contract; the live preflight reports that support has not been verified.

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
- This is an opt-in manual release verification, not a mandatory CI gate.

Live council smoke fixture:

- A harmless architecture question with two or three available targets.
- The test requires valid positions, revisions, candidate, ballots, and one documented terminal outcome.
- It does not require the models to agree.
- This is an opt-in manual release verification, not a mandatory CI gate.

## 12. Definition of done

The MVP is complete only when all of the following are true:

### Code Once

- A real Codex driver can modify an isolated Git worktree for one simple task.
- Between one and five configured review targets can review the same immutable bounded diff concurrently.
- `@driver` produces a fresh independent review session.
- Valid findings are returned once to the original Codex driver session.
- The driver produces a disposition for every finding and may modify the code once.
- The controller commits the final isolated state and stops without re-reviewing.
- The original checked-out files, index, branch, `HEAD`, pre-existing branches, and `main` remain unchanged; the intentionally added Dialectic branch, worktree metadata, commits, and objects are reported.
- The summary explicitly states whether repair occurred and that repaired code was not re-reviewed.

### Council Once

- Between two and five participants receive one identical blind prompt concurrently.
- They perform exactly one anonymized cross-examination round using their original sessions.
- A fresh non-voting moderator creates independently votable propositions.
- Participants cast complete structured ballots.
- The controller deterministically separates finalized outcomes (`UNANIMOUS`, `ROUGH_CONSENSUS`, `CONTESTED`) from execution failure (`NO_QUORUM`, moderator/provider failure), timeout, and cancellation, then stops.
- Dissent and blocking objections remain visible in the result.

### Engineering

- All mandatory tests pass offline on Windows and Linux.
- No test or implementation path uses shell interpolation.
- Large prompts travel over stdin or ACP, never argv.
- Timeouts and cancellation reap complete process trees on Windows and Linux.
- Controller-owned Git operations execute no repository hook or external diff/text-conversion command.
- The Codex driver runs in the required no-network `workspace-write` profile, cannot write the linked Git metadata, and cannot request an expanded permission boundary.
- Controller staging invokes no clean/process filter, including for a path created after preflight.
- Credentials are absent from persisted fixtures and run artifacts.
- Exact call-count tests prove neither workflow loops.
- The stable run-status/outcome schemas and exit-code table are implemented exactly.
- A short README documents installation, native CLI prerequisites, trusted-local-process boundary, configuration, commands, artifact locations, cleanup commands, cost/quota warning, and MVP limitations.

## 13. Agile implementation slices

### Slice 0: Skeleton and contracts

Deliver:

- Python package and CLI skeleton.
- Pydantic configuration and artifact schemas.
- `AgentAdapter` protocol and scripted adapter.
- Atomic `RunStore` and redaction.
- Canonical run status, outcome, failure-kind, run-ID, artifact, and exit-code contracts.
- Cross-platform process-tree and repository-lock abstractions with fake-process tests.
- Core unit tests.

Exit criterion: CORE-001 through CORE-023 pass without Git or native agents.

### Slice 1: Offline Code Once vertical slice

Deliver:

- Git preflight and isolated worktree.
- Scripted Codex driver.
- Parallel scripted reviewers.
- Feedback construction.
- One scripted driver resume.
- Final commits and summary.

Exit criterion: CODE-001 through CODE-034 pass with no native AI CLI installed.

### Slice 2: Native agent adapters

Deliver:

- Codex adapter supporting start, fresh review, structured output, and resume.
- Claude adapter supporting fresh review/council turn, structured output, and resume.
- Grok ACP adapter supporting fresh review/council turns, deterministic JSON extraction/validation, and same-session continuation.
- Version-labeled adapter fixtures and preflight diagnostics.
- Opt-in live code smoke test.

Exit criterion: an explicitly opt-in manual smoke run proves that a simple Codex task can flow through the available real reviewers and stop correctly; recorded adapter fixtures remain the CI gate.

### Slice 3: Offline Council Once vertical slice

Deliver:

- Blind opening fan-out.
- Alias/position ledger.
- One session-resumed cross-examination round.
- Fresh moderator candidate.
- Final ballots and deterministic consensus.
- Council report.

Exit criterion: COUNCIL-001 through COUNCIL-029 pass offline.

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
11. Add capability-enforced container/OS isolation for untrusted reviewer and council executables.
12. Add explicit retry/backoff and reviewer-substitution policy for rate limits and transient failures.

This ordering preserves the MVP's two proven vertical flows and adds looping as a state-machine extension rather than a rewrite.

## 15. Provider adapter references

The adapter contract is based on current primary documentation and MUST be rechecked when a fixture's CLI version changes:

- [Codex non-interactive and developer commands](https://developers.openai.com/codex/non-interactive-mode) and [sandbox/approval security](https://developers.openai.com/codex/agent-approvals-security): stdin prompt `-`, JSONL, structured output, resume, repository-check override, configuration isolation, non-interactive workspace editing, and protected Git metadata.
- [Claude Code CLI reference](https://code.claude.com/docs/en/cli-reference) and [headless mode](https://code.claude.com/docs/en/headless): piped input, JSON Schema output, session resume, safe mode, tool/MCP restrictions, and stdin size.
- [Grok Build headless/ACP documentation](https://docs.x.ai/build/cli/headless-scripting) and [CLI reference](https://docs.x.ai/build/cli/reference): ACP over stdio, sessions, JSON output, safety controls, and update suppression.

## 16. Revision 0.2 reconciliation summary

Revision 0.2 incorporates the blocking and correctness findings from the independent Codex/Sol and Claude/Opus reviews. In particular it adds safe Windows prompt transport, executable resolution, normalized finding keys, bounded consensus arithmetic, strict ballot identity/coherence, complete process-tree termination, a bounded Codex driver profile, disabled Git hooks/external diff helpers, pre-staging clean-filter rejection, pinned diff generation, binary/unsupported-repository rejection, precise Git side-effect language, canonical status/outcome/exit contracts, versioned run artifacts, run-ID validation, disposition precedence, repository locking, deterministic output extraction, and expanded negative tests.

The only deliberate scope choice is reviewer/council isolation: v0.1.0 provides packet/context isolation with current native safe-mode controls on a trusted local machine. It does not claim an OS confidentiality boundary. Capability-enforced isolation remains post-MVP.
