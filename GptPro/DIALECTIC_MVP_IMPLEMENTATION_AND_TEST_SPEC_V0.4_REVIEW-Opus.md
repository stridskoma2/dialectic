# Review: Dialectic MVP Implementation and Test Specification v0.4

**Reviewed document:** `DIALECTIC_MVP_IMPLEMENTATION_AND_TEST_SPEC_V0.4.md` (revision 0.4, 1617 lines)

**Prior reviews by this reviewer:** v0.1 (29 findings, 9 notes), v0.2 (10 findings),
v0.3 (8 findings), plus `DIALECTIC_ORCHESTRATION_AND_MCP_NOTE-Opus.md`

**Review date:** 2026-08-28

**Review question:** did revision 0.4 resolve the v0.3 findings, and does the new
`DialecticService` boundary — plus the control/scratch split and candidate-change
bounds — introduce problems of its own?

**Status:** advisory. Nothing in this file is normative until folded into the spec.

---

## 1. Summary verdict

Revision 0.4 resolves **all 8 findings** from the v0.3 review, and in three cases
the fix is better than what was asked for.

R3-1 asked for the Codex evidence to be split into offline and live halves. v0.4
does that, renames CODE-039/040 to say "Offline … construction fixture" in the test
name itself, adds `LIVE-CODE-001` and `LIVE-CODE-002` as pinned-native release
evidence, gates Slice 1's exit criterion explicitly ("CODE-039 and CODE-040 prove
construction only; native enforcement belongs to Slice 2"), and closes with the line
that settles the question: *"A fake adapter can never satisfy their claims."*

R3-2 asked for probe caching; v0.4 adds a versioned attestation keyed on eight
inputs, a separate `capability_probe_seconds` budget, an explicit re-probe on any
mismatch or managed-policy change, and CORE-030 to test the stale/corrupt/dynamic
cases. R3-5 asked that sampling stop being described as a bound; v0.4 says
"Sampling is not a filesystem quota" and makes the post-exit check authoritative.

