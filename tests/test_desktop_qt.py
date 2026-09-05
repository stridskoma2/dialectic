from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
pytest.importorskip("PySide6")

from PySide6.QtCore import QMimeData, QPoint, QPointF, QSettings, Qt, QUrl
from PySide6.QtGui import QDragEnterEvent, QDragMoveEvent, QDropEvent
from PySide6.QtWidgets import QApplication, QFileDialog, QMessageBox

from dialectic.config import ConfigLoader
from dialectic.desktop_qt import MainWindow, RunWorker
from dialectic.service import DialecticService
from dialectic.store import RunStore
from dialectic.ui import _model_options, _prepare_run
from dialectic.ui_config import DEFAULT_LIMITS


@pytest.fixture(scope="module")
def qt_app() -> QApplication:
    return QApplication.instance() or QApplication([])


def _options() -> dict[str, object]:
    return {
        "models": {
            "codex": [
                {
                    "id": "codex-model",
                    "name": "Codex Model",
                    "description": "Test Codex model",
                    "source": "catalog",
                }
            ],
            "claude-code": [
                {
                    "id": "claude-model",
                    "name": "Claude Model",
                    "description": "Test Claude model",
                    "source": "catalog",
                }
            ],
            "grok-build": [
                {
                    "id": "grok-model",
                    "name": "Grok Model",
                    "description": "Test Grok model",
                    "source": "catalog",
                }
            ],
        },
        "runtimes": {
            "codex": {"installed": True, "executable": "codex"},
            "claude-code": {"installed": True, "executable": "claude"},
            "grok-build": {"installed": False, "executable": "grok"},
        },
        "browseSupported": True,
    }


def _window(tmp_path: Path) -> MainWindow:
    settings = QSettings(str(tmp_path / "desktop.ini"), QSettings.Format.IniFormat)
    return MainWindow(options=_options(), settings=settings)


@pytest.mark.parametrize("mode", ["code", "council"])
@pytest.mark.parametrize("suffix", [".md", ".markdown", ".TXT"])
def test_native_desktop_loads_prompt_file_as_undoable_edit(
    qt_app: QApplication, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    mode: str, suffix: str,
) -> None:
    plan = "# Implementation plan\n\n- Preserve café and 中文\n- Run tests\n"
    path = tmp_path / ("implementation plan" + suffix)
    original = b"\xef\xbb\xbf" + plan.replace("\n", "\r\n").encode("utf-8")
    path.write_bytes(original)
    selected: list[bool] = []

    def choose_file(*_args: object) -> tuple[str, str]:
        selected.append(True)
        return str(path), ""

    monkeypatch.setattr(QFileDialog, "getOpenFileName", choose_file)
    window = _window(tmp_path)
    try:
        window._switch_mode(mode)
        window.repository.setText(str(tmp_path))
        window.prompt_edit.setPlainText("Existing draft")
        window.prompt_tabs.setCurrentWidget(window.prompt_preview)
        window.load_prompt_button.click()

        assert selected == [True]
        assert window.prompt_edit.toPlainText() == plan
        assert window.prompt_tabs.currentWidget() is window.prompt_edit
        assert window.prompt_counter.text() == f"{len(plan):,} characters"
        assert _prepare_run(window._payload()).prompt_bytes == plan.strip().encode("utf-8")
        assert window._worker is None
        window.prompt_tabs.setCurrentWidget(window.prompt_preview)
        assert "Implementation plan" in window.prompt_preview.toPlainText()
        window._switch_mode("council" if mode == "code" else "code")
        window._switch_mode(mode)
        assert window.prompt_edit.toPlainText() == plan
        # Mode restoration uses setPlainText, so verify import undo after reloading.
        window.prompt_edit.setPlainText("Existing draft")
        window.load_prompt_button.click()
        window.prompt_edit.undo()
        assert window.prompt_edit.toPlainText() == "Existing draft"
        window.prompt_edit.redo()
        assert window.prompt_edit.toPlainText() == plan
        assert path.read_bytes() == original
    finally:
        window.close()


