# Configuration

AI Pipeline uses two configuration layers:

1. `.env` / environment variables for global tool paths and runtime settings.
2. `config/pipeline.defaults.yaml` for per-pipeline default task parameters.

## Global settings

Common `.env` values:

```env
PIPELINE_DATA_DIR=output
PIPELINE_DEFAULTS_FILE=config/pipeline.defaults.yaml
GEMINI_SKILL_DIR=G:\Job\gemini-skill
PHOTO_PROCESS_DIR=G:\Job\Photo-Process
PHOTO_PROCESS_PYTHON=G:\Job\Photo-Process\.venv311\Scripts\python.exe
COVER_FORGE_URL=http://127.0.0.1:3000
COVER_FORGE_TIMEOUT_SECONDS=60
MPT_DIR=G:\Job\MoneyPrinterTurbo
MPT_PYTHON=G:\Job\MoneyPrinterTurbo\.venv\Scripts\python.exe
SAU_DIR=G:\Job\social-auto-upload
SAU_EXE=G:\Job\social-auto-upload\.venv\Scripts\sau.exe
SAU_BILIBILI_PRIVATE_ARGS=--is-only-self 1
FINANCE_MD_DIR=G:\Job\Automation-Output\sajin\fund-daily
AI_BRIEFING_DIR=G:\Hermes-Output\每日AI简报
TTSKILL_COMMAND=ttskill
TTSKILL_TIMEOUT_SECONDS=120
SCRIPT_LLM_BASE_URL=
SCRIPT_LLM_API_KEY=
SCRIPT_LLM_MODEL=
SCRIPT_LLM_TIMEOUT_SECONDS=120
```

`SCRIPT_LLM_*` configures the shared OpenAI-compatible client used by AI Briefing and finance script planning, drafting, targeted repair, and independent title/description generation. When endpoint, key, or model is empty, the corresponding `ROUTER_LLM_*` value is used for backwards compatibility. `SCRIPT_LLM_TIMEOUT_SECONDS` defaults to 120 and accepts 5–600 seconds. Pipeline params select `script_failure_policy: fail` or `validated_rule_fallback` and allow `script_repair_attempts` from 0 to 2 (default 1). Generated jobs keep the legacy script file and additionally write `script/plan.json`, `script/attempts.json`, `script/quality-report.json`, and `script/audit.json`. `quality_gate` defaults to `soft` and is recorded in `script/quality-report.json`.

`COVER_FORGE_URL` and `COVER_FORGE_TIMEOUT_SECONDS` configure the optional localhost sidecar used by the AI Briefing cover stage; start Cover-Forge separately with `npm start`. When the sidecar is unavailable or fails, the pipeline falls back to built-in Pillow-based cover rendering (gradient background with the normalized title), so covers are generated even without Cover-Forge. Dry-run AI Briefing tasks skip this call.

`TTSKILL_COMMAND` and `TTSKILL_TIMEOUT_SECONDS` configure the content studio's read-only financial research adapter. The browser cannot override its fixed Skill allowlist or action parameters; authenticate the local CLI separately with `ttskill login`.

## Container runtime storage

The production `Dockerfile` runs as the non-root `appuser` and sets `PIPELINE_DATA_DIR=/data`. The image copies `config/pipeline.defaults.yaml` into the image, then creates `/data` before dropping privileges so runtime storage is writable without root.

When using `docker-compose.yml`, the host `./output` directory is mounted at `/data`, and Compose sets `PIPELINE_DATA_DIR=/data` so a host `.env` cannot redirect container job state back to a workstation path. Compose publishes the service on host loopback with `.env` `WEB_PORT` while the container listener stays on port `8080`. The Compose service also enables `restart: unless-stopped`, `init: true`, and `no-new-privileges:true` so normal restarts are resilient without giving the container privilege escalation:

```yaml
restart: unless-stopped
init: true
security_opt:
  - no-new-privileges:true
ports:
  - "127.0.0.1:${WEB_PORT:-8080}:8080"
environment:
  PIPELINE_DATA_DIR: /data
volumes:
  - ./output:/data
```

For direct `docker run` deployments, mount a writable directory to `/data` instead of relying on the container's ephemeral filesystem, and pass the required Web authentication secrets explicitly:

```bash
docker run --rm \
  -p 127.0.0.1:8080:8080 \
  -v "$PWD/output:/data" \
  -e WEB_AUTH_REQUIRED=true \
  -e WEB_ADMIN_TOKEN="$WEB_ADMIN_TOKEN" \
  -e WEB_API_TOKEN="$WEB_API_TOKEN" \
  -e WEB_SESSION_SECRET="$WEB_SESSION_SECRET" \
  ai-pipeline
```

`WEB_ADMIN_TOKEN`, `WEB_API_TOKEN`, and `WEB_SESSION_SECRET` must be independent random values with at least 32 characters. If a platform pre-creates the host volume as root-only, fix the host directory permissions before starting the container. Do not mount `/data` read-only; jobs need it for task state, generated assets, logs, and diagnostic bundles.

Validate global settings with:

```powershell
ai-pipeline doctor
```

For production handoff, keep the generated diagnostics bundle with the deployment record:

```powershell
ai-pipeline doctor --bundle output\diagnostics.zip
```

The bundle redacts fields whose names contain `token`, `secret`, `key`, `password`, `credential`, or `cookie`. It contains `diagnostics.txt` only: Python/platform metadata, redacted settings, external-tool availability, Web bind-policy results, and configuration warnings. It must not include `.env` contents, task JSON, `status.json`, `events.jsonl`, media artifacts, browser cookies, or raw lower-tool logs. Treat the remaining paths and tool availability as operational data: share it only with the operators who need to debug this deployment.

## Web and ZeroTier settings

The packaged operator console is served at `/` by `ai-pipeline serve`. The safe default is loopback-only, without Web/API publishing:

```env
WEB_BIND_HOST=127.0.0.1
WEB_PORT=8080
WEB_ALLOWED_NETWORKS=
WEB_ALLOWED_HOSTS=127.0.0.1,localhost,::1
WEB_ALLOWED_ORIGINS=http://127.0.0.1:8080,http://localhost:8080
WEB_AUTH_REQUIRED=false
WEB_PUBLISH_ENABLED=false
WEB_ALLOW_ZEROTIER_HTTP=false
CACHE_CLEANUP_ROOT=
```

Remote access requires the exact ZeroTier interface IP and all of the following:

- `WEB_ALLOWED_NETWORKS`: comma-separated ZeroTier CIDRs whose direct peers may connect.
- `WEB_ALLOWED_HOSTS`: comma-separated HTTP Host names/IPs; include the exact bind IP.
- `WEB_ALLOWED_ORIGINS`: comma-separated browser origins including scheme and port.
- `WEB_AUTH_REQUIRED=true`.
- independent `WEB_ADMIN_TOKEN`, `WEB_API_TOKEN`, and `WEB_SESSION_SECRET` values of at least 32 characters.
- both `WEB_TLS_CERTFILE` and `WEB_TLS_KEYFILE`, unless the explicit restricted-mode `WEB_ALLOW_ZEROTIER_HTTP=true` exception is accepted.

`WEB_SESSION_TTL_SECONDS` controls session lifetime (604800 seconds, or 7 days, by default). `WEB_PUBLISH_REAUTH_SECONDS` independently controls how recently a session must have logged in before a publish request; it also defaults to 604800 seconds, so the default publishing window lasts as long as the session. Set it to a shorter value when publishing should require more recent authentication; matching the session lifetime reduces protection against publishing from a stolen or unattended session. `WEB_PUBLISH_ENABLED` defaults to false and is an additional API-level guard; it does not override pipeline privacy checks.

`ai-pipeline serve` rejects wildcard listeners, remote addresses outside the configured CIDRs, missing authentication, partial TLS configuration, and bind addresses that are not assigned to a local interface. See the [ZeroTier Web Access Runbook](zerotier-web-access.md) before enabling remote access.

## Production configuration baseline

Use `.env.example` as a template, not as a deployable configuration. For each environment:

