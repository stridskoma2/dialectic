# Dialectic whole-repository code review — Opus

Reviewer: Claude Opus 5
Date: 2026-09-01
Scope: entire repository at `main` @ `4256552` (working tree clean) — 13.5k lines of
`src/dialectic`, plus tests, packaging, CI, and the launcher scripts.
Effort: extra-high (recall-oriented; uncertain findings are surfaced and labelled).

## How to read this

Each finding is labelled with its verification status:

- **Verified** — reproduced or observed directly this session; the evidence is quoted.
- **Read** — derived from reading the code and its call sites, not executed.

Severity is about product impact, not code aesthetics. Findings 20–28 are cleanup
(reuse, simplification, efficiency, altitude) and carry no correctness claim.

## Suite status

`pytest -q` **cannot run to completion on this machine**: 170 of 224 tests error at
fixture setup with `PermissionError: [WinError 5] Access is denied:
'C:\Users\user\AppData\Local\Temp\pytest-of-user'`. That directory exists with an ACL
that denies the current account even `Get-Acl`. It is **not a repository defect** —
pointing pytest elsewhere makes the suite green:

```
python -m pytest -q --basetemp=<clean dir>
211 passed, 13 skipped in 181.45s
```

Removing the poisoned directory restores the documented `pytest -q` command.

None of the correctness findings below are caught by the offline suite. Findings 1, 6,
7 and 8 live on native paths exercised only by the opt-in `-m live` tests; finding 18
explains why the largest module in the repo has no CI coverage at all.

---

## Critical

### 1. Driver `--output-schema` file breaks reserved-turn cleanup on every repair turn

`src/dialectic/native_adapters.py:2033` · **Verified**

For a `driver-write` request with an output schema, `_write_schema_file` writes
`output-schema.json` into `.dialectic-turn/control/`:

```python
if request.access_mode == "driver-write":
    directory = bound.dynamic_paths["turn_scratch_control"]
    filename = "output-schema.json"
```

`TurnWorkspace.verify_and_cleanup` requires that directory to hold exactly one entry:

```python
entries = list(os.scandir(self.control))
if len(entries) != 1 or entries[0].name != self.output.name:   # turn_workspace.py:70
    raise TurnWorkspaceError("turn control directory contains unexpected entries")
```

**Path to failure.** Any native Code Once run whose reviewers return findings reaches
`DRIVER_REPAIR` with `output_schema=repair_schema` (`code_once.py:430`).
`CodexAdapter._turn_arguments` writes the schema into `control/`, then the `finally` at
`code_once.py:449` calls `_cleanup_driver_scratch` → `verify_and_cleanup`, which sees
two entries and raises. The orchestrator converts that to
`DialecticFailure("INTERNAL_ERROR", "reserved driver workspace validation failed")`.

**Reproduced.** Creating a `TurnWorkspace`, dropping `output-schema.json` into
`control/`, and calling `verify_and_cleanup` raises `TurnWorkspaceError: reserved turn
workspace validation failed`. Nothing anywhere deletes the schema file first
(`grep -rn "output-schema" src/` confirms only the write).

**Direction.** Either place the driver schema under `tmp/` (it is controller-authored,
so the control/tmp security split is about *who writes*, not where the controller may
read from), or teach `verify_and_cleanup` an explicit allowlist of controller-authored
control-directory filenames. The second keeps the invariant legible; the first keeps
the check maximally strict. Whichever is chosen, the spec's control/tmp clause should
be the arbiter.

### 2. `NativePreflightError` is used but never imported in `council_once`

`src/dialectic/council_once.py:890` · **Verified**

`_persistent_call`'s handler references a name the module never imports:

```python
if isinstance(exc, NativePreflightError):    # NameError
    raise DialecticFailure("NO_QUORUM", ...)
if isinstance(exc, (NativeEnvelopeError, AgentProcessError, TimeoutError)):
    raise DialecticFailure("NO_QUORUM", "persistent participant turn failed")
raise DialecticFailure("NO_QUORUM", f"participant invocation failed: {type(exc).__name__}")
```

The imports at `council_once.py:57` bring in only `NativeEnvelopeError`,
`NativeInvocationEvidence` and `NativeTurnError`.

