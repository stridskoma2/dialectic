# Dialectic MVP Implementation and Test Specification

**Specification revision:** 0.4

**Target product version:** 0.1.0

**Status:** Final implementation candidate after targeted v0.3 reconciliation

**Date:** 2026-08-28

**Working product name:** Dialectic

**Alternative names retained:** VerityLoop, OmniPilot

## 1. Purpose

Build the smallest useful local supervisor that proves two cross-model workflows:

1. **Code Once:** one Codex driver performs one small coding task; one or more configured review agents review the resulting immutable diff in parallel; their structured findings are returned to the same Codex driver session once; the driver incorporates, rebuts, or leaves them unresolved; the run then stops.
2. **Council Once:** one prompt is sent blindly to two or more configured model agents; each sees the anonymized positions in one cross-examination round; a moderator creates a candidate conclusion; the participants cast structured ballots; the controller calculates unanimous, rough-consensus, or contested outcome; the run then stops.

This MVP proves orchestration, session continuation, structured cross-provider communication, concurrency, Git isolation, deterministic consensus calculation, and durable evidence. It deliberately does **not** implement continuous loops yet.

The MVP CLI MUST invoke a transport-neutral application-service boundary rather than embedding workflow logic in command handlers. This is an architectural seam, not an additional MVP interface: it allows a later inbound MCP server, API, TUI, or editor integration to request the same controller-owned workflows without moving orchestration authority into the calling host.

The design must make a later backward transition possible:

- Code mode: `REPAIR -> REVIEW`
- Council mode: `BALLOT -> DISCUSSION_ROUND`

Those transitions are not enabled in version 0.1.

Normative terms **MUST**, **SHOULD**, and **MAY** have their usual requirement meanings.

## 2. MVP product decisions

### 2.1 Fixed decisions

- The supervisor is a local Python application.
- Codex is the only writable driver supported by the MVP.
- Codex, Claude Code, and Grok Build are supported as reviewer and council-agent targets when their native CLIs are installed and authenticated.
- A reviewer entry named `@driver` resolves to the driver's runtime, model, effort, and authentication context, but starts a fresh independent session.
- No tracked content in the target repository's checked-out working tree, its index, its checked-out branch, its original `HEAD`, any pre-existing branch, or `main` is modified by the supervisor. A code run intentionally adds a `dialectic/<run-id>` branch, linked-worktree metadata, commits, and Git objects to the repository's shared Git database.
- The controller, not an AI agent, owns Git branch creation, worktree creation, snapshot commits, final commits, state transitions, timeouts, and consensus calculation.
- The CLI is the only MVP ingress. It validates human-supplied paths and invokes the same `DialecticService` application boundary that any future ingress MUST use; no ingress owns workflow semantics.
- Every model-facing output used for a control decision must validate against a controller-owned schema.
- Mutable runtime state is stored outside the target repository.
- The MVP never pushes, opens a pull request, merges, deploys, or deletes a worktree automatically.
- Provider credentials and authentication files are never copied into run artifacts.
- Code mode takes an exclusive advisory lock per target Git common directory. Concurrent code runs against one repository fail before worktree creation; council runs do not require this lock.
- Windows 11 and Linux are release platforms. macOS may work but is not part of the v0.1.0 definition of done.
- Reviewer and council packet isolation is a context-minimization contract on a trusted local machine, not an OS confidentiality boundary. Configured native CLIs execute with the user's operating-system identity and may retain access granted by user or managed configuration.
- Code Once and Council Once participants MUST NOT inherit MCP servers, apps, general shell tools, or other user-configured tool surfaces. Any future participant capability must be controller-issued, role-bound, bounded, auditable, and introduced as a separately specified workflow/profile.

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
- TUI, web UI, editor extension, inbound MCP server, API server, ACP server, or background daemon. MCP is deliberately deferred until after the native CLI alpha or beta has proved the engine.
- Registered repository/profile catalogs, non-CLI caller authorization, idempotent remote start requests, and other controls needed by a future inbound interface.
- Participant access to MCP servers or user-configured tool surfaces in Code Once or Council Once.
- Gemini and any provider runtime other than the three named MVP runtimes; new runtimes remain adapter additions rather than controller changes.
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
  max_findings_per_reviewer: 20
  max_total_findings: 50
  max_council_participants: 5
  max_propositions: 8
  max_config_bytes: 65536
  max_input_bytes: 65536
  max_diff_bytes: 262144
  max_changed_paths: 1000
  max_changed_regular_file_bytes: 8388608
  max_candidate_change_bytes: 33554432
  max_packet_bytes: 393216
  max_lens_chars: 4096
  max_model_field_chars: 32768
  max_model_list_items: 100
  max_agent_stdout_bytes: 8388608
  max_agent_stderr_bytes: 2097152
  max_turn_scratch_bytes: 67108864
  preflight_seconds: 30
  capability_probe_seconds: 120
  agent_turn_seconds: 300
  code_run_seconds: 1200
  council_run_seconds: 1200
  graceful_kill_seconds: 5
  code_review_cycles: 1
  council_discussion_rounds: 1
