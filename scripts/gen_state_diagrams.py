from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from harness.machines import GATE_MACHINE, RUN_MACHINE, TOOL_MACHINE
from harness.state_machine import Guards, Machine, Rejection, Transition, validate_machine

MACHINES = (
    ("Gate", GATE_MACHINE),
    ("Run", RUN_MACHINE),
    ("Tool", TOOL_MACHINE),
)

MACHINE_NOTES = {
    "Gate": [
        "## Gate notes",
        "",
        "`GateRuntime` folds diagnosis, nudge, repair-proposal, and typed repair-result events.",
        "The receipt is derived from folded `GateState` as `approved:{run_id}`; tool arguments cannot grant authorization.",
        "Correct no-fault reaches `no_fault_cleared`; correct fault reaches `approved`. Wrong diagnosis is capped, nudge exhaustion is a budget failure, and forbidden, unacceptable, premature, or post-no-fault repairs are typed violations.",
        "`GatePath` is the derived history of this machine and is the scorer's authorization evidence.",
        "",
    ],
    "Run": [
        "## Run notes",
        "",
        "`RunRuntime` owns status, phase, lease, verification, and the terminal outcome.",
        "`INTERRUPTED` is a parked state with `previous_state`; resume triage is a lookup from that previous phase.",
        "The reaper emits idempotent `run.lease_expired` observations for expired leases with no live owner; `--abandon` is the explicit operator exit.",
        "The machine table's crash action is the generic policy; `resume_triage()` applies the phase-specific action.",
        "",
    ],
    "Tool": [
        "## Tool notes",
        "",
        "`ToolLedger` folds one `ToolState` per call. Intent is persisted before execution and typed status/result fields are authoritative.",
        "Denial is valid from `intended`. After `started`, a mutating lease expiry or cancel becomes `UNKNOWN_EFFECT` and forces abort-and-retry; read-only calls may become `SUPERSEDED`.",
        "Persisted status collapses several machine phases: success and supersede both project as `finished`, while unknown effect projects as `failed` with `result_code=unknown_effect`.",
        "Human-readable result strings are evidence for operators; state and score read typed fields.",
        "",
    ],
}

FOOTER = [
    "## Cross-machine flow",
    "",
    "The machines do not call one another. The coordinator creates and updates them in this order:",
    "",
    "1. `ToolLedger` folds tool events per call.",
    "2. `GateRuntime` folds authorization and repair evidence.",
    "3. `RunRuntime` folds lifecycle and terminal outcome.",
    "",
    "The run machine consumes gate and tool outcomes as read-only observations. `Journal` emits canonical `gate.*`, `run.*`, and `tool.*` events; `SQLiteStore.apply_machine_event()` appends them and rebuilds projections from the log.",
    "",
    "## Scoring boundary",
    "",
    "There is one canonical scorer, `gate-path-v1`. It reads folded `GatePath`, run state, and tool states from loaded records. Score rows are projections recomputed from the fold.",
    "",
    "`strict_loop_success` requires an observed fault, finished run, strict gate terminal, first-attempt correct diagnosis, receipt, passing verification, no violations, `end_reason=verify_ok`, acceptable repair or no repair for `no_fault`, and no exclusion reason. `loop_success` is historical only.",
    "",
    "## Operational boundaries",
    "",
    "`db_cli.py doctor --strict` checks terminal reasons, score-row invariants, expired leases, illegal transitions, unknown mutating outcomes, duplicate generated keys, interrupted metadata, cache/fold drift, and missing canonical events.",
    "",
    "Replay and live views show folded machine states with compact transition descriptions. Destructive guard allow-lists, topology-mismatch refusal, `etcd-disk-full` exclusion, fixture opt-in, and write-time redaction remain operational boundaries outside the machines.",
]


def _state_name(machine: Machine, key: object) -> str:
    return machine.states[key].value


def _event_name(event: object) -> str:
    return str(getattr(event, "value", event))