**Verified:** `'NativePreflightError' in dir(dialectic.council_once)` is `False`.

**Path to failure.** Any persistent ACP participant turn that fails with something
other than `DialecticFailure` / `ModelMismatchError` / `NativeTurnError`-with-kind
reaches this line — including `TurnDeadlineExpired` (a `TimeoutError`) raised by
`context.turn_deadlines.wait_for`, `NativeEnvelopeError`, and `AgentProcessError`. The
handler raises `NameError` instead of the intended failure, and the three branches
below it are unreachable dead code. The cohort still ends as `NO_QUORUM`, but with a
generic diagnostic and none of the intended per-cause detail.

**Direction.** Add the import; then confirm the now-live branches are actually the
intended mapping (in particular that `TimeoutError` should be `NO_QUORUM` rather than a
timeout-shaped outcome).

---

## High

### 3. An empty allowlisted credential variable blocks every run

`src/dialectic/redaction.py:36` · **Verified**

`from_environment` collects any allowlisted name that is *present*, then `__init__`
rejects short values, conflating "set but empty" with "boundary violation":

```python
for supplied_name, value in environment.items():
    if key in by_name:
        credentials.append(KnownCredential(by_name[key], value))   # includes ""
...
if credential.value == "" or len(credential.value) < 8:
    raise CredentialBoundaryError(...)
```

**Reproduced** against a real code-mode config:

| environment | result |
| --- | --- |
| `OPENAI_API_KEY` absent | OK (0 credentials) |
| `OPENAI_API_KEY=""` | `CredentialBoundaryError: ... must contain at least eight Unicode scalar values` |
| `OPENAI_API_KEY="short"` | same |

`DialecticService._execute` turns that into a persisted `FAILED` /
`PREFLIGHT_FAILED` run. Codex authenticates through `codex login status` / `auth.json`,
not the API key, so a leftover cleared variable that has no bearing on the run blocks
the product entirely, with a diagnostic that points at the wrong thing.

**Direction.** Treat an empty value as absent (skip it in `from_environment`); keep the
minimum-length rejection for values that are actually present. The `value == ""` clause
in the length check then becomes redundant and should go.

### 4. Deeply nested configuration escapes as an unhandled `RecursionError`

`src/dialectic/config.py:177` · **Verified**

Neither `_parse_yaml` nor the recursive `_validate_json_compatible` bounds nesting
depth. `MAX_JSON_DEPTH` exists in `contracts.py:14` but is applied only to *model
output* (`output.py:_validate_json_nesting`), never to configuration.

**Reproduced.** A 12 021-byte config — far under `limits.max_config_bytes` (65 536) and
`MAX_NAMED_INPUT_BYTES` (262 144) — containing `limits: {a:{a:{a:…3000 deep…}}}`
produces:

```
UNCAUGHT RecursionError maximum recursion depth exceeded
```

`DialecticService._execute` wraps the loader in `except ConfigError` only, so the
`RecursionError` escapes `_execute` entirely: the CLI prints a raw traceback
(`pretty_exceptions_enable=False`) and both UIs report `UI_ERROR`, instead of the
contractual `FAILED` / `INVALID_INPUT` record and exit code 2.

**Direction.** Apply `MAX_JSON_DEPTH` during config parsing, the same bound the model
output path already enforces — this is the "generalize the existing mechanism" fix
rather than a `try/except RecursionError` bandaid.

### 5. `GitRunner` inherits `GIT_DIR` and the rest of git's environment

`src/dialectic/git_workspace.py:89` · **Verified**

```python
environment = os.environ.copy()
environment.update({"LC_ALL": "C", "LANG": "C",
                    "GIT_OPTIONAL_LOCKS": "0", "GIT_PAGER": "cat"})
```

`GIT_DIR`, `GIT_WORK_TREE`, `GIT_INDEX_FILE`, `GIT_CONFIG_GLOBAL`,
`GIT_CONFIG_NOSYSTEM`, `GIT_TERMINAL_PROMPT` and `GIT_ASKPASS` all pass through
untouched, for every controller-owned Git operation.

**Demonstrated.** In a repository `repoA`:

