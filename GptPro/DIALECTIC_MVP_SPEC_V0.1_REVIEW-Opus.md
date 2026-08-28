# Review: Dialectic MVP Implementation and Test Specification v0.1

**Reviewed document:** `DIALECTIC_MVP_IMPLEMENTATION_AND_TEST_SPEC_V0.1.md` (v0.1, 897 lines)

**Review date:** 2026-08-27

**Review question:** can a competent engineer build exactly this without guessing,
and will the result behave as the document claims?

**Status:** advisory. Nothing in this file is normative until folded into the spec.

---

## 1. Summary verdict

The specification is unusually disciplined. The controller-owns-everything boundary
(Git, state transitions, timeouts, consensus arithmetic), fail-closed schema
validation with no model-powered format repair, the blind-then-anonymized council
structure, and the exact-call-count tests that prove the absence of loops are all
correct choices and are stated precisely. The slice plan is honest about what is
deferred.

It is close to implementable. Blocking it are:

- **two platform assumptions that do not hold on Windows** (section 3), which will
  stop Slice 2 outright and which section 12 of the spec makes part of the
  definition of done;
- **one consensus rule that is arithmetically wrong** and can report agreement on a
  unanimous rejection (F-C1);
- **a set of terminal statuses that no single normative list defines**, scattered
  across five sections and two test tables, with no exit-code mapping despite a
  test asserting exit-code equivalence (F-U1).

The remainder are under-specifications: real, but each is a paragraph of prose
rather than a design change.

Counts: 2 blockers, 7 correctness defects, 17 under-specifications, 3 test-plan
gaps, 9 smaller notes.

---

## 2. How the platform claims were checked

Findings F-B1, F-B2, and F-C4 assert things about Windows and Git behaviour. They
were verified empirically on the target platform (Windows 11, Python 3.11.9,
git 2.55.0.windows.3) rather than asserted from memory. Raw observations are
reproduced inline with each finding so they can be re-run. Every other finding is
a statement about the document's internal consistency and is checkable by reading
the cited lines.

One caveat: the probes ran under Python 3.11, while the spec requires 3.12+. The
two behaviours probed are `CreateProcess` characteristics surfaced through
`subprocess`, unchanged in 3.12, but re-running them under 3.12 before acting is
cheap and advisable.

---

## 3. Blockers

Fix these before Slice 2 begins. Both concern how a native CLI is invoked, so both
land in the same place: the `AgentAdapter` contract in section 5.4.

### F-B1 — A 256 KB review packet cannot be passed on a Windows command line

**Refers to:** section 4 `max_diff_bytes: 262144` (line 146); section 5.1
`asyncio.create_subprocess_exec` (line 169); section 5.1
"executable-plus-argument arrays" (line 178); section 12 "All mandatory tests pass
offline on Windows and Linux".

**Severity:** blocker.

The spec forbids `shell=True` and mandates argument arrays — correct — but never
says how the prompt reaches the child. The obvious reading of
"executable-plus-argument arrays" is that the prompt is an argv element. On
Windows, `CreateProcess` caps the *entire* command line at 32767 characters, so a
review packet anywhere near `max_diff_bytes` cannot be delivered that way.
Measured:

```text
ARGV   30000 -> rc=0 out=b'30000'
ARGV   32000 -> rc=0 out=b'32000'
ARGV   33000 -> FileNotFoundError: [WinError 206] The filename or extension is too long
ARGV   40000 -> FileNotFoundError: [WinError 206] The filename or extension is too long
ARGV  262144 -> FileNotFoundError: [WinError 206] The filename or extension is too long
```

This is not a tuning problem. Lowering `max_diff_bytes` below 32 KB would make the
review packet useless for real diffs, and the packet also carries the task
document, the lens, and the schema.

**Recommended fix.** Add to section 5.4, normatively: prompts, diffs, and schemas
MUST be delivered to the child process over **stdin or a temporary file passed by
path**; they MUST NOT be placed in argv. Argv carries only flags, model
identifiers, and session IDs. Adapters wrapping a CLI with no stdin prompt mode
MUST use a temp file and MUST delete it in a `finally` block.

This also removes the argument-injection surface that section 11.6 exists to test,
rather than merely testing that it is absent.

**Test to add:** a contract test asserting that no argv element exceeds a small
bound (say 4096 bytes) for any adapter, driven by a 200 KB diff fixture.

