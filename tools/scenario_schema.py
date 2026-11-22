from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from tools.scenario import (
    IMPACT_CLASSES,
    ORDINAL_ROLES,
    REQUIRED_FIELDS,
    RISK_LEVELS,
    SELECTOR_KINDS,
    Scenario,
    ValidationReport,
)


def scenario_dir(root: str | Path) -> Path:
    return Path(root) / "scenarios"


def scenario_files(root: str | Path) -> list[Path]:
    return sorted(scenario_dir(root).glob("*.json"))


def scenario_ids(root: str | Path) -> list[str]:
    return [path.stem for path in scenario_files(root)]


def load_scenario(root: str | Path, scenario_id: str) -> dict[str, Any]:
    path = scenario_dir(root) / f"{scenario_id}.json"
    with path.open(encoding="utf-8") as handle:
        return json.load(handle)


def load_scenario_object(root: str | Path, scenario_id: str) -> Scenario:
    path = scenario_dir(root) / f"{scenario_id}.json"
    scenario = Scenario.from_file(path, expected_id=scenario_id)
    errors = scenario.validate(root, expected_id=scenario_id)
    if not errors.ok:
        raise ValueError("; ".join(errors.to_list()))
    return scenario


def validate_scenario(
    scenario: dict[str, Any],
    root: str | Path | None = None,
    expected_id: str | None = None,
) -> list[str]:
    return Scenario.from_dict(scenario).validate(root, expected_id).to_list()


def _selector_kind(value: object) -> str:
    return Scenario({}).selector_kind(value)


def _command_path(command: str) -> str:
    return Scenario({}).command_path(command)


__all__ = [
    "IMPACT_CLASSES",
    "ORDINAL_ROLES",
    "REQUIRED_FIELDS",
    "RISK_LEVELS",
    "SELECTOR_KINDS",
    "Scenario",
    "ValidationReport",
    "scenario_dir",
    "scenario_files",
    "scenario_ids",
    "load_scenario",
    "load_scenario_object",
    "validate_scenario",
]
