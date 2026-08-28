# Review: Dialectic MVP Implementation and Test Specification v0.5

**Reviewed document:** `DIALECTIC_MVP_IMPLEMENTATION_AND_TEST_SPEC_V0.5.md` (revision 0.5, 1771 lines)

**Prior reviews by this reviewer:** v0.1 (29 findings, 9 notes), v0.2 (10), v0.3 (8),
v0.4 (5), plus `DIALECTIC_ORCHESTRATION_AND_MCP_NOTE-Opus.md`

**Review date:** 2026-08-28

**Review question:** did revision 0.5 close the v0.4 findings, and is the document
ready to implement against?

**Status:** advisory. Nothing in this file is normative until folded into the spec.

---

## 1. Summary verdict

**Yes — this is implementable.** All 5 findings from the v0.4 review are resolved,
and the resolutions are the strongest of any round so far: each one specifies a
mechanism, gives it a bound, and names the failure kind that fires when the bound is
exceeded. Test counts are unchanged at 108 (30 core, 46 Code Once, 32 Council Once,
verified against the tables) because v0.5 strengthened existing rows rather than
adding new ones — the right call for a closure revision.

Two of the fixes are notably better than what was asked for. R4-1 asked for the
`DialecticService` boundary to be made consistent; v0.5 replaces the ambiguity with
a five-step lifecycle contract — `create_run(mode)` returning an opaque `RunHandle`,
CLI acquisition bounded to ceiling-plus-one bytes with a regular-file-only open
sequence, `fail_invalid_input(handle, bounded_error)` capped at 4096 bytes, and an
explicit statement that the CLI never writes `RunStore`. R4-4 asked for a symlink
*test*; v0.5 instead rejects added and modified symlinks and multiply linked files
outright, keeps unchanged repository symlinks working, and records the narrowing in
§2.2 as deferred scope.

The v0.4 fix I'd single out is R4-2's: `64 + 22 + max(0, L − 1)`, failing as
`PREFLIGHT_FAILED` and naming the credential *environment name* by deterministic
lexical tie-break rather than its value, with `L = 0` for saved-auth-only targets.
That is a more careful answer than the finding asked for.

Three items remain, all small and all fixable in one sitting. None blocks Slice 0.
Only one has operational consequence, and it is confined to the Windows path.

Counts: 0 blockers, 2 low-medium, 1 low.

---

## 2. Disposition of the v0.4 findings

Each row checked against the v0.5 text.

| v0.4 finding | Status | Resolved by |
|---|---|---|
| R4-1 `DialecticService` boundary contradicts §6.3 | Resolved | §5.2 replaces the prose with a numbered five-step lifecycle: `create_run(mode)` persists the explicit-null `CREATED` record and returns an opaque `RunHandle`; the CLI performs only bounded regular-file acquisition; `fail_invalid_input(handle, bounded_error)` makes the service persist `INVALID_INPUT`; "the CLI never writes `RunStore` directly"; parser errors before `create_run` still exit 2 with no record. `ConfigLoader` is now labeled service-side in the component table. CORE-026 asserts "service—not CLI storage code—persists `INVALID_INPUT`" |
| R4-2 Stream-bound floor cannot fit guard plus marker | Resolved | §5.4.3 derives the minimum as `64 + 22 + max(0, L − 1)` from the longest supplied credential; a smaller cap fails `PREFLIGHT_FAILED` naming the limit field and credential environment name by deterministic lexical tie-break, never the value; saved-auth-only targets use `L = 0`; §5.4.5 now discards "the preflight-sized unpersisted trailing guard"; CORE-027 configures a cap below the derived minimum |
| R4-3 Worktree-path claim unqualified | Resolved | CODE-05 adds the qualifying paragraph: the rule "constrains controller injection, not arbitrary product text," driver-authored diff content containing a path stays in the packet unrewritten, and the summary and README describe the limitation; §12 README line extended to "the controller-injection qualification for paths authored in product content" |
| R4-4 Product symlinks supported but untested | Resolved, more strictly | CODE-04 step 3 requires every present changed entry to be a regular file with exactly one link; added/modified symlinks, junctions, hard-linked files, directories, FIFOs, sockets, and devices fail as `UNSUPPORTED_CHANGE`, while unchanged repository symlinks and deletions still work; §2.2 records the narrowing as deferred scope; the `UNSUPPORTED_CHANGE` trigger row and CODE-045 both name it; §5.4.4 adds a native hard-link aliasing enforcement probe and `LIVE-CODE-002` covers cross-boundary link attempts |
| R4-5 `dial status` run-directory requirement untested | Resolved | §6.3 now states the MUST inline with the exit-code contract; CORE-022 asserts "Each record and its canonical absolute run-artifact directory are displayed faithfully" |