### F-B2 — Executable resolution is unspecified, and bare-name exec fails

**Refers to:** section 5.1 (line 169); section 5.4 `preflight`; section 7.3
CODE-01 step 6.

**Severity:** blocker.

`claude` and `grok` are commonly installed as `.cmd` shims on Windows. Measured:

```text
EXEC "shim.cmd"   -> FileNotFoundError: [WinError 2] The system cannot find the file specified
which()           -> .\shim.cmd
EXEC ".\shim.cmd" -> OK rc=0 out=b'hello from cmd shim\r\n'
```

So `.cmd` targets *do* execute — but only when the adapter passes a resolved path
including the extension. A bare `"claude"` fails. The spec never says who resolves
the executable name, when, or what is recorded.

**Recommended fix.** In section 7.3 CODE-01 and section 8.3 COUNCIL-01: preflight
MUST resolve each runtime's executable via `shutil.which()`, MUST fail preflight
with a named target if resolution returns `None`, and MUST record the resolved
absolute path in `run.json`. All subsequent invocations use the recorded path.

**Second-order consequence, worth one sentence in section 10.** A `.cmd` target is
executed *through* `cmd.exe`, which re-parses its arguments. Metacharacters `&`,
`|`, `^`, `%`, `<`, `>` become live for arguments passed to a `.cmd` shim even
though the supervisor never used `shell=True`. Applying F-B1 (stdin delivery)
neutralizes this for prompts. Section 11.6's hostile-input list is POSIX-only — it
names `$()` and backticks — and MUST be extended with the Windows set above.

---

## 4. Correctness defects

### F-C1 — `ROUGH_CONSENSUS` can fire on a unanimous rejection

**Refers to:** section 8.3 COUNCIL-06 (line 611); section 4 `max_dissenters`
(line 140).

**Severity:** high. Silently reports agreement that does not exist.

The rule is `A >= N - max_dissenters`, and section 4 places no bound on
`max_dissenters`. With `N = 3` and `max_dissenters = 3`, the rule reduces to
`A >= 0`, which is always true — three overall `reject` ballots would be reported
as `ROUGH_CONSENSUS`.

**Recommended fix.** Two changes, both cheap:

1. Section 4 validation: `0 <= max_dissenters <= len(participants) - 1`. Reject
   otherwise, with a message naming both values.
2. Section 8.3 COUNCIL-06: state the rule as
   `A >= 1 and A >= N - max_dissenters and not blocking_objection`. The `A >= 1`
   guard is redundant given (1) but costs nothing and makes the rule correct in
   isolation.

**Tests to add:** `COUNCIL-016` — `max_dissenters >= N` is rejected at config load.
`COUNCIL-017` — all-reject ballots yield `CONTESTED`, never `ROUGH_CONSENSUS`.

### F-C2 — `blocking_objection_prevents_consensus` is a dead config key

**Refers to:** section 4 (line 141); section 8.3 COUNCIL-06 (lines 610-611).

**Severity:** medium. Configuration that appears to do something and does not.

The key is declared in the example configuration, but COUNCIL-06 hardcodes "no
blocking objection" into both `UNANIMOUS` and `ROUGH_CONSENSUS` and never
references it. Behaviour when it is `false` is undefined.

**Recommended fix.** Preferred: delete the key from the MVP configuration and list
it in section 14 as a post-MVP knob — the MVP's whole point is determinism, and a
blocking objection that does not block is a strange option to ship on day one.
Alternative: specify the `false` branch explicitly and test both branches.

### F-C3 — Mixed dispositions have no terminal status

**Refers to:** section 7.3 CODE-10 (lines 484-487); tests CODE-012, CODE-013,
CODE-014.

**Severity:** high. The most likely real-world outcome is undefined.

The four success statuses are exhaustive only for *pure* dispositions, and the
three tests cover only pure cases: all `fixed`, all `rejected_with_evidence`, one
`not_fixed`. A run with one of each — the common case with three reviewers —
matches `COMPLETED_AFTER_REPAIR`, `COMPLETED_WITH_REBUTTALS`, and
`COMPLETED_WITH_UNRESOLVED_FINDINGS` simultaneously. Two implementers will choose
differently, and the status is user-facing.

**Recommended fix.** Add a normative precedence rule to CODE-10:

