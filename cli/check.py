"""`omni check` command implementation."""

from __future__ import annotations

import json
import time
import argparse
import hashlib
import base64
from pathlib import Path
from typing import Any

from PIL import Image

from assertions import AssertionContext, assertion_needs_parse, evaluate_assertion
from config import OmniConfigRequiredError, ProjectConfig


class CheckCommandError(Exception):
    """Input/runtime error for check command."""

    exit_code = 1


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while True:
            chunk = handle.read(1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def _save_annotated_image(annotated_b64: str, output_path: Path) -> Path:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_bytes(base64.b64decode(annotated_b64))
    return output_path.resolve()


def add_check_subparser(
    *,
    subparsers: Any,
    add_trailing_global_flags: Any,
    default_box_threshold: float,
) -> Any:
    epilog = (
        "Examples:\n"
        "  omni check screenshot.png --quiet\n"
        "  omni check screenshot.png --only sidebar-width,topbar-height\n"
        "  omni check screenshot.png --skip min-elements --save-report /tmp/report.json"
    )
    parser = subparsers.add_parser(
        "check",
        help="Run project assertions from .omni.json against a screenshot.",
        epilog=epilog,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    add_trailing_global_flags(parser)
    parser.add_argument("image", help="Path to screenshot image.")
    parser.add_argument(
        "--only",
        help="Comma-separated assertion IDs to run exclusively.",
    )
    parser.add_argument(
        "--skip",
        help="Comma-separated assertion IDs to skip.",
    )
    parser.add_argument(
        "--save-report",
        help="Write JSON check report to this path in addition to stdout.",
    )
    parser.add_argument(
        "--confidence-threshold",
        type=float,
        default=default_box_threshold,
        help=(
            "Detection threshold for parse-backed assertions "
            f"(default: {default_box_threshold})."
        ),
    )
    parser.add_argument(
        "--save-annotated",
        help="Save OmniParser annotated image used during check to this path.",
    )
    return parser


def _split_ids(raw: str | None) -> set[str]:
    if not raw:
        return set()
    return {item.strip() for item in raw.split(",") if item.strip()}


def run_check_command(
    *,
    args: Any,
    runtime: Any,
    project_config: ProjectConfig | None,
    image_path: Path,
    response_context: Any,
    config_sha256: str | None,
    meta_builder: Any,
    cli_version: str,
) -> tuple[dict[str, Any], int]:
    if project_config is None:
        raise OmniConfigRequiredError(
            "`omni check` requires a project config. Provide --config <path> or create .omni.json."
        )

    started_ms = time.perf_counter() * 1000.0
    image_sha256 = _sha256_file(image_path)

    all_assertions = list(project_config.assertions)
    all_ids = {str(item["id"]) for item in all_assertions}
    only_ids = _split_ids(args.only)
    skip_ids = _split_ids(args.skip)

    if only_ids:
        unknown_only = sorted(only_ids - all_ids)
        if unknown_only:
            raise CheckCommandError(
                f"--only contains unknown assertion IDs: {', '.join(unknown_only)}"
            )

    unknown_skip = sorted(skip_ids - all_ids)
    if unknown_skip:
        raise CheckCommandError(
            f"--skip contains unknown assertion IDs: {', '.join(unknown_skip)}"
        )

    selected_assertions = [
        assertion
        for assertion in all_assertions
        if (not only_ids or assertion["id"] in only_ids)
    ]

    evaluate_queue = [a for a in selected_assertions if a["id"] not in skip_ids]
    parse_needed = any(
        assertion_needs_parse(assertion, project_config.targets)
        for assertion in evaluate_queue
    )

    cache_hit = False
    elements: list[dict[str, Any]] = []
    parsed_data: dict[str, Any] | None = None

    if parse_needed:
        parsed_data, cache_hit, _logs = runtime.parse_image(
            image_path=image_path,
            box_threshold=args.confidence_threshold,
            use_cache=args.cache,
        )
        image_width = int(parsed_data["image_width"])
        image_height = int(parsed_data["image_height"])
        elements = list(parsed_data["elements"])
    else:
        image = Image.open(image_path)
        image_width, image_height = image.size

    if args.save_annotated and parsed_data is None:
        parsed_data, cache_hit, _logs = runtime.parse_image(
            image_path=image_path,
            box_threshold=args.confidence_threshold,
            use_cache=args.cache,
        )
        image_width = int(parsed_data["image_width"])
        image_height = int(parsed_data["image_height"])
        elements = list(parsed_data["elements"])

    assertion_ctx = AssertionContext(
        image_path=image_path,
        image_width=image_width,
        image_height=image_height,
        regions=project_config.regions,
        targets=project_config.targets,
        elements=elements,
    )

    results: list[dict[str, Any]] = []
    passed = 0
    failed = 0
    skipped = 0

    for assertion in selected_assertions:
        assertion_id = str(assertion["id"])
        if assertion_id in skip_ids:
            skipped += 1
            results.append(
                {
                    "id": assertion_id,
                    "description": assertion.get("description"),
                    "type": assertion["type"],
                    "passed": None,
                    "skipped": True,
                    "details": f"Skipped via --skip filter ({assertion_id}).",
                }
            )
            continue

        try:
            result = evaluate_assertion(assertion, assertion_ctx)
            results.append(result)
            if result.get("passed"):
                passed += 1
            else:
                failed += 1
        except Exception as exc:
            failed += 1
            results.append(
                {
                    "id": assertion_id,
                    "description": assertion.get("description"),
                    "type": assertion["type"],
                    "passed": False,
                    "details": f"FAIL: assertion evaluation error: {exc}",
                }
            )

    result = "pass" if failed == 0 else "fail"
    processing_time_ms = int(round((time.perf_counter() * 1000.0) - started_ms))

    payload = {
        "schema_version": "1.1",
        "command": str(response_context.command),
        "request_id": str(response_context.request_id),
        "timestamp_utc": str(response_context.timestamp_utc),
        "status": "success",
        "result": result,
        "error": None,
        "warnings": [],
        "summary": {
            "total": len(selected_assertions),
            "passed": passed,
            "failed": failed,
            "skipped": skipped,
        },
        "image": str(image_path),
        "config": str(project_config.path),
        "results": results,
        "meta": meta_builder(
            response_context=response_context,
            image_path=str(image_path),
            image_width=image_width,
            image_height=image_height,
            processing_time_ms=processing_time_ms,
            omniparser_version=runtime.omniparser_version,
            cli_version=cli_version,
            cache_hit=cache_hit,
            extra={
                "config_path": str(project_config.path),
                "elements_detected": len(elements),
                "parse_required": parse_needed,
                "parse_performed": bool(parsed_data is not None),
                "image_sha256": image_sha256,
                "config_sha256": config_sha256,
            },
        ),
    }

    if args.save_report:
        output_path = Path(args.save_report).expanduser().resolve()
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(json.dumps(payload, ensure_ascii=False, sort_keys=True), encoding="utf-8")
        payload["meta"]["report_path"] = str(output_path)

    if args.save_annotated:
        if parsed_data is None:
            raise CheckCommandError(
                "Unable to generate annotated image for check results."
            )
        annotated_path = _save_annotated_image(
            parsed_data["annotated_image_base64"],
            Path(args.save_annotated).expanduser().resolve(),
        )
        payload["meta"]["annotated_path"] = str(annotated_path)

    exit_code = 0 if result == "pass" else 4
    return payload, exit_code
