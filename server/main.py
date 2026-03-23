from __future__ import annotations

import base64
import json
import os
import shutil
import subprocess
import tempfile
import uuid
from pathlib import Path
from typing import Any

from fastapi import FastAPI, File, Form, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse


def _resolve_caliper_bin() -> str:
    explicit = os.environ.get("CALIPER_BIN")
    if explicit:
        return str(Path(explicit).expanduser().resolve())

    local_default = (Path(__file__).resolve().parents[1] / "bin" / "caliper").resolve()
    if local_default.exists():
        return str(local_default)

    discovered = shutil.which("caliper")
    if discovered:
        return discovered

    raise RuntimeError(
        "Unable to resolve CALIPER_BIN. Set CALIPER_BIN or ensure ../bin/caliper (relative to server/) exists."
    )


def _work_root() -> Path:
    configured = os.environ.get("CALIPER_WORK_DIR")
    root = Path(configured).expanduser() if configured else Path(tempfile.gettempdir()) / "caliper-server"
    resolved = root.resolve()
    resolved.mkdir(parents=True, exist_ok=True)
    return resolved


def _new_request_dir(prefix: str) -> Path:
    request_dir = _work_root() / f"{prefix}-{uuid.uuid4().hex}"
    request_dir.mkdir(parents=True, exist_ok=True)
    return request_dir


async def _save_upload(upload: UploadFile, destination: Path) -> Path:
    destination.parent.mkdir(parents=True, exist_ok=True)
    content = await upload.read()
    destination.write_bytes(content)
    await upload.close()
    return destination


def _parse_cli_payload(*, stdout: str, stderr: str, command: str, exit_code: int) -> dict[str, Any]:
    text = stdout.strip()
    candidate = text.splitlines()[-1] if text else ""
    if candidate:
        try:
            payload = json.loads(candidate)
            if isinstance(payload, dict):
                return payload
        except json.JSONDecodeError:
            pass

    message = stderr.strip() or candidate or f"caliper {command} produced no JSON output"
    return {
        "schema_version": "1.1",
        "command": command,
        "status": "error",
        "error": {
            "type": "CLIOutputError",
            "message": message,
            "exit_code": exit_code,
        },
        "warnings": [],
        "meta": {
            "command": command,
        },
    }


def _run_caliper(*, command: str, args: list[str], cwd: Path) -> tuple[int, dict[str, Any]]:
    process = subprocess.run(
        [_resolve_caliper_bin(), command, *args],
        cwd=str(cwd),
        capture_output=True,
        text=True,
        check=False,
    )
    payload = _parse_cli_payload(
        stdout=process.stdout,
        stderr=process.stderr,
        command=command,
        exit_code=process.returncode,
    )
    return process.returncode, payload


def _json_response(*, exit_code: int, payload: dict[str, Any]) -> JSONResponse:
    if exit_code == 0:
        body = dict(payload)
        body["http_status"] = 200
        return JSONResponse(status_code=200, content=body)

    body = dict(payload)
    if not isinstance(body.get("error"), dict):
        body["error"] = {
            "type": "CLIError",
            "message": f"caliper command failed with exit code {exit_code}",
            "exit_code": exit_code,
        }
    body["http_status"] = 422
    return JSONResponse(status_code=422, content=body)


def _attach_base64_image(payload: dict[str, Any], *, field_name: str, image_path: Path) -> None:
    if not image_path.exists() or not image_path.is_file():
        return
    payload[field_name] = base64.b64encode(image_path.read_bytes()).decode("ascii")


app = FastAPI(title="CaliperUI Server", version="1.0.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/health")
async def health() -> JSONResponse:
    work_dir = _new_request_dir("health")
    code, payload = _run_caliper(command="doctor", args=["--quiet"], cwd=work_dir)
    return _json_response(exit_code=code, payload=payload)


@app.post("/parse")
async def parse(
    image: UploadFile = File(...),
    confidence_threshold: float | None = Form(None),
) -> JSONResponse:
    work_dir = _new_request_dir("parse")
    image_path = await _save_upload(image, work_dir / image.filename)

    cli_args = [str(image_path), "--quiet"]
    if confidence_threshold is not None:
        cli_args.extend(["--confidence-threshold", str(confidence_threshold)])

    code, payload = _run_caliper(command="parse", args=cli_args, cwd=work_dir)
    return _json_response(exit_code=code, payload=payload)


@app.post("/debug")
async def debug(
    image: UploadFile = File(...),
    confidence_threshold: float | None = Form(None),
    max_elements: int | None = Form(None),
) -> JSONResponse:
    work_dir = _new_request_dir("debug")
    image_path = await _save_upload(image, work_dir / image.filename)
    output_path = work_dir / "debug.png"

    cli_args = [str(image_path), "-o", str(output_path), "--quiet"]
    if confidence_threshold is not None:
        cli_args.extend(["--confidence-threshold", str(confidence_threshold)])
    if max_elements is not None:
        cli_args.extend(["--max-elements", str(max_elements)])

    code, payload = _run_caliper(command="debug", args=cli_args, cwd=work_dir)
    if code == 0:
        _attach_base64_image(payload, field_name="annotated_image_base64", image_path=output_path)
    return _json_response(exit_code=code, payload=payload)


