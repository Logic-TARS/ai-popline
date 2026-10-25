# Web Operator Console

AI Pipeline includes a no-Node, no-CDN operator console packaged with the Python service. It is intended for one operator on loopback or the guarded ZeroTier boundary. Production handoff should use the packaged `ai-pipeline serve` entry point only after `bash scripts/check.sh` has passed the package, dependency, installed-wheel, and isolated-wheel smoke gates.

## Start and open

```powershell
ai-pipeline serve
```

Open `http://127.0.0.1:8080/` for the default loopback configuration. A configured ZeroTier deployment uses its HTTPS origin instead; follow the [ZeroTier Web Access Runbook](zerotier-web-access.md).

When `WEB_AUTH_REQUIRED=true`, enter `WEB_ADMIN_TOKEN` on the login screen. The token is exchanged for a signed, HttpOnly session cookie that lasts 7 days by default; the console automatically supplies the separate CSRF token for writes and clears both cookies when you log out. Publishing has an independently configurable recent-login window that also defaults to 7 days. Operators can shorten `WEB_PUBLISH_REAUTH_SECONDS` to require renewed administrator authentication for publishing without treating the normal session as expired; leaving it equal to the session lifetime reduces protection against publishing from a stolen or unattended session.

## Content studio

The default workspace is **内容工作台**. It includes a financial template that collects current research through a fixed read-only ttskill allowlist—gold, bond market, China macro data, and up to five operator-selected stock quotes—and an AI briefing template that imports the latest dated `article.md` and handoff metadata from `AI_BRIEFING_DIR`. Account, holding, profit, order, and trading Skills cannot be invoked from the browser.

Research/import runs in the background and each source retains both its collection time and the source-provided data time. Partial results remain available when one Skill fails. Review the source cards, manually write and save a 350–500 character narration containing the investment-risk disclaimer, then select **保存并生成视频**. This always queues a local-only `script_video` MoneyPrinterTurbo job with `publish=false` and switches to the task center. Content Studio never uploads during generation.

Drafts and sanitized source results are stored under `output/content-drafts/<draft_id>/`. ttskill credentials, cookies, identity data, and arbitrary gateway metadata are not stored or returned by the content API. If the local CLI session expires, run `ttskill login` interactively on the server and refresh the research.

The draft library supports create, read, revision-checked update, single delete, and batch delete. A draft with active research collection cannot be deleted; retry after collection reaches a terminal state. Batch deletion reports deleted, blocked, missing, and failed IDs independently instead of hiding partial success.

## Dashboard

The **任务中心** shows:

- recent-job counts and active/attention totals;
- storage, profiles, packaged pipeline defaults, and Web publishing readiness flags without local paths;
- status, pipeline, and text filters;
- automatic 2.5-second refresh while jobs are queued or running, and slower idle refresh;
- separate generation and publication status columns, so a generated video is never presented as already published;
- the selected job's current step, timestamps, validation evidence, publication account/evidence, events, and raw snapshot.

For a running **口播视频** task, the detail panel also shows a live progress bar. It reports saved-script, MoneyPrinterTurbo startup, recognized downstream milestones (TTS/subtitles, material retrieval, and rendering), artifact copy, and media validation. Downstream milestones are explicitly labeled **预计进度** when MoneyPrinterTurbo does not expose a machine-readable percentage; task paths and raw tool output are never shown. Historical jobs created before this feature retain their existing step history but cannot be assigned reconstructed progress.

Polling pauses while the browser tab is hidden. Use **Refresh** for an immediate update.

Recent tasks can be renamed, read, deleted individually, or deleted in a selected batch. Renaming writes a separate display name and never mutates the original task description, execution parameters, event history, artifacts, status, or publication evidence. Queued/running tasks and tasks with an active publication attempt cannot be deleted. These restrictions are enforced by the API even if a browser control is bypassed.

The console and API expose a versioned CRUD capability contract through `/ui/bootstrap`. If the browser reports `405 Method Not Allowed` after an upgrade, the static files and backend route table are out of sync: restart the non-reloading `ai-pipeline serve` process, then refresh the page. The console disables unsupported CRUD controls when it detects an older backend contract.

## Scheduled tasks

The **定时任务** workspace manages recurring task schedules. Each schedule binds a name, a pipeline, task parameters (description, topic, optional params JSON), and a daily or weekly local run time. A background runner inside the web process checks schedules every 30 seconds and submits due tasks through the same orchestrator path as manual runs, so results appear in the **任务中心** like any other job.

