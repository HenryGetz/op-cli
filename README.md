# Omni CLI (`omni`)

Production CLI wrapper around OmniParser for deterministic, script-friendly usage by humans and AI agents.

## Installation

`omni.py` lives at `cli/omni.py` and is launched by the POSIX wrapper at `bin/omni`.

Recommended setup:

```sh
ln -sf /home/wavy/ai/op-cli/bin/omni ~/bin/omni
chmod +x ~/bin/omni
```

The wrapper auto-discovers both:

- CLI root (folder containing `cli/omni.py`)
- OmniParser runtime root (folder containing `util/utils.py`)

Discovery sources (in order):

1. Env vars
2. `~/.config/omni/install.env`
3. Common local install paths

Optional `~/.config/omni/install.env` example:

```sh
OMNI_CLI_ROOT=/path/to/op-cli
OMNIPARSER_ROOT=/path/to/OmniParser
OMNI_PYTHON=/path/to/python
```

Optional environment overrides:

- `OMNI_CLI_ROOT` (explicit CLI root containing `cli/omni.py`)
- `OMNI_ROOT` (legacy alias for CLI root)
- `OMNIPARSER_ROOT` / `OMNI_RUNTIME_ROOT` (OmniParser runtime root with `util/utils.py`)
- `OMNI_PYTHON` (force interpreter)
- `OMNI_MODEL_DIR` (default: `$OMNIPARSER_ROOT/weights`)
- `OMNI_INSTALL_ENV` (override install-env file path)

## Quick Start

```sh
# Parse to structured JSON
omni parse screenshot.png --quiet

# Reference by stable element id
omni measure screenshot.png --from id:e_abc123... --to id:e_def456... --quiet

# Parse + save OmniParser-native annotated debug image
omni parse screenshot.png --save-annotated /tmp/annotated.png --quiet

# Render full labeled debug image (idx/type/conf/label)
omni debug screenshot.png -o /tmp/debug-labeled.png --quiet

# Locate best match with proximity hints + ranked debug image
omni locate screenshot.png --query "label:save|side:right|near:1400,900" --save-annotated /tmp/locate.png --quiet

# Match one element across two screenshots robustly
omni match before.png after.png --query "label:save|side:right|near:1400,900" --anchor "region:sidebar" --save-annotated /tmp/match.png --quiet

# Pixel ruler
omni measure screenshot.png --from element:0 --to element:1 --edge center

# Crop by coordinates
omni crop screenshot.png --region 0,0,200,200 -o /tmp/crop.png

# Structural diff
omni diff before.png after.png --tolerance 5 --save-diff /tmp/diff.png

# Metadata summary
omni info screenshot.png

# Assertion-driven checks
omni check screenshot.png --quiet

# Overlay comparison
omni overlay before.png after.png -o /tmp/overlay.png --bbox-ref --bbox-test

# Print schema path for a command
omni parse --schema
```

## Commands

- `omni parse <image>`
- `omni debug <image> -o <output>`
- `omni locate <image> --query <selector>`
- `omni match <image1> <image2> --query <selector>`
- `omni measure <image> --from <spec> --to <spec>`
- `omni crop <image> --region <x,y,w,h> | --region-name <name> | --element <index>`
- `omni diff <image1> <image2>`
- `omni info <image>`
- `omni check <image>`
- `omni overlay <image1> <image2> -o <output_path>`
- `omni help` and `omni <subcommand> --help`

## Global Flags

- `--json` - shorthand for `--format json` where applicable
- `--verbose` - verbose stderr logs
- `--quiet` - suppress stderr logs
- `--no-color` - plain/log-safe output mode
- `--version` - print CLI + OmniParser version
- `--schema` - print JSON schema path for the selected subcommand
- `--model-dir <path>` - override model path
- `--runtime-root <path>` - override OmniParser runtime root (must contain `util/utils.py`)
- `--config <path>` - explicit `.omni.json` path (overrides discovery)
- `--device cpu|cuda` - inference device (default `cpu`)
- `--cache/--no-cache` - parse cache toggle (`~/.cache/omni`)