- Copy `.env.example` to `.env`; never commit `.env`, `.env.*`, TLS private keys, or diagnostic bundles.
- Set `PIPELINE_DATA_DIR` to a durable writable directory that is backed up or intentionally disposable according to the operator's retention policy.
- Replace workstation-specific tool paths such as `GEMINI_SKILL_DIR`, `PHOTO_PROCESS_DIR`, `MPT_DIR`, `SAU_DIR`, `FINANCE_MD_DIR`, and `AI_BRIEFING_DIR` with paths that exist on that host.
- Keep `WEB_PUBLISH_ENABLED=false` until generated artifacts have been reviewed and the publishing runbook has been followed.
- Set `ENV=production` only with `WEB_AUTH_REQUIRED=true`; production mode fails closed even on loopback if authentication is disabled.
- For all production or remote access, configure three independent random secrets; do not reuse the admin token as the API token or session secret.
- Prefer TLS with `WEB_TLS_CERTFILE` and `WEB_TLS_KEYFILE`; record any `WEB_ALLOW_ZEROTIER_HTTP=true` deployment as temporary restricted mode.
- Keep `SAU_BILIBILI_PRIVATE_ARGS=--is-only-self 1` unless a reviewed uploader version changes the verified private-visibility flag; it is required for private (default) Bilibili uploads and omitted only when a target explicitly selects public visibility.
- Run `ai-pipeline doctor` after every configuration change and keep `ai-pipeline doctor --bundle output\diagnostics.zip` as the redacted handoff evidence.

## Per-pipeline defaults

Edit:

```text
config/pipeline.defaults.yaml
```

Or use the web console 设置中心 (定时任务 → 设置中心 → 流水线默认参数); console edits are saved to `<data_dir>/pipeline_defaults_override.json` and take precedence over the YAML file. The same data is available through `GET/PUT /pipeline-defaults`.

It is disabled by default:

```yaml
enabled: false
```

Set it to true when you want the file to supply defaults:

```yaml
enabled: true
```

Merge rules:

- `pipelines.<content_type>.params` becomes default task params.
- Explicit values in a task JSON always override YAML defaults.
- Packaged defaults may define params for these task `content_type` values: `anime`, `finance`, `ai_briefing`, `japanese`, `ai_art`, `xhs_image_note`, and `grouped_anime`.
- Supported placeholders in YAML strings:
  - `{data_dir}`
  - `{task_id}`
  - `{content_type}`
  - `{topic}`

Example Japanese output layout:

```yaml
enabled: true
pipelines:
  japanese:
    params:
      source_dir: "G:\\Job\\Photo-Datasets\\jap"
      output_dir: "{data_dir}\\japanese\\{task_id}"
      process_name: "日语视觉化"
      target_gem_url: "https://gemini.google.com/gem/f306c82a8105"
      image_prompt: ""  # optional legacy extra text; Photo-Process owns real prompts
```

Then a minimal task can be:

```json
{
  "description": "日语视觉化图片-only",
  "content_type": "japanese",
  "publish": false,
  "params": {}
}
```

The final output would go under:

```text
output\japanese\<task_id>\
```

### AI Briefing and finance titles/descriptions

Only `ai_briefing` and `finance` accept these metadata and quality-gate parameters:

| Parameter | Values | Default |
| --- | --- | --- |
| `title_writer` | `follow_script`, `rule`, `llm` | `follow_script` |
| `description_writer` | `follow_script`, `rule`, `llm` | `follow_script` |
| `metadata_failure_policy` | `fail`, `validated_rule_fallback` | `fail` |
| `metadata_repair_attempts` | Integer from 0 to 2, per stage | `1` |
| `quality_gate` | `soft`, `strict` | `soft` |

`quality_gate` applies to both content gates — the narration script check and the title/description check. `soft` runs the same checks but records every non-compliance issue as a warning instead of an error, so it no longer blocks the job; the issues stay visible in `script/quality-report.json`, `metadata_audit.json`, and the job events, and both reports record the gate that produced them. `strict` restores the pre-soft behaviour where any issue blocks. Two compliance sets stay blocking under `soft`: the finance narration checks `finance_prohibited`, `finance_disclaimer`, and `finance_risk_warning`, and the metadata grounding checks `empty_text`, `fact_anchor_missing`, and `script_anchor_missing`.

`follow_script` resolves each metadata writer from `script_writer`. A nonempty `params.title` or `params.description` takes priority and skips that field's generation stage, independently of the other field. The task's top-level `description` remains a task label; it is not the publishing description.

Both pipelines now default `params.description` to `""` (automatic). Rule mode restores the original fixed descriptions: `今日AI简报` for AI Briefing and `每日基金日报，仅供参考，不构成投资建议` for finance. Existing schedules, saved console form values, and enabled defaults containing nonempty descriptions remain manual overrides; explicitly clear those values to opt into generation. The following task requests all three text stages from the configured model without real media generation or publishing:

```json
{
  "description": "Review generated AI titles and descriptions",
  "content_type": "ai_briefing",
  "publish": false,
  "params": {
    "script_writer": "llm",
    "title_writer": "follow_script",
    "description_writer": "follow_script",
    "metadata_failure_policy": "fail",
    "metadata_repair_attempts": 1,
    "title": "",
    "description": "",
    "dry_run": true
  }
}
```

The order is validated narration → title → description → media → optional publishing. The final title is passed to existing cover/video/publishing handling, and the final description is passed to publishing. Failures block downstream media and publishing by default; `validated_rule_fallback` permits a rule candidate only if it passes validation. Metadata text, source, quality results, and attempts are retained for audit, including on failure. See [Script generation](script-generation.md) for length targets, finance risk notices, and validation limits.

**Dry-run is not an offline text mode:** LLM narration, title, and description stages still make real requests and may incur charges or send source content to the provider. Use `script_writer: rule` with metadata writers set to `rule` or `follow_script` for local-only text generation. Keep `publish: false` during review.

The source and packaged YAML templates remain `enabled: false`; this feature does not enable defaults, change publishing policy, or migrate production schedules. Long-running backends require a separately managed deployment/restart before they can execute the new code. Review and migrate existing project schedules only after verification; changing a template alone does not update their explicit parameters.

## Cache maintenance

The console settings center (定时任务 → 设置中心 → 缓存维护) scans and deletes regenerable caches through `POST /maintenance/cache-cleanup`. Scanning (`{"apply": false}`) only lists candidates and never deletes; `{"apply": true}` removes them. Responses carry paths relative to the scanned root, so the console never receives absolute filesystem locations.

The swept set is bytecode and tool state only: `__pycache__/`, `*.pyc`, `*.pyo`, `.pytest_cache`, `.ruff_cache`, `.mypy_cache`, `.hypothesis`, `.pytype`, `.ipynb_checkpoints`, and `.coverage*`. Never touched: version control data, virtual environments (recognised by `pyvenv.cfg`, so `dist/check-venv/` is skipped too), `output/`, `data/`, `logs/`, `node_modules/`, and `.codegraph/`. Build outputs (`build/`, `dist/`, `htmlcov/`, `*.egg-info/`) are opt-in and only reachable from the CLI.

`CACHE_CLEANUP_ROOT` selects the tree to sweep and defaults to the directory the backend was started from; a root that is a filesystem root, the home directory, a protected directory name, or a virtual environment is refused with `409 cache_cleanup_unavailable`. The action is a mutating request, so authenticated sessions must send the CSRF header (the console does this automatically) and it is unavailable without the Web/API boundary.

The CLI mirrors the same implementation:

```powershell
# preview only (default) — nothing is deleted
.\scripts\clean_cache.ps1
# delete the previewed items; add -IncludeBuild for build/, dist/, *.egg-info/
.\scripts\clean_cache.ps1 -Apply
# or directly, on any platform
python scripts\clean_cache.py --apply
```

## Central publish policy

The publish decision for every inline-publish pipeline (`finance`, `ai_briefing`, `script_video`, `grouped_anime`, `ai_art`) is resolved in one place:

1. `task.publish` / `task.publish_targets` set explicitly on the task always win.
2. Operator overrides saved from the web console (定时任务 → 发布设置) are stored in `<data_dir>/publish_policy.json`.
3. Packaged defaults live in `config/publish.defaults.yaml` (override path with `PUBLISH_DEFAULTS_FILE`).

Policy shape per content type:

```yaml
finance:
  enabled: false            # default when a task omits "publish"
  targets:                  # default when a task omits "publish_targets"
    - { platform: douyin, account: 金融破壁人 }
```

The same policy is readable and editable through `GET/PUT /publish-policy`, and platform account login status is exposed read-only through `GET /account-status`.

## Keep publish explicit

Pipeline param defaults never enable publishing by themselves. A task publishes when it sets `"publish": true` or when it omits `publish` and the central publish policy has `enabled: true` for its content type.

Bilibili remains blocked unless `SAU_BILIBILI_PRIVATE_ARGS=--is-only-self 1` is configured.