@pytest.mark.parametrize(
    "kind, message",
    [
        ("missing", "unable to acquire"),
        ("directory", "regular"),
        ("oversize", "ceiling"),
        ("encoding", "UTF-8"),
        ("binary", "binary"),
        ("empty", "empty"),
        ("extension", ".txt"),
    ],
)
def test_native_desktop_failed_prompt_import_preserves_draft(
    qt_app: QApplication, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    kind: str, message: str,
) -> None:
    path = tmp_path / ("plan.pdf" if kind == "extension" else "plan.md")
    if kind == "directory":
        path.mkdir()
    elif kind != "missing":
        path.write_bytes({
            "oversize": b"a" * (DEFAULT_LIMITS["max_input_bytes"] + 1),
            "encoding": b"\xffprivate file contents",
            "binary": b"private\x00file contents",
            "empty": b"\xef\xbb\xbf \r\n\t",
            "extension": b"private file contents",
        }[kind])
    errors: list[str] = []
    monkeypatch.setattr(QMessageBox, "warning", lambda _parent, _title, text: errors.append(text))
    window = _window(tmp_path)
    try:
        window.prompt_edit.setPlainText("Existing draft")
        window.prompt_tabs.setCurrentWidget(window.prompt_preview)
        window._load_prompt_file(str(path))
        assert len(errors) == 1 and message in errors[0]
        assert "private" not in errors[0]
        assert window.prompt_edit.toPlainText() == "Existing draft"
        assert not window.prompt_edit.document().isUndoAvailable()
        assert window.prompt_tabs.currentWidget() is window.prompt_preview
    finally:
        window.close()


def _drop_prompt_data(window: MainWindow, mime: QMimeData) -> tuple[bool, QDropEvent]:
    actions = Qt.DropAction.CopyAction | Qt.DropAction.MoveAction
    buttons = Qt.MouseButton.LeftButton
    modifiers = Qt.KeyboardModifier.NoModifier
    enter = QDragEnterEvent(QPoint(12, 12), actions, mime, buttons, modifiers)
    move = QDragMoveEvent(QPoint(12, 12), actions, mime, buttons, modifiers)
    drop = QDropEvent(QPointF(12, 12), actions, mime, buttons, modifiers)
    for event in (enter, move, drop):
        QApplication.sendEvent(window.prompt_edit.viewport(), event)
    return enter.isAccepted() and move.isAccepted(), drop


def test_native_desktop_drops_file_contents_and_preserves_text_dragging(
    qt_app: QApplication, tmp_path: Path,
) -> None:
    path = tmp_path / "计划 with spaces.md"
    path.write_text("# Dropped plan\n\nRun the tests.", encoding="utf-8")
    window = _window(tmp_path)
    try:
        window.show()
        qt_app.processEvents()
        window.prompt_edit.setPlainText("Existing draft")
        mime = QMimeData()
        mime.setUrls([QUrl.fromLocalFile(str(path))])
        mime.setText(str(path))  # File drags can also advertise a text path.
        accepted, drop = _drop_prompt_data(window, mime)
        assert accepted and drop.isAccepted()
        assert drop.dropAction() == Qt.DropAction.CopyAction
        assert window.prompt_edit.toPlainText() == path.read_text(encoding="utf-8")
        window.prompt_edit.undo()
        assert window.prompt_edit.toPlainText() == "Existing draft"

        text_mime = QMimeData()
        text_mime.setText("Ordinary dragged text")
        accepted, drop = _drop_prompt_data(window, text_mime)
        assert accepted and drop.isAccepted()
        assert "Ordinary dragged text" in window.prompt_edit.toPlainText()
        window.prompt_edit.insertFromMimeData(text_mime)
        assert window.prompt_edit.toPlainText().count("Ordinary dragged text") == 2
    finally:
        window.close()


