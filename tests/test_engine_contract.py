from __future__ import annotations

import json
import os
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parents[1]
CLI_ROOT = REPO_ROOT / "cli"


if str(CLI_ROOT) not in os.sys.path:
    os.sys.path.insert(0, str(CLI_ROOT))


from engines.base import DetectedElement  # noqa: E402
from engines.registry import get_engine, list_engine_status, list_engines  # noqa: E402


def _selected_engine_names(config: pytest.Config) -> list[str]:
    requested = config.getoption("engine")
    names = list_engines()
    if requested:
        if requested not in names:
            raise pytest.UsageError(
                f"Unknown --engine '{requested}'. Registered engines: {', '.join(names)}"
            )
        return [requested]
    return names


def _engine_status_map() -> dict[str, dict]:
    return {item["name"]: item for item in list_engine_status()}


def pytest_generate_tests(metafunc: pytest.Metafunc) -> None:
    if "engine_name" in metafunc.fixturenames:
        selected = _selected_engine_names(metafunc.config)
        metafunc.parametrize("engine_name", selected, ids=selected)


def test_engine_detect_contract(engine_name: str) -> None:
    image_path = REPO_ROOT / "cli-debug-inputs" / "acist-1280x800-playwright.png"
    assert image_path.exists(), "expected real test image in cli-debug-inputs/"

    if engine_name == "omniparser":
        runtime_root = REPO_ROOT / "OmniParser"
        if runtime_root.exists():
            os.environ["OMNIPARSER_ROOT"] = str(runtime_root)

    if engine_name == "uied":
        candidate_roots = [
            Path(os.environ.get("UIED_ROOT", "")).expanduser() if os.environ.get("UIED_ROOT") else None,
            REPO_ROOT / "UIED",
            Path("/home/wavy/ai/UIED"),
        ]
        resolved = next((candidate for candidate in candidate_roots if candidate and candidate.exists()), None)
        if resolved is None:
            pytest.skip("UIED root not found")
        os.environ["UIED_ROOT"] = str(resolved)

    status = _engine_status_map().get(engine_name)
    if status is None:
        pytest.fail(f"registry status missing for engine '{engine_name}'")
    if not bool(status.get("available")):
        pytest.skip(str(status.get("reason") or "engine dependencies unavailable"))

    model_dir = REPO_ROOT / "OmniParser" / "weights"
    engine = get_engine(engine_name)
    engine.load(model_dir=str(model_dir), device="cpu")
    elements = engine.detect(str(image_path))

    assert isinstance(elements, list)
    assert elements, "detect() should return a non-empty list for a real image"

    element_ids: set[str] = set()
    for element in elements:
        assert isinstance(element, DetectedElement)
        assert element.bbox.x >= 0
        assert element.bbox.y >= 0
        assert element.bbox.w >= 0
        assert element.bbox.h >= 0
        assert 0.0 <= float(element.confidence) <= 1.0
        assert element.source_engine == engine.name
        assert element.element_id not in element_ids
        element_ids.add(element.element_id)
        json.dumps(element.raw)


def test_detected_element_field_set_is_explicit() -> None:
    expected_fields = {
        "element_id",
        "element_type",
        "bbox",
        "label",
        "confidence",
        "source_engine",
        "raw",
    }
    actual_fields = set(DetectedElement.__dataclass_fields__.keys())
    assert actual_fields == expected_fields
