from __future__ import annotations

import http.cookiejar
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
    _launch_chromium_app,
    _model_options,
    _prepare_run,
    _ui_html,
)


def test_desktop_ui_contains_the_primary_workflow_controls() -> None:
    html = _ui_html().decode("utf-8")
    required = (
        "Code Once",
        "Council Once",
        "Repository",
        "Browse",
        "Main model",
        "mainModel",
        "Review models",
        "Council participants",
        "Allowed dissenters",
        "Run Code Once",
        "App log",
    )
    assert all(item in html for item in required)
    assert "Model ID" not in html


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
    assert ConfigLoader({}).load(prepared.config_bytes, mode="code").config.driver is not None


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
        }
    )

    assert prepared.repository is None
    assert ConfigLoader({}).load(prepared.config_bytes, mode="council").config.council is not None


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


def test_desktop_server_requires_its_session_cookie_and_same_origin() -> None:
    token = "test-session-token"
    server = _UiServer(("127.0.0.1", 0), token, _UiState())
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
