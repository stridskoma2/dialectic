# Review: Dialectic MVP Implementation and Test Specification v0.3

**Reviewed document:** `DIALECTIC_MVP_IMPLEMENTATION_AND_TEST_SPEC_V0.3.md` (revision 0.3, 1449 lines)

**Prior reviews by this reviewer:** `DIALECTIC_MVP_SPEC_V0.1_REVIEW-Opus.md` (29 findings, 9 notes) and
`DIALECTIC_MVP_IMPLEMENTATION_AND_TEST_SPEC_V0.2_REVIEW-Opus.md` (10 findings)

**Review date:** 2026-08-28

**Review question:** did revision 0.3 resolve the v0.2 findings, and does the new
security and validation machinery introduce problems of its own?

**Status:** advisory. Nothing in this file is normative until folded into the spec.

---

## 1. Summary verdict

Revision 0.3 resolves **all 10 findings** from the v0.2 review, including the
blocking one. `overall_vote` is gone from `CouncilBallot` entirely; the controller
derives it and persists a `DerivedBallot` alongside the model's original. That is
the correct fix rather than the minimum one.

The revision also absorbs a second Codex/Sol review round, adding a credential
boundary, a shared `ChangeValidator` that runs after both writable turns, and a
race-free Windows process-creation sequence. The offline suite grew from 86 to 103
cases (28 core, 43 Code Once, 32 Council Once — count verified against the tables).

Two things stand out as unusually good. `CORE-026` is a table-driven meta-test
requiring every declared `FailureKind` to have a triggering test, which structurally
prevents the enum-drift problem that both earlier revisions had. And `CODE-043`
tests the staged-versus-committed byte and hash equality assertion including a
mutate-between-validation-and-confirmation race, which is a level of paranoia the
diff-integrity chain actually warrants.

**No blocking findings.** The residual issues are concentrated in one area: the
v0.3 security hardening added real native-process behavior requirements without
resolving how they fit the offline test strategy, the preflight budget, or the
Windows I/O model. R3-1 through R3-4 are all consequences of that. None of them
changes the design; they change what Slice 0 through Slice 2 must actually deliver,
and R3-1 in particular affects whether a definition-of-done item is genuinely
demonstrated or only asserted.

Counts: 0 blockers, 2 medium-high, 2 medium, 2 low-medium, 2 low, 1 residual note.

---

## 2. Disposition of the v0.2 findings

Each row checked against the v0.3 text.

| v0.2 finding | Status | Resolved by |
|---|---|---|
| N1 Derived `overall_vote` can kill the run on a model's slip | Resolved | COUNCIL-05 removes `overall_vote` from `CouncilBallot`; adds `DerivedBallot` with controller-computed `derived_overall_vote`; both artifacts retained; "Model prose is never reparsed to alter this derivation." COUNCIL-023 rewritten to assert the four derivations |
| N2 No `max_propositions` | Resolved | `max_propositions: 8` with a 1..20 hard ceiling; COUNCIL-04 states the rationale explicitly ("not merely a payload-size limit"); COUNCIL-030 |
| N3 Driver-added binary fails as `UNSUPPORTED_REPOSITORY` | Resolved | `UNSUPPORTED_CHANGE` added to `FailureKind` with its own trigger row, cleanly split from `UNSUPPORTED_REPOSITORY` (initial repository state); CODE-024, CODE-034, CODE-035, CODE-036 |
| N4 Staging scope ambiguous about ignored files | Resolved | CODE-04 step 2 says "staged, unstaged, and untracked non-ignored product paths" and "Untracked ignored files are excluded"; the driver prompt now forbids build output; CODE-038 |
| N5 Five `FailureKind` values have no normative trigger | Resolved | Section 6.3 adds a complete trigger/status/exit table for all 18 kinds; CODE-03 and CODE-09 add explicit turn-success definitions; CORE-026 makes the completeness a test |
| N6 `phase` untyped; `CREATED`/`FINALIZED` collide with `RunStatus` | Resolved | `CodePhase` and `CouncilPhase` are typed and mode-validated; `CREATED`/`FINALIZED` removed from the phase enums; `phase=null` valid only while `CREATED`; sections 7.2 and 8.2 are transition tables labeled as phases; CORE-025 |
| N7 `UNANIMOUS`'s blocking clause unreachable-redundant | Resolved | Rule 1 now reads "`UNANIMOUS` when `A == N`" with an explanatory note that the derived-vote rule makes `B` impossible there |
| N8 Reserved turn directory has no name | Resolved | Named exactly `.dialectic-turn/` at the worktree root, with absence proof, private creation, `TMP`/`TEMP`/`TMPDIR` redirect, size monitoring, `finally` removal, and absence re-proof before Git inspection |
| N9 Lock key canonicalization undefined | Resolved | Lock keyed on a stable filesystem identity — `(st_dev, st_ino)` after realpath on POSIX, `(volume_serial, file_index)` on Windows — not path spelling; `WorkspaceRecord` records it; CODE-041 tests symlink/junction/drive-case spellings |
| N10 COUNCIL-010 incorrect at documented `max_dissenters` | Resolved | COUNCIL-010 now names `max_dissenters=0`; COUNCIL-032 adds the complementary `max_dissenters=1` rough-consensus case |

