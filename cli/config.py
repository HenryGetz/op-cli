"""Project config discovery and validation for omni CLI."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any


CONFIG_FILENAME = ".omni.json"
SUPPORTED_ASSERTION_TYPES = {
    "region_dimension",
    "measurement",
    "element_count",
    "elements_in_region",
    "region_color_dominant",
}
SUPPORTED_OPERATORS = {"eq", "gte", "lte"}
SUPPORTED_REGION_DIMENSIONS = {"width", "height"}
SUPPORTED_MEASUREMENT_AXES = {"x", "y", "euclidean", "both"}


class OmniConfigError(Exception):
    """Configuration error with dedicated exit code."""

    exit_code = 5


class OmniConfigRequiredError(OmniConfigError):
    """Raised when config is required but unavailable."""


@dataclass
class ProjectConfig:
    path: Path
    data: dict[str, Any]
    version: int
    project_name: str | None
    reference_image: Path | None
    viewport: dict[str, Any]
    regions: dict[str, dict[str, Any]]
    targets: dict[str, dict[str, Any]]
    assertions: list[dict[str, Any]]


def _fail(path: str, message: str) -> OmniConfigError:
    return OmniConfigError(f"Config validation failed at '{path}': {message}")


def _is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _ensure_non_negative_numbers(value: Any, path: str) -> None:
    if _is_number(value):
        if value < 0:
            raise _fail(path, f"expected non-negative number, got {value}")
        return
    if isinstance(value, dict):
        for key, nested in value.items():
            _ensure_non_negative_numbers(nested, f"{path}.{key}")
        return
    if isinstance(value, list):
        for idx, nested in enumerate(value):
            _ensure_non_negative_numbers(nested, f"{path}[{idx}]")


def _require_dict(value: Any, path: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise _fail(path, "expected object")
    return value


def _require_list(value: Any, path: str) -> list[Any]:
    if not isinstance(value, list):
        raise _fail(path, "expected array")
    return value


def _require_string(value: Any, path: str, *, allow_empty: bool = False) -> str:
    if not isinstance(value, str):
        raise _fail(path, "expected string")
    if not allow_empty and not value.strip():
        raise _fail(path, "expected non-empty string")
    return value


def _require_number(value: Any, path: str) -> float:
    if not _is_number(value):
        raise _fail(path, "expected number")
    if value < 0:
        raise _fail(path, f"expected non-negative number, got {value}")
    return float(value)


def _normalize_region(name: str, raw: dict[str, Any], path: str) -> dict[str, Any]:
    x = int(_require_number(raw.get("x"), f"{path}.x"))
    y = int(_require_number(raw.get("y"), f"{path}.y"))
    w = int(_require_number(raw.get("w"), f"{path}.w"))
    h = int(_require_number(raw.get("h"), f"{path}.h"))
    if w == 0 or h == 0:
        raise _fail(path, "region width and height must be > 0")
    description = raw.get("description")
    if description is not None:
        description = _require_string(description, f"{path}.description", allow_empty=True)
    return {
        "name": name,
        "x": x,
        "y": y,
        "w": w,
        "h": h,
        "description": description,
    }


def _assert_region_exists(region_name: str, regions: dict[str, Any], path: str) -> None:
    if region_name not in regions:
        available = ", ".join(sorted(regions.keys())) or "<none>"
        raise _fail(path, f"unknown region '{region_name}'. Available: {available}")


def _extract_region_ref(raw_ref: str) -> str | None:
    base_ref = raw_ref.split("|", 1)[0].strip()
    if base_ref.startswith("region:"):
        return base_ref.split(":", 1)[1].strip()
    return None


def _extract_target_ref(raw_ref: str) -> str | None:
    base_ref = raw_ref.split("|", 1)[0].strip()
    if base_ref.startswith("target:"):
        return base_ref.split(":", 1)[1].strip()
    return None


def _assert_target_exists(target_name: str, targets: dict[str, Any], path: str) -> None:
    if target_name not in targets:
        available = ", ".join(sorted(targets.keys())) or "<none>"
        raise _fail(path, f"unknown target '{target_name}'. Available: {available}")


def _normalize_target(name: str, raw: Any, path: str) -> dict[str, Any]:
    if isinstance(raw, str):
        ref = _require_string(raw, f"{path}.ref")
        description = None
    else:
        raw_obj = _require_dict(raw, path)
        ref = _require_string(raw_obj.get("ref"), f"{path}.ref")
        description = raw_obj.get("description")
        if description is not None:
            description = _require_string(description, f"{path}.description", allow_empty=True)

    return {
        "name": name,
        "ref": ref,
        "description": description,
    }


def _validate_targets(
    targets: dict[str, dict[str, Any]],
    *,
    regions: dict[str, dict[str, Any]],
) -> None:
    def _walk(target_name: str, seen: list[str]) -> None:
        if target_name in seen:
            cycle = " -> ".join(seen + [target_name])
            raise _fail("targets", f"target reference cycle detected: {cycle}")

        ref = str(targets[target_name]["ref"])
        region_ref = _extract_region_ref(ref)
        if region_ref is not None:
            _assert_region_exists(region_ref, regions, f"targets.{target_name}.ref")
            return

        target_ref = _extract_target_ref(ref)
        if target_ref is not None:
            _assert_target_exists(target_ref, targets, f"targets.{target_name}.ref")
            _walk(target_ref, seen + [target_name])

    for target_name in targets:
        _walk(target_name, [])


def _validate_assertion(
    assertion: dict[str, Any],
    idx: int,
    regions: dict[str, dict[str, Any]],
    targets: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    path = f"assertions[{idx}]"
    assertion_id = _require_string(assertion.get("id"), f"{path}.id")
    assertion_type = _require_string(assertion.get("type"), f"{path}.type")
    if assertion_type not in SUPPORTED_ASSERTION_TYPES:
        supported = ", ".join(sorted(SUPPORTED_ASSERTION_TYPES))
        raise _fail(f"{path}.type", f"unsupported assertion type '{assertion_type}'. Supported: {supported}")

    description = assertion.get("description")
    if description is not None:
        _require_string(description, f"{path}.description", allow_empty=True)

    if assertion_type == "region_dimension":
        region_name = _require_string(assertion.get("region"), f"{path}.region")
        _assert_region_exists(region_name, regions, f"{path}.region")
        prop = _require_string(assertion.get("property"), f"{path}.property")
        if prop not in SUPPORTED_REGION_DIMENSIONS:
            raise _fail(f"{path}.property", "must be 'width' or 'height'")
        _require_number(assertion.get("expected"), f"{path}.expected")
        _require_number(assertion.get("tolerance"), f"{path}.tolerance")

    elif assertion_type == "measurement":
        from_obj = _require_dict(assertion.get("from"), f"{path}.from")
        to_obj = _require_dict(assertion.get("to"), f"{path}.to")
        from_ref = _require_string(from_obj.get("ref"), f"{path}.from.ref")
        to_ref = _require_string(to_obj.get("ref"), f"{path}.to.ref")
        from_region = _extract_region_ref(from_ref)
        to_region = _extract_region_ref(to_ref)
        if from_region is not None:
            _assert_region_exists(from_region, regions, f"{path}.from.ref")
        if to_region is not None:
            _assert_region_exists(to_region, regions, f"{path}.to.ref")

        from_target = _extract_target_ref(from_ref)
        to_target = _extract_target_ref(to_ref)
        if from_target is not None:
            _assert_target_exists(from_target, targets, f"{path}.from.ref")
        if to_target is not None:
            _assert_target_exists(to_target, targets, f"{path}.to.ref")

        axis = _require_string(assertion.get("axis"), f"{path}.axis")
        if axis not in SUPPORTED_MEASUREMENT_AXES:
            supported = ", ".join(sorted(SUPPORTED_MEASUREMENT_AXES))
            raise _fail(f"{path}.axis", f"unsupported axis '{axis}'. Supported: {supported}")
        _require_number(assertion.get("expected"), f"{path}.expected")
        _require_number(assertion.get("tolerance"), f"{path}.tolerance")

    elif assertion_type == "element_count":
        operator = _require_string(assertion.get("operator"), f"{path}.operator")
        if operator not in SUPPORTED_OPERATORS:
            raise _fail(f"{path}.operator", "must be one of eq/gte/lte")
        _require_number(assertion.get("expected"), f"{path}.expected")

    elif assertion_type == "elements_in_region":
        region_name = _require_string(assertion.get("region"), f"{path}.region")
        _assert_region_exists(region_name, regions, f"{path}.region")
        operator = _require_string(assertion.get("operator"), f"{path}.operator")
        if operator not in SUPPORTED_OPERATORS:
            raise _fail(f"{path}.operator", "must be one of eq/gte/lte")
        _require_number(assertion.get("expected"), f"{path}.expected")

    elif assertion_type == "region_color_dominant":
        region_name = _require_string(assertion.get("region"), f"{path}.region")
        _assert_region_exists(region_name, regions, f"{path}.region")
        expected_hex = _require_string(assertion.get("expected_hex"), f"{path}.expected_hex")
        if not expected_hex.startswith("#") or len(expected_hex) != 7:
            raise _fail(f"{path}.expected_hex", "must be #RRGGBB")
        _require_number(assertion.get("tolerance_delta"), f"{path}.tolerance_delta")

    return assertion


def discover_config_path(*, cwd: Path, explicit_path: str | None) -> Path | None:
    if explicit_path:
        candidate = Path(explicit_path).expanduser().resolve()
        if not candidate.exists():
            raise OmniConfigError(f"Config file not found: {candidate}")
        if not candidate.is_file():
            raise OmniConfigError(f"Config path is not a file: {candidate}")
        return candidate

    current = cwd.resolve()
    home = Path.home().resolve()
    while True:
        candidate = current / CONFIG_FILENAME
        if candidate.exists() and candidate.is_file():
            return candidate

        reached_root = current.parent == current
        reached_home = current == home
        if reached_root or reached_home:
            break
        current = current.parent

    return None


def load_project_config(config_path: Path) -> ProjectConfig:
    try:
        content = config_path.read_text(encoding="utf-8")
    except Exception as exc:
        raise OmniConfigError(f"Failed to read config file '{config_path}': {exc}") from exc

    try:
        raw_data = json.loads(content)
    except json.JSONDecodeError as exc:
        raise OmniConfigError(f"Invalid JSON in config '{config_path}': {exc}") from exc

    data = _require_dict(raw_data, "root")
    _ensure_non_negative_numbers(data, "root")

    version = data.get("version")
    if version != 1:
        raise _fail("version", f"expected 1, got {version!r}")

    project_name = data.get("project_name")
    if project_name is not None:
        project_name = _require_string(project_name, "project_name", allow_empty=True)

    reference_image_raw = data.get("reference_image")
    reference_image = None
    if reference_image_raw is not None:
        reference_image_str = _require_string(reference_image_raw, "reference_image")
        reference_path = Path(reference_image_str).expanduser()
        if not reference_path.is_absolute():
            reference_path = (config_path.parent / reference_path).resolve()
        reference_image = reference_path

    viewport = data.get("viewport", {})
    if viewport is None:
        viewport = {}
    viewport = _require_dict(viewport, "viewport")
    for field in ("width", "height", "device_pixel_ratio"):
        if field in viewport:
            _require_number(viewport[field], f"viewport.{field}")

    raw_regions = data.get("regions", {})
    if raw_regions is None:
        raw_regions = {}
    raw_regions = _require_dict(raw_regions, "regions")
    normalized_regions: dict[str, dict[str, Any]] = {}
    for region_name, region_raw in raw_regions.items():
        if not isinstance(region_name, str) or not region_name.strip():
            raise _fail("regions", "region names must be non-empty strings")
        normalized_regions[region_name] = _normalize_region(
            region_name,
            _require_dict(region_raw, f"regions.{region_name}"),
            f"regions.{region_name}",
        )

    raw_targets = data.get("targets", {})
    if raw_targets is None:
        raw_targets = {}
    raw_targets = _require_dict(raw_targets, "targets")
    normalized_targets: dict[str, dict[str, Any]] = {}
    for target_name, target_raw in raw_targets.items():
        if not isinstance(target_name, str) or not target_name.strip():
            raise _fail("targets", "target names must be non-empty strings")
        normalized_targets[target_name] = _normalize_target(
            target_name,
            target_raw,
            f"targets.{target_name}",
        )
    _validate_targets(normalized_targets, regions=normalized_regions)

    raw_assertions = data.get("assertions", [])
    if raw_assertions is None:
        raw_assertions = []
    raw_assertions = _require_list(raw_assertions, "assertions")
    assertions: list[dict[str, Any]] = []
    ids_seen: set[str] = set()
    for idx, raw_assertion in enumerate(raw_assertions):
        assertion = _validate_assertion(
            _require_dict(raw_assertion, f"assertions[{idx}]"),
            idx,
            normalized_regions,
            normalized_targets,
        )
        assertion_id = str(assertion["id"])
        if assertion_id in ids_seen:
            raise _fail(f"assertions[{idx}].id", f"duplicate assertion id '{assertion_id}'")
        ids_seen.add(assertion_id)
        assertions.append(assertion)

    return ProjectConfig(
        path=config_path.resolve(),
        data=data,
        version=1,
        project_name=project_name,
        reference_image=reference_image,
        viewport=viewport,
        regions=normalized_regions,
        targets=normalized_targets,
        assertions=assertions,
    )


class ProjectConfigManager:
    """Discover and cache config lookup once per CLI invocation."""

    def __init__(self, *, cwd: Path, explicit_path: str | None) -> None:
        self.cwd = cwd
        self.explicit_path = explicit_path
        self._loaded = False
        self._config: ProjectConfig | None = None

    def get(self, *, required: bool = False) -> ProjectConfig | None:
        if not self._loaded:
            config_path = discover_config_path(cwd=self.cwd, explicit_path=self.explicit_path)
            if config_path is None:
                self._config = None
            else:
                self._config = load_project_config(config_path)
            self._loaded = True

        if required and self._config is None:
            if self.explicit_path:
                raise OmniConfigRequiredError(
                    f"Config file required but not found: {Path(self.explicit_path).expanduser().resolve()}"
                )
            raise OmniConfigRequiredError(
                f"Config file required for this command. Provide --config <path> or create {CONFIG_FILENAME} in this directory tree."
            )
        return self._config
