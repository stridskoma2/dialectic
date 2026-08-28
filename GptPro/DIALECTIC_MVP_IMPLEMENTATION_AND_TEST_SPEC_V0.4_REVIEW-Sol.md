# Review Feedback: Dialectic MVP Implementation and Test Specification v0.4

**Reviewed document:** `DIALECTIC_MVP_IMPLEMENTATION_AND_TEST_SPEC_V0.4.md`  
**Review date:** 2026-08-28  
**Reviewer:** Sol  
**Review type:** Read-only implementation, testability, control-contract, and safety review  
**Recommendation:** Revise once more before implementation

## 1. Overall assessment

Version 0.4 is a serious improvement over v0.3. It resolves the prior Sol findings
on paper: the Codex access matrix is now coherent and selects one named permission
profile; controller control files are separated from model-writable temporary data;
candidate changes are bounded before staging; POSIX containment is described
honestly; JSON parsing is strict; response artifacts are versioned; offline and
pinned-native evidence are separated; the native environment is fixture-exact;
the reserved path is collision-checked; Windows has an explicit pipe-reader design;
and fail-fast peer cleanup is specified. The declared offline test count is also
correct: 30 core + 46 code + 32 council = 108.

The MCP additions are appropriately non-MVP. The new `DialecticService` seam,
registered future identities, external authorization, idempotency, bounded
untrusted results, and explicit asynchronous job-owner requirement preserve the
controller as the authority rather than turning a conversational host into the
orchestrator.

I do not yet consider the document a final implementation candidate. Five remaining
issues affect a literal implementation's state contract, resource bounds, audit
evidence, or Git-integrity boundary. Three additional ambiguities should be closed
before their slices. None requires changing Code Once or Council Once as a product;
they are boundary and evidence corrections.

Priority meanings:

- **P1:** Resolve before implementation. A literal implementation can violate a
  normative property or leave a safety/resource boundary unenforced.
- **P2:** Resolve before the corresponding slice. Different conforming
  implementations can otherwise behave or attest differently.

## 2. P1 findings

### P1-01: The service boundary and `CREATED` lifecycle cannot both be implemented as written

**Affected clauses:** component responsibilities around lines 264-283 and run-record
creation around line 764.

The three relevant requirements conflict:

1. `DialecticCLI` loads the named local files.
2. `DialecticService` accepts already validated, bounded domain requests rather than
   configuration-file paths.
3. The controller creates the `CREATED` record before loading the named configuration
   and input files, so missing, oversized, wrongly encoded, or schema-invalid inputs
   persist as `INVALID_INPUT`.

If the CLI loads and validates first, an input failure occurs before the service or
controller can create the required record. If the service creates the record and
loads the files, it now accepts or owns ingress paths despite the opposite contract.
If the CLI creates the run record itself, run-state authority has leaked out of the
service and into the transport adapter.

The file-reading bound is also incomplete. Section 4 says the raw configuration and
input must fit hard ceilings, but it does not require a bounded `limit + 1` read. An
implementation may read an arbitrarily large or growing file into memory and only
then compare its length, defeating the purpose of the pre-parse ceiling.

**Required contract change:** Define an explicit preparation lifecycle. One workable
shape is:

```text
DialecticService.create_run(mode) -> RunHandle with persisted CREATED record
IngressAdapter.acquire_inputs_bounded(RunHandle, source arguments)
DialecticService.execute_code_once(RunHandle, validated CodeOnceRequest)
DialecticService.execute_council_once(RunHandle, validated CouncilOnceRequest)
DialecticService.fail_invalid_input(RunHandle, bounded diagnostic)
```

The ingress adapter may interpret CLI paths, but it must read at most the product
hard ceiling plus one byte before configured limits are known, reject special files
that can block indefinitely, decode strict UTF-8, and pass bytes/domain values rather
than paths into the service. Run creation and every terminal transition remain
service-owned; the CLI only translates ingress failures into the service's bounded
input-failure operation. An equivalent single-call design is fine if it preserves
those properties explicitly.

**Tests to add or strengthen:**

- Missing configuration/task/prompt after a valid command creates a run and persists
  `INVALID_INPUT`.
- Huge, sparse, growing, FIFO/device, UTF-16, BOM, and invalid-UTF-8 input cases do
  not allocate or block beyond the bounded acquisition contract.
- `dial` and `dialectic` produce the same record and exit behavior for each ingress
  failure.
- CLI handlers cannot write `RunRecord` or call an orchestrator directly.

### P1-02: The Windows pipe bridge can queue unbounded output ahead of the event loop

**Affected clauses:** Windows reader design around lines 434-438 and CORE-028.

