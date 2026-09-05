# Dialectic — project instructions

Canonical instruction file for this repository. Every coding agent working here —
Claude Code, Codex, or any other — should read this file. `CLAUDE.md` points here;
do not maintain a second copy of these rules.

Dialectic is a local Python supervisor that orchestrates cross-model code review and
council deliberation. Two bounded workflows: **Code Once** (one Codex driver
implements, configured reviewers review the immutable diff in parallel, one repair
turn, stop) and **Council Once** (blind openings, one anonymized cross-examination
round, fresh moderator, ballots, controller-derived consensus, stop).

## The specification is the source of truth

`GptPro/DIALECTIC_MVP_IMPLEMENTATION_AND_TEST_SPEC_V0.5.4.md` — **frozen MVP
implementation baseline**, 2026 lines.

`GptPro/DIALECTIC_COUNCIL_MODERATOR_MODE_EXTENSION_V0.1.md`,
`GptPro/DIALECTIC_WEB_RESEARCH_EXTENSION_V0.1.md`, and
`GptPro/DIALECTIC_TURN_TIMING_EXTENSION_V0.1.md` are the normative post-baseline
extensions. They add user-selectable fresh-versus-independent-opening moderator
behavior, bounded provider-native web research, and controller-owned idle/allotted
turn deadlines while preserving fresh final synthesis, one cross-examination round,
non-voting moderation, and the packet-only security boundary.

`GptPro/DIALECTIC_NATIVE_EXECUTABLE_SELECTION_EXTENSION_V0.1.md` adds optional
controller-only native CLI paths by runtime and role. It preserves existing
version qualification, capability binding, and workflow bounds.

Read the relevant section before implementing against it. It went through five
review rounds with two independent reviewers and its clauses are load-bearing:
bounds, orderings, and failure kinds are deliberate, not incidental. When code and
spec disagree, the spec wins. When the spec is genuinely ambiguous, say so and ask —
do not resolve it silently.

Earlier revisions (V0.1–V0.5.3) and all `*_REVIEW-*.md` files are history. Consult
them only to understand *why* a clause exists; never implement from them.

## Settled decisions — do not reopen

These were decided across five revisions. Do not relitigate, "improve", or design
around them:

- Codex is the only writable driver. Claude Code and Grok Build are reviewer and
  council targets only.
- Exactly one repair turn, no re-review. Exactly one cross-examination round.
- No continuous loops, second review, extra council rounds, or plan DAGs.
- The controller — never a model — owns Git operations, state transitions, timeouts,
  and consensus calculation.
- MCP is deferred until after a native alpha/beta, and only ever as a thin northbound
  ingress over `DialecticService`. Participants never receive MCP servers or
  user-configured tool surfaces.
- No API transport, daemon, background job ownership, or crash resumption.
- Gemini and other providers are future `AgentAdapter` additions, not MVP work.
- Windows 11 and Linux are release platforms. macOS is not in the definition of done.

## Deliberate choices that look like mistakes

Each of these was a defect at some point in the spec's history and was fixed to its
current form. Do not "simplify" them back:

- **`canonical_instantiation_verified: Literal[True]`** — not a typo for `bool`. The
  artifact's existence *is* the proof of successful canonical construction. Failed
  construction is represented by run failure evidence, never by a negative artifact.
- **`CouncilBallot` has no `overall_vote`** — the controller derives it into
  `derived_overall_vote`. Models submitted it in an earlier revision; one model's
  bookkeeping slip could then destroy an entire council run, and it moved a
  deterministic computation across the controller/model boundary.
- **`.dialectic-turn/control/` vs `.dialectic-turn/tmp/`** — a security split, not
  organization. Model-generated commands write only under `tmp/`; `control/` and the
  scratch root are not model-writable.
- **Two distinct phase vocabularies** — `RunPhase` (run lifecycle) and `turn_phase`
  (which native call). They were one overloaded field and were deliberately split.
- **Sampling described as detection, not enforcement** — scratch-size polling is a
  best-effort in-flight detector; the post-exit check is authoritative. Do not
  reword it into a guarantee the implementation cannot make.
- **Fail-closed everywhere** — no provider retry, no format-repair, no heuristic
  JSON extraction, no truncation of findings. A required reviewer failing kills the
  run by design.

## Test inventory is frozen at 108

**30 core + 46 Code Once + 32 Council Once.** Strengthen existing rows; do not add
IDs. Frozen means the IDs and counts are fixed — it does **not** preserve obsolete
expectations. Spec §11 requires a semantic audit of the corrected rows (CORE-015,
CORE-017, CORE-019, CORE-027, CORE-028, CORE-030, CODE-001, CODE-040, COUNCIL-001,
COUNCIL-011, COUNCIL-012, COUNCIL-013, COUNCIL-014, COUNCIL-024, COUNCIL-027) and a
freeze-check failure if a stale expectation survives even while the count still
reads 108.

## Verification

```bash
pytest -q
```

Offline suite, mandatory, must pass with no network and no provider credentials.
Also `pytest -q -m integration` (local Git/subprocess). Live tests are opt-in and
cost-bearing: `DIALECTIC_LIVE=1 pytest -q -m live`.

Run the full offline suite after every code change — bounds, schemas, and constants
in this codebase have non-local blast radius. Never claim a check passed unless it
ran.

## Slice discipline

Implement in the spec's slice order (§13) and stop at each exit criterion. Slice 0
is schemas, `DialecticService`, `RunStore`, redaction, and the cross-platform
process-unit and lock abstractions. Do not start a later slice's work early because
it seems convenient.

## Conventions

- Never commit credentials, run artifacts, or anything from the state root. Run
  directories are sensitive by design.
- Review documents are named `<subject>_REVIEW-<Reviewer>.md` (e.g. `-Opus`,
  `-Sol`), because several models review the same document side by side.
- Follow the spec's own naming for components, artifacts, statuses, and failure
  kinds exactly. These names are part of the contract.

## A note on this file's second audience

Spec §5.4.4 requires the Codex driver adapter to preserve ordinary `AGENTS.md`
repository-instruction discovery while marking the worktree untrusted for project
`.codex/` configuration. So when Dialectic is eventually run against its own
repository, this file is what the driver reads inside the isolated worktree. Keep it
accurate and keep it free of anything that should not be model-facing.