@app.post("/check")
async def check(
    image: UploadFile = File(...),
    config_json: str | None = Form(None),
    config_file: UploadFile | None = File(None),
    only: str | None = Form(None),
    skip: str | None = Form(None),
) -> JSONResponse:
    work_dir = _new_request_dir("check")
    image_path = await _save_upload(image, work_dir / image.filename)

    cli_args = [str(image_path), "--quiet"]

    config_path: Path | None = None
    if config_json:
        config_path = work_dir / ".caliper.json"
        try:
            parsed = json.loads(config_json)
        except json.JSONDecodeError:
            return JSONResponse(
                status_code=422,
                content={
                    "status": "error",
                    "error": {
                        "type": "UserInputError",
                        "message": "config_json must be valid JSON.",
                    },
                    "http_status": 422,
                },
            )
        config_path.write_text(json.dumps(parsed, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    elif config_file is not None:
        config_path = await _save_upload(config_file, work_dir / (config_file.filename or ".caliper.json"))

    if config_path is not None:
        cli_args.extend(["--config", str(config_path)])
    if only:
        cli_args.extend(["--only", only])
    if skip:
        cli_args.extend(["--skip", skip])

    code, payload = _run_caliper(command="check", args=cli_args, cwd=work_dir)
    return _json_response(exit_code=code, payload=payload)


@app.post("/baseline")
async def baseline(
    image: UploadFile = File(...),
    project_name: str | None = Form(None),
    tolerance: int | None = Form(None),
    custom_regions: str | None = Form(None),
) -> JSONResponse:
    work_dir = _new_request_dir("baseline")
    image_path = await _save_upload(image, work_dir / image.filename)
    config_path = work_dir / ".caliper.json"

    cli_args = [str(image_path), "--save-config", str(config_path), "--quiet"]
    if project_name:
        cli_args.extend(["--project-name", project_name])
    if tolerance is not None:
        cli_args.extend(["--tolerance", str(tolerance)])

    code, payload = _run_caliper(command="baseline", args=cli_args, cwd=work_dir)
    if code == 0 and custom_regions:
        try:
            payload["custom_regions"] = json.loads(custom_regions)
        except json.JSONDecodeError:
            payload["custom_regions"] = custom_regions
    return _json_response(exit_code=code, payload=payload)


@app.post("/diff")
async def diff(
    image1: UploadFile = File(...),
    image2: UploadFile = File(...),
    tolerance: int | None = Form(None),
) -> JSONResponse:
    work_dir = _new_request_dir("diff")
    image1_path = await _save_upload(image1, work_dir / image1.filename)
    image2_path = await _save_upload(image2, work_dir / image2.filename)

    cli_args = [str(image1_path), str(image2_path), "--quiet"]
    if tolerance is not None:
        cli_args.extend(["--tolerance", str(tolerance)])

    code, payload = _run_caliper(command="diff", args=cli_args, cwd=work_dir)
    return _json_response(exit_code=code, payload=payload)


@app.post("/overlay")
async def overlay(
    image1: UploadFile = File(...),
    image2: UploadFile = File(...),
    opacity: float | None = Form(None),
) -> JSONResponse:
    work_dir = _new_request_dir("overlay")
    image1_path = await _save_upload(image1, work_dir / image1.filename)
    image2_path = await _save_upload(image2, work_dir / image2.filename)
    output_path = work_dir / "overlay.png"

    cli_args = [str(image1_path), str(image2_path), "-o", str(output_path), "--quiet"]
    if opacity is not None:
        cli_args.extend(["--opacity", str(opacity)])

    code, payload = _run_caliper(command="overlay", args=cli_args, cwd=work_dir)
    if code == 0:
        _attach_base64_image(payload, field_name="overlay_image_base64", image_path=output_path)
    return _json_response(exit_code=code, payload=payload)


@app.post("/crop")
async def crop(
    image: UploadFile = File(...),
    region: str | None = Form(None),
    region_name: str | None = Form(None),
    element: int | None = Form(None),
    padding: int | None = Form(None),
) -> JSONResponse:
    work_dir = _new_request_dir("crop")
    image_path = await _save_upload(image, work_dir / image.filename)
    output_path = work_dir / "crop.png"

    cli_args = [str(image_path), "-o", str(output_path), "--quiet"]

    selectors = [
        int(region is not None),
        int(region_name is not None),
        int(element is not None),
    ]
    if sum(selectors) != 1:
        return JSONResponse(
            status_code=422,
            content={
                "status": "error",
                "error": {
                    "type": "UserInputError",
                    "message": "Provide exactly one of region, region_name, or element for /crop.",
                },
                "http_status": 422,
            },
        )

    if region is not None:
        cli_args.extend(["--region", region])
    if region_name is not None:
        cli_args.extend(["--region-name", region_name])
    if element is not None:
        cli_args.extend(["--element", str(element)])
    if padding is not None:
        cli_args.extend(["--padding", str(padding)])

    code, payload = _run_caliper(command="crop", args=cli_args, cwd=work_dir)
    if code == 0:
        _attach_base64_image(payload, field_name="cropped_image_base64", image_path=output_path)
    return _json_response(exit_code=code, payload=payload)


def run() -> None:
    import uvicorn

    port = int(os.environ.get("SERVER_PORT", "7771"))
    uvicorn.run("server.main:app", host="0.0.0.0", port=port)


if __name__ == "__main__":
    run()
