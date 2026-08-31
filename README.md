# Dialectic 0.1.0

Dialectic is a bounded local supervisor for two one-shot workflows:

- **Code Once:** one Codex driver implementation, one concurrent immutable-diff
  review, at most one repair turn, then stop without re-review.
- **Council Once:** blind openings, one anonymized cross-examination, a fresh
  moderator, complete ballots, controller-derived consensus, then stop.

Dialectic is alpha software for trusted local use. It launches installed native
AI CLIs with narrow, version-qualified policies, but it does not claim to confine
a compromised CLI executable or provider implementation. On POSIX it owns the
spawned session/process group; a malicious child that deliberately escapes with
`setsid()` is outside that guarantee. Windows uses a kill-on-close Job Object.

## Install

Python 3.12 or newer and Git are required. From a source checkout:

```bash
python -m venv .venv
# Linux
.venv/bin/python -m pip install -e ".[test]"
# Windows PowerShell
.venv\Scripts\python.exe -m pip install -e ".[test]"
```

Both installed command-line entry points invoke the same application:

```text
dial --help
dialectic --help
```

For the fast local interface, double-click `Launch Dialectic UI.cmd` on Windows
or run `dialectic-ui` on either release platform. The Windows launcher prefers the
checkout's Windows `.venv`; when that is absent, it falls back to Dialectic installed
in Ubuntu WSL at `~/.local/share/dialectic/venv`. It opens
the localhost-only interface in a dedicated Edge or Chrome app window without tabs
or an address bar, falling back to the default browser only when neither is
available. The WSL path uses an authenticated, per-launch file bridge under `.git/`
for the native Windows repository picker and removes that control directory on exit.
The interface calls
`DialecticService` directly; it does not shell out to the command-line interface.
It provides Code/Council mode selection, an implementation or deliberation prompt,
a repository browser for Code Once, friendly model dropdowns with installed-CLI
status, effort selection, one to five reviewers or two to five council participants,
reviewer focus, consensus tolerance, live phase/status, and direct access to final
summaries, artifacts, application logs, and isolated worktrees. The catalog includes
locally configured selectors, while authentication and account-specific model access
remain verified by native preflight. Council Once remains prompt-only, so a repository
selected for Code Once is visibly disabled and is not disclosed to council participants.

For a release build, install `.[release]` and run `python -m build`. The package
version, controller version, and artifact version are pinned to `0.1.0`, `0.1.0`,
and `1` respectively.

## Native prerequisites

Install and authenticate every CLI named by the selected workflow. The v0.1.0
fixtures recognize only these version-eligible builds; Gate A still runs the
current host's native capability probe before any model turn:

| Runtime | Executable | Accepted version |
|---|---|---|
| Codex on Linux/WSL | `codex` | `0.150.0-alpha.12.2`, `0.151.0-alpha.7.1` |
| Codex driver on native Windows | `codex` | `0.150.0-alpha.12.2` |
| Codex packet roles on native Windows | `codex` | `0.150.0-alpha.12.2`, `0.151.0-alpha.7.1` |
| Claude Code | `claude` | `2.1.177` |
| Grok Build | `grok` | `0.1.220` |

Authentication may come from the native CLI's saved authentication or its
fixture-declared credential environment (`OPENAI_API_KEY`, `ANTHROPIC_API_KEY`,
or `XAI_API_KEY`). Credentials are not accepted in Dialectic YAML. Unsupported
versions and unverified permission shapes fail closed during preflight.

Codex CLI `0.151.0` is detected but deliberately rejected. Native-Windows live
qualification failed both profiles: its elevated runner did not preserve the
isolated-worktree CWD or the driver `control/` plus writable `tmp/` split, while its
packet-only path either rejected the split read policy or failed the private neutral
CWD/read probe. A separate WSL2 live driver probe failed because Bubblewrap could
not construct narrower allowed worktree and Git mounts beneath denied ancestors,
repository `AGENTS.md` discovery was not preserved, and the available tool surface
exceeded the qualified fixture. A stable version label does not override either
permission matrix.
The version allowlist is only Gate A eligibility: every listed build must still pass
the current host's native capability probe before a model turn is allowed.
On native Windows, Dialectic explicitly selects Codex's `elevated` sandbox backend
for both effective-policy inspection and every isolated turn; this does not rely on
the user's ignored `config.toml`. Private packet-only role directories live under
protected per-run directories created directly beneath `%PUBLIC%`, outside Windows'
virtualized per-user `AppData\Local` tree. Their public parent is only the traversal
root; each role directory receives Dialectic's private user-and-system DACL. The
trusted CLI can consume its controller-owned schema there, while model-generated
Windows child commands can enter the CWD but cannot read or write its files. Run
artifacts and structured logs remain in their documented platform state/log locations.
Native-Windows live v3 evidence qualifies `0.151.0-alpha.7.1` for packet-only
Council/reviewer/moderator roles. Its driver-write matrix still denies required
`tmp/` writes and read-only Git inspection, so Code mode requires the separately
qualified `0.150.0-alpha.12.2`; Dialectic rejects the newer alpha as a driver before
incurring a model turn.