The reader threads submit chunks with `loop.call_soon_threadsafe`. That API schedules
callbacks; it does not provide queue capacity or backpressure. A fast child and a
temporarily slow event loop can therefore let a reader thread enqueue many chunks
before the event-loop-side supervisor notices `max_agent_stdout_bytes` or
`max_agent_stderr_bytes`. Each chunk may be bounded while the total queued memory is
not.

This contradicts the hard stream-bound claim and can turn the infinite-output test
into a process-memory exhaustion test.

**Required contract change:** Put the byte accounting and overflow decision in the
reader thread before scheduling a chunk, or bridge through a bounded queue/semaphore
whose capacity is included in the limit. At most `configured limit + one bounded
read chunk + fixed event metadata` may exist across the pipe reader, bridge, and
event loop. Overflow must atomically signal one termination path; further bytes are
discarded/drained only as required to close the handles, not enqueued.

**Tests to add:**

- A native writer floods stdout while the event loop is deliberately stalled.
- Stdout and stderr flood simultaneously.
- Peak queued/captured bytes remain within the declared limit plus the documented
  fixed allowance.
- Overflow, timeout, and cancellation races still join both readers and close every
  handle exactly once.

### P1-03: The artifact table omits most `AgentRequest` and `AgentResponse` records

**Affected clauses:** `AgentResponse` around lines 326-348, artifact trees and schema
bindings around lines 468-700, and the requirement to retain model prompts and
responses around line 700.

Only `driver/*.request.json` and `driver/*.response.json` are bound to
`AgentRequestArtifact` and `AgentResponse`. Reviewer files contain normalized review
payloads, and council files contain normalized opening/revision/candidate/ballot
payloads. Raw stdout/stderr is named by alias and turn, but there is no specified
request or normalized response artifact for:

- each reviewer;
- each council opening;
- each council cross-examination resume;
- the moderator;
- each final ballot resume.

This loses the exact composed prompt for the reviewer/cross-examination/ballot turns
and loses per-turn `AgentResponse` fields such as native session ID, actual model,
CLI version, launch kind, effective safety flags/profile, duration, stream counts,
and usage. The alias map cannot substitute for per-turn execution evidence, and raw
streams do not contain all normalized metadata.

The omission violates the explicit retention requirement and weakens two core MVP
claims: exact session continuation and durable evidence of what each participant
saw and how it ran.

**Required contract change:** Define one generic per-turn artifact layout or add
request/response pairs to each current role directory. For example:

```text
turns/<role>/<alias>/<phase>.request.json   # AgentRequestArtifact
turns/<role>/<alias>/<phase>.response.json  # AgentResponse
turns/<role>/<alias>/<phase>.stdout.txt
turns/<role>/<alias>/<phase>.stderr.txt
```

The normalized review/council wrappers can remain as control artifacts. The schema
table must bind every request, response, and raw stream path. Add a versioned schema
for `config.redacted.json` and the capability-attestation object rather than naming
their contents only descriptively.

The sentence that embedded model payloads are “byte-for-byte equivalent to the
validated parsed values” should also be corrected. Parsed values have no byte-level
identity, JSON parsing loses lexical formatting, and known-value redaction may alter
persisted text. Require field/value equivalence to the validated parsed object;
retain the bounded redacted native bytes separately.

**Tests to add:**

- A complete happy-path artifact walk finds exactly one request and response for
  every native start/resume/moderator call.
- Every resume request uses the session ID from that participant's preceding
  persisted response.
- Prompt hashes match the exact outbound bytes before documented redaction, while
  persisted redacted hashes are distinguished if needed.
- No council model-facing artifact contains the alias-to-runtime map.

### P1-04: Scratch byte monitoring leaves an unbounded entry-count traversal

**Affected clauses:** `max_turn_scratch_bytes` around lines 176 and 230, scratch
monitoring/cleanup around lines 390-394, and CORE-027.

The scratch detector sums regular-file logical bytes. A process can create a very
large number of empty files, directories, or symlinks while contributing zero or
negligible logical bytes. The four-times-per-second recursive scan, authoritative
post-exit scan, and cleanup then have unbounded path count and traversal cost. A deep
tree can also stress recursion/path handling independently of bytes.

Version 0.4 correctly states that sampling is not a filesystem quota, but it does
not acknowledge or bound the inode/entry dimension. A literal implementation can
spend unbounded CPU/time walking or deleting `tmp/`, or exhaust local directory
entries before the byte detector fires.

**Required contract change:** Add `max_turn_scratch_entries` and a traversal-depth
limit, count every directory entry without following links, and terminate on the
first overage. The monitor must use an iterative bounded walk, not build a complete
path list. The text should make the same honest qualification as the byte sampler:
without a quota-backed scratch filesystem, entry-count detection is best effort and
does not prevent a creation burst between samples. If cleanup cannot finish within
a separately bounded cleanup contract, fail as `PROCESS_CLEANUP_FAILED` or a clearly
assigned internal failure without proceeding to Git validation.

