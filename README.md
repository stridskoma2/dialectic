# Dialectic

Dialectic is a bounded local supervisor for cross-model code review and council
deliberation. This checkout currently implements the frozen MVP's **Slice 0**:
the CLI/application boundary, strict contracts, durable run state, redaction,
and cross-platform supervision primitives. Native-agent workflows arrive in the
later specification slices and deliberately are not started by this slice.

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
