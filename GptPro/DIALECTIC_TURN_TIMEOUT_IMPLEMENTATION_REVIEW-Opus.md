# Review: Turn Timing Controls and Activity Watchdogs

**Author:** Opus

**Date:** 2026-09-02

**Type:** Implementation review. Verifies the issues raised in
`DIALECTIC_TURN_TIMEOUT_NOTE-Opus.md` against the code that landed. Not normative.

**Subject:** commits `4256552` (*Add turn timing controls and activity watchdogs*),
`136dcc5`, and `7a0d7b6`, reviewed at `HEAD` with a clean working tree.

**Verdict:** all three issues from the note are genuinely fixed. Four follow-up
items are raised in §4, of which the unreconciled specification is the one worth
acting on. No code was changed by this review.

---

## 1. Verification

```
211 passed, 13 skipped in 187.72s (0:03:07)
```

Exit code 0, `pytest -q` against the offline suite. The 13 skips are all legitimate
guards: nine platform contracts (Win32 Unicode paths, unprivileged Win32 symlinks,
POSIX FIFO/socket/fd-relative races) and four cost-bearing live tests gated behind
`DIALECTIC_LIVE=1`.

**Process note:** an earlier run of the same command reported 170 errors and an
apparent exit code 0. Both readings were wrong. The errors were the sandbox denying
pytest's temp root (`PermissionError: [WinError 5] ... pytest-of-user`), which fails
every `tmp_path` fixture at setup; the exit code belonged to `tail`, because the
command had been piped. Re-running with `--basetemp` pointed at a writable directory
and without a pipe produced the result above.

---

## 2. Issues from the note — status

### 2.1 Idle watchdog — FIXED

New module `src/dialectic/turn_timing.py` introduces `TurnDeadlineController` with:

| Constant | Value |
|---|---|
| `IDLE_WATCHDOG_SECONDS` | `90.0` |
| `MAXIMUM_TURN_SECONDS` | `3_600.0` |
| `TURN_EXTENSION_SECONDS` | `300.0` |

Idle trigger plus a retained absolute ceiling — the shape the note argued for.
`_remaining()` returns whichever of the idle and allotted deadlines is nearer,
tagged with its reason, so the expiry message names the actual cause.

### 2.2 The missing liveness wire — FIXED

The note's central finding was that no signal anywhere in the system carried
"last byte at T", and that the only event a reader could raise was *overflow*. All
three reader paths now emit activity:

- POSIX asyncio reader — `src/dialectic/native_process.py:138`
- Windows threaded pipe reader — `src/dialectic/process.py:615`
- ACP `session/update` dispatcher — `src/dialectic/acp_transport.py:469`

The third is the source the note identified as the cleanest available and unused;
it is now the liveness source for Grok.

**Cross-thread safety is correct.** `record()` (`turn_timing.py:81`) acquires a
`threading.RLock` and marshals to the loop via `call_soon_threadsafe`, with both the
registry lookup and the loop call *inside* the lock. A Windows reader thread firing
after the turn deregisters therefore finds no entry and never touches a dead loop.
This was the awkward integration the note flagged; it is handled properly.

### 2.3 The §3.5 open question — RESOLVED CONSERVATIVELY

The note called the Codex streaming question load-bearing: an idle timer would kill
a provider that buffers until its final response. The implementation answers it by
opting in only where streaming is established:

```python
_STREAMING_RUNTIMES = frozenset({"codex", "grok-build"})
```

with `claude-code` excluded outright at `native_adapters.py:401`. The README states
the reasoning — Claude Code's current output mode is buffered, so it uses the
absolute allotment rather than an unsafe idle assumption. This is the right
resolution: the watchdog applies only where incremental output is a property of the
transport, not an assumption about it.

### 2.4 Diagnosability — FIXED

The note observed that the artifacts could not distinguish "silent for four minutes"
from "streaming steadily until the axe fell". `TurnDeadlineExpired` now carries
`reason` (`"idle"` or `"allotted"`) and a message naming the silence duration, and
`workflow_evidence.py:363` persists `str(exc)` as the attempt diagnostic.

---

## 3. Integration points that could have gone wrong

