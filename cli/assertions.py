"""Assertion evaluation engine for `omni check`."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from PIL import Image

from resolution import element_center, point_in_region, resolve_reference_spec


class AssertionEvaluationError(Exception):
    """Unexpected assertion evaluation failure."""

    exit_code = 2


def _compare(operator: str, actual: float, expected: float) -> bool:
    if operator == "eq":
        return actual == expected
    if operator == "gte":
        return actual >= expected
    if operator == "lte":
        return actual <= expected
    raise AssertionEvaluationError(f"Unsupported operator '{operator}'")


def _parse_hex_color(value: str) -> tuple[int, int, int]:
    raw = value.strip()
    if len(raw) != 7 or not raw.startswith("#"):
        raise AssertionEvaluationError(f"Invalid hex color '{value}'. Expected #RRGGBB.")
    try:
        r = int(raw[1:3], 16)
        g = int(raw[3:5], 16)
        b = int(raw[5:7], 16)
    except ValueError as exc:
        raise AssertionEvaluationError(f"Invalid hex color '{value}'.") from exc
    return r, g, b


def _rgb_to_hex(rgb: tuple[int, int, int]) -> str:
    return f"#{rgb[0]:02X}{rgb[1]:02X}{rgb[2]:02X}"


def assertion_needs_parse(assertion: dict[str, Any]) -> bool:
    assertion_type = assertion["type"]
    if assertion_type in {"element_count", "elements_in_region"}:
        return True
    if assertion_type == "measurement":
        from_ref = str(assertion["from"]["ref"])
        to_ref = str(assertion["to"]["ref"])
        for ref in (from_ref, to_ref):
            if ref.startswith("element:"):
                return True
            if ref.startswith("region:"):
                continue
            if "," in ref:
                # raw x,y coordinate pair
                continue
            # fallback label matching requires parsed elements
            return True
        return False
    return False


@dataclass
class AssertionContext:
    image_path: Path
    image_width: int
    image_height: int
    regions: dict[str, dict[str, Any]]
    elements: list[dict[str, Any]]


def evaluate_assertion(assertion: dict[str, Any], ctx: AssertionContext) -> dict[str, Any]:
    assertion_id = str(assertion["id"])
    assertion_type = str(assertion["type"])
    description = assertion.get("description")

    base = {
        "id": assertion_id,
        "description": description,
        "type": assertion_type,
        "passed": False,
    }

    if assertion_type == "region_dimension":
        region_name = str(assertion["region"])
        region = ctx.regions[region_name]
        expected = float(assertion["expected"])
        tolerance = float(assertion["tolerance"])
        prop = str(assertion["property"])
        actual = float(region["w"] if prop == "width" else region["h"])
        delta = actual - expected
        passed = abs(delta) <= tolerance
        details = (
            f"{'PASS' if passed else 'FAIL'}: region '{region_name}' {prop} is {actual:.0f}px, "
            f"expected {expected:.0f}px ±{tolerance:.0f}px (delta {delta:+.0f}px)."
        )
        return {
            **base,
            "passed": passed,
            "expected": expected,
            "actual": actual,
            "delta": delta,
            "tolerance": tolerance,
            "details": details,
        }

    if assertion_type == "measurement":
        expected = float(assertion["expected"])
        tolerance = float(assertion["tolerance"])
        axis = str(assertion["axis"])
        if axis == "both":
            axis = "euclidean"

        from_spec = str(assertion["from"]["ref"])
        to_spec = str(assertion["to"]["ref"])
        from_edge = str(assertion["from"].get("edge", "center"))
        to_edge = str(assertion["to"].get("edge", "center"))

        from_resolved = resolve_reference_spec(
            spec=from_spec,
            elements=ctx.elements,
            image_width=ctx.image_width,
            image_height=ctx.image_height,
            edge=from_edge,
            role=f"assertion:{assertion_id}:from",
            regions=ctx.regions,
        )
        to_resolved = resolve_reference_spec(
            spec=to_spec,
            elements=ctx.elements,
            image_width=ctx.image_width,
            image_height=ctx.image_height,
            edge=to_edge,
            role=f"assertion:{assertion_id}:to",
            regions=ctx.regions,
        )

        fx = int(from_resolved["resolved_point"]["x"])
        fy = int(from_resolved["resolved_point"]["y"])
        tx = int(to_resolved["resolved_point"]["x"])
        ty = int(to_resolved["resolved_point"]["y"])
        dx = tx - fx
        dy = ty - fy

        if axis == "x":
            actual = float(abs(dx))
        elif axis == "y":
            actual = float(abs(dy))
        else:
            actual = float((dx * dx + dy * dy) ** 0.5)

        delta = actual - expected
        passed = abs(delta) <= tolerance
        details = (
            f"{'PASS' if passed else 'FAIL'}: {axis} distance is {actual:.3f}px between "
            f"{from_spec} ({fx},{fy}) and {to_spec} ({tx},{ty}); expected {expected:.3f}px ±{tolerance:.3f}px "
            f"(delta {delta:+.3f}px)."
        )
        return {
            **base,
            "passed": passed,
            "expected": expected,
            "actual": actual,
            "delta": delta,
            "tolerance": tolerance,
            "axis": axis,
            "details": details,
            "resolved": {
                "from": from_resolved,
                "to": to_resolved,
                "delta_px": {"x": dx, "y": dy},
            },
        }

    if assertion_type == "element_count":
        operator = str(assertion["operator"])
        expected = int(assertion["expected"])
        actual = len(ctx.elements)
        passed = _compare(operator, actual, expected)
        details = (
            f"{'PASS' if passed else 'FAIL'}: detected {actual} elements, "
            f"expected {operator} {expected}."
        )
        return {
            **base,
            "passed": passed,
            "operator": operator,
            "expected": expected,
            "actual": actual,
            "details": details,
        }

    if assertion_type == "elements_in_region":
        region_name = str(assertion["region"])
        region = ctx.regions[region_name]
        operator = str(assertion["operator"])
        expected = int(assertion["expected"])

        count = 0
        for element in ctx.elements:
            cx, cy = element_center(element)
            if point_in_region(x=cx, y=cy, region=region):
                count += 1

        passed = _compare(operator, count, expected)
        details = (
            f"{'PASS' if passed else 'FAIL'}: region '{region_name}' contains {count} element centers; "
            f"expected {operator} {expected}. Region bounds: x={region['x']}, y={region['y']}, "
            f"w={region['w']}, h={region['h']}."
        )
        return {
            **base,
            "passed": passed,
            "operator": operator,
            "expected": expected,
            "actual": count,
            "region": region_name,
            "details": details,
        }

    if assertion_type == "region_color_dominant":
        region_name = str(assertion["region"])
        region = ctx.regions[region_name]
        expected_rgb = _parse_hex_color(str(assertion["expected_hex"]))
        tolerance_delta = int(assertion["tolerance_delta"])

        image = Image.open(ctx.image_path).convert("RGB")
        x1 = int(region["x"])
        y1 = int(region["y"])
        x2 = x1 + int(region["w"])
        y2 = y1 + int(region["h"])

        x1_clamped = max(0, min(ctx.image_width, x1))
        y1_clamped = max(0, min(ctx.image_height, y1))
        x2_clamped = max(0, min(ctx.image_width, x2))
        y2_clamped = max(0, min(ctx.image_height, y2))

        if x2_clamped <= x1_clamped or y2_clamped <= y1_clamped:
            return {
                **base,
                "passed": False,
                "expected": str(assertion["expected_hex"]).upper(),
                "actual": None,
                "tolerance_delta": tolerance_delta,
                "details": (
                    f"FAIL: region '{region_name}' falls outside the image bounds and cannot be sampled for color."
                ),
            }

        cropped = image.crop((x1_clamped, y1_clamped, x2_clamped, y2_clamped))
        pixels = list(cropped.getdata())
        if not pixels:
            raise AssertionEvaluationError(
                f"Region '{region_name}' yielded zero pixels during color evaluation."
            )

        r_mean = int(round(sum(px[0] for px in pixels) / len(pixels)))
        g_mean = int(round(sum(px[1] for px in pixels) / len(pixels)))
        b_mean = int(round(sum(px[2] for px in pixels) / len(pixels)))
        actual_rgb = (r_mean, g_mean, b_mean)

        dr = abs(actual_rgb[0] - expected_rgb[0])
        dg = abs(actual_rgb[1] - expected_rgb[1])
        db = abs(actual_rgb[2] - expected_rgb[2])
        passed = dr <= tolerance_delta and dg <= tolerance_delta and db <= tolerance_delta
        details = (
            f"{'PASS' if passed else 'FAIL'}: dominant region color is {_rgb_to_hex(actual_rgb)} "
            f"(rgb={actual_rgb}); expected {_rgb_to_hex(expected_rgb)} (rgb={expected_rgb}) with per-channel "
            f"tolerance <= {tolerance_delta}. Deltas: (R={dr}, G={dg}, B={db})."
        )
        return {
            **base,
            "passed": passed,
            "expected": _rgb_to_hex(expected_rgb),
            "actual": _rgb_to_hex(actual_rgb),
            "actual_rgb": list(actual_rgb),
            "expected_rgb": list(expected_rgb),
            "channel_deltas": {"r": dr, "g": dg, "b": db},
            "tolerance_delta": tolerance_delta,
            "details": details,
        }

    raise AssertionEvaluationError(f"Unhandled assertion type '{assertion_type}'")

