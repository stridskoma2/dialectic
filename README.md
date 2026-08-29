# Dialectic

Dialectic is a bounded local supervisor for cross-model code review and council
deliberation. This checkout implements the frozen MVP's **Slices 0 through 2**:
the CLI/application boundary, strict contracts, durable run state, redaction,
cross-platform supervision, the offline Code Once workflow, and versioned native
Codex, Claude Code, and Grok Build adapters.

Code Once gives reviewers an immutable, diff-only packet and no controller-added
repository or worktree path. That boundary does not rewrite user-authored task
text or driver-authored product content, so a path already present in the bounded
task or diff remains visible to reviewers.

Requires Python 3.12 or newer.

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e ".[test]"
.\.venv\Scripts\python.exe -m pytest -q
```

Both installed entry points invoke the same application:

```text
dial --help
dialectic --help
```

Run artifacts are private, sensitive, and retained beneath the platform user
state directory in `dialectic/runs/<run-id>/`.

The current native fixtures recognize Codex CLI `0.150.0-alpha.12.2` and
`0.151.0-alpha.7.1`, Claude Code `2.1.177`, and Grok Build `0.1.220`. Other
versions fail preflight until their behavior is independently qualified. Native
roles also require the corresponding installed CLI and authentication.

Native release checks are opt-in and cost-bearing:

```powershell
$env:DIALECTIC_LIVE = "1"
$env:DIALECTIC_CODEX_MODEL = "<pinned-model>"
$env:DIALECTIC_CLAUDE_MODEL = "<external-reviewer-model>" # or DIALECTIC_GROK_MODEL
$env:OPENAI_API_KEY = "<trusted-cli-only credential>"
.\.venv\Scripts\python.exe -m pytest -q -m live
```

They must be run separately on Windows and Linux for each release fixture. Fake
or recorded transports prove adapter construction only; they do not establish
native credential isolation or permission enforcement. Packet-only prompts omit
controller-added repository paths, but Dialectic does not rewrite paths already
present in user task text or driver-authored product content.