The MCP note was also absorbed, and §10.1 goes materially further than it proposed —
idempotency via `client_request_id`, authorization established outside
model-authored arguments (with the excellent "`authorized: true` is never evidence
of approval"), separable read/council/code permissions, and a prohibition on a thin
adapter hiding a daemon. Recording the design without adding a dependency, command,
or test is the right call.

**No blocking findings.** The five below cluster around the one genuinely new
structural element: the `DialecticService` boundary is asserted in five places but
its ownership of run-record creation and configuration loading contradicts §6.3
(R4-1). The rest are a numeric edge case, one unqualified claim, and two test gaps.

Counts: 0 blockers, 2 medium, 1 low-medium, 2 low.

---

## 2. Disposition of the v0.3 findings

Each row checked against the v0.4 text.

| v0.3 finding | Status | Resolved by |
|---|---|---|
| R3-1 CODE-040 cannot be offline and prove the Codex sandbox | Resolved | CODE-039/040 renamed to offline construction fixtures; `LIVE-CODE-001`/`002` added in §11.7 as platform-gated manual release evidence excluded from the offline count; Slice 1 and Slice 2 exit criteria both amended; the two DoD lines now separate construction evidence from pinned-native behavior evidence |
| R3-2 Per-run probe cost, no caching | Resolved | §5.4.1 step 7 validates a cached attestation or probes; attestation keyed on runtime, executable identity, CLI version, platform, fixture version, profile-template hash, probe-result hash; `capability_probe_seconds: 120` is a separate budget; `dial doctor` noted as optional; CORE-030 |
| R3-3 Windows has no async I/O strategy | Resolved | §5.4.5 specifies one dedicated blocking reader thread per pipe feeding the loop via `loop.call_soon_threadsafe`, draining continued during graceful termination, and every thread/handle joined or closed before the turn returns; Slice 0 lists bounded Windows reader threads; CORE-028 extended to concurrent stream draining plus overflow |
| R3-4 "OS-minimal baseline" undefined | Resolved | §5.4.3: "The conceptual baseline is empty," with fixtures required to name `SystemRoot` on Windows and `PATH`/`HOME`/locale on POSIX explicitly; the effective name set is recorded |
| R3-5 Scratch enforcement is sampling described as a bound | Resolved | §5.4.2 restates it as a best-effort in-flight detector with an authoritative post-exit check and states "Sampling is not a filesystem quota"; CORE-027 asserts the final check is authoritative and sampling is reported only as detection |
| R3-6 Pre-preflight failures leave uncleaned run directories | Resolved | §10 adds a bullet requiring `dial status` to print the run-artifact directory and the README to identify its platform parent, state that failed/cancelled runs are deliberately retained and sensitive, and explain safe manual removal; §12 README line extended to artifact/run-directory locations |
| R3-7 Artifact trees do not map filenames to schemas | Resolved | §6.1 adds a complete path-pattern-to-schema binding table, plus named wrapper models (`AgentRequestArtifact`, `ReviewReportArtifact`, `OpeningPositionArtifact`, `CouncilRevisionArtifact`, `CandidateConclusionArtifact`) and the `raw/` naming convention |
| R3-8 "Not a Git repository" has no failure kind | Resolved | `UNSUPPORTED_REPOSITORY` now reads "The supplied path is not a non-bare Git working tree, or its initial structure/state is unsupported…"; `PREFLIGHT_FAILED` narrowed to "stable identity for an otherwise supported repository"; CODE-046 |

The v0.3 residual note is also resolved: §12 requires the README to document the
"ignored build-output expectation."

---

## 3. Findings

### R4-1 — The `DialecticService` boundary contradicts §6.3 on who loads configuration and creates the run record

**Refers to:** §2.1 CLI-ingress bullet (line 45); §5.2 component table (lines
263-265) and the boundary paragraph (line 283); §6.3 run-record creation paragraph;
§10.1 first bullet (line 1237); CORE-026.

**Severity:** medium. No MVP runtime consequence — the CLI is the only ingress — but
it corrupts the boundary that §10.1 and §14 build the entire future ingress story
on, and two implementers would resolve it differently.

Three statements cannot all hold:

1. **§5.2, line 264.** `DialecticCLI` "Parse[s] the human CLI surface, **load[s]
   named local files**, and invoke[s] `DialecticService`; contains no workflow
   logic."
2. **§5.2, line 283.** "The service accepts already validated, bounded domain
   requests rather than CLI argv, **configuration-file paths**, executable paths, or
   provider credentials."
3. **§6.3.** "After Typer has identified a valid command/mode, **the controller
   creates the explicit-null `CREATED` record before loading the user-named
   configuration and input files**; subsequent input/config errors therefore persist
   as `INVALID_INPUT`."

Statements 1 and 2 put file loading and configuration validation *above* the service
boundary, in the ingress. Statement 3 puts run-record creation *before* configuration
loading. Together they force the CLI to create and persist the `CREATED` record and
then persist `INVALID_INPUT` into it — which §10.1's first bullet forbids in as many
words: "only `DialecticService` and the controller may … persist evidence."

The alternative reading is no better. If the CLI validates configuration before
calling the service and simply exits on failure, then no run record exists for an
`INVALID_INPUT`, which contradicts §6.3 directly and leaves CORE-026 — the
table-driven test requiring every `FailureKind` to produce "exactly the status and
exit code in section 6.3" — with no persisted status to assert for that row.

There is also a smaller loose end in the same area: §5.2's component table lists
`ConfigLoader` without saying which side of the boundary it sits on, and the service's
enumerated use cases are Code Once, Council Once, status, and result — none of which
is "create a run."

This matters precisely because it is not an MVP problem. §14's three-step MCP plan
requires each step to be "a thin ingress over `DialecticService`" that may not
"duplicate controller logic." If configuration parsing, environment expansion, limit
validation, and `INVALID_INPUT` classification live in `DialecticCLI`, then step 2 of
that plan must reimplement all of it, which is the exact outcome the boundary was
introduced to prevent.

**Recommended fix.** Draw the line at *bytes*, not at parsed objects:

1. `DialecticCLI` resolves and validates human-supplied **paths**, reads the
   configuration and task/prompt files as bounded byte strings, and passes those
   bytes plus the mode to the service. Path handling is a genuine ingress concern and
   §2.1 already assigns it there.
2. `DialecticService` owns everything downstream: creating the `CREATED` record,
   invoking `ConfigLoader`, environment expansion, limit and count validation, and
   persisting `INVALID_INPUT` on failure. Add an explicit entry point for it so §5.2
   lists a use case that can produce a run record.
3. Amend §5.2 line 283 to say the service accepts bounded byte payloads and typed
   identifiers rather than filesystem paths — which is the actual invariant worth
   protecting — instead of "already validated" requests.
4. State in §5.2 that `ConfigLoader` is service-side.

A future MCP ingress then supplies bytes it obtained from its own registered profile
catalog, and inherits validation and failure classification unchanged.

**Test to add:** assert that an invalid configuration produces a persisted run record
with `status=FAILED`, `failure_kind=INVALID_INPUT`, and exit 2 — reached through the
service boundary, not the CLI handler. CORE-026 already needs this row; make its
provenance explicit.

### R4-2 — The stream-bound floor cannot accommodate the credential guard plus truncation marker

**Refers to:** §5.4.3 (line 400, credential minimum length); §5.4.5 (lines 436 and
438, truncation marker and trailing guard); §4 ceilings table
(`max_agent_stdout_bytes` 256..67108864, `max_agent_stderr_bytes` 256..16777216);
CORE-027.

**Severity:** medium. A configurable combination produces undefined behavior in the
credential-safety path.

On overflow the persisted diagnostic must satisfy three requirements at once:

- it retains a deterministic prefix and "the persisted stream remains within the
  configured limit";
- it appends the fixed marker `<dialectic:truncated>\n` — 22 bytes;
- it first discards "an unpersisted trailing guard **at least as long as the longest
  known credential byte sequence minus one**."

The reserved space is therefore `22 + (longest_credential − 1)` bytes. But
credentials are bounded only *below* — §5.4.3 requires at least eight Unicode scalar
values and sets no ceiling — while the stream bounds are floored at a constant 256.

With a 300-byte credential and `max_agent_stdout_bytes: 256`, the guard alone exceeds
the entire cap and the required arithmetic is negative. The spec does not say what
happens: whether the guard is truncated (reintroducing the boundary-fragment risk the
guard exists to prevent), whether the marker is dropped, or whether the run fails.
Long credentials are not exotic — service-account tokens and JWTs with embedded
claims routinely exceed 256 bytes.

CORE-027 tests "a credential split across capture chunks/overflow boundary," but not
the case where the guard cannot fit inside the configured cap.

**Recommended fix.** Make the relationship explicit rather than leaving two
independent floors:

1. At preflight, once the credential values in scope are known, require each
   configured stream bound to satisfy
   `bound >= len(marker) + (longest_credential_bytes − 1) + minimum_useful_prefix`.
   Fail as `PREFLIGHT_FAILED`, naming the field, the credential name (never its
   value), and the required minimum.
2. Alternatively, add an explicit `max_credential_bytes` ceiling to §5.4.3 and derive
   the stream floors from it in the ceilings table, so the constraint is visible in
   configuration rather than computed at runtime.

Option 1 is preferable: the constraint depends on the deployment's actual
credentials, and a static ceiling would reject legitimate long tokens.

**Test to add:** extend CORE-027 with a credential longer than the configured stream
bound. Preflight fails with the field and required minimum named; no model runs.

### R4-3 — The worktree-path claim is unqualified while alias anonymity is qualified

**Refers to:** CODE-05 (line 904, "The controller does not supply reviewers with …
The target repository or worktree path"); CODE-006 (line 1318); CODE-03 (line 845,
the driver receives the worktree path); compare CODE-08 and COUNCIL-03, which do
carry the qualification.

**Severity:** low-medium. An asymmetry in how carefully two structurally identical
claims are stated.

Revision 0.4 is admirably careful about semantic anonymity. CODE-05 itself says
"Dialectic does not claim semantic anonymity: a model may identify or speculate about
itself in authored prose, and the controller preserves that content after ordinary
redaction." CODE-08 repeats it ("This is controller nondisclosure, not guaranteed
semantic anonymity"), COUNCIL-03 repeats it for the ledger, and CODE-042 and
COUNCIL-031 test it.

The worktree-path claim has exactly the same structure and no equivalent
qualification. The driver receives the isolated worktree path by design (CODE-03).
Whatever the driver writes into product files enters the staged diff, and that diff
is the review packet. A driver that writes `# generated in
/state/worktrees/20260827T142355Z-k7m2q4v5wx` into a source file — or emits a build
manifest, a lockfile with absolute paths, or a symlink whose target is an absolute
path outside the worktree (see R4-4) — puts the path in front of every reviewer.

CODE-006's assertion is sentinel-based ("Both are absent from reviewer prompt, argv,
and packet artifact"), which tests what the *controller* injects. That is the right
test; the claim above it just needs to describe the same scope.

**Recommended fix.** Add one sentence to CODE-05, mirroring the wording already used
two paragraphs earlier: the controller never supplies the repository or worktree path
in a prompt, argv, environment override, or packet artifact, but content authored by
the driver may contain it, and Dialectic does not inspect or rewrite product content
to remove it. Mention it in the README's trusted-local-process boundary section
alongside the existing nondisclosure caveats.

### R4-4 — Product symlinks are a supported change class with no test

**Refers to:** CODE-04 step 3 ("Each present changed entry must be a supported regular
file or symlink … the sum of logical sizes across present changed files/symlink
payloads"); CODE-044 (scratch and control-path symlinks only); §11.4 generally.

**Severity:** low.

Step 3 explicitly admits symlinks as product content and accounts for their payloads
in the aggregate bound, so they are a first-class supported change class. The Code
Once table covers binary changes (CODE-024), gitlinks (CODE-036), filtered paths
(CODE-034), invalid UTF-8 paths and content (CODE-037), size and count bounds
(CODE-045), and scratch-directory symlink and reparse attacks (CODE-044) — but no
case where the *product* change is a symlink.

Two behaviors go unasserted. First, that `git add -A` records mode `120000` with the
target as blob content rather than following the link and copying the target's
contents into the repository — the correct behavior, but the one worth pinning given
how much care step 3 takes with no-follow inspection. Second, that an absolute
target pointing outside the worktree appears in the diff as a path string, which is
the concrete instance of R4-3.

**Test to add:** `CODE-047` — the driver adds a relative in-tree symlink and an
absolute out-of-tree symlink. Both are staged as mode-`120000` entries whose blob
content is the target path; no target content is read or copied; the aggregate size
accounting matches; and the out-of-tree target path appears in the diff, documenting
the R4-3 nondisclosure scope rather than being silently filtered.

### R4-5 — No test covers the new `dial status` run-directory requirement

**Refers to:** §10 (line 1231, "`dial status` MUST print the run-artifact
directory"); CORE-021, CORE-022, CORE-023.

**Severity:** low.

The requirement is new in v0.4 and is the discoverability half of the R3-6 fix — the
README explains where run directories live, and `dial status` is what points a user
at the specific one. The three `dial status` tests cover an unknown ID, faithful
display of `RUNNING`/`FINALIZED`/`FAILED` records with exit 0, and corrupt records
with exit 3. None asserts that the run-artifact directory is printed.

This is minor on its own, but the spec's own standard is CORE-026: no enum member
lacks a trigger test. A `MUST` in §10 with no assertion anywhere is the same class of
omission, and it is the one users encounter first when a run fails.

**Recommended fix.** Extend CORE-022 to assert that the printed output contains the
absolute run-artifact directory for each status, or add a short dedicated case.

---

## 4. Suggested order of work

1. **R4-1** — settle the boundary before Slice 0 delivers `DialecticService`, since
   it determines where `ConfigLoader`, run-record creation, and `INVALID_INPUT`
   classification live, and CORE-026 depends on the answer.
2. **R4-2** — a preflight validation rule plus one CORE-027 case; cheap now, and it
   sits in the credential-safety path where undefined behavior is least acceptable.
3. **R4-3** — one sentence in CODE-05 and one README line.
4. **R4-4, R4-5** — two test rows.

None of these blocks starting Slice 0, and none changes the workflow.

---

## 5. Finding index

| ID | Severity | Title |
|---|---|---|
| R4-1 | medium | `DialecticService` boundary contradicts §6.3 on config loading and run-record creation |
| R4-2 | medium | Stream-bound floor cannot fit the credential guard plus truncation marker |
| R4-3 | low-medium | Worktree-path claim unqualified while alias anonymity is qualified |
| R4-4 | low | Product symlinks are a supported change class with no test |
| R4-5 | low | New `dial status` run-directory requirement has no test |
