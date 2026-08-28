# Review Feedback: Dialectic MVP Implementation and Test Specification v0.2

**Reviewed document:** `DIALECTIC_MVP_IMPLEMENTATION_AND_TEST_SPEC_V0.2.md`  
**Review date:** 2026-08-27  
**Reviewer:** Sol  
**Review type:** Read-only implementation, testability, control-contract, and safety review  
**Recommendation:** Revise before implementation

## 1. Overall assessment

Version 0.2 materially improves the specification and closes the issues raised
in the v0.1 Sol review on paper. In particular, it now gives finding dispositions
globally unambiguous keys, validates consensus configuration, separates
controller-owned prompts from repository content, defines cancellation more
completely, strengthens isolated-worktree checks, bounds packets and diffs, and
adds test coverage for those contracts.

The architecture remains strong: deterministic controller decisions are kept
separate from model judgment, branch/worktree ownership is explicit, the
one-pass boundaries are understandable, and the fake-agent test strategy makes
the orchestration testable without provider access.

Before implementation, three remaining P1 issues should be resolved. They allow
credentials or unsupported repository state to cross a safety boundary, or
leave a Windows process-tree race in the normative design. Five P2 issues should
also be resolved before their corresponding slices because literal compliant
implementations could otherwise be unsafe or incompatible.

Priority meanings in this review:

- **P1:** Resolve before implementation. The current contract can produce an
  unsafe or unsupported result even if implemented literally.
- **P2:** Resolve before the corresponding vertical slice. Independent correct
  implementations could otherwise behave incompatibly, hang, or exhaust local
  resources.

## 2. P1 findings

### P1-01: Driver commands can inherit provider authentication secrets

**Affected clauses:** configuration requirements around lines 169-170, Codex
driver runtime policy around line 324, and the initial driver behavior around
lines 610-620.

The specification permits credentials to be supplied through inherited
environment variables. The Codex driver is also expressly permitted to run
repository-controlled scripts and tests. These two permissions compose into a
credential boundary failure unless the environment visible to model-generated
commands is separately constrained.

For example, the controller may need `OPENAI_API_KEY` or another provider key to
authenticate the native CLI process. If that CLI inherits the controller's full
environment and then launches a repository script, the script can read the key
and write it into a source file, test artifact, terminal log, or final diff.
Disabling network access reduces exfiltration paths but does not prevent secret
persistence into the worktree or run artifacts.

Current Codex configuration supports an explicit shell environment policy,
including `inherit`, `filters`, and `ignore_default_excludes`. The specification
does not currently require such a policy or establish that provider credentials
must be available to the CLI process but unavailable to commands run on behalf
of the model.

**Required contract change:** Define two distinct environments:

1. The native CLI process may receive only the credential material required by
   its adapter, or use the provider's saved native authentication.
2. Model-generated child commands must receive a minimal environment that
   excludes every adapter credential and controller secret.

For the Codex adapter, require an explicit `shell_environment_policy` rather
than relying on installation defaults. A suitable normative profile would use
`inherit = "core"` or `inherit = "none"`, set
`ignore_default_excludes = false`, and add explicit exclusions for every
credential name accepted by the adapter. The adapter credential allowlist
should be the single source from which both injection and exclusion are
derived.

The contract should also state that authentication through an inherited
environment is valid only when the credential is consumed by the native CLI and
is not forwarded to model-generated commands. Redaction is defense in depth;
it is not a substitute for withholding the secret.

**Tests to add:**

- Authenticate the fake/native adapter with an environment key, then have a
  driver command attempt to read that key. The command must not see it.
- Use a malicious repository script that prints and writes all environment
  variables. No provider credential may appear in stdout, stderr, artifacts, or
  the final diff.
- Cover every credential name in the adapter allowlist, not only one example.
- Verify saved native authentication separately so the no-environment-key path
  remains usable.
