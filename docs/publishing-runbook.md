# Publishing Runbook

**⚠️ DANGER: Publishing is real.** Every action in this document actually publishes content to social platforms. Follow each step carefully.

## Release-gate checklist

Before publishing from a release or deployment candidate, verify the local build and runtime contract:

- [ ] Run `bash scripts/check.sh` or `./scripts/check.ps1`; the gate must pass formatting, Ruff lint, non-external/non-publish tests, sdist/wheel content checks, `uv pip check` after normal and isolated wheel installation, installed-wheel CLI smoke tests for `capabilities` and `validate-task`, and the isolated wheel smoke that loads packaged `config/pipeline.defaults.yaml` without the repository checkout.
- [ ] Install or deploy only the verified package from `dist/check/`, or the matching `ai-pipeline-dist` CI artifact retained for 14 days from a workflow run that completed the Bash quality gate before the production Docker image build.
- [ ] Confirm the intended license with the project owner before release; do not infer or ship unapproved license metadata.
- [ ] Run `ai-pipeline capabilities` from the installed environment and confirm the expected publishing-capable pipelines are present.
- [ ] Run `ai-pipeline doctor`; resolve the external tools needed by the selected pipeline before starting the job.
- [ ] Start the service through `ai-pipeline serve` or the production `Dockerfile`; do not bypass the guarded listener with a raw non-loopback Uvicorn command.
- [ ] If using Docker Compose, create `.env` with unique `WEB_ADMIN_TOKEN`, `WEB_API_TOKEN`, and `WEB_SESSION_SECRET`, publish only on host loopback, and confirm `/health` is healthy.

## Pre-flight

```bash
ai-pipeline doctor
```

Verify:
- [ ] All external tool paths resolve for the selected pipeline
- [ ] Output directory is writable
- [ ] Browser profile is accessible (for Photo-Process pipelines)
- [ ] `publish` is `false` for the first run of any new or changed task
- [ ] `publish_targets` contains only the intended platform/account pairs
- [ ] Existing `status.json`, `events.jsonl`, and output artifacts from earlier runs have been reviewed before reusing a task

## Dry-run review

Run the exact task with publishing disabled before any upload:

```bash
ai-pipeline validate-task --task path/to/task.json
ai-pipeline run --task path/to/task.json
ai-pipeline jobs show <task_id>
ai-pipeline jobs events <task_id>
```

Verify:
- [ ] The terminal job status is `succeeded` or expected `partial`; `failed` and `blocked` must be resolved before publishing.
- [ ] `status.json` records the expected `content_type`, source material, generated artifacts, and validation evidence.
- [ ] Every generated image, video, subtitle, narration, and thumbnail intended for publication has been reviewed by an operator.
- [ ] `artifacts.validation` proves media decodability and required audio/subtitle evidence for the pipeline.
- [ ] No unexpected platform appears in `artifacts.publish_results` from prior attempts.

## Publish execution

Only after the dry-run review passes:

1. Change the reviewed task to include explicit `"publish": true` and the intended `publish_targets`.
2. Run `ai-pipeline validate-task --task path/to/task.json` again and confirm the normalized target list.
3. Submit the task from the CLI or authenticated Web console. Web publishing also requires `WEB_PUBLISH_ENABLED=true` and the task-bound warning confirmation.
4. Watch `ai-pipeline jobs events <task_id>` until the job reaches a terminal state.
5. Archive `status.json`, `events.jsonl`, generated artifacts, and platform proof for release evidence.

Completion evidence:
- [ ] `status.json` has terminal status `succeeded` or documented `partial`.
- [ ] `artifacts.publish_results` records each requested platform target and its visibility/draft proof.
- [ ] Douyin/Kuaishou uploads include proof of the selected visibility; private Bilibili includes only-self proof.
- [ ] Any failed target is recorded as `partial`, `failed`, or `blocked`; never treat missing proof as success.

## Platform-Specific Guides

### Douyin (抖音)

1. **Pre-flight**: Log into Douyin Creator account in the SAU-authenticated browser session.
2. **Dry-run**: Run the task with `"publish": false` first. Review the generated video.
3. **Publish**: Set `"publish": true` and run with target (add `"visibility": "public"` to publish openly):
   ```json
   {"platform": "douyin", "account": "account-name"}
   ```
4. **Verify**: Check `publish_results` in status.json. Must show proof of the selected visibility ("仅自己可见" or "公开可见").
5. **Rollback**: Delete the video from Douyin Creator Center if needed.

### Kuaishou (快手)

1. **Pre-flight**: Log into Kuaishou Creator account.
2. **Dry-run**: Same as Douyin.
3. **Publish**: Set `"publish": true` with target:
   ```json
   {"platform": "kuaishou", "account": "account-name"}
   ```
4. **Verify**: Check the selected-visibility proof in publish_results.
5. **Rollback**: Delete from Kuaishou Creator Center.

### Bilibili (B站)

1. **Pre-flight**: Verify `SAU_BILIBILI_PRIVATE_ARGS=--is-only-self 1` in `.env`.
2. **Dry-run**: Same as above.
3. **Publish**: Requires `tid` (category ID) in publish target:
   ```json
   {"platform": "bilibili", "account": "account-name", "tid": 27}
   ```
4. **Verify**: Private targets must include `--is-only-self 1` and are blocked if private args are missing; public targets omit the flag.
5. **Rollback**: Delete from Bilibili Creator Center.

## Safety Rules (Fail-Closed)

- All publish defaults to `false`. Explicit `"publish": true` required.
- Every target defaults to `"visibility": "private"`. Explicit `"visibility": "public"` publishes openly; the choice is part of the confirmation fingerprint.
- Douyin/Kuaishou: must prove the selected visibility ("仅自己可见" or "公开可见").
- Bilibili: private targets must pass `--is-only-self 1`, blocked if `SAU_BILIBILI_PRIVATE_ARGS` is missing; public targets omit the flag.
- Any uncertainty → `BLOCKED` status. No silent publication.

## Troubleshooting

| Problem | Check |
|---|---|
| `PRIVATE_VISIBILITY_UNSUPPORTED` | SAU version supports private args? Account logged in? |
| `ALREADY_PUBLISHED` | Video hash matches a prior upload. Force re-encode or change content. |
| `PUBLISH_FAILED` | Check SAU logs. Account session valid? Platform rate limits? |
| `BLOCKED` on Bilibili | `SAU_BILIBILI_PRIVATE_ARGS` env var set? |
