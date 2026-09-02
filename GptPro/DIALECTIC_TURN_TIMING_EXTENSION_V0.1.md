# Dialectic Turn Timing Extension v0.1

Status: normative post-baseline extension.

This document extends `DIALECTIC_MVP_IMPLEMENTATION_AND_TEST_SPEC_V0.5.4.md`.
All baseline workflow bounds, phase counts, failure mappings, cleanup precedence,
controller authority, and no-retry behavior remain in force unless this document
says otherwise.

## 1. Controller authority and timing model

The controller, never a model or provider process, owns logical-turn deadlines.
Each logical turn has:

- an initial allotted deadline from `limits.agent_turn_seconds`;
- a fixed hard ceiling of 3600 seconds from logical-turn registration;
- for qualified streaming runtimes only, a fixed 90-second observable-activity
  watchdog; and
- zero or more controller-authorized five-minute allotted-deadline extensions.

The workflow-level `code_run_seconds` or `council_run_seconds` deadline remains an
independent wall clock. Turn extensions MUST NOT move or pause that workflow wall
clock.

The matching terminal response ends the logical response deadline described by
baseline section 5.4.5. A retained ACP capture epoch may continue afterward exactly
as specified by the baseline.

## 2. Fixed policy constants

The version-0.1 timing policy fixes these controller-owned values:

| Policy | Value |
|---|---:|
| Observable-activity watchdog | 90 seconds |
| Logical-turn hard ceiling | 3600 seconds |
| User extension increment | 300 seconds |

These are product policy constants, not `LimitsSpec` fields. A hand-authored
configuration continues to control the initial allotment through
`agent_turn_seconds` within its existing `1..3600` range and the independent
workflow wall clocks through their existing fields. It cannot weaken the fixed
watchdog, raise the hard ceiling, or alter the extension increment. Constructor
overrides used by offline tests do not create a configuration surface.

This extension therefore does not add configuration fields or test IDs. A future
tunable timing policy requires a new normative revision with explicit hard ranges,
configuration validation, UI behavior, and strengthened frozen-row coverage.

## 3. Allotted deadline and extension

At registration, the controller sets the allotted deadline to the earlier of
`agent_turn_seconds` and the fixed hard ceiling. A `+5 min` action extends every
currently active logical turn by exactly 300 seconds, independently clamping each
deadline to its own registration-time hard ceiling.

An extension:

- MUST NOT reset or move an activity watchdog;
- MUST NOT move the workflow wall clock;
- MUST NOT revive a completed, expired, cancelled, or unregistered turn;
- MUST report how many active turns actually moved; and
- MUST be refused when there is no active turn or every active turn is already at
  its hard ceiling.

The first-party browser endpoint remains controller-owned and MUST retain its
existing same-origin and session-cookie checks. Models receive no extension tool,
prompt affordance, or authority to mutate their own deadlines.

## 4. Observable activity

The 90-second watchdog applies only when the qualified native transport provides a
continuous, controller-observable activity signal:

- Codex: admitted native stdout or stderr bytes.
- Grok Build ACP: a valid dispatched `session/update` notification.

Each signal atomically records the controller monotonic clock and wakes the logical
deadline waiter. Activity that arrives after deregistration is ignored and MUST NOT
touch a closed event loop. Provider-authored timestamps are never authoritative.

The watchdog begins at logical-turn registration. When both allotted and idle
deadlines exist, the earlier deadline wins. Output chatter may keep the idle
watchdog alive but cannot move the fixed hard ceiling.

Claude Code is deliberately excluded because its qualified print-mode transport is
buffered and does not provide a safe incremental liveness signal. A Claude Code
turn therefore uses only its allotted deadline and the fixed hard ceiling. The
first-party UI default is 1800 seconds per turn and 3600 seconds per workflow, so a
silent Claude turn may occupy its full 30-minute allotment. This is an explicit
availability tradeoff, not an assertion that buffered silence proves progress.

## 5. Transport layering and cleanup

The logical deadline controller is authoritative for the allotted and idle
deadlines. A native per-turn transport receives the 3600-second hard ceiling as its
flat wall-clock backstop, not the current allotted deadline. Otherwise a transport
timeout would make extension ineffective.

When a logical deadline expires, the controller cancels the invocation. Native
adapters MUST translate that cancellation into their existing supervisor-owned
graceful-then-force termination path, await confirmed process/reader/handle cleanup,
and only then propagate the timeout. `PROCESS_CLEANUP_FAILED` retains baseline
precedence over the initiating timeout.

Persistent ACP participant sessions follow the same rule. No retained lease or
capture epoch may survive terminal run persistence.

## 6. Diagnostics and failure mapping

Logical expiry evidence distinguishes:

- `idle`: no observable provider activity for the fixed watchdog interval; and
- `allotted`: the current allotted deadline was reached.

The bounded diagnostic MUST preserve that controller-authored distinction in the
adjacent turn attempt and in the workflow failure detail, including the persistent
ACP participant path. It MUST NOT include provider output or credential values.

An individual logical-turn expiry retains the baseline phase-specific failure kind.
For a persistent council participant it maps to `NO_QUORUM`; for a moderator it
maps to `MODERATOR_FAILED`; and only expiry of the independent workflow wall clock
produces terminal status `TIMED_OUT`.

## 7. Presentation

Both first-party UIs display allotted time separately from the activity watchdog,
show parallel-turn count when applicable, and enable `+5 min` only while at least
one active turn can move. Presentation polling is non-authoritative; the service's
deadline controller remains the sole timing authority.

## 8. Verification

The frozen inventory remains 108 IDs. CORE-007 is strengthened to prove:

- normal process-supervisor timeout still reaps the complete owned unit;
- a logical turn can complete after an allotted extension;
- observable activity resets the streaming watchdog;
- streaming silence reports `idle` expiry; and
- the hard ceiling cannot be weakened by extension.

Existing Council failure/cleanup coverage is strengthened to require the bounded
idle-versus-allotted diagnostic to survive the persistent participant path. UI tests
cover separate allotted/watchdog presentation and extension availability. No new
test ID is introduced.
