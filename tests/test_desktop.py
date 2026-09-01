from __future__ import annotations

import json
from pathlib import Path

from dialectic.desktop import load_desktop_responses


def test_native_desktop_loads_complete_persisted_responses(tmp_path: Path) -> None:
    opening = tmp_path / "turns/participant/participant-a/opening.attempt.json"
    opening.parent.mkdir(parents=True)
    opening.write_text(
        json.dumps(
            {
                "role": "participant",
                "target_id": "participant-a",
                "turn_phase": "opening",
                "response_completed_at": "2026-09-01T01:02:03Z",
                "response": {
                    "runtime": "codex",
                    "requested_model": "gpt-5.6-sol",
                    "text": "# Opening\n\nComplete **Markdown** response.",
                },
            }
        ),
        encoding="utf-8",
    )
    failed = tmp_path / "turns/reviewer/reviewer-b/review.attempt.json"
    failed.parent.mkdir(parents=True)
    failed.write_text(
        json.dumps(
            {
                "role": "reviewer",
                "target_id": "reviewer-b",
                "turn_phase": "review",
                "capture_completed_at": "2026-09-01T01:03:03Z",
                "response": None,
                "bounded_diagnostic": "native process exited before a response",
            }
        ),
        encoding="utf-8",
    )
    malformed = tmp_path / "turns/reviewer/reviewer-c/review.attempt.json"
    malformed.parent.mkdir(parents=True)
    malformed.write_text("not json", encoding="utf-8")

    responses = load_desktop_responses(tmp_path)

    assert [item.target_id for item in responses] == ["participant-a", "reviewer-b"]
    assert responses[0].text == "# Opening\n\nComplete **Markdown** response."
    assert responses[0].status == "response"
    assert responses[1].status == "failed"
    assert responses[1].text == "native process exited before a response"
    assert responses[0].path == opening.resolve()


def test_native_desktop_response_projection_is_empty_without_turns(tmp_path: Path) -> None:
    assert load_desktop_responses(tmp_path) == ()
