from __future__ import annotations

from typing import Any

from .base import DetectionEngine
from .omniparser import OmniParserEngine, engine_availability as omniparser_availability
from .uied import UIEDEngine, engine_availability as uied_availability


ENGINES: dict[str, type[DetectionEngine]] = {
    "omniparser": OmniParserEngine,
    "uied": UIEDEngine,
}


_ENGINE_AVAILABILITY_CHECKS = {
    "omniparser": omniparser_availability,
    "uied": uied_availability,
}


def get_engine(name: str) -> DetectionEngine:
    key = str(name).strip().lower()
    if key not in ENGINES:
        available = ", ".join(list_engines())
        raise ValueError(f"Unknown engine '{name}'. Available engines: {available}")
    return ENGINES[key]()


def list_engines() -> list[str]:
    return sorted(ENGINES.keys())


def list_engine_status() -> list[dict[str, Any]]:
    statuses: list[dict[str, Any]] = []
    for name in list_engines():
        engine_class = ENGINES[name]
        check = _ENGINE_AVAILABILITY_CHECKS.get(name)
        available = True
        reason = None
        if check is not None:
            try:
                available, reason = check()
            except Exception as exc:  # noqa: BLE001
                available = False
                reason = str(exc)
        entry: dict[str, Any] = {
            "name": name,
            "display_name": str(getattr(engine_class, "display_name", name)),
            "available": bool(available),
        }
        if reason:
            entry["reason"] = str(reason)
        statuses.append(entry)
    return statuses

