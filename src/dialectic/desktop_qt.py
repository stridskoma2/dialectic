"""PySide6 desktop frontend for the bounded Dialectic workflows."""

from __future__ import annotations

import asyncio
import logging
import os
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from PySide6.QtCore import QSettings, QThread, QTimer, Qt, QUrl, Signal, Slot
from PySide6.QtGui import QCloseEvent, QDesktopServices, QTextDocument
from PySide6.QtWidgets import (
    QApplication,
    QButtonGroup,
    QComboBox,
    QDialog,
    QFileDialog,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMainWindow,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QSpinBox,
    QSplitter,
    QTabWidget,
    QTextBrowser,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from .app_logging import log_event
from .contracts import RunMode
from .desktop import DesktopResponse, load_desktop_responses
from .runtime import build_service
from .schemas import RunRecord
from .ui import (
    _PreparedRun,
    _choose_repository,
    _model_options,
    _open_path,
    _prepare_run,
    _text_preview,
)
from .ui_config import SUPPORTED_EFFORTS

_LOGGER = logging.getLogger("dialectic.desktop")
_RUNTIME_LABELS = {
    "codex": "Codex",
    "claude-code": "Claude Code",
    "grok-build": "Grok Build",
    "@driver": "Main driver",
}
_FOCUSES = (
    "general-correctness",
    "tests-and-edge-cases",
    "security-and-operational-risk",
    "maintainability-and-scope",
    "performance-and-concurrency",
)
_CODE_STEPS = (
    ("Preflight", {"PREFLIGHT", "WORKTREE_SETUP"}),
    ("Implement", {"DRIVER_INITIAL", "INITIAL_VALIDATION"}),
    ("Review", {"REVIEWERS", "FEEDBACK"}),
    ("Repair", {"DRIVER_REPAIR", "FINAL_VALIDATION"}),
    ("Final", {"REPORTING"}),
)
_COUNCIL_STEPS = (
    ("Preflight", {"PREFLIGHT"}),
    ("Openings", {"OPENING_POSITIONS"}),
    ("Cross-exam", {"CROSS_EXAMINATION"}),
    ("Moderator", {"MODERATION"}),
    ("Ballots", {"BALLOTS", "REPORTING"}),
)
_MARKDOWN_FEATURES = (
    QTextDocument.MarkdownFeature.MarkdownDialectGitHub
    | QTextDocument.MarkdownFeature.MarkdownNoHTML
)


@dataclass(frozen=True, slots=True)
class DesktopRunResult:
    record: RunRecord
    artifact_dir: Path
    summary_path: Path
    worktree: Path | None
    branch: str
    unresolved_count: int


class RunWorker(QThread):
    run_created = Signal(str, str)
    progress = Signal(object)
    result_ready = Signal(object)
    run_error = Signal(str)

    def __init__(self, prepared: _PreparedRun, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._prepared = prepared
        self._loop: asyncio.AbstractEventLoop | None = None
        self._task: asyncio.Task[RunRecord] | None = None
        self._cancel_requested = False

    @Slot()
    def request_cancel(self) -> None:
        self._cancel_requested = True
        loop = self._loop
        task = self._task
        if loop is not None and task is not None and not task.done():
            loop.call_soon_threadsafe(task.cancel)

    def run(self) -> None:
        prepared = self._prepared
        log_event(_LOGGER, logging.INFO, "ui.run_requested", mode=prepared.mode)
        loop = asyncio.new_event_loop()
        self._loop = loop
        try:
            asyncio.set_event_loop(loop)
            service = build_service()
            service.set_progress_observer(self._present_progress)
            handle = service.create_run(prepared.mode)
            self.run_created.emit(handle.run_id, str(handle.path))
            log_event(
                _LOGGER,
                logging.INFO,
                "ui.run_created",
                run_id=handle.run_id,
                mode=prepared.mode,
            )
            if prepared.mode == "code":
                assert prepared.repository is not None
                operation = service.execute_code_once(
                    handle,
                    config_bytes=prepared.config_bytes,
                    task_bytes=prepared.prompt_bytes,
                    repository_path=prepared.repository,
                )
            else:
                operation = service.execute_council_once(
                    handle,
                    config_bytes=prepared.config_bytes,
                    prompt_bytes=prepared.prompt_bytes,
                )
            self._task = loop.create_task(operation)
            try:
                record = loop.run_until_complete(self._task)
            except asyncio.CancelledError:
                # A cancellation requested immediately after creation can arrive
                # before DialecticService._execute enters its own cancellation
                # boundary. Persist the same terminal contract in that narrow race.
                record = service.cancel_run(handle, "user cancellation")
            summary = service.get_result(record.run_id)
            artifact_dir = service.run_artifact_directory(record.run_id)
            workspace = service.get_workspace(record.run_id)
            worktree = None
            branch = ""
            if workspace is not None:
                if workspace.dialectic_worktree:
                    worktree = Path(workspace.dialectic_worktree)
                branch = workspace.dialectic_branch or ""
            result = DesktopRunResult(
                record=record,
                artifact_dir=artifact_dir,
                summary_path=artifact_dir / "summary.md",
                worktree=worktree,
                branch=branch,
                unresolved_count=len(summary.unresolved_items),
            )
            log_event(
                _LOGGER,
                logging.INFO if record.status == "FINALIZED" else logging.WARNING,
                "ui.run_completed",
                run_id=record.run_id,
                mode=record.mode,
                status=record.status,
                failure_kind=record.failure_kind,
                failure_detail=record.failure_detail,
            )
            self.result_ready.emit(result)
        except Exception as exc:
            log_event(
                _LOGGER,
                logging.ERROR,
                "ui.run_error",
                exception_type=type(exc).__name__,
            )
            self.run_error.emit(f"{type(exc).__name__}: {exc}")
        finally:
            self._task = None
            self._loop = None
            asyncio.set_event_loop(None)
            loop.close()

    def _present_progress(self, record: RunRecord) -> None:
        self.progress.emit(record)
        task = self._task
        if (
            self._cancel_requested
            and record.status == "RUNNING"
            and task is not None
            and not task.done()
        ):
            task.cancel()


class PhaseStrip(QFrame):
    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("PhaseStrip")
        self._mode: RunMode = "code"
        self._labels: list[QLabel] = []
        layout = QHBoxLayout(self)
        layout.setContentsMargins(12, 8, 12, 8)
        layout.setSpacing(6)
        for index in range(5):
            label = QLabel()
            label.setAlignment(Qt.AlignmentFlag.AlignCenter)
            label.setProperty("phaseState", "idle")
            layout.addWidget(label, 1)
            self._labels.append(label)
        self.set_mode("code")

    def set_mode(self, mode: RunMode) -> None:
        self._mode = mode
        steps = _CODE_STEPS if mode == "code" else _COUNCIL_STEPS
        for label, (title, _phases) in zip(self._labels, steps, strict=True):
            label.setText(title)
            self._set_state(label, "idle")

    def set_phase(self, phase: str | None, *, terminal: bool = False) -> None:
        steps = _CODE_STEPS if self._mode == "code" else _COUNCIL_STEPS
        active = next(
            (index for index, (_title, phases) in enumerate(steps) if phase in phases),
            None,
        )
        for index, label in enumerate(self._labels):
            state = "current" if index == active else "idle"
            if terminal and index == len(self._labels) - 1:
                state = "complete"
            self._set_state(label, state)

    @staticmethod
    def _set_state(label: QLabel, state: str) -> None:
        if label.property("phaseState") == state:
            return
        label.setProperty("phaseState", state)
        label.style().unpolish(label)
        label.style().polish(label)


class AgentRow(QFrame):
    remove_requested = Signal(object)

    def __init__(
        self,
        options: dict[str, object],
        *,
        mode: RunMode,
        choice: dict[str, str],
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setObjectName("AgentRow")
        self._options = options
        self._mode = mode
        layout = QGridLayout(self)
        layout.setContentsMargins(8, 8, 8, 8)
        layout.setHorizontalSpacing(7)
        layout.setVerticalSpacing(6)

        self.runtime = QComboBox()
        self.model = QComboBox()
        self.effort = QComboBox()
        for combo in (self.runtime, self.model, self.effort):
            _configure_combo(combo)
        self.runtime.setMinimumWidth(108)
        self.effort.setMinimumWidth(84)
        self.focus = QLineEdit()
        self.focus.setPlaceholderText("Review focus")
        self.remove = QPushButton("×")
        self.remove.setObjectName("Remove")
        self.remove.setFixedWidth(28)
        self.remove.setToolTip("Remove model")
        layout.addWidget(self.runtime, 0, 0)
        layout.addWidget(self.model, 0, 1)
        layout.addWidget(self.remove, 0, 2)
        layout.setColumnStretch(1, 1)
        if mode == "code":
            layout.addWidget(self.effort, 1, 0)
            layout.addWidget(self.focus, 1, 1, 1, 2)
        else:
            layout.addWidget(self.effort, 1, 0, 1, 2)
            self.focus.hide()

        for runtime in (("@driver",) if mode == "code" else ()) + (
            "codex",
            "claude-code",
            "grok-build",
        ):
            label = _RUNTIME_LABELS[runtime]
            if runtime != "@driver":
                runtime_status = _runtime_status(options, runtime)
                label += " · installed" if runtime_status else " · not installed"
            self.runtime.addItem(label, runtime)
        selected_runtime = choice.get("runtime", "codex")
        _select_data(self.runtime, selected_runtime)
        self._populate_for_runtime(
            selected_model=choice.get("model", ""),
            selected_effort=choice.get("effort", ""),
        )
        self.focus.setText(choice.get("lens", "general-correctness"))
        self.runtime.currentIndexChanged.connect(self._runtime_changed)
        self.remove.clicked.connect(lambda: self.remove_requested.emit(self))

    def payload(self) -> dict[str, str]:
        return {
            "runtime": str(self.runtime.currentData() or ""),
            "model": str(self.model.currentData() or ""),
            "effort": str(self.effort.currentData() or ""),
            "lens": self.focus.text().strip(),
        }

    def set_running(self, running: bool) -> None:
        for widget in (self.runtime, self.model, self.effort, self.focus, self.remove):
            widget.setDisabled(running)
        if not running and self.runtime.currentData() == "@driver":
            self.model.setDisabled(True)
            self.effort.setDisabled(True)

    def _runtime_changed(self) -> None:
        self._populate_for_runtime()

    def _populate_for_runtime(
        self,
        *,
        selected_model: str = "",
        selected_effort: str = "",
    ) -> None:
        runtime = str(self.runtime.currentData() or "")
        self.model.clear()
        self.effort.clear()
        if runtime == "@driver":
            self.model.addItem("Inherits main model", "")
            self.effort.addItem("inherits", "")
            self.model.setDisabled(True)
            self.effort.setDisabled(True)
            return
        self.model.setEnabled(True)
        self.effort.setEnabled(True)
        for item in _models_for(self._options, runtime):
            self.model.addItem(str(item.get("name", item.get("id", ""))), item.get("id", ""))
        _select_data(self.model, selected_model)
        for effort in SUPPORTED_EFFORTS[runtime]:  # type: ignore[index]
            self.effort.addItem(effort or "default", effort)
        _select_data(self.effort, selected_effort)


class MainWindow(QMainWindow):
    def __init__(
        self,
        *,
        options: dict[str, object] | None = None,
        log_path: Path | None = None,
        worker_factory: Callable[[_PreparedRun, QWidget | None], RunWorker] = RunWorker,
        settings: QSettings | None = None,
    ) -> None:
        super().__init__()
        self._options = options or _model_options()
        self._log_path = log_path
        self._worker_factory = worker_factory
        self._worker: RunWorker | None = None
        self._running = False
        self._close_after_run = False
        self._mode: RunMode = "code"
        self._artifact_dir: Path | None = None
        self._summary_path: Path | None = None
        self._worktree: Path | None = None
        self._responses: dict[str, DesktopResponse] = {}
        self._response_signature: tuple[tuple[str, str, int], ...] = ()
        self._agent_rows: list[AgentRow] = []
        self._settings = settings or QSettings("OpenAI", "Dialectic")
        self._drafts = _default_drafts(self._options)

        self.setWindowTitle("Dialectic")
        self.resize(1280, 820)
        self.setMinimumSize(920, 650)
        self._build_ui()
        self._load_mode("code")
        self._restore_settings()
        self.log_button.setEnabled(log_path is not None)
        self.summary_button.setEnabled(False)
        self.artifacts_button.setEnabled(False)
        self.worktree_button.setEnabled(False)

        self._response_timer = QTimer(self)
        self._response_timer.setInterval(500)
        self._response_timer.timeout.connect(self._refresh_responses)

    def _build_ui(self) -> None:
        root = QWidget()
        root.setObjectName("Root")
        self.setCentralWidget(root)
        layout = QVBoxLayout(root)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        layout.addWidget(self._build_header())
        self.main_splitter = QSplitter(Qt.Orientation.Horizontal)
        self.main_splitter.setChildrenCollapsible(False)
        self.main_splitter.setHandleWidth(7)
        self.main_splitter.addWidget(self._build_setup_panel())
        self.main_splitter.addWidget(self._build_workspace_panel())
        self.main_splitter.setStretchFactor(0, 0)
        self.main_splitter.setStretchFactor(1, 1)
        self.main_splitter.setSizes([460, 820])
        layout.addWidget(self.main_splitter, 1)
        layout.addWidget(self._build_footer())

    def _build_header(self) -> QWidget:
        header = QFrame()
        header.setObjectName("Header")
        header.setFixedHeight(62)
        layout = QHBoxLayout(header)
        layout.setContentsMargins(22, 0, 22, 0)
        layout.setSpacing(16)
        brand = QLabel("Dialectic")
        brand.setObjectName("Brand")
        subbrand = QLabel("bounded multi-model work")
        subbrand.setObjectName("Muted")
        self.header_status = QLabel("●  Ready")
        self.header_status.setObjectName("HeaderStatus")
        layout.addWidget(brand)
        layout.addWidget(subbrand)
        layout.addStretch(1)
        layout.addWidget(self.header_status)
        return header

    def _build_setup_panel(self) -> QWidget:
        scroll = QScrollArea()
        scroll.setObjectName("SetupScroll")
        scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        scroll.setMinimumWidth(360)
        scroll.setMaximumWidth(520)
        content = QWidget()
        content.setObjectName("SetupContent")
        scroll.setWidget(content)
        layout = QVBoxLayout(content)
        layout.setContentsMargins(16, 16, 16, 16)
        layout.setSpacing(12)

        panel = QFrame()
        panel.setObjectName("Panel")
        panel_layout = QVBoxLayout(panel)
        panel_layout.setContentsMargins(14, 14, 14, 14)
        panel_layout.setSpacing(13)
        mode_row = QHBoxLayout()
        mode_row.setSpacing(4)
        self.code_mode = QPushButton("Code Once")
        self.council_mode = QPushButton("Council Once")
        for button in (self.code_mode, self.council_mode):
            button.setCheckable(True)
            button.setObjectName("ModeButton")
            mode_row.addWidget(button, 1)
        self.mode_group = QButtonGroup(self)
        self.mode_group.setExclusive(True)
        self.mode_group.addButton(self.code_mode)
        self.mode_group.addButton(self.council_mode)
        self.code_mode.clicked.connect(lambda: self._switch_mode("code"))
        self.council_mode.clicked.connect(lambda: self._switch_mode("council"))
        panel_layout.addLayout(mode_row)
        self.mode_hint = QLabel()
        self.mode_hint.setObjectName("Hint")
        self.mode_hint.setWordWrap(True)
        panel_layout.addWidget(self.mode_hint)

        self.main_title = QLabel("Main model")
        self.main_title.setObjectName("SectionTitle")
        panel_layout.addWidget(self.main_title)
        main_grid = QGridLayout()
        main_grid.setHorizontalSpacing(7)
        main_grid.setVerticalSpacing(5)
        for column, text in enumerate(("Runtime", "Model", "Effort")):
            label = QLabel(text)
            label.setObjectName("FieldLabel")
            main_grid.addWidget(label, 0, column)
        self.main_runtime = QComboBox()
        self.main_model = QComboBox()
        self.main_effort = QComboBox()
        for combo in (self.main_runtime, self.main_model, self.main_effort):
            _configure_combo(combo)
        self.main_runtime.setMinimumWidth(86)
        self.main_effort.setMinimumWidth(72)
        main_grid.addWidget(self.main_runtime, 1, 0)
        main_grid.addWidget(self.main_model, 1, 1)
        main_grid.addWidget(self.main_effort, 1, 2)
        main_grid.setColumnStretch(1, 1)
        panel_layout.addLayout(main_grid)
        self.main_model_hint = QLabel()
        self.main_model_hint.setObjectName("Hint")
        self.main_model_hint.setWordWrap(True)
        panel_layout.addWidget(self.main_model_hint)
        self.main_runtime.currentIndexChanged.connect(self._main_runtime_changed)
        self.main_model.currentIndexChanged.connect(self._show_main_model_hint)

        repo_label = QLabel("Repository")
        repo_label.setObjectName("FieldLabel")
        panel_layout.addWidget(repo_label)
        repo_row = QHBoxLayout()
        self.repository = QLineEdit()
        self.repository.setPlaceholderText(r"C:\git\your-project")
        self.browse = QPushButton("Browse")
        self.browse.clicked.connect(self._browse_repository)
        repo_row.addWidget(self.repository, 1)
        repo_row.addWidget(self.browse)
        panel_layout.addLayout(repo_row)
        self.repo_hint = QLabel()
        self.repo_hint.setObjectName("Hint")
        self.repo_hint.setWordWrap(True)
        panel_layout.addWidget(self.repo_hint)

        agent_head = QHBoxLayout()
        self.agents_title = QLabel("Review models")
        self.agents_title.setObjectName("SectionTitle")
        self.add_agent = QPushButton("+ Add model")
        self.add_agent.clicked.connect(self._add_agent)
        agent_head.addWidget(self.agents_title)
        agent_head.addStretch(1)
        agent_head.addWidget(self.add_agent)
        panel_layout.addLayout(agent_head)
        self.agent_layout = QVBoxLayout()
        self.agent_layout.setSpacing(8)
        panel_layout.addLayout(self.agent_layout)

        self.council_settings = QFrame()
        council_layout = QGridLayout(self.council_settings)
        council_layout.setContentsMargins(0, 6, 0, 0)
        council_layout.setHorizontalSpacing(8)
        moderator_label = QLabel("Moderator behavior")
        moderator_label.setObjectName("FieldLabel")
        self.moderator_mode = QComboBox()
        self.moderator_mode.addItem("Fresh synthesis only", "fresh")
        self.moderator_mode.addItem(
            "Independent opening + synthesis", "independent-opening"
        )
        dissent_label = QLabel("Allowed dissenters")
        dissent_label.setObjectName("FieldLabel")
        self.max_dissenters = QSpinBox()
        self.max_dissenters.setMinimum(0)
        council_layout.addWidget(moderator_label, 0, 0)
        council_layout.addWidget(self.moderator_mode, 1, 0)
        council_layout.addWidget(dissent_label, 0, 1)
        council_layout.addWidget(self.max_dissenters, 1, 1)
        council_layout.setColumnStretch(0, 1)
        panel_layout.addWidget(self.council_settings)

        layout.addWidget(panel)
        layout.addStretch(1)
        return scroll

    def _build_workspace_panel(self) -> QWidget:
        container = QWidget()
        container.setObjectName("Workspace")
        layout = QVBoxLayout(container)
        layout.setContentsMargins(0, 16, 16, 16)
        layout.setSpacing(10)

        self.phase_strip = PhaseStrip()
        layout.addWidget(self.phase_strip)

        self.workspace_tabs = QTabWidget()
        self.workspace_tabs.setObjectName("WorkspaceTabs")
        self.prompt_tab = self._build_prompt_tab()
        self.summary_view = _markdown_browser()
        _set_markdown(
            self.summary_view,
            "# No run yet\n\nTerminal summaries and evidence will appear here.",
        )
        self.responses_tab = self._build_responses_tab()
        self.evidence_tree = QTreeWidget()
        self.evidence_tree.setHeaderLabels(["Artifact", "Size"])
        self.evidence_tree.setColumnWidth(0, 420)
        self.evidence_tree.itemDoubleClicked.connect(self._open_evidence_item)
        self.workspace_tabs.addTab(self.prompt_tab, "Prompt")
        self.workspace_tabs.addTab(self.summary_view, "Summary")
        self.workspace_tabs.addTab(self.responses_tab, "Responses")
        self.workspace_tabs.addTab(self.evidence_tree, "Evidence")
        layout.addWidget(self.workspace_tabs, 1)

        result = QFrame()
        result.setObjectName("ResultBar")
        result_layout = QHBoxLayout(result)
        result_layout.setContentsMargins(14, 11, 14, 11)
        result_text = QVBoxLayout()
        result_text.setSpacing(2)
        self.result_title = QLabel("No run yet")
        self.result_title.setObjectName("ResultTitle")
        self.result_detail = QLabel("Terminal summaries and evidence will appear here.")
        self.result_detail.setObjectName("Hint")
        self.result_detail.setWordWrap(True)
        result_text.addWidget(self.result_title)
        result_text.addWidget(self.result_detail)
        result_layout.addLayout(result_text, 1)
        self.log_button = QPushButton("App log")
        self.summary_button = QPushButton("Summary")
        self.artifacts_button = QPushButton("Artifacts")
        self.worktree_button = QPushButton("Worktree")
        self.log_button.clicked.connect(self._show_log)
        self.summary_button.clicked.connect(
            lambda: self.workspace_tabs.setCurrentWidget(self.summary_view)
        )
        self.artifacts_button.clicked.connect(lambda: self._open_named_path("artifacts"))
        self.worktree_button.clicked.connect(lambda: self._open_named_path("worktree"))
        for button in (
            self.log_button,
            self.summary_button,
            self.artifacts_button,
            self.worktree_button,
        ):
            result_layout.addWidget(button)
        layout.addWidget(result)
        return container

    def _build_prompt_tab(self) -> QWidget:
        tab = QWidget()
        layout = QVBoxLayout(tab)
        layout.setContentsMargins(14, 14, 14, 14)
        layout.setSpacing(8)
        head = QHBoxLayout()
        self.prompt_title = QLabel("Implementation task")
        self.prompt_title.setObjectName("SectionTitle")
        self.prompt_counter = QLabel("0 characters")
        self.prompt_counter.setObjectName("Hint")
        head.addWidget(self.prompt_title)
        head.addStretch(1)
        head.addWidget(self.prompt_counter)
        layout.addLayout(head)
        self.prompt_tabs = QTabWidget()
        self.prompt_edit = QPlainTextEdit()
        self.prompt_edit.setPlaceholderText(
            "Describe the change, constraints, and verification you expect…"
        )
        self.prompt_edit.setLineWrapMode(QPlainTextEdit.LineWrapMode.WidgetWidth)
        self.prompt_preview = _markdown_browser()
        self.prompt_tabs.addTab(self.prompt_edit, "Write")
        self.prompt_tabs.addTab(self.prompt_preview, "Preview")
        self.prompt_tabs.currentChanged.connect(self._prompt_tab_changed)
        self.prompt_edit.textChanged.connect(self._prompt_changed)
        layout.addWidget(self.prompt_tabs, 1)
        return tab

    def _build_responses_tab(self) -> QWidget:
        tab = QWidget()
        layout = QVBoxLayout(tab)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        toolbar = QFrame()
        toolbar.setObjectName("ResponseToolbar")
        toolbar_layout = QHBoxLayout(toolbar)
        toolbar_layout.setContentsMargins(12, 8, 12, 8)
        self.response_heading = QLabel("Model responses")
        self.response_heading.setObjectName("SectionTitle")
        self.copy_response = QPushButton("Copy Markdown")
        self.open_response = QPushButton("Open artifact")
        self.copy_response.setEnabled(False)
        self.open_response.setEnabled(False)
        self.copy_response.clicked.connect(self._copy_current_response)
        self.open_response.clicked.connect(self._open_current_response)
        toolbar_layout.addWidget(self.response_heading)
        toolbar_layout.addStretch(1)
        toolbar_layout.addWidget(self.copy_response)
        toolbar_layout.addWidget(self.open_response)
        layout.addWidget(toolbar)
        splitter = QSplitter(Qt.Orientation.Horizontal)
        splitter.setChildrenCollapsible(False)
        self.response_list = QListWidget()
        self.response_list.setMinimumWidth(230)
        self.response_list.setMaximumWidth(390)
        self.response_list.currentItemChanged.connect(self._response_selected)
        self.response_view = _markdown_browser()
        _set_markdown(
            self.response_view,
            "# Waiting for responses\n\nCompleted native turns will appear here.",
        )
        splitter.addWidget(self.response_list)
        splitter.addWidget(self.response_view)
        splitter.setStretchFactor(0, 0)
        splitter.setStretchFactor(1, 1)
        layout.addWidget(splitter, 1)
        return tab

    def _build_footer(self) -> QWidget:
        footer = QFrame()
        footer.setObjectName("Footer")
        layout = QHBoxLayout(footer)
        layout.setContentsMargins(22, 10, 22, 10)
        self.run_note = QLabel()
        self.run_note.setObjectName("Hint")
        self.run_note.setWordWrap(True)
        self.cancel_button = QPushButton("Cancel run")
        self.cancel_button.setObjectName("Danger")
        self.cancel_button.hide()
        self.cancel_button.clicked.connect(self._cancel_run)
        self.run_button = QPushButton("Run Code Once")
        self.run_button.setObjectName("Primary")
        self.run_button.clicked.connect(self._start_run)
        layout.addWidget(self.run_note, 1)
        layout.addWidget(self.cancel_button)
        layout.addWidget(self.run_button)
        return footer

    def _switch_mode(self, mode: RunMode) -> None:
        if self._running or mode == self._mode:
            return
        self._save_draft()
        self._load_mode(mode)

    def _load_mode(self, mode: RunMode) -> None:
        self._mode = mode
        draft = self._drafts[mode]
        self.code_mode.setChecked(mode == "code")
        self.council_mode.setChecked(mode == "council")
        self.phase_strip.set_mode(mode)
        self.phase_strip.set_phase(None)
        self.mode_hint.setText(
            "Implement once, review the immutable diff in parallel, repair at most once, then stop."
            if mode == "code"
            else "Parallel blind openings, one anonymized cross-examination, fresh synthesis, ballots, then controller-derived consensus."
        )
        self.main_title.setText("Main model" if mode == "code" else "Moderator")
        self.agents_title.setText("Review models" if mode == "code" else "Council participants")
        self.prompt_title.setText(
            "Implementation task" if mode == "code" else "Question, evidence, and decision criteria"
        )
        self.prompt_edit.setPlaceholderText(
            "Describe the change, constraints, and verification you expect…"
            if mode == "code"
            else "State the decision, evidence, alternatives, and what consensus should resolve…"
        )
        self.run_button.setText("Run Code Once" if mode == "code" else "Run Council Once")
        self.run_note.setText(
            "Installed CLIs are labeled; model access and authentication are verified during preflight."
            if mode == "code"
            else "Participant phases run in parallel. Web search, MCP, and built-in tools are disabled in the secure profile."
        )
        self.repository.setDisabled(mode == "council")
        self.browse.setDisabled(
            mode == "council" or not bool(self._options.get("browseSupported"))
        )
        self.repo_hint.setText(
            "Required for Code Once. Dialectic works in an isolated linked worktree."
            if mode == "code"
            else "Council is prompt-only; repository paths are not disclosed to participants."
        )
        self.council_settings.setVisible(mode == "council")
        self._populate_main(
            runtime=str(draft["main"]["runtime"]),  # type: ignore[index]
            model=str(draft["main"]["model"]),  # type: ignore[index]
            effort=str(draft["main"]["effort"]),  # type: ignore[index]
        )
        self.prompt_edit.setPlainText(str(draft["prompt"]))
        _select_data(self.moderator_mode, str(draft["moderatorMode"]))
        self.max_dissenters.setValue(int(draft["maxDissenters"]))
        self._replace_agents(list(draft["agents"]))  # type: ignore[arg-type]
        self._update_agent_bounds()

    def _save_draft(self) -> None:
        self._drafts[self._mode] = {
            "main": {
                "runtime": str(self.main_runtime.currentData() or ""),
                "model": str(self.main_model.currentData() or ""),
                "effort": str(self.main_effort.currentData() or ""),
            },
            "agents": [row.payload() for row in self._agent_rows],
            "prompt": self.prompt_edit.toPlainText(),
            "maxDissenters": self.max_dissenters.value(),
            "moderatorMode": str(self.moderator_mode.currentData() or "fresh"),
        }

    def _populate_main(self, *, runtime: str, model: str, effort: str) -> None:
        self.main_runtime.blockSignals(True)
        self.main_runtime.clear()
        runtimes = ("codex",) if self._mode == "code" else (
            "codex",
            "claude-code",
            "grok-build",
        )
        for item in runtimes:
            label = _RUNTIME_LABELS[item]
            label += " · installed" if _runtime_status(self._options, item) else " · not installed"
            self.main_runtime.addItem(label, item)
        _select_data(self.main_runtime, "codex" if self._mode == "code" else runtime)
        self.main_runtime.setDisabled(self._mode == "code")
        self.main_runtime.blockSignals(False)
        self._populate_main_model(model=model, effort=effort)

    def _main_runtime_changed(self) -> None:
        self._populate_main_model(model="", effort="")

    def _populate_main_model(self, *, model: str, effort: str) -> None:
        runtime = str(self.main_runtime.currentData() or "codex")
        self.main_model.blockSignals(True)
        self.main_model.clear()
        for item in _models_for(self._options, runtime):
            self.main_model.addItem(str(item.get("name", item.get("id", ""))), item.get("id", ""))
        _select_data(self.main_model, model)
        self.main_model.blockSignals(False)
        self.main_effort.clear()
        for item in SUPPORTED_EFFORTS[runtime]:  # type: ignore[index]
            self.main_effort.addItem(item or "default", item)
        _select_data(self.main_effort, effort)
        self._show_main_model_hint()

    def _show_main_model_hint(self) -> None:
        runtime = str(self.main_runtime.currentData() or "")
        model = str(self.main_model.currentData() or "")
        choice = next(
            (item for item in _models_for(self._options, runtime) if item.get("id") == model),
            None,
        )
        if choice is None:
            self.main_model_hint.setText("Account access is verified during preflight.")
            return
        suffix = " · configured locally" if choice.get("source") == "environment" else ""
        self.main_model_hint.setText(
            f"{choice.get('description', '')} · selector {model}{suffix}"
        )

    def _replace_agents(self, choices: list[dict[str, str]]) -> None:
        while self._agent_rows:
            row = self._agent_rows.pop()
            self.agent_layout.removeWidget(row)
            row.deleteLater()
        for choice in choices:
            self._append_agent_row(choice)

    def _append_agent_row(self, choice: dict[str, str]) -> None:
        row = AgentRow(self._options, mode=self._mode, choice=choice)
        row.remove_requested.connect(self._remove_agent)
        self.agent_layout.addWidget(row)
        self._agent_rows.append(row)

    def _add_agent(self) -> None:
        if len(self._agent_rows) >= 5:
            return
        runtimes = [
            runtime
            for runtime in ("codex", "claude-code", "grok-build")
            if _runtime_status(self._options, runtime)
        ] or ["codex", "claude-code", "grok-build"]
        runtime = runtimes[len(self._agent_rows) % len(runtimes)]
        focus = _FOCUSES[len(self._agent_rows) % len(_FOCUSES)]
        self._append_agent_row(
            {"runtime": runtime, "model": "", "effort": "", "lens": focus}
        )
        self._update_agent_bounds()

    @Slot(object)
    def _remove_agent(self, row: AgentRow) -> None:
        minimum = 1 if self._mode == "code" else 2
        if len(self._agent_rows) <= minimum:
            return
        self._agent_rows.remove(row)
        self.agent_layout.removeWidget(row)
        row.deleteLater()
        self._update_agent_bounds()

    def _update_agent_bounds(self) -> None:
        minimum = 1 if self._mode == "code" else 2
        for row in self._agent_rows:
            row.remove.setDisabled(self._running or len(self._agent_rows) <= minimum)
        self.add_agent.setDisabled(self._running or len(self._agent_rows) >= 5)
        self.max_dissenters.setMaximum(max(0, len(self._agent_rows) - 1))
        if self.max_dissenters.value() >= len(self._agent_rows):
            self.max_dissenters.setValue(max(0, len(self._agent_rows) - 1))

    def _browse_repository(self) -> None:
        try:
            selected = _choose_repository()
        except ValueError:
            selected = QFileDialog.getExistingDirectory(
                self,
                "Select a Git repository",
                self.repository.text() or os.getcwd(),
            )
        if selected:
            self.repository.setText(selected)

    def _prompt_changed(self) -> None:
        self.prompt_counter.setText(f"{len(self.prompt_edit.toPlainText()):,} characters")
        if self.prompt_tabs.currentWidget() is self.prompt_preview:
            self._render_prompt_preview()

    def _prompt_tab_changed(self, _index: int) -> None:
        if self.prompt_tabs.currentWidget() is self.prompt_preview:
            self._render_prompt_preview()

    def _render_prompt_preview(self) -> None:
        markdown = self.prompt_edit.toPlainText().strip()
        _set_markdown(self.prompt_preview, markdown or "_Nothing to preview yet._")

    def _payload(self) -> dict[str, object]:
        return {
            "mode": self._mode,
            "prompt": self.prompt_edit.toPlainText(),
            "repository": self.repository.text(),
            "main": {
                "runtime": str(self.main_runtime.currentData() or ""),
                "model": str(self.main_model.currentData() or ""),
                "effort": str(self.main_effort.currentData() or ""),
            },
            "agents": [row.payload() for row in self._agent_rows],
            "maxDissenters": self.max_dissenters.value(),
            "moderatorMode": str(self.moderator_mode.currentData() or "fresh"),
        }

    def _start_run(self) -> None:
        try:
            prepared = _prepare_run(self._payload())
        except ValueError as exc:
            self._show_start_error(str(exc))
            return
        self._save_draft()
        self._reset_run_view()
        self._set_running(True)
        self.result_title.setText("Starting…")
        self.result_detail.setText("Validating selections and entering native preflight.")
        worker = self._worker_factory(prepared, self)
        self._worker = worker
        worker.run_created.connect(self._run_created)
        worker.progress.connect(self._run_progress)
        worker.result_ready.connect(self._run_completed)
        worker.run_error.connect(self._run_failed)
        worker.finished.connect(self._worker_finished)
        worker.start()

    def _reset_run_view(self) -> None:
        self._artifact_dir = None
        self._summary_path = None
        self._worktree = None
        self._responses = {}
        self._response_signature = ()
        self.response_list.clear()
        _set_markdown(
            self.response_view,
            "# Waiting for responses\n\nCompleted native turns will appear here.",
        )
        _set_markdown(
            self.summary_view,
            "# Run in progress\n\nDurable phase transitions and the terminal summary will appear here.",
        )
        self.evidence_tree.clear()
        self.copy_response.setEnabled(False)
        self.open_response.setEnabled(False)
        self.phase_strip.set_phase(None)
        self.result_detail.setProperty("error", False)
        self.result_detail.style().unpolish(self.result_detail)
        self.result_detail.style().polish(self.result_detail)

    @Slot(str, str)
    def _run_created(self, run_id: str, artifact_dir: str) -> None:
        self._artifact_dir = Path(artifact_dir)
        self.result_title.setText("Run created")
        self.result_detail.setText(f"Run {run_id} · evidence is being persisted.")
        self._response_timer.start()
        self._refresh_artifacts()

    @Slot(object)
    def _run_progress(self, record: RunRecord) -> None:
        phase = record.phase or "-"
        shown_phase = phase.replace("_", " ").title()
        self.header_status.setText(f"●  {shown_phase} · {record.status}")
        self.result_title.setText(shown_phase)
        self.result_detail.setText(f"Run {record.run_id} · {record.status}")
        self.phase_strip.set_phase(record.phase)
        self._refresh_responses()

    @Slot(object)
    def _run_completed(self, result: DesktopRunResult) -> None:
        self._response_timer.stop()
        self._artifact_dir = result.artifact_dir
        self._summary_path = result.summary_path
        self._worktree = result.worktree
        record = result.record
        self._set_running(False)
        self.phase_strip.set_phase(record.phase, terminal=True)
        outcome = record.code_outcome or record.consensus_outcome or record.status
        self.header_status.setText(f"●  {record.status}")
        self.result_title.setText(f"{outcome} · {record.status}")
        self.result_detail.setProperty("error", bool(record.failure_kind))
        self.result_detail.style().unpolish(self.result_detail)
        self.result_detail.style().polish(self.result_detail)
        details = [f"Run {record.run_id}"]
        if result.branch:
            details.append(result.branch)
        if result.unresolved_count:
            details.append(f"{result.unresolved_count} unresolved")
        if record.failure_kind:
            details.append(f"{record.failure_kind}: {record.failure_detail or ''}".strip())
        self.result_detail.setText(" · ".join(details))
        self._load_summary()
        self._refresh_responses()
        self._refresh_artifacts()
        self.workspace_tabs.setCurrentWidget(self.summary_view)

    @Slot(str)
    def _run_failed(self, message: str) -> None:
        self._response_timer.stop()
        self._set_running(False)
        self.header_status.setText("●  UI error")
        self.result_title.setText("Could not run")
        self.result_detail.setText(message)
        self.result_detail.setProperty("error", True)
        self.result_detail.style().unpolish(self.result_detail)
        self.result_detail.style().polish(self.result_detail)

    @Slot()
    def _worker_finished(self) -> None:
        worker = self._worker
        self._worker = None
        if worker is not None:
            worker.deleteLater()
        if self._close_after_run:
            QTimer.singleShot(0, self.close)

    def _set_running(self, running: bool) -> None:
        self._running = running
        for widget in (
            self.code_mode,
            self.council_mode,
            self.main_runtime,
            self.main_model,
            self.main_effort,
            self.repository,
            self.browse,
            self.moderator_mode,
            self.max_dissenters,
            self.prompt_edit,
            self.run_button,
        ):
            widget.setDisabled(running)
        if not running:
            self.main_runtime.setDisabled(self._mode == "code")
            self.repository.setDisabled(self._mode == "council")
            self.browse.setDisabled(
                self._mode == "council" or not bool(self._options.get("browseSupported"))
            )
        for row in self._agent_rows:
            row.set_running(running)
        self.cancel_button.setVisible(running)
        self.cancel_button.setEnabled(running)
        self.cancel_button.setText("Cancel run")
        self._update_agent_bounds()
        self.summary_button.setEnabled(not running and self._summary_path is not None)
        self.artifacts_button.setEnabled(self._artifact_dir is not None)
        self.worktree_button.setEnabled(self._worktree is not None)
        self.log_button.setEnabled(self._log_path is not None)
        if not running and self.header_status.text().startswith("●  Ready") is False:
            self.header_status.setProperty("active", False)

    def _cancel_run(self) -> None:
        worker = self._worker
        if worker is None:
            return
        answer = QMessageBox.question(
            self,
            "Cancel this run?",
            "Dialectic will stop the active native process unit, persist a CANCELLED run, and retain its evidence.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        worker.request_cancel()
        self.cancel_button.setText("Cancelling…")
        self.cancel_button.setEnabled(False)
        self.result_detail.setText("Cancellation requested; waiting for bounded cleanup.")

    def _refresh_responses(self) -> None:
        artifact_dir = self._artifact_dir
        if artifact_dir is None:
            return
        responses = load_desktop_responses(artifact_dir)
        signature = tuple((item.identity, item.completed_at, len(item.text)) for item in responses)
        if signature == self._response_signature:
            return
        selected = self._selected_response_id()
        self._response_signature = signature
        self._responses = {item.identity: item for item in responses}
        self.response_list.blockSignals(True)
        self.response_list.clear()
        selected_row = -1
        for index, response in enumerate(responses):
            agent = _friendly(response.target_id)
            model = _friendly_model(self._options, response.runtime, response.model)
            phase = _friendly(response.phase)
            item = QListWidgetItem(f"{agent} · {model}\n{phase}")
            item.setData(Qt.ItemDataRole.UserRole, response.identity)
            item.setToolTip(response.path.name)
            self.response_list.addItem(item)
            if response.identity == selected:
                selected_row = index
        self.response_list.blockSignals(False)
        if responses:
            self.workspace_tabs.setTabText(2, f"Responses ({len(responses)})")
            self.response_list.setCurrentRow(selected_row if selected_row >= 0 else len(responses) - 1)
            if self.workspace_tabs.currentWidget() is self.prompt_tab:
                self.workspace_tabs.setCurrentWidget(self.responses_tab)
        else:
            self.workspace_tabs.setTabText(2, "Responses")

    def _selected_response_id(self) -> str:
        item = self.response_list.currentItem()
        return str(item.data(Qt.ItemDataRole.UserRole)) if item is not None else ""

    @Slot(object, object)
    def _response_selected(
        self, current: QListWidgetItem | None, _previous: QListWidgetItem | None
    ) -> None:
        if current is None:
            self.copy_response.setEnabled(False)
            self.open_response.setEnabled(False)
            return
        response = self._responses.get(str(current.data(Qt.ItemDataRole.UserRole)))
        if response is None:
            return
        agent = _friendly(response.target_id)
        self.response_heading.setText(f"{agent} · {_friendly(response.phase)}")
        if response.status == "failed":
            self.response_view.setPlainText(response.text)
        else:
            _set_markdown(self.response_view, response.text)
        self.copy_response.setEnabled(True)
        self.open_response.setEnabled(response.path.is_file())

    def _copy_current_response(self) -> None:
        response = self._responses.get(self._selected_response_id())
        if response is not None:
            QApplication.clipboard().setText(response.text)

    def _open_current_response(self) -> None:
        response = self._responses.get(self._selected_response_id())
        if response is not None:
            self._open_path_checked(response.path)

    def _load_summary(self) -> None:
        path = self._summary_path
        if path is None:
            return
        try:
            text, truncated = _text_preview(path, tail=False)
        except (OSError, ValueError) as exc:
            self.summary_view.setPlainText(str(exc))
            return
        if truncated:
            text += "\n\n_Preview truncated; open the artifact for the complete summary._"
        _set_markdown(self.summary_view, text)
        self.summary_button.setEnabled(True)

    def _refresh_artifacts(self) -> None:
        root = self._artifact_dir
        self.evidence_tree.clear()
        if root is None or not root.is_dir():
            return
        nodes: dict[Path, QTreeWidgetItem] = {}
        root_item = QTreeWidgetItem([root.name, ""])
        root_item.setData(0, Qt.ItemDataRole.UserRole, str(root))
        self.evidence_tree.addTopLevelItem(root_item)
        nodes[root] = root_item
        count = 0
        for path in sorted(root.rglob("*"), key=lambda item: (len(item.parts), str(item))):
            count += 1
            if count > 500:
                root_item.addChild(QTreeWidgetItem(["… additional artifacts omitted …", ""]))
                break
            parent = nodes.get(path.parent, root_item)
            size = ""
            if path.is_file():
                try:
                    size = _format_bytes(path.stat().st_size)
                except OSError:
                    size = ""
            item = QTreeWidgetItem([path.name, size])
            item.setData(0, Qt.ItemDataRole.UserRole, str(path))
            parent.addChild(item)
            if path.is_dir():
                nodes[path] = item
        root_item.setExpanded(True)
        self.artifacts_button.setEnabled(True)

    @Slot(QTreeWidgetItem, int)
    def _open_evidence_item(self, item: QTreeWidgetItem, _column: int) -> None:
        raw = item.data(0, Qt.ItemDataRole.UserRole)
        if isinstance(raw, str) and raw:
            self._open_path_checked(Path(raw))

    def _show_log(self) -> None:
        path = self._log_path
        if path is None:
            return
        try:
            text, truncated = _text_preview(path, tail=True)
        except (OSError, ValueError) as exc:
            self._show_start_error(str(exc))
            return
        _TextPreviewDialog(
            title="App log",
            path=path,
            text=text,
            truncated=truncated,
            parent=self,
        ).exec()

    def _open_named_path(self, name: str) -> None:
        path = self._artifact_dir if name == "artifacts" else self._worktree
        if path is not None:
            self._open_path_checked(path)

    def _open_path_checked(self, path: Path) -> None:
        try:
            resolved = path.resolve(strict=True)
            _open_path(resolved)
        except (OSError, ValueError) as exc:
            QMessageBox.warning(self, "Could not open path", str(exc))

    def _show_start_error(self, message: str) -> None:
        self.result_title.setText("Could not start")
        self.result_detail.setText(message)
        QMessageBox.warning(self, "Could not start Dialectic", message)

    def _restore_settings(self) -> None:
        geometry = self._settings.value("desktop/geometry")
        if geometry is not None:
            self.restoreGeometry(geometry)
        splitter = self._settings.value("desktop/mainSplitter")
        if splitter is not None:
            self.main_splitter.restoreState(splitter)
        repository = self._settings.value("desktop/repository", "")
        if isinstance(repository, str):
            self.repository.setText(repository)

    def _save_settings(self) -> None:
        self._settings.setValue("desktop/geometry", self.saveGeometry())
        self._settings.setValue("desktop/mainSplitter", self.main_splitter.saveState())
        self._settings.setValue("desktop/repository", self.repository.text())

    def closeEvent(self, event: QCloseEvent) -> None:
        if self._running and self._worker is not None:
            answer = QMessageBox.question(
                self,
                "Cancel the active run and close?",
                "The window will close after Dialectic persists cancellation and finishes bounded cleanup.",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            )
            if answer != QMessageBox.StandardButton.Yes:
                event.ignore()
                return
            self._close_after_run = True
            self._worker.request_cancel()
            self.cancel_button.setText("Cancelling…")
            self.cancel_button.setEnabled(False)
            event.ignore()
            return
        self._save_settings()
        event.accept()


class _TextPreviewDialog(QDialog):
    def __init__(
        self,
        *,
        title: str,
        path: Path,
        text: str,
        truncated: bool,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle(title)
        self.resize(900, 650)
        layout = QVBoxLayout(self)
        path_label = QLabel(str(path) + (" · preview truncated" if truncated else ""))
        path_label.setObjectName("Hint")
        path_label.setWordWrap(True)
        content = QPlainTextEdit()
        content.setReadOnly(True)
        content.setPlainText(text)
        close = QPushButton("Close")
        close.clicked.connect(self.accept)
        row = QHBoxLayout()
        row.addStretch(1)
        row.addWidget(close)
        layout.addWidget(path_label)
        layout.addWidget(content, 1)
        layout.addLayout(row)


def _default_drafts(options: dict[str, object]) -> dict[RunMode, dict[str, object]]:
    codex = _first_model(options, "codex")
    claude = _first_model(options, "claude-code")
    return {
        "code": {
            "main": {"runtime": "codex", "model": codex, "effort": "high"},
            "agents": [
                {"runtime": "@driver", "model": "", "effort": "", "lens": _FOCUSES[0]},
                {
                    "runtime": "claude-code",
                    "model": claude,
                    "effort": "",
                    "lens": _FOCUSES[1],
                },
            ],
            "prompt": "",
            "maxDissenters": 0,
            "moderatorMode": "fresh",
        },
        "council": {
            "main": {"runtime": "codex", "model": codex, "effort": "high"},
            "agents": [
                {"runtime": "codex", "model": codex, "effort": "", "lens": ""},
                {
                    "runtime": "claude-code",
                    "model": claude,
                    "effort": "",
                    "lens": "",
                },
            ],
            "prompt": "",
            "maxDissenters": 0,
            "moderatorMode": "fresh",
        },
    }


def _models_for(options: dict[str, object], runtime: str) -> list[dict[str, Any]]:
    models = options.get("models")
    if not isinstance(models, dict):
        return []
    choices = models.get(runtime)
    return [item for item in choices if isinstance(item, dict)] if isinstance(choices, list) else []


def _runtime_status(options: dict[str, object], runtime: str) -> bool:
    runtimes = options.get("runtimes")
    if not isinstance(runtimes, dict):
        return False
    detail = runtimes.get(runtime)
    return bool(detail.get("installed")) if isinstance(detail, dict) else False


def _first_model(options: dict[str, object], runtime: str) -> str:
    choices = _models_for(options, runtime)
    return str(choices[0].get("id", "")) if choices else ""


def _select_data(combo: QComboBox, value: str) -> None:
    index = combo.findData(value)
    combo.setCurrentIndex(index if index >= 0 else (0 if combo.count() else -1))


def _configure_combo(combo: QComboBox) -> None:
    combo.setMinimumWidth(0)
    combo.setMinimumContentsLength(6)
    combo.setSizeAdjustPolicy(
        QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon
    )
    combo.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Fixed)


def _friendly(value: str) -> str:
    return value.replace("_", " ").replace("-", " ").title()


def _friendly_model(options: dict[str, object], runtime: str, model: str) -> str:
    for item in _models_for(options, runtime):
        if item.get("id") == model:
            return str(item.get("name", model))
    return model or _RUNTIME_LABELS.get(runtime, runtime)


def _format_bytes(value: int) -> str:
    if value < 1_024:
        return f"{value} B"
    if value < 1_048_576:
        return f"{value / 1_024:.1f} KiB"
    return f"{value / 1_048_576:.1f} MiB"


def _markdown_browser() -> QTextBrowser:
    browser = QTextBrowser()
    browser.setOpenExternalLinks(False)
    browser.setOpenLinks(False)
    browser.document().setDefaultStyleSheet(
        "body { color: #e8edf2; } "
        "h1, h2, h3 { color: #f3f5f8; } "
        "a { color: #59d0ac; } "
        "code, pre { color: #dce7e2; background-color: #10141a; } "
        "table { border-collapse: collapse; } "
        "th, td { border: 1px solid #39414d; padding: 5px; }"
    )
    browser.anchorClicked.connect(lambda url: _confirm_link(browser, url))
    return browser


def _set_markdown(browser: QTextBrowser, markdown: str) -> None:
    browser.document().setMarkdown(markdown, _MARKDOWN_FEATURES)


def _confirm_link(parent: QWidget, url: QUrl) -> None:
    answer = QMessageBox.question(
        parent,
        "Open link?",
        f"Open this model-authored link with the system handler?\n\n{url.toString()}",
        QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        QMessageBox.StandardButton.No,
    )
    if answer == QMessageBox.StandardButton.Yes:
        QDesktopServices.openUrl(url)


_STYLE = """
QWidget#Root, QMainWindow { background: #0c0e12; color: #f3f5f8; }
QWidget { color: #f3f5f8; font-family: "Segoe UI"; font-size: 13px; }
QFrame#Header { background: #0c0e12; border-bottom: 1px solid #2a303b; }
QLabel#Brand { font-size: 18px; font-weight: 700; }
QLabel#Muted, QLabel#Hint, QLabel#FieldLabel { color: #929baa; }
QLabel#FieldLabel { font-size: 11px; }
QLabel#SectionTitle, QLabel#ResultTitle { font-weight: 650; }
QLabel#HeaderStatus { color: #929baa; }
QScrollArea#SetupScroll, QWidget#SetupContent, QWidget#Workspace { background: #0c0e12; border: 0; }
QFrame#Panel, QFrame#PhaseStrip, QFrame#ResultBar {
    background: #14171d; border: 1px solid #2a303b; border-radius: 10px;
}
QFrame#AgentRow { background: #1b1f27; border: 1px solid #2a303b; border-radius: 8px; }
QFrame#ResponseToolbar { background: #14171d; border-bottom: 1px solid #2a303b; }
QFrame#Footer { background: #0f1116; border-top: 1px solid #2a303b; }
QLineEdit, QComboBox, QSpinBox, QPlainTextEdit, QTextBrowser, QListWidget, QTreeWidget {
    background: #1c2027; color: #f3f5f8; border: 1px solid #2a303b; border-radius: 7px;
    padding: 7px;
}
QLineEdit:focus, QComboBox:focus, QSpinBox:focus, QPlainTextEdit:focus,
QTextBrowser:focus, QListWidget:focus, QTreeWidget:focus { border-color: #4e7b70; }
QComboBox::drop-down { border: 0; width: 20px; }
QComboBox QAbstractItemView { background: #1c2027; color: #f3f5f8; selection-background-color: #303640; }
QPushButton { background: #232832; color: #dbe0e8; border: 1px solid #2a303b; border-radius: 7px; padding: 7px 10px; }
QPushButton:hover { background: #2b313d; }
QPushButton:disabled { color: #657080; background: #1a1d23; }
QPushButton#ModeButton { background: #1b1f27; color: #929baa; padding: 9px; }
QPushButton#ModeButton:checked { background: #303640; color: #f3f5f8; }
QPushButton#Primary { background: #59d0ac; color: #07120f; border: 0; font-weight: 700; padding: 10px 19px; }
QPushButton#Primary:hover { background: #6dd9b8; }
QPushButton#Danger { color: #ff9e9e; }
QPushButton#Remove { background: transparent; border: 0; color: #929baa; font-size: 17px; padding: 2px; }
QPushButton#Remove:hover { color: #ff8f8f; }
QTabWidget::pane { background: #14171d; border: 1px solid #2a303b; border-radius: 8px; top: -1px; }
QTabBar::tab { background: #11141a; color: #929baa; border: 1px solid #2a303b; padding: 8px 14px; }
QTabBar::tab:selected { background: #303640; color: #f3f5f8; }
QListWidget::item { border-bottom: 1px solid #2a303b; padding: 9px; }
QListWidget::item:selected { background: #303640; color: #f3f5f8; }
QTreeWidget::item:selected { background: #303640; }
QLabel[phaseState="idle"] { color: #707988; background: #1b1f27; border-radius: 6px; padding: 7px; }
QLabel[phaseState="current"] { color: #f3f5f8; background: #303640; border: 1px solid #4e7b70; border-radius: 6px; padding: 7px; }
QLabel[phaseState="complete"] { color: #07120f; background: #59d0ac; border-radius: 6px; padding: 7px; font-weight: 650; }
QLabel[error="true"] { color: #ff8f8f; }
QSplitter::handle { background: #202630; }
QScrollBar:vertical { background: #11141a; width: 11px; }
QScrollBar::handle:vertical { background: #3a424f; border-radius: 5px; min-height: 28px; }
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { height: 0; }
"""


def run_desktop(log_path: Path | None = None) -> None:
    app = QApplication.instance() or QApplication(sys.argv)
    app.setApplicationName("Dialectic")
    app.setOrganizationName("OpenAI")
    app.setStyle("Fusion")
    app.setStyleSheet(_STYLE)
    window = MainWindow(log_path=log_path)
    window.show()
    raise SystemExit(app.exec())
