"""Pure configuration construction for the desktop ingress."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, TypeAlias

import yaml

from .config import ConfigLoader
from .contracts import RunMode

RuntimeName: TypeAlias = Literal["codex", "claude-code", "grok-build"]
ReviewRuntime: TypeAlias = RuntimeName | Literal["@driver"]

SUPPORTED_EFFORTS: dict[RuntimeName, tuple[str, ...]] = {
    "codex": ("", "low", "medium", "high", "xhigh", "max", "ultra"),
    "claude-code": ("", "low", "medium", "high", "xhigh", "max"),
    "grok-build": ("", "low", "medium", "high", "xhigh"),
}

# The UI emits the documented v0.1 defaults. Safety limits remain controller-owned
# and deliberately are not user-editable in the simple desktop surface.
DEFAULT_LIMITS: dict[str, int] = {
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


@dataclass(frozen=True, slots=True)
class UiAgentChoice:
    runtime: ReviewRuntime
    model: str = ""
    effort: str = ""
    lens: str = "general-correctness"


@dataclass(frozen=True, slots=True)
class UiRunConfig:
    mode: RunMode
    main_runtime: RuntimeName
    main_model: str
    main_effort: str
    agents: tuple[UiAgentChoice, ...]
    max_dissenters: int = 0


def build_config_bytes(request: UiRunConfig) -> bytes:
    """Build and validate the exact YAML submitted to ``DialecticService``."""

    if request.main_runtime not in SUPPORTED_EFFORTS:
        raise ValueError(f"Unsupported main runtime {request.main_runtime!r}")
    main_model = _required(request.main_model, "main model")
    main_effort = request.main_effort.strip()
    _validate_effort(request.main_runtime, main_effort)

    data: dict[str, object] = {
        "version": 1,
        "limits": dict(DEFAULT_LIMITS),
    }
    main_target: dict[str, str] = {
        "runtime": request.main_runtime,
        "model": main_model,
    }
    if main_effort:
        main_target["effort"] = main_effort

    if request.mode == "code":
        if request.main_runtime != "codex":
            raise ValueError("Code mode requires Codex as the main driver")
        if not 1 <= len(request.agents) <= 5:
            raise ValueError("Code mode requires between 1 and 5 reviewers")
        data["driver"] = main_target
        data["reviewers"] = [
            _reviewer_payload(choice, index)
            for index, choice in enumerate(request.agents)
        ]
    else:
        if not 2 <= len(request.agents) <= 5:
            raise ValueError("Council mode requires between 2 and 5 participants")
        if not 0 <= request.max_dissenters < len(request.agents):
            raise ValueError(
                "Allowed dissenters must be at least zero and less than the "
                "participant count"
            )
        data["council"] = {
            "participants": [
                _participant_payload(choice, index)
                for index, choice in enumerate(request.agents)
            ],
            "moderator": main_target,
            "consensus": {"max_dissenters": request.max_dissenters},
        }

    raw = yaml.safe_dump(data, sort_keys=False, allow_unicode=True).encode("utf-8")
    ConfigLoader(environment={}).load(raw, mode=request.mode)
    return raw


def _reviewer_payload(choice: UiAgentChoice, index: int) -> dict[str, str]:
    _validate_choice_runtime(choice.runtime)
    lens = _required(choice.lens, f"reviewer {index + 1} focus")
    payload = {"id": f"reviewer-{chr(ord('a') + index)}", "lens": lens}
    if choice.runtime == "@driver":
        payload["target"] = "@driver"
        return payload
    payload.update(_concrete_target(choice, index, "reviewer"))
    return payload


def _participant_payload(choice: UiAgentChoice, index: int) -> dict[str, str]:
    _validate_choice_runtime(choice.runtime)
    if choice.runtime == "@driver":
        raise ValueError("Council participants must select a concrete runtime and model")
    payload = {"id": f"participant-{chr(ord('a') + index)}"}
    payload.update(_concrete_target(choice, index, "participant"))
    return payload


def _concrete_target(
    choice: UiAgentChoice,
    index: int,
    role: str,
) -> dict[str, str]:
    model = _required(choice.model, f"{role} {index + 1} model")
    effort = choice.effort.strip()
    _validate_effort(choice.runtime, effort)
    payload = {"runtime": choice.runtime, "model": model}
    if effort:
        payload["effort"] = effort
    return payload


def _validate_effort(runtime: RuntimeName, effort: str) -> None:
    if effort not in SUPPORTED_EFFORTS[runtime]:
        shown = effort or "<default>"
        raise ValueError(f"Unsupported effort {shown!r} for {runtime}")


def _validate_choice_runtime(runtime: ReviewRuntime) -> None:
    if runtime != "@driver" and runtime not in SUPPORTED_EFFORTS:
        raise ValueError(f"Unsupported agent runtime {runtime!r}")


def _required(value: str, label: str) -> str:
    normalized = value.strip()
    if not normalized:
        raise ValueError(f"{label.capitalize()} is required")
    return normalized
