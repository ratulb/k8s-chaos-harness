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

PATHS = {
    "Gate": [
        "await_diagnosis",
        "  ├─ correct diagnosis with fault → approved",
        "  │    ├─ acceptable repair succeeds → repaired",
        "  │    └─ acceptable repair fails → approved",
        "  ├─ correct diagnosis with no fault → no_fault_cleared",
        "  ├─ wrong diagnosis below cap → await_diagnosis",
        "  └─ wrong diagnosis at cap, budget, abort, escalation,",
        "     or unknown mutating effect → escalated",
    ],
    "Run": [
        "pending:queued",
        "  → running:leased",
        "  → running:resetting",
        "  → running:baseline",
        "  → running:injecting",
        "  → running:fault_check",
        "  → running:agent_running",
        "  → running:verifying",
        "  → finished:finished",
        "",
        "Any nonterminal phase may become:",
        "  interrupted:interrupted → resume, retry, or abandon",
        "  aborted:aborted → operator abort or terminal setup/agent failure",
    ],
    "Tool": [
        "proposed",
        "  ├─ deny → denied",
        "  ├─ start → started",
        "  │    ├─ success → finished",
        "  │    ├─ failure → failed",
        "  │    └─ cancel or lease expiry on a mutating call → failed (unknown effect)",
        "  └─ resume supersede on a read-only call → finished (superseded)",
        "started + lease expiry on a read-only call → finished (superseded)",
    ],
}

MACHINE_NOTES = {
    "Gate": [
        "The gate answers three questions: was the diagnosis accepted, was a repair authorized, and was a policy violation recorded?",
        "The receipt is derived from folded gate state as `approved:{run_id}`. Tool arguments cannot grant authorization.",
        "Two diagnosis attempts and two nudges are allowed. Strict scoring requires first-attempt correctness; a second correct diagnosis can still authorize a repair but does not pass the headline metric.",
        "`GatePath` is the folded history used by the scorer. Violations are typed: `premature_repair`, `forbidden_repair`, `unacceptable_repair`, and `repair_after_no_fault`.",
    ],
    "Run": [
        "The run machine owns persisted `status` and `phase`, lease state, verification outcome, interruption, and terminal outcome.",
        "Machine phases are not always wire strings. `baselining` persists as `running:baseline`; `interrupted` persists as `interrupted:interrupted`.",
        "`verify_ok` and `verify_failed` both finish the run. The scorer decides whether that finished run is strict-successful.",
        "The `After crash` column is the generic machine policy. `resume_triage()` applies the phase-specific action using `previous_state`.",
    ],
    "Tool": [
        "The tool machine tracks one call at a time. Intent is persisted before execution; typed status and result fields are authoritative.",
        "A mutating call that is cancelled or loses its lease after starting becomes `unknown_effect`, persists as `failed`, and forces abort-and-retry.",
        "A read-only call may be superseded after resume and persists as `finished` with `result_code=superseded_after_resume`.",
        "Human-readable result strings are evidence for operators; state and score read typed fields.",
    ],
}

