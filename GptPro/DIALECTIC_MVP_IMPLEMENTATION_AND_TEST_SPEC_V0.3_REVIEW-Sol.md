# Review Feedback: Dialectic MVP Implementation and Test Specification v0.3

**Reviewed document:** `DIALECTIC_MVP_IMPLEMENTATION_AND_TEST_SPEC_V0.3.md`  
**Review date:** 2026-08-28  
**Reviewer:** Sol  
**Review type:** Read-only implementation, testability, control-contract, and safety review  
**Recommendation:** Revise before implementation

## 1. Overall assessment

Version 0.3 resolves the findings from the v0.2 Sol review on paper. It now
defines a native-CLI/model-child credential boundary, reruns complete change
validation after repair, assigns Windows processes to a Job Object before they
execute, rejects newly staged Git links, bounds native streams, defines strict
diff encoding, qualifies controller nondisclosure, closes the configuration
grammar, and derives council ballot outcomes in the controller.

The remaining problems arise primarily from interactions between those new
controls. Four issues should be resolved before implementation because they can
make the required security profile impossible to instantiate literally, allow a
model-writable path to become a controller privilege-escalation channel, consume
unbounded local resources before validation, or leave Linux descendants outside
the promised process-tree boundary. Five further contract gaps should be closed
before their corresponding slices.

Priority meanings in this review:

- **P1:** Resolve before implementation. A literal implementation can be unsafe,
  unbounded, or incapable of satisfying two normative requirements at once.
- **P2:** Resolve before the corresponding vertical slice. Independent
  implementations could otherwise validate different data or provide different
  evidence for the same safety claim.

## 2. P1 findings

### P1-01: The Codex filesystem policy contradicts itself

**Affected clauses:** authentication and child-environment boundary around line
366, driver runtime policy around lines 372-376, adapter tests around line 1273,
and the engineering definition of done around line 1336.

Section 5.4.3 requires the effective permission profile to deny model-generated
reads of the original repository/common Git directory. Section 5.4.4 then
requires the linked worktree's resolved Git directory to be read-only. For a
linked worktree, those requirements overlap: Git needs read access to the common
object database, refs, configuration, and linked-worktree metadata.

A literal deny makes ordinary commands such as `git status`, `git diff`, and
repository-aware tooling fail. Allowing the read contradicts the earlier deny.
Current Codex permission-profile semantics also make the conflict consequential:
a deny rule takes precedence over a read rule.

The selection mechanism is also ambiguous. The specification repeatedly calls
for a controller-defined permission profile but the definition of done calls it
a no-network `workspace-write` profile, and the packet fixture language refers
to read-only sandboxing. Current Codex documentation states that named
permission profiles do not compose with the older `sandbox_mode` or `--sandbox`
settings. If an implementation supplies `--sandbox workspace-write`, the named
profile containing the required saved-auth and state-root read denials is not the
active enforcement mechanism.

**Required contract change:** Define one exact access matrix:

- Isolated worktree product files: read/write.
- Linked-worktree `.git` pointer and the minimum required common Git and
  per-worktree metadata: read-only.
- Original checked-out worktree files: deny.
- Saved-auth paths and Dialectic state: deny.
- Git metadata writes: deny.
- Platform/runtime paths required by tools: minimal read.
- Command network: disabled.

Select a named Codex permission profile exclusively. State that the adapter does
not pass `--sandbox` and does not load `sandbox_mode` for this execution path.
Define how the controller-generated profile and dynamic path rules are supplied
while `--ignore-user-config` remains active, including the safe Windows batch-shim
transport. Record the exact selected profile and resolved effective rules.

**Tests to add or strengthen:**

- A child command can run `git status` and read the base commit from the linked
  worktree.
- Direct writes to refs, objects, Git config, and the linked-worktree index fail.
- Reads of original-worktree ignored secrets, saved authentication, and the
  Dialectic state root fail.
- The active policy is the named profile; no old `--sandbox` or `sandbox_mode`
  setting displaced it.
- A managed-policy conflict fails preflight rather than silently selecting a
  weaker mode.

### P1-02: Controller handling of `.dialectic-turn/` creates a confused-deputy path

**Affected clauses:** driver schema/output placement and reserved scratch
handling around lines 356-358.

