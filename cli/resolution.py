"""Reference and edge resolution helpers for omni CLI."""

from __future__ import annotations

import difflib
import math
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

QUERY_SIDE_CHOICES = (
    "left",
    "right",
    "top",
    "bottom",
    "center",
)


class ResolutionError(Exception):
    """Invalid coordinate/reference resolution request."""

    exit_code = 1


class ResolutionConfigRequiredError(ResolutionError):
    """Raised when a region reference is requested without config."""

    exit_code = 5


def _parse_label_query_spec(
    *,
    spec: str,
    role: str,
) -> tuple[str, dict[str, Any], bool]:
    if "|" not in spec and not spec.startswith("label:"):
        return spec, {}, False

    tokens = [token.strip() for token in spec.split("|") if token.strip()]
    if not tokens:
        raise ResolutionError(f"Invalid {role} reference '{spec}'.")

    base_spec = tokens[0]
    explicit_label_prefix = False
    if base_spec.startswith("label:"):
        explicit_label_prefix = True
        base_spec = base_spec.split(":", 1)[1].strip()

    hints: dict[str, Any] = {}
    for token in tokens[1:]:
        if ":" not in token:
            raise ResolutionError(
                f"Invalid query hint '{token}' for {role}. Use key:value format, e.g. near:120,400"
            )
        key, value = token.split(":", 1)
        key = key.strip().casefold()
        value = value.strip()

        if key == "near":
            coord = parse_coord_pair(value)
            if coord is None:
                raise ResolutionError(
                    f"Invalid near hint '{value}' for {role}. Expected near:x,y"
                )
            hints["near_point"] = {"x": int(coord[0]), "y": int(coord[1])}
            continue

        if key == "side":
            side = value.casefold()
            if side not in QUERY_SIDE_CHOICES:
                raise ResolutionError(
                    f"Invalid side hint '{value}' for {role}. Supported sides: {', '.join(QUERY_SIDE_CHOICES)}"
                )
            hints["side"] = side
            continue

        if key == "within":
            try:
                within = float(value)
            except ValueError as exc:
                raise ResolutionError(
                    f"Invalid within hint '{value}' for {role}. Expected a numeric pixel value."
                ) from exc
            if within < 0:
                raise ResolutionError(
                    f"Invalid within hint '{value}' for {role}. Value must be >= 0."
                )
            hints["within"] = within
            continue

        raise ResolutionError(
            f"Unknown query hint '{key}' for {role}. Supported hints: near, side, within"
        )

    return base_spec, hints, explicit_label_prefix


def _distance_score(*, x1: float, y1: float, x2: float, y2: float, max_distance: float) -> float:
    if max_distance <= 0:
        return 1.0
    distance = math.hypot(x2 - x1, y2 - y1)
    return max(0.0, 1.0 - (distance / max_distance))


def _side_score(*, side: str, x: float, y: float, image_width: int, image_height: int) -> float:
    width = max(1, int(image_width))
    height = max(1, int(image_height))
    nx = min(1.0, max(0.0, x / width))
    ny = min(1.0, max(0.0, y / height))

    if side == "left":
        return 1.0 - nx
    if side == "right":
        return nx
    if side == "top":
        return 1.0 - ny
    if side == "bottom":
        return ny

    # center
    cx = width / 2.0
    cy = height / 2.0
    max_distance = math.hypot(cx, cy)
    return _distance_score(x1=x, y1=y, x2=cx, y2=cy, max_distance=max_distance)


