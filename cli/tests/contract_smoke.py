#!/usr/bin/env python3
"""Light contract regression checks for omni CLI JSON envelopes.

Usage:
  OMNI_TEST_IMAGE=/abs/path/image.png python3 cli/tests/contract_smoke.py
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path


EXPECTED_SCHEMA_VERSION = "1.1"


def run(cmd: list[str], cwd: Path) -> tuple[int, str, str]:
    result = subprocess.run(cmd, cwd=str(cwd), capture_output=True, text=True)
    return result.returncode, result.stdout, result.stderr


def require(cond: bool, message: str) -> None:
    if not cond:
        raise AssertionError(message)


def parse_json(stdout: str) -> dict:
    return json.loads(stdout)


def check_envelope(payload: dict, command: str) -> None:
    require(payload.get("schema_version") == EXPECTED_SCHEMA_VERSION, f"{command}: schema_version mismatch")
    require(payload.get("command") == command, f"{command}: command mismatch")
    require(isinstance(payload.get("request_id"), str) and payload["request_id"], f"{command}: request_id missing")
    require(isinstance(payload.get("timestamp_utc"), str) and payload["timestamp_utc"].endswith("Z"), f"{command}: timestamp_utc missing")
    require(payload.get("status") in {"success", "error"}, f"{command}: status invalid")
    require("warnings" in payload and isinstance(payload["warnings"], list), f"{command}: warnings missing")
    meta = payload.get("meta", {})
    require(meta.get("command") == command, f"{command}: meta.command mismatch")
    require(meta.get("request_id") == payload.get("request_id"), f"{command}: request_id mismatch")
    require(meta.get("timestamp_utc") == payload.get("timestamp_utc"), f"{command}: timestamp mismatch")


def main() -> int:
    image = os.environ.get("OMNI_TEST_IMAGE")
    if not image:
        raise SystemExit("Set OMNI_TEST_IMAGE=/absolute/path/to/image before running")
    image_path = Path(image).expanduser().resolve()
    if not image_path.exists():
        raise SystemExit(f"OMNI_TEST_IMAGE not found: {image_path}")

    work = Path("/tmp/omni-contract-smoke").resolve()
    work.mkdir(parents=True, exist_ok=True)
    config_path = work / ".omni.json"
    config_path.write_text(
        json.dumps(
            {
                "version": 1,
                "project_name": "contract-smoke",
                "regions": {
                    "left": {"x": 0, "y": 0, "w": 100, "h": 100},
                    "right": {"x": 100, "y": 0, "w": 100, "h": 100},
                },
                "targets": {
                    "left-rail": "region:left",
                    "right-rail": "region:right",
                    "probe": "label:*|side:right|near:120,50",
                },
                "assertions": [
                    {
                        "id": "region-width",
                        "type": "region_dimension",
                        "region": "left",
                        "property": "width",
                        "expected": 100,
                        "tolerance": 0,
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    checks = [
        ("parse", ["omni", "parse", str(image_path), "--quiet"]),
        ("debug", ["omni", "debug", str(image_path), "-o", str(work / "debug.png"), "--quiet"]),
        ("locate", ["omni", "locate", str(image_path), "--query", "target:probe", "--quiet"]),
        ("match", ["omni", "match", str(image_path), str(image_path), "--query", "target:probe", "--anchor", "target:left-rail", "--quiet"]),
        ("measure", ["omni", "measure", str(image_path), "--from", "target:left-rail", "--to", "target:right-rail", "--quiet"]),
        ("crop", ["omni", "crop", str(image_path), "--region", "0,0,20,20", "-o", str(work / "crop.png"), "--quiet"]),
        ("diff", ["omni", "diff", str(image_path), str(image_path), "--quiet"]),
        ("info", ["omni", "info", str(image_path), "--quiet"]),
        ("check", ["omni", "check", str(image_path), "--config", str(config_path), "--quiet"]),
        ("overlay", ["omni", "overlay", str(image_path), str(image_path), "-o", str(work / "overlay.png"), "--quiet"]),
    ]

    for command, cmd in checks:
        code, stdout, stderr = run(cmd, cwd=work)
        require(code in {0, 4}, f"{command}: unexpected exit code {code}")
        require(stderr == "", f"{command}: expected quiet stderr")
        payload = parse_json(stdout)
        check_envelope(payload, command)

    for command in ["parse", "debug", "locate", "match", "measure", "crop", "diff", "info", "check", "overlay"]:
        code, stdout, stderr = run(["omni", command, "--schema"], cwd=work)
        require(code == 0, f"schema:{command}: exit code")
        require(stderr == "", f"schema:{command}: expected empty stderr")
        payload = parse_json(stdout)
        require(payload.get("status") == "success", f"schema:{command}: expected success")
        require(payload.get("command") == command, f"schema:{command}: command mismatch")
        require(Path(payload["schema"]["path"]).exists(), f"schema:{command}: path does not exist")

    print("contract smoke: PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