```text
if any disposition.outcome == "not_fixed":     COMPLETED_WITH_UNRESOLVED_FINDINGS
elif any disposition.outcome == "fixed":       COMPLETED_AFTER_REPAIR
elif dispositions non-empty (all rebutted):    COMPLETED_WITH_REBUTTALS
else (no findings existed):                    COMPLETED_NO_FINDINGS
```

Unresolved outranks fixed deliberately: the status should surface the weakest part
of the outcome, not the strongest.

**Test to add:** `CODE-021` — one `fixed`, one `rejected_with_evidence`, one
`not_fixed` yields `COMPLETED_WITH_UNRESOLVED_FINDINGS`, and the summary names the
unresolved finding.

### F-C4 — "The original repository remains untouched" is false as written

**Refers to:** section 2.1 (line 37); section 7.3 CODE-02 (line 345); section 12
"The original repository remains unchanged"; section 10 final bullet.

**Severity:** medium. The guarantee's substance holds; its wording does not, and
users will rely on the wording.

Verified on a scratch repository. `git worktree add -b dialectic/run-1` writes into
the *original* repository:

```text
--- .git before ---            --- .git after ---
COMMIT_EDITMSG HEAD config     COMMIT_EDITMSG HEAD config
description hooks index        description hooks index
info logs objects refs         info logs objects refs worktrees   <-- new

--- refs before ---            --- refs after ---
refs/heads/master              refs/heads/dialectic/run-1          <-- new
                               refs/heads/master
```

And the driver's snapshot commit lands in the original repository's object store,
not in the worktree directory:

```text
objects before: 2
objects after:  5
$ git cat-file -p dialectic/run-1:evil.txt
model authored
$ git status --porcelain        (empty)
$ git rev-parse --abbrev-ref HEAD
master
```

HEAD, the current branch, and the working tree are genuinely unchanged — the
guarantee that matters is intact. But model-authored content is now resident in
the user's `.git/objects` and reachable from a ref in their repository, and
section 10's deliberate no-cleanup policy means `.git/worktrees/` metadata
accumulates run after run with no `git worktree prune`.

**Recommended fix.**

1. Reword section 2.1 and CODE-02 to the precise claim: *no tracked file content,
   no checked-out working tree, and no pre-existing branch or HEAD is modified.*
2. Add to section 10 and the README limitations: each run adds one
   `dialectic/<run-id>` branch, one `.git/worktrees/` entry, and model-authored
   objects to the target repository; cleanup is the user's responsibility, and the
   README MUST give the commands (`git worktree remove`, `git branch -D`,
   `git worktree prune`).

**Test to add:** `CODE-022` — after a completed run, the original repository's HEAD
SHA, current branch name, and `git status --porcelain` are byte-identical to their
pre-run values, and `main` points at the same SHA.

### F-C5 — "No heuristic acceptance" contradicts Grok JSON extraction

**Refers to:** section 7.3 CODE-06 final bullet (line 432); section 13 Slice 2
("Grok adapter supporting fresh review/council turn, JSON extraction/validation").

**Severity:** medium. Two normative statements that cannot both be satisfied.

CODE-06 forbids heuristic acceptance and model-powered format repair. Extracting
JSON from a prose-wrapped response is heuristic by nature: which fence, what if
there are two, what about a preamble.

**Recommended fix.** Pin one deterministic extraction rule in section 5.4 and apply
it to every adapter, so behaviour does not vary by runtime:

> The adapter attempts, in order: (1) parse the complete stdout as JSON; (2) if
> that fails, locate fenced blocks tagged `json`. Exactly one such block MUST
> exist; zero or more than one is a parse failure. No other extraction strategy is
> permitted. A parse failure fails that agent turn, and the raw stdout is retained.

That is deterministic and testable, and it keeps CODE-06's promise honest.

**Test to add:** extend section 11.6 with fixtures for zero fences, two fences, and
a fence surrounded by prose.

### F-C6 — Env-reference redaction is over-broad and collides with the audit requirement

**Refers to:** section 4 requirement 1; section 6 "Model prompts and responses MUST
be retained for audit"; CORE-004; CORE-006; COUNCIL-015 ("actual identities").

**Severity:** medium.

