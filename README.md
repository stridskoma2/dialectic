# Dialectic

Dialectic is a bounded local supervisor for cross-model code review and council
deliberation. This checkout implements the frozen MVP's **Slices 0 and 1**: the
CLI/application boundary, strict contracts, durable run state, redaction,
cross-platform supervision primitives, and the offline Code Once workflow.
Native CLI enforcement and live-provider runs arrive in Slice 2.

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
