# AI Pipeline Architecture

## Overview

AI Pipeline is a local AI content production pipeline that orchestrates Gemini, Photo-Process, Cover-Forge, MoneyPrinterTurbo (MPT), social-auto-upload (SAU), and allowlisted read-only ttskill research to generate validated short-form media across 8 content pipelines.

```
TaskInput (JSON/MCP/REST)
  → Orchestrator.submit() → JobStore
  → route_task() → RouteResult
  → pipeline dispatch → adapter calls
  → JobSnapshot (persisted to output/jobs/<id>/)
```

## Eight Content Pipelines

| Pipeline | Content Type | Flow | External Tools |
|---|---|---|---|
| **Anime** | `anime` | Gemini image gen → MPT video → Bilibili upload | Gemini, MPT, SAU |
| **Finance** | `finance` | Markdown parsing → narrated MPT video → Douyin/Kuaishou publish | MPT, SAU |
| **AI Briefing** | `ai_briefing` | Daily handoff → script → cover (Cover-Forge or local Pillow fallback) → narrated MPT → optional publish | Cover-Forge (optional), MPT, SAU |
| **AI Art** | `ai_art` | Photo-Process folder editing → slideshow groups → optional private publish | Photo-Process, ffmpeg, SAU |
| **Xiaohongshu Image Note** | `xhs_image_note` | Photo-Process optimization → local image output only | Photo-Process |
| **Grouped Anime** | `grouped_anime` | Source images grouped by prefix → per-group MPT video → optional publish | MPT, SAU |
| **Japanese** | `japanese` | Photo-Process Gem editing → local image output only | Photo-Process |
| **Script Video** | `script_video` | Reviewed 350–500 character narration → local 9:16 video | MPT |

## Pipeline Steps

Every pipeline progresses through a subset of these steps:

```
ROUTE → SOURCE_SCAN → IMAGE → VIDEO → ARCHIVE → UPLOAD → COMPLETE
```

- **Japanese** stops after IMAGE (no video or upload)
- **AI Briefing** uses IMAGE for its required cover before VIDEO
- **Finance** skips IMAGE (no image generation)
- **Anime** (legacy) uses IMAGE → VIDEO → UPLOAD

## External Tool Dependencies

| Tool | Path | Adapter | Purpose |
|---|---|---|---|
| Gemini Skill | `GEMINI_SKILL_DIR` | `adapters/gemini_mcp.py` | MCP-based image generation |
| Photo-Process | `PHOTO_PROCESS_DIR` | `adapters/photo_process.py` | Browser-automated image editing |
| Cover-Forge | `COVER_FORGE_URL` | `tools/cover_forge_client.py` | Localhost HTTP cover rendering |
| MoneyPrinterTurbo | `MPT_DIR` | `adapters/mpt.py`, `adapters/mpt_narrated.py` | Video generation |
| social-auto-upload | `SAU_DIR` | `adapters/sau.py` | Multi-platform publishing |
| ffmpeg | (system) | `adapters/slideshow.py`, `adapters/audio.py` | Slideshow rendering, music prep |
| ttskill | `TTSKILL_COMMAND` | `content_studio/ttskill_client.py` | Allowlisted read-only financial research |

## Entry Points

| Interface | Command | Description |
|---|---|---|
| CLI | `python -m content_pipeline.orchestrator --task <file>` | Run a task JSON synchronously |
| MCP | `python -m content_pipeline.mcp_server` | MCP stdio server (18 tools) |
| Web / REST | `ai-pipeline serve` | Packaged operator console and guarded API for loopback or an explicitly configured ZeroTier interface |
| Doctor | `ai-pipeline doctor` | Environment and tool diagnostics |
| Capabilities | `ai-pipeline capabilities` | List available pipelines |

## Production Handoff Boundary

Production delivery is gated by `bash scripts/check.sh`, which runs formatting, Ruff lint, non-external/non-publish tests, sdist and wheel content checks, dependency compatibility through `uv pip check`, installed-wheel CLI smoke tests, and an isolated wheel smoke that proves packaged defaults load without the repository checkout. Runtime handoff uses the guarded `ai-pipeline serve` entry point, durable writable job storage, private publishing defaults, and the local-only adapter boundary documented in the runbooks.

## Adapter Pattern

All external tool calls return a uniform `AdapterResult`:

```json
{
  "ok": true,
  "tool": "Photo-Process",
  "code": "OK",
  "message": null,
  "artifacts": {},
  "evidence": {},
  "raw": {}
}
```

Adapters isolate subprocess execution, timeouts, stdout parsing, and tool-specific quirks. The orchestrator never depends on Gemini browser state, MPT internals, or uploader UI details directly.

## Job Persistence

Jobs are stored at `<data_dir>/jobs/<task_id>/`:

- `status.json` — full `JobSnapshot` (task, route, artifacts, validation, error)
- `events.jsonl` — append-only audit log (job_created, step_started, job_finished, etc.)
- `images/` — generated/staged images
- `video/` — generated MP4 files
- `cover/cover.png` — exact-size cover for real AI Briefing jobs (Cover-Forge output or local Pillow fallback)
- Pipeline-specific directories (groups/, processed/, photo_process_sources/)

## Publishing Safety

All publishing defaults to OFF. `TaskInput.publish` must be explicitly `true`. Visibility defaults to private (仅自己可见); each publish target may explicitly select `public`. Both modes are evidence-enforced:

- **Douyin**: must prove the selected visibility ("仅自己可见" or "公开可见")
- **Kuaishou**: must prove the selected visibility
- **Bilibili**: private requires `SAU_BILIBILI_PRIVATE_ARGS=--is-only-self 1`; public omits the flag

Fail-closed: any uncertainty → BLOCKED status.

## Directory Structure

```
src/content_pipeline/
  api/          # FastAPI app factory, Web schema/preflight, access controls
  web/static/   # Packaged no-Node operator console
  cli/          # CLI entry points (doctor, capabilities, orchestrator runner)
  core/         # settings, models, errors, router
  pipelines/    # Pipeline registration package and registry
  adapters/     # external tool clients (gemini, mpt, photo_process, sau, ffmpeg)
  storage/      # job store persistence
  validation/   # media validation, grouping
```

## Module layering status

The project now uses a `src/` layout. Public facades exist for the intended layers:

- `content_pipeline.core.*`: models, errors, and settings.
- `content_pipeline.pipelines.*`: registered workflow implementations.
- `content_pipeline.adapters.*`: external tool adapters for Gemini, Photo-Process, MPT, and SAU.
- `content_pipeline.storage.*`: job persistence.
- `content_pipeline.validation.*`: media validation.

Legacy imports such as `content_pipeline.tools.*` and top-level pipeline modules remain supported while the implementation is migrated incrementally.