"Environment-variable references MUST be expanded without persisting their values"
is applied blanket to all env references. But `${CLAUDE_REVIEW_MODEL}` is a model
name, not a secret. Redacting it means `config.redacted.json` cannot record which
model was configured, which fights section 6's audit purpose and COUNCIL-015's
requirement to report actual identities. (`AgentResponse.requested_model` rescues
the audit trail in practice, but the two requirements still read as contradictory,
and a run that fails at preflight has no responses at all.)

There is also an implementation trap: value-based redaction on a short env value
corrupts artifacts. If `CODEX_DRIVER_EFFORT=high`, naive redaction replaces every
occurrence of "high" in every persisted diff and transcript — including
`severity: "high"` in prose.

**Recommended fix.** Split the rule in section 4:

- Fields declared non-secret (`model`, `effort`, `runtime`, `lens`, `id`) expand
  **and** persist their expanded value in `config.redacted.json`.
- Secret-bearing fields never persist a value, only the reference name.
- Section 5.2 `Redactor`: value-based redaction applies only to values of at least
  8 characters drawn from an explicit secret set. State plainly that redaction of
  secrets the supervisor was never told about is best-effort, so section 6's
  "tokens MUST NOT be persisted" is not read as a guarantee the code cannot make.

### F-C7 — CORE-002 has no detection rule

