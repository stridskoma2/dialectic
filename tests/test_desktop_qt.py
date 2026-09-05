from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
pytest.importorskip("PySide6")

from PySide6.QtCore import QSettings
from PySide6.QtWidgets import QApplication

from dialectic.config import ConfigLoader
from dialectic.desktop_qt import MainWindow, RunWorker
from dialectic.service import DialecticService
from dialectic.store import RunStore
from dialectic.ui import _model_options, _prepare_run


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