```

Requirements:

- Configuration, task, and council-prompt files MUST decode as strict UTF-8 without a byte-order mark. UTF-8 BOM, UTF-16, invalid UTF-8, and Unicode surrogate code points are rejected as `INVALID_INPUT` before model or repository work.
- Before parsing, the raw configuration MUST fit the product hard ceiling of 262144 bytes. It is then parsed with a safe loader as a JSON-compatible YAML subset: mappings, sequences, strings, numbers, booleans, and null only. Custom tags, object construction, anchors, aliases, merge keys, and duplicate mapping keys are rejected. After the limits object validates, the same raw byte count MUST also fit its configured `max_config_bytes`.
- Environment expansion recognizes only a complete string scalar matching `${[A-Z_][A-Z0-9_]*}` in model, effort, runtime, lens, or ID fields. A missing or empty variable fails validation. `$${NAME}` represents the literal `${NAME}`; every other dollar sign is literal. Resolved non-secret values are retained in the normalized audit configuration.
- The strict configuration schema exposes no credential, token, API-key, auth-file, shell-environment, or permission-profile field. Unknown fields are rejected. Credentials MUST be supplied through the native CLI's saved authentication or the adapter's constrained process environment, never through Dialectic configuration.
- The MVP MUST accept exactly `code_review_cycles: 1` and `council_discussion_rounds: 1`. Other values MUST fail validation with a message explaining that iteration is a post-MVP feature.
- Code mode MUST accept between one and five reviewers and MUST reject a count above the configured `max_reviewers`.
- `max_total_findings` MUST be no greater than both `max_reviewers * max_findings_per_reviewer` and `max_model_list_items`, so the aggregate feedback and required disposition list always have a representable bound.
- Council mode MUST accept between two and five participants and MUST reject a count above the configured `max_council_participants`.
- The configured `max_propositions` MUST be between 1 and 20. A moderator candidate MUST contain between one proposition and that configured maximum. The moderator does not vote, so it MUST NOT be allowed to increase ballot difficulty beyond this controller-owned bound.
- Reviewer and participant IDs MUST be unique within their respective lists and match `[a-z][a-z0-9-]{0,31}`.
- After environment expansion, every configured model selector MUST be between 1 and 128 characters and match `[A-Za-z0-9][A-Za-z0-9._:/@+\[\]-]{0,127}`. An adapter MAY narrow that allowlist for a specific native CLI but MUST NOT accept shell metacharacters or quoting/control characters.
- A reviewer with `target: "@driver"` MUST NOT also specify `runtime`, `model`, or `effort`. A concrete reviewer MUST specify `runtime` and `model` and MUST NOT specify `target`.
- `lens` is model-facing free text between 1 and `max_lens_chars` characters; it is not a file path or enum.
- Unless a model-facing schema gives a smaller bound, every non-null free-text field MUST contain at most `max_model_field_chars` Unicode scalar values and every model-produced list MUST contain at most `max_model_list_items` entries. Required semantic strings are non-empty after trimming.
- Every byte and timeout limit MUST be positive and within the hard ceilings in the following table.
- `max_changed_regular_file_bytes` MUST be no greater than `max_candidate_change_bytes`. Candidate-change limits apply before staging and are independent of the smaller final `max_diff_bytes` review-packet limit.
- Every outbound `AgentRequest` prompt, including council ledgers, candidates, and ballots, MUST fit `max_packet_bytes` before that phase launches any participant. Overflow fails as `PACKET_TOO_LARGE` without launching a partial phase.
- After participant resolution, consensus MUST satisfy `0 <= max_dissenters < N`, where `N` is the participant count. Blocking objections always prevent consensus in the MVP; there is no configuration switch for this behavior.
- No implicit model replacement or fallback is permitted. A documented provider alias may resolve to its canonical model and is not a fallback. Each adapter records the requested selector and canonical resolution when available; a known non-equivalent `actual_model` fails as `MODEL_MISMATCH`. An unavailable actual-model field remains `null` and is not invented.

Hard validation ceilings prevent accidental unbounded local runs:

| Field | Allowed value |
|---|---:|
| `max_reviewers` | 1..5 |
| `max_findings_per_reviewer` | 1..100 |
| `max_total_findings` | 1..500 |
| `max_council_participants` | 2..5 |
| `max_propositions` | 1..20 |
| `max_config_bytes` | 1..262144 |
| `max_input_bytes` | 1..262144 |
| `max_diff_bytes` | 1..1048576 |
| `max_changed_paths` | 1..10000 |
| `max_changed_regular_file_bytes` | 1..67108864 |
| `max_candidate_change_bytes` | 1..268435456 |
| `max_packet_bytes` | 1..1572864 |
| `max_lens_chars` | 1..8192 |
| `max_model_field_chars` | 1..65536 |
| `max_model_list_items` | 1..500 |
| `max_agent_stdout_bytes` | 256..67108864 |
| `max_agent_stderr_bytes` | 256..16777216 |
| `max_turn_scratch_bytes` | 1..1073741824 |
| `preflight_seconds` | 1..300 |
| `capability_probe_seconds` | 1..600 |
| `agent_turn_seconds` | 1..3600 |
| `code_run_seconds`, `council_run_seconds` | 1..14400 |
| `graceful_kill_seconds` | 1..30 |

## 5. Technical architecture

### 5.1 Stack

- Python 3.12+
- `asyncio.create_subprocess_exec` for POSIX native CLI execution
- A narrow pywin32/ctypes Win32 launcher using `STARTUPINFOEXW` so processes are created suspended and inside a Job Object
- Pydantic v2 for configuration and message schemas
- PyYAML with a strict safe-loader subclass for the closed configuration grammar
- Typer for the CLI
- Rich for concise progress and final summaries
- `platformdirs` for the default state directory
- `filelock` for a cross-platform per-repository advisory lock
- `pywin32` on Windows for Job Object process-tree ownership
- Git CLI for repository and worktree operations
- Pytest and pytest-asyncio for tests
- JSON and JSONL artifacts; no database in the MVP

Logical commands MUST be represented as executable-plus-argument arrays. Direct executables are spawned without a shell. The only MVP exception is a resolved Windows `.cmd`/`.bat` vendor shim: a versioned adapter may convert it to a dedicated `WindowsBatchLaunchSpec` whose suspended root executable is the absolute system `cmd.exe` obtained from `GetSystemDirectoryW`. That launch uses fixed `/d /q /v:off /s /c` controls, a tested command-line encoder, a shim path that excludes `% ! ^ & | < >` and control characters, and only fixed or grammar-validated adapter arguments. It is not a general-purpose shell facility.

The implementation MUST NOT interpolate task/prompt text, repository paths, credential values, or other unbounded input into a command string. Prompts and diffs travel only over stdin/ACP; model selectors, session IDs, effort values, and controller file names remain bounded by their declared grammars. An adapter that cannot produce a safe direct or batch launch plan fails preflight.

### 5.2 Components

| Component | Responsibility |
|---|---|
| `ConfigLoader` | Load YAML, expand permitted environment references, validate limits and targets |
| `DialecticCLI` | Parse the human CLI surface, load named local files, and invoke `DialecticService`; contains no workflow logic |
| `DialecticService` | Expose typed Code Once, Council Once, status, and result use cases independently of ingress transport |
| `AgentRegistry` | Resolve runtime names and `@driver` into concrete agent targets |
| `AgentAdapter` | Invoke one native agent turn, continue a known session, parse its envelope, and return normalized metadata |
| `CodexAdapter` | Codex driver, reviewer, moderator, and council participant execution |
| `ClaudeAdapter` | Claude reviewer and council participant/moderator execution |
| `GrokAdapter` | Grok reviewer and council participant/moderator execution |
| `GitWorkspace` | Validate the repository and create an isolated branch/worktree |
| `ChangeValidator` | Stage through the controller, reject unsupported index/diff state, and persist the exact bounded snapshot |
| `RunStore` | Atomically persist state, events, prompts, normalized reports, and summaries outside the repository |
| `ProcessSupervisor` | Own each platform process unit, bound streams/scratch, enforce timeouts, and confirm platform-scoped cleanup |
| `CredentialBoundary` | Construct minimal CLI environments and withhold adapter credentials from model-generated child commands |
| `TurnWorkspace` | Reserve, monitor, audit, and remove per-turn `.dialectic-turn/` scratch |
| `RepositoryLock` | Prevent concurrent code runs against the same Git common directory |
| `CodeOnceOrchestrator` | Execute the one coding/review/repair state machine |
| `CouncilOnceOrchestrator` | Execute the one bounded council state machine |
| `ConsensusCalculator` | Calculate final council status from validated ballots without model judgment |
| `Redactor` | Remove known secret values and sensitive environment fields before artifact persistence |

The MVP `DialecticCLI` is the only caller of `DialecticService`. The service accepts already validated, bounded domain requests rather than CLI argv, configuration-file paths, executable paths, or provider credentials. `run_code_once` and `run_council_once` retain foreground ownership until a terminal record has been persisted; `get_run` and `get_result` return bounded typed projections. A future MCP or API ingress MUST translate its own authorized identifiers into these same requests and MUST NOT call orchestrator internals directly. Returning a `run_id` while execution continues is not supported until a post-MVP job owner and crash-recovery contract exist.

### 5.3 Agent target

The internal selection unit is an agent target, not merely a model:

```python
class AgentTarget(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")
    runtime: Literal["codex", "claude-code", "grok-build"]
    model: str
    effort: str | None = None

class ReviewerSpec(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")
    id: str
    target: Literal["@driver"] | None = None
    runtime: Literal["codex", "claude-code", "grok-build"] | None = None
    model: str | None = None
    effort: str | None = None
    lens: str

class ParticipantSpec(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")
    id: str
    runtime: Literal["codex", "claude-code", "grok-build"]
    model: str
    effort: str | None = None

class ModeratorSpec(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")
    runtime: Literal["codex", "claude-code", "grok-build"]
    model: str
    effort: str | None = None
```

Every external-input, model-facing, and controller-artifact Pydantic model uses `ConfigDict(strict=True, extra="forbid")`; coercion of string booleans, numbers, or other alternate representations is forbidden. `ReviewerSpec` enforces the `@driver` versus concrete-target exclusive-or rule from section 4. Runtime-specific effort values are validated by the corresponding adapter; an unsupported value fails preflight rather than being silently dropped.

Runtime/model identity and invocation transport are separate internal concerns. A versioned adapter fixture selects its supported native transport (`stdin`, print-mode stdin, or ACP stdio); the MVP configuration cannot override that choice. A later CLI, ACP, or direct-API transport therefore changes an adapter fixture, not controller workflow semantics. MCP is an ingress concern and is not an agent-invocation transport in this design.

The response MUST distinguish the requested target from what the native CLI reports actually ran:

```python
class AgentResponse(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")
    artifact_schema_version: Literal[1]
    tool_version: str
    runtime: str
    requested_model: str
    resolved_requested_model: str | None
    actual_model: str | None
    session_id: str | None
    resolved_executable: str
    spawned_root_executable: str
    launch_kind: Literal["direct", "windows-batch-shim"]
    cli_version: str
    prompt_transport: Literal["stdin", "acp-stdio"]
    effective_safety_flags: list[str]
    effective_permission_profile: dict
    credential_env_names: list[str]  # names only, never values
    denied_credential_paths: list[str]
    text: str
    structured_output: dict | None
    exit_code: int
    duration_ms: int
    stdout_bytes: int
    stderr_bytes: int
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
2. Convert the result to an absolute path. On Windows, classify `.exe`/`.com` as direct and `.cmd`/`.bat` through the constrained batch-shim contract in section 5.1; reject an unrecognized extension or unsafe shim path.
3. Record the resolved launcher path, actual spawned root executable, launch kind, and reported CLI version in `run.json`.
4. Verify authentication without printing or persisting credential material.
5. Verify that the installed version supports every flag and protocol the adapter requires.
6. Resolve the adapter fixture's credential-environment names, saved-auth paths, and required non-secret process-environment names without reading saved-auth file contents.
7. Validate a cached attestation from the adapter's versioned local capability probe, or run the probe under `capability_probe_seconds` when no valid attestation exists; checking argv flags alone is insufficient.

All later invocations MUST use the recorded launch plan. Because prompts and repository paths never enter the batch command line, user content cannot be reinterpreted by the shim's `cmd.exe` parser.

Capability attestations live under the private state root and include `artifact_schema_version`, `tool_version`, runtime, resolved executable identity, CLI version, platform, adapter-fixture version, permission-profile template hash, and probe-result hash. Any mismatch, unreadable entry, managed-policy change, or failed dynamic path check invalidates the attestation and causes a fresh probe rather than a skip. The cost-bearing probe has its own timeout and does not consume `preflight_seconds`; an implementation MAY expose the same operation as `dial doctor`, but that command is not required for v0.1.0.

#### 5.4.2 Prompt transport

- Codex requests MUST pass `-` as the prompt argument and stream the complete UTF-8 prompt over stdin for both `exec` and `exec resume`.
- Claude requests MUST use print mode and stream the complete packet over stdin. The argv prompt, if required by the installed CLI, is a fixed controller-owned sentence directing Claude to process stdin.
- Grok requests MUST use ACP stdio with update checks, memory, web search, planning, subagents, and built-in tools disabled through supported flags. The adapter speaks JSON-RPC over stdin/stdout, creates a session with no filesystem, terminal, or MCP client capabilities, and sends prompt text in `session/prompt`. A council participant's ACP process and session remain alive through its three turns.
- No argv element may exceed 4096 UTF-8 bytes. Runtime adapters MUST apply the configured model-selector rule and MUST validate any native session ID placed in argv against `[A-Za-z0-9][A-Za-z0-9._:-]{0,255}`. Nonconforming values are rejected rather than escaped heuristically.
- A model process's working directory MUST be supplied through the subprocess `cwd` parameter, not a CLI path argument. Schema and output-file arguments, when required, MUST be controller-generated relative names containing no `..` component. Packet-only roles place them in the private neutral CWD. Driver roles place them only in `.dialectic-turn/control/` at the isolated-worktree root; model-generated commands may write only `.dialectic-turn/tmp/`, never `control/` or the scratch root itself. This keeps task text and user-selected filesystem paths out of a Windows `.cmd` command line without trusting a model-replaceable control path.

Repository preflight MUST reject any tracked entry at or below `.dialectic-turn/` as `UNSUPPORTED_REPOSITORY`. Before every driver turn, the controller MUST prove the path is absent without following links; create `.dialectic-turn/control/` and `.dialectic-turn/tmp/` privately; pre-create bounded control files; and set `TMP`, `TEMP`, and `TMPDIR` to `tmp/`. The active child permission profile denies creation, rename, replacement, or deletion of the scratch root and `control/` while permitting bounded writes beneath `tmp/`.

Controller ingestion opens expected control output with platform no-follow/reparse-safe semantics, requires one regular single-link file owned by the current user, obtains its size bound from the opened handle, and rejects identity changes, symlinks, junctions/reparse points, hard-link anomalies, FIFOs, sockets, devices, and unexpected entries. Cleanup removes directory entries without traversing symlink or reparse targets and remains anchored to the verified scratch-root handle. Any type, identity, containment, ingestion, or cleanup failure is `INTERNAL_ERROR`; no untrusted content is copied first.

The controller samples no-follow regular-file logical size beneath `tmp/` at least four times per second as a best-effort in-flight detector and performs the authoritative size/type check after process exit. Sampling is not a filesystem quota and does not claim that transient physical writes never exceed `max_turn_scratch_bytes`. A detected or final overage terminates the owned process unit and fails as `AGENT_OUTPUT_TOO_LARGE`. After copying verified control artifacts, the controller removes the complete reserved directory in a `finally` block and proves absence before Git inspection. A task MUST NOT use it for product output.

#### 5.4.3 Authentication and child-environment boundary

Every versioned, per-platform adapter fixture owns three normalized sets: credential environment names it may consume, saved-auth paths its CLI may read, and the complete non-secret environment-name set required to launch that CLI. The conceptual baseline is empty. A Windows fixture must explicitly name values such as `SystemRoot` when required; a POSIX fixture must explicitly name values such as `PATH`, `HOME`, or locale controls when required. Environment-name comparison is exact on POSIX and case-insensitive on Windows; paths use platform-appropriate canonical comparison. The controller copies only the fixture-declared non-secret values, the credential names actually used by the trusted CLI, and controller-owned `TMP`/`TEMP`/`TMPDIR` redirects. Proxy variables containing authentication are credential variables. No other controller environment is inherited, and the effective name set—but never secret values—is recorded.

A supplied credential-environment value MUST be non-empty and at least eight Unicode scalar values long; otherwise preflight fails because the value cannot satisfy the deliberately narrow known-value redaction contract. Saved native authentication has no such content check because Dialectic neither reads nor copies the auth file.

The Codex adapter MUST separately set an explicit `shell_environment_policy` for model-generated commands: `inherit="core"`, `ignore_default_excludes=false`, `experimental_use_profile=false`, and an `exclude` filter for every fixture credential name. It MUST NOT restore a credential with `set`. The effective permission profile MUST deny model-generated reads of every resolved saved-auth path, the Dialectic state root, and the original checked-out worktree while permitting only the minimum read-only linked-worktree Git metadata specified below. Authentication through an environment variable is valid only when the native CLI consumes it and the capability probe proves a model-generated child command cannot observe it. Redaction remains defense in depth; it is not a substitute for withholding credentials.

Credential names, denied paths, and the non-secret effective policy are auditable; credential values and saved-auth contents are not. If the installed CLI, managed policy, or platform cannot prove this separation, Codex driver preflight fails. Packet-only Claude and Grok roles expose no controller-supplied command/filesystem capability, but their native processes remain trusted executables running as the user.

#### 5.4.4 Driver and packet-only runtime policy

The Codex driver MUST run with the isolated worktree as its process CWD under one controller-defined **named permission profile**. This execution path MUST NOT pass `--sandbox`, load `sandbox_mode`/`sandbox_workspace_write`, or otherwise combine the named profile with the older sandbox configuration. A managed or installed configuration that displaces, weakens, or prevents the named profile fails preflight. The effective matrix for model-generated commands is:

| Resource | Access |
|---|---|
| Isolated-worktree product files | Read/write |
| `.dialectic-turn/tmp/` | Read/write within its detector and final bound |
| `.dialectic-turn/` and `control/` | Read only where the CLI requires it; no create, replace, rename, or delete |
| Linked-worktree `.git` pointer and minimum common/per-worktree metadata required for read-only Git inspection | Read only |
| Linked-worktree index, refs, objects, config, locks, and all other Git metadata mutations | Deny writes |
| Original checked-out worktree, saved-auth paths, and Dialectic state root | Deny reads and writes |
| Fixture-declared platform/runtime paths | Read only |
| Pre-redirect OS temporary roots and all other filesystem paths | Deny |
| Command network | Disabled |

Model-generated Git inspection runs with `GIT_OPTIONAL_LOCKS=0`. The profile uses a non-interactive never-ask approval policy and MUST NOT use `danger-full-access`, `--yolo`, auto-review, or any path that can expand the boundary. The resolved profile name and effective non-secret rules are retained in the turn artifact.

The adapter supplies the generated profile as a canonical, lexicographically ordered set of repeatable Codex `-c key=value` overrides defining `default_permissions`, `[permissions.dialectic-driver]`, its dynamic exact-path filesystem rules, network-off policy, and approval/configuration controls. It invokes `codex exec --ignore-user-config --ignore-rules` so authentication still uses the existing `CODEX_HOME` while `$CODEX_HOME/config.toml` does not. It does not create a profile file in `CODEX_HOME`, copy authentication, pass `--profile`, or pass `--sandbox`. Each override is one bounded argv element; direct launches use the ordinary argument array and Windows batch shims use only the fixed validated encoder from section 5.1. The recorded profile hash is computed from these canonical non-secret overrides. Preflight inspects the effective configuration and fails if system/managed policy introduces a legacy `sandbox_mode`, disallows the named profile, or otherwise changes the probed matrix.

The driver adapter MUST ignore user configuration and exec-policy rules, mark the worktree untrusted for project `.codex/` configuration/hooks/rules, disable MCP/apps/web search/subagents, and preserve ordinary `AGENTS.md` repository-instruction discovery. A versioned behavior probe MUST demonstrate: product and `tmp/` writes succeed; read-only `git status` and base-object inspection succeed; control-path and Git-metadata writes fail; and original-worktree, saved-auth, state-root, pre-redirect OS-temp, outside-workspace, network, and permission-expansion access fail without prompting.

Packet-only Codex roles MUST use a controller-defined read-only permission profile, a neutral temporary CWD, `--skip-git-repo-check`, `--ignore-user-config`, `--ignore-rules`, and a never-ask approval policy. Packet-only Claude roles MUST use `--safe-mode`, an empty built-in tool set, and no MCP configuration. Grok ACP roles MUST disable update checks, memory, web search, planning, subagents, and tools; advertise no filesystem, terminal, or MCP capabilities; and use supported configuration isolation so user, project, Claude-compatibility, Cursor-compatibility, and `.mcp.json` sources contribute zero MCP servers. The versioned Grok preflight records the effective `grok inspect` source inventory and fails if any MCP server or unapproved tool remains. Equivalent current controls may replace these only through an adapter fixture and specification update.

These controls minimize capabilities and context, but Dialectic does not claim that it confines a compromised native CLI executable or managed provider implementation. The target repository path MUST never appear in a packet-only prompt, argv, environment override, or packet artifact.

#### 5.4.5 Bounded native output

`ProcessSupervisor` MUST drain stdout and stderr concurrently and incrementally. POSIX uses asyncio subprocess pipes. The Windows `CreateProcessW` path uses one dedicated blocking reader thread per pipe; each thread feeds bounded chunks and completion/error events to the owning event loop with `loop.call_soon_threadsafe`. Both readers continue during graceful termination until EOF, overflow, or forced job termination, and every thread/handle is joined or closed before the turn returns. No execution path may use an API that accumulates an unbounded complete stream.

The supervisor accepts at most `max_agent_stdout_bytes` and `max_agent_stderr_bytes` respectively. On the first overflow it terminates the owned process unit, records `AGENT_OUTPUT_TOO_LARGE`, and retains a deterministic prefix. After known-value redaction, it shortens that prefix as needed and appends the fixed ASCII marker `<dialectic:truncated>\n`; the persisted stream remains within the configured limit. Structured truncation metadata also lives in the enclosing versioned turn artifact.

For a completed bounded stream, the controller concatenates captured chunks before known-value redaction, so a credential split across read boundaries is still removed. For an overflow diagnostic, it additionally discards an unpersisted trailing guard at least as long as the longest known credential byte sequence minus one before redaction, preventing a partial credential at the truncation boundary. Native envelopes and assistant text must decode as strict UTF-8. The strict JSON loader rejects duplicate object keys, `NaN`, positive/negative `Infinity`, nesting deeper than 64 during parsing, and strings containing lone surrogate code points; valid supplementary Unicode scalars remain accepted. Collection and string bounds from section 4 apply before an object becomes a control artifact.

#### 5.4.6 Output extraction

Each adapter first parses its versioned native envelope. When the runtime exposes a schema-validated structured field, that field is the only accepted payload. Otherwise the adapter applies this deterministic rule to the final assistant text:

1. Parse the complete text as JSON.
2. If that fails, accept exactly one fenced block tagged `json` and parse its complete contents.
3. Zero or multiple `json` fences, trailing non-whitespace inside the fence, or any schema failure fails the turn.

No other substring search, prose stripping, model-powered repair, or retry is permitted. Bounded stdout and stderr are retained in the agent's artifact directory after known-secret redaction.

#### 5.4.7 Session continuation

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
    initial.diff.sha256
    repair.delta.diff             # only when findings exist
    repair.delta.diff.sha256      # only when findings exist
    final.diff
    final.diff.sha256
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
# These declarations assume postponed annotation evaluation; outcome and failure
# aliases are the canonical definitions in section 6.3.
RunStatus = Literal[
    "CREATED", "RUNNING", "FINALIZED", "FAILED", "TIMED_OUT", "CANCELLED"
]

CodePhase = Literal[
    "PREFLIGHT", "WORKTREE_SETUP", "DRIVER_INITIAL", "INITIAL_VALIDATION",
    "REVIEWERS", "FEEDBACK", "DRIVER_REPAIR", "FINAL_VALIDATION", "REPORTING",
]

CouncilPhase = Literal[
    "PREFLIGHT", "OPENING_POSITIONS", "CROSS_EXAMINATION", "MODERATION",
    "BALLOTS", "REPORTING",
]

RunPhase = CodePhase | CouncilPhase

class RunRecord(BaseModel):
    artifact_schema_version: Literal[1]
    tool_version: str
    run_id: str
    mode: Literal["code", "council"]
    status: RunStatus
    phase: RunPhase | None
    code_outcome: CodeOutcome | None
    consensus_outcome: ConsensusOutcome | None
    failure_kind: FailureKind | None
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
    phase: RunPhase | None
    event_type: str
    payload: dict

class WorkspaceRecord(BaseModel):
    artifact_schema_version: Literal[1]
    tool_version: str
    repo_common_dir: str
    repo_filesystem_identity: str
    repo_lock_identity_sha256: str
    original_worktree: str
    original_branch: str | None
    base_sha: str
    dialectic_branch: str | None
    dialectic_worktree: str | None
    review_sha: str | None
    final_sha: str | None
    initial_diff_sha256: str | None
    repair_delta_sha256: str | None
    final_diff_sha256: str | None

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
    findings: list["NormalizedFinding"]

class SummaryRecord(BaseModel):
    artifact_schema_version: Literal[1]
    tool_version: str
    run_id: str
    mode: Literal["code", "council"]
    status: RunStatus
    outcome: CodeOutcome | ConsensusOutcome | None
    failure_kind: FailureKind | None
    unresolved_items: list[str]
    artifact_paths: dict[str, str]

class AliasMapArtifact(BaseModel):
    artifact_schema_version: Literal[1]
    tool_version: str
    aliases: dict[str, AgentTarget]

class AgentRequestArtifact(BaseModel):
    artifact_schema_version: Literal[1]
    tool_version: str
    role: str
    target_id: str
    prompt_sha256: str
    prompt: str
    output_schema: dict | None
    timeout_seconds: int
    access_mode: str

class ReviewReportArtifact(BaseModel):
    artifact_schema_version: Literal[1]
    tool_version: str
    reviewer_alias: str
    target: AgentTarget
    packet_sha256: str
    report: "ReviewReport"

class OpeningPositionArtifact(BaseModel):
    artifact_schema_version: Literal[1]
    tool_version: str
    participant_alias: str
    packet_sha256: str
    position: "OpeningPosition"

class CouncilRevisionArtifact(BaseModel):
    artifact_schema_version: Literal[1]
    tool_version: str
    participant_alias: str
    packet_sha256: str
    revision: "CouncilRevision"

class CandidateConclusionArtifact(BaseModel):
    artifact_schema_version: Literal[1]
    tool_version: str
    moderator_target: AgentTarget
    packet_sha256: str
    candidate: "CandidateConclusion"
```

Artifact-schema version 1 rejects undeclared fields. A later artifact-schema version MAY add fields, but existing fields cannot change meaning within version 1. Every declared field is always serialized. A value that is not yet available is explicitly `null` only when its type is nullable; declared fields are never omitted to signal absence. `phase=null` is valid only while status is `CREATED`. Model validators require a `CodePhase` for a code run and a `CouncilPhase` for a council run after execution begins, restrict summary outcomes to the matching mode, and enforce the status/outcome/failure invariants in section 6.3.

All timestamps are timezone-aware UTC and serialize as RFC 3339; event sequence numbers start at 1 and increase without gaps. Summary artifact paths are relative to the run directory. `events.jsonl` uses one `EventRecord` per line. `aliases.json` is the only council artifact that maps participant aliases to actual runtimes/models; model-facing artifacts never contain that mapping.

Every JSON filename has one unambiguous schema binding:

| Path pattern | Persisted schema/content |
|---|---|
| `run.json` | `RunRecord` |
| `events.jsonl` | One `EventRecord` per line |
| `input/config.redacted.json` | Versioned normalized configuration artifact |
| `git/workspace.json` | `WorkspaceRecord` |
| `driver/*.request.json` | Versioned `AgentRequestArtifact` containing the exact bounded request and prompt hash |
| `driver/*.response.json` | `AgentResponse`; raw native streams remain separate text files |
| `reviews/manifest.json` | `ReviewManifest` |
| `reviews/reviewer-*.json` | Versioned `ReviewReportArtifact` containing alias, target audit fields, packet hash, and the exact validated `ReviewReport` |
| `feedback.json` | `FeedbackArtifact` |
| `council/aliases.json` | `AliasMapArtifact` |
| `council/opening/participant-*.json` | Versioned wrapper containing alias, packet hash, and exact validated `OpeningPosition` |
| `council/cross-examination/participant-*.json` | Versioned wrapper containing alias, packet hash, and exact validated `CouncilRevision` |
| `council/candidate.json` | Versioned wrapper containing moderator audit fields and exact validated `CandidateConclusion` |
| `council/ballots/participant-*.json` | `DerivedBallot`, including the original validated `CouncilBallot` |
| `summary.json` | `SummaryRecord` |

Every wrapper is controller-owned and therefore carries `artifact_schema_version` and `tool_version`; embedded model payloads remain byte-for-byte equivalent to the validated parsed values and are never silently rewritten. `reviews/raw/` and `council/raw/` contain bounded, redacted `*.stdout.txt` and `*.stderr.txt` named by alias and turn.

### 6.2 Persistence and redaction requirements

- `run.json` MUST be written by temporary-file-plus-atomic-rename.
- `events.jsonl` MUST be append-only.
- Model prompts and responses MUST be retained for audit, after redaction.
- The alias map MUST identify which configured target corresponds to Participant A/B/C in local artifacts; model-facing prompts MUST use aliases rather than provider brand names.
- Authentication tokens, complete process environments, provider auth files, and unredacted secret-bearing command lines MUST NOT be persisted.
- On POSIX, run directories and temporary packet directories MUST be mode `0700` and files `0600`. On Windows, the controller MUST apply a DACL limited to the current user and required system principals. Failure to establish a private artifact directory fails preflight.
- Known-value redaction applies only to non-empty values of at least eight characters actually supplied under the adapter-maintained credential environment-name allowlist. It does not read saved-auth files to discover secrets and does not redact ordinary non-secret model, effort, runtime, ID, or lens values.
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
    "UNSUPPORTED_CHANGE", "DIFF_TOO_LARGE", "PACKET_TOO_LARGE",
    "AGENT_OUTPUT_TOO_LARGE", "MODEL_MISMATCH", "REVIEW_FAILED",
    "REPAIR_FAILED", "NO_QUORUM", "MODERATOR_FAILED",
    "PROCESS_CLEANUP_FAILED", "STATE_CORRUPT", "INTERNAL_ERROR",
]
```

- `FINALIZED` requires exactly one mode-appropriate product outcome and no failure kind.
- `FAILED` requires one failure kind and no product outcome.
- `TIMED_OUT` and `CANCELLED` have no product outcome. Their phase identifies where work stopped.
- `CREATED` and `RUNNING` are non-terminal.
- A valid `CONTESTED` council decision and a code result containing unresolved findings are completed product outcomes, not orchestration failures.

Every `FailureKind` has one normative trigger and terminal mapping. More specific rows take precedence over a generic phase failure:

| Failure kind | Normative trigger | Status | Exit |
|---|---|---|---:|
| `INVALID_INPUT` | CLI argument, task/prompt, or configuration syntax/schema/value is invalid before repository/provider preflight | `FAILED` | 2 |
| `PREFLIGHT_FAILED` | Executable, authentication, required capability, private-directory setup, stable identity for an otherwise supported repository, or other non-repository preflight check fails or its individual timeout expires | `FAILED` | 2 |
| `REPOSITORY_BUSY` | The canonical repository identity lock is held by another live code run | `FAILED` | 2 |
| `UNSUPPORTED_REPOSITORY` | The supplied path is not a non-bare Git working tree, or its initial structure/state is unsupported: dirty, sparse, submodule/gitlink, tracked filter/LFS, tracked `.dialectic-turn` entry, or otherwise outside the declared repository subset | `FAILED` | 2 |
| `DRIVER_FAILED` | Initial driver turn exits nonzero, reaches its individual timeout, has an invalid native/schema envelope, or lacks its required resumable session | `FAILED` | 3 |
| `NO_CHANGES` | Successful initial driver turn leaves no product change relative to `base_sha` | `FAILED` | 3 |
| `UNSUPPORTED_CHANGE` | A writable driver turn introduces a filter-matched path, index gitlink, binary change, invalid-UTF-8 path/diff, or other change the `ChangeValidator` cannot safely snapshot | `FAILED` | 3 |
| `DIFF_TOO_LARGE` | A validated staged diff against the original base exceeds `max_diff_bytes` | `FAILED` | 3 |
| `PACKET_TOO_LARGE` | A complete outbound model packet exceeds `max_packet_bytes` before any member of that phase launches | `FAILED` | 3 |
| `AGENT_OUTPUT_TOO_LARGE` | A native process exceeds its stdout/stderr bound, or its reserved turn scratch exceeds `max_turn_scratch_bytes` | `FAILED` | 3 |
| `MODEL_MISMATCH` | A native CLI reports a known, non-equivalent actual model | `FAILED` | 3 |
| `REVIEW_FAILED` | A required reviewer exits nonzero, reaches its individual timeout, fails envelope/report validation, authentication, or another reviewer-phase requirement not covered above | `FAILED` | 3 |
| `REPAIR_FAILED` | Repair exits nonzero, reaches its individual timeout, lacks/mismatches dispositions, has malformed output, or violates repair invariants not covered above | `FAILED` | 3 |
| `NO_QUORUM` | A required council participant fails an opening, cross-examination, or ballot turn, including individual timeout, missing session, or invalid participant artifact | `FAILED` | 3 |
| `MODERATOR_FAILED` | Moderator exits nonzero, reaches its individual timeout, or returns an invalid candidate | `FAILED` | 3 |
| `PROCESS_CLEANUP_FAILED` | Termination/reaping of the platform-owned process unit cannot be confirmed; this overrides the initiating failure, timeout, or cancellation | `FAILED` | 3 |
| `STATE_CORRUPT` | A controller-owned artifact required for continued execution is unreadable, schema-invalid, or violates a persisted-state invariant | `FAILED` | 3 |
| `INTERNAL_ERROR` | An unexpected controller defect or infrastructure error is not classified by another row | `FAILED` | 3 |

An overall workflow wall-clock expiry is not a `FailureKind`: it produces `TIMED_OUT` and exit 4. User cancellation produces `CANCELLED` and exit 130 after successful cleanup. Individual turn timeouts use the phase-specific failure row above. Failure detail MUST record the concrete trigger without credential values.

After Typer has identified a valid command/mode, the controller creates the explicit-null `CREATED` record before loading the user-named configuration and input files; subsequent input/config errors therefore persist as `INVALID_INPUT`. A parser-level CLI syntax error or invalid mode that occurs before a run ID exists returns exit 2 and creates no run record.

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

| From | Condition | To |
|---|---|---|
| `PREFLIGHT` | Success | `WORKTREE_SETUP` |
| `WORKTREE_SETUP` | Success | `DRIVER_INITIAL` |
| `DRIVER_INITIAL` | Successful driver response | `INITIAL_VALIDATION` |
| `INITIAL_VALIDATION` | Valid snapshot committed | `REVIEWERS` |
| `REVIEWERS` | All pass with no findings | `REPORTING` |
| `REVIEWERS` | At least one valid finding | `FEEDBACK` |
| `FEEDBACK` | Packet persisted | `DRIVER_REPAIR` |
| `DRIVER_REPAIR` | Valid repair response | `FINAL_VALIDATION` |
| `FINAL_VALIDATION` | Valid final state | `REPORTING` |

These are `CodePhase` values, not statuses. After the initial record is created, status becomes `RUNNING` and advances through the phases above; `REPORTING` ends in one terminal `RunStatus`. Any phase may instead end in a terminal failure, timeout, or cancellation status. There is no phase transition from `REPORTING` back to `REVIEWERS` in the MVP.

### 7.3 Detailed flow

#### CODE-01: Preflight

The controller MUST:

1. Validate the configuration grammar/schema, input byte bound, counts, limits, IDs, and consensus settings.
2. Resolve the repository to an absolute path.
3. Confirm that path is a non-bare Git working tree. A non-repository path or bare repository fails as `UNSUPPORTED_REPOSITORY` before common-directory identity work.
4. Resolve and record the Git common directory and its canonical diagnostic path.
5. Derive a stable filesystem identity for that directory: `(st_dev, st_ino)` after `realpath` on POSIX; `(volume_serial_number, file_index)` from an opened directory handle after final-path resolution on Windows. Fail as `PREFLIGHT_FAILED` if a stable identity cannot be obtained for the otherwise supported repository.
6. Create one controller-owned empty hooks directory under the private run directory.
7. Acquire the repository advisory lock or set status `FAILED` with failure kind `REPOSITORY_BUSY`, naming the holding run ID when available.
8. Confirm the original working tree has no staged, unstaged, or untracked non-ignored files.
9. Reject sparse checkout, any mode-`160000` entry from `git ls-files -s`, any tracked path for which `git check-attr filter` returns a value other than `unspecified`, and any tracked or on-disk entry at `.dialectic-turn`. This rejects submodules, Git LFS, custom clean/smudge-filtered content, and reserved-path collisions as `UNSUPPORTED_REPOSITORY` without deleting user content.
10. Record the original branch, original `HEAD`, base SHA, `main` SHA when present, and byte-for-byte `git status --porcelain=v1 -z` result.
11. Resolve and preflight the Codex driver and every distinct reviewer target as specified in section 5.4.
12. Fail without creating a branch or worktree if any check fails.

Preflight has its own timeout and does not consume the workflow wall clock.

The lock path is `<state-root>/locks/<sha256(platform-tag || stable-filesystem-identity)>.lock`; it is keyed by identity rather than path spelling. A user-private metadata sidecar contains the holding run ID and canonical diagnostic path. The controller holds the OS-backed lock for the complete code run and releases it on every normal or exceptional exit. A stale sidecar without an acquired OS lock does not block a new run and is replaced. Symlink, junction, drive-letter-case, and equivalent normalized path spellings that resolve to one directory MUST contend on the same lock.

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

The turn is successful only when the native process exits zero within its individual timeout, its envelope and structured response validate, its requested/actual model check passes, and it supplies a conforming resumable session ID. Otherwise the run fails as `DRIVER_FAILED`, except that the more specific `AGENT_OUTPUT_TOO_LARGE`, `MODEL_MISMATCH`, or `PROCESS_CLEANUP_FAILED` classification takes precedence. No reviewer starts after an unsuccessful initial driver turn.

#### CODE-04: Shared change validation and initial snapshot

After every writable driver turn, `ChangeValidator` MUST run the following algorithm before any controller commit. Initial and repair turns use the same implementation and policy:

1. Prove the reserved `.dialectic-turn/` directory is absent.
2. Incrementally enumerate staged, unstaged, and untracked non-ignored product paths using NUL-delimited Git output. Stop and terminate the Git subprocess on item `max_changed_paths + 1`; never accumulate the complete command output first. Decode every raw path as strict UTF-8; invalid UTF-8 fails as `UNSUPPORTED_CHANGE`. Untracked ignored files are excluded. The driver prompt states that it MUST NOT create build output or caches; a non-ignored generated artifact is an ordinary proposed change and remains subject to every check below.
3. Before staging, inspect every enumerated entry with no-follow filesystem operations. Deletions contribute zero bytes. Each present changed entry must be a supported regular file or symlink; a regular file's logical `st_size` must not exceed `max_changed_regular_file_bytes`, and the sum of logical sizes across present changed files/symlink payloads must not exceed `max_candidate_change_bytes`. Sparse files are measured by logical size. Path-count, type, individual-size, or aggregate-size failure is `UNSUPPORTED_CHANGE` and occurs before shared Git object creation.
4. Query `filter` for the complete bounded path set with `git check-attr -z --stdin filter`. Reject any value other than `unspecified` as `UNSUPPORTED_CHANGE`, before a clean or process filter can run.
5. Stage the complete proposed product state with the NUL-safe equivalent of `git add -A -- .`, using controller-owned Git configuration including disabled hooks, `core.autocrlf=false`, `core.fsmonitor=false`, and no pager. Because step 3 bounded every candidate file and their aggregate logical size, any unreachable Git objects left by a later rejected validation remain bounded; a quarantined object database is deferred.
6. Inspect `git ls-files --stage -z`. Reject every mode-`160000` entry as `UNSUPPORTED_CHANGE`, including a newly copied or initialized embedded repository that Git converted to a gitlink.
7. Inspect the staged state against the original `base_sha` with `git diff --cached --numstat -z --no-renames --no-ext-diff --no-textconv <base_sha> --`. Split each NUL-terminated record at only its first two tab separators, so a legal tab byte in the remaining raw path cannot alter the numeric columns. A `-` in either numeric column identifies a binary change and fails as `UNSUPPORTED_CHANGE`.
8. Generate the exact staged review bytes with behavior equivalent to:

   ```text
   git -c core.hooksPath=<empty> -c core.autocrlf=false \
       -c core.quotePath=false -c diff.external= -c diff.algorithm=histogram \
       -c diff.indentHeuristic=false -c color.ui=false \
       --no-pager diff --cached --no-color --no-ext-diff --no-textconv \
       --no-renames --full-index --src-prefix=a/ --dst-prefix=b/ --unified=3 \
       <base_sha> --
   ```

9. Run path enumeration, inspection, and diff generation with `LC_ALL=C`. Stream the generated diff into the hash/persistence sink and terminate it immediately after byte `max_diff_bytes + 1`; never accumulate a larger complete diff in memory. An overage fails as `DIFF_TOO_LARGE`. Otherwise decode the exact bytes as strict UTF-8, failing invalid data as `UNSUPPORTED_CHANGE`, and persist those same bytes plus SHA-256 before committing. The model-visible diff string MUST be the strict decoding of that persisted artifact; no normalization or replacement step is permitted.
10. For a repair turn, also stream, bound, and persist the exact staged delta against `review_sha` under the same policy. This delta, not model prose, determines whether the repair changed the reviewed tree.
11. Only after all checks pass, create the controller-owned commit when the relevant turn delta is non-empty. Immediately regenerate the committed tree diff against `base_sha` using the same pinned options and assert byte equality and hash equality with the validated staged artifact. Any mismatch is `INTERNAL_ERROR`.
12. Confirm the post-commit worktree/index is clean. A concurrent or unexplained change is `INTERNAL_ERROR`; Dialectic does not silently include it in a later phase.

Every controller-owned Git subprocess drains stdout and stderr incrementally. Diff output uses the exact bound above; other Git output is bounded by the smaller applicable structural limit and `max_candidate_change_bytes`, while stderr is bounded by `max_agent_stderr_bytes`. Overflow terminates and reaps Git before returning the specific validation or internal failure; no terminal record is written while a Git subprocess remains active.

For the initial turn, an empty staged diff against `base_sha` fails as `NO_CHANGES`. A successful non-empty initial validation produces and records `review_sha`, `initial.diff`, and its hash. Only then does the controller construct every reviewer packet; if any encoded packet exceeds `max_packet_bytes`, the run fails as `PACKET_TOO_LARGE` before any reviewer starts.

No AI agent may stage or commit. A validation failure leaves the proposed changes uncommitted in the isolated worktree, creates no review snapshot, and launches no reviewer. The final report identifies the rejected change class and preserved worktree without attempting to interpret unsafe content.

#### CODE-05: Blind parallel reviews

Every reviewer receives the same immutable packet core:

- Task and acceptance criteria.
- Base SHA.
- Review SHA.
- Complete bounded unified diff.
- The controller-owned review schema.

The controller then adds only that reviewer's configured lens. It records both the common-core hash and complete per-reviewer packet hash.

The controller does not supply reviewers with:

- The Codex implementation transcript or self-assessment.
- Other reviewer identities or outputs.
- Authentication or cost information.
- The target repository or worktree path.
- Writable repository access supplied by Dialectic.

The MVP uses **diff-only reviews**. Reviewer processes run with a neutral private temporary directory and the packet-only runtime policy from section 5.4.4. This is prompt/context isolation, not an OS sandbox guarantee. Dialectic does not claim semantic anonymity: a model may identify or speculate about itself in authored prose, and the controller preserves that content after ordinary redaction.

All reviewers MUST start concurrently. `@driver` MUST create a fresh Codex session and MUST NOT resume the driver session.

The review barrier is fail-fast. Once any required reviewer reaches a terminal failure, the controller cancels every still-active peer, terminates and reaps each owned process unit, retains already completed/partial artifacts, and only then persists `REVIEW_FAILED` or the more specific failure. No repair starts and no terminal run record is written while a reviewer process remains active.

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
- A report contains at most configured `max_findings_per_reviewer` findings.
- Finding IDs MUST be unique within a report.
- Finding IDs MUST be non-empty and no longer than 64 characters; a non-null line number MUST be at least 1.
- Every finding MUST contain a concrete claim and evidence; evidence may explain why the diff itself demonstrates the concern.
- A finding may name a file outside the supplied diff when it explains a cross-cutting impact; Dialectic passes it through and does not attempt repository-backed path validation.
- Malformed or schema-invalid output fails that reviewer. Only section 5.4.6's deterministic extraction is allowed; no model-powered format-repair retry is included.

#### CODE-07: Review barrier

All configured reviewers are required. If any reviewer:

- Times out,
- Exits unsuccessfully,
- Returns malformed output,
- Reports a mismatched SHA, or
- Reports a known model different from the requested model, or
- Cannot be authenticated,

the run becomes `FAILED` with failure kind `REVIEW_FAILED` or `MODEL_MISMATCH`. The driver repair turn is not invoked. Rate limits, quota exhaustion, and transient provider failures are failures in the MVP; completed turns are retained but not reused by an automatic retry.

After all reports validate, the controller also requires the aggregate finding count to be at most `max_total_findings`. An over-limit review set fails as `REVIEW_FAILED` before feedback construction; Dialectic neither truncates findings nor asks the driver to disposition an incomplete set.

#### CODE-08: Feedback packet

If every reviewer passes with no findings, the controller skips repair, sets `final_sha=review_sha`, reuses the verified initial diff/hash as the final diff/hash, enters `REPORTING`, and sets run status `FINALIZED` with code outcome `COMPLETED_NO_FINDINGS`.

If at least one finding exists, the controller creates one deterministic feedback packet:

- The controller labels reviewers Reviewer A, Reviewer B, and so forth and injects no runtime, provider, or model metadata.
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

Duplicate local IDs across reports therefore remain unambiguous. The repair prompt refers only to `finding_key`; the controller does not expose provider or model identity. Model-authored finding text is preserved, however, so a reviewer's own self-identification or stylistic clues may still appear. This is controller nondisclosure, not guaranteed semantic anonymity.

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

The repair turn is successful only when the exact recorded driver session resumes, the native process exits zero within its individual timeout, its envelope/report validates, and every disposition rule holds. Otherwise the run fails as `REPAIR_FAILED`, except that the more specific `AGENT_OUTPUT_TOO_LARGE`, `MODEL_MISMATCH`, or `PROCESS_CLEANUP_FAILED` classification takes precedence.

#### CODE-10: Finalization

After a valid repair response, the controller MUST:

1. Invoke the complete `ChangeValidator` algorithm from CODE-04 again, against the original `base_sha` and with `review_sha` as the repair-delta reference. Filter, gitlink, binary, UTF-8, complete-diff-size, hook, external-command, and exact-byte checks are identical to initial validation. A validation failure creates no repair commit, launches no re-review, and uses the validator's specific failure kind.
2. Use the validated repair delta against `review_sha` to decide whether the aggregate repair changed the reviewed tree. Set status `FAILED` with failure kind `REPAIR_FAILED` if any disposition says `fixed` but that delta is empty. The MVP does not attempt to prove a semantic one-to-one relationship between individual findings and hunks.
3. Permit an empty repair delta only when every disposition is `rejected_with_evidence` or `not_fixed`. In that case no second commit is created and `final_sha` equals `review_sha`.
4. When the validated repair delta is non-empty, retain the second controller-owned commit created by `ChangeValidator` and its verified `final_sha`.
5. Persist `final.diff` as the validator's exact bounded diff of `final_sha` against the original `base_sha`, plus its SHA-256 hash.
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

| From | Condition | To |
|---|---|---|
| `PREFLIGHT` | Success | `OPENING_POSITIONS` |
| `OPENING_POSITIONS` | Complete quorum with sessions | `CROSS_EXAMINATION` |
| `CROSS_EXAMINATION` | Complete quorum | `MODERATION` |
| `MODERATION` | Valid bounded candidate | `BALLOTS` |
| `BALLOTS` | Complete valid derived ballots | `REPORTING` |

These are `CouncilPhase` values, with `RunStatus` tracked independently as specified in section 6. After `REPORTING`, the run reaches exactly one terminal status. There is no MVP transition from `BALLOTS` to another discussion round.

### 8.3 Detailed flow

#### COUNCIL-01: Preflight

The controller MUST validate counts, unique IDs, consensus bounds, input byte limits, and all participant/moderator targets before any model is invoked. It then resolves executables, capabilities, versions, and authentication as specified in section 5.4. All configured participants are required for quorum in the MVP.

#### COUNCIL-02: Blind opening positions

Participants start concurrently in fresh sessions. Every participant receives exactly the same user prompt and opening-position schema. The controller supplies no participant with another participant's identity or response during this phase.

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

Every concurrent council phase uses the same fail-fast barrier: one required participant failure cancels and reaps all still-active peers before `NO_QUORUM` or another terminal result is persisted. Valid completed artifacts and bounded partial diagnostics remain available; later phases never start from a partial quorum.

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

Alias substitution is a controller-nondisclosure rule, not guaranteed semantic anonymity. Model-authored positions and revisions are preserved after ordinary redaction; if a model names or hints at its own provider/model identity, that content can appear in the ledger. Dialectic does not inspect or rewrite prose to conceal such clues.

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

Validation requires between one and configured `max_propositions` propositions, unique non-empty proposition IDs matching `[a-z][a-z0-9-]{0,31}`, non-empty statements and rationales, and `supporting_participants` containing only known participant aliases without duplicates. Unknown aliases or an over-limit proposition list invalidate the moderator artifact and fail the run with `MODERATOR_FAILED`.

Because a derived overall acceptance requires acceptance of every proposition, proposition count affects consensus difficulty. The controller-owned maximum deliberately bounds that moderator influence; it is not merely a payload-size limit.

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
    blocking_objection: bool
    blocking_objection_evidence: str | None
    minority_report: str | None

class DerivedBallot(BaseModel):
    artifact_schema_version: Literal[1]
    tool_version: str
    participant_alias: str
    ballot: CouncilBallot
    derived_overall_vote: Literal["accept", "reject", "abstain"]
```

Every candidate proposition MUST receive exactly one vote from every participant.

For each ballot:

- The proposition-ID set MUST equal the candidate-ID set exactly; duplicates, omissions, and unknown IDs are invalid.
- `blocking_objection=true` requires non-empty evidence.
- `blocking_objection=false` requires `blocking_objection_evidence=null`.
- The model does not submit an overall vote. The controller derives it: a blocking objection or any proposition rejection produces `reject`; otherwise any proposition abstention produces `abstain`; otherwise every proposition was accepted and the result is `accept`.
- The original ballot and controller-owned `DerivedBallot` MUST both be retained. Model prose is never reparsed to alter this derivation.
- Invalid ballots fail the participant phase as `NO_QUORUM`; Dialectic does not repair or reinterpret them.

#### COUNCIL-06: Deterministic outcome

Let `N` be the number of configured participants, `A` the number of controller-derived `accept` votes, and `B` indicate that any ballot contains a blocking objection. Configuration has already established `0 <= max_dissenters < N`.

Only after every required ballot validates, the controller evaluates these mutually exclusive rules in order:

1. `UNANIMOUS` when `A == N`. The derived-vote rule already makes this impossible when `B` is true.
2. `ROUGH_CONSENSUS` when `A >= 1`, `A >= N - max_dissenters`, and `B` is false.
3. `CONTESTED` otherwise.

Participant failure or invalid output produces run status `FAILED` with failure kind `NO_QUORUM`; moderator failure produces `MODERATOR_FAILED`; overall expiry produces `TIMED_OUT`. None of these is a `ConsensusOutcome`.

For the normal three-participant configuration with `max_dissenters: 1`, two accept votes can produce rough consensus only when nobody raises a blocking objection.

Per-proposition votes and blocking objections determine each participant's derived overall vote as specified in COUNCIL-05. Consensus arithmetic uses only those controller-derived votes and blocking-objection flags. Semantically, a participant accepts the candidate only when it accepts every proposition and raises no blocker. Confidence values MUST NOT affect the vote calculation. The supervisor MUST NOT claim that consensus proves factual correctness.

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
- Every native invocation MUST be owned as one platform process unit. On POSIX, the MVP unit is the newly created session/process group: the controller signals that group and, after normal root exit as well as timeout/cancellation/failure, terminates and reaps any lingering members before validation or terminal persistence. This contains ordinary descendants but is not a cgroup or security boundary; a process that deliberately calls `setsid()` can escape the group. Deliberate daemonization is outside the trusted-local MVP threat model and the specification does not claim complete POSIX descendant containment.
- On Windows 11, `ProcessSupervisor` MUST use this race-free sequence: create and configure a non-inheritable Job Object with `JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE` and no breakaway permission; create explicitly selected standard-stream pipe handles; place the job in `PROC_THREAD_ATTRIBUTE_JOB_LIST` and child pipe ends in `PROC_THREAD_ATTRIBUTE_HANDLE_LIST`; then call `CreateProcessW` with `EXTENDED_STARTUPINFO_PRESENT | CREATE_SUSPENDED | CREATE_UNICODE_ENVIRONMENT`. The operating system therefore assigns the root to the job as part of process creation, before user code can run. The controller verifies membership and only then calls `ResumeThread`. No handle other than the explicit child ends of the standard streams may be inherited.
- If attribute-list construction, process creation, membership verification, or resume fails, the controller MUST terminate the job/root without allowing the target entry point to run when it is still suspended, then close every process, thread, job, pipe, and attribute-list resource. Dialectic does not fall back to create-then-assign on Windows 11 because that would reintroduce a controller-crash gap. A versioned preflight probe MUST confirm creation-time assignment under the host's current parent-job/nested-job conditions before model work begins.
- On timeout, cancellation, or fail-fast peer failure, the controller requests graceful termination of the owned process unit, waits at most `graceful_kill_seconds`, force-terminates the unit, and awaits confirmed cleanup before recording a terminal state.
- On Windows, forced termination uses `TerminateJobObject`; normal root-process completion closes the kill-on-close job only after collecting process diagnostics, thereby terminating any lingering descendants. Every success and failure path closes the primary process/thread handles, all pipe handles, and the job handle exactly once. Because the job handle is not inherited, an abrupt controller-process exit also closes it and invokes kill-on-close. A root process cannot spawn outside controller ownership before assignment because it never executes while unassigned.
- If tree cleanup cannot be confirmed, the run becomes `FAILED` with `PROCESS_CLEANUP_FAILED`; the trigger and surviving-process diagnostics are retained without credentials.
- Ctrl+C initiates the same tree-cleanup path and normally records `CANCELLED`.
- Terminal precedence for competing events is: process-cleanup failure, explicit cancellation, overall timeout, individual-turn timeout/phase failure. Individual-turn timeouts use the phase-specific mapping in section 6.3; only the overall wall clock produces status `TIMED_OUT`.
- Partial artifacts MUST remain available.
- The supervisor performs no automatic provider retry in the MVP.
- Rate limits, quota exhaustion, transient transport errors, and malformed provider output therefore fail the affected required turn. The report MUST make clear that this is deliberate fail-closed MVP behavior.
- Failure messages MUST name the phase and configured target but MUST NOT expose credentials or complete environment contents.
- A failed code run never merges, pushes, or copies partial code into the original working tree.

## 10. Security and safety requirements

- Only the Codex driver receives the isolated writable worktree path.
- Dialectic supplies reviewers only a diff packet and neutral temporary CWD, and supplies council agents only the user prompt plus controller-produced discussion artifacts.
- Packet-only adapters use the customization/tool restrictions in section 5.4.4 and record their effective flags. Dialectic does not claim that CWD or prompt isolation prevents a trusted native process, managed policy, inherited environment, or compromised executable from discovering other locally readable data.
- Model output is data. It MUST NOT be executed as a shell command by the supervisor.
- Target-repository files MUST NOT be treated as supervisor configuration unless explicitly named by the user as the configuration file.
- The Codex driver intentionally receives normal repository context. Packet-only agents do not receive project instructions from Dialectic. Native user or managed configuration is disabled where current supported flags allow it; any residual configuration risk is recorded rather than hidden.
- Every controller-owned Git command uses per-invocation `core.hooksPath=<empty>`, `core.fsmonitor=false`, and `core.pager=cat`. Commits additionally use `commit.gpgSign=false`; diff generation disables external diff and text-conversion commands. Global, system, and repository Git configuration are not modified.
- Every writable driver turn passes through the shared `ChangeValidator` in CODE-04 before a commit. It rejects filtered paths before staging, then rejects index gitlinks, binary content, invalid UTF-8, or an over-limit complete diff before reviewers or finalization.
- Git commands use argument arrays and a controller-local commit identity.
- There is no automatic cleanup because preserving the branch/worktree is safer and more auditable for the MVP.
- The final terminal output MUST give the isolated worktree and branch, state exactly which original repository properties remained unchanged, and state that shared Git metadata and objects were added.
- The final output and README MUST provide explicit cleanup commands using the recorded paths: `git worktree remove <path>`, `git branch -D dialectic/<run-id>`, and `git worktree prune`. Dialectic does not run them automatically.
- `dial status` MUST print the run-artifact directory. The README MUST identify its platform-specific parent, state that failed/cancelled runs are deliberately retained and sensitive, and explain that a user may remove a terminal run directory manually only after confirming no Dialectic process owns it. No cleanup command is added to the MVP.

### 10.1 Future inbound-interface invariants

This subsection deliberately adds no v0.1.0 command, dependency, transport, or test. It preserves the authority model for an MCP server or another non-CLI ingress added after the native alpha or beta:

- A conversational host may request a run and render its bounded result; only `DialecticService` and the controller may select participants, construct packets, schedule concurrency, continue sessions, operate Git, enforce schemas/timeouts, calculate consensus, or persist evidence.
- The first proof surface SHOULD be read-only profile/run/result inspection, followed by Council Once launch, and only later guarded Code Once launch.
- A non-human caller MUST select a controller-registered `profile_id` and, for code, a controller-registered `repo_id` resolved by the same stable filesystem identity used by `RepositoryLock`. It MUST NOT supply repository/configuration/executable/artifact paths, provider credentials, reviewer lists, model overrides, lenses, packet fragments, ballots, or consensus overrides.
- Code Once launch requires authorization established outside model-authored arguments; an `authorized: true` field is never evidence of approval. Read-only, council-launch, and writable-code permissions remain separable.
- Start requests use a caller-scoped `client_request_id`. Repeating an accepted key with identical normalized arguments returns the same `run_id`; reusing it with different arguments fails. Server-side call, concurrency, byte, time, and cost ceilings remain authoritative.
- Result operations return closed enums and a bounded typed `SummaryRecord` projection or approved opaque artifact handles—never raw diffs, native streams, complete transcripts, arbitrary paths, or credentials. Every model-authored field is labeled untrusted data because it may carry instructions originating in adversarial repository text.
- The ingress exposes no general shell operation, artifact deletion, cleanup, or participant-to-participant call. It never forwards the host's MCP servers, apps, tools, environment, or conversational transcript into a run.
- A future asynchronous start cannot return before execution has a specified durable job owner, disconnect/cancellation behavior, duplicate-launch protection, crash detection, recovery, and finalization contract. A thin adapter MUST NOT hide a daemon or make the MCP client's lifetime an undocumented process-ownership boundary.
- Outbound MCP remains disabled for Code Once and Council Once. A future controller-issued capability is permissible only in a separately named workflow/profile with per-run, per-role allowlists, evidence capture, and byte/call/time/network/cost bounds; inheriting a user's general MCP configuration remains forbidden.

These requirements distinguish interface from engine: an MCP host may initiate the protocol, but it cannot curate or adjudicate the supposedly independent process.

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
| CORE-006 | Known allowlisted secret of at least eight characters appears, then a supplied credential shorter than eight is configured | Persisted artifact contains a redaction marker and ordinary short words remain intact; short credential fails preflight |
| CORE-007 | Agent timeout with delayed grandchild in the same POSIX session/process group or Windows Job | The complete owned process unit is reaped and the sentinel is never written; no claim is made about a deliberate POSIX `setsid()` escape |
| CORE-008 | Ctrl+C with concurrent process units and delayed grandchildren | Every owned unit is reaped and run becomes `CANCELLED` |
| CORE-009 | Two delayed parallel invocations | Recorded invocation intervals overlap; no wall-clock ratio assertion is used |
| CORE-010 | CLI reports a documented alias resolution, then a known non-equivalent model | Alias case is accepted and canonicalized; non-equivalent case fails as `MODEL_MISMATCH`; all names are recorded |
| CORE-011 | Invoke matching fixture through `dial` and `dialectic` | After normalizing run IDs, timestamps, and paths: artifact tree shape, summary, status, and exit code are identical |
| CORE-012 | Resolve executable available only as a safe Windows `.cmd` shim, then an unsafe-name/argument variant | Safe shim yields a recorded constrained batch launch plan rooted at suspended system `cmd.exe`; unsafe variant fails before invocation |
| CORE-013 | Deliver a 200 KiB prompt containing Windows/POSIX metacharacters | Prompt travels by stdin/ACP; no argv element exceeds 4096 bytes; nothing is shell-expanded |
| CORE-014 | Invalid or traversal-shaped run ID passed to `dial status` | Rejected before path joining with exit code 2 |
| CORE-015 | Controller artifact lacks or mismatches `artifact_schema_version` | Artifact validation fails explicitly |
| CORE-016 | Count, ID, model-selector, lens, limit, or timeout boundary violated | Configuration fails with the exact field and bound |
| CORE-017 | Private run-directory permissions cannot be established | Preflight fails without launching a model |
| CORE-018 | Output is whole JSON, one `json` fence, zero fences, two fences, duplicate keys, non-finite numbers, a lone surrogate, excessive nesting, or a coercible wrong type | Only the two valid strict forms parse; every ambiguous/non-scalar/coercive form fails and bounded raw output is retained |
| CORE-019 | Tree termination cannot be confirmed after timeout or cancellation | Status is `FAILED`, failure kind is `PROCESS_CLEANUP_FAILED`, and trigger is retained |
| CORE-020 | CLI does not report an actual model | `actual_model=null` is recorded and the turn is not failed solely for absence |
| CORE-021 | Well-formed but unknown run ID passed to `dial status` | Clear not-found message and exit code 2 |
| CORE-022 | `dial status` reads valid `RUNNING`, `FINALIZED`, and `FAILED` records | Each record is displayed faithfully and lookup exits 0 |
| CORE-023 | `dial status` reads truncated, malformed, or schema-invalid `run.json` | Reports corrupt state without guessing and exits 3 |
| CORE-024 | YAML contains a tag, anchor, alias, merge key, duplicate key, partial `${NAME}` interpolation, missing/empty variable, or `$${NAME}` | Unsafe/ambiguous YAML and invalid expansion fail; escaped variable becomes literal; unrelated dollars remain literal |
| CORE-025 | Serialize every status/phase combination and a just-created partial run | Only mode-appropriate typed phases validate; all nullable declared fields are present as explicit `null`; status/outcome/failure invariants hold |
| CORE-026 | Trigger every declared `FailureKind` plus overall timeout and cancellation in a table-driven fixture | Each produces exactly the status and exit code in section 6.3; no enum member lacks a trigger test |
| CORE-027 | Agent emits infinite stdout, infinite stderr, sampled/final oversized scratch, and a credential split across capture chunks/overflow boundary | Owned unit is killed as `AGENT_OUTPUT_TOO_LARGE`; persisted redacted prefix plus marker stays within cap; final scratch check is authoritative and sampling is reported only as detection |
| CORE-028 | Windows launcher emits concurrent stdout/stderr while attempting an immediate descendant/sentinel; also force stream overflow, creation-time Job-list failure, and nested-parent-job execution | Root is born in the kill-on-close job before entry-point execution; bounded reader threads drain both pipes; overflow kills the job; all threads/handles/resources close; supported nested-job probe passes or preflight fails closed |
| CORE-029 | Configuration/task/prompt uses valid UTF-8 supplementary characters, UTF-8 BOM, UTF-16, invalid UTF-8, or a surrogate-bearing parsed value | Only scalar-value UTF-8 without BOM is accepted; every other form fails as `INVALID_INPUT` before model/repository work |
| CORE-030 | Capability attestation is absent, valid, stale by CLI/profile/fixture/platform identity, corrupt, or affected by a dynamic-path/managed-policy mismatch | Missing/stale/corrupt entries re-probe under the separate budget; only an exact valid attestation plus current dynamic checks may skip the cost-bearing probe |

### 11.4 Code Once tests

| ID | Test | Expected result |
|---|---|---|
| CODE-001 | Happy path: two reviewers return findings | One driver start, two parallel fresh reviews, one driver resume, two commits at most, then stop |
| CODE-002 | All reviewers pass | No repair call; status `FINALIZED`, outcome `COMPLETED_NO_FINDINGS` |
| CODE-003 | `@driver` reviewer | Same target/model as driver, operation `start` rather than `resume`, and a different session ID |
| CODE-004 | Reviewer concurrency | All reviewer start timestamps precede first reviewer completion |
| CODE-005 | Immutable review core | Every reviewer receives identical task, base SHA, review SHA, and diff hash; only lens and packet hash differ |
| CODE-006 | Driver transcript and worktree-path sentinels | Both are absent from reviewer prompt, argv, and packet artifact |
| CODE-007 | One reviewer fails while another remains long-running | Remaining peers are cancelled and reaped before status `FAILED`/`REVIEW_FAILED`; partial artifacts remain and driver resume count is zero |
| CODE-008 | One reviewer returns invalid JSON/schema, exceeds its finding limit, or valid reports collectively exceed the aggregate finding limit | Status `FAILED`, kind `REVIEW_FAILED`; raw/valid reports retained; no truncation or repair |
| CODE-009 | Reviewer returns mismatched SHA | Report rejected and run fails closed |
| CODE-010 | Driver repair feedback without reviewer self-identification | Every normalized finding key appears and the controller injects no provider/model identity into the repair packet |
| CODE-011 | Driver omits, duplicates, or invents a disposition key | Report rejected; status `FAILED`, kind `REPAIR_FAILED` |
| CODE-012 | Driver fixes findings and edits worktree | New changes committed; outcome `COMPLETED_AFTER_REPAIR` |
| CODE-013 | Driver rebuts every finding without edits | No second commit; outcome `COMPLETED_WITH_REBUTTALS` |
| CODE-014 | Driver leaves any finding `not_fixed` | Outcome `COMPLETED_WITH_UNRESOLVED_FINDINGS`; summary highlights every unresolved key |
| CODE-015 | Driver produces no initial changes | Status `FAILED`, kind `NO_CHANGES`; no reviewers run |
| CODE-016 | Streamed staged diff reaches `max_diff_bytes + 1` | Diff subprocess stops without accumulating the remainder; status `FAILED`, kind `DIFF_TOO_LARGE`; changes remain uncommitted and no reviewer runs |
| CODE-017 | Original repository is dirty | Preflight fails before worktree creation |
| CODE-018 | Full happy-path Git integration | Original files, index, branch, `HEAD`, pre-existing refs, `main`, and status bytes match baseline; Dialectic branch contains final code |
| CODE-019 | Exact call-count guard | No second review call exists after repair |
| CODE-020 | Failure after driver changes | Partial isolated worktree is preserved and reported |
| CODE-021 | Two reviewers both use local finding ID `F1` | Feedback assigns distinct `reviewer-a/001` and `reviewer-b/001` keys; both require dispositions |
| CODE-022 | Mixed `fixed`, rebutted, and `not_fixed` dispositions | Outcome is `COMPLETED_WITH_UNRESOLVED_FINDINGS`; rebuttal and edits remain visible |
| CODE-023 | Driver claims any finding `fixed` but makes no aggregate repair change | Status `FAILED`, kind `REPAIR_FAILED` |
| CODE-024 | Driver adds or modifies a binary file | Status `FAILED`, kind `UNSUPPORTED_CHANGE`; changes remain uncommitted and no reviewer runs |
| CODE-025 | Repository uses sparse checkout, tracked submodule, Git LFS path, or tracked clean/smudge filter | Preflight rejects the repository before worktree creation |
| CODE-026 | Repository contains checkout/commit hook sentinels | Worktree creation and both controller commits execute no repository hook; global/repository config is unchanged |
| CODE-027 | External-diff, textconv, fsmonitor, or commit-signing sentinels are configured | Review diff is deterministic and no external command executes |
| CODE-028 | Initial Codex turn exits nonzero, reaches its individual timeout, returns a malformed envelope/schema, or lacks a resumable session ID | Status `FAILED`, kind `DRIVER_FAILED`; no reviewer runs; more-specific overflow/model/cleanup errors retain their own kind |
| CODE-029 | Second code run targets a repository whose lock is held | It fails as `REPOSITORY_BUSY` and names the holding run; first run is unaffected |
| CODE-030 | Code workflow wall clock expires during concurrent reviews | All owned process units are reaped; status `TIMED_OUT`; no repair starts |
| CODE-031 | Diff fits `max_diff_bytes` but one lens makes its packet exceed `max_packet_bytes` | Status `FAILED`, kind `PACKET_TOO_LARGE`; no reviewer starts |
| CODE-032 | Fresh linked worktree lacks an ignored environment sentinel | Driver prompt warns about absent ignored artifacts and does not ask it to repair environment setup |
| CODE-033 | Reviewer finding names a path outside the diff | Structurally valid report is accepted and finding passes through unchanged |
| CODE-034 | Driver adds a path newly matched by a clean/process filter | Run fails as `UNSUPPORTED_CHANGE` before staging; filter sentinel never executes and no snapshot commit exists |
| CODE-035 | Repair introduces, parametrically, a binary, gitlink, filtered path, invalid-UTF-8 path/content, or complete diff over the bound | Full shared validation reruns; status is `UNSUPPORTED_CHANGE` or `DIFF_TOO_LARGE`; no repair commit or re-review occurs |
| CODE-036 | Initial or repair turn copies/initializes an embedded Git repository | Post-staging mode-`160000` inspection rejects the new gitlink before any snapshot/final commit |
| CODE-037 | Change uses a valid Unicode/tab-containing filename and UTF-8 text, then invalid Latin-1 path/content variants | Valid case retains readable UTF-8 and exact diff hash with NUL-safe parsing; invalid cases fail `UNSUPPORTED_CHANGE` |
| CODE-038 | Driver creates ignored `__pycache__`/bytecode plus a valid non-ignored source edit | Ignored untracked artifacts are excluded; only the source edit is staged, hashed, and committed |
| CODE-039 | Offline Codex construction fixture requires one credential name plus declared non-secret names | Trusted CLI environment contains exactly the declared names; generated-child environment/profile excludes the credential and saved-auth/state paths; values never enter audit artifacts |
| CODE-040 | Offline Codex profile fixture constructs a driver turn | One named profile—never `--sandbox`/`sandbox_mode`—contains the exact product/Git/control/tmp/auth/state/network matrix; managed displacement fails preflight; scratch is removed before validation; `.codex` is ignored while `AGENTS.md` discovery remains configured |
| CODE-041 | Concurrent runs address one repository through POSIX symlink spellings or Windows junction/drive-case spellings | Stable filesystem identity produces one lock key; second run fails `REPOSITORY_BUSY`; identity failure fails preflight |
| CODE-042 | Reviewer output explicitly names its own provider/model | Controller injects no identity metadata, but authored self-identification survives unchanged in normalized feedback; audit alias map remains correct |
| CODE-043 | Valid text state is staged, hashed, committed, and regenerated; a fixture mutates state between validation and equality confirmation | Normal case has byte-identical staged/committed diff and hashes; race/mismatch fails `INTERNAL_ERROR` and no reviewer/finalization follows |
| CODE-044 | Driver replaces control output or scratch children with a symlink, hard link, FIFO, socket/device, POSIX rename race, or Windows junction/reparse point targeting auth/state/original-repository content | Ingestion/cleanup rejects the object or identity race without following, reading, overwriting, hanging on, or deleting the outside target; status is `INTERNAL_ERROR` and no Git validation starts |
| CODE-045 | Driver creates an over-limit regular/sparse file, aggregate candidate bytes, or empty-file storm | No-follow pre-stage inspection rejects at the first configured bound as `UNSUPPORTED_CHANGE`; `git add` and diff generation never start and no shared Git blob is created for the rejected content |
| CODE-046 | Repository path is not Git, is bare, or contains a tracked/on-disk `.dialectic-turn` file, directory, symlink, or junction | Fails deterministically as `UNSUPPORTED_REPOSITORY` before worktree/model work and never deletes the colliding user entry |

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
| COUNCIL-010 | Three participants, `max_dissenters=0`, two accept and one abstains | Status `FINALIZED`, outcome `CONTESTED` |
| COUNCIL-011 | Participant fails during opening/cross-exam/ballot while a peer remains active | Active peers are cancelled and reaped; status `FAILED`, kind `NO_QUORUM`; partial artifacts retained and no later phase starts |
| COUNCIL-012 | Moderator fails | Status `FAILED`, kind `MODERATOR_FAILED`; no ballots run |
| COUNCIL-013 | Overall wall clock expires | Status `TIMED_OUT`; all active owned process units reaped |
| COUNCIL-014 | Exact round-count guard | No participant receives a second cross-examination prompt after ballots |
| COUNCIL-015 | User-facing report | Contains answer, vote matrix, dissent, blockers, unresolved questions, and actual identities |
| COUNCIL-016 | `max_dissenters` is negative | Configuration is rejected |
| COUNCIL-017 | `max_dissenters >= N` | Configuration is rejected with both values named |
| COUNCIL-018 | Every participant rejects | Outcome is `CONTESTED`, never consensus |
| COUNCIL-019 | Every participant accepts and rough threshold also passes | Ordered evaluation returns only `UNANIMOUS` |
| COUNCIL-020 | Candidate has zero propositions, duplicate IDs, empty IDs, or unknown supporting alias | Moderator artifact fails as `MODERATOR_FAILED` |
| COUNCIL-021 | Ballot duplicates, omits, or invents a proposition ID | Participant phase fails as `NO_QUORUM` |
| COUNCIL-022 | Blocking flag and evidence are inconsistent | Ballot is rejected as invalid |
| COUNCIL-023 | Ballots contain all-accept, one-abstain, one-reject, and blocker combinations | Models submit no overall field; controller derives `accept`, `abstain`, `reject`, and `reject` respectively and persists `DerivedBallot` |
| COUNCIL-024 | Opening participant lacks a resumable session ID | Immediate `NO_QUORUM`; no cross-examination or moderator call |
| COUNCIL-025 | Cross-examination ledger delivered to Participant B | It contains all positions including B's, identifies B's own alias, and contains no runtime/model map |
| COUNCIL-026 | Participant count outside 2..5 or participant IDs duplicate/invalid | Configuration fails before model invocation |
| COUNCIL-027 | Council wall clock expires with several active agents | Every participant/moderator process unit is reaped before `TIMED_OUT` is persisted |
| COUNCIL-028 | Confidence is outside 0.0..1.0 | Opening position is schema-invalid and run fails as `NO_QUORUM` |
| COUNCIL-029 | Cross-examination or ballot packet exceeds `max_packet_bytes` | Status `FAILED`, kind `PACKET_TOO_LARGE`; no participant in that phase starts |
| COUNCIL-030 | Moderator returns zero, exactly configured maximum, or one over `max_propositions` | Zero and over-limit candidates fail `MODERATOR_FAILED`; bounded candidate proceeds |
| COUNCIL-031 | One opening/revision self-identifies its provider/model | Controller adds only aliases; authored self-identification remains unchanged in later ledgers, proving nondisclosure rather than semantic anonymity |
| COUNCIL-032 | Three participants, `max_dissenters=1`, two accept and one abstains with no blocker | Status `FINALIZED`, outcome `ROUGH_CONSENSUS` |

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
- Timeout and platform-owned process-unit cleanup, with the explicit POSIX process-group scope from section 9.
- Provider-reported actual model and usage when present.
- Prompts containing spaces, quotes, newlines, Unicode, `$()`, backticks, `&`, `|`, `^`, `%`, `<`, and `>` without shell or `.cmd` reinterpretation.
- Unsafe model selectors or native session IDs are rejected before invocation; all CLI file arguments are safe relative controller names.
- The exact neutral CWD and session-continuation shape used by packet-only roles.
- Incremental stdout/stderr limits on both POSIX and Windows reader paths, strict UTF-8/JSON grammar, field/list bounds, and reserved control/tmp enforcement.
- Exact native CLI environment construction from the empty conceptual baseline using only fixture-declared credential and complete required non-secret name sets; persisted audit data contains names/policy but no credential values.

Offline Codex driver fixtures MUST cover stdin prompt `-`, ignored user configuration/rules, retained `AGENTS.md` discovery, explicit child `shell_environment_policy`, saved-auth/path denials, the exclusive named no-network permission profile, never-ask behavior, the exact read-only Git/write-denial matrix, protected control/tmp split, and absence of any full-access or older-sandbox bypass. These tests prove what Dialectic constructs; they do not claim that a fake process proves native Codex enforcement. Codex packet fixtures MUST cover stdin prompt `-`, `--skip-git-repo-check`, `--ignore-user-config`, `--ignore-rules`, and the exclusive named read-only profile. Claude fixtures MUST cover piped stdin, `--safe-mode`, disabled tools/MCP, schema output, and resume. Grok fixtures MUST cover ACP initialization with empty capabilities, zero effective MCP/config-compatibility sources, session creation, repeated `session/prompt`, and process teardown.

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

Pinned-native Codex boundary verification:

- `LIVE-CODE-001` supplies authentication only to the trusted Codex CLI and asks a model-generated child to probe that environment name and a saved-auth path; the child observes neither while the CLI authenticates successfully.
- `LIVE-CODE-002` proves product/`tmp/` writes and read-only Git inspection succeed while control/Git-metadata writes and original-worktree/state/auth/pre-redirect-temp/outside-workspace/network/permission-expansion operations fail without prompting; project `.codex` configuration remains ignored and `AGENTS.md` remains discoverable.
- These opt-in, platform-gated tests are required as manual release evidence for each pinned Codex fixture/platform combination but are excluded from the mandatory offline CI count. A fake adapter can never satisfy their claims.

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
- CLI handlers contain no workflow logic; Code Once, Council Once, status, and result behavior are reached through the typed `DialecticService` boundary.
- No test or implementation path uses shell interpolation.
- Large prompts travel over stdin or ACP, never argv.
- Timeouts, cancellation, and fail-fast barriers reap the complete platform-owned unit: the Windows Job Object or the POSIX session/process group. Deliberate POSIX `setsid()` escape remains an explicit trusted-local limitation rather than a claimed guarantee.
- Windows model entry points remain suspended until their kill-on-close Job Object owns them; assignment-failure and handle-cleanup tests pass.
- Controller-owned Git operations execute no repository hook or external diff/text-conversion command.
- The Codex driver runs under the exclusive named no-network permission profile and exact access matrix; older sandbox settings cannot displace it, Git metadata is not writable, and permission expansion is impossible without failed preflight.
- Initial and repair turns both pass through one `ChangeValidator`; path/file/aggregate candidate bounds are enforced before staging, controller Git invokes no clean/process filter, and unsupported/binary/gitlink/invalid-UTF-8/over-limit output cannot reach a commit or re-review.
- Offline fixtures prove the exact credential/environment/profile construction, and pinned-native manual release tests prove that authentication reaches the trusted Codex CLI while its model-generated child cannot observe the credential or saved-auth path.
- Injected credential values are absent from persisted fixtures and run artifacts. This requirement does not claim discovery or removal of arbitrary secrets already present in repository/task/model content.
- Exact call-count tests prove neither workflow loops.
- All 108 mandatory core, code, and council cases pass on their applicable release platforms: 30 core, 46 Code Once, and 32 Council Once.
- The stable run-status/outcome schemas and exit-code table are implemented exactly.
- A short README documents installation, native CLI prerequisites, trusted-local-process boundary, POSIX process-group limitation, configuration, commands, artifact/run-directory locations, cleanup, cost/quota warning, ignored build-output expectation, and MVP limitations.

## 13. Agile implementation slices

### Slice 0: Skeleton and contracts

Deliver:

- Python package and CLI skeleton.
- Typed `DialecticService` application boundary called by the CLI.
- Pydantic configuration and artifact schemas.
- `AgentAdapter` protocol and scripted adapter.
- Atomic `RunStore` and redaction.
- Canonical run status, outcome, failure-kind, run-ID, artifact, and exit-code contracts.
- Cross-platform process-unit/repository-lock abstractions, including bounded Windows reader threads, with fake-process tests.
- Core unit tests.

Exit criterion: CORE-001 through CORE-030 pass without native agents, except the platform-gated Windows launcher/reader contract in CORE-028.

### Slice 1: Offline Code Once vertical slice

Deliver:

- Git preflight and isolated worktree.
- Scripted Codex driver.
- Parallel scripted reviewers.
- Feedback construction.
- One scripted driver resume.
- Final commits and summary.

Exit criterion: CODE-001 through CODE-046 pass with no native AI CLI installed. CODE-039 and CODE-040 prove construction only; native enforcement belongs to Slice 2.

### Slice 2: Native agent adapters

Deliver:

- Codex adapter supporting start, fresh review, structured output, and resume.
- Claude adapter supporting fresh review/council turn, structured output, and resume.
- Grok ACP adapter supporting fresh review/council turns, deterministic JSON extraction/validation, and same-session continuation.
- Version-labeled adapter fixtures and preflight diagnostics.
- Opt-in live code smoke test.
- Pinned-native `LIVE-CODE-001` and `LIVE-CODE-002` permission/credential evidence on applicable release platforms.

Exit criterion: an explicitly invoked manual smoke run proves that a simple Codex task can flow through the available real reviewers and stop correctly, and `LIVE-CODE-001`/`002` supply pinned-native release evidence on each applicable platform; recorded offline adapter fixtures remain the ordinary CI gate.

### Slice 3: Offline Council Once vertical slice

Deliver:

- Blind opening fan-out.
- Alias/position ledger.
- One session-resumed cross-examination round.
- Fresh moderator candidate.
- Final ballots and deterministic consensus.
- Council report.

Exit criterion: COUNCIL-001 through COUNCIL-032 pass offline.

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

MCP is intentionally not one of these first increments. After the native CLI has completed an alpha or beta soak and execution recovery/ownership is understood, add it in three separately releasable steps: (1) read-only profile/run/result operations, (2) authorized idempotent Council Once launch, and (3) authorized idempotent Code Once launch restricted to registered repository identities. Each remains a thin ingress over `DialecticService`; none may duplicate controller logic. Gemini or another provider is independently added through `AgentAdapter` and does not require MCP.

## 15. Provider adapter references

The adapter contract is based on current primary documentation and MUST be rechecked when a fixture's CLI version changes:

- [Codex non-interactive mode](https://developers.openai.com/codex/non-interactive-mode), [developer commands](https://developers.openai.com/codex/developer-commands), [configuration reference](https://developers.openai.com/codex/config-reference), [advanced configuration](https://developers.openai.com/codex/config-advanced), [permissions](https://developers.openai.com/codex/permissions), and [sandbox/approval security](https://developers.openai.com/codex/agent-approvals-security): stdin prompt `-`, JSONL, structured output, resume, repository-check/configuration isolation, named permission profiles, child shell-environment policy, non-interactive workspace editing, temporary-directory exclusions, and protected Git metadata.
- [Claude Code CLI reference](https://code.claude.com/docs/en/cli-reference) and [headless mode](https://code.claude.com/docs/en/headless): piped input, JSON Schema output, session resume, safe mode, tool/MCP restrictions, and stdin size.
- [Grok Build headless/ACP documentation](https://docs.x.ai/build/cli/headless-scripting) and [CLI reference](https://docs.x.ai/build/cli/reference): ACP over stdio, sessions, JSON output, safety controls, and update suppression.
- Microsoft [CreateProcessW](https://learn.microsoft.com/en-us/windows/win32/api/processthreadsapi/nf-processthreadsapi-createprocessw), [UpdateProcThreadAttribute](https://learn.microsoft.com/en-us/windows/win32/api/processthreadsapi/nf-processthreadsapi-updateprocthreadattribute), [AssignProcessToJobObject](https://learn.microsoft.com/en-us/windows/win32/api/jobapi2/nf-jobapi2-assignprocesstojobobject), and [Job Objects](https://learn.microsoft.com/en-us/windows/win32/procthread/job-objects): `PROC_THREAD_ATTRIBUTE_JOB_LIST`/handle lists, suspended creation, pre-execution job assignment, process-tree membership, kill-on-close, handle lifecycle, and supported nested-job behavior.

Future-interface references, with no v0.1.0 dependency:

- [Grok Build MCP servers](https://docs.x.ai/build/features/mcp-servers): Grok can load local/remote MCP servers plus compatibility configuration from other clients, which is why Dialectic participant fixtures must prove an empty effective MCP set; the same client capability could later call a Dialectic MCP server.
- [Gemini MCP and coding-agent guidance](https://ai.google.dev/gemini-api/docs/coding-agents): Gemini CLI can consume MCP servers, so a future Gemini-facing ingress remains compatible with the northbound design; Gemini participation itself remains a separate `AgentAdapter` addition.

## 16. Revision 0.2 reconciliation summary

Revision 0.2 incorporates the blocking and correctness findings from the independent Codex/Sol and Claude/Opus reviews. In particular it adds safe Windows prompt transport, executable resolution, normalized finding keys, bounded consensus arithmetic, strict ballot identity/coherence, complete process-tree termination, a bounded Codex driver profile, disabled Git hooks/external diff helpers, pre-staging clean-filter rejection, pinned diff generation, binary/unsupported-repository rejection, precise Git side-effect language, canonical status/outcome/exit contracts, versioned run artifacts, run-ID validation, disposition precedence, repository locking, deterministic output extraction, and expanded negative tests.

The only deliberate scope choice is reviewer/council isolation: v0.1.0 provides packet/context isolation with current native safe-mode controls on a trusted local machine. It does not claim an OS confidentiality boundary. Capability-enforced isolation remains post-MVP.

## 17. Revision 0.3 reconciliation summary

Revision 0.3 incorporates the second independent Codex/Sol and Claude/Opus review set. The blocking corrections are: a tested native-CLI/model-child credential boundary; complete validation after the repair turn; suspended Windows process creation with Job Object assignment before execution; and removal of model-computed council `overall_vote` in favor of a controller-derived value.

It also adds one shared pre-commit `ChangeValidator`, post-staging gitlink detection, strict UTF-8 and NUL-safe Git parsing, exact staged/committed diff hashes, bounded native streams and reserved scratch, a controller-owned proposition bound, strict JSON-compatible YAML/environment grammar, typed mode-specific phases, explicit-null artifact fields, exhaustive failure triggers, filesystem-identity repository locks, qualified controller nondisclosure, and the complementary rough-consensus threshold cases. The offline specification now defines 28 core, 43 Code Once, and 32 Council Once cases: 103 mandatory tests in total.

## 18. Revision 0.4 reconciliation and future-ingress summary

Revision 0.4 closes the targeted v0.3 review findings without expanding the MVP workflow. It makes the Codex named permission profile exclusive and internally consistent; separates protected control files from model-writable scratch; bounds path count and candidate bytes before staging; streams bounded Git output; defines strict UTF-8/JSON grammar and artifact bindings; specifies Windows reader threads and an exact per-fixture environment; splits offline construction evidence from pinned-native Codex behavior evidence; defines fail-fast parallel cleanup; and narrows the POSIX guarantee honestly to its process group rather than requiring cgroup-v2 in the MVP.

It also records—but does not implement—the post-alpha/beta MCP design. The CLI now targets a transport-neutral `DialecticService`; a future MCP host may initiate and render runs but cannot select or frame participants, alter packets, own Git or consensus, or pass its tools into participants. Future writable starts require registered repository/profile identities, external authorization, idempotency, bounded untrusted results, and an explicit asynchronous job-owner/recovery contract. No MCP dependency, command, server, participant capability, or MCP-specific MVP test has been added. The offline specification now defines 30 core, 46 Code Once, and 32 Council Once cases: 108 mandatory tests in total.