## Project Configuration

`omni` supports project-local config via `.omni.json`.

### Discovery behavior

For every subcommand invocation, `omni` searches from current working directory upward for `.omni.json`, stopping at `$HOME` or filesystem root (whichever comes first). If not found, commands still run normally unless config is required (for example `omni check`, or `region:<name>` references).

Use `--config <path>` to bypass discovery and load a specific config file.

### Proximity-aware reference queries

When label text varies between screenshots, use selector hints to bias matching:

- `near:x,y` prefers candidates near a point
- `side:left|right|top|bottom|center` biases toward a side/zone
- `within:px` enforces max distance from the `near` point

Examples:

- `label:save|side:right|near:1400,900`
- `submit|near:300,220|within:250`
- `*|side:bottom|near:960,1020`

These selectors work anywhere a reference string is accepted (`omni locate`, `omni measure --from/--to`, and `measurement` assertions in `.omni.json`).

`parse` emits `element_id` per detection. `id:<element_id>` (or `element-id:<element_id>`) is accepted in selectors anywhere references are used.

### Reusable targets

Define named selectors once and reuse everywhere via `target:<name>`.

Example:

```json
{
  "targets": {
    "primary-save": {
      "ref": "label:save|side:right|near:1400,900",
      "description": "Primary save action"
    },
    "left-rail": "region:sidebar"
  }
}
```

Then use them in commands and assertions:

- `omni locate screenshot.png --query target:primary-save`
- `omni measure screenshot.png --from target:left-rail --to target:primary-save`
- `omni match before.png after.png --query target:primary-save --anchor target:left-rail`
- `.omni.json` measurement assertions can use `from.ref`/`to.ref` as `target:<name>`

### Schema

```json
{
  "version": 1,
  "project_name": "pulse-hub-lca",
  "reference_image": "./screenshots/reference.png",
  "viewport": {
    "width": 1920,
    "height": 1080,
    "device_pixel_ratio": 1
  },
  "regions": {
    "sidebar": {
      "x": 0,
      "y": 0,
      "w": 280,
      "h": 1080,
      "description": "Left navigation sidebar"
    }
  },
  "targets": {
    "primary-save": {
      "ref": "label:save|side:right|near:1400,900"
    },
    "left-rail": "region:sidebar"
  },
  "assertions": [
    {
      "id": "sidebar-width",
      "type": "region_dimension",
      "region": "sidebar",
      "property": "width",
      "expected": 280,
      "tolerance": 3
    }
  ]
}
```

### Assertion types

- `region_dimension`:
  - Required: `region`, `property` (`width|height`), `expected`, `tolerance`
- `measurement`:
  - Required: `from.ref`, `to.ref`, `axis` (`x|y|euclidean`), `expected`, `tolerance`
- `element_count`:
  - Required: `operator` (`eq|gte|lte`), `expected`
- `elements_in_region`:
  - Required: `region`, `operator`, `expected`
- `region_color_dominant`:
  - Required: `region`, `expected_hex` (`#RRGGBB`), `tolerance_delta`

### Validation rules

- `version` must be `1`
- all numeric fields must be non-negative
- assertion IDs must be unique
- region references used by assertions must exist
- target references used by assertions/targets must exist
- target cycles are rejected at config-load time
- relative `reference_image` resolves relative to config file directory

On config validation failure, `omni` exits code `5` with a specific field-level error.

## Named Regions

Named regions are usable in commands and assertions via `region:<name>` references.

### Measure syntax

```sh
omni measure screenshot.png --from region:sidebar --to region:main-content --edge right
```

### Crop syntax

```sh
omni crop screenshot.png --region-name sidebar -o /tmp/sidebar.png
```

`--padding` works for `--region-name` and `--element`.

### Edge resolution table

| Edge | X | Y |
|---|---|---|
| `left` | `x` | `y + h/2` |
| `right` | `x + w` | `y + h/2` |
| `top` | `x + w/2` | `y` |
| `bottom` | `x + w/2` | `y + h` |
| `center` | `x + w/2` | `y + h/2` |
| `top-left` | `x` | `y` |
| `top-right` | `x + w` | `y` |
| `bottom-left` | `x` | `y + h` |
| `bottom-right` | `x + w` | `y + h` |