def test_native_desktop_rejects_unsupported_file_drops_and_locks_import_during_run(
    qt_app: QApplication, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "plan.txt"
    path.write_text("a" * DEFAULT_LIMITS["max_input_bytes"], encoding="utf-8")
    window = _window(tmp_path)
    try:
        window.show()
        qt_app.processEvents()
        window._load_prompt_file(str(path))
        assert len(window.prompt_edit.toPlainText()) == DEFAULT_LIMITS["max_input_bytes"]
        window.prompt_edit.setPlainText("Existing draft")
        for urls in (
            [QUrl("https://example.com/plan.md")],
            [QUrl.fromLocalFile(str(tmp_path / "plan.pdf"))],
            [QUrl.fromLocalFile(str(path)), QUrl.fromLocalFile(str(path))],
        ):
            mime = QMimeData()
            mime.setUrls(urls)
            mime.setText("Do not paste this path")
            accepted, drop = _drop_prompt_data(window, mime)
            assert not accepted and not drop.isAccepted()
            assert window.prompt_edit.toPlainText() == "Existing draft"

        monkeypatch.setattr(QFileDialog, "getOpenFileName", lambda *_args: ("", ""))
        window.load_prompt_button.click()
        assert window.prompt_edit.toPlainText() == "Existing draft"
        window._set_running(True)
        assert not window.load_prompt_button.isEnabled()
        window._load_prompt_file(str(path))
        mime.setUrls([QUrl.fromLocalFile(str(path))])
        accepted, drop = _drop_prompt_data(window, mime)
        assert not accepted and not drop.isAccepted()
        assert window.prompt_edit.toPlainText() == "Existing draft"
        window._set_running(False)
        assert window.load_prompt_button.isEnabled()
    finally:
        window._set_running(False)
        window.close()


def test_native_desktop_switches_modes_and_previews_markdown(
    qt_app: QApplication, tmp_path: Path
) -> None:
    window = _window(tmp_path)
    try:
        assert window.code_mode.isChecked()
        assert len(window._agent_rows) == 2
        window.prompt_edit.setPlainText("# Native prompt\n\n- one\n- two")
        window.prompt_tabs.setCurrentWidget(window.prompt_preview)
        qt_app.processEvents()
        assert "Native prompt" in window.prompt_preview.toPlainText()
        assert "one" in window.prompt_preview.toPlainText()

        window._switch_mode("council")
        assert window.council_mode.isChecked()
        assert window.repository.isEnabled() is False
        assert window.council_settings.isVisible() is False  # Window is not shown.
        assert window.council_settings.isHidden() is False
        assert len(window._agent_rows) == 2
        assert all(row.runtime.findData("@driver") == -1 for row in window._agent_rows)
        assert window.research_mode.currentData() == "live-web"
        assert "provider-native web" in window.research_hint.text()
        window._set_running(True)
        assert not window.run_button.isEnabled()
        window._set_running(False)
        assert window.run_button.isEnabled()

        class _TimingWorker:
            @staticmethod
            def deadline_snapshot() -> dict[str, object]:
                return {
                    "active": True,
                    "turnCount": 1,
                    "canExtend": True,
                    "turns": [
                        {
                            "targetId": "participant-a",
                            "phase": "opening",
                            "remainingSeconds": 1_797,
                            "idleRemainingSeconds": 87,
                        }
                    ],
                }

        window._worker = _TimingWorker()  # type: ignore[assignment]
        window._refresh_turn_timing()
        assert "29m 57s allotted" in window.deadline_label.text()
        assert "1m 27s silence watchdog" in window.deadline_label.text()
        assert window.extend_button.isEnabled()
        window._worker = None
    finally:
        window.close()


def test_native_desktop_builds_the_same_validated_code_request(
    qt_app: QApplication, tmp_path: Path
) -> None:
    repository = tmp_path / "repo"
    repository.mkdir()
    settings = QSettings(str(tmp_path / "desktop.ini"), QSettings.Format.IniFormat)
    window = MainWindow(options=_model_options(), settings=settings)
    try:
        window.repository.setText(str(repository))
        window.prompt_edit.setPlainText("Implement the focused change.")
        model_index = window.main_model.findData("gpt-6-astra")
        assert model_index >= 0
        assert window.main_model.itemText(model_index) == "GPT-6 Astra"
        window.main_model.setCurrentIndex(model_index)

        run = _prepare_run(window._payload())
        config = ConfigLoader({}).load(run.config_bytes, mode="code").config
        assert run.repository == repository.resolve()
        assert config.driver is not None
        assert config.driver.model == "gpt-6-astra"
        assert config.reviewers is not None
        assert len(config.reviewers) == 2
        assert config.research_mode == "offline"
    finally:
        window.close()


def test_native_worker_executes_through_dialectic_service(
    qt_app: QApplication, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repository = tmp_path / "repo"
    repository.mkdir()
    prepared = _prepare_run(
        {
            "mode": "code",
            "prompt": "Implement the focused change.",
            "repository": str(repository),
            "main": {"runtime": "codex", "model": "codex-model", "effort": ""},
            "agents": [
                {
                    "runtime": "@driver",
                    "model": "",
                    "effort": "",
                    "lens": "general-correctness",
                }
            ],
            "maxDissenters": 0,
        }
    )

    async def executor(context: object):
        service = context.service
        return service.finalize_code(context.handle, "COMPLETED_NO_FINDINGS")

    service = DialecticService(
        RunStore(tmp_path / "state"),
        code_executor=executor,
    )
    monkeypatch.setattr("dialectic.desktop_qt.build_service", lambda: service)
    results: list[object] = []
    errors: list[str] = []
    worker = RunWorker(prepared)
    worker.result_ready.connect(results.append)
    worker.run_error.connect(errors.append)

    worker.run()

    assert errors == []
    assert len(results) == 1
    result = results[0]
    assert result.record.status == "FINALIZED"
    assert result.summary_path.is_file()


def test_native_worker_persists_immediate_cancellation(
    qt_app: QApplication, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repository = tmp_path / "repo"
    repository.mkdir()
    prepared = _prepare_run(
        {
            "mode": "code",
            "prompt": "Implement the focused change.",
            "repository": str(repository),
            "main": {"runtime": "codex", "model": "codex-model", "effort": ""},
            "agents": [
                {
                    "runtime": "@driver",
                    "model": "",
                    "effort": "",
                    "lens": "general-correctness",
                }
            ],
            "maxDissenters": 0,
        }
    )

    async def executor(_context: object):
        await asyncio.Event().wait()

    service = DialecticService(
        RunStore(tmp_path / "state"),
        code_executor=executor,
    )
    monkeypatch.setattr("dialectic.desktop_qt.build_service", lambda: service)
    results: list[object] = []
    errors: list[str] = []
    worker = RunWorker(prepared)
    worker.result_ready.connect(results.append)
    worker.run_error.connect(errors.append)
    worker.request_cancel()

    worker.run()

    assert errors == []
    assert len(results) == 1
    assert results[0].record.status == "CANCELLED"
    assert results[0].summary_path.is_file()

    class ClosedLoop:
        def call_soon_threadsafe(self, _callback: object) -> None:
            raise RuntimeError("Event loop is closed")

    class PendingTask:
        @staticmethod
        def done() -> bool:
            return False

        @staticmethod
        def cancel() -> None:
            return None

    worker._loop = ClosedLoop()  # type: ignore[assignment]
    worker._task = PendingTask()  # type: ignore[assignment]
    worker.request_cancel()


def test_native_desktop_renders_complete_model_response(
    qt_app: QApplication, tmp_path: Path
) -> None:
    artifact_dir = tmp_path / "run"
    attempt = artifact_dir / "turns/reviewer/reviewer-a/review.attempt.json"
    attempt.parent.mkdir(parents=True)
    attempt.write_text(
        json.dumps(
            {
                "role": "reviewer",
                "target_id": "reviewer-a",
                "turn_phase": "review",
                "started_at": "2026-09-01T01:01:00Z",
                "response_completed_at": "2026-09-01T01:02:03Z",
                "response": {
                    "runtime": "claude-code",
                    "requested_model": "claude-model",
                    "text": "# Review\n\nThe complete response is visible.",
                },
            }
        ),
        encoding="utf-8",
    )
    window = _window(tmp_path)
    try:
        window._artifact_dir = artifact_dir
        window._refresh_responses()
        qt_app.processEvents()
        assert window.response_list.count() == 1
        assert "Reviewer A" in window.response_list.item(0).text()
        assert "1m 03s" in window.response_list.item(0).text()
        assert "1m 03s" in window.response_heading.text()
        assert "complete response" in window.response_view.toPlainText()
        assert window.copy_response.isEnabled()
        assert window.open_response.isEnabled()
    finally:
        window.close()


def test_native_desktop_renders_model_cited_web_sources(
    qt_app: QApplication, tmp_path: Path
) -> None:
    artifact_dir = tmp_path / "run"
    path = artifact_dir / "research/sources/participant/participant-a/opening.json"
    path.parent.mkdir(parents=True)
    path.write_text(
        json.dumps(
            {
                "role": "participant",
                "target_id": "participant-a",
                "turn_phase": "opening",
                "captured_at": "2026-09-01T01:02:03Z",
                "sources": [
                    {
                        "title": "Example Domain",
                        "url": "https://example.com/",
                        "claim_context": "Current evidence from the reply.",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    window = _window(tmp_path)
    try:
        window._artifact_dir = artifact_dir
        window._refresh_sources()
        qt_app.processEvents()
        assert window.workspace_tabs.tabText(3) == "Sources (1)"
        assert "Example Domain" in window.sources_view.toPlainText()
        assert "not independent verification" in window.sources_view.toPlainText()
        path.unlink()
        window._refresh_sources()
        qt_app.processEvents()
        assert window.workspace_tabs.tabText(3) == "Sources"
        assert window.sources_view.toPlainText() == ""
    finally:
        window.close()
