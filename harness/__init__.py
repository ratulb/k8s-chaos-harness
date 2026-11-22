from typing import Any

__all__ = ["run_scenario"]


def __getattr__(name: str) -> Any:
    if name == "run_scenario":
        from .agent import run_scenario

        return run_scenario
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