- Verify that an explicitly permitted non-secret environment variable still
  reaches a driver command.

### P1-02: Repair changes bypass the complete repository-state validation gate

**Affected clauses:** initial change capture and validation in CODE-04 around
lines 628-645, repair execution in CODE-10 around lines 768-774, and the
pre-staging filter requirement around line 970.

The initial driver path validates the proposed change before review: filters
are checked, unsupported file modes and binary content are rejected, UTF-8 and
diff-size requirements are applied, and an exact review snapshot is produced.
The repair path can make a new set of changes and commit them, but it does not
normatively repeat the full validation sequence.

The global rule requiring filter checks before each staging operation closes
only one part of this gap. It does not reject a newly staged mode-160000 entry,
binary file, invalid UTF-8 text diff, or an oversized final diff. A repair can
therefore introduce repository state that the initial driver would have been
forbidden to submit. Since there is no second review pass, this is also the last
controller-owned gate before success.

**Required contract change:** Define one shared final-state validation operation
and invoke it after every writable driver turn and before any successful run can
be reported. The operation should apply to the staged index plus the complete
resulting diff and should include, in this order:

1. Re-run the NUL-safe pre-staging path and attribute-filter checks.
2. Stage through the controller-owned path.
3. Inspect staged index modes and reject any mode `160000` entry.
4. Detect and reject binary changes.
5. Strictly validate the defined text encoding.
6. Enforce the final diff byte bound.
7. Persist the exact validated diff and its hash before commit/success.

The specification should say whether the initial `max_diff_bytes` limit is also
the final limit or introduce a separate `max_final_diff_bytes`. Reusing one
limit is simpler for the MVP. A failed repair snapshot may be retained for
diagnostics, but the run must not advance to success.

**Tests to add:**

- A repair adds a binary file.
- A repair creates a nested repository that stages as a Git link.
- A repair touches a filtered path.
- A repair creates a non-UTF-8 text diff.
- A repair expands the final diff beyond the configured byte limit.
- Each case fails closed with the exact documented failure kind, persists
  bounded diagnostics, and does not invoke another reviewer or synthesizer.
- The unchanged-repair and valid-repair paths still complete normally.

### P1-03: Windows Job Object assignment occurs too late to contain the full process tree

**Affected clauses:** Windows process-tree timeout and cancellation around lines
950-952 and the Windows dependency choice around line 209.

Assigning a normally started process to a Job Object after process creation
leaves a race. The child can execute immediately and spawn descendants before
the controller assigns the parent to the job. Descendants created during that
window are not guaranteed to become members of the subsequently assigned job,
so closing or terminating the job may leave an escaped process alive.

Tests that use a delayed child or grandchild will not expose this race. It is
most likely to appear under exactly the failure conditions for which reliable
cleanup matters: fast launchers, wrappers, and command shims.

**Required contract change:** On Windows, launch the native CLI suspended using
`CREATE_SUSPENDED`, create/configure the Job Object, assign the suspended process
to it, and resume the primary thread only after assignment succeeds. Configure
kill-on-job-close before resuming. If creation, configuration, or assignment
fails, terminate the still-suspended process, close all handles deterministically,
and fail the phase without allowing the target program to execute.

The implementation contract should also define handle ownership, cleanup on
normal completion, timeout, cancellation, and controller crash. Nested-job
behavior should be tested on the supported Windows 11 baseline rather than
assumed from older Windows limitations.

Microsoft's `AssignProcessToJobObject` documentation explicitly describes
creating a process suspended when job assignment must precede execution.

**Tests to add:**

- A fake executable spawns a child at its first executable instruction. Both
  processes must be in the job and must terminate on timeout/cancellation.
- Simulate Job Object assignment failure and prove the target executable never
  reaches its entry-point side effect.
- Verify kill-on-job-close for parent, immediate child, and grandchild.
- Verify normal completion closes process, thread, and job handles.
- Inject cleanup failure and confirm bounded diagnostics are persisted without
  incorrectly reporting success.