def rank_reference_candidates(
    *,
    spec: str,
    elements: list[dict[str, Any]],
    image_width: int,
    image_height: int,
    role: str,
) -> list[dict[str, Any]]:
    normalized_spec = spec.strip()
    lookup_spec, query_hints, explicit_label_prefix = _parse_label_query_spec(
        spec=normalized_spec,
        role=role,
    )

    # Ranking is only meaningful for label-style lookups.
    if lookup_spec.startswith("region:"):
        return []
    if lookup_spec.startswith("element:"):
        return []
    if parse_coord_pair(lookup_spec) is not None:
        return []

    query_norm = normalize_text(lookup_spec)
    wildcard_query = query_norm in {"", "*", "any"}

    has_hint_bias = bool(query_hints)
    max_distance = math.hypot(max(1, image_width), max(1, image_height))

    ranked: list[dict[str, Any]] = []
    for element in elements:
        label = str(element.get("label", "")).strip()
        label_norm = normalize_text(label)
        if not label_norm and not wildcard_query:
            continue

        if wildcard_query:
            label_score = 0.5
        else:
            label_score = difflib.SequenceMatcher(None, query_norm, label_norm).ratio()
            if query_norm and query_norm in label_norm:
                label_score = max(label_score, 0.95)

        bbox = element["bbox"]
        cx, cy = bbox_point_for_edge(bbox, edge="center")

        near_score = None
        distance_to_near = None
        near_point = query_hints.get("near_point")
        within = query_hints.get("within")
        if near_point is not None:
            nx = int(near_point["x"])
            ny = int(near_point["y"])
            distance_to_near = math.hypot(cx - nx, cy - ny)
            if within is not None and distance_to_near > within:
                continue
            near_score = _distance_score(
                x1=cx,
                y1=cy,
                x2=nx,
                y2=ny,
                max_distance=max_distance,
            )

        side_score = None
        side = query_hints.get("side")
        if side is not None:
            side_score = _side_score(
                side=str(side),
                x=float(cx),
                y=float(cy),
                image_width=image_width,
                image_height=image_height,
            )

        weighted = 0.0
        total_weight = 0.0

        if wildcard_query:
            weighted += label_score * 0.2
            total_weight += 0.2
        else:
            weighted += label_score * 0.65
            total_weight += 0.65

        if near_score is not None:
            weighted += near_score * 0.25
            total_weight += 0.25

        if side_score is not None:
            weighted += side_score * 0.10
            total_weight += 0.10

        final_score = weighted / total_weight if total_weight > 0 else label_score

        ranked.append(
            {
                "element": element,
                "score": float(final_score),
                "label_score": float(label_score),
                "near_score": float(near_score) if near_score is not None else None,
                "side_score": float(side_score) if side_score is not None else None,
                "distance_to_near_px": float(distance_to_near) if distance_to_near is not None else None,
                "hints": query_hints,
                "explicit_label_prefix": explicit_label_prefix,
            }
        )

    ranked.sort(
        key=lambda item: (
            -item["score"],
            -item["label_score"],
            int(item["element"].get("index", 0)),
        )
    )
    return ranked


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

    lookup_spec, query_hints, _explicit_label_prefix = _parse_label_query_spec(
        spec=normalized_spec,
        role=role,
    )

    if query_hints and (
        lookup_spec.startswith("region:")
        or lookup_spec.startswith("element:")
        or parse_coord_pair(lookup_spec) is not None
    ):
        raise ResolutionError(
            f"Query hints are only supported for label matching. Received '{normalized_spec}' for {role}."
        )

    region_resolved = resolve_region_ref(
        spec=lookup_spec,
        regions=regions,
        role=role,
        edge=edge,
    )
    if region_resolved is not None:
        return region_resolved

    if lookup_spec.startswith("element:"):
        idx_text = lookup_spec.split(":", 1)[1].strip()
        try:
            idx = int(idx_text)
        except ValueError as exc:
            raise ResolutionError(f"Invalid {role} element reference '{lookup_spec}'.") from exc
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

    coord = parse_coord_pair(lookup_spec)
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

    ranked = rank_reference_candidates(
        spec=normalized_spec,
        elements=elements,
        image_width=image_width,
        image_height=image_height,
        role=role,
    )
    best = ranked[0] if ranked else None
    if best is None:
        raise ResolutionError(
            f"Unable to resolve '{normalized_spec}' for {role}. Try coordinates, element:<index>, or region:<name>."
        )

    best_score = float(best["score"])
    # Require stronger label confidence when no positional hints are supplied.
    min_score = 0.35
    if best.get("hints"):
        min_score = 0.2

    if best_score < min_score:
        hint_text = ""
        if best.get("hints"):
            hint_text = " Try adjusting near/side hints or add within:<px>."
        raise ResolutionError(
            f"Unable to confidently resolve '{normalized_spec}' for {role}. Best score={best_score:.3f} (< {min_score:.3f}).{hint_text}"
        )

    best_element = best["element"]

    point_x, point_y = bbox_point_for_edge(best_element["bbox"], edge=edge)
    return {
        "mode": "label",
        "spec": normalized_spec,
        "matched_score": round(best_score, 4),
        "matched_label_score": round(float(best["label_score"]), 4),
        "matched_near_score": round(float(best["near_score"]), 4) if best["near_score"] is not None else None,
        "matched_side_score": round(float(best["side_score"]), 4) if best["side_score"] is not None else None,
        "distance_to_near_px": round(float(best["distance_to_near_px"]), 3)
        if best["distance_to_near_px"] is not None
        else None,
        "query_hints": best.get("hints") or {},
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
