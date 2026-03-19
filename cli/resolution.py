"""Reference and edge resolution helpers for omni CLI."""

from __future__ import annotations

import difflib
import re
from typing import Any


EDGE_CHOICES = (
    "left",
    "right",
    "top",
    "bottom",
    "center",
    "top-left",
    "top-right",
    "bottom-left",
    "bottom-right",
)


class ResolutionError(Exception):
    """Invalid coordinate/reference resolution request."""

    exit_code = 1


class ResolutionConfigRequiredError(ResolutionError):
    """Raised when a region reference is requested without config."""

    exit_code = 5


def normalize_text(value: str) -> str:
    return " ".join(value.strip().casefold().split())


def parse_coord_pair(spec: str) -> tuple[int, int] | None:
    match = re.fullmatch(r"\s*(-?\d+)\s*,\s*(-?\d+)\s*", spec)
    if not match:
        return None
    return int(match.group(1)), int(match.group(2))


def bbox_point_for_edge(bbox_xywh: dict[str, int], edge: str) -> tuple[int, int]:
    x = int(bbox_xywh["x"])
    y = int(bbox_xywh["y"])
    width = int(bbox_xywh["width"])
    height = int(bbox_xywh["height"])
    cx = x + width // 2
    cy = y + height // 2

    if edge == "left":
        return x, cy
    if edge == "right":
        return x + width, cy
    if edge == "top":
        return cx, y
    if edge == "bottom":
        return cx, y + height
    if edge == "top-left":
        return x, y
    if edge == "top-right":
        return x + width, y
    if edge == "bottom-left":
        return x, y + height
    if edge == "bottom-right":
        return x + width, y + height
    return cx, cy


def region_to_bbox(region: dict[str, Any]) -> dict[str, int]:
    return {
        "x": int(region["x"]),
        "y": int(region["y"]),
        "width": int(region["w"]),
        "height": int(region["h"]),
    }


def resolve_region_ref(
    *,
    spec: str,
    regions: dict[str, dict[str, Any]] | None,
    role: str,
    edge: str,
) -> dict[str, Any] | None:
    if not spec.startswith("region:"):
        return None

    region_name = spec.split(":", 1)[1].strip()
    if not region_name:
        raise ResolutionError(f"Invalid {role} region reference '{spec}'.")
    if regions is None:
        raise ResolutionConfigRequiredError(
            f"{role} uses region reference '{spec}', but no project config is loaded. Provide --config <path> or create .omni.json."
        )
    if region_name not in regions:
        available = ", ".join(sorted(regions.keys())) or "<none>"
        raise ResolutionError(
            f"Unknown region '{region_name}' for {role}. Available regions: {available}"
        )

    region = regions[region_name]
    bbox = region_to_bbox(region)
    point_x, point_y = bbox_point_for_edge(bbox, edge=edge)
    return {
        "mode": "region",
        "spec": spec,
        "region_name": region_name,
        "resolved_point": {"x": point_x, "y": point_y},
        "resolved_bbox": bbox,
    }


def resolve_reference_spec(
    *,
    spec: str,
    elements: list[dict[str, Any]],
    image_width: int,
    image_height: int,
    edge: str,
    role: str,
    regions: dict[str, dict[str, Any]] | None = None,
) -> dict[str, Any]:
    normalized_spec = spec.strip()
    if edge not in EDGE_CHOICES:
        raise ResolutionError(
            f"Unsupported edge '{edge}' for {role}. Supported: {', '.join(EDGE_CHOICES)}"
        )

    region_resolved = resolve_region_ref(
        spec=normalized_spec,
        regions=regions,
        role=role,
        edge=edge,
    )
    if region_resolved is not None:
        return region_resolved

    if normalized_spec.startswith("element:"):
        idx_text = normalized_spec.split(":", 1)[1].strip()
        try:
            idx = int(idx_text)
        except ValueError as exc:
            raise ResolutionError(f"Invalid {role} element reference '{normalized_spec}'.") from exc
        if idx < 0 or idx >= len(elements):
            raise ResolutionError(
                f"{role} references element:{idx}, but valid indexes are 0..{max(0, len(elements) - 1)}."
            )
        element = elements[idx]
        point_x, point_y = bbox_point_for_edge(element["bbox"], edge=edge)
        return {
            "mode": "element",
            "spec": normalized_spec,
            "element_index": idx,
            "element_label": str(element.get("label", "")),
            "resolved_point": {"x": point_x, "y": point_y},
            "resolved_bbox": element["bbox"],
        }

    coord = parse_coord_pair(normalized_spec)
    if coord is not None:
        x, y = coord
        if not (0 <= x <= image_width and 0 <= y <= image_height):
            raise ResolutionError(
                f"{role} coordinates ({x},{y}) are outside image bounds 0..{image_width}, 0..{image_height}."
            )
        return {
            "mode": "coordinates",
            "spec": normalized_spec,
            "resolved_point": {"x": x, "y": y},
        }

    query_norm = normalize_text(normalized_spec)
    best_score = -1.0
    best_element: dict[str, Any] | None = None
    for element in elements:
        label = str(element.get("label", "")).strip()
        if not label:
            continue
        label_norm = normalize_text(label)
        if not label_norm:
            continue
        score = difflib.SequenceMatcher(None, query_norm, label_norm).ratio()
        if query_norm in label_norm:
            score = max(score, 0.95)
        if score > best_score:
            best_score = score
            best_element = element

    if best_element is None or best_score < 0.35:
        raise ResolutionError(
            f"Unable to resolve '{normalized_spec}' for {role}. Try coordinates, element:<index>, or region:<name>."
        )

    point_x, point_y = bbox_point_for_edge(best_element["bbox"], edge=edge)
    return {
        "mode": "label",
        "spec": normalized_spec,
        "matched_score": round(best_score, 4),
        "element_index": int(best_element["index"]),
        "element_label": str(best_element.get("label", "")),
        "resolved_point": {"x": point_x, "y": point_y},
        "resolved_bbox": best_element["bbox"],
    }


def element_center(element: dict[str, Any]) -> tuple[int, int]:
    bbox = element["bbox"]
    return bbox_point_for_edge(bbox, edge="center")


def point_in_region(*, x: int, y: int, region: dict[str, Any]) -> bool:
    rx = int(region["x"])
    ry = int(region["y"])
    rw = int(region["w"])
    rh = int(region["h"])
    return rx <= x <= (rx + rw) and ry <= y <= (ry + rh)