## 3. P2 findings

### P2-01: Newly created Git links are not authoritatively rejected before commit

**Affected clauses:** repository preflight around line 589 and initial staging
and validation around lines 628-631.

Preflight rejects mode-160000 entries that already exist, but the writable
driver can create or copy an embedded Git repository after preflight. A normal
`git add` can stage that directory as a Git link. The current pre-staging rule
checks attributes and filters, not the resulting index mode, and binary
`--numstat` inspection is not an authoritative Git-link check.

The review packet can consequently contain only a `Subproject commit` line
instead of the contents the model created. This conflicts with both the
complete-diff review contract and the stated exclusion of submodules.

**Required contract change:** After controller-owned staging and before commit,
inspect the staged index with NUL-safe Git plumbing and reject every entry whose
mode is `160000`. The staged index is the authoritative boundary. Apply the
same check after the repair turn through the shared final-state validation gate
from P1-02.

The specification should define the resulting failure kind and the preserved
diagnostic state. It should not require a destructive reset merely to report
the failure.

**Tests to add:**

- The driver creates a new nested Git repository.
- The driver copies an existing embedded repository into the worktree.
- Both cases fail before commit and before model review.
- A normal directory named `.git` only as part of a filename does not cause a
  false positive.
- Retain the existing tracked-submodule preflight test; it covers a different
  boundary.

### P2-02: Native agent output is unbounded before parsing and artifact creation

**Affected clauses:** configuration limits around lines 151-164, output
extraction around lines 330-338, and raw stdout/stderr artifact requirements.

The specification bounds tasks, diffs, packets, and some structured fields, but
does not bound native agent stdout, stderr, final response text, or aggregate
structured-list sizes before parsing. A runaway CLI, broken adapter, or hostile
fake process can exhaust memory or disk before `max_packet_bytes` is ever
evaluated.

Reading all output into memory and then truncating the artifact would not close
the resource boundary. The controller must stop accepting bytes once the limit
is exceeded.

**Required contract change:** Add `max_agent_stdout_bytes` and
`max_agent_stderr_bytes`, or a single documented `max_agent_output_bytes` shared
across both streams. Capture incrementally with fixed bounds. On overflow,
terminate the entire contained process tree, retain only a bounded diagnostic
tail or prefix according to a documented rule, and return a distinct failure.

The output extractor should also impose schema-level collection and string
limits before constructing large in-memory objects. Credential redaction must
remain correct across stream chunk boundaries; a simple per-chunk text replace
can leak a credential split across two reads. One straightforward MVP approach
is to capture only up to the hard bound, then redact the complete bounded byte
buffer before persistence.

**Tests to add:**

- Fake CLI exceeds the stdout limit.
- Fake CLI exceeds the stderr limit.
- Fake CLI emits indefinitely until the controller kills the job.
- The retained artifact never exceeds its documented maximum.
- A credential split across two capture chunks is still redacted.
- Large output just below the limit parses and proceeds normally.

### P2-03: “Exact UTF-8 diff” is not defined for Git text that is not UTF-8

**Affected clause:** CODE-04 around line 643.

Git's binary heuristic does not establish that a text diff is valid UTF-8. A
Latin-1 or arbitrary non-NUL byte sequence can be treated as text by Git while
still failing UTF-8 decoding. The requirement to store the “exact UTF-8 diff”
is therefore impossible or ambiguous for a permitted repository state.

This matters to hashing as well as display. If one implementation decodes with
replacement characters while another preserves bytes, the review packet and
the recorded hash no longer identify the same evidence.

**Required contract change:** Define the byte and text representation
explicitly. The smallest MVP rule is:

- Obtain the diff as bytes using a deterministic Git invocation.
- Reject binary diffs using the documented binary policy.
- Decode remaining path/content data with strict UTF-8.
- On any decoding failure, return `UNSUPPORTED_REPOSITORY` before review.
- Hash the exact accepted byte sequence and build the packet from its strict
  UTF-8 decoding.

