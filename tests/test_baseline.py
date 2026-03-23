from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

from PIL import Image


REPO_ROOT = Path(__file__).resolve().parents[1]
CLI_DIR = REPO_ROOT / "cli"
CALIPER_MODULE_PATH = CLI_DIR / "caliper.py"


def _load_caliper_module():
    if str(CLI_DIR) not in sys.path:
        sys.path.insert(0, str(CLI_DIR))

    spec = importlib.util.spec_from_file_location("caliper_cli", CALIPER_MODULE_PATH)
    assert spec is not None
    assert spec.loader is not None

    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_baseline_writes_config_and_emits_contract(tmp_path, monkeypatch, capsys):
    caliper = _load_caliper_module()

    width, height = 320, 180
    image_path = tmp_path / "reference.png"
    Image.new("RGB", (width, height), (16, 24, 40)).save(image_path)

    output_config = tmp_path / "generated.caliper.json"

    parsed_data = {
        "image_width": width,
        "image_height": height,
        "elements": [
            {
                "index": 0,
                "element_id": "e_button_primary",
                "label": "Primary",
                "bbox": {"x": 10, "y": 12, "width": 80, "height": 24},
            },
            {
                "index": 1,
                "element_id": "e_button_secondary",
                "label": "Secondary",
                "bbox": {"x": 110, "y": 12, "width": 90, "height": 24},
            },
            {
                "index": 2,
                "element_id": "e_footer",
                "label": "Footer",
                "bbox": {"x": 10, "y": 72, "width": 190, "height": 30},
            },
        ],
    }

    class FakeRuntime:
        def __init__(
            self,
            *,
            repo_root,
            model_dir,
            requested_device,
            logger,
            omniparser_version,
            engine,
            uied_root,
            uied_text_engine,
        ):
            self.omniparser_version = omniparser_version

        def parse_image(self, *, image_path, box_threshold, use_cache):
            return parsed_data, True, ""

    monkeypatch.setattr(caliper, "OmniRuntime", FakeRuntime)
    monkeypatch.setattr(caliper, "_resolve_runtime_root", lambda *args, **kwargs: tmp_path)
    monkeypatch.setattr(caliper, "_omniparser_version", lambda *_args, **_kwargs: "test-omniparser")

    exit_code = caliper.main(
        [
            "baseline",
            str(image_path),
            "--save-config",
            str(output_config),
            "--project-name",
            "baseline-test",
            "--tolerance",
            "7",
            "--quiet",
        ]
    )

    stdout = capsys.readouterr().out
    payload = json.loads(stdout)

    assert exit_code == 0
    assert payload["schema_version"] == "1.1"
    assert payload["command"] == "baseline"
    assert payload["status"] == "success"

    assert output_config.exists(), "baseline should write config file"
    config = json.loads(output_config.read_text(encoding="utf-8"))

    for required_key in ("version", "viewport", "assertions", "reference_image"):
        assert required_key in config

    assert config["viewport"]["width"] == width
    assert config["viewport"]["height"] == height

    assertion_ids = [assertion["id"] for assertion in config["assertions"]]
    assert len(assertion_ids) == len(set(assertion_ids)), "assertion ids must be unique"
