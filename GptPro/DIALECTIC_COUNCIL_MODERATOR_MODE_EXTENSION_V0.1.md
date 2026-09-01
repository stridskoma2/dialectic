# Dialectic Council Moderator Mode Extension

**Extension revision:** 0.1

**Applies after:** `DIALECTIC_MVP_IMPLEMENTATION_AND_TEST_SPEC_V0.5.4.md`

**Date:** 2026-09-01

This extension adds one bounded Council Once choice without changing the number of
discussion rounds, ballot authority, participant quorum, or model tool access.
All baseline requirements remain normative unless explicitly changed below.

## Configuration

`CouncilSpec` adds:

```python
moderator_mode: Literal["fresh", "independent-opening"] = "fresh"
```

Omission is backward-compatible and selects `fresh`.

## `fresh`

The baseline flow is unchanged: participant openings run concurrently, participant
cross-examinations run concurrently, and a fresh non-voting moderator receives the
original prompt plus the complete opening and revision ledgers before producing the
candidate.

## `independent-opening`

During `OPENING_POSITIONS`, before participant calls launch, the selected moderator
target receives the original prompt and `OpeningPosition` schema in an isolated blind
call. It sees no participant response. Failure or invalid output is
`MODERATOR_FAILED`. The controller persists `council/moderator-opening.json` and the
ordinary adjacent moderator turn evidence.

Participant openings then run concurrently exactly as in the baseline. The
cross-examination position ledger includes the blind result under the role alias
`Moderator opening`; that role alias reveals no runtime or model identity. The
moderator opening is not a participant, is not eligible for
`supporting_participants`, and never receives a ballot.

Final candidate synthesis still starts a fresh non-voting moderator session. It
receives the original prompt, the complete position ledger including the optional
moderator opening, and all participant revisions. This separate call keeps the mode
valid for every supported moderator runtime, including per-turn Grok moderator
execution, and prevents an idle retained moderator process.

## Parallelism and tools

Within each participant phase, all required participants launch as one fail-fast
parallel cohort and the controller waits for the complete cohort before advancing.
The optional moderator opening is deliberately completed before the participant
opening cohort so it cannot observe participant output.

This extension does not grant web search, MCP, apps, shell tools, or other built-in
tool surfaces to any packet-only role. Such access requires a separately specified,
controller-issued, role-bound, bounded, auditable capability profile.

## Verification

The frozen 108 test IDs remain unchanged. COUNCIL-005 is strengthened to cover both
moderator modes, verify the blind opening's visibility in later ledgers, require a
fresh synthesis call, and prove the moderator remains non-voting.