Two corrections v0.5 made without being asked, both worth noting because they show
the artifact model being taken seriously rather than defended:

- The alias-map claim was narrowed honestly. v0.4 said `aliases.json` is "the only
  council artifact" mapping aliases to targets; v0.5 says it is the only
  *consolidated* map and acknowledges that per-turn audit paths and responses
  "necessarily bind one alias to the target that executed that turn," then restates
  the real invariant — no model-facing prompt, normalized artifact, or
  participant-visible ledger contains either form.
- Raw streams moved from `reviews/raw/` and `council/raw/` into
  `turns/<role>/<alias>/`, so every native call — reviews, openings,
  cross-examinations, moderation, ballots — now has a request, response, stdout, and
  stderr set, with `outbound`/`persisted` prompt hashes and `captured`/`persisted`
  stream hashes, plus an explicit prohibition on retaining an unredacted prompt copy
  to make the hashes reproducible.

---

## 3. Findings

### R5-1 — The Windows reader-thread join is the one blocking operation left without a bound or a failure kind

**Refers to:** §5.4.5 (lines 465 and 467); §6.3 `PROCESS_CLEANUP_FAILED` trigger row
(line 891); §9; CORE-028.

**Severity:** low-medium. Confined to Windows — one of the two release platforms —
and the failure mode is a hung turn rather than a wrong result.

§5.4.5 specifies a reader thread per pipe with a byte-bounded handoff queue, and
states that a reader "blocks on queue capacity." Line 467 then requires that "every
thread/handle is joined or closed before the turn returns."

Nothing bounds that join or says what happens if it does not complete. A reader
blocked on queue capacity is waiting on the event loop — the same event loop
CORE-028 deliberately stalls — and a blocked `put` is not released by the child
process dying, because the thread is waiting on the queue, not on `ReadFile`. Forced
`TerminateJobObject` therefore does not unblock it.

In practice the window is narrow: the queue's capacity is the stream limit plus one
chunk, and the reader accounts bytes *before* enqueueing, so it normally reaches the
one-shot overflow transition rather than the capacity block. But the specification
asserts both behaviors, and it does not say which one is guaranteed to win.

