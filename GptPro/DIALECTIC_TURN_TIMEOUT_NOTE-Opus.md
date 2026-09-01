# Note: Per-Turn Timeouts — Wall Clock vs. Idle Watchdog

**Author:** Opus

**Date:** 2026-09-01

**Type:** Opinion / design note plus a code investigation. Not a spec review, not
normative. Written against `DIALECTIC_MVP_IMPLEMENTATION_AND_TEST_SPEC_V0.5.4.md`
and the working tree at commit `7830e49`.

**Prompt:** a Council Once run (`20260901T082925Z-63udxmkg6i`) in which the
Moderator emitted a preliminary answer, performed three web searches, then stopped
producing events and was killed at the configured five-minute per-turn timeout,
ending the run as `MODERATOR_FAILED`. The question raised: *why do we impose time
limits at all — wouldn't the models themselves know how much time to use, given the
effort level?*

No code was changed. Section 3 is a read-only investigation of the current
implementation.

---

## 1. Opinion: the mechanism is right, the primitive is wrong

My first answer to this question defended the mechanism and then handed the hard
part back to the user as a settings dropdown. That was correct but slightly
cowardly — a dropdown just relocates the guess.

The real problem is that one primitive is being used for two different failure
modes:

- **A process that is hung.** CLI wedged, socket dead, provider never emits another
  byte. This should die fast; five minutes is already too generous.
- **A turn that is legitimately long.** A Moderator doing real live-web synthesis
  across a dozen searches. This needs room, and a wall-clock deadline punishes it
  for doing the work well.

A single wall-clock timeout cannot tell those apart, so **any value chosen is wrong
for one of them.** That is why the knob feels unsatisfying no matter where it is
set, and why exposing it in an Advanced section does not actually resolve anything.

### The alternative

Kill on **silence**, not on **duration**. An idle watchdog resets its timer on every
event the provider emits — token, tool call, search result, status frame. Roughly
90 seconds of no output kills the turn, plus a much larger absolute ceiling
(30–60 min) retained purely as a backstop against a pathological loop.

Applied to the run above: the Moderator emitted an answer and three searches, then
went silent. An idle watchdog would have caught it at roughly 90 seconds instead of
300 — *faster* failure detection — while simultaneously letting a genuinely
productive 20-minute research turn run to completion. Both properties at once, which
the current design cannot provide.

### On "the model knows how much effort it needs"

Half right, and it is the interesting half. Effort level should absolutely inform
the *budget*. But the model cannot be the watchdog, because the thing that fails is
not the model's judgment; it is the layer underneath it:

- The model does not control CLI, network, or web-tool hangs.
- Providers make no promise that "high effort" completes within any duration.
- A model may search indefinitely, or never emit the final structured response.
- Dialectic must eventually terminate and clean up the owned process unit.

A wedged pipe emits no opinion about how long it needs. The supervisor has to stay
external and dumb.

---

## 2. What the run actually did

- Run: `20260901T082925Z-63udxmkg6i`
- All openings and cross-examinations completed.
- Moderator candidate synthesis started 16:35:05 SGT, performed three web-search
  actions, then stopped producing events.
- Killed at the five-minute per-turn timeout at 16:40:10.
- Final status: `MODERATOR_FAILED`. No provider process remained.

The timeout protection worked. This is unrelated to the Codex JSON parser bug; the
parser fix held throughout the run.

---

## 3. Investigation of the current implementation

### 3.1 Two timeouts, both pure wall clock

**Per-turn** — `ProcessSupervisor.supervise`, `src/dialectic/process.py:58`:

```python
done, _ = await asyncio.wait(
    {root_wait, cancel_wait, overflow_wait},
    timeout=turn_timeout_seconds,
    return_when=asyncio.FIRST_COMPLETED,
)
```

Fed from `limits.agent_turn_seconds` — default `300` (`src/dialectic/ui_config.py:49`),
bounded `1..3600` (`src/dialectic/schemas.py:206`).

**Whole-run** — `src/dialectic/council_once.py:168` races the workflow task against
`asyncio.sleep(council_run_seconds)`; Code Once uses
`asyncio.timeout(code_run_seconds)` at `src/dialectic/code_once.py:248`. Both
default `1200`.