Also resolved without being raised: `core.quotePath` flipped to `false`, so non-ASCII
filenames reach reviewers readable rather than octal-escaped.

---

## 3. Findings

### R3-1 — CODE-040 cannot be both an offline Slice 1 test and a proof of the Codex sandbox

**Refers to:** test CODE-040 and CODE-039 (section 11.4); Slice 1 exit criterion
(section 13); section 11.1 layer rules; section 12 Engineering definition of done;
section 5.4.1 step 7.

**Severity:** medium-high. A definition-of-done item may be asserted rather than
demonstrated.

CODE-040 expects that "every forbidden operation fails without prompting" and that
"project `.codex` config is ignored while `AGENTS.md` remains discoverable." Those
are properties of the **Codex CLI's own sandbox and configuration loader**, not of
Dialectic's controller. A scripted or fake process can only demonstrate that the
controller passed the intended flags and constructed the intended profile; it
cannot demonstrate that Codex honors them.

But Slice 1's exit criterion is "CODE-001 through CODE-043 pass with no native AI
CLI installed," and section 11.1 requires that all mandatory tests run "without
network access and without provider credentials." Codex cannot satisfy CODE-040's
assertions under those conditions.

The consequence reaches the definition of done, which claims "A fixture-declared
native authentication value can reach the trusted Codex CLI but cannot be observed
by its model-generated child command." If the only test behind that line is a fake,
the claim is about the controller's intent, not the observed system.

Revision 0.3 already solved this exact problem once, correctly, for the Windows
launcher: Slice 0's exit criterion reads "CORE-001 through CORE-028 pass without Git
or native agents, **except the platform-gated Windows launcher contract in
CORE-028**." CODE-039 and CODE-040 need the same treatment.

**Recommended fix.**

1. Split each into two tests. The offline half (Slice 1, fake process) asserts what
   the controller constructs: the environment it builds, the names it includes and
   omits, the permission profile and denied paths it passes, the scratch redirect,
   and the removal of `.dialectic-turn/` before validation.
2. The live half (Slice 2, `live` marker, opt-in) asserts what the native CLI
   actually does: forbidden reads fail, the credential is invisible to a
   model-generated child, `.codex` project config is ignored, `AGENTS.md` is still
   discovered.
3. Gate Slice 1's exit criterion the way Slice 0 gates CORE-028, naming the excepted
   cases.
4. Reword the two definition-of-done lines to say which half proves what, so the
   trusted-CLI claim is not carried by a fixture.

This is bookkeeping, not a design change — but as written, the strongest security
claim in the document rests on a test that cannot run under the conditions its own
slice specifies.

### R3-2 — The per-run capability probe has unbudgeted cost and no caching