```
$ git rev-parse --show-toplevel                 → .../repoA
$ GIT_DIR=../repoB/.git GIT_WORK_TREE=../repoB \
  git rev-parse --show-toplevel                 → .../repoB
```

**Path to failure.** Launch Dialectic from a git hook, `git rebase --exec`, or an IDE
task that exports `GIT_DIR`, and every `runner.run(..., cwd=…)` call — preflight,
`worktree add`, `add -A`, `commit`, diff validation — targets the environment's
repository rather than `cwd`, while reporting success for the one the user named. The
absence of `GIT_TERMINAL_PROMPT=0` separately allows a credential prompt to hang until
the 30-second timeout.

This is inconsistent with the repo's own practice: the capability-probe helpers at
`native_adapters.py:1481` and `1521` already set `GIT_CONFIG_NOSYSTEM` and
`GIT_TERMINAL_PROMPT=0`. The controller — which AGENTS.md says owns all Git
operations — is the weaker of the two.

**Direction.** Build the git environment from an explicit allowlist the way
`_trusted_environment` does for agent processes, rather than copying and patching.

### 6. Capability probe builds an invalid `TurnPhase` for the moderator role

`src/dialectic/native_adapters.py:1420` · **Verified**

```python
def _probe_turn_phase(role: Role) -> str:
    return {"driver": "initial", "reviewer": "review",
            "participant": "opening", "moderator": "moderation"}[role]
```

`"moderation"` is not one of the seven `TurnPhase` literals (`contracts.py:42`).

**Verified:** constructing the probe request raises

```
ValidationError: turn_phase — Input should be 'initial', 'repair', 'review',
'opening', 'cross-examination', 'candidate' or 'ballot' [input_value='moderation']
```

**Path to failure.** A council config naming a `claude-code` moderator reaches
`ClaudeAdapter._native_capability_probe`, which builds
`AgentRequest(..., turn_phase=_probe_turn_phase(self.role))`. The probe never runs, no
attestation can be produced, and preflight fails for every Claude Code moderator
target. `CodexAdapter._run_probe_turn` avoids this only because it hardcodes a
separate, correct mapping — see finding 7.

**Direction.** The council moderator's real phase is `candidate`. Fix the mapping, and
make it the single shared source (findings 7 and 25 are the same disease).

### 7. Grok's probe repeats the invalid mapping in a second copy

`src/dialectic/grok_acp.py:215` · **Read** (same literal as finding 6, verified there)

`GrokAdapter._native_capability_probe` inlines its own copy rather than calling
`_probe_turn_phase`:

```python
turn_phase={"reviewer": "review", "participant": "opening",
            "moderator": "moderation"}[self.role],
```

Same defect, independently. Fixing `_probe_turn_phase` alone leaves this call site
broken, so a `grok-build` moderator can never be preflighted. The inline dict also
omits `"driver"` entirely, so it would `KeyError` rather than fail cleanly if the role
set ever widened.

### 8. Windows graceful termination is silently never delivered

`src/dialectic/windows_process.py:235` · **Read**

```python
def request_graceful_termination(self, process: object) -> None:
    handle = self._require_handle(process)
    if handle.process_id is not None:
        self._kernel32.GenerateConsoleCtrlEvent(_CTRL_BREAK_EVENT, handle.process_id)
```

Two problems compound:

1. The child is created with `_CREATE_NO_WINDOW` (`windows_process.py:205`), so it is
   not in the caller's console. `GenerateConsoleCtrlEvent` can only signal process
   groups attached to the *calling* process's console. When the host is `pythonw.exe`
   — which `Launch Dialectic UI.ps1` prefers for both frontends — there is no console
   at all.
2. The `BOOL` return value is discarded, so a failed signal is indistinguishable from
   a delivered one.

**Consequence.** On every Windows timeout, output-limit overflow and user cancellation,
`ProcessSupervisor.supervise` (`process.py:75`) requests graceful termination, waits out
the full `graceful_kill_seconds`, and then hard-kills the whole Job with
`TerminateJobObject`. The agent never gets the chance to flush or clean up, and nothing
records that the graceful step was a no-op.

`terminate_job` (`windows_process.py:239`) compounds this by swallowing
`ERROR_ACCESS_DENIED` (5) as success.

