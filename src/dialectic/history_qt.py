"""Native, read-only session browser. No workflow worker or execution controls."""

from __future__ import annotations

import json
from typing import Callable

from PySide6.QtCore import QThread, QTimer, Qt, Signal
from PySide6.QtGui import QCloseEvent, QTextCharFormat, QTextCursor, QTextDocument
from PySide6.QtWidgets import (
    QApplication, QDialog, QHBoxLayout, QLabel, QLineEdit, QListWidget,
    QPlainTextEdit, QPushButton, QSplitter, QTabWidget, QTreeWidget,
    QTreeWidgetItem, QVBoxLayout, QWidget,
)

from .desktop import load_desktop_responses, load_desktop_web_sources
from .desktop_qt import _format_duration, _markdown_browser, _set_markdown, _TextPreviewDialog
from .history import HistoryListing, HistorySnapshot, RunHistory
from .schemas import RunAuditReport


class _HistoryDocument(QTextDocument):
    def loadResource(self, resource_type: int, name):
        # Saved Markdown is data; rendering it must not fetch URLs or local images.
        return None


def _history_markdown_browser():
    browser = _markdown_browser()
    document = _HistoryDocument(browser)
    document.setDefaultStyleSheet(browser.document().defaultStyleSheet())
    browser.setDocument(document)
    return browser


def _show_markdown(browser, text: str) -> None:
    _set_markdown(browser, text)
    browser.moveCursor(QTextCursor.MoveOperation.Start)
    browser.verticalScrollBar().setValue(0)


def _saved_settings(raw: str, mode: str | None) -> str:
    try:
        payload = json.loads(raw)
        config = payload["normalized_config"]
        if not isinstance(config, dict):
            raise ValueError("invalid saved settings")
        lines = ["Saved model settings", "", f"Research: {config.get('research_mode', 'not recorded')}"]

        def target(label: str, value: dict) -> None:
            identity = value.get("id")
            lines.append(f"\n{label}" + (f" · {identity}" if identity else ""))
            if value.get("target") == "@driver":
                lines.append("Same model settings as the driver; independent review session.")
            else:
                lines.extend([
                    f"Runtime: {value.get('runtime', 'not recorded')}",
                    f"Model: {value.get('model', 'not recorded')}",
                    f"Effort: {value.get('effort') or 'default'}",
                ])
            if value.get("lens"):
                lines.append(f"Review focus: {value['lens']}")

        if mode == "code":
            target("Driver", config["driver"])
            for reviewer in config.get("reviewers", []):
                target("Reviewer", reviewer)
        elif mode == "council":
            council = config["council"]
            lines.extend([
                f"Moderator mode: {council.get('moderator_mode', 'not recorded')}",
                f"Allowed dissenters: {council.get('consensus', {}).get('max_dissenters', 'not recorded')}",
            ])
            target("Moderator", council["moderator"])
            for participant in council.get("participants", []):
                target("Participant", participant)
        else:
            return "Run mode is unavailable. Inspect input/config.redacted.json in Evidence."
        allotment = config.get("limits", {}).get("agent_turn_seconds")
        if allotment is not None:
            lines.append(f"\nInitial turn allotment: {allotment} seconds")
        return "\n".join(lines)
    except (ValueError, TypeError, KeyError, AttributeError):
        return "Saved settings are missing or unreadable. Inspect input/config.redacted.json in Evidence and run Audit evidence for details."


class HistoryWorker(QThread):
    result_ready = Signal(object)
    failed = Signal(str)

    def __init__(self, operation: Callable[[], object], parent: QWidget) -> None:
        super().__init__(parent)
        self.operation = operation

    def run(self) -> None:
        try:
            self.result_ready.emit(self.operation())
        except Exception as exc:
            # Validation exceptions may contain artifact contents; keep diagnostics bounded.
            self.failed.emit(f"Could not read retained evidence ({type(exc).__name__}).")