Apply the same rule after repair through P1-02. If reversible escaping is chosen
instead, the specification must define the exact encoding and model-visible
semantics; that is more complex than needed for the MVP.

**Tests to add:**

- Latin-1 content with no NUL byte is rejected deterministically.
- Invalid UTF-8 in a changed path or content is rejected.
- Valid multibyte Unicode paths and content succeed.
- The persisted bytes, recorded hash, and model-visible packet are proven to
  represent the same accepted diff.

### P2-04: The Codex runtime profiles do not fully close approval and writable-root behavior

**Affected clauses:** adapter runtime and packet policies around lines 322-328
and the driver permission claim around line 620.

There are two related omissions:

1. The packet-only Codex profile requires read-only sandboxing and ignored
   repository configuration, but does not explicitly require
   `--ask-for-approval never`. In a headless run, an attempted command outside
   policy can otherwise wait for approval until the general timeout fires.
2. The driver is described as able to modify only the isolated worktree, while
   Codex workspace-write policy can also expose platform temporary directories
   unless they are excluded or redirected. The effective write boundary is
   therefore broader than the prose claims.

**Required contract change:** Require the packet-only Codex profile to combine
read-only sandboxing with `--ask-for-approval never`. For the driver profile,
define the effective writable roots, not only the headline sandbox mode. Either
exclude slash-temp and the environment-selected temp directory using the Codex
workspace-write settings, or redirect `TEMP`, `TMP`, and `TMPDIR` to a
controller-owned scratch directory inside the isolated worktree. If scratch is
permitted, reserve it, exclude it from staging and review, bound it, and delete
it during normal cleanup.

Continue to rely on Codex protection for `.git`, but verify that protection in
the effective profile used by the pinned adapter version. Configuration tests
should assert behavior, not merely inspect command-line flags.

**Tests to add:**

- A packet-only agent attempts a write and a command requiring approval; it
  fails immediately without an interactive prompt.
- A driver attempts to write in the OS temp directory and outside the isolated
  worktree. Both fail unless the exact controller-owned scratch path is used.
- A driver cannot modify `.git` metadata directly.
- A permitted scratch file is never staged, hashed into the review diff, or
  left behind after successful cleanup.

### P2-05: Council anonymity does not cover model-generated self-identification

**Affected clauses:** blinded opening and deliberation behavior in COUNCIL-02
and COUNCIL-03 around lines 817-839 and the corresponding user-facing claims and
tests COUNCIL-002/003.

The controller correctly withholds the identity map and uses aliases. However,
the opening text itself is model-generated and copied completely into the
ledger. A model can write “As Claude…” or include its configured model/runtime
name. The specification currently has no deterministic validator or wording
qualification for that case, so “identity is not revealed” is stronger than
the implemented guarantee.

This cannot be solved reliably with semantic model-powered redaction because
that would add another judgment step and could alter substantive content.

**Required contract change:** Choose and document one product contract:

1. Define blindness as **controller nondisclosure** only. State explicitly that
   the controller never supplies the identity map, but cannot prevent a model
   from voluntarily self-identifying in its response.
2. Additionally reject exact configured provider, runtime, and model identifiers
   in all model-facing structured string fields, using a deterministic check.
   Even with this option, retain the controller-nondisclosure wording because
   exact-name filtering cannot recognize every paraphrase or nickname.

The first option is the simpler and more honest MVP contract. The second adds a
useful accidental-disclosure guard but should not be described as guaranteed
anonymity.

**Tests to add:**

- An opening fixture self-identifies using its exact runtime/model name.
- The run follows the selected reject-or-preserve policy deterministically.
- No packet sent to another model contains the controller's alias-to-identity
  map.
- User-facing metadata describes controller nondisclosure rather than absolute
  anonymity.

