from __future__ import annotations

import yaml
import pytest

from dialectic.config import ConfigLoader
from dialectic.ui_config import UiAgentChoice, UiRunConfig, build_config_bytes


def test_desktop_code_config_maps_main_and_review_models() -> None:
    raw = build_config_bytes(
        UiRunConfig(
            mode="code",
            main_runtime="codex",
            main_model="codex-main",
            main_effort="high",
            agents=(
                UiAgentChoice(runtime="@driver", lens="general-correctness"),
                UiAgentChoice(
                    runtime="claude-code",
                    model="claude-review",
                    effort="medium",
                    lens="tests-and-edge-cases",
                ),
            ),
        )
    )

    parsed = yaml.safe_load(raw)
    assert parsed["driver"] == {
        "runtime": "codex",
        "model": "codex-main",
        "effort": "high",
    }
    assert parsed["reviewers"] == [
        {
            "id": "reviewer-a",
            "lens": "general-correctness",
            "target": "@driver",
        },
        {
            "id": "reviewer-b",
            "lens": "tests-and-edge-cases",
            "runtime": "claude-code",
            "model": "claude-review",
            "effort": "medium",
        },
    ]
    assert "council" not in parsed
    assert ConfigLoader({}).load(raw, mode="code").config.driver is not None


def test_desktop_council_config_maps_moderator_participants_and_consensus() -> None:
    raw = build_config_bytes(
        UiRunConfig(
            mode="council",
            main_runtime="codex",
            main_model="codex-moderator",
            main_effort="xhigh",
            agents=(
                UiAgentChoice(runtime="codex", model="codex-participant"),
                UiAgentChoice(runtime="claude-code", model="claude-participant"),
                UiAgentChoice(runtime="grok-build", model="grok-participant"),
            ),
            max_dissenters=1,
        )
    )

    parsed = yaml.safe_load(raw)
    assert parsed["council"]["moderator"] == {
        "runtime": "codex",
        "model": "codex-moderator",
        "effort": "xhigh",
    }
    assert [item["id"] for item in parsed["council"]["participants"]] == [
        "participant-a",
        "participant-b",
        "participant-c",
    ]
    assert parsed["council"]["consensus"] == {"max_dissenters": 1}
    assert "driver" not in parsed
    assert ConfigLoader({}).load(raw, mode="council").config.council is not None


@pytest.mark.parametrize(
    ("run_config", "message"),
    [
        (
            UiRunConfig(
                mode="code",
                main_runtime="claude-code",
                main_model="claude-main",
                main_effort="",
                agents=(UiAgentChoice(runtime="@driver"),),
            ),
            "requires Codex",
        ),
        (
            UiRunConfig(
                mode="council",
                main_runtime="codex",
                main_model="codex-main",
                main_effort="",
                agents=(UiAgentChoice(runtime="codex", model="codex-only"),),
            ),
            "between 2 and 5",
        ),
        (
            UiRunConfig(
                mode="council",
                main_runtime="codex",
                main_model="codex-main",
                main_effort="",
                agents=(
                    UiAgentChoice(runtime="codex", model="codex-a"),
                    UiAgentChoice(runtime="claude-code", model="claude-b"),
                ),
                max_dissenters=2,
            ),
            "less than the participant count",
        ),
    ],
)
def test_desktop_config_rejects_invalid_role_shapes(
    run_config: UiRunConfig,
    message: str,
) -> None:
    with pytest.raises(ValueError, match=message):
        build_config_bytes(run_config)
