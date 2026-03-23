# CaliperUI Validation Report

## CLI
| Test | Status | Notes |
|------|--------|-------|
| baseline runs | PASS | `validation/cli-baseline.exit` = `0`, `validation/cli-baseline.json` has `status: success`. |
| baseline output valid | PASS | `validation/acist.caliper.json` exists and includes `version`, `viewport`, `assertions`, `reference_image`. |
| viewport matches image | PASS | `viewport.width = 1280`, `viewport.height = 800`. Assertion IDs are unique (`124/124`). |
| check runs on generated config | PASS | `validation/cli-check.exit` = `0`, `validation/cli-check.json` has `status: success` and includes `result: pass`. |
| parse runs | PASS | `validation/cli-parse.exit` = `0`, `validation/cli-parse.json` has `status: success`. |
| diff runs | PASS | `validation/cli-diff.exit` = `0`, `validation/cli-diff.json` has `status: success`. |
| engines lists correctly | PASS | `validation/cli-engines.exit` = `0`, `validation/cli-engines.json` has `status: success` and engine list. |

## Server
| Endpoint | Status | Notes |
|----------|--------|-------|
| GET /health | FAIL | HTTP `000` (`validation/server-health.http`), no server response. |
| POST /parse | FAIL | HTTP `000` (`validation/server-parse.http`), no server response. |
| POST /baseline | FAIL | HTTP `000` (`validation/server-baseline.http`), no server response. |
| POST /check | FAIL | HTTP `000` (`validation/server-check.http`), no server response. |
| POST /diff | FAIL | HTTP `000` (`validation/server-diff.http`), no server response. |
| POST /check missing config → 422 | FAIL | HTTP `000` (`validation/server-check-missing-config.http`), expected `422`. |

## Studio
| Test | Status | Notes |
|------|--------|-------|
| /author loads | FAIL | `http://localhost:3000/author` renders nginx `404 Not Found` page; upload/drop zone not present; nav links to `/triage` and `/history` not visible. |
| parse overlays render after upload | FAIL | No file input on page (`no file input`), no overlays rendered. |
| element inspector opens on click | FAIL | No overlay candidates found (`no overlay candidates found`), inspector panel did not open. |
| generate config triggers download/output | FAIL | `Generate Config` button not found; no download/output triggered. |
| /triage loads | FAIL | `http://localhost:3000/triage` renders nginx `404 Not Found` page, not Studio Triage UI. |
| triage overlays render after dual upload | FAIL | No triage file inputs found (`expected >=3 file inputs, found 0`), so no overlays or result list rendered. |
| /history loads | FAIL | `http://localhost:3000/history` renders nginx `404 Not Found` page. |

## Blockers
List any FAIL items with exact error messages, HTTP status codes, or console output captured.

- Server failed to boot with the requested command:
  - Command: `SERVER_PORT=7771 CALIPER_BIN=./bin/caliper $PY server/main.py`
  - Error from `validation/logs/server.log`:
    - `ModuleNotFoundError: No module named 'server'`
  - Result: all server endpoint checks returned HTTP `000`.
- Studio could not bind to port `3000` (required by validation steps):
  - `validation/logs/studio.log` shows:
    - `Port 3000 is in use` (also 3001–3007 in use), then Next started on `http://localhost:3008`.
  - `http://localhost:3000` is served by nginx (`Server: nginx/1.24.0 (Ubuntu)`).
- Browser console during each Studio step on `localhost:3000` captured 404 errors:
  - `[ERROR] Failed to load resource: the server responded with a status of 404 (Not Found)`
  - Console captures: `validation/studio-console-3a.txt` through `validation/studio-console-3g.txt`.

## Screenshots
List all screenshots saved to validation/ with one-line description of what each shows.

- `validation/studio-author-empty.png`: `/author` on port 3000 showing nginx `404 Not Found` page.
- `validation/studio-author-parsed.png`: after upload attempt in Author mode; still nginx `404` page.
- `validation/studio-author-inspector.png`: after overlay-click attempt; still nginx `404` page.
- `validation/studio-triage-empty.png`: `/triage` on port 3000 showing nginx `404` page.
- `validation/studio-triage-results.png`: after dual-upload/check attempt; still nginx `404` page.
- `validation/studio-history.png`: `/history` on port 3000 showing nginx `404` page.

## Fix Run

### Server (startup path + import fix attempt)

Tried both requested startup strategies and captured details in `validation/logs/server-fix-attempt.log`:

- Strategy 1: `python -m uvicorn server.main:app` from repo root.
- Strategy 2: `python -m uvicorn main:app` from `server/`.

Server process started, but every endpoint returned HTTP `422` due forwarded CLI runtime error.