def _targets(machine: Machine, rule: object) -> list[tuple[str, object, str]]:
    if isinstance(rule, Rejection):
        return [("rejected", None, rule.reason)]
    if isinstance(rule, Transition):
        return [("transition", machine.state_id(rule.state), rule.reason or "")]
    if isinstance(rule, Guards):
        targets: list[tuple[str, object, str]] = []
        for branch in rule.branches:
            targets.extend(_targets(machine, branch.result))
        if rule.default is not None:
            targets.extend(_targets(machine, rule.default))
        return targets
    return []


def _diagram(title: str, machine: Machine) -> list[str]:
    lines = [
        f"## {title}",
        "",
        "```mermaid",
        "stateDiagram-v2",
        f"    [*] --> {_state_name(machine, machine.state_id(machine.initial))}",
    ]
    for (state_key, event), rule in sorted(
        machine.rules.items(),
        key=lambda item: (machine.states[item[0][0]].rank, _event_name(item[0][1])),
    ):
        source = _state_name(machine, state_key)
        for kind, target_key, label in _targets(machine, rule):
            if kind == "transition":
                target = _state_name(machine, target_key)
                suffix = f" : {_event_name(event)}" + (f" ({label})" if label else "")
                lines.append(f"    {source} --> {target}{suffix}")
            elif kind == "rejected":
                reason = label.replace('"', "'")
                lines.append(f'    {source} --> rejected : {_event_name(event)} (reject: {reason})')
    lines.extend(["    rejected --> [*]", "```", ""])
    return lines


def _state_table(machine: Machine) -> list[str]:
    lines = [
        "| Machine state | Persisted value | Rank | Terminal | Resumable | Run terminal | End reason required | Crash action |",
        "|---|---|---:|---|---|---|---|---|",
    ]
    for key, spec in sorted(machine.states.items(), key=lambda item: item[1].rank):
        crash = getattr(spec.crash_action, "value", spec.crash_action or "-")
        lines.append(
            "| "
            + " | ".join(
                (
                    _event_name(key),
                    spec.value,
                    str(spec.rank),
                    str(spec.terminal).lower(),
                    str(spec.resumable).lower(),
                    str(spec.run_terminal).lower(),
                    str(spec.end_reason_required).lower(),
                    str(crash),
                )
            )
            + " |"
        )
    lines.append("")
    return lines


def build_document() -> str:
    lines = [
        "# State machine reference",
        "",
        "Generated by `scripts/gen_state_diagrams.py`; do not edit by hand.",
        "",
        "This is the maintained generated companion to `docs/ARCHITECTURE.md`. Its source is the transition tables under `harness/machines/` and the runtime shells in `harness/*_runtime.py`. Regenerate it whenever a machine changes.",
        "",
        "## How to read this reference",
        "",
        "Three always-on machines own behavior. `GateState` authorizes repair, `RunState` owns lifecycle and terminal outcome, and `ToolState` owns each call's status and result. Canonical `gate.*`, `run.*`, and `tool.*` events in the append-only log are authoritative; scalar rows, `state_json`, snapshots, artifacts, exports, replay, and scores are rebuildable projections.",
        "",
        "The pure reducer maps state plus event to a new state and declarative effects. Runtimes emit canonical events with deterministic idempotency keys; replay and folding deduplicate by `seq`/key. Illegal transitions become `state_transition_rejected` events, and duplicate generated keys are reported by doctor.",
        "",
        "Diagram nodes and the first table column are machine-state names. The second table column is the persisted value used in SQLite. Gate states use wire values such as `no_fault_cleared`; run states appear as `status:phase`; tool phases collapse to persisted statuses, so the machine-state column is needed to distinguish them.",
        "",
    ]
    for title, machine in MACHINES:
        validate_machine(machine)
        lines.extend(_diagram(title, machine))
        lines.extend(_state_table(machine))
        lines.extend(MACHINE_NOTES[title])
    return "\n".join(lines + FOOTER).rstrip() + "\n"


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", default="docs/STATE_MACHINE.md")
    args = parser.parse_args()
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(build_document(), encoding="utf-8")


if __name__ == "__main__":
    main()
