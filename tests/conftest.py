from __future__ import annotations

import copy
from collections.abc import Callable
from pathlib import Path

import pytest
import yaml

from dialectic.store import RunStore


@pytest.fixture
def limits() -> dict[str, int]:
    return {
        "max_reviewers": 5,
        "max_findings_per_reviewer": 20,
        "max_total_findings": 50,
        "max_council_participants": 5,
        "max_propositions": 8,
        "max_config_bytes": 65_536,
        "max_input_bytes": 65_536,
        "max_diff_bytes": 262_144,
        "max_changed_paths": 1_000,
        "max_changed_regular_file_bytes": 8_388_608,
        "max_candidate_change_bytes": 33_554_432,
        "max_web_sources_per_turn": 20,
        "max_packet_bytes": 393_216,
        "max_lens_chars": 4_096,
        "max_model_field_chars": 32_768,
        "max_model_list_items": 100,
        "max_agent_stdout_bytes": 8_388_608,
        "max_agent_stderr_bytes": 2_097_152,
        "max_turn_scratch_bytes": 67_108_864,
        "max_turn_scratch_entries": 10_000,
        "max_turn_scratch_depth": 64,
        "preflight_seconds": 30,
        "capability_probe_seconds": 120,
        "agent_turn_seconds": 300,
        "code_run_seconds": 1_200,
        "council_run_seconds": 1_200,
        "graceful_kill_seconds": 5,
        "turn_cleanup_seconds": 30,
        "code_review_cycles": 1,
        "council_discussion_rounds": 1,
    }


@pytest.fixture
def config_data(limits: dict[str, int]) -> dict[str, object]:
    return {
        "version": 1,
        "driver": {"runtime": "codex", "model": "codex-model", "effort": "high"},
        "reviewers": [
            {"id": "self-review", "target": "@driver", "lens": "correctness"}
        ],
        "council": {
            "participants": [
                {"id": "codex", "runtime": "codex", "model": "codex-model"},
                {
                    "id": "claude",
                    "runtime": "claude-code",
                    "model": "claude-model",
                },
            ],
            "moderator": {"runtime": "codex", "model": "codex-model"},
            "consensus": {"max_dissenters": 1},
        },
        "limits": copy.deepcopy(limits),
    }


@pytest.fixture
def config_bytes(config_data: dict[str, object]) -> bytes:
    return yaml.safe_dump(config_data, sort_keys=False).encode("utf-8")


@pytest.fixture
def store_factory(tmp_path: Path) -> Callable[..., RunStore]:
    counter = 0

    def make(**kwargs: object) -> RunStore:
        nonlocal counter
        counter += 1
        run_id = f"20260828T0000{counter:02d}Z-aaaaaaaaaa"
        return RunStore(
            tmp_path / f"state-{counter}",
            run_id_factory=lambda: run_id,
            **kwargs,
        )

    return make