| Endpoint | Status | Notes |
|----------|--------|-------|
| GET /health | FAIL | HTTP `422`, payload `status: error` (`validation/server-health.json`). |
| POST /parse | FAIL | HTTP `422`, payload `status: error` (`validation/server-parse.json`). |
| POST /baseline | FAIL | HTTP `422`, payload `status: error` (`validation/server-baseline.json`). |
| POST /check | FAIL | HTTP `422`, payload `status: error` (`validation/server-check.json`). |
| POST /diff | FAIL | HTTP `422`, payload `status: error` (`validation/server-diff.json`). |
| POST /check missing config → 422 | PASS | HTTP `422` observed (`validation/server-check-missing-config.json`). |

Forwarded error seen across server responses:

`caliper: unable to locate OmniParser runtime root (expected <root>/util/utils.py)`

`Set OMNIPARSER_ROOT (or CALIPER_RUNTIME_ROOT) to your OmniParser install directory.`

### Studio (forced to port 3008)

Ran all requested Playwright checks against `http://localhost:3008` and overwrote screenshots.
Step details: `validation/logs/studio-fix-results.json`.
Console/page/request failure logs per step: `validation/logs/studio-console-3a.txt` … `validation/logs/studio-console-3g.txt`.

| Test | Status | Notes |
|------|--------|-------|
| /author loads | PASS | Drop/upload UI and nav links to `/triage` and `/history` present (`validation/studio-author-empty.png`). |
| parse overlays render after upload | FAIL | `/parse` returned HTTP `422`; no overlays detected (`overlayCount: 0`) and error text visible (`validation/studio-author-parsed.png`). |
| element inspector opens on click | FAIL | No overlay candidates to click; inspector details/tolerance controls not populated (`validation/studio-author-inspector.png`). |
| generate config triggers download/output | FAIL | Button exists/enabled but no download event and no JSON output rendered (likely blocked by upstream `/parse` failure). |
| /triage loads | PASS | Reference/iteration UI present with 3 file inputs (`validation/studio-triage-empty.png`). |
| triage overlays render after dual upload | FAIL | `/diff` and `/check` returned HTTP `422`; no overlays detected (`overlayCount: 0`) though failure text/list is present (`validation/studio-triage-results.png`). |
| /history loads | PASS | Page loaded with content and no crash markers (`validation/studio-history.png`). |

## Fix Run 2

Server was restarted with `OMNIPARSER_ROOT` set (and companion runtime/python env so `caliper` subprocesses resolve correctly from the server process), then `/health` was re-polled.

Health verification now passes and reports OmniParser availability:

- `validation/server-health.json`: `status: success`, `http_status: 200`, `doctor.result: pass`
- `validation/server-health.json`: `doctor.checks[id="engine:omniparser"].details.available = true`

### Server (Step 2 rerun)

| Endpoint | Status | Notes |
|----------|--------|-------|
| GET /health | PASS | HTTP `200`, payload `status: success`, doctor pass. |
| POST /parse | PASS | HTTP `200`, payload `status: success` (`validation/server-parse.json`). |
| POST /baseline | PASS | HTTP `200`, payload `status: success`; response contains generated assertions (`124`). |
| POST /check | PASS | HTTP `200`, payload `status: success` (`result: pass`). |
| POST /diff | PASS | HTTP `200`, payload `status: success`. |
| POST /check missing config → 422 | PASS | HTTP `422` observed with forwarded CLI `error` object. |

### Studio (Step 3 rerun on port 3008)

All requested Playwright checks were rerun against `http://localhost:3008`; screenshots and `validation/studio-console-3*.txt` were overwritten.

| Test | Status | Notes |
|------|--------|-------|
| /author loads | PASS | Drop zone + nav links visible (`validation/studio-author-empty.png`). |
| parse overlays render after upload | PASS | `/parse` returned `200`; overlays detected (`overlayCount: 36`) (`validation/studio-author-parsed.png`). |
| element inspector opens on click | PASS | Overlay click succeeded; inspector fields + tolerance control visible (`validation/studio-author-inspector.png`). |
| generate config triggers download/output | PASS | `Generate Config` triggered file download (`validation/generated-from-author-fix2.caliper.json`). |
| /triage loads | PASS | Reference/iteration upload UI visible (`validation/studio-triage-empty.png`). |
| triage overlays render after dual upload | PASS | `/diff` and `/check` returned `200`; overlays + assertion list visible (`validation/studio-triage-results.png`). |
| /history loads | PASS | History route loads with content and no crash text (`validation/studio-history.png`). |

Detailed machine-readable run output: `validation/logs/studio-fix2-results.json` and `validation/logs/server-fix2-checks-summary.txt`.
