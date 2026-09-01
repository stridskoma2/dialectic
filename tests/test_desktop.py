from __future__ import annotations

import json
from pathlib import Path

from dialectic.desktop import load_desktop_responses, load_desktop_web_sources


def test_native_desktop_loads_complete_persisted_responses(tmp_path: Path) -> None:
    opening = tmp_path / "turns/participant/participant-a/opening.attempt.json"
    opening.parent.mkdir(parents=True)
    opening.write_text(
        json.dumps(
            {
                "role": "participant",
                "target_id": "participant-a",
                "turn_phase": "opening",
                "started_at": "2026-09-01T01:01:00Z",
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
    assert responses[0].duration_seconds == 63.0
    assert responses[1].status == "failed"
    assert responses[1].text == "native process exited before a response"
    assert responses[0].path == opening.resolve()


def test_native_desktop_response_projection_is_empty_without_turns(tmp_path: Path) -> None:
    assert load_desktop_responses(tmp_path) == ()


def test_native_desktop_loads_bounded_web_source_projection(tmp_path: Path) -> None:
    path = tmp_path / "research/sources/moderator/moderator/moderation.json"
    path.parent.mkdir(parents=True)
    path.write_text(
        json.dumps(
            {
                "role": "moderator",
                "target_id": "moderator",
                "turn_phase": "moderation",
                "captured_at": "2026-09-01T01:04:03Z",
                "sources": [
                    {
                        "title": "Example Domain",
                        "url": "https://example.com/",
                        "claim_context": "Used for the current-product comparison.",
                    },
                    {
                        "title": "Unsafe",
                        "url": "http://example.com/",
                        "claim_context": "Ignored by the presentation boundary.",
                    },
                ],
            }
        ),
        encoding="utf-8",
    )

    sources = load_desktop_web_sources(tmp_path)

    assert len(sources) == 1
    assert sources[0].target_id == "moderator"
    assert sources[0].title == "Example Domain"
    assert sources[0].url == "https://example.com/"
    assert sources[0].path == path.resolve()