- Schedules are stored as JSON files under `<data_dir>/schedules/` and survive restarts.
- Scheduled tasks run with `publish=false` by default; the create dialog offers a **生成后尝试发布** toggle to opt into publishing after generation and validation. Publishing schedules are rejected with 403 while `WEB_PUBLISH_ENABLED` is off, matching the manual run policy.
- Each schedule can be edited in place (**编辑** updates name, timing, pipeline, task parameters, and the publish toggle), enabled/disabled (disabling clears the next run time), deleted, or triggered immediately with **立即运行**, which does not disturb the regular cadence.
- The `/schedules` API requires the same authentication as the rest of the console, and the `task_schedules` feature flag in `/ui/bootstrap` advertises availability.

## Create a task

Select **New task**, then choose one of the six pipelines. The form is generated from the authenticated `/ui/bootstrap` contract and presents only that pipeline's common parameters.

1. Enter a description and optional topic.
2. Fill required pipeline fields; safe dry-run defaults are enabled where supported.
3. Keep publishing off for the first run.
4. Optionally add an advanced JSON object; advanced keys override matching form fields.
5. Select **Preflight task** to validate and normalize the task without creating a job or calling an external tool.
6. Review the final JSON and select **Start run**.

Path fields are paths on the AI Pipeline server workstation. Browsers cannot browse arbitrary server files, and a path from a different client operating system is not uploaded or translated automatically.

## Job details and artifacts

Select a task row to view its step track and append-only event timeline. The details panel shows media validation, grouped results, and an evidence-based publication summary. Content Studio tasks move independently through **待生成**, **未发布**, **发布中**, **已发布**, **部分发布**, or **发布失败**; **已成功** in the generation column only means the video was generated and validated. Until an uploader returns success evidence, the publication card explicitly says that the video has not been sent to any social-media account.

A completed, non-dry-run Content Studio video with a guarded in-job artifact and successful media validation exposes **设置发布**. Successful targets display the platform, selected account, private visibility, completion time, and uploader proof; failed targets display their error and can be retried. The publication attempt has its own queued/running/terminal state and history, and it never changes the generation job's terminal status.

Only files registered in `JobSnapshot.artifacts` and resolving inside that job's directory appear in the artifact gallery. Images and videos can be previewed; other registered files open through the same authenticated, containment-checked endpoint. External source documents and arbitrary workstation paths are never served.

## Publishing

Content Studio publishing is a separate, post-generation operation. Generating content or merely entering the task center never invokes an uploader; the console shows **已发布** only after explicit confirmation and a platform success proof:

1. Generate the video in Content Studio and review the validated artifact in the task center.
2. Ensure `WEB_PUBLISH_ENABLED=true` is configured on the server.
3. Select **设置发布** on the completed task and adjust platform, account, visibility, title, description, and tags. Each target defaults to **仅自己可见**; selecting **公开可见** publishes openly, is highlighted in the confirmation dialog, and changes the confirmation fingerprint.
4. Type `确认发布` and submit. The backend binds `PUBLISH:<fingerprint>` to the source task ID, current video SHA-256, and the complete normalized publication request (including each target's visibility).

The publisher reuses the guarded video artifact and never reruns MoneyPrinterTurbo. Video replacement after confirmation invalidates the fingerprint. An active attempt blocks concurrent publication; completed partial or failed attempts can be adjusted and retried. Per-platform results are independent, while recent-login, private visibility, artifact containment, deduplication, and uploader guards remain enforced.

The generic **新建任务** dialog retains its existing optional generate-and-publish workflow for non-Content-Studio use. Content Studio itself has no publishing switch.

## Common errors

- **401 / login screen:** the 7-day session expired or its signature is invalid; log in again.
- **Recent authentication required for publishing:** the normal session remains valid, but the configured publishing authentication window elapsed; log in again before publishing. This normally appears only when `WEB_PUBLISH_REAUTH_SECONDS` has been set shorter than the session lifetime.
- **CSRF failed:** refresh the page and log in again so the session and CSRF cookies match.
- **Peer, Host, Origin, or HTTPS rejected:** review the ZeroTier listener, firewall, allowed hosts/origins, and certificate configuration.
- **Preflight field errors:** correct the named form field or advanced JSON value; preflight does not create a failed job.
- **No artifacts:** the job may still be running, may have failed before output, or may only reference files outside its guarded job directory.
- **Publishing disabled:** keep publishing off, or deliberately configure the server-side Web publishing guard after reviewing the publishing runbook.
