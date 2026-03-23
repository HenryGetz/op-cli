from __future__ import annotations

import json
import os
import socket
import stat
import subprocess
import sys
import textwrap
import time
from pathlib import Path

import pytest
import requests


REPO_ROOT = Path(__file__).resolve().parents[1]


def _get_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _write_stub_caliper(stub_path: Path) -> None:
    script = textwrap.dedent(
        """\
        #!/usr/bin/env python3
        import json
        import pathlib
        import sys

        def emit(payload, code=0):
            print(json.dumps(payload))
            raise SystemExit(code)

        args = sys.argv[1:]
        if not args:
            emit({"status": "error", "error": {"message": "missing command"}, "command": "unknown", "schema_version": "1.1"}, 2)

        command = args[0]
        base = {
            "schema_version": "1.1",
            "command": command,
            "request_id": "stub-request-id",
            "timestamp_utc": "2026-03-22T00:00:00Z",
            "warnings": [],
            "meta": {
                "command": command,
                "request_id": "stub-request-id",
                "timestamp_utc": "2026-03-22T00:00:00Z",
            },
        }

        if command == "doctor":
            payload = dict(base)
            payload.update({"status": "success", "error": None, "doctor": {"status": "ok"}})
            emit(payload, 0)

        if command == "parse":
            payload = dict(base)
            payload.update(
                {
                    "status": "success",
                    "error": None,
                    "image_width": 64,
                    "image_height": 64,
                    "elements": [],
                }
            )
            emit(payload, 0)

        if command == "check":
            config_path = None
            for idx, token in enumerate(args):
                if token == "--config" and idx + 1 < len(args):
                    config_path = args[idx + 1]
                    break

            if not config_path or not pathlib.Path(config_path).exists():
                payload = dict(base)
                payload.update(
                    {
                        "status": "error",
                        "error": {
                            "type": "FileMissingError",
                            "message": "Config file not found",
                            "exit_code": 3,
                        },
                    }
                )
                emit(payload, 3)

            payload = dict(base)
            payload.update(
                {
                    "status": "success",
                    "error": None,
                    "check": {"passed": 1, "failed": 0, "results": []},
                }
            )
            emit(payload, 0)

        payload = dict(base)
        payload.update(
            {
                "status": "error",
                "error": {
                    "type": "UnsupportedCommand",
                    "message": f"unsupported command: {command}",
                    "exit_code": 2,
                },
            }
        )
        emit(payload, 2)
        """
    )
    stub_path.write_text(script, encoding="utf-8")
    current_mode = stub_path.stat().st_mode
    stub_path.chmod(current_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


@pytest.fixture(scope="module")
def server_base_url(tmp_path_factory):
    tmp_dir = tmp_path_factory.mktemp("caliper-server-test")
    stub_caliper = tmp_dir / "caliper-stub"
    _write_stub_caliper(stub_caliper)

    port = _get_free_port()
    env = os.environ.copy()
    env["CALIPER_BIN"] = str(stub_caliper)
    env["CALIPER_WORK_DIR"] = str(tmp_dir / "work")
    env["SERVER_PORT"] = str(port)

    process = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "uvicorn",
            "server.main:app",
            "--host",
            "127.0.0.1",
            "--port",
            str(port),
        ],
        cwd=str(REPO_ROOT),
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )

    base_url = f"http://127.0.0.1:{port}"
    start_deadline = time.time() + 25
    last_error = None
    while time.time() < start_deadline:
        try:
            response = requests.get(f"{base_url}/health", timeout=1)
            if response.status_code in {200, 422}:
                break
        except Exception as exc:  # noqa: BLE001
            last_error = exc
            time.sleep(0.2)
    else:
        process.terminate()
        stdout, stderr = process.communicate(timeout=5)
        raise RuntimeError(
            f"server failed to start: last_error={last_error}\nstdout={stdout}\nstderr={stderr}"
        )

    try:
        yield base_url
    finally:
        process.terminate()
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)


def test_health_returns_200(server_base_url):
    response = requests.get(f"{server_base_url}/health", timeout=10)
    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == "success"
    assert payload["http_status"] == 200


def test_parse_with_real_png_returns_success(server_base_url):
    image_path = REPO_ROOT / "cli-debug-inputs" / "acist-1280x800-playwright.png"
    assert image_path.exists(), "expected PNG fixture image at cli-debug-inputs/"

    with image_path.open("rb") as image_file:
        response = requests.post(
            f"{server_base_url}/parse",
            files={"image": (image_path.name, image_file, "image/png")},
            timeout=20,
        )

    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == "success"
    assert payload["http_status"] == 200


def test_check_missing_config_returns_422_with_forwarded_error(server_base_url):
    image_path = REPO_ROOT / "cli-debug-inputs" / "acist-1280x800-playwright.png"

    with image_path.open("rb") as image_file:
        response = requests.post(
            f"{server_base_url}/check",
            files={"image": (image_path.name, image_file, "image/png")},
            timeout=20,
        )

    assert response.status_code == 422
    payload = response.json()
    assert isinstance(payload.get("error"), dict)
    assert payload["error"].get("message") == "Config file not found"
    assert payload["http_status"] == 422