The specification places controller-named schema and output files beneath
`.dialectic-turn/`, while model-generated commands are permitted to write that
directory. After the native process completes, the trusted CLI or controller
copies files from the directory and recursively removes it.

A model-generated command can replace an expected file or directory with a
symbolic link, Windows junction/reparse point, FIFO, socket, or other special
object. The target need not be readable to the sandboxed command. When the
trusted native CLI or controller later opens that path outside the child-command
sandbox, it can follow the indirection and:

- Copy denied saved-auth or state content into run artifacts.
- Overwrite an outside file when the native CLI writes its final output.
- Hang while opening a FIFO or device-like object.
- Traverse outside the worktree during recursive cleanup.

The permission-profile denial therefore does not close the credential boundary
if a more privileged controller later follows a model-created path.

**Required contract change:** Separate controller control files from writable
scratch. One suitable structure is:

```text
.dialectic-turn/
  control/     # native CLI may use fixed files; model-generated commands deny write
  tmp/         # model-generated commands may write; never trusted as control data
```

Expected output ingestion must open with no-follow semantics, require a regular
file, enforce its byte bound before reading, and verify that file identity does
not change between inspection and the opened handle. Reject reparse points,
junctions, FIFOs, sockets, devices, and unexpected hard-link states. Cleanup must
remove link objects themselves and must never traverse a link or reparse target.
All deletion must remain contained beneath the verified worktree scratch root.

**Tests to add:**

- Replace the expected output with a symlink to a saved-auth file.
- Replace a scratch child with a junction to the original repository or state
  root.
- Supply a FIFO or other non-regular object as the expected output.
- Rename and replace the scratch root between process exit and cleanup.
- Prove no outside content is read, overwritten, or deleted in every case.

### P1-03: The change-size gate runs after potentially unbounded writes and Git object creation

**Affected clauses:** configured bounds around lines 151-166, scratch monitoring
around line 358, and the shared `ChangeValidator` around lines 739-760.

`max_diff_bytes` is enforced only after the complete product state is staged and
the complete diff is generated. Before that check:

- The driver can create an arbitrarily large product file outside
  `.dialectic-turn/`.
- `git add` writes the file's blob into the repository's shared Git object
  database, even if the file is subsequently rejected as binary or oversized.
- A complete oversized diff can be accumulated in memory or on disk before its
  length is compared with `max_diff_bytes`.
- Millions of empty files consume directory entries and make path enumeration
  unbounded while contributing almost nothing to a logical-byte counter.
- Four-times-per-second scratch polling can miss a burst that writes and deletes
  data between samples; it is a detector, not a hard quota.

The validator can therefore reject a change only after large shared Git objects,
memory pressure, disk consumption, or inode exhaustion has already occurred.
This conflicts with the claim that the configured hard ceilings prevent
unbounded local runs.

**Required contract change:** Add pre-staging bounds such as:

- `max_changed_paths`.
- `max_changed_regular_file_bytes` for each file.
- `max_candidate_change_bytes` across the proposed state.
- A bounded controller-subprocess stdout/stderr limit for Git commands.

Inspect path count, file type, and logical size before `git add`. Stream diff
output and stop after `max_diff_bytes + 1` rather than accumulating it first.
Consider writing validation objects to a quarantined object directory and making
them reachable from the shared database only after validation succeeds. If no
filesystem quota is used during the driver turn, state explicitly that product
file writes and sampled scratch use are not hard write quotas.

**Tests to add:**

- A huge binary file and a huge UTF-8 text file.
- A huge sparse file whose physical and logical sizes differ.
- A large empty-file storm that exceeds the path/entry limit.
- Git diff or enumeration output that exceeds the controller-process bound.
- Rejection occurs before unbounded objects are left in the shared Git database.

### P1-04: A POSIX process group is not complete process-tree containment

**Affected clauses:** POSIX supervision and cleanup around lines 1079-1084 and
the process-tree tests around lines 1143-1144.

Starting a new POSIX session/process group and signalling that group contains
ordinary descendants, but it is not a complete process-tree boundary. A child
can call `setsid()` and move into a new session and process group. Repository
scripts and test runners can daemonize this way intentionally or accidentally.