These edges apply to both region and element references.

## `omni check` Reference

### Usage

```sh
omni check <image> [--config <path>] [--only <id,id,...>] [--skip <id,id,...>] [--save-report <path>] [--save-annotated <path>] [--quiet]
```

### Behavior

1. Loads and validates project config (`.omni.json`) (required)
2. Runs only the assertions selected by filters
3. Skips OmniParser inference when selected assertions are static-only
4. Emits JSON report to stdout
5. Optionally writes report to disk via `--save-report`
6. Optionally saves annotated debug image via `--save-annotated`

## `omni locate` Reference

### Usage

```sh
omni locate <image> --query <selector> [--edge <edge>] [--top-k <n>] [--require-unambiguous] [--save-annotated <path>] [--quiet]
```

### Purpose

- Debugs label mismatch situations by returning ranked candidates
- Shows how proximity hints influenced matching scores
- `--require-unambiguous` turns ambiguity warnings into hard failures for safe automation
- Produces a visual candidate ranking image when `--save-annotated` is provided

### Output fields

- `locate.query`: original selector
- `locate.resolved`: final resolved target (same contract used by `measure`)
- `locate.candidates[]`: ranked candidates with `score`, `label_score`, `near_score`, `side_score`, and distance-to-near
- `locate.ambiguity`: ambiguity summary (`top2_gap`, `ambiguous`) to guard autonomous actions
- `meta.annotated_path`: debug image path when requested

## `omni match` Reference

### Usage

```sh
omni match <image1> <image2> --query <selector> [--anchor <selector>] [--top-k <n>] [--min-score <float>] [--require-unambiguous] [--save-annotated <path>] [--quiet]
```

### Purpose

- Reliably track the same logical UI element across two screenshots
- Handles duplicate/changed labels using multi-factor scoring
- Supports optional shared anchors (`--anchor`) to stabilize matching across layout shifts
- `--require-unambiguous` forces a non-ambiguous winner before proceeding

### Scoring factors

- `query_score`: selector match quality in image2 (includes proximity hints)
- `label_similarity`: source label vs candidate label
- `position_score`: normalized center-position consistency across screenshots
- `size_score`: width/height similarity
- `type_score`: element type match (`text`, `icon`, etc.)
- `anchor_score`: relative-position consistency to resolved anchors (when provided)

`match.ambiguity` is included to highlight close-call matches; agents can enforce this automatically with `--require-unambiguous`.

## `omni debug` Reference

### Usage

```sh
omni debug <image> -o <output_path> [--max-elements <n>] [--confidence-threshold <float>] [--quiet]
```

### Purpose

- Produces a robust visual debug artifact with box overlays and text labels
- Each box includes `index`, `element_type`, `confidence`, and truncated label text
- Useful for quickly verifying parser quality and label correctness before assertions/matching

### Output schema

```json
{
  "schema_version": "1.1",
  "command": "check",
  "request_id": "5f7f7f2b45f84f1ab8f17d7fc711f51e",
  "timestamp_utc": "2026-03-19T22:12:34Z",
  "status": "success",
  "result": "pass|fail",
  "error": null,
  "warnings": [],
  "summary": {
    "total": 6,
    "passed": 4,
    "failed": 2,
    "skipped": 0
  },
  "image": "/abs/path/screenshot.png",
  "config": "/abs/path/.omni.json",
  "results": [
    {
      "id": "sidebar-width",
      "type": "region_dimension",
      "passed": true,
      "expected": 280,
      "actual": 281,
      "delta": 1,
      "tolerance": 3,
      "details": "PASS: ..."
    }
  ],
  "meta": {
    "image_width": 1920,
    "image_height": 1080,
    "elements_detected": 33,
    "processing_time_ms": 4521,
    "cache_hit": true,
    "cli_version": "...",
    "omniparser_version": "..."
  }
}
```

### Exit codes

- `0` = all evaluated assertions passed
- `4` = one or more assertions failed
- `5` = config missing/invalid
- `2` = processing/runtime failure