**Direction.** Check and log the return value at minimum. Delivering a real graceful
signal to a `CREATE_NO_WINDOW` child needs a different mechanism (a shared console
group, or a cooperative shutdown protocol over the existing stdin channel).

---

## Medium

### 9. `RunWorker.request_cancel` can post to a closed event loop

`src/dialectic/desktop_qt.py:120` · **Read**

The GUI thread reads `self._loop` / `self._task` and then calls
`loop.call_soon_threadsafe(task.cancel)`, while the worker thread's `finally`
concurrently runs `self._task = None; self._loop = None; loop.close()`
(`desktop_qt.py:228`). There is no lock, no `isRunning()` check, and no `try/except`
around the call site.

**Path to failure.** Cancel is clicked as the run completes: the GUI thread passes
`not task.done()`, the worker closes the loop, and `call_soon_threadsafe` raises
`RuntimeError: Event loop is closed` inside a Qt slot. Neither `_cancel_run`
(`desktop_qt.py:1198`) nor `closeEvent` (`desktop_qt.py:1459`) handles it; in the
`closeEvent` path `_close_after_run` has already been set but `event.ignore()` leaves
the window open indefinitely with a disabled Cancel button.

### 10. Response redaction skips JSON object keys

`src/dialectic/workflow_evidence.py:692` · **Read**

```python
def redact(value: Any) -> Any:
    if isinstance(value, str):
        return context.credentials.redact_text(value)
    if isinstance(value, dict):
        return {key: redact(child) for key, child in value.items()}   # keys pass through
```

A credential echoed back as an object key — `{"sk-live-…": 1}` in `structured_output`
or `usage` — survives into `turns/<role>/<id>/<phase>.attempt.json` in clear text,
defeating the known-value boundary that `redact_bytes` / `redact_text` enforce on every
other persisted channel.

**Direction.** `redact_text` the key as well as the value.

### 11. Ballot report ordering depends on task completion order

`src/dialectic/council_once.py:1412` · **Read**

`derived.append(artifact)` runs inside the concurrent per-participant `ballot`
coroutine (`council_once.py:695`), so `_report_lines` iterates ballots in completion
order:

```python
for ballot in ballots:
    if ballot.ballot.minority_report:
        lines.append(f"- {ballot.participant_alias} minority report: …")
```

With two dissenting participants, `summary.md` — a persisted run artifact — differs
byte-for-byte between otherwise identical runs. Every other artifact in the module is
deterministically ordered (`_position_ledger`, `_revision_ledger`, and the vote matrix
all iterate `participants`); only this section is not.

**Direction.** Order the dissent section by `participants`, as the vote matrix already
does via `by_alias`.

### 12. `force_terminate` signals a possibly recycled process group

`src/dialectic/process.py:81` · **Read**

On a *normal* root exit the supervisor still calls `force_terminate()`:

```python
else:
    # A normal root exit still owns and reaps lingering unit members.
    await unit.force_terminate()
```

For `PosixProcessUnit` that is `os.killpg(self.process_group, SIGKILL)` issued after
`process.wait()` has already reaped the group leader. If no members remain, the kernel
is free to recycle that pgid; on a host that wraps PIDs the signal lands on an
unrelated process group. `confirm_cleanup` has the mirror-image exposure — a recycled
pgid keeps `killpg(pgid, 0)` succeeding, so cleanup is reported unconfirmed and a
healthy turn fails as `PROCESS_CLEANUP_FAILED`.

The intent (reap stragglers) is right; the mechanism is unsafe once the leader is
reaped.

### 13. WSL bridge token is passed on process command lines

`Launch Dialectic UI.ps1:29` · **Verified** (by reading the launcher and `windows_bridge.main`)