**Tests to add:** Empty-file storm, empty-directory storm, deep nesting, symlink
storm, create/delete burst, and a cleanup-timeout fixture. Verify the monitor itself
never accumulates the complete entry set.

### P1-05: Multi-link product files are not covered by the Git-metadata write boundary

**Affected clauses:** the exact Codex access matrix around lines 408-424,
single-link control-file rule around line 392, candidate inspection around line 860,
CODE-045, and LIVE-CODE-002.

The control-file contract correctly rejects hard-link anomalies. The candidate
change contract, however, accepts any regular file regardless of `st_nlink`.

That leaves an aliasing case unaddressed: a model-generated command may attempt to
hard-link a readable file from the common Git directory or another allowed read-only
location into the writable product tree, then access the same inode through its
writable path. Whether a particular Codex sandbox/backend blocks that cross-boundary
link operation is platform-specific. The specification currently neither requires
the permission probe to test it nor requires `ChangeValidator` to reject the result.
The official permission documentation describes path rules and platform caveats but
does not establish a portable hard-link invariant.

This matters because the definition of done claims that Git metadata is not
writable. Direct-path denial is not sufficient evidence for aliasing operations.

**Required contract change:**

- Add cross-boundary hard-link creation and write-through attempts to the pinned
  native capability probe on Windows and Linux; unsupported enforcement fails
  preflight.
- Before staging, reject changed regular files with an unexpected link count unless
  the implementation can prove every link is contained in the isolated worktree and
  outside all controller/Git/auth/state paths. The simpler MVP rule is `st_nlink ==
  1`.
- Treat a discovered multi-link candidate as `UNSUPPORTED_CHANGE`; do not read or
  stage it first.

**Tests to add:** Hard-link a Git-object sentinel, original-worktree sentinel, and
ordinary outside sentinel into the worktree, then attempt mutation through the
worktree name. The source must remain byte-identical and the run must fail closed if
the operation can be created at all.

## 3. P2 findings

### P2-01: “Enumerate changed paths” does not define the leaf set on which the new bounds rely

**Affected clauses:** ChangeValidator steps 2-3 around lines 859-860 and CODE-045.

The specification requires incremental NUL-delimited enumeration, but does not pin
the command(s), untracked-file mode, union, or deduplication rule. This became
material in v0.4 because path count and aggregate bytes are now safety inputs.

Default `git status` behavior may collapse a whole untracked directory to one entry.
An implementation using that output can either reject a valid directory as a
non-file in step 3 or count one path while `git add -A` later expands many files.
Another implementation using `--untracked-files=all` sees each leaf. A path that is
both staged and modified again can also appear in multiple source sets and be
double-counted unless the raw path union is defined.

**Required contract change:** Specify an exact NUL-safe leaf enumeration, such as a
byte-exact union of tracked staged/unstaged path commands plus
`git ls-files --others --exclude-standard -z`, with untracked directories always
expanded to files. Deduplicate paths before applying `max_changed_paths` and byte
sums; preserve deterministic ordering for audit; define rename and deletion
handling; and stop every contributing subprocess as soon as the unique union reaches
the configured overage.

**Tests to add:** Nested untracked directory, more-than-limit untracked leaves,
staged-then-modified path, rename, deletion, duplicate raw path across sources, and
valid Unicode/tab-containing leaves.

### P2-02: Capability-attestation reuse does not define what is generic and what is run-specific

**Affected clauses:** preflight and attestation around lines 376-380, CORE-030, and
the dynamic permission rules around lines 408-426.

The cache records a permission-profile **template** hash while each run generates
exact rules for a new worktree, original worktree, Git directory, state root,
saved-auth paths, and temporary roots. “Failed dynamic path check” invalidates the
attestation, but the check is not defined. A static inspection can prove that the
new paths were inserted; it cannot by itself prove that the pinned native sandbox
enforces the rule classes that the cached cost-bearing probe exercised elsewhere.

“Resolved executable identity” is also not defined tightly enough to say whether an
in-place binary replacement with the same path and version string invalidates the
cache.

**Required contract change:** Split the attestation into:

1. A generic native/backend behavior attestation keyed by executable file identity
   or digest, CLI version, platform/sandbox backend and elevation mode, adapter
   fixture, canonical profile-template hash, and relevant managed-policy fingerprint.
2. A per-run construction record containing the complete concrete profile hash and
   resolved dynamic filesystem identities.

Define the generic probe's sentinel substitution so every rule class is exercised,
and define the cheap per-run validation that proves the concrete rules instantiate
that attested template exactly. Any new rule shape, backend, managed requirement, or
binary fingerprint re-probes.

**Tests to add:** Same path/version with replaced executable identity, same template
with different dynamic roots, elevation/backend change, managed-policy change, and a
concrete path omitted or inserted into the wrong rule class.