## 4. Additional contract clarifications

These items are lower priority than the findings above but are inexpensive to
settle while revising the data and failure contracts.

### 4.1 Nullable `RunRecord` fields need presence semantics

Fields typed as `str | None` without `= None` remain required in Pydantic v2.
That is compatible with a “present with null” contract, but conflicts with the
statement around line 493 that partial runs may omit nullable fields.

Choose one rule and test it:

- Require every field and serialize unavailable values as explicit `null`; or
- Give optional fields defaults so they may actually be omitted at validation.

The first option usually produces the more stable append-only record shape.

### 4.2 `FeedbackArtifact.findings` should use the versioned model

Typing `findings` as `list[dict]` bypasses the `NormalizedFinding` validation
that the artifact contract otherwise appears to promise. Use
`list[NormalizedFinding]` so persisted feedback cannot silently drift from the
schema consumed by repair.

### 4.3 Credential claims should match the redaction boundary

The definition of done says credentials are absent from prompts and artifacts,
while the redaction section correctly notes that arbitrary secrets embedded in
task text, repository source, or model prose are not discoverable in general.
Narrow the DoD claim to provider authentication files and the adapter's known,
allowlisted credential values. This keeps the success criterion testable.

### 4.4 YAML loading and variable expansion need a closed grammar

Require a safe YAML loader, reject custom tags, bound the configuration file
size, and specify the exact `${VAR}` expansion grammar. Define missing-variable,
empty-variable, escaping, and literal-dollar behavior. Otherwise configuration
parsers can diverge or accidentally acquire object-construction behavior.

### 4.5 Map every failure kind to an exit code

The category table leaves some named outcomes, such as packet overflow and
possibly no-change results, open to interpretation. Add an exhaustive mapping
from every `FailureKind` to exactly one exit code and terminal run status.

### 4.6 Make binary-diff inspection filename-safe

Any parsing of `git diff --numstat` should use its NUL-delimited form and avoid
line/tab splitting. Git filenames can contain tabs and newlines, so a text-line
parser can misclassify the changed file or fail to detect a binary marker.

## 5. Provider and platform validation

The v0.2 design is directionally consistent with current official Codex
documentation: read-only non-interactive use should disable approval prompts,
workspace-write has an effective writable-root policy beyond the repository,
Git metadata receives special protection, and network behavior is controlled by
the sandbox profile. The new requirements above make those external behaviors
explicit and testable rather than relying on defaults.

References checked for this review:

- [OpenAI configuration reference](https://learn.chatgpt.com/docs/config-file/config-reference)
- [OpenAI non-interactive mode](https://learn.chatgpt.com/docs/non-interactive-mode)
- [OpenAI agent approvals and security](https://learn.chatgpt.com/docs/agent-approvals-security)
- [Microsoft `AssignProcessToJobObject`](https://learn.microsoft.com/en-us/windows/win32/api/jobapi2/nf-jobapi2-assignprocesstojobobject)

These external behaviors were checked on 2026-08-27. The specification should
continue to pin adapter versions and retain executable fixture tests because CLI
flags, defaults, and configuration keys can change independently of this
document.

## 6. Recommended revision gate

Version 0.2 is close to an implementable Slice 0 contract, but implementation
should wait for the following changes:

1. Separate native CLI authentication from the environment visible to
   model-generated commands.
2. Apply one complete repository-state validation gate after both initial work
   and repair.
3. Require suspended Windows process creation before Job Object assignment.
4. Reject newly staged Git links authoritatively.
5. Bound streamed stdout/stderr before parsing or persistence.
6. Define strict text-diff encoding behavior.
7. Close the Codex approval and writable-root profiles.
8. Qualify the council anonymity guarantee.

Once those requirements and focused tests are incorporated, the remaining
clarifications can be completed as part of the schema/failure-contract slice
without changing the overall architecture.