class HistoryDialog(QDialog):
    def __init__(self, history: RunHistory, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.history = history
        self._worker: HistoryWorker | None = None
        self._close_pending = False
        self._closed = False
        self._snapshot: HistorySnapshot | None = None
        self._responses = ()
        self.setObjectName("Root")
        self.setStyleSheet(
            "QHeaderView::section { background: #14171d; color: #929baa; border: 0; padding: 6px; }"
        )
        self.setWindowTitle("Dialectic · Session history · READ ONLY")
        self.resize(1220, 800)
        self.setMinimumSize(850, 600)
        layout = QVBoxLayout(self)
        heading = QLabel("Session history · READ ONLY")
        heading.setObjectName("SectionTitle")
        layout.addWidget(heading)
        layout.addWidget(QLabel("Browse retained sessions. Your current draft and run stay in the main window."))
        search_row = QHBoxLayout()
        self.search = QLineEdit()
        self.search.setPlaceholderText("Search original prompt, run ID, mode, status or outcome…")
        self.search.setAccessibleName("Search session history")
        self.refresh_button = QPushButton("Refresh")
        self.refresh_button.clicked.connect(self.refresh)
        search_row.addWidget(self.search, 1)
        search_row.addWidget(self.refresh_button)
        layout.addLayout(search_row)
        splitter = QSplitter(Qt.Orientation.Horizontal)
        self.sessions = QTreeWidget()
        self.sessions.setHeaderLabels(["Session / local date", "Outcome"])
        self.sessions.setRootIsDecorated(False)
        self.sessions.setColumnWidth(0, 270)
        self.sessions.currentItemChanged.connect(lambda *_: self._update_buttons())
        self.sessions.itemDoubleClicked.connect(lambda *_: self.open_selected())
        splitter.addWidget(self.sessions)
        detail = QWidget()
        detail_layout = QVBoxLayout(detail)
        detail_layout.setContentsMargins(0, 0, 0, 0)
        self.session_heading = QLabel("Select a session, then Open read-only.")
        self.session_heading.setTextFormat(Qt.TextFormat.PlainText)
        self.session_heading.setWordWrap(True)
        detail_layout.addWidget(self.session_heading)
        self.tabs = QTabWidget()
        self.prompt_view = _history_markdown_browser()
        self.settings_view = QPlainTextEdit()
        self.settings_view.setReadOnly(True)
        self.summary_view = _history_markdown_browser()
        self.responses_tab = QWidget()
        response_layout = QVBoxLayout(self.responses_tab)
        self.response_list = QListWidget()
        self.response_list.setMaximumHeight(160)
        self.response_list.currentRowChanged.connect(self._select_response)
        self.response_view = _history_markdown_browser()
        self.copy_button = QPushButton("Copy Markdown")
        self.copy_button.clicked.connect(self._copy_response)
        response_layout.addWidget(self.response_list)
        response_layout.addWidget(self.copy_button)
        response_layout.addWidget(self.response_view, 1)
        self.sources_view = _history_markdown_browser()
        self.evidence_tree = QTreeWidget()
        self.evidence_tree.setHeaderLabels(["Artifact · double-click to preview", "Bytes"])
        self.evidence_tree.setRootIsDecorated(False)
        self.evidence_tree.setColumnWidth(0, 420)
        self.evidence_tree.itemDoubleClicked.connect(self._preview_artifact)
        self.audit_view = QPlainTextEdit()
        self.audit_view.setReadOnly(True)
        for widget, label in (
            (self.prompt_view, "Prompt"), (self.settings_view, "Settings"),
            (self.summary_view, "Summary"), (self.responses_tab, "Responses"),
            (self.sources_view, "Sources"), (self.evidence_tree, "Evidence"),
            (self.audit_view, "Audit"),
        ):
            self.tabs.addTab(widget, label)
        detail_layout.addWidget(self.tabs, 1)
        splitter.addWidget(detail)
        splitter.setSizes([390, 800])
        layout.addWidget(splitter, 1)
        self.status = QLabel()
        self.status.setWordWrap(True)
        self.status.setTextFormat(Qt.TextFormat.PlainText)
        layout.addWidget(self.status)
        buttons = QHBoxLayout()
        self.open_button = QPushButton("Open read-only")
        self.open_button.clicked.connect(self.open_selected)
        self.audit_button = QPushButton("Audit evidence")
        self.audit_button.clicked.connect(self.audit)
        self.close_button = QPushButton("Close history")
        self.close_button.clicked.connect(self.reject)
        buttons.addWidget(self.open_button)
        buttons.addWidget(self.audit_button)
        buttons.addStretch(1)
        buttons.addWidget(self.close_button)
        layout.addLayout(buttons)
        self._search_timer = QTimer(self)
        self._search_timer.setSingleShot(True)
        self._search_timer.setInterval(300)
        self._search_timer.timeout.connect(self.refresh)
        self.search.textChanged.connect(lambda _: self._search_timer.start())
        self._update_buttons()
        QTimer.singleShot(0, self.refresh)

    def _start(self, message: str, operation: Callable[[], object], receive: Callable) -> None:
        if self._worker is not None or self._closed:
            return
        self.status.setText(message)
        worker = HistoryWorker(operation, self)
        self._worker = worker
        worker.result_ready.connect(receive)
        worker.failed.connect(self._read_failed)
        worker.finished.connect(self._finished)
        self._update_buttons()
        worker.start()

    def _read_failed(self, message: str) -> None:
        self.status.setText(message)
        if self.audit_view.toPlainText() == "Auditing retained evidence…":
            self.audit_view.setPlainText(message)

    def _finished(self) -> None:
        worker = self._worker
        self._worker = None
        if worker is not None:
            worker.deleteLater()
        self._update_buttons()
        if self._close_pending:
            self.reject()

    def _update_buttons(self) -> None:
        idle = self._worker is None
        self.search.setEnabled(idle)
        self.refresh_button.setEnabled(idle)
        self.sessions.setEnabled(idle)
        self.open_button.setEnabled(idle and self.sessions.currentItem() is not None)
        self.audit_button.setEnabled(idle and self._snapshot is not None)
        self.evidence_tree.setEnabled(idle)
        self.copy_button.setEnabled(bool(self._responses))

    def refresh(self) -> None:
        query = self.search.text()
        self._start("Searching retained sessions…", lambda: self.history.list_runs(query), self._listed)

    def _listed(self, listing: HistoryListing) -> None:
        self.sessions.clear()
        for entry in listing.entries:
            record = entry.record
            date = record.created_at.astimezone().strftime("%Y-%m-%d %H:%M") if record else entry.run_id
            mode = record.mode.title() if record else "Unavailable"
            outcome = (record.code_outcome or record.consensus_outcome or record.status) if record else "Unreadable"
            item = QTreeWidgetItem([f"{entry.title}\n{date} · {mode}", outcome])
            item.setData(0, Qt.ItemDataRole.UserRole, entry.run_id)
            item.setToolTip(0, f"{entry.run_id}\n{entry.title}\n" + "\n".join(entry.warnings))
            self.sessions.addTopLevelItem(item)
        if listing.entries:
            self.sessions.setCurrentItem(self.sessions.topLevelItem(0))
        suffix = " · Results limited; narrow your search." if listing.limited else ""
        self.status.setText(f"{len(listing.entries)} sessions · newest first{suffix}")

    def open_selected(self) -> None:
        item = self.sessions.currentItem()
        if item is None:
            return
        run_id = str(item.data(0, Qt.ItemDataRole.UserRole))
        self._start("Loading saved session…", lambda: self.history.load_run(run_id), self._loaded)

    def _loaded(self, snapshot: HistorySnapshot) -> None:
        self._snapshot = snapshot
        entry = snapshot.entry
        record = entry.record
        state = f"{record.mode.title()} · {record.status}" if record else "Unreadable run metadata"
        outcome = (record.code_outcome or record.consensus_outcome or "") if record else ""
        self.session_heading.setText(f"READ ONLY · {entry.run_id}\n{state} · {outcome}")
        _show_markdown(self.prompt_view, entry.prompt or "No prompt was retained for this session.")
        self.settings_view.setPlainText(_saved_settings(
            snapshot.contents.get("input/config.redacted.json", ""), record.mode if record else None,
        ))
        _show_markdown(self.summary_view, snapshot.contents.get("summary.md", "No summary was retained. Available partial evidence is shown in the other tabs."))
        self._responses = load_desktop_responses(snapshot.artifact_dir, contents=snapshot.contents)
        self.response_list.clear()
        self.response_view.clear()
        for response in self._responses:
            self.response_list.addItem(
                f"{response.target_id} · {response.phase} · {response.model or response.runtime}"
                f" · {_format_duration(response.duration_seconds)}"
            )
        if self._responses:
            self.response_list.setCurrentRow(0)
        self.tabs.setTabText(3, f"Responses ({len(self._responses)})")
        sources = load_desktop_web_sources(snapshot.artifact_dir, contents=snapshot.contents)
        # Plain titles/context keep artifact-supplied Markdown from inventing links.
        self.sources_view.clear()
        cursor = self.sources_view.textCursor()
        for source in sources:
            cursor.insertText(f"{source.title}\n{source.target_id} · {source.phase}\n{source.claim_context}\n")
            link = QTextCharFormat()
            link.setAnchor(True)
            link.setAnchorHref(source.url)
            link.setFontUnderline(True)
            cursor.insertText(source.url, link)
            cursor.insertText("\n\n", QTextCharFormat())
        if not sources:
            self.sources_view.setPlainText("No retained web sources.")
        self.tabs.setTabText(4, f"Sources ({len(sources)})")
        self.evidence_tree.clear()
        for relative, size in snapshot.artifacts:
            self.evidence_tree.addTopLevelItem(QTreeWidgetItem([relative, f"{size:,}"]))
        self.audit_view.setPlainText("Not audited. Use Audit evidence to validate this saved session offline.")
        self.tabs.setCurrentWidget(self.summary_view)
        note = "Saved snapshot; opening history does not resume or refresh a run."
        if record and record.status in {"CREATED", "RUNNING"}:
            note = "Partial snapshot. The saved status does not prove a process is still running."
        self.status.setText(note + ("\n" + "\n".join(snapshot.warnings[:4]) if snapshot.warnings else ""))

    def _select_response(self, index: int) -> None:
        if 0 <= index < len(self._responses):
            response = self._responses[index]
            if response.status == "failed":
                self.response_view.setPlainText(response.text)
            else:
                _show_markdown(self.response_view, response.text)

    def _copy_response(self) -> None:
        index = self.response_list.currentRow()
        if 0 <= index < len(self._responses):
            QApplication.clipboard().setText(self._responses[index].text)

    def audit(self) -> None:
        if self._snapshot is None:
            return
        run_id = self._snapshot.entry.run_id
        self.audit_view.setPlainText("Auditing retained evidence…")
        self.tabs.setCurrentWidget(self.audit_view)
        self._start("Auditing evidence offline…", lambda: self.history.audit_run(run_id), self._audited)

    def _audited(self, report: RunAuditReport) -> None:
        verdict = "VALID" if report.valid else "INVALID"
        completeness = "complete" if report.complete else "incomplete"
        lines = [
            f"{verdict} · {completeness} · saved status: {report.status or 'unavailable'}",
            f"Run: {report.run_id}",
            f"Checked {report.files_checked} files, {report.events_checked} events, {report.attempts_checked} turns ({report.bytes_checked:,} bytes).",
            f"Manifest SHA-256: {report.manifest_sha256 or 'unavailable'}", "",
        ]
        for issue in report.issues:
            lines.extend([f"{issue.severity.upper()} · {issue.code} · {issue.path or '.'}", issue.detail, ""])
        if not report.issues:
            lines.append("No integrity issues found.")
        self.audit_view.setPlainText("\n".join(lines))
        self.status.setText(f"Audit: {verdict} · {completeness}. Evidence was not changed.")

    def _preview_artifact(self, item: QTreeWidgetItem, _column: int) -> None:
        snapshot = self._snapshot
        if snapshot is None:
            return
        relative = item.text(0)

        def show(text: str) -> None:
            preview = _TextPreviewDialog(
                title="Read-only artifact", path=snapshot.artifact_dir / relative,
                text=text, truncated=False, parent=self,
            )
            preview.setObjectName("Root")
            preview.exec()
            self.status.setText("Read-only artifact preview closed.")

        self._start("Reading artifact…", lambda: self.history.read_artifact(snapshot.entry.run_id, relative), show)

    def reject(self) -> None:
        self._search_timer.stop()
        if self._worker is not None:
            self._close_pending = True
            self.status.setText("Closing after the current read finishes…")
            return
        self._closed = True
        super().reject()

    def closeEvent(self, event: QCloseEvent) -> None:
        self.reject()
        if self._worker is not None:
            event.ignore()
        else:
            event.accept()
