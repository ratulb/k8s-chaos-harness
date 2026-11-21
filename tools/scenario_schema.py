from __future__ import annotations

import json
import re
import shlex
from pathlib import Path
from typing import Any

from tools.schemas import REPAIR_TOOLS, ROOT_CAUSE_LABELS

RISK_LEVELS = {"read-only", "disruptive", "destructive"}
IMPACT_CLASSES = {"control", "single_member_fault", "quorum_loss", "total_outage"}
SELECTOR_KINDS = {"role", "index", "node", "quorum_losing_set"}
ORDINAL_ROLES = {
    "first", "second", "third", "fourth", "fifth", "sixth",
    "seventh", "eighth", "ninth", "tenth",
}

REQUIRED_FIELDS = {
    "id": str,
    "root_cause": str,
    "acceptable_repairs": list,
    "forbidden_repairs": list,
    "inject": str,
    "verify": str,
    "timeout_seconds": int,
    "max_steps": int,
    "max_tokens": int,
    "risk": str,
    "menu": dict,
    "min_etcd_members": int,
    "selector_requirements": dict,
    "impact_class": str,
}


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


def _selector_kind(value: object) -> str:
    text = str(value)
    if text == "quorum_losing_set":
        return "quorum_losing_set"
    if text.isdigit() and int(text) >= 1:
        return "index"
    if text in ORDINAL_ROLES:
        return "role"
    return "node"


def _command_path(command: str) -> str:
    parts = shlex.split(command)
    return parts[0] if parts else ""


def validate_scenario(
    scenario: dict[str, Any],
    root: str | Path | None = None,
    expected_id: str | None = None,
) -> list[str]:
    errors: list[str] = []

    for field, expected_type in REQUIRED_FIELDS.items():
        if field not in scenario:
            errors.append(f"{field}: missing")
        elif not isinstance(scenario[field], expected_type):
            errors.append(f"{field}: expected {expected_type.__name__}")
        elif expected_type is int and isinstance(scenario[field], bool):
            errors.append(f"{field}: expected int, got bool")

    if "fault_present_timeout_seconds" in scenario:
        timeout = scenario["fault_present_timeout_seconds"]
        if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or timeout < 0:
            errors.append("fault_present_timeout_seconds: expected non-negative number")

    scenario_id = str(scenario.get("id", ""))
    if expected_id and scenario_id != expected_id:
        errors.append(f"id: {scenario_id!r} does not match filename {expected_id!r}")
    if not re.fullmatch(r"[a-z0-9][a-z0-9-]*", scenario_id or "x"):
        errors.append(f"id: invalid scenario id {scenario_id!r}")

    if scenario.get("root_cause") not in ROOT_CAUSE_LABELS:
        errors.append(f"root_cause: unsupported {scenario.get('root_cause')!r}")

    for field in ("acceptable_repairs", "forbidden_repairs"):
        value = scenario.get(field)
        if isinstance(value, list):
            unknown = sorted({str(item) for item in value} - REPAIR_TOOLS)
            if unknown:
                errors.append(f"{field}: unknown repair tools {unknown}")

    acceptable = scenario.get("acceptable_repairs")
    forbidden = scenario.get("forbidden_repairs")
    if isinstance(acceptable, list) and isinstance(forbidden, list):
        overlap = sorted({str(item) for item in acceptable} & {str(item) for item in forbidden})
        if overlap:
            errors.append(f"repairs: overlap in acceptable/forbidden {overlap}")

    if scenario.get("risk") not in RISK_LEVELS:
        errors.append(f"risk: unsupported {scenario.get('risk')!r}")
    if scenario.get("impact_class") not in IMPACT_CLASSES:
        errors.append(f"impact_class: unsupported {scenario.get('impact_class')!r}")

    min_members = scenario.get("min_etcd_members")
    if isinstance(min_members, int) and not isinstance(min_members, bool) and min_members < 1:
        errors.append("min_etcd_members: must be at least 1")

    avoid_local = scenario.get("avoid_local_target")
    if avoid_local is not None and not isinstance(avoid_local, bool):
        errors.append("avoid_local_target: expected bool")

    menu = scenario.get("menu")
    if isinstance(menu, dict):
        for field in ("label", "summary"):
            value = menu.get(field)
            if not isinstance(value, str) or not value.strip():
                errors.append(f"menu.{field}: missing or empty")

    selector_requirements = scenario.get("selector_requirements")
    if isinstance(selector_requirements, dict):
        min_targets = selector_requirements.get("min_targets")
        if not isinstance(min_targets, int) or isinstance(min_targets, bool) or min_targets < 0:
            errors.append("selector_requirements.min_targets: expected non-negative int")
        allowed = selector_requirements.get("allowed")
        if not isinstance(allowed, list) or not allowed:
            errors.append("selector_requirements.allowed: expected non-empty list")
        else:
            unknown = sorted({str(item) for item in allowed} - SELECTOR_KINDS)
            if unknown:
                errors.append(f"selector_requirements.allowed: unknown selector kinds {unknown}")

    targets = scenario.get("target_nodes", [])
    if isinstance(targets, str):
        targets = [targets]
    if not isinstance(targets, list):
        errors.append("target_nodes: expected list or string")
        targets = []
    if isinstance(selector_requirements, dict):
        min_targets = selector_requirements.get("min_targets")
        allowed = selector_requirements.get("allowed")
        if isinstance(min_targets, int) and len(targets) < min_targets:
            errors.append(f"target_nodes: expected at least {min_targets} target(s)")
        if isinstance(allowed, list):
            for target in targets:
                kind = _selector_kind(target)
                if kind not in {str(item) for item in allowed}:
                    errors.append(f"target_nodes: selector kind {kind!r} not allowed for {target!r}")

    for field in ("timeout_seconds", "max_steps", "max_tokens"):
        value = scenario.get(field)
        if isinstance(value, int) and not isinstance(value, bool) and value <= 0:
            errors.append(f"{field}: must be positive")

    if root is not None:
        root_path = Path(root)
        for field in ("inject", "verify"):
            command = scenario.get(field)
            if not isinstance(command, str) or not command.strip():
                continue
            relative = _command_path(command)
            path = Path(relative)
            if not path.is_absolute() and not (root_path / path).exists():
                errors.append(f"{field}: script not found {relative!r}")

    return errors
