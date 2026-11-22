from __future__ import annotations

import copy
import json
import re
import shlex
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from common.vocab import REPAIR_TOOLS, ROOT_CAUSE_LABELS
from tools.naming import normalize_repair_name

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


@dataclass(frozen=True)
class ValidationReport:
    errors: tuple[str, ...] = ()

    @property
    def ok(self) -> bool:
        return not self.errors

    def to_list(self) -> list[str]:
        return list(self.errors)


@dataclass(frozen=True)
class Scenario:
    raw: dict[str, Any]

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Scenario":
        return cls(data)

    @classmethod
    def from_file(cls, path: str | Path, expected_id: str | None = None) -> "Scenario":
        with Path(path).open(encoding="utf-8") as handle:
            data = json.load(handle)
        scenario = cls(data)
        errors = scenario.validate(expected_id=expected_id)
        if not errors.ok:
            raise ValueError("; ".join(errors.to_list()))
        return scenario

    @property
    def id(self) -> str:
        return str(self.raw.get("id", ""))

    @property
    def root_cause(self) -> str:
        return str(self.raw.get("root_cause", ""))

    @property
    def target_nodes(self) -> list[Any]:
        targets = self.raw.get("target_nodes", [])
        return [targets] if isinstance(targets, str) else list(targets)

    @property
    def avoid_local_target(self) -> bool:
        return bool(self.raw.get("avoid_local_target", False))

    @property
    def acceptable_repairs(self) -> frozenset[str]:
        return frozenset(normalize_repair_name(item) for item in self.raw.get("acceptable_repairs", []))

    @property
    def forbidden_repairs(self) -> frozenset[str]:
        return frozenset(normalize_repair_name(item) for item in self.raw.get("forbidden_repairs", []))

    @property
    def inject(self) -> str:
        return str(self.raw.get("inject", ""))

    @property
    def verify(self) -> str:
        return str(self.raw.get("verify", ""))

    @property
    def timeout_seconds(self) -> int:
        return int(self.raw.get("timeout_seconds", 0))

    @property
    def fault_present_timeout_seconds(self) -> int | float | None:
        return self.raw.get("fault_present_timeout_seconds")

    @property
    def max_steps(self) -> int:
        return int(self.raw.get("max_steps", 0))

    @property
    def max_tokens(self) -> int:
        return int(self.raw.get("max_tokens", 0))

    @property
    def risk(self) -> str:
        return str(self.raw.get("risk", ""))

    @property
    def impact_class(self) -> str:
        return str(self.raw.get("impact_class", ""))

    @property
    def min_etcd_members(self) -> int:
        return int(self.raw.get("min_etcd_members", 0))

    @property
    def menu_label(self) -> str:
        return str((self.raw.get("menu") or {}).get("label", ""))

    @property
    def menu_summary(self) -> str:
        return str((self.raw.get("menu") or {}).get("summary", ""))

    def allows_repair(self, name: str) -> bool:
        return name in self.acceptable_repairs and name not in self.forbidden_repairs

    def is_destructive(self) -> bool:
        return self.risk == "destructive"

    def to_dict(self) -> dict[str, Any]:
        return copy.deepcopy(self.raw)

    def command_path(self, command: str) -> str:
        parts = shlex.split(command)
        return parts[0] if parts else ""

    def script_paths(self, root: str | Path) -> list[Path]:
        root_path = Path(root)
        paths: list[Path] = []
        for command in (self.inject, self.verify):
            relative = self.command_path(command)
            path = Path(relative)
            paths.append(path if path.is_absolute() else root_path / path)
        return paths

    def selector_kind(self, value: object) -> str:
        text = str(value)
        if text == "quorum_losing_set":
            return "quorum_losing_set"
        if text.isdigit() and int(text) >= 1:
            return "index"
        if text in ORDINAL_ROLES:
            return "role"
        return "node"

    def validate(
        self,
        root: str | Path | None = None,
        expected_id: str | None = None,
    ) -> ValidationReport:
        errors: list[str] = []
        scenario = self.raw

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
            canonical_overlap = sorted(
                {normalize_repair_name(item) for item in acceptable}
                & {normalize_repair_name(item) for item in forbidden}
            )
            if canonical_overlap:
                errors.append(f"repairs: alias/target policy divergence {canonical_overlap}")

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

        targets = self.target_nodes
        if "target_nodes" in scenario and not isinstance(scenario["target_nodes"], (list, str)):
            errors.append("target_nodes: expected list or string")
            targets = []
        if isinstance(selector_requirements, dict):
            min_targets = selector_requirements.get("min_targets")
            allowed = selector_requirements.get("allowed")
            if isinstance(min_targets, int) and len(targets) < min_targets:
                errors.append(f"target_nodes: expected at least {min_targets} target(s)")
            if isinstance(allowed, list):
                for target in targets:
                    kind = self.selector_kind(target)
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
                relative = self.command_path(command)
                path = Path(relative)
                if not path.is_absolute() and not (root_path / path).exists():
                    errors.append(f"{field}: script not found {relative!r}")

        return ValidationReport(tuple(errors))