**Deadline layering — correct.** `ProcessSupervisor` still uses a flat wall clock
(`process.py:58`), but it is now fed `turn_max_seconds = MAXIMUM_TURN_SECONDS`
(3600) through `_turn_transport_timeout` (`native_adapters.py:405`,
`native_runtime.py:87`) rather than the turn allotment. The outer controller owns the
real deadline; the supervisor is a pure backstop. Had this been left at
`request.timeout_seconds`, the inner wall clock would have fired first and the
**+5 min** control would have been silently useless.

**Cancellation still reaps.** `native_adapters.py:452` catches `CancelledError`, sets
the `cancellation` event, then re-awaits the *shielded* task so the supervisor
completes graceful → force → `confirm_cleanup` before re-raising. An idle kill
therefore cannot leak a process unit — the property the spec's
`PROCESS_CLEANUP_FAILED` path depends on.

**Failure mapping matches §6.3.** `TurnDeadlineExpired` subclasses `TimeoutError`, so
it lands in the existing `asyncio.TimeoutError` arm and maps to the phase-specific
kind; the ACP participant path maps it to `NO_QUORUM` (`council_once.py:900`). Only
the overall wall clock still produces `TIMED_OUT`.

**Extension cannot weaken the ceiling.** `extend_active` clamps to
`maximum_deadline_monotonic`; `service.py:111` refuses when no run is active and when
every active turn has reached the ceiling. `/api/extend` sits behind the existing
session-cookie and same-origin check (`ui.py:565`), which runs before route dispatch.

**Test freeze respected.** Watchdog coverage is folded into the existing CORE row
(`tests/test_core.py:289`, exercising extension, activity-resets-idle, and idle
expiry) rather than adding an ID, and the `== 108` assertion still stands at
`tests/test_council_once.py:1063`.

---

## 4. Follow-up items

### 4.1 The specification was not updated — the one worth acting on

`git diff 7830e49..HEAD -- GptPro/` shows only two new *note* files.
`DIALECTIC_MVP_IMPLEMENTATION_AND_TEST_SPEC_V0.5.4.md` is untouched. Its §9 still
reads:

> Every agent turn has an individual timeout, and each workflow has one overall
> wall-clock timeout.

There is no mention of an idle watchdog, of a hard ceiling distinct from
`agent_turn_seconds`, or of user-initiated extension. `AGENTS.md` is explicit that
**when code and spec disagree, the spec wins**. The code now carries a
controller-owned timing model and a new user-facing control that mutates a running
turn's deadline, with no normative home — the same class of change that received
dedicated extension documents for moderator mode and web research.

### 4.2 New constants bypass `LimitsSpec`

`IDLE_WATCHDOG_SECONDS`, `MAXIMUM_TURN_SECONDS`, and `TURN_EXTENSION_SECONDS` are
module-level constants in `turn_timing.py`. Every other safety bound in this codebase
goes through `LimitsSpec` with an explicit range and CORE-016 configuration
coverage. These three cannot be tuned by a hand-authored config and are invisible to
the freeze-check.

### 4.3 The `claude-code` path is now six times looser

Defaults moved `agent_turn_seconds` 300 → 1800 and both run limits 1200 → 3600
(`ui_config.py`). For streaming runtimes the increase is offset by the 90-second idle
watchdog. `claude-code` is excluded from that watchdog, so its only protection is now
the 1800-second allotment: a hung Claude Code turn ties up a run six times longer
than before. This is deliberate and documented, but it is the one place where the
change is strictly a loosening rather than a tightening.

### 4.4 Minor: diagnostic loss on the ACP participant path

`council_once.py:901` collapses `TimeoutError` into the fixed string
`"persistent participant turn failed"`, discarding `TurnDeadlineExpired`'s
"no observable provider activity for 90 seconds". The native path preserves it via
`str(exc)`. Same information available, one path drops it.

---

## 5. Summary

The design the note recommended was implemented faithfully and, in the two places
where a subtle error would have made it useless or unsafe — inner deadline layering
and cancellation cleanup — implemented correctly. The offline suite passes at 211.

What remains is reconciliation, not correctness: the frozen baseline does not yet
describe the timing model the controller now owns.