Two further wrappers apply the same 300s at the adapter layer —
`asyncio.wait_for(invocation, ...)` at `src/dialectic/council_once.py:846` and
`src/dialectic/workflow_evidence.py:344` — plus the ACP JSON-RPC deadline at
`src/dialectic/acp_transport.py:508`.

### 3.2 The finding that matters: no liveness signal exists

**The supervisor has no liveness input whatsoever.** The stream readers can raise
exactly one event to it, and it is not "output happened"
(`src/dialectic/native_process.py:133`):

```python
if capture.feed(chunk):
    overflow.set()
```

The only signal a reader sends upward is *too much* output.
`BoundedStreamCapture.feed` (`src/dialectic/redaction.py:178`) tracks bytes and a
one-shot overflow flag, with no timestamp. `CapturedStream` and
`StreamCaptureResult` carry counts and hashes, no last-activity time.

There is therefore no wire anywhere in the system carrying "last byte at T".

This is worse than a missing feature: **the stall cannot be diagnosed from the
artifacts either.** Attempt evidence records `started_at`, `capture_completed_at`,
and `attempt_end_reason="timeout"` — nothing separating "silent for four minutes"
from "streaming productively right up until the axe fell." The claim that the
Moderator "went silent" came from the event log, not from anything the supervisor
recorded.

### 3.3 The spec is more permissive than expected

§9, line 1559: *"Every agent turn has an individual timeout, and each workflow has
one overall wall-clock timeout."* The words **wall-clock** attach to the workflow,
not to the turn.

§6.3 (line 1103 and the failure table) constrains only the *consequence* of a turn
timeout — a phase-specific kind, `MODERATOR_FAILED` here — and its precedence:
cleanup-failure > cancellation > overall timeout > individual-turn timeout. Nothing
dictates the trigger function.

An idle-based per-turn deadline therefore appears spec-compatible. **The overall
wall clock must stay wall-clock.**

One snag: §5.4.5, line 548 names the field explicitly — *"ends the logical turn's
`agent_turn_seconds` response deadline"* — so redefining that field's meaning
reaches into ACP session semantics as well.

### 3.4 Cost

Not a one-liner, but structurally clean. The supervisor change itself is small: it
already multiplexes a task set, so an idle budget that resets on an activity event
is one rewritten function.

The work is plumbing an activity signal out of dissimilar readers:

1. POSIX asyncio readers (`src/dialectic/native_process.py`).
2. The Windows *threaded* pipe handoff with its epoch machinery
   (`src/dialectic/process.py:510` onward) — cross-thread, the awkward one.
3. The ACP JSON-RPC notification dispatcher.

That third one is worth calling out: `src/dialectic/acp_transport.py:444` already
parses `session/update` notifications. It is the cleanest liveness source in the
codebase and is currently unused for this purpose.

Config side: `LimitsSpec` is a `ClosedModel`, so a new field means schema + bound +
`ui_config` default + spec §3 table + a CORE-016 expectation. `AGENTS.md` freezes
the test inventory at 108 IDs, so this strengthens CORE-007 (agent timeout with
delayed grandchild) rather than adding a row.

### 3.5 The open question that could kill the idea

An idle watchdog is only as good as its signal. For the Codex native path the reset
would be driven by stdout bytes, which cuts both ways:

- A provider emitting progress chatter while genuinely wedged upstream keeps the
  timer alive indefinitely. Hence the absolute ceiling stays — non-negotiable.
- Worse: **a provider that buffers and emits nothing until its final response would
  be killed by an idle timer that wall-clock tolerated fine.**

Whether the Codex CLI streams incrementally in the mode Dialectic invokes it is the
load-bearing question, and it is **unverified**. If it does not stream, the idea
collapses for that adapter and survives only for Grok/ACP. That is the first thing
to check before any implementation work.

---

## 4. Summary position

Keep hard limits — the controller, never a model, owns timeouts, and that is a
settled decision. But split the primitive:

| Concern | Trigger | Rough default |
|---|---|---|
| Hung / silent provider | Idle since last emitted event | ~90 s |
| Pathological but live turn | Absolute per-turn ceiling | 30–60 min |
| Whole run | Overall wall clock (unchanged) | 20–30 min |

The safety boundary is preserved, failure detection gets *faster*, and a substantial
live-research Moderator is no longer given the same five-minute budget as a simple
offline response.

Blocked on §3.5 before this is worth building.