What makes this stand out is that v0.5 bounded every peer operation explicitly:
`turn_cleanup_seconds` for scratch removal, `graceful_kill_seconds` for termination,
`capability_probe_seconds` for the probe, and byte bounds on every stream and walk.
The `PROCESS_CLEANUP_FAILED` trigger was even extended in this revision to cover
turn-workspace cleanup ("or bounded no-follow cleanup cannot remove and prove absence
of its reserved turn workspace"). Reader threads are the one thing left out — they
are not the "platform-owned process unit" that row describes.

CORE-028 asserts "all threads/handles/resources close," which is the right
assertion — but a test for an unbounded join is a test that hangs rather than fails.

**Recommended fix.** Three sentences in §5.4.5 and one clause in §6.3:

1. The handoff `put` is abortable through a supervisor-owned cancellation event
   checked alongside capacity, set on overflow, forced termination, and turn
   teardown.
2. The consumer continues draining until both readers signal completion, so a
   stalled loop cannot strand a producer.
3. The join is bounded by `graceful_kill_seconds` (or its own small bound), and a
   reader thread that cannot be joined within it is `PROCESS_CLEANUP_FAILED` —
   extend that trigger row to name reader threads alongside the process unit, so
   CORE-026's "no enum member lacks a trigger test" completeness check covers it.

### R5-2 — `DialecticConfig` is referenced but never defined, and no top-level configuration model exists

**Refers to:** `RedactedConfigArtifact.normalized_config: DialecticConfig` (line
704); §6.1 binding table row for `input/config.redacted.json`; §4; §5.3; CORE-004.

**Severity:** low-medium. An implementer will write the obvious model in the first
hour — but this is the only dangling type reference in a document whose value is that
everything else is pinned.

The document defines 36 models. Every other type referenced by an artifact —
`ReviewReport`, `OpeningPosition`, `CouncilRevision`, `CandidateConclusion`,
`NormalizedFinding`, `CouncilBallot`, `AgentTarget` — has a class definition.
`DialecticConfig` appears exactly once, as the annotation on the field that holds the
entire normalized configuration, and is never declared.

§5.3 defines the leaves (`AgentTarget`, `ReviewerSpec`, `ParticipantSpec`,
`ModeratorSpec`) but nothing composes them, and there is no model for the `driver`,
`council`, `consensus`, or `limits` mappings that §4's YAML example and twenty-two
row ceilings table describe in prose.

This matters more than a missing annotation because `input/config.redacted.json` is
the audit anchor — the artifact that records what configuration actually ran. §6.1
requires that "Artifact-schema version 1 rejects undeclared fields" and that "Every
declared field is always serialized," neither of which is checkable against an
undefined type. CORE-004 asserts a resolved model value "is retained in normalized
audit configuration," which needs the shape to assert against.

A second, smaller gap sits in the same model: `redaction_applied: bool` has no stated
trigger. §4 guarantees the strict schema "exposes no credential, token, API-key,
auth-file, shell-environment, or permission-profile field," so nothing in a valid
configuration is a credential by construction — which suggests the flag is always
`False`. It becomes `True` only if a lens or model string incidentally contains a
byte sequence matching an allowlisted credential value under §6.2's known-value rule.
That is a real but non-obvious case, and unlike the stream and prompt artifacts —
which carry paired pre/post-redaction hashes — the configuration artifact records
only a single boolean with no indication of what changed.

**Recommended fix.**

1. Add `DialecticConfig` to §6.1 (or §5.3) composing `version`, `driver`,
   `reviewers`, `council` with its `participants`, `moderator`, and `consensus`, and
   `limits` — with `model_config = ConfigDict(strict=True, extra="forbid")`
   matching the other spec models, and `DriverSpec`, `ConsensusSpec`, and
   `LimitsSpec` as the missing leaves.
2. State when `redaction_applied` is `True`, and either add a
   `source_sha256`-style pre/post pair or a bounded list of redacted field paths, so
   the configuration artifact is as auditable as the stream artifacts beside it.

### R5-3 — `phase` names two disjoint vocabularies across artifact schemas

**Refers to:** `RunRecord.phase: RunPhase | None` (line 632); `EventRecord.phase:
RunPhase | None` (line 648); `AgentRequestArtifact.phase: Literal[...]` (line 755);
§6.1 binding table.

**Severity:** low.

Three persisted artifacts carry a field named `phase`, typed as `Literal`, with no
overlapping values:

- `RunRecord` and `EventRecord` use `RunPhase` — `PREFLIGHT`, `WORKTREE_SETUP`,
  `DRIVER_INITIAL`, `INITIAL_VALIDATION`, `REVIEWERS`, `FEEDBACK`, `DRIVER_REPAIR`,
  `FINAL_VALIDATION`, `REPORTING`, and the council equivalents.
- `AgentRequestArtifact` uses `initial`, `repair`, `review`, `opening`,
  `cross-examination`, `candidate`, `ballot` — turn identifiers that also form the
  `turns/<role>/<alias>/<phase>.request.json` filenames.

Both are correct in isolation. Together they mean that anyone writing tooling across
the artifact set — which is the stated purpose of §6.1's binding table — reads
`phase` from two files and gets values from different domains, with the run-level
vocabulary uppercase and the turn-level vocabulary lowercase-hyphenated as the only
signal.

This is the same class of collision v0.3 fixed thoroughly when `CREATED` and
`FINALIZED` appeared in both the phase diagrams and `RunStatus`. The fix there was to
give the concepts distinct names rather than rely on context.

**Recommended fix.** Rename `AgentRequestArtifact.phase` to `turn_phase` (or
`call_phase`) and define it as a named `TurnPhase` alias next to `CodePhase` and
`CouncilPhase`. Keep the filename segment as-is — the path pattern in the binding
table already disambiguates it by position — or rename both together for symmetry.

---

## 4. Two smaller notes

- **`CapabilityProbeResult` enum tense.** `expected` takes `allow`/`deny` while
  `observed` takes `allowed`/`denied`/`unavailable`, so the two can never be compared
  by equality and `passed` carries the real answer. Harmless, but `observed:
  "unavailable"` has no `expected` counterpart and the spec does not say whether that
  forces `passed = False`. One sentence would settle it.
- **`CapabilityBindingArtifact.canonical_instantiation_verified: Literal[True]`** is
  a nice use of the type system — the artifact's existence is the proof, and §5.4.1
  fails preflight rather than writing a negative record. Worth keeping, and worth one
  sentence saying so explicitly, since a reader may otherwise take it for an
  oversight and "fix" it to `bool`.

---

## 5. Suggested order of work

1. **R5-1** — three sentences in §5.4.5 plus one clause in the §6.3 trigger row.
   Do it before Slice 0 delivers `ProcessSupervisor`, since it changes the queue
   contract.
2. **R5-2** — one model plus three leaves; Slice 0 needs it anyway for
   `ConfigLoader` and `RedactedConfigArtifact`.
3. **R5-3** — a rename, cheapest before any artifact is written.

All three are documentation-level. Nothing here changes a workflow, a bound, a
failure kind, or a test count, and none of them should hold up starting Slice 0.

---

## 6. Finding index

| ID | Severity | Title |
|---|---|---|
| R5-1 | low-medium | Windows reader-thread join has no bound or failure classification |
| R5-2 | low-medium | `DialecticConfig` referenced but undefined; no top-level config model |
| R5-3 | low | `phase` names two disjoint vocabularies across artifact schemas |