### P2-03: The promised future transport separation is not reflected in `AgentResponse`

**Affected clauses:** transport statement around line 321 and `AgentResponse` around
lines 326-348.

The text says a later direct-API transport changes an adapter fixture rather than
controller workflow semantics. That is correct at the state-machine level, but the
current neutral adapter response requires CLI/process-only fields:
`resolved_executable`, `spawned_root_executable`, `launch_kind`, `cli_version`, and a
`prompt_transport` limited to `stdin` or `acp-stdio`. A direct API cannot populate
those fields without inventing values, so the interface and artifact schema would
have to change.

This does not block the native-CLI MVP. Clarify that transport independence applies
to workflow semantics, not the current response schema, or define a discriminated
transport audit union now:

```text
transport_kind: cli | acp | api
process_audit: ProcessAudit | null
api_audit: ApiAudit | null
```

Keep provider/model identity separate from transport so a future API adapter does
not need to masquerade as `claude-code` or `grok-build`.

## 4. Provider and protocol validation

The direction of v0.4's Codex correction is supported by current official OpenAI
documentation:

- Permission profiles are beta and do not compose with `sandbox_mode` or
  `--sandbox`; the legacy settings win if both are loaded.
- Named profiles support exact paths, workspace-root rules, more-specific carveouts,
  default-deny patterns, and network-off policy.
- `--ignore-user-config` skips `$CODEX_HOME/config.toml` while authentication still
  uses `CODEX_HOME`.

References checked:

- [OpenAI Codex permission profiles](https://learn.chatgpt.com/docs/permissions)
- [OpenAI Codex non-interactive mode](https://learn.chatgpt.com/docs/non-interactive-mode)
- [OpenAI Codex configuration reference](https://learn.chatgpt.com/docs/config-file/config-reference)

The installed local `codex-cli 0.150.0-alpha.8` also exposes repeatable `-c
key=value`, `--ignore-user-config`, `--ignore-rules`, stdin prompt `-`, output schema,
and output-last-message options. That is supporting local evidence only; the
versioned fixture and pinned-native tests remain necessary because the permission
profile feature is explicitly beta.

The new Grok MCP-source check is also grounded in current primary documentation:
`grok inspect` reports loaded MCP servers and their origins, including Claude,
Cursor, and project compatibility sources. See
[Grok Build MCP servers](https://docs.x.ai/build/features/mcp-servers) and the
[Grok CLI reference](https://docs.x.ai/build/cli/reference).

## 5. Confirmed strengths

The following v0.4 changes should be retained:

- One exclusive named Codex permission profile with a concrete Git read/write
  matrix and no legacy sandbox fallback.
- Protected `control/` plus model-writable `tmp/`, with handle-anchored no-follow
  ingestion and cleanup.
- Pre-stage file/count/aggregate limits and streamed diff generation.
- Strict JSON grammar and strict Pydantic models.
- Separate offline construction evidence and pinned-native enforcement evidence.
- Fail-fast review and council barriers that reap peers before terminal persistence.
- Honest POSIX process-group limitation instead of an unprovable complete-tree
  claim.
- A controller-owned application seam and future MCP rules that preserve neutral
  orchestration authority.
- Explicit refusal to hide asynchronous job ownership inside a thin MCP adapter.

## 6. Recommended revision gate

Before implementation begins, revision 0.5 should:

1. Reconcile run creation, bounded input acquisition, validation, and the typed
   `DialecticService` request lifecycle.
2. Make the Windows reader bridge bounded across threads and the event-loop queue.
3. Persist request and normalized response artifacts for every native turn.
4. Bound scratch entry count/depth as well as logical bytes.
5. Close the hard-link alias case in the permission probe and candidate validator.
6. Pin the unique changed-leaf enumeration used by all pre-stage limits.
7. Define the generic versus run-specific portions of capability attestation.
8. Scope or generalize the transport-specific `AgentResponse` contract.

After those changes, I would be comfortable treating the document as an
implementation baseline. The workflow itself is stable; the remaining work is to
make the new safety and audit claims implementable exactly as written.

## 7. Finding index

| ID | Priority | Title |
|---|---|---|
| P1-01 | P1 | Service boundary conflicts with pre-load `CREATED` persistence |
| P1-02 | P1 | Windows callback bridge can queue unbounded native output |
| P1-03 | P1 | Reviewer/council request and response artifacts are missing |
| P1-04 | P1 | Scratch bytes do not bound entry-count traversal |
| P1-05 | P1 | Multi-link product files are outside the Git-integrity proof |
| P2-01 | P2 | Changed-path leaf enumeration and deduplication are undefined |
| P2-02 | P2 | Capability-attestation reuse lacks a generic/run-specific contract |
| P2-03 | P2 | `AgentResponse` remains CLI-specific despite future transport claims |
