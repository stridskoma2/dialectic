from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
pytest.importorskip("PySide6")

from PySide6.QtCore import QUrl
from PySide6.QtGui import QTextDocument
from PySide6.QtWidgets import QApplication, QPushButton

from dialectic.history_qt import HistoryDialog
from dialectic.service import DialecticService
from tests.test_desktop_qt import _window
from tests.test_history import fingerprint, saved_run


@pytest.fixture(scope="module")
def qt_app() -> QApplication:
    return QApplication.instance() or QApplication([])


def finish(qt_app: QApplication, dialog: HistoryDialog) -> None:
    deadline = time.monotonic() + 10
    # Process the initial single-shot timer as well as queued worker signals.
    qt_app.processEvents()
    while dialog._worker is not None or dialog._search_timer.isActive():
        assert time.monotonic() < deadline, "history read did not finish"
        qt_app.processEvents()
        time.sleep(0.005)
    qt_app.processEvents()


def test_history_ui_search_open_responses_audit_and_draft_preservation(qt_app: QApplication, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    service, handle = saved_run(tmp_path)
    attempt = handle.path / "turns/participant/participant-a/opening.attempt.json"
    attempt.parent.mkdir(parents=True)
    attempt.write_text(json.dumps({
        "target_id": "participant-a", "turn_phase": "opening", "role": "participant",
        "response_completed_at": "2026-09-05T01:02:00Z",
        "response": {"runtime": "codex", "requested_model": "historic-model", "text": "# Harness answer\n\nA complete saved answer."},
    }), encoding="utf-8")
    (handle.path / "input/config.redacted.json").write_text(json.dumps({
        "normalized_config": {"council": {
            "moderator": {"runtime": "codex", "model": "historic-model", "effort": "high"},
            "participants": [],
        }},
    }))
    main = _window(tmp_path)
    main.prompt_edit.setPlainText("Keep this draft")
    reader = DialecticService.open_history(service.store.state_root)
    before = fingerprint(service.store.state_root)
    dialog = HistoryDialog(reader, main)
    try:
        dialog.show()
        finish(qt_app, dialog)
        assert dialog.sessions.topLevelItemCount() == 1
        dialog.search.setText("missing phrase")
        finish(qt_app, dialog)
        assert dialog.sessions.topLevelItemCount() == 0
        dialog.search.setText("harness")
        finish(qt_app, dialog)
        dialog.open_button.click()
        finish(qt_app, dialog)
        assert handle.run_id in dialog.session_heading.text()
        assert "READ ONLY" in dialog.session_heading.text()
        assert "harness repository" in dialog.prompt_view.toPlainText()
        assert "historic-model" in dialog.settings_view.toPlainText()
        assert "FAILED" in dialog.summary_view.toPlainText()
        assert dialog.response_list.count() == 1
        assert "complete saved answer" in dialog.response_view.toPlainText()
        dialog.copy_button.click()
        assert "# Harness answer" in QApplication.clipboard().text()
        assert all(widget.isReadOnly() for widget in (
            dialog.prompt_view, dialog.settings_view, dialog.summary_view,
            dialog.response_view, dialog.sources_view, dialog.audit_view,
        ))
        assert not any("Run Code" in b.text() or "Run Council" in b.text() for b in dialog.findChildren(QPushButton))
        dialog.audit_button.click()
        finish(qt_app, dialog)
        # Deliberately incomplete attempt/config fixtures must display a failed audit.
        assert dialog.audit_view.toPlainText().startswith("INVALID")
        assert "ATTEMPT" in dialog.audit_view.toPlainText() or "SCHEMA" in dialog.audit_view.toPlainText()
        assert main.prompt_edit.toPlainText() == "Keep this draft"
        assert main._worker is None
        assert fingerprint(service.store.state_root) == before
    finally:
        finish(qt_app, dialog)
        dialog.close()
        main.close()


def test_history_ui_valid_audit_partial_session_and_off_thread_close(qt_app: QApplication, tmp_path: Path) -> None:
    service, handle = saved_run(tmp_path)
    _, partial = saved_run(tmp_path, suffix="bbbbbbbbbb", terminal=False)
    reader = DialecticService.open_history(service.store.state_root)
    dialog = HistoryDialog(reader)
    try:
        dialog.show()
        finish(qt_app, dialog)
        dialog.open_button.click()
        finish(qt_app, dialog)
        assert partial.run_id in dialog.session_heading.text()
        assert "Partial snapshot" in dialog.status.text()
        assert "No summary" in dialog.summary_view.toPlainText()
        dialog.sessions.setCurrentItem(dialog.sessions.topLevelItem(1))
        dialog.open_button.click()
        finish(qt_app, dialog)
        dialog.audit_button.click()
        finish(qt_app, dialog)
        assert dialog.audit_view.toPlainText().startswith("VALID · complete")
        gate = threading.Event()
        worker_threads: list[int] = []

        def delayed_read():
            worker_threads.append(threading.get_ident())
            assert gate.wait(3)
            return reader.audit_run(handle.run_id)

        dialog._start("Reading…", delayed_read, dialog._audited)
        assert not dialog.open_button.isEnabled()
        dialog.reject()
        assert dialog._close_pending
        assert dialog.isVisible()
        gate.set()
        finish(qt_app, dialog)
        assert not dialog.isVisible()
        assert worker_threads and worker_threads[0] != threading.get_ident()
    finally:
        finish(qt_app, dialog)
        dialog.close()


def test_main_history_button_uses_read_only_service(qt_app: QApplication, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    reader = DialecticService.open_history(tmp_path / "missing")
    calls: list[object] = []
    monkeypatch.setattr(DialecticService, "open_history", lambda: reader)

    def inspect(dialog):
        calls.append(dialog.history)
        dialog._search_timer.stop()
        return 0

    monkeypatch.setattr(HistoryDialog, "exec", inspect)
    main = _window(tmp_path)
    try:
        main.prompt_edit.setPlainText("Draft preserved")
        main.history_button.click()
        assert calls == [reader]
        assert main.prompt_edit.toPlainText() == "Draft preserved"
        assert main._worker is None
    finally:
        main.close()


def test_history_markdown_starts_at_top_and_does_not_load_resources(qt_app: QApplication) -> None:
    from dialectic.history_qt import _history_markdown_browser, _show_markdown

    browser = _history_markdown_browser()
    try:
        browser.resize(400, 250)
        browser.show()
        _show_markdown(browser, "# Beginning\n\n" + "A paragraph of saved content.\n\n" * 200)
        qt_app.processEvents()
        assert browser.verticalScrollBar().maximum() > 0
        assert browser.verticalScrollBar().value() == 0
        browser.verticalScrollBar().setValue(browser.verticalScrollBar().maximum())
        _show_markdown(browser, "# Next response\n\n" + "Another saved paragraph.\n\n" * 200)
        qt_app.processEvents()
        assert browser.verticalScrollBar().value() == 0
        for url in (QUrl("https://example.com/image.png"), QUrl.fromLocalFile("C:/Windows/image.png")):
            assert browser.document().loadResource(QTextDocument.ResourceType.ImageResource, url) is None
    finally:
        browser.close()