The specification also defines lingering-descendant cleanup after a normal root
process exit only for Windows. On Linux, a background descendant can close its
inherited stdout/stderr handles, survive the successful native root, and continue
modifying the worktree while `ChangeValidator` runs.

**Required contract change:** Either:

1. Require per-turn cgroup-v2 ownership on the Linux release platform and kill
   through that cgroup, failing preflight when the required facility cannot be
   established; or
2. Narrow the Linux guarantee explicitly to process-group members and document
   that daemonized descendants are outside the MVP safety boundary.

For the current definition-of-done claim, the first option is the consistent
choice. On normal root completion as well as failure, timeout, and cancellation,
the controller must terminate and reap every remaining owned descendant before
returning a successful `AgentResponse`.

**Tests to add:**

- An immediate child calls `setsid()`, closes inherited streams, and attempts a
  delayed sentinel write.
- A background child remains alive after the native root exits zero.
- Validation does not start until the containment unit is empty.
- Unsupported cgroup ownership fails preflight rather than weakening the stated
  guarantee silently.

## 3. P2 findings

### P2-01: The model JSON grammar is not actually strict

**Affected clauses:** global model bounds around line 190, model configuration
around line 294, bounded parsing around line 384, and output extraction around
lines 388-394.

`extra="forbid"` rejects unknown fields, but Pydantic v2 is coercive unless strict
mode is selected. An implementation can consequently accept strings for boolean
or numeric fields that another strict implementation rejects.

The JSON parser contract also does not reject duplicate keys, non-standard
`NaN`/`Infinity` constants, or escaped lone surrogate code points. A lone
surrogate arrives as valid ASCII JSON bytes, survives strict UTF-8 decoding, and
then cannot be encoded as a Unicode scalar for a later prompt or artifact.

**Required contract change:** Require `ConfigDict(strict=True, extra="forbid")`
for every external or model-facing schema. The JSON parser must reject duplicate
keys and non-finite constants, enforce the depth limit during parsing, and
recursively reject strings containing non-scalar surrogate code points.

**Tests to add:** String booleans/numbers, duplicate ballot fields, `NaN`,
`Infinity`, escaped lone surrogates, excessive nesting, and a valid supplementary
Unicode character.

### P2-02: Persisted response files do not have an unambiguous artifact schema

**Affected clauses:** `AgentResponse` around lines 298-320, artifact trees around
lines 414-475, and the versioning rule around line 480.

`AgentResponse` contains controller-normalized execution and safety metadata but
lacks `artifact_schema_version` and `tool_version`. The code artifact tree then
persists files such as `initial.response.json`, while section 6.1 requires every
controller-owned JSON object to carry those version fields.

Other mappings are left to inference, especially whether
`council/ballots/participant-a.json` contains the original `CouncilBallot`, the
controller-owned `DerivedBallot`, or a wrapper that preserves both.

**Required contract change:** Add version fields to `AgentResponse` or define a
versioned `AgentTurnArtifact` that wraps it. Add a filename-to-schema table for
every JSON artifact and state which objects are verbatim model payloads versus
controller-normalized artifacts.

### P2-03: Offline tests cannot prove native Codex enforcement

**Affected clauses:** CODE-039 and CODE-040 around lines 1208-1209, adapter tests
around line 1273, the Slice 1 exit criterion around line 1372, and the engineering
definition of done around lines 1336-1339.

CODE-039 and CODE-040 claim that credential, filesystem, configuration, approval,
and network restrictions take effect. Slice 1 nevertheless requires all Code
Once tests to pass with no native AI CLI installed. A scripted or fake adapter
can prove only the environment, profile, and flags that Dialectic constructs; it
cannot prove that a particular Codex CLI version enforces them.

**Required contract change:** Split each case into:

- A mandatory offline construction test that verifies the exact environment,
  generated permission profile, denied paths, flags, and scratch redirect.
- A pinned-native, platform-gated behavior test that performs the permitted and
  forbidden operations against the real CLI.

State which evidence supports each definition-of-done claim. Where possible,
use the local Codex sandbox helper for non-model filesystem/network probes;
reserve authenticated model invocations for behavior that cannot be established
locally.