**Refers to:** section 11.3 CORE-002 ("Inline secret-like config field supplied →
Validation rejects it").

**Severity:** low-medium. Untestable as written; will produce false positives.

"Secret-like" is undefined. Entropy heuristics will reject legitimate lens text and
future model identifiers.

**Recommended fix.** Replace detection with structure: designate the secret-bearing
fields in the config schema and require them to be `${ENV_NAME}` references — any
literal in those fields is rejected. Deterministic, no heuristics, and the test
writes itself.

---

## 5. Under-specifications

Each of these is a place where two competent implementers would produce different,
both-defensible behaviour.

### F-U1 — No canonical status enum, and no exit-code mapping

**Refers to:** sections 7.2 and 7.3 (lines 363, 367, 434, 438, 484-487); section
8.3 COUNCIL-06 (lines 610-613); section 9; tests CODE-011 (line 713) and
COUNCIL-012 (line 739); CORE-011.

Terminal values are scattered across five sections and two test tables.
`REPAIR_FAILED` and `MODERATOR_FAILED` appear **only** inside test-table cells and
never in the normative body. Section 8.3 lists five council outcomes; COUNCIL-012
then introduces a sixth.

Two distinct concepts are also conflated in council mode: *run status* (did the
machinery complete — `NO_QUORUM`, `TIMEOUT`, `MODERATOR_FAILED`, `CANCELLED`) and
*product outcome* (what the council decided — `UNANIMOUS`, `ROUGH_CONSENSUS`,
`CONTESTED`). Section 8.3 already recognises this in prose: "a contested result is
a valid completed product outcome, not an execution failure."

And CORE-011 asserts `dial` and `dialectic` "return the same exit codes" while no
exit codes are defined anywhere in the document.

**Recommended fix.** Add a new section 7.4 / 8.4 (or a shared appendix) with:

1. One `RunStatus` enum covering both modes, exhaustive, each value defined in one
   sentence.
2. A separate nullable `ConsensusOutcome` enum for council, populated only when
   `RunStatus == FINALIZED`.
3. A table mapping every `RunStatus` to a process exit code — at minimum: success
   statuses to 0; product-level non-agreement (`CONTESTED`) to 0 with a distinct
   status in the output; configuration/preflight failure to 2; execution failure to
   3; timeout to 4; cancelled to 130.

### F-U2 — Code mode has no timeout status

Section 9 gives every workflow an overall wall clock and section 4 defines
`code_run_seconds`, but section 7.3's terminal statuses include no timeout value.
Council has `TIMEOUT`; code does not. Precedence between `TIMEOUT`, `CANCELLED`,
and an in-flight phase failure is undefined in both modes — if the wall clock
expires while a reviewer has already returned malformed output, the status is a
coin flip.

**Recommended fix.** Add `TIMEOUT` to code mode. State precedence explicitly:
`CANCELLED` > `TIMEOUT` > phase failure. Define when the wall clock starts (first
model invocation, not `CREATED`, so preflight latency does not consume the budget)
and give preflight its own timeout.

### F-U3 — Diff generation is not pinned

CODE-005 asserts every reviewer receives an identical diff hash, but no `git diff`
invocation is specified. The user's global configuration will change the bytes:
`diff.external`, `diff.algorithm`, textconv filters, `core.pager`, and
`core.autocrlf`. The scratch repository emitted
`warning: in the working copy of 'evil.txt', LF will be replaced by CRLF` on a
plain commit — line-ending translation is live on Windows by default. Binary files
are also unaddressed; `git diff` emits `Binary files ... differ`, which is useless
to a reviewer and silently shrinks the packet.

**Recommended fix.** Pin the exact invocation in CODE-04, with `-c` overrides so
the user's global configuration cannot alter it, for example:

```text
git -c core.autocrlf=false -c diff.external= -c diff.algorithm=histogram \
    diff --no-color --no-ext-diff --no-textconv -U3 <base_sha>..<review_sha>
```

State the binary-file policy: binary hunks are excluded from the packet and listed
by path in a `binary_files_changed` field, so reviewers know something changed that
they cannot see.

### F-U4 — `max_diff_bytes` measures a per-reviewer quantity

CODE-04 step 6 (line 367) bounds "the UTF-8 encoded review packet", but each
reviewer's packet differs — different lens text — so the bound is
non-deterministic across reviewers and a run could pass for one and fail for
another.

**Recommended fix.** Measure the **diff alone** against `max_diff_bytes`, checked
once. If a packet-level ceiling is also wanted, add a separate `max_packet_bytes`
checked per reviewer after construction.

### F-U5 — No schema for run artifacts

`run.json`, `events.jsonl` records, `summary.json`, `feedback.json`, and
`reviews/manifest.json` have no defined shape, yet CORE-005 tests atomic rename of
`run.json`, section 12 requires machine-readable summaries, and several tests
assert on summary content. The council tree (section 6) shows `opening/`,
`cross-examination/`, and `ballots/` as bare directories with no filename
convention, while COUNCIL-003 depends on aliases appearing in artifacts but not in
prompts.

Model-facing schemas all carry `schema_version: Literal[1]`; run artifacts carry
none, which will hurt at post-MVP item 9 (SQLite-backed recovery).

**Recommended fix.** Add Pydantic models for all five, each with
`artifact_schema_version` and the emitting tool version. Specify artifact filenames
as `participant-a.json` and so on, with `aliases.json` as the only place the
mapping to real runtimes and models lives.

### F-U6 — No run-id format

`<run-id>` becomes a Git ref (`dialectic/<run-id>`), a directory name under the
state root, and a user-supplied argument to `dial status`. It therefore needs a
charset that is simultaneously ref-safe (no `~ ^ : ? * [ \`, no `..`, no trailing
`.lock`, no leading or trailing `.`) and path-safe, plus traversal rejection on
input.

**Recommended fix.** Specify a sortable, safe format — for example
`YYYYMMDDTHHMMSSZ-<8 lowercase base32 chars>` — and require `dial status` to
validate its argument against that pattern *before* joining it to any path.

### F-U7 — Reviewer configuration schema exists only as an example

Section 4 shows YAML but never gives the reviewer entry's model. Open questions an
implementer must guess: is `target: "@driver"` mutually exclusive with
`runtime`/`model`? Is `lens` an enum, free text, or a file path? Is it
length-bounded? `effort` appears on the driver but on no reviewer — does it apply
to `claude-code` and `grok-build` at all, and what happens when a runtime does not
support it? `AgentTarget.effort` is typed `str | None` with no enum.

**Recommended fix.** Publish the Pydantic models for reviewer, participant, and
moderator entries alongside `AgentTarget` in section 5.3. Define `lens` as free
text with a length bound — it is model-facing, user-authored text injected into a
prompt that also carries a controller-owned schema. Say explicitly whether an
unsupported `effort` is a validation error or is dropped with a warning; silently
dropping it would violate section 4's no-implicit-replacement rule.

### F-U8 — Council has no session-ID guard

Section 5.4 (line 236) states a specific MUST for code mode: fail before review if
the driver returned no resumable session ID. Council mode resumes sessions twice
(cross-examination, ballots) and has no equivalent rule, so a participant that
returns no session ID fails late, after every participant has burned an opening
turn.

**Recommended fix.** Add to COUNCIL-02: if any participant completes its opening
position without a resumable session ID, the run fails immediately as `NO_QUORUM`,
naming the participant.

### F-U9 — Validation strictness is asymmetric

CODE-09 states plainly that unknown finding IDs are invalid. Council has no
equivalent for `PropositionVote.proposition_id` (COUNCIL-05 requires full coverage
but never forbids *extra* IDs) or for `CandidateProposition.supporting_participants`
containing an alias absent from the alias map.

**Recommended fix.** Mirror CODE-09's wording in COUNCIL-04 and COUNCIL-05. Unknown
proposition IDs and unknown participant aliases are invalid artifacts and fail the
producing agent.

### F-U10 — Proposition votes are collected but never used

COUNCIL-05 requires every proposition to receive exactly one vote from every
participant. COUNCIL-06 then computes the outcome from `overall_vote` alone.
Proposition votes reach only the vote matrix in the report. That is a defensible
design, but it is never stated, so an implementer may reasonably try to derive the
outcome per proposition.

Related: nothing forbids a ballot that rejects every proposition while voting
`accept` overall.

**Recommended fix.** State in COUNCIL-06: "Per-proposition votes are recorded and
displayed; only `overall_vote` determines the outcome." Then decide the consistency
question explicitly — either validate coherence, or state that incoherent ballots
are accepted as-is and surfaced in the report.

### F-U11 — Cross-examination ledger composition is undefined

COUNCIL-03 says each participant receives "the complete anonymized opening-position
ledger". Two unanswered questions change prompt construction and the
COUNCIL-002/003 tests: does the ledger include the participant's **own** position,
and does a participant know **which alias it is**?

Both answers are defensible — including one's own position unlabelled preserves
blindness and can produce genuinely useful self-critique — but the choice must be
deliberate and recorded.

**Recommended fix.** State the decision and the reason in COUNCIL-03, and add an
assertion to COUNCIL-002 or COUNCIL-003 that locks it in.

### F-U12 — A `fixed` disposition is never verified

CODE-10 step 2 permits a no-change repair turn when everything was rebutted or left
unfixed. But nothing rejects a driver that reports `outcome: "fixed"` for every
finding and edits nothing — the run finalizes as `COMPLETED_AFTER_REPAIR` with no
second commit. The controller can check this deterministically, which is exactly
the kind of check the document's own philosophy calls for.

**Recommended fix.** Add to CODE-10: if any disposition is `fixed` and the repair
turn produced an empty diff against `review_sha`, the run fails as `REPAIR_FAILED`,
naming the findings claimed fixed.

**Test to add:** `CODE-023` — driver claims `fixed` but writes nothing yields
`REPAIR_FAILED`.

### F-U13 — Native CLIs load user-global configuration the supervisor does not control

Section 10 states that no repository hooks, MCP definitions, skills, or model
instruction files are loaded **by the supervisor**. True but incomplete: the
*native CLI* loads its own user-global configuration regardless of CWD —
`~/.claude/CLAUDE.md`, `~/.codex/`, globally registered MCP servers. A globally
configured filesystem MCP server hands a supposedly diff-only reviewer (line 389)
real repository access, defeating the isolation section 7.3 promises. It also makes
reviews non-reproducible across machines.

**Recommended fix.** In section 10: adapters MUST pass each CLI's flags for
disabling project and user configuration and for restricting or disabling MCP
servers, where such flags exist; where they do not, section 10 MUST record the
residual risk explicitly rather than implying isolation the supervisor cannot
enforce. Record the flags used in `run.json` so a review's provenance is auditable.

### F-U14 — Child process trees survive timeout and cancellation

Section 9 requires terminate, wait, force-kill. On Windows `terminate()` maps to
`TerminateProcess`, which does not touch descendants; on POSIX a plain `kill()`
reaches only the direct child. Agent CLIs spawn plenty of children. CORE-007 and
CORE-008 will pass — the direct child dies and the status is recorded — while
orphaned processes continue running and holding provider connections.

**Recommended fix.** Specify the mechanism in section 9: POSIX uses
`start_new_session=True` plus `os.killpg`; Windows assigns each child to a Job
Object with kill-on-close. Strengthen CORE-007 and CORE-008 to assert the whole
tree is gone, using a fake executable that spawns a long-lived grandchild.

### F-U15 — No repository-level locking

Two concurrent `dial code` runs against one repository both observe a clean working
tree at CODE-01 and both proceed. They will not corrupt each other's worktrees, but
the clean-tree precondition is no longer meaningful and the second run's base SHA
may not be what the user assumes.

**Recommended fix.** Either take an advisory lock file per target repository for
the duration of a code run, failing fast with the holding run-id, or state plainly
in section 2.2 that concurrent runs against one repository are unsupported in the
MVP.

### F-U16 — The worktree lacks gitignored files

CODE-01 step 3 requires a clean tree including untracked non-ignored files, and
CODE-02 creates a fresh worktree. Ignored files do not exist there: no `.venv`, no
`node_modules`, no `.env`. CODE-03 then instructs the driver to "run whatever
narrow checks it considers appropriate" — which will frequently fail for reasons
unrelated to the task, wasting a driver turn and possibly producing findings about
a broken environment.

**Recommended fix.** Note it in the README limitations and, in CODE-03, tell the
driver explicitly that the worktree is a fresh checkout without ignored build or
environment artifacts, and that fixing environment setup is not part of the task.

### F-U17 — No rate-limit or transient-failure note

Section 9 states no automatic provider retry, and CODE-07 makes every reviewer
required. Combined: a single HTTP 429 on one reviewer discards a completed driver
turn and fails the run. This is a defensible fail-closed MVP policy, but it will be
the most common real-world failure and it is not called out anywhere.

**Recommended fix.** State it as an accepted consequence in section 9, and add it
to the README's limitations next to the cost warning, so the first user to hit it
recognises the behaviour as designed rather than broken.

---

## 6. Test-plan gaps

### F-T1 — Normative MUSTs with no corresponding test

The test tables are strong on the workflow spine and thin on the validation edges.
No test exists for:

| Requirement | Source |
|---|---|
| Code mode accepts between one and five reviewers | section 4 |
| Council mode accepts between two and five participants | section 4 |
| Reviewer and participant IDs are unique | section 4 |
| Driver completed without a resumable session ID fails before review | section 5.4, line 236 |
| Code-mode overall wall clock expires | section 9; section 4 `code_run_seconds` |
| Mixed dispositions (see F-C3) | section 7.3 CODE-10 |
| Reviewer prompt does not contain the worktree path | section 7.3, line 389 |

The last is notable: CODE-006 tests only that the driver transcript sentinel is
absent. The stronger isolation promise — reviewers never learn where the writable
worktree is — is untested, and it is the one that matters if F-U13's global-MCP
risk is real.

### F-T2 — CORE-009 is a wall-clock timing assertion

"Elapsed time approximates maximum delay, not sum of delays" is flaky under CI
load. Section 11.2 already records per-invocation start and end timestamps, and
CODE-004 uses them correctly ("all reviewer start timestamps precede first reviewer
completion").

**Recommended fix.** Restate CORE-009 in terms of recorded timestamp overlap, in
the same style as CODE-004. Keep no wall-clock ratio assertion in the mandatory
suite.

### F-T3 — CORE-011 needs a comparison rule

`dial` and `dialectic` invoked with matching arguments produce two *different* runs
with different run-ids, timestamps, and artifact paths. "Equivalent state effects"
therefore cannot mean equality.

**Recommended fix.** Specify the comparison: identical artifact tree *structure*,
identical status, identical exit code, and identical summary content after
normalising run-id, timestamps, and paths.

---

## 7. Smaller notes

- **Section 8.3 COUNCIL-06, "no abstentions".** Ambiguous. If `A == N` then no
  participant abstained *overall* by definition, so the clause is either redundant
  or it means *per-proposition* abstentions. If the latter, say so — a run where
  everyone accepts overall but one abstains on a single proposition would then be
  `ROUGH_CONSENSUS`, which is meaningful and non-obvious.
- **Section 8.3 COUNCIL-02, `confidence: float`.** The comment says 0.0 through 1.0
  but the field carries no Pydantic constraint. Add `Field(ge=0.0, le=1.0)`; the
  spec already promises confidence never affects the calculation, and an
  out-of-range value should fail loudly rather than be ignored quietly.
- **Section 7.3 CODE-04 step 2.** "Fail as `NO_CHANGES` unless a future
  configuration explicitly permits no-change tasks; the MVP exposes no such option"
  is a normative sentence conditioned on something that does not exist, and it
  duplicates step 1. Reduce to: "Fail as `NO_CHANGES` when the worktree is
  unchanged relative to the base SHA."
- **Section 7.3 CODE-04 ordering.** The snapshot commit (step 3) happens before the
  `DIFF_TOO_LARGE` check (step 6), so an over-large run still leaves a commit and a
  branch behind. That is consistent with the preserve-everything policy, but say so
  — otherwise it reads like an oversight, and CODE-016's "no reviewers run" is
  silent on what *did* happen.
- **Section 7.3 CODE-06, `ReviewFinding.file` / `line`.** No rule about findings
  that point at a path absent from the diff. Semantic deduplication is correctly
  out of scope, but this is a cheap structural check. At minimum, state that
  unmatched paths are permitted and passed through, so implementers do not add a
  validator that fails legitimate cross-cutting findings.
- **Section 11.3 CORE-010.** Requested and actual model differ, both recorded.
  Worth adding: the run does **not** fail on a mismatch. Section 4 forbids implicit
  fallback, so an implementer could reasonably read a mismatch as a violation to
  reject. Decide and state it.
- **Section 11.4 CODE-003.** "Different fresh session ID" is trivially satisfiable
  by a scripted adapter returning distinct IDs; it proves nothing about the
  controller. Assert instead on the recorded **operation type** — `start`, never
  `resume` — which section 11.2 already captures.
- **Section 12 platform coverage.** Windows and Linux only. `platformdirs` and the
  CLI ecosystem both suggest macOS users will try this on day one. Confirm the
  omission is deliberate and say so, or add macOS to the definition of done.
- **Section 13 Slice 2 exit criterion.** "A real simple Codex task can flow through
  available real reviewers and stop correctly" requires live credentials and cannot
  run in CI. Mark it explicitly as manually verified, so the slice is not blocked
  waiting for an automated gate that section 11.7 already excludes from CI.

---

## 8. Suggested order of work

1. **F-B1 and F-B2** — they change the `AgentAdapter` contract in section 5.4, so
   every later decision depends on them.
2. **F-U1** — the status enum and exit-code table; several other findings resolve
   into it (F-C3, F-U2, F-U12).
3. **F-C1, F-C2** — the consensus arithmetic; small, self-contained, and wrong
   today.
4. **F-U5, F-U6** — artifact schemas and run-id format; Slice 0 cannot be finished
   cleanly without them.
5. **F-C4, F-U13, F-U14** — the honesty and isolation cluster; these change what
   the README promises as much as what the code does.
6. Everything else, in place, as the relevant slice is implemented.

---

## 9. Finding index

| ID | Severity | Title |
|---|---|---|
| F-B1 | blocker | 256 KB review packet cannot be passed on a Windows command line |
| F-B2 | blocker | Executable resolution unspecified; bare-name exec fails |
| F-C1 | high | `ROUGH_CONSENSUS` can fire on a unanimous rejection |
| F-C2 | medium | `blocking_objection_prevents_consensus` is a dead config key |
| F-C3 | high | Mixed dispositions have no terminal status |
| F-C4 | medium | "Original repository remains untouched" is false as written |
| F-C5 | medium | "No heuristic acceptance" contradicts Grok JSON extraction |
| F-C6 | medium | Env-reference redaction over-broad; collides with audit |
| F-C7 | low-medium | CORE-002 has no detection rule |
| F-U1 | high | No canonical status enum, no exit-code mapping |
| F-U2 | medium | Code mode has no timeout status |
| F-U3 | medium | Diff generation is not pinned |
| F-U4 | medium | `max_diff_bytes` measures a per-reviewer quantity |
| F-U5 | medium | No schema for run artifacts |
| F-U6 | medium | No run-id format |
| F-U7 | medium | Reviewer configuration schema exists only as an example |
| F-U8 | medium | Council has no session-ID guard |
| F-U9 | low | Validation strictness is asymmetric |
| F-U10 | low | Proposition votes are collected but never used |
| F-U11 | medium | Cross-examination ledger composition is undefined |
| F-U12 | medium | A `fixed` disposition is never verified |
| F-U13 | high | Native CLIs load global config the supervisor does not control |
| F-U14 | medium | Child process trees survive timeout and cancellation |
| F-U15 | low | No repository-level locking |
| F-U16 | low | The worktree lacks gitignored files |
| F-U17 | low | No rate-limit or transient-failure note |
| F-T1 | medium | Normative MUSTs with no corresponding test |
| F-T2 | low | CORE-009 is a wall-clock timing assertion |
| F-T3 | low | CORE-011 needs a comparison rule |