```powershell
$bridgeArguments = "-m dialectic.windows_bridge --directory `"$bridgeDirectory`" --token $bridgeToken"
...
$bridgeEnvironment = "DIALECTIC_WINDOWS_BRIDGE_DIR=$quotedBridgeDirectory DIALECTIC_WINDOWS_BRIDGE_TOKEN=$bridgeToken "
```

The token appears in the `pythonw` command line (line 29) and again inside the
`wsl.exe` argument string (line 47). Any local process able to enumerate command lines
— Task Manager details, `Get-CimInstance Win32_Process`, `wmic process get
commandline`, `ps` inside the distro — can read it while Dialectic is open, then write
its own `request-<id>.json` into the bridge directory to drive `_choose_repository` and
harvest returned filesystem paths.

`windows_bridge.main` (`windows_bridge.py:99`) accepts the token only via `argparse`,
so there is currently no other channel. The environment-variable mechanism the script
already uses for the WSL client side does not have this exposure.

**Direction.** Give `windows_bridge` an env-var (or stdin) token channel and stop
putting the secret on argv.

### 14. Moderator shares one working directory across the blind opening and the "fresh" synthesis

`src/dialectic/council_once.py:567` · **Read**

In `independent-opening` mode the neutral role directory created for the moderator's
blind opening turn (`council_once.py:340`) is reused as the working directory for the
later `candidate` synthesis turn:

```python
if moderator_directory is None:
    moderator_directory = context.service.store.create_role_directory(...)
```

AGENTS.md and the moderator-mode extension require the synthesis to come from a *fresh*
moderator. That holds at the process/session level, but the two turns share a writable
CWD, so anything the opening turn leaves behind — a note, a scratch file, its own
`output-schema-opening.json` — is readable by the session that is supposed to be blind
to it.

**Direction.** Give the moderator a phase-specific role directory, the way participants
already get per-target directories. That makes freshness structural rather than
dependent on the model not looking.

### 15. Scratch cleanup in `finally` masks the failure that caused it

`src/dialectic/code_once.py:312` · **Read**

```python
finally:
    self._cleanup_driver_scratch(initial_scratch, context)
```

`_cleanup_driver_scratch` raises `DialecticFailure` on failure. When the driver turn has
*already* failed (say `DRIVER_FAILED`) and cleanup then also fails, the `finally`'s
exception replaces the original, so the run reports `PROCESS_CLEANUP_FAILED` and the
actual cause is lost from `failure_kind` / `failure_detail`. The same shape repeats at
`code_once.py:449` for the repair turn.

Cleanup failure arguably *should* dominate — but if so that is a decision worth making
explicitly, and the original failure should still reach the evidence trail rather than
being silently dropped.

---

## Low / robustness

### 16. `_execute` discards the detail of unexpected controller errors

`src/dialectic/service.py:263` · **Read**

```python
except Exception as exc:
    return self.fail_run(handle, "INTERNAL_ERROR",
                         f"unexpected controller error: {type(exc).__name__}")