**Refers to:** section 5.4.1 step 7; section 5.4.3 final paragraph; section 5.4.4
("A versioned behavior probe MUST demonstrate…"); `preflight_seconds: 30`.

**Severity:** medium-high.

Preflight must "prove the required effective permission and child-environment
behavior with the adapter's versioned local capability probe," and section 5.4.4
requires the probe to demonstrate a list of successful and denied operations —
worktree write, `.dialectic-turn/` write, and failures for original-repository,
Git-metadata, saved-auth, state-root, pre-redirect-temp, outside-workspace, and
network access. Section 11.6 reinforces that argv comparison is insufficient.

That is the right standard for proving the boundary. But as written it runs **on
every `dial code` invocation**, before any product work, inside a 30-second
preflight budget, and against a real authenticated CLI — so it costs provider
tokens and cold-start latency on every run, and it is the kind of check that turns
a fast local command into a slow one. Nothing in the document caches the result,
even though the inputs are stable: CLI version, platform, resolved permission
profile, and fixture identity.

**Recommended fix.**

1. Cache the probe result keyed on (runtime, resolved executable path, CLI version,
   platform, permission-profile hash, fixture version). A cache hit skips the probe;
   any input change re-runs it.
2. Store the cache under the state root with its own artifact schema, so a stale or
   unreadable entry fails closed to a re-probe rather than to a skip.
3. Give the probe its own budget separate from `preflight_seconds`, since a cold
   Codex start plus a denial sweep against a 30-second ceiling shared with
   executable resolution, authentication, and repository checks is tight.
4. Alternatively, expose it as an explicit `dial doctor` command that the README
   directs users to run after a CLI upgrade, with a cached attestation consulted at
   preflight.

### R3-3 — Windows has two execution paths and no described async I/O strategy

**Refers to:** section 5.1 ("`asyncio.create_subprocess_exec` for **POSIX** native
CLI execution" plus a pywin32/ctypes Win32 launcher); section 5.4.5 (concurrent
incremental draining under byte bounds); section 9 (Windows creation sequence);
Slice 0 deliverable "Cross-platform process-tree and repository-lock abstractions."

**Severity:** medium.

Revision 0.3 correctly concludes that Windows needs `CreateProcessW` with
`STARTUPINFOEXW`, `PROC_THREAD_ATTRIBUTE_JOB_LIST`, and `CREATE_SUSPENDED` to close
the create-then-assign gap — `asyncio.create_subprocess_exec` cannot express that.
The consequence is that Windows no longer goes through asyncio's subprocess
transport at all, so it does not inherit asyncio's pipe reading either.

Section 5.4.5 then requires the supervisor to "drain stdout and stderr concurrently
and incrementally" and forbids "an API that accumulates either stream without
enforcing those bounds." On Windows, with raw pipe handles created outside asyncio,
that requires either overlapped I/O with completion handling or dedicated reader
threads bridged back to the event loop. The document specifies neither, and Slice 0
lists the work as an "abstraction," which understates it: this is two independent
process-execution implementations, only one of which the ecosystem provides.

The 4 Hz scratch monitor and `graceful_kill_seconds` both interact with whichever
mechanism is chosen, so the choice is not private to the launcher.

**Recommended fix.** Specify the Windows read strategy — reader threads feeding
bounded buffers via `loop.call_soon_threadsafe`, or overlapped reads — state how it
reports back into the async orchestration, and describe how draining continues
during graceful termination so an overflow diagnostic prefix is still captured.
Then split the Slice 0 deliverable into POSIX and Windows execution paths so the
estimate reflects the work.

### R3-4 — The "OS-minimal baseline" environment is undefined