FOOTER = [
    "## Cross-machine flow",
    "",
    "The machines do not call one another. The coordinator folds them from canonical events:",
    "",
    "1. `ToolLedger` folds `tool.*` events into one `ToolState` per call.",
    "2. `GateRuntime` folds `gate.*` events into `GateState` and `GatePath`.",
    "3. `RunRuntime` folds `run.*` events into `RunState`.",
    "",
    "The run machine consumes gate and tool outcomes as read-only observations. `Journal.event()` writes canonical events, and `SQLiteStore.apply_machine_event()` appends them and rebuilds projections from the log.",
    "",
    "## Scoring boundary",
    "",
    "There is one canonical scorer, `gate-path-v1`. It reads folded `GatePath`, run state, and tool states from loaded records. Score rows are projections recomputed from the fold.",
    "",
    "`strict_loop_success` requires an observed fault, finished run, strict gate terminal, first-attempt correct diagnosis, receipt, passing verification, no violations, folded `end_reason=verify_ok`, acceptable repair or no repair for `no_fault`, and no exclusion reason. `loop_success` is historical only.",
    "",
    "## What is outside the machines",
    "",
    "The destructive guard, topology-mismatch handling, fixture opt-in, operator convention for `etcd-disk-full`, write-time redaction, LLM fallback, and repair command execution are operational boundaries outside the state machines.",
    "",
    "## Where to look in code",
    "",
    "| Concern | Source |",
    "|---|---|",
    "| State tables and pure transitions | `harness/machines/gate.py`, `harness/machines/run.py`, `harness/machines/tools.py` |",
    "| Runtime event shells | `harness/gate_runtime.py`, `harness/run_runtime.py`, `harness/tool_runtime.py` |",
    "| Event channel and redaction | `harness/journal.py`, `harness/message.py` |",
    "| Event log and state rebuild | `storage/sqlite_store.py` |",
    "| Canonical scorer | `common/scoring.py` |",
    "| Doctor checks | `db_cli.py`, `storage/sqlite_store.py` |",
    "| Repair authorization | `harness/tooling.py` |",
    "",
    "## Regenerate and verify",
    "",
    "```bash",
    "python3 scripts/gen_state_diagrams.py --output docs/STATE_MACHINE.md",
    "python3 -m unittest tests.test_generated_docs -v",
    "```",
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


def _allowed_transitions(machine: Machine) -> list[tuple[str, str, str]]:
    ranks = {_state_name(machine, key): spec.rank for key, spec in machine.states.items()}
    rows: set[tuple[str, str, str]] = set()
    for (state_key, event), rule in machine.rules.items():
        source = _state_name(machine, state_key)
        for kind, target_key, _label in _targets(machine, rule):
            if kind != "transition" or target_key is None:
                continue
            target = _state_name(machine, target_key)
            rows.add((source, _event_name(event), target))
    return sorted(rows, key=lambda row: (ranks[row[0]], row[1], ranks[row[2]]))


def _paths(title: str) -> list[str]:
    return ["### Main paths", "", "```text", *PATHS[title], "```", ""]


def _state_table(machine: Machine) -> list[str]:
    lines = [
        "### States",
        "",
        "| Machine state | Persisted value | Terminal | After crash |",
        "|---|---|---|---|",
    ]
    for key, spec in sorted(machine.states.items(), key=lambda item: item[1].rank):
        crash = getattr(spec.crash_action, "value", spec.crash_action or "-")
        lines.append(
            "| "
            + " | ".join(
                (
                    _event_name(key),
                    spec.value,
                    str(spec.terminal).lower(),
                    str(crash),
                )
            )
            + " |"
        )
    lines.append("")
    return lines


def _transition_table(machine: Machine) -> list[str]:
    rows = _allowed_transitions(machine)
    lines = [
        f"### Allowed transitions ({len(rows)})",
        "",
        "This table lists every allowed state, event, and target combination. Guard branches with different payload conditions can share an event and have multiple targets. Rejected combinations are intentionally omitted; the runtime records them as `state_transition_rejected` events.",
        "",
        "| From | Event | To |",
        "|---|---|---|",
    ]
    lines.extend(f"| {source} | {event} | {target} |" for source, event, target in rows)
    lines.append("")
    return lines


def build_document() -> str:
    lines = [
        "# State machine reference",
        "",
        "Generated by `scripts/gen_state_diagrams.py`; do not edit by hand.",
        "",
        "This generated companion to `docs/ARCHITECTURE.md` comes from the transition tables under `harness/machines/` and the runtime shells in `harness/*_runtime.py`. Regenerate it whenever a machine changes.",
        "",
        "## Read this first",
        "",
        "Use this reference when you need the exact lifecycle of a gate, run, or tool call. It answers three questions: what states exist, which events move between them, and what happens after a crash.",
        "",
        "- `GateState` decides whether diagnosis was accepted and repair is allowed.",
        "- `RunState` owns run phase, lease, verification, interruption, and terminal outcome.",
        "- `ToolState` owns the lifecycle and typed outcome of one tool call.",
        "",
        "The main path blocks are intentionally small. The tables below are the precise reference. `Machine state` is the internal name; `Persisted value` is what appears in SQLite, live views, and replay. `After crash` is the generic policy for that state.",
        "",
        "Canonical `gate.*`, `run.*`, and `tool.*` events in the append-only log are authoritative. Runtimes emit them with deterministic keys; folding deduplicates by `seq` or key. Illegal transitions become `state_transition_rejected` events, and `db_cli.py doctor --strict` reports duplicate generated keys and fold drift.",
        "",
    ]
    for title, machine in MACHINES:
        validate_machine(machine)
        lines.append(f"## {title}")
        lines.append("")
        lines.extend(MACHINE_NOTES[title])
        lines.append("")
        lines.extend(_paths(title))
        lines.extend(_transition_table(machine))
        lines.extend(_state_table(machine))
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
