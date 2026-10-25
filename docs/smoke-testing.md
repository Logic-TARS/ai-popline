# Smoke Testing

## Quick Reference

```bash
# Default: skip external and publish tests
pytest

# Run external tool tests (requires configured tools)
RUN_EXTERNAL_TESTS=1 pytest -m external

# DANGER: actually publishes to social platforms
RUN_PUBLISH_TESTS=1 pytest -m publish

# Run everything except publish
pytest -m "not publish"

# Run a specific smoke test
RUN_EXTERNAL_TESTS=1 pytest tests/test_japanese_pipeline.py -m external -v
```

## Test Markers

| Marker | Purpose | Default | Env Var Required |
|---|---|---|---|
| `external` | Tests calling real external tools (Gemini, Photo-Process, MPT, SAU) | Skipped | `RUN_EXTERNAL_TESTS=1` |
| `smoke` | End-to-end smoke tests requiring full environment | Skipped | `RUN_EXTERNAL_TESTS=1` |
| `publish` | Tests that actually publish to social platforms | **Always skipped** | `RUN_PUBLISH_TESTS=1` |

## Production quality gate

Before handing off a build, run `bash scripts/check.sh` on Git Bash or `./scripts/check.ps1` on PowerShell. The gate checks formatting, Ruff lint, packaged Web static JavaScript syntax with `node --check`, non-external/non-publish tests, sdist and wheel contents, then reinstalls the built wheel, runs `uv pip check`, and runs installed-wheel CLI smoke tests for `ai-pipeline capabilities` and `ai-pipeline validate-task --task examples/tasks/dry-run/task.example.json`. It also installs the wheel into a temporary isolated project, runs `uv pip check` there, verifies `config/pipeline.defaults.yaml` loads from the packaged fallback when the repository checkout is absent, and removes the temporary isolated project even when a smoke step fails.

CI runs the same Bash quality gate on pushes to `main`, pull requests, and manual `workflow_dispatch` runs with Python 3.11 and Node.js 24 before building the production Docker image. The only package artifact intended for handoff is the checked `dist/check/*` output uploaded as `ai-pipeline-dist`; do not substitute an unchecked local build.

## Pre-flight Checklist

Before running external tests:

1. Run `ai-pipeline doctor` — verify all tool paths and executables
2. Confirm `.env` is configured with valid paths
3. Ensure no conflicting browser processes (production profile must be free)
4. Verify external tools are installed and working independently
5. For publish tests: log into target platform accounts first

## External tool troubleshooting

Use this sequence when an external smoke test fails. Keep the failing `task_id`, `status.json`,
`events.jsonl`, and the exact command output together as incident evidence. Do not enable
`RUN_PUBLISH_TESTS=1` while diagnosing generation-only failures.

1. Run `ai-pipeline doctor --bundle output/diagnostics.zip` and attach only the generated bundle.
   The bundle must be redacted and must not include `.env`, task JSON, browser cookies, media
   artifacts, or raw lower-tool logs.
2. Inspect `ai-pipeline jobs show <task_id>` and `ai-pipeline jobs events <task_id>` before rerunning.
   Treat `failed`, `blocked`, and `partial` as real terminal evidence; do not overwrite artifacts
   until the operator has copied the incident directory if it is needed for support.
3. Reproduce the failing lower-tool command outside the pipeline only after confirming the command
   does not publish. Record the working directory and arguments, but redact tokens, cookies, profile
   paths that expose account names, and platform session details.
4. After remediation, rerun a dry task first and verify `artifacts.validation` before enabling any
   publish-capable example or platform account.

Tool-specific checks:

| Tool | Common symptoms | Evidence to collect | Recovery rule |
|---|---|---|---|
| Gemini MCP / `gemini-skill` | MCP exits early, `gemini_generate_image` returns `isError`, request timeout, or success without a new image file | `ai-pipeline doctor`, stderr tail from the failed adapter, output directory listing, prompt count/full-size settings | Restart the MCP process by rerunning the job; verify `GEMINI_SKILL_DIR` and Node dependencies; require a new decoded image before marking recovery successful |
| Photo-Process / browser worker | `AUTH_REQUIRED`, `BROWSER_BUSY`, `GEM_ACCESS_FAILED`, `UI_CHANGED`, `TIMEOUT`, `NO_OUTPUT`, or `VALIDATION_FAILED` | `photo_process_contract`, worker URL or CLI command, target `/gem/<id>` URL, copied image validation metadata | Fail closed: free the Chrome profile, sign in interactively if needed, verify Gem access, then rerun one image; never treat a visible browser as success |
| MoneyPrinterTurbo (MPT) | command timeout, no `.mp4` path in output, missing subtitle, subtitle looks like a file path, or video has no audio | MPT task directory, `final-1.mp4`, `subtitle.srt`, `generation_manifest.json`, stdout/stderr tail | Remove or archive only stale task directories for the same task identity; rerun dry first when supported; require video decode, audio, and subtitle validation |
| SAU uploaders | `PRIVATE_VISIBILITY_UNSUPPORTED`, `PUBLISH_FAILED`, `ALREADY_PUBLISHED`, or missing selected-visibility proof | SAU command arguments, `artifacts.publish_results`, platform proof text, account/profile used | Keep publishing disabled until generation artifacts pass review; for private Bilibili restore `SAU_BILIBILI_PRIVATE_ARGS=--is-only-self 1`; require Douyin/Kuaishou visibility proof; never treat missing proof as success |

## Writing Smoke Tests

```python
import os
import pytest

@pytest.mark.external
def test_real_photo_process():
    if not os.getenv("RUN_EXTERNAL_TESTS"):
        pytest.skip("RUN_EXTERNAL_TESTS not set")
    # ... real external tool call

@pytest.mark.publish
def test_real_upload():
    if not os.getenv("RUN_PUBLISH_TESTS"):
        pytest.skip("RUN_PUBLISH_TESTS not set")
    # ... real publish call
```