### P2-04: The “OS-minimal baseline” environment is undefined

**Affected clause:** native environment construction around line 362.

The baseline determines whether a native runtime can start and which values reach
the child policy. Windows runtimes commonly require values such as `SystemRoot`,
while POSIX tools commonly require an explicit `PATH` and locale behavior. Two
implementations can therefore expose different environments while both claiming
to use an “OS-minimal” set.

**Required contract change:** Prefer an empty conceptual baseline and make each
versioned, per-platform adapter fixture enumerate every required non-secret name.
If a common baseline is retained, list it exactly for Windows and Linux. Record
and test the effective environment-name set without persisting values.

### P2-05: The fixed scratch name can collide with tracked repository content

**Affected clauses:** reserved scratch handling around line 358 and repository
preflight around lines 692-706.

A valid repository may already track a file, directory, or symlink named
`.dialectic-turn`. The controller then cannot prove the reserved path absent, but
the preflight subset does not reject the repository or assign the collision a
failure kind.

**Required contract change:** Reject any tracked entry at or below the reserved
path during repository preflight as `UNSUPPORTED_REPOSITORY`, or move all control
files outside the worktree and reserve only a collision-checked temporary
subtree. Add tracked file, directory, and symlink cases.

## 4. Additional clarifications

### 4.1 Define task, prompt, and configuration encoding

The files are byte-bounded and later described as UTF-8 prompts, but their input
decoding contract is not explicit. Require strict UTF-8 and define whether a UTF-8
BOM is accepted and stripped. Reject UTF-16 and invalid UTF-8 deterministically
before model work.

### 4.2 Make the truncation contract representable at every allowed limit

The output limits may be configured as low as one byte, while the overflow
artifact must contain a truncation marker and remain within that limit. Redaction
can also expand a retained value. Set a minimum large enough for the fixed marker
and worst-case redaction bookkeeping, or store truncation metadata outside the
bounded byte artifact.

### 4.3 Define concurrent fan-out failure behavior

When one reviewer or council participant fails while peers are still running,
choose one normative behavior: fail fast and cancel/reap all peers, or await every
peer within its existing timeout. A terminal record must never be written while
an owned peer process remains active. Add an early-invalid peer plus a long-running
peer fixture.

### 4.4 Classify a non-repository path explicitly

CODE-01 resolves the common Git directory before confirming a non-bare working
tree. A path that is not a Git repository can therefore plausibly map to either
`PREFLIGHT_FAILED` or `UNSUPPORTED_REPOSITORY`. Assign it to exactly one trigger
row so the exhaustive failure-kind test has one expected answer.

## 5. Provider validation

The direction of the new Codex boundary is supported by current official OpenAI
documentation: named permission profiles can express read, write, and deny rules;
deny rules take precedence; shell-environment filters can remove credential
variables; and project configuration can be ignored independently of
authentication.

The important qualification is that named permission profiles are beta and do
not compose with the older sandbox settings. Versioned fixtures and effective
behavior probes are therefore essential rather than optional implementation
detail.

References checked for this review:

- [OpenAI Permissions](https://learn.chatgpt.com/docs/permissions)
- [OpenAI configuration reference](https://learn.chatgpt.com/docs/config-file/config-reference)
- [OpenAI developer commands](https://learn.chatgpt.com/docs/developer-commands)
- [OpenAI agent approvals and security](https://learn.chatgpt.com/docs/agent-approvals-security)

These external behaviors were checked on 2026-08-28 and should be revalidated
whenever the pinned Codex adapter fixture changes.

## 6. Recommended revision gate

Before implementation begins, revision 0.4 should:

1. Define one internally consistent named Codex permission profile and Git access
   matrix.
2. Prevent controller/native-CLI trust of model-replaceable scratch paths.
3. Bound candidate changes and Git subprocess output before shared-object
   creation or full diff accumulation.
4. Replace or qualify the Linux complete-process-tree guarantee.
5. Close strict JSON, artifact versioning, native-proof, environment-baseline,
   and reserved-path contracts.

The adjacent Opus review independently identifies overlapping concerns about
scratch sampling, offline/native proof separation, the environment baseline, and
artifact mapping. The four P1 findings above are additional interaction-level
blockers.
