from __future__ import annotations

import os
from pathlib import Path

import pytest

from dialectic import history as history_module
from dialectic.service import DialecticService
from dialectic.store import RunStore


def saved_run(tmp_path: Path, *, suffix: str = "aaaaaaaaaa", mode: str = "council", terminal: bool = True):
    run_id = f"20260905T010101Z-{suffix}"
    service = DialecticService(RunStore(tmp_path / "state", run_id_factory=lambda: run_id))
    handle = service.create_run(mode)
    service.store.write_artifact(handle, f"input/{'prompt' if mode == 'council' else 'task'}.md", b"# A new harness repository\n\nWhat should it include?\n")
    if terminal:
        service.fail_run(handle, "PREFLIGHT_FAILED", "expected fixture failure", phase="PREFLIGHT")
    return service, handle


def fingerprint(root: Path):
    return {
        path.relative_to(root).as_posix(): (path.read_bytes(), path.stat().st_mtime_ns)
        for path in root.rglob("*") if path.is_file()
    }


def test_history_search_load_and_audit_are_read_only(tmp_path: Path) -> None:
    service, handle = saved_run(tmp_path)
    _, partial = saved_run(tmp_path, suffix="bbbbbbbbbb", mode="code", terminal=False)
    before = fingerprint(service.store.state_root)
    reader = DialecticService.open_history(service.store.state_root)
    listing = reader.list_runs("HARNESS")
    assert [entry.run_id for entry in listing.entries] == [partial.run_id, handle.run_id]
    assert not listing.limited
    assert reader.list_runs("failed").entries[0].run_id == handle.run_id
    assert reader.list_runs("code").entries[0].run_id == partial.run_id
    assert not reader.list_runs("not present").entries
    snapshot = reader.load_run(handle.run_id)
    assert "harness repository" in snapshot.entry.prompt
    assert snapshot.entry.record.status == "FAILED"
    assert "summary.md" in snapshot.contents
    assert not snapshot.warnings
    assert reader.audit_run(handle.run_id) == service.audit_run(handle.run_id)
    assert reader.audit_run(handle.run_id).valid
    partial_snapshot = reader.load_run(partial.run_id)
    assert partial_snapshot.entry.record.status == "CREATED"
    assert "summary.md" not in partial_snapshot.contents
    assert not reader.audit_run(partial.run_id).complete
    assert fingerprint(service.store.state_root) == before


def test_history_does_not_bootstrap_state_or_construct_providers(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def forbidden(*args, **kwargs):
        pytest.fail("History must not construct a writable RunStore")

    monkeypatch.setattr(RunStore, "__init__", forbidden)
    absent = tmp_path / "missing"
    reader = DialecticService.open_history(absent)
    assert reader.list_runs().entries == ()
    assert not absent.exists()


def test_history_keeps_corrupt_sessions_visible_and_reports_preview_limits(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    service, handle = saved_run(tmp_path)
    (handle.path / "run.json").write_bytes(b"not json")
    reader = DialecticService.open_history(service.store.state_root)
    entry = reader.list_runs("harness").entries[0]
    assert entry.record is None and entry.warnings
    assert reader.load_run(handle.run_id).contents["summary.md"]
    assert not reader.audit_run(handle.run_id).valid
    monkeypatch.setattr(history_module, "MAX_HISTORY_FILE_BYTES", 20)
    snapshot = reader.load_run(handle.run_id)
    assert not snapshot.contents
    assert any("unavailable" in warning for warning in snapshot.warnings)


def test_history_rejects_traversal_hardlinks_and_marks_listing_limits(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    service, handle = saved_run(tmp_path)
    saved_run(tmp_path, suffix="bbbbbbbbbb")
    reader = DialecticService.open_history(service.store.state_root)
    with pytest.raises(ValueError):
        reader.load_run("../outside")
    for name in ("../outside", "/outside", "C:/outside", "input\\prompt.md", "run.json:stream"):
        with pytest.raises(ValueError):
            reader.read_artifact(handle.run_id, name)
    outside = tmp_path / "outside.txt"
    outside.write_text("external content", encoding="utf-8")
    os.link(outside, handle.path / "linked.txt")
    with pytest.raises(ValueError, match="non-linked"):
        reader.read_artifact(handle.run_id, "linked.txt")
    snapshot = reader.load_run(handle.run_id)
    assert "linked.txt" not in dict(snapshot.artifacts)
    assert any("linked.txt" in warning for warning in snapshot.warnings)
    monkeypatch.setattr(history_module, "MAX_HISTORY_RESULTS", 1)
    assert reader.list_runs().limited
    assert reader.list_runs(handle.run_id).entries[0].run_id == handle.run_id


@pytest.mark.skipif(os.name == "nt", reason="POSIX symlink/FIFO variant")
def test_history_rejects_linked_directories_and_special_files(tmp_path: Path) -> None:
    service, handle = saved_run(tmp_path)
    reader = DialecticService.open_history(service.store.state_root)
    external = tmp_path / "external"
    external.mkdir()
    (external / "private.txt").write_text("private")
    (handle.path / "linked").symlink_to(external, target_is_directory=True)
    os.mkfifo(handle.path / "pipe")
    for name in ("linked/private.txt", "pipe"):
        with pytest.raises(ValueError):
            reader.read_artifact(handle.run_id, name)
    snapshot = reader.load_run(handle.run_id)
    assert not any(name.startswith("linked/") or name == "pipe" for name, _ in snapshot.artifacts)