## Configure and run

Copy [examples/dialectic.yaml](examples/dialectic.yaml), set the referenced model
environment variables, and remove unused targets if necessary. A present unused
mode section must still be valid, but it is not resolved or launched.

```bash
# One coding/review/repair pass
dial code --config dialectic.yaml --repo /path/to/repository --task-file task.md

# One bounded council pass
dial council --config dialectic.yaml --prompt-file prompt.md

# Inspect any run
dial status <run-id>
```

Example inputs are in [examples/task.md](examples/task.md) and
[examples/prompt.md](examples/prompt.md). Configuration, task, and prompt files
must be scalar-value UTF-8 without a BOM. Limits are controller-enforced; the
example uses the normative v0.1 defaults. Commands print concise persisted
phase/status transitions and a final summary, not raw model event streams.

## Runs, artifacts, and cleanup

Every run is durably retained under the platform user-state directory:

- Windows: `%LOCALAPPDATA%\dialectic\runs\<run-id>\`
- Linux: `${XDG_STATE_HOME:-~/.local/state}/dialectic/runs/<run-id>/`

`dial status <run-id>` prints the canonical absolute artifact directory. Artifacts
include `run.json`, `events.jsonl`, redacted inputs, bounded turn evidence, and
`summary.json`/`summary.md`. Code runs also record `git/workspace.json` and bounded
diff/review evidence. Failed and cancelled runs are deliberately retained and are
sensitive. Remove a terminal run directory manually only after confirming that no
Dialectic process owns it; the MVP has no run-cleanup command.

The CLI and UI also write private, rotating JSONL application logs under
`%LOCALAPPDATA%\dialectic\logs\` on Windows or the corresponding
`${XDG_STATE_HOME:-~/.local/state}/dialectic/logs/` directory on Linux. Each frontend
process gets its own timestamped file, capped at 5 MiB with three backups. These logs
record application lifecycle, run identifiers and state transitions, and bounded
controller diagnostics; they do not record prompts, configuration bodies, model output,
credentials, or HTTP session tokens. The CLI prints its log path, and the UI exposes it
through **App log**. Treat application logs as sensitive operational evidence.

Code Once leaves its isolated worktree and `dialectic/<run-id>` branch in place.
The original checked-out files, index, checked-out branch, `HEAD`, pre-existing
branches, and `main` remain unchanged. Creating the linked worktree intentionally
adds shared Git worktree metadata, a Dialectic branch, commits, and objects. The
terminal prints the recorded path and branch. After inspection, run these commands
from the original repository, substituting the recorded values:

```bash
git worktree remove <recorded-isolated-worktree-path>
git branch -D dialectic/<run-id>
git worktree prune
```

Cleanup is never automatic.

## Security, cost, and operational boundaries

- Model output is data and is never executed by the controller as a shell command.
  Native commands use executable/argument arrays; prompts travel over stdin or ACP.
- Codex is the only writable driver. Reviewers, council participants, and the
  moderator receive packet-only neutral directories.
- The controller does not inject a repository/worktree path into packet-only
  prompts, argv, environment overrides, or packet artifacts. A bounded user task
  or driver-authored product diff may itself contain such a path; Dialectic neither
  discovers nor rewrites paths already authored in that content.
- A fresh linked worktree does not contain ignored local artifacts such as `.venv`,
  `node_modules`, build caches, generated output, or `.env`. Driver prompts say not
  to repair the environment or create build output. Target-project test/build gates
  are not a deterministic v0.1 controller feature.
- Native calls may consume paid quota. Dialectic has no cost estimator or retry,
  fallback, or format-repair loop. A required target failure stops the run while
  retaining partial evidence, so provider charges can occur without finalization.
- Keep task, prompt, repository, and native configuration content trusted. Known
  injected credential values are redacted, but Dialectic does not discover arbitrary
  secrets already present in user or repository content.

## Verification and release evidence

The mandatory suite is offline and uses no provider credentials:

```bash
pytest -q
pytest -q -m integration
```

CI runs both commands on Windows and Linux. Native release evidence is manual,
cost-bearing, and must be invoked separately on each applicable platform:

```bash
export DIALECTIC_LIVE=1
export DIALECTIC_CODEX_MODEL='<pinned-model>'
export DIALECTIC_CLAUDE_MODEL='<pinned-model>'
export DIALECTIC_GROK_MODEL='<pinned-model>'
pytest -q -m live
```

PowerShell uses `$env:NAME = 'value'`. The Code smoke requires Codex plus an
external reviewer. The Council smoke uses two or three available configured CLIs
and does not require agreement. Recorded/fake fixtures prove construction only;
they are not native permission or credential-isolation evidence.

## MVP limitations

There are no continuous loops, second review, extra council round, provider retry,
API transport, MCP server, daemon, background ownership, or crash resumption. Code
Once does not support submodules, Git LFS, sparse checkout, active clean/smudge
filters, binary changes, invalid-UTF-8 diffs, symlink additions, or multiply linked
changed files. macOS may work but is not a v0.1.0 release platform.
