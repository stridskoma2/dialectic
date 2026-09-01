from __future__ import annotations

import http.cookiejar
import json
import os
import subprocess
import threading
import urllib.error
import urllib.request
from pathlib import Path

import pytest

from dialectic.config import ConfigLoader
from dialectic.ui import (
    _UiServer,
    _UiState,
    _choose_repository,
    _launch_chromium_app,
    _model_options,
    _prepare_run,
    _response_excerpts,
    _source_excerpts,
    _resolve_repository_path,
    _summary_brief,
    _shutdown_windows_bridge,
    _ui_html,
    _windows_bridge_config,
)
from dialectic.windows_bridge import _run_bridge


def test_desktop_ui_contains_the_primary_workflow_controls(tmp_path: Path) -> None:
    html = _ui_html().decode("utf-8")
    required = (
        "Code Once",
        "Council Once",
        "Repository",
        "Browse",
        "Main model",
        "Moderator",
        "Moderator behavior",
        "Fresh synthesis only",
        "Independent opening + synthesis",
        "mainModel",
        "Review models",
        "Council participants",
        "Allowed dissenters",
        "Run Code Once",
        "App log",
        "Model responses",
        "Research access",
        "Live web",
        "Web sources",
        "data-model-link",
        "window.confirm",
        'researchMode:"live-web"',
        "outcomePanel",
        "Outcome / summary",
        "summaryBrief",
        "previewDialog",
        "/api/content",
        "contextmenu",
    )
    assert all(item in html for item in required)
    assert "Model ID" not in html

    attempt = tmp_path / "turns/participant/participant-a/opening.attempt.json"
    attempt.parent.mkdir(parents=True)
    attempt.write_text(
        json.dumps(
            {
                "role": "participant",
                "target_id": "participant-a",
                "turn_phase": "opening",
                "response_completed_at": "2026-09-01T01:02:03Z",
                "response": {
                    "runtime": "codex",
                    "requested_model": "gpt-5.6-sol",
                    "text": "First line\n\nSecond line " + "x" * 400,
                },
                "bounded_diagnostic": None,
            }
        ),
        encoding="utf-8",
    )

    responses = _response_excerpts(tmp_path)

    assert responses[0] | {"excerpt": ""} == {
        "role": "participant",
        "targetId": "participant-a",
        "phase": "opening",
        "runtime": "codex",
        "model": "gpt-5.6-sol",
        "excerpt": "",
        "status": "response",
        "completedAt": "2026-09-01T01:02:03Z",
    }
    assert responses[0]["excerpt"].startswith("First line Second line")
    assert len(responses[0]["excerpt"]) == 280
    assert responses[0]["excerpt"].endswith("…")

    summary = tmp_path / "summary.md"
    summary.write_text(
        "# Dialectic run test\n\nStatus: FINALIZED\nOutcome: UNANIMOUS\n\n"
        "## Council answer\n\nUse the bounded final answer.\n\n## Vote matrix\n\nignored\n",
        encoding="utf-8",
    )
    assert _summary_brief(summary) == "Use the bounded final answer."
    summary.write_text(
        "# Dialectic run test\n\nStatus: FINALIZED\n"
        "Outcome: COMPLETED_NO_FINDINGS\n\n"
        "Repair turn: not performed.\nRe-review: not applicable.\n",
        encoding="utf-8",
    )
    assert _summary_brief(summary) == (
        "Repair turn: not performed.\nRe-review: not applicable."
    )

    source = tmp_path / "research/sources/participant/participant-a/opening.json"
    source.parent.mkdir(parents=True)
    source.write_text(
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
                        "claim_context": "Current example evidence.",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    assert _source_excerpts(tmp_path) == [
        {
            "role": "participant",
            "targetId": "participant-a",
            "phase": "opening",
            "title": "Example Domain",
            "url": "https://example.com/",
            "claimContext": "Current example evidence.",
            "capturedAt": "2026-09-01T01:02:03Z",
        }
    ]


def test_desktop_model_options_use_friendly_names_and_report_installed_clis(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("CODEX_DRIVER_MODEL", "organization-codex-model")
    monkeypatch.setattr(
        "dialectic.ui.shutil.which",
        lambda name: f"C:/bin/{name}.exe" if name in {"codex", "claude"} else None,
    )

    options = _model_options()

    assert options["models"]["codex"][:2] == [
        {
            "id": "organization-codex-model",
            "name": "Configured model (organization-codex-model)",
            "description": "Provided by the local environment",
            "source": "environment",
        },
        {
            "id": "gpt-5.6-sol",
            "name": "GPT-5.6 Sol",
            "description": "Flagship capability for complex work",
            "source": "catalog",
        },
    ]
    assert options["models"]["claude-code"][0]["name"] == "Claude Opus 5"
    assert options["models"]["grok-build"][0]["name"] == "Grok 4.6"
    assert options["runtimes"] == {
        "codex": {"installed": True, "executable": "codex"},
        "claude-code": {"installed": True, "executable": "claude"},
        "grok-build": {"installed": False, "executable": "grok"},
    }


def test_desktop_ui_launches_chromium_in_a_dedicated_app_window(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[list[str], dict[str, object]]] = []

    def fake_popen(argv: list[str], **kwargs: object) -> object:
        calls.append((argv, kwargs))
        return object()

    monkeypatch.setattr("dialectic.ui.subprocess.Popen", fake_popen)
    url = "http://127.0.0.1:12345/?token=test"
    executable = Path("browser-executable")

    assert _launch_chromium_app(executable, url)
    assert calls == [
        (
            [
                os.fspath(executable),
                "--new-window",
                f"--app={url}",
                "--no-first-run",
            ],
            {
                "stdin": subprocess.DEVNULL,
                "stdout": subprocess.DEVNULL,
                "stderr": subprocess.DEVNULL,
                "close_fds": True,
                "creationflags": getattr(subprocess, "CREATE_NO_WINDOW", 0),
            },
        )
    ]


def test_desktop_run_request_uses_the_selected_repository(tmp_path: Path) -> None:
    repository = tmp_path / "repo"
    repository.mkdir()
    prepared = _prepare_run(
        {
            "mode": "code",
            "prompt": "Implement the focused change.",
            "repository": str(repository),
            "main": {"runtime": "codex", "model": "codex-main", "effort": "high"},
            "agents": [
                {"runtime": "@driver", "model": "", "effort": "", "lens": "correctness"},
                {
                    "runtime": "claude-code",
                    "model": "claude-review",
                    "effort": "medium",
                    "lens": "tests-and-edge-cases",
                },
            ],
            "maxDissenters": 0,
        }
    )

    assert prepared.repository == repository.resolve()
    assert prepared.prompt_bytes == b"Implement the focused change."
    config = ConfigLoader({}).load(prepared.config_bytes, mode="code").config
    assert config.driver is not None
    assert config.research_mode == "offline"


def test_wsl_translates_a_windows_repository_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repository = tmp_path / "repo"
    repository.mkdir()
    calls: list[list[str]] = []

    def fake_run(argv: list[str], **_kwargs: object) -> subprocess.CompletedProcess[bytes]:
        calls.append(argv)
        return subprocess.CompletedProcess(argv, 0, f"{repository}\n".encode(), b"")

    monkeypatch.setattr("dialectic.ui._is_wsl", lambda: True)
    monkeypatch.setattr("dialectic.ui.subprocess.run", fake_run)

    assert _resolve_repository_path(r"C:\git\repo") == repository.resolve()
    assert calls == [["wslpath", "-a", "-u", r"C:\git\repo"]]


def test_wsl_exposes_the_authenticated_windows_bridge(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    bridge_directory = tmp_path / "bridge"
    bridge_directory.mkdir()
    monkeypatch.setattr("dialectic.ui._is_wsl", lambda: True)
    monkeypatch.setenv("DIALECTIC_WINDOWS_BRIDGE_DIR", str(bridge_directory))
    monkeypatch.setenv("DIALECTIC_WINDOWS_BRIDGE_TOKEN", "a" * 32)

    assert _windows_bridge_config() == (bridge_directory.resolve(), "a" * 32)


def test_wsl_rejects_a_missing_windows_bridge_directory(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("dialectic.ui._is_wsl", lambda: True)
    monkeypatch.setenv("DIALECTIC_WINDOWS_BRIDGE_DIR", "/missing/bridge")
    monkeypatch.setenv("DIALECTIC_WINDOWS_BRIDGE_TOKEN", "a" * 32)

    assert _windows_bridge_config() is None


def test_windows_bridge_returns_the_selected_path_and_shuts_down(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    token = "test-bridge-token-1234567890"
    bridge_directory = tmp_path / "bridge"
    bridge_directory.mkdir()
    monkeypatch.setattr("dialectic.ui._is_wsl", lambda: True)
    monkeypatch.setenv("DIALECTIC_WINDOWS_BRIDGE_DIR", str(bridge_directory))
    monkeypatch.setenv("DIALECTIC_WINDOWS_BRIDGE_TOKEN", token)
    worker = threading.Thread(
        target=_run_bridge,
        args=(bridge_directory, token, lambda: r"C:\git\repo"),
        kwargs={"poll_seconds": 0.001},
        daemon=True,
    )
    worker.start()
    for _attempt in range(100):
        if (bridge_directory / ".ready").is_file():
            break
        threading.Event().wait(0.01)

    unauthorized_id = "0" * 32
    unauthorized = bridge_directory / f"request-{unauthorized_id}.json"
    unauthorized.write_text('{"token":"wrong","action":"browse"}', encoding="utf-8")
    for _attempt in range(100):
        if not unauthorized.exists():
            break
        threading.Event().wait(0.01)
    assert not (bridge_directory / f"response-{unauthorized_id}.json").exists()

    assert _choose_repository() == r"C:\git\repo"
    _shutdown_windows_bridge()
    worker.join(timeout=5)
    assert not worker.is_alive()
    assert not bridge_directory.exists()


def test_desktop_council_request_does_not_disclose_repository() -> None:
    prepared = _prepare_run(
        {
            "mode": "council",
            "prompt": "Reach a bounded decision.",
            "repository": "C:/must/not/be/used",
            "main": {"runtime": "codex", "model": "codex-moderator", "effort": "high"},
            "agents": [
                {"runtime": "codex", "model": "codex-a", "effort": "", "lens": ""},
                {
                    "runtime": "claude-code",
                    "model": "claude-b",
                    "effort": "",
                    "lens": "",
                },
            ],
            "maxDissenters": 0,
            "moderatorMode": "independent-opening",
        }
    )

    assert prepared.repository is None
    council = ConfigLoader({}).load(
        prepared.config_bytes, mode="council"
    ).config.council
    assert council is not None
    assert council.moderator_mode == "independent-opening"


@pytest.mark.parametrize(
    ("payload", "message"),
    [
        ({}, "Mode must be text"),
        (
            {
                "mode": "code",
                "prompt": "x",
                "repository": "missing",
                "main": {"runtime": "codex", "model": "m"},
                "agents": [{"runtime": "@driver"}],
            },
            "existing directory",
        ),
        (
            {
                "mode": "council",
                "prompt": "x",
                "main": {"runtime": "codex", "model": "m"},
                "agents": [
                    {"runtime": "unknown", "model": "x"},
                    {"runtime": "codex", "model": "y"},
                ],
            },
            "Unsupported agent runtime",
        ),
    ],
)
def test_desktop_run_request_rejects_invalid_input(payload: object, message: str) -> None:
    with pytest.raises(ValueError, match=message):
        _prepare_run(payload)


def test_desktop_server_requires_its_session_cookie_and_same_origin(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    token = "test-session-token"
    log_path = tmp_path / "app.jsonl"
    log_path.write_text('{"event":"application.started"}\n', encoding="utf-8")
    opened: list[Path] = []
    monkeypatch.setattr("dialectic.ui._open_path", opened.append)
    server = _UiServer(("127.0.0.1", 0), token, _UiState(log_path))
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    try:
        with pytest.raises(urllib.error.HTTPError) as denied:
            urllib.request.urlopen(f"{server.origin}/api/status", timeout=5)
        assert denied.value.code == 403

        cookies = http.cookiejar.CookieJar()
        opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(cookies))
        with opener.open(f"{server.origin}/?token={token}", timeout=5) as response:
            assert b"Dialectic" in response.read()
        with opener.open(f"{server.origin}/api/status", timeout=5) as response:
            assert response.status == 200
            assert json.load(response)["summaryBrief"] == ""

        preview_request = urllib.request.Request(
            f"{server.origin}/api/content",
            data=b'{"target":"log"}',
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with opener.open(preview_request, timeout=5) as response:
            preview = json.load(response)
        assert preview == {
            "title": "App log",
            "path": str(log_path.resolve()),
            "content": log_path.read_bytes().decode("utf-8"),
            "truncated": False,
        }

        external_request = urllib.request.Request(
            f"{server.origin}/api/open",
            data=b'{"target":"log"}',
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with opener.open(external_request, timeout=5) as response:
            assert json.load(response) == {"ok": True}
        assert opened == [log_path.resolve()]

        request = urllib.request.Request(
            f"{server.origin}/api/run",
            data=b"{}",
            headers={"Content-Type": "application/json", "Origin": "https://example.invalid"},
            method="POST",
        )
        with pytest.raises(urllib.error.HTTPError) as cross_origin:
            opener.open(request, timeout=5)
        assert cross_origin.value.code == 403
    finally:
        server.shutdown()
        server.server_close()
        worker.join(timeout=5)