```

Not leaking the message into the persisted record is deliberate, but nothing logs the
traceback either — `fail_run`'s `log_event` records only the same type name. An
`INTERNAL_ERROR` therefore arrives with no way to diagnose it after the fact.

**Direction.** `_LOGGER.exception(...)` into the private structured log (which is
already permission-hardened) before returning the bounded record.

### 17. Windows reserved-device names are rejected on POSIX too

`src/dialectic/ingress.py:193` · **Read**

`acquire_named_file` calls `_is_device_namespace` unconditionally, and that function
applies the Windows reserved-name set (`CON`, `PRN`, `AUX`, `NUL`, `COM1`–`COM9`,
`LPT1`–`LPT9`, `CLOCK$`) on every platform. On Linux a perfectly ordinary
`aux.md`, `con.md` or `prn.yaml` is rejected with "path uses a rejected device
namespace".

**Direction.** Gate the reserved-name half of the check on `os.name == "nt"`; keep the
`\\.\` / `\\?\` / `\??\` prefix check everywhere.

---

## Test and CI gaps

### 18. CI never installs the `desktop` extra, so `desktop_qt.py` is untested there

`.github/workflows/ci.yml:26` · **Verified**

The offline job installs `".[test]"` only. `tests/test_desktop_qt.py:11` opens with
`pytest.importorskip("PySide6")`, so all 315 lines of its tests skip silently on both
matrix legs — and the job still reports green.

`desktop_qt.py` is the largest module in the repository (1 711 lines) and the *default*
entry point: `Launch Dialectic UI.ps1:9-13` prefers `python -m dialectic.desktop`
whenever `import PySide6` succeeds. Regressions there — such as finding 9 — reach users
with a clean build.

**Direction.** Add a matrix leg (or a step) that installs `".[test,desktop]"` with
`QT_QPA_PLATFORM=offscreen`, which the test module already sets for itself.

### 19. CI runs the integration tests twice

`.github/workflows/ci.yml:28` · **Verified**

`[tool.pytest.ini_options] addopts = "-ra"` sets no default marker filter, so
`python -m pytest -q` (line 27) already collects and runs the `integration`-marked
tests; line 28 then re-runs them under `-m integration`. Beyond the wasted CI minutes,
this means the *offline* guarantee AGENTS.md describes ("must pass with no network and
no provider credentials") is never actually verified in isolation.

**Direction.** Make line 27 `-m "not integration"`, so the two steps mean what their
names say.

---

## Cleanup — altitude, reuse, simplification, efficiency

### 20. `codex_policy.py` is a dead, superseded duplicate of the live Codex profile

`src/dialectic/codex_policy.py:36` · **Verified** — altitude

No module under `src/` imports it; only `tests/test_code_once.py` does. The shipping
Codex permission profile is built by `native_adapters._fixture` (`native_adapters.py:1666`).

The two have already drifted: `build_codex_driver_construction` hardcodes profile name
`"dialectic-driver"` and `web_search: "disabled"`, while the live fixture supports
`dialectic-packet` / `dialectic-packet-web` and `web_search: "live"`.
`_reject_displacing_policy` here checks three keys; the live
`_reject_displacing_codex_policy` (`native_adapters.py:2349`) checks a dozen.

Because the tests pin the dead copy, the suite reports coverage for a construction the
product never executes, and future tightening of the real profile silently leaves this
one behind — the inverse of the intended safety property.

**Direction.** Delete the module and repoint its tests at `native_adapters._fixture`,
or make the adapter actually call it. Not both.

### 21. The event log is fully re-parsed twice per appended event

`src/dialectic/store.py:367` · **Read** — efficiency

`_next_event_sequence` reads and pydantic-validates *every* prior line of
`events.jsonl`. Each append pays for it twice: `DialecticService._persist_with_event`
calls `store.next_event_sequence(handle)`, then `append_event` recomputes the identical
value at `store.py:303`. Appending the Nth event validates `2(N-1)` `EventRecord`s.

Council runs make this worse — `_append_lease_event` adds three events per persistent
participant on top of every phase transition, so each state change re-validates the
whole log.

**Direction.** Cache the sequence on the handle, or have `append_event` return it and
drop the separate public call.

### 22. Turn waiting polls every 250 ms even when no idle watchdog applies

`src/dialectic/turn_timing.py:126` · **Read** — efficiency

```python
done, _ = await asyncio.wait({task}, timeout=min(0.25, remaining), ...)
```

For runtimes outside `_STREAMING_RUNTIMES` (`claude-code`), `idle_seconds` is `None`, so
no activity can ever shorten the deadline — yet the controller still wakes four times a
second, taking `self._lock` inside `_remaining` each time. A turn at the 3 600-second
ceiling is 14 400 iterations, multiplied by every concurrent reviewer or participant.

**Direction.** Await the remaining allotment directly when `idle_seconds is None`, and
re-arm only when `extend_active` fires.

### 23. `_run_reviewers` takes an unused parameter and re-parses its own packet

`src/dialectic/code_once.py:589` · **Read** — simplification

`_run_reviewers(self, context, reviewers, initial)` never reads `initial`. Meanwhile the
inner task does:

```python
packet = json.loads(reviewer.prompt)
core = packet["core"]
... context={"base_sha": core["base_sha"], "head_sha": core["review_sha"], ...}
```

— re-parsing the full review packet (task text plus the entire diff, up to
`max_packet_bytes`) from JSON purely to recover two SHA strings that are
`workspace.baseline.base_sha` and `initial.head_sha`. The dead parameter makes the data
flow actively misleading, and the extra `json.JSONDecodeError` / `KeyError` / `TypeError`
handlers exist only to guard a round-trip the controller itself produced.

Separately, `ReviewReport.model_json_schema()` is recomputed per reviewer inside `one()`
although `_prepare_reviewers` already computed it as `review_schema`.

### 24. `ui._response_excerpts` duplicates `desktop.load_desktop_responses`

`src/dialectic/ui.py:817` · **Read** — reuse

Same `turns/*/*/*.attempt.json` glob, same size guard, same `response` →
`bounded_diagnostic` fallback, same field extraction, same sort key — in a module that
already imports `attempt_duration_seconds` and `load_desktop_web_sources` from
`desktop.py`. Any change to how a turn attempt is projected must be made twice or the
web UI and the Qt desktop will disagree about the same run.

It also sits on the `/api/status` polling path, so every poll re-globs, re-reads and
re-parses every attempt file. One shared parser would give one place to add caching.

### 25. `_root_command` and `_WindowsPipeReader` are duplicated verbatim

`src/dialectic/acp_transport.py:637,646` and `src/dialectic/native_process.py:284,336` · **Read** — reuse

Both the class and the function are byte-identical copies. `native_process` already
imports six names from `process.py`; these belong beside them.

### 26. Redundant hashing and re-sorting in `build_capability_binding`

`src/dialectic/capabilities.py:129` · **Read** — simplification

```python
if hashlib.sha256(attestation_bytes).hexdigest() != hashlib.sha256(
    canonical_json_bytes(attestation)
).hexdigest():
```

Hashing both sides to compare them is a byte comparison written the long way, and the
left-hand digest is already available as `attestation_sha256` from line 124. Separately,
`identities.sort(...)` at line 146 repeats a sort `_capture_dynamic_identities` already
performed at line 203 with the identical key.

### 27. Unreachable branch in `ReviewerSpec.target_is_exclusive`

`src/dialectic/schemas.py:112` · **Read** — dead code

```python
if self.target == "@driver": ...
elif self.runtime is None or self.model is None: raise ...
elif self.target is not None: raise ...      # unreachable
```

`target` is typed `Literal["@driver"] | None`, so reaching the third branch requires
`target` to be `None` — making its condition constantly false.

### 28. `extend_turn_deadlines` has a parameter that must equal its default

`src/dialectic/service.py:110` · **Read** — simplification

```python
def extend_turn_deadlines(self, run_id, seconds=TURN_EXTENSION_SECONDS):
    if seconds != TURN_EXTENSION_SECONDS:
        raise ValueError("turn deadlines may be extended only in five-minute increments")