**Refers to:** section 5.4.3 ("The controller builds the native CLI environment from
an OS-minimal baseline plus only the fixture's required non-secret names and
credential names actually used. No other controller environment is inherited.").

**Severity:** medium.

The credential boundary is otherwise precise — exact-on-POSIX and
case-insensitive-on-Windows name comparison, proxy variables classified as
credentials, an eight-character floor, a Codex `shell_environment_policy` with an
explicit `exclude` filter. The baseline itself is the one undefined term, and it is
the one that determines whether the CLI starts at all.

A Windows process launched with a near-empty environment typically fails obscurely
without `SystemRoot` and `windir`; `COMSPEC`, `PATHEXT`, `PATH`, `TEMP`/`TMP`, and
`NUMBER_OF_PROCESSORS` are commonly assumed by runtimes. On POSIX, `PATH`, `HOME`,
and the locale variables matter — and note that section 5.4.3's minimal environment
must coexist with CODE-04's requirement to run Git with `LC_ALL=C`. The failure mode
is not a security hole; it is a cross-platform startup failure that surfaces as an
unexplained non-zero exit inside `DRIVER_FAILED`.

**Recommended fix.** Either enumerate the per-platform baseline in section 5.4.3, or
declare the baseline empty and require each adapter fixture to enumerate every
non-secret name its CLI needs — the second is more testable and matches the
fixture-owns-the-contract design already used for credential names and saved-auth
paths. Add a contract test asserting each CLI starts under exactly its declared
environment and no more.

### R3-5 — Scratch enforcement is sampling described as a bound

**Refers to:** section 5.4.2 ("monitors regular-file logical size without following
symlinks at least four times per second and again after the process exits.
Exceeding `max_turn_scratch_bytes` terminates the process tree and fails as
`AGENT_OUTPUT_TOO_LARGE`").

**Severity:** low-medium.

Polling at 4 Hz cannot bound a burst — a process can write far past
`max_turn_scratch_bytes` inside a 250 ms window — so the stated limit is a
detection threshold, not an enforced ceiling. The stream bounds in section 5.4.5 are
genuinely enforced because the controller is the reader; the scratch bound is not,
because the filesystem is. The post-exit check is the only authoritative gate.

Recursive stat of a scratch tree four times a second also has its own cost during
every driver turn.

**Recommended fix.** Restate it as a best-effort in-flight detector with the
post-exit measurement as the authoritative check, so `AGENT_OUTPUT_TOO_LARGE` is not
read as a hard guarantee that no more than N bytes were ever written. If a real
ceiling is wanted later, note the platform mechanisms (a quota-backed volume, or a
size-capped scratch image) as post-MVP rather than implying the poll achieves it.

### R3-6 — Runs that fail before preflight still leave directories nothing cleans up

**Refers to:** section 6.3 ("the controller creates the explicit-null `CREATED`
record before loading the user-named configuration and input files; subsequent
input/config errors therefore persist as `INVALID_INPUT`"); section 10 (no automatic
cleanup); section 12 README requirements.

**Severity:** low-medium.

Creating the record first is the right call — it makes `INVALID_INPUT` inspectable
via `dial status` and keeps the exit-code contract uniform. The consequence is that
every mistyped configuration, every missing task file, and every bad limit leaves a
run directory under the state root, and section 10 forbids automatic cleanup.

Section 10 and the definition of done require the README to document cleanup
commands, but only the Git ones: `git worktree remove`, `git branch -D`,
`git worktree prune`. Nothing addresses the runs root, which is where the
accumulation actually happens, and which is also where section 6.2 requires
`0700`/DACL-protected artifacts users are told to treat as sensitive.

**Recommended fix.** Extend the README cleanup requirement to the runs root:
where it lives per platform, that failed and cancelled runs are retained
deliberately, that its contents are sensitive, and how to remove old runs safely.
A `dial status` line pointing at the run directory would make this discoverable at
the moment it matters.

### R3-7 — The artifact trees do not map filenames to schema types

**Refers to:** section 6 code and council trees; section 6.1 models.

**Severity:** low.

Section 6.1 now types every controller-owned artifact, but the trees in section 6
are bare filenames, so several bindings are left to inference:
`reviews/reviewer-a.json` (a `ReviewReport`, or a wrapper carrying the alias?),
`council/ballots/participant-a.json` (a `CouncilBallot`, or the `DerivedBallot` that
embeds it?), `council/opening/participant-a.json`, and `council/raw/`, which is shown
with no contents at all while the code tree's `reviews/raw/` shows its naming.

The ballot case is the one that matters, because COUNCIL-05 requires both the
original and the derived artifact to be retained and the tree shows one file per
participant. `DerivedBallot.ballot` embedding `CouncilBallot` makes one file
sufficient — but the reader has to work that out.

**Recommended fix.** Add a filename-to-model column or an annotation beside each
tree, and show the council `raw/` naming convention as the code tree does.

### R3-8 — "Not a Git repository" has no assigned failure kind

**Refers to:** CODE-01 steps 3, 4, and 7; section 6.3 trigger rows for
`PREFLIGHT_FAILED` and `UNSUPPORTED_REPOSITORY`.

**Severity:** low.

CODE-01 resolves and records the Git common directory (step 3) and derives its
filesystem identity (step 4) before confirming that the path is a non-bare Git
working tree (step 7). A path that is not a repository at all therefore fails at
step 3, where `PREFLIGHT_FAILED` covers "stable repository identity," while step 7
and the `UNSUPPORTED_REPOSITORY` row ("bare, dirty, sparse, submodule/gitlink,
tracked filter/LFS, or otherwise outside the declared repository subset") also
plausibly apply.

Both map to exit 2, so user impact is limited to the diagnostic — but section 6.3
opens by asserting that every failure kind has *one* normative trigger, and
CORE-026 will need a single expected answer for this case.

**Recommended fix.** Either move the non-bare-working-tree confirmation ahead of the
common-directory resolution, or state in the `UNSUPPORTED_REPOSITORY` row that a
path which is not a Git working tree is classified there, with `PREFLIGHT_FAILED`
reserved for a repository that is valid but whose stable identity cannot be
obtained.

---

## 4. Residual note, not a defect

CODE-04 step 2 now states that the driver prompt forbids build output and that "a
non-ignored generated artifact is an ordinary proposed change and remains subject to
every check below," and CODE-038 tests the ignored case. That closes v0.2's N4.

The residual behavior is still sharp: in a repository whose `.gitignore` does not
cover its own build artifacts, a driver that follows CODE-03's instruction to "run
whatever narrow checks it considers appropriate" produces non-ignored bytecode,
which fails the whole run as `UNSUPPORTED_CHANGE` — after the driver turn has been
paid for. This is now deliberate and documented rather than accidental, which is the
right outcome for the MVP. It deserves one line in the README's limitations so the
first user to hit it recognizes it as designed: Dialectic expects the target
repository to ignore its own build output.

---

## 5. Suggested order of work

1. **R3-1** — it changes two test rows, a slice exit criterion, and two
   definition-of-done lines, and it determines whether the credential-boundary claim
   is demonstrated or asserted. Settle it before Slice 1 is called complete.
2. **R3-3, R3-4** — both are Slice 0 and Slice 2 scoping. R3-3 in particular may
   change the estimate for the process-supervision work.
3. **R3-2** — needed before the first real-CLI run, and it is easier to design the
   cache now than to retrofit it once preflight is written.
4. **R3-5, R3-6, R3-8** — wording and README precision; no code consequences beyond
   one classification choice.
5. **R3-7** — documentation clarity in section 6.

None of these blocks starting Slice 0.

---

## 6. Finding index

| ID | Severity | Title |
|---|---|---|
| R3-1 | medium-high | CODE-040 cannot be offline and also prove the Codex sandbox |
| R3-2 | medium-high | Per-run capability probe has unbudgeted cost and no caching |
| R3-3 | medium | Windows has two execution paths and no async I/O strategy |
| R3-4 | medium | "OS-minimal baseline" environment is undefined |
| R3-5 | low-medium | Scratch enforcement is sampling described as a bound |
| R3-6 | low-medium | Pre-preflight failures leave run directories nothing cleans up |
| R3-7 | low | Artifact trees do not map filenames to schema types |
| R3-8 | low | "Not a Git repository" has no assigned failure kind |