## `omni overlay` Reference

### Usage

```sh
omni overlay <image1> <image2> -o <output_path> [--opacity <float>] [--bbox-ref [#RRGGBB]] [--bbox-test [#RRGGBB]] [--draw-regions] [--quiet]
```

### Notes

- images must have identical dimensions
- `--opacity 0.0` = image1 only, `1.0` = image2 only
- `--bbox-ref` draws parsed bboxes from image1 (default color `#FF4444`)
- `--bbox-test` draws parsed bboxes from image2 (default color `#4444FF`)
- `--draw-regions` draws named config regions (dashed green outlines)

### JSON output

```json
{
  "status": "success",
  "output_path": "/abs/path/overlay.png",
  "dimensions": {"width": 1920, "height": 1080},
  "ref_elements": 33,
  "test_elements": 31,
  "meta": {"...": "..."}
}
```

## JSON Output Contract (General)

For standard commands (`parse|debug|locate|match|measure|crop|diff|info|check|overlay`), top-level JSON includes:

- `schema_version`: current response schema version (`1.1`)
- `command`: subcommand that produced this response
- `request_id`: unique ID for this invocation (stable across stdout + stderr references)
- `timestamp_utc`: UTC timestamp in RFC 3339 format
- `status`: `success|error`
- `error`: `null` on success, error object on failure
- `warnings`: array of non-fatal messages (empty when none)
- `meta`: includes `image_path`, `image_width`, `image_height`, `processing_time_ms`, `omniparser_version`, `cli_version`

`meta` additionally includes traceability and reproducibility fields:

- `command`, `request_id`, `timestamp_utc`
- `image_sha256` (and `image_sha256_2` when two images are used)
- `config_sha256` when a project config is loaded

Error payloads include machine-actionable fields:

- `error.code`
- `error.type`
- `error.message`
- `error.hint`
- `error.retryable`

Canonical JSON schema files are provided in `cli/schemas/`:

- `cli/schemas/parse.v1.json`
- `cli/schemas/debug.v1.json`
- `cli/schemas/locate.v1.json`
- `cli/schemas/match.v1.json`
- `cli/schemas/measure.v1.json`
- `cli/schemas/crop.v1.json`
- `cli/schemas/diff.v1.json`
- `cli/schemas/info.v1.json`
- `cli/schemas/check.v1.json`
- `cli/schemas/overlay.v1.json`
- `cli/schemas/error.v1.json`

Contract smoke test helper:

- `cli/tests/contract_smoke.py` (run with `OMNI_TEST_IMAGE=/abs/path/image.png`)

## Exit Codes (General)

- `0` success
- `1` user input error
- `2` processing/runtime error
- `3` file not found
- `4` check assertions failed
- `5` config required/invalid

## For AI Agents

Recommended agent workflow:

1. Modify source code (CSS, JSX, etc.)
2. Trigger screenshot capture (Playwright, Puppeteer, etc.)
3. Run: `omni check screenshot.png --quiet`
4. Parse stdout JSON
5. If `status == "success"` and `result == "pass"`: done
6. If `status == "success"` and `result == "fail"`: read `results[].details` for each failed assertion
7. `details` contains exact pixel measurements and deltas; use these to calculate CSS adjustments
8. Apply fixes and return to step 1

Additional recommendations:

- always use absolute image paths in automation
- keep `--quiet` enabled for parser-safe stdout JSON
- use `--save-report` for audit trail artifacts
- when label matching is unstable, use `omni locate ... --query "label:...|near:...|side:..."` before `measure/check`
- for autonomous actions, add `--require-unambiguous` to `locate` and `match`
- pin runtime portability with `--runtime-root <path>` (or `OMNIPARSER_ROOT`) in CI/agents
- use `--only` and `--skip` to narrow expensive checks during iterative tuning

## Known Limitations

- this OmniParser build is effectively CPU-bound in this environment
- inference can be slow on CPU-only hardware
- supported input formats: `png`, `jpg/jpeg`, `bmp`, `webp`, `tif/tiff`