```

The parameter can only ever hold one value; both call sites (`ui.py:341`,
`desktop_qt.py:146`) pass the constant or omit it. Dropping it removes an argument, a
guard, and a `ValueError` path.

---

## Themes

Three patterns account for most of the correctness findings and are worth addressing at
the pattern level rather than one site at a time:

1. **Duplicated policy tables drift.** Findings 6/7 (turn-phase mapping), 20
   (Codex profile), 24 and 25 (copied helpers) are all the same failure: a second copy
   made for local convenience that then diverges. The repo's own discipline —
   `AGENTS.md` exists precisely so rules "must not drift between copies" — argues for
   one source per table.

2. **Boundary conditions treated as violations.** Findings 3 (empty credential) and 4
   (deep nesting) both turn an ordinary input into a hard failure or a crash, because
   the absent case and the malformed case were not distinguished. Fail-closed is the
   right default; it should still classify correctly.

3. **Silently ignored failure signals.** Findings 8 (discarded `BOOL`), 15 (`finally`
   masking), and 16 (dropped traceback) all discard information at exactly the moment
   something went wrong. The evidence architecture elsewhere in this codebase is
   unusually rigorous; these three are out of character with it.

## Verification appendix

Commands run this session that produced the evidence quoted above:

- `pytest -q` → 45 passed, 170 errors (environmental, see *Suite status*).
- `pytest -q --basetemp=<clean>` → 211 passed, 13 skipped.
- `TurnWorkspace.create` + `output-schema.json` + `verify_and_cleanup` → finding 1.
- `dir(dialectic.council_once)` → finding 2.
- `native_credentials` with absent / empty / short `OPENAI_API_KEY` → finding 3.
- `ConfigLoader.load` on a 12 KB deeply nested document → finding 4.
- `git rev-parse --show-toplevel` with and without `GIT_DIR` → finding 5.
- `AgentRequest(turn_phase=_probe_turn_phase("moderator"))` → finding 6.
- `grep -rn "codex_policy" src/ tests/` → finding 20.
