from __future__ import annotations

import json
import re
from concurrent.futures import Future, ThreadPoolExecutor, as_completed
from datetime import UTC, datetime, timedelta
from typing import Any

from content_pipeline.ai_briefing_pipeline import build_briefing_script_material
from content_pipeline.content_studio.models import ContentDraft, CreateDraftInput, ResearchSource
from content_pipeline.content_studio.store import ContentDraftStore, DraftConflictError, DraftCorruptError
from content_pipeline.content_studio.ttskill_client import TTSkillClient
from content_pipeline.finance_pipeline import (
    DISCLAIMER,
    RISK_NOTE,
    build_finance_script_material,
    extract_response,
    select_daily_markdown,
    validate_finance_narration,
)
from content_pipeline.finance_script import build_structured_finance_script
from content_pipeline.script_generation import ScriptGenerationService, build_structured_facts, validate_script
from content_pipeline.settings import Settings
from content_pipeline.tools.narrated_mpt_client import looks_like_file_reference

__all__ = ["ContentStudioService"]

FINANCE_DISCLAIMER = "以上内容仅为市场信息整理，不构成投资建议。"
FINANCE_DEFAULT_SOURCE_IDS = ["finance-daily", "gold", "bond", "macro-cn"]
TEMPLATES = [
    {
        "template_id": "finance_90s",
        "label": "90 秒金融资讯口播",
        "description": "汇总黄金、债市、宏观和自选股票的最新只读数据，自动生成可审阅的90秒口播初稿。",
        "default_title": "今日金融资讯",
        "supports_focus_assets": True,
        "requires_finance_disclaimer": True,
        "target_chars": {"minimum": 300, "maximum": 600},
        "sources": ["黄金行情", "债市晴雨表", "中国宏观数据", "关注股票（可选）"],
    },
    {
        "template_id": "ai_briefing_90s",
        "label": "90 秒 AI 简报",
        "description": "读取 AI_BRIEFING_DIR 中最新的 article.md 和交接信息，自动生成可审阅的90秒 AI 简报口播初稿。",
        "default_title": "每日 AI 简报",
        "supports_focus_assets": False,
        "requires_finance_disclaimer": False,
        "target_chars": {"minimum": 350, "maximum": 500},
        "sources": ["最新 AI 简报文章与交接信息"],
    },
]


class ContentStudioService:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.store = ContentDraftStore(settings.data_dir)
        self.client = TTSkillClient(settings)
        self.executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="content-research")
        self._futures: dict[str, Future] = {}

    def shutdown(self) -> None:
        self.executor.shutdown(wait=False, cancel_futures=True)

    def bootstrap(self) -> dict[str, Any]:
        return {"templates": TEMPLATES, "ttskill": self.client.readiness(), "disclaimer": FINANCE_DISCLAIMER}

    def create(self, request: CreateDraftInput) -> ContentDraft:
        focus_assets = request.focus_assets if request.template_id == "finance_90s" else []
        enabled_source_ids = self._normalize_enabled_source_ids(
            request.template_id, request.enabled_source_ids, default_all=True
        )
        sources = self._pending_sources(request.template_id, focus_assets, enabled_source_ids)
        draft = self.store.create(
            template_id=request.template_id,
            title=request.title.strip(),
            focus_assets=focus_assets,
            enabled_source_ids=enabled_source_ids,
            research=sources,
        )
        self._queue_collection(draft.draft_id)
        return draft

    def refresh(
        self, draft_id: str, expected_revision: int, enabled_source_ids: list[str] | None = None
    ) -> ContentDraft:
        draft = self.store.get(draft_id)
        if draft.revision != expected_revision:
            raise DraftConflictError("content draft changed; reload it before refreshing")
        if enabled_source_ids is not None:
            draft.enabled_source_ids = self._normalize_enabled_source_ids(draft.template_id, enabled_source_ids)
        effective_source_ids = self._enabled_source_ids(draft)
        draft.research = self._pending_sources(draft.template_id, draft.focus_assets, effective_source_ids)
        draft.status = "collecting"
        draft.research_error = None
        saved = self.store.save(draft, expected_revision=expected_revision)
        self._queue_collection(draft_id)
        return saved

    def update(self, draft_id: str, *, revision: int, title: str, script: str) -> ContentDraft:
        draft = self.store.get(draft_id)
        if draft.revision != revision:
            raise DraftConflictError("content draft changed; reload it before saving")
        draft.title = title.strip()
        draft.script = script.strip()
        return self.store.save(draft, expected_revision=revision)

    def register_video_task(self, draft_id: str, revision: int, task_id: str) -> ContentDraft:
        draft = self.store.get(draft_id)
        if draft.revision != revision:
            raise DraftConflictError("content draft changed; save the latest version before generating video")
        draft.video_task_ids.append(task_id)
        return self.store.save(draft, expected_revision=revision)

    def delete(self, draft_id: str) -> None:
        draft = self.store.get(draft_id)
        active = self._futures.get(draft_id)
        if draft.status == "collecting" or (active is not None and not active.done()):
            raise DraftConflictError("content draft collection is active and cannot be deleted")
        self.store.delete(draft_id)
        self._futures.pop(draft_id, None)

    def delete_many(self, draft_ids: list[str]) -> dict[str, list[str]]:
        result = {"deleted": [], "blocked": [], "not_found": [], "failed": []}
        for draft_id in dict.fromkeys(draft_ids):
            try:
                self.delete(draft_id)
                result["deleted"].append(draft_id)
            except DraftConflictError:
                result["blocked"].append(draft_id)
            except FileNotFoundError:
                result["not_found"].append(draft_id)
            except (DraftCorruptError, OSError):
                result["failed"].append(draft_id)
        return result

    def validate_for_video(self, draft: ContentDraft) -> list[str]:
        script_errors = self._script_validation_errors(draft)
        script_ready = not script_errors
        errors: list[str] = []
        if draft.status == "collecting":
            errors.append("最新资料仍在采集中，请完成资料核对后再生成视频")
        elif not any(source.status == "succeeded" for source in draft.research) and not script_ready:
            errors.append("没有可供核对的最新资料，不能生成视频")
        errors.extend(script_errors)
        if not draft.title.strip():
            errors.append("视频标题不能为空")
        return errors

    def prepare_for_video(self, draft_id: str, revision: int) -> ContentDraft:
        draft = self.store.get(draft_id)
        if draft.revision != revision:
            raise DraftConflictError("content draft changed; save the latest version first")
        if draft.template_id == "finance_90s" and (draft.status != "ready" or self._script_validation_errors(draft)):
            self.collect_now(draft_id)
            return self.store.get(draft_id)
        return draft

    def _script_validation_errors(self, draft: ContentDraft) -> list[str]:
        script = draft.script.strip()
        errors: list[str] = []
        if not script:
            errors.append("口播稿不能为空")
        if looks_like_file_reference(script) or re.search(r"(?:[A-Za-z]:[\\/]|/home/).+\.(?:md|txt|json)", script):
            errors.append("口播稿不能是文件路径或包含本地文件引用")
        if any(marker in script for marker in ("请根据左侧", "待补充", "TODO", "{{", "}}")):
            errors.append("口播稿仍包含未完成的占位内容")
        content_type = "finance_multi_asset" if draft.template_id == "finance_90s" else "ai_briefing"
        material = (
            {"modules": {"review": "人工审核稿"}, "risk_note": RISK_NOTE, "disclaimer": FINANCE_DISCLAIMER}
            if draft.template_id == "finance_90s"
            else {"summary": "人工审核稿"}
        )
        report = validate_script(
            script, build_structured_facts(content_type, material), profile="reviewed", gate="strict"
        )
        for issue in report.errors:
            if issue.code == "finance_disclaimer":
                errors.append(f"口播稿必须包含风险提示：{FINANCE_DISCLAIMER}")
            elif issue.code == "finance_risk_warning":
                errors.append("财经口播稿必须包含市场风险提示")
            elif issue.code == "finance_prohibited":
                errors.append("财经口播稿包含确定性或诱导性投资表述")
            elif issue.code in {
                "file_path",
                "markdown",
                "explanation_prefix",
                "sentence_end",
                "punctuation",
                "unbalanced_punctuation",
            }:
                errors.append(issue.message)
        return errors

    def collect_now(self, draft_id: str) -> None:
        draft = self._ensure_current_research_sources(self.store.get(draft_id))
        results: dict[str, ResearchSource] = {}
        finance_fallback_sources: list[ResearchSource] = []
        if draft.template_id == "ai_briefing_90s":
            original = draft.research[0]
            try:
                results[original.source_id] = self._collect_ai_briefing_source(draft_id)
            except Exception as exc:
                results[original.source_id] = original.model_copy(
                    update={"status": "failed", "error": str(exc)[:300], "fetched_at": self._now()}
                )
        elif draft.template_id == "finance_90s":
            enabled_source_ids = set(self._enabled_source_ids(draft))
            daily_source = next((item for item in draft.research if item.source_id == "finance-daily"), None)
            if "finance-daily" in enabled_source_ids:
                try:
                    results["finance-daily"] = self._collect_finance_daily_source(draft_id)
                except Exception as exc:
                    if daily_source is not None:
                        results["finance-daily"] = daily_source.model_copy(
                            update={"status": "failed", "error": str(exc)[:300], "fetched_at": self._now()}
                        )
            finance_fallback_sources = [
                ResearchSource(source_id=source_id, label=label, skill_id=skill_id)
                for source_id, label, skill_id, _body in self._source_requests(draft.focus_assets, enabled_source_ids)
            ]
            requests = self._source_requests(draft.focus_assets, enabled_source_ids)
            if requests:
                with ThreadPoolExecutor(max_workers=min(4, len(requests)), thread_name_prefix="ttskill") as pool:
                    futures = {
                        pool.submit(self._collect_source, draft_id, source_id, label, skill_id, body): source_id
                        for source_id, label, skill_id, body in requests
                    }
                    for future in as_completed(futures):
                        source_id = futures[future]
                        try:
                            results[source_id] = future.result()
                        except Exception as exc:
                            original = next(item for item in finance_fallback_sources if item.source_id == source_id)
                            results[source_id] = original.model_copy(
                                update={"status": "failed", "error": str(exc)[:300], "fetched_at": self._now()}
                            )
        else:
            requests = self._source_requests(draft.focus_assets)
            with ThreadPoolExecutor(max_workers=min(4, len(requests)), thread_name_prefix="ttskill") as pool:
                futures = {
                    pool.submit(self._collect_source, draft_id, source_id, label, skill_id, body): source_id
                    for source_id, label, skill_id, body in requests
                }
                for future in as_completed(futures):
                    source_id = futures[future]
                    try:
                        results[source_id] = future.result()
                    except Exception as exc:
                        original = next(item for item in draft.research if item.source_id == source_id)
                        results[source_id] = original.model_copy(
                            update={"status": "failed", "error": str(exc)[:300], "fetched_at": self._now()}
                        )

        collected = draft.model_copy(deep=True)
        collected.research = [
            results.get(item.source_id, item) for item in self._ensure_current_research_sources(collected).research
        ]
        succeeded = sum(item.status == "succeeded" for item in collected.research)
        failed = sum(item.status == "failed" for item in collected.research)
        daily_succeeded = collected.template_id == "finance_90s" and any(
            item.source_id == "finance-daily" and item.status == "succeeded" for item in collected.research
        )
        collected.status = (
            "ready" if succeeded and (not failed or daily_succeeded) else "partial" if succeeded else "failed"
        )
        collected.research_error = (
            None if daily_succeeded else "部分资料获取失败，可刷新重试。" if succeeded and failed else None
        )
        if collected.template_id == "finance_90s" and not collected.research:
            collected.research_error = "没有启用任何财经资料源，无法生成初稿。"
        elif not succeeded:
            collected.research_error = (
                "未找到可用的最新 AI 简报文章，请检查 AI_BRIEFING_DIR。"
                if collected.template_id == "ai_briefing_90s"
                else "没有启用或获取到可用于生成初稿的财经资料。"
            )
        elif not collected.script.strip():
            try:
                collected.script, collected.script_generation = self._generate_initial_script(collected)
                if collected.template_id == "ai_briefing_90s" and collected.title == "每日 AI 简报":
                    collected.title = str(collected.research[0].payload.get("title") or collected.title)
            except Exception as exc:
                warning = f"资料已就绪，但自动初稿生成失败：{str(exc)[:180]}"
                collected.research_error = (
                    f"{collected.research_error} {warning}" if collected.research_error else warning
                )
                if collected.template_id == "finance_90s":
                    collected.status = "failed"

        def finish(current: ContentDraft) -> None:
            current.research = collected.research
            current.status = collected.status
            current.research_error = collected.research_error
            if current.revision == draft.revision and not current.script.strip():
                current.script = collected.script
                current.script_generation = collected.script_generation
                current.title = collected.title

        self.store.mutate(draft_id, finish)

    def _queue_collection(self, draft_id: str) -> None:
        active = self._futures.get(draft_id)
        if active and not active.done():
            return
        self._futures[draft_id] = self.executor.submit(self.collect_now, draft_id)

    def _collect_source(
        self, draft_id: str, source_id: str, label: str, skill_id: str, body: dict[str, Any]
    ) -> ResearchSource:
        business = self.client.invoke(skill_id, body)
        normalized = self._normalize(skill_id, business)
        self.store.write_source(source_id=source_id, draft_id=draft_id, payload=normalized)
        return ResearchSource(
            source_id=source_id,
            label=label,
            skill_id=skill_id,
            status="succeeded",
            fetched_at=self._now(),
            data_as_of=self._find_as_of(normalized),
            summary=self._summary(skill_id, normalized),
            payload=normalized,
        )

    def _collect_finance_daily_source(self, draft_id: str) -> ResearchSource:
        source_root = self.settings.finance_md_dir.expanduser().resolve()
        source = select_daily_markdown(source_root, "auto")
        date_match = re.match(r"(\d{4}-\d{2}-\d{2})", source.name)
        publication_date = date_match.group(1) if date_match else datetime.now().date().isoformat()
        response = extract_response(source.read_text(encoding="utf-8-sig"))
        payload = {"date": publication_date, "path": str(source), "response": response[:80_000]}
        self.store.write_source(draft_id=draft_id, source_id="finance-daily", payload=payload)
        return ResearchSource(
            source_id="finance-daily",
            label="当天基金日报",
            skill_id="FINANCE_DAILY_MARKDOWN",
            status="succeeded",
            fetched_at=self._now(),
            data_as_of=publication_date,
            summary=f"{publication_date} · {source.name}",
            payload=payload,
        )

    def _collect_ai_briefing_source(self, draft_id: str) -> ResearchSource:
        source_root = self.settings.ai_briefing_dir.expanduser().resolve()
        if not source_root.is_dir():
            raise FileNotFoundError("AI_BRIEFING_DIR does not exist")
        today = datetime.now().strftime("%Y%m%d")
        candidates = sorted(
            (
                path
                for path in source_root.iterdir()
                if path.is_dir() and re.fullmatch(r"\d{8}", path.name) and path.name <= today
            ),
            key=lambda path: path.name,
            reverse=True,
        )
        day_dir = next((path for path in candidates if (path / "article.md").is_file()), None)
        if day_dir is None:
            raise FileNotFoundError("no dated AI briefing article.md was found")
        article_path = day_dir / "article.md"
        if article_path.stat().st_size > 250_000:
            raise ValueError("AI briefing article exceeds the safe size limit")
        article = article_path.read_text(encoding="utf-8-sig")
        handoff_path = day_dir / "video_handoff.json"
        try:
            handoff = json.loads(handoff_path.read_text(encoding="utf-8")) if handoff_path.is_file() else {}
        except (OSError, json.JSONDecodeError):
            handoff = {}
        title_match = re.search(r"(?m)^标题:\s*(.+)$", article)
        summary_match = re.search(r"(?m)^摘要:\s*(.+)$", article)
        title = str(handoff.get("title") or (title_match.group(1).strip() if title_match else "每日 AI 简报"))
        normalized = {
            "date": day_dir.name,
            "title": title,
            "summary": summary_match.group(1).strip() if summary_match else "",
            "article_markdown": article[:50_000],
            "handoff_created_at": handoff.get("created_at"),
            "platforms": handoff.get("platforms") or [],
        }
        self.store.write_source(draft_id=draft_id, source_id="ai-briefing", payload=normalized)
        return ResearchSource(
            source_id="ai-briefing",
            label="最新 AI 简报文章",
            skill_id="AI_BRIEFING_HANDOFF",
            status="succeeded",
            fetched_at=self._now(),
            data_as_of=day_dir.name,
            summary=f"{day_dir.name} · {title}",
            payload=normalized,
        )

    def _ensure_current_research_sources(self, draft: ContentDraft) -> ContentDraft:
        if draft.template_id != "finance_90s":
            return draft
        current_sources = {source.source_id: source for source in draft.research}
        normalized = [
            current_sources.get(source.source_id, source)
            for source in self._pending_sources("finance_90s", draft.focus_assets, self._enabled_source_ids(draft))
        ]
        if [source.source_id for source in normalized] == [source.source_id for source in draft.research]:
            return draft
        return draft.model_copy(update={"research": normalized})

    def _enabled_source_ids(self, draft: ContentDraft) -> list[str]:
        return self._normalize_enabled_source_ids(draft.template_id, draft.enabled_source_ids, default_all=True)

    @staticmethod
    def _normalize_enabled_source_ids(
        template_id: str, source_ids: list[str] | None, *, default_all: bool = False
    ) -> list[str]:
        if template_id != "finance_90s":
            return []
        if source_ids is None:
            return FINANCE_DEFAULT_SOURCE_IDS.copy() if default_all else []
        allowed = set(FINANCE_DEFAULT_SOURCE_IDS)
        return [source_id for source_id in source_ids if source_id in allowed]

    def _pending_sources(
        self, template_id: str, focus_assets: list[str], enabled_source_ids: list[str] | None = None
    ) -> list[ResearchSource]:
        if template_id == "ai_briefing_90s":
            return [
                ResearchSource(
                    source_id="ai-briefing",
                    label="最新 AI 简报文章",
                    skill_id="AI_BRIEFING_HANDOFF",
                )
            ]
        if template_id == "finance_90s":
            enabled = set(FINANCE_DEFAULT_SOURCE_IDS if enabled_source_ids is None else enabled_source_ids)
            sources = []
            if "finance-daily" in enabled:
                sources.append(
                    ResearchSource(
                        source_id="finance-daily",
                        label="当天基金日报",
                        skill_id="FINANCE_DAILY_MARKDOWN",
                    )
                )
            sources.extend(
                ResearchSource(source_id=source_id, label=label, skill_id=skill_id)
                for source_id, label, skill_id, _body in self._source_requests(focus_assets, enabled)
            )
            return sources
        return [
            ResearchSource(source_id=source_id, label=label, skill_id=skill_id)
            for source_id, label, skill_id, _body in self._source_requests(focus_assets)
        ]

    def _source_requests(self, focus_assets: list[str], enabled_source_ids: set[str] | None = None):
        now = datetime.now(UTC).date()
        enabled = set(FINANCE_DEFAULT_SOURCE_IDS) if enabled_source_ids is None else enabled_source_ids
        requests = []
        if "gold" in enabled:
            requests.append(("gold", "黄金行情", "TTFUND_GOLD_INFO", {"query_scope": "all"}))
        if "bond" in enabled:
            requests.append(("bond", "债市晴雨表", "TTFUND_BOND_MARKET", {}))
        if "macro-cn" in enabled:
            requests.append(
                (
                    "macro-cn",
                    "中国宏观数据",
                    "TTFUND_MACRO_DATA",
                    {
                        "region": "cn",
                        "categories": "cpi,ppi,pmi,m2,afre,usdcny",
                        "start_date": (now - timedelta(days=180)).isoformat(),
                        "end_date": now.isoformat(),
                    },
                )
            )
        requests.extend(
            (f"stock-{index}", f"股票 · {asset}", "TTFUND_STOCK_PRICE_QUERY", {"query": asset})
            for index, asset in enumerate(focus_assets, start=1)
        )
        return requests

    def _generate_initial_script(self, draft: ContentDraft) -> tuple[str, dict[str, Any]]:
        generator = ScriptGenerationService(self.settings)
        if draft.template_id == "ai_briefing_90s":
            source = next(item for item in draft.research if item.status == "succeeded")
            article = str(source.payload.get("article_markdown") or "")
            date = str(source.payload.get("date") or datetime.now().strftime("%Y%m%d"))
            material = build_briefing_script_material(article, date)
            result = generator.generate(
                content_type="ai_briefing",
                material=material,
                fallback_script=str(material.get("fallback_script") or ""),
                failure_policy="validated_rule_fallback",
                repair_attempts=1,
            )
            return result.script, result.audit.model_dump(mode="json")
        return self._build_finance_initial_script(draft)

    def _build_finance_initial_script(self, draft: ContentDraft) -> tuple[str, dict[str, Any]]:
        sources = {item.source_id: item.payload for item in draft.research if item.status == "succeeded"}
        daily = sources.get("finance-daily")
        if not daily:
            if sources:
                script = build_structured_finance_script(sources, disclaimer=FINANCE_DISCLAIMER)
                if not any(marker in script for marker in ("风险承受能力", "审慎决策", "市场波动", "注意风险")):
                    script = script.removesuffix(FINANCE_DISCLAIMER)
                    script += "面对市场波动，请结合自身风险承受能力审慎决策。" + FINANCE_DISCLAIMER
                material = {
                    "modules": sources,
                    "risk_note": "面对市场波动，请结合自身风险承受能力审慎决策。",
                    "disclaimer": FINANCE_DISCLAIMER,
                }
                result = ScriptGenerationService(self.settings).validate_rule(
                    content_type="finance_multi_asset",
                    material=material,
                    script=script,
                )
                audit = result.audit.model_dump(mode="json")
                audit["generation_mode"] = "validated_rule_fallback"
                return result.script, audit
            raise ValueError("没有启用或获取到可用于生成初稿的财经资料")
        response = str(daily.get("response") or "")
        publication_date = str(daily.get("date") or datetime.now().date().isoformat())
        material = build_finance_script_material(response, publication_date)
        fallback = str(material.get("fallback_script") or "")
        if fallback:
            try:
                ScriptGenerationService(self.settings).validate_rule(
                    content_type="finance",
                    material=material,
                    script=fallback,
                )
            except Exception:
                fallback = ""
        if not fallback:
            fallback = self._build_minimal_finance_daily_script(material)
        result = ScriptGenerationService(self.settings).generate(
            content_type="finance",
            material=material,
            fallback_script=fallback,
            failure_policy="validated_rule_fallback",
            repair_attempts=1,
        )
        return result.script, result.audit.model_dump(mode="json")

    def _finance_fallback_script(self, material: dict[str, object], original_error: Exception) -> str:
        errors: list[str] = []
        fallback_script = str(material.get("fallback_script") or "").strip()
        if fallback_script:
            try:
                validate_finance_narration(fallback_script, material, enforce_length=False)
                return fallback_script
            except Exception as exc:
                errors.append(str(exc))
        try:
            minimal_script = self._build_minimal_finance_daily_script(material)
            validate_finance_narration(minimal_script, material, enforce_length=False)
            return minimal_script
        except Exception as exc:
            errors.append(str(exc))
        detail = "；".join(errors) or str(original_error)
        raise ValueError(f"金融口播初稿 LLM 生成失败，且规则兜底稿不合规：{detail}") from original_error

    @staticmethod
    def _build_minimal_finance_daily_script(material: dict[str, object]) -> str:
        publication_date = str(material.get("publication_date") or "").strip()
        date_text = publication_date.replace("-", "年", 1).replace("-", "月", 1) + "日" if publication_date else "今日"
        summary = str(material.get("summary") or "").strip("，。； ")
        index_summary = str(material.get("index_summary") or "").strip("，。； ")
        sector_summary = str(material.get("sector_summary") or "").strip("，。； ")
        indices = ContentStudioService._finance_index_text(material.get("indices")) or index_summary
        sectors = ContentStudioService._finance_sector_text(material.get("sector_rows")) or sector_summary
        if not indices or not sectors:
            raise ValueError("每日金融报告缺少可解析的指数或板块信息，无法生成合规口播初稿")
        parts = [f"{date_text}A股收盘观察。"]
        if summary:
            parts.append(summary + "。")
        parts.append("指数方面，" + indices + "。")
        parts.append("板块方面，" + sectors + "。")
        keyword_summary = str(material.get("keyword_summary") or "").strip("，。； ")
        if keyword_summary:
            parts.append("信号方面，" + keyword_summary + "。")
        strategy_summary = str(material.get("strategy_summary") or "").strip("，。； ")
        if strategy_summary:
            parts.append("策略上，" + strategy_summary + "。")
        parts.append("短期盘面只代表当天资金偏好，不能直接外推为中长期趋势。")
        parts.append("接下来仍需观察成交变化、指数承接、板块持续性和资金风格是否延续，再判断当日信号有没有形成趋势。")
        parts.append("具体操作应结合个人持仓成本、投资期限和可承受波动，不能仅凭单日涨跌追高或恐慌离场。")
        parts.append(RISK_NOTE)
        parts.append(DISCLAIMER)
        return "".join(parts)

    @staticmethod
    def _finance_index_text(value: object) -> str:
        rows = value if isinstance(value, list) else []
        items = []
        for row in rows[:4]:
            if not isinstance(row, list | tuple) or len(row) < 3:
                continue
            items.append(f"{row[0]}报{row[1]}点，{row[2]}")
        return "；".join(items)

    @staticmethod
    def _finance_sector_text(value: object) -> str:
        rows = value if isinstance(value, list) else []
        items = []
        for row in rows:
            if not isinstance(row, list | tuple) or len(row) < 2:
                continue
            cells = [str(cell).strip() for cell in row if str(cell).strip()]
            if not cells or cells[0] in {"板块", "品种"}:
                continue
            details = cells[:2]
            number = next((cell for cell in reversed(cells[2:]) if re.search(r"\d", cell)), "")
            if number:
                details.append(number)
            items.append("".join(details))
            if len(items) == 4:
                break
        return "；".join(items)

    @classmethod
    def _normalize(cls, skill_id: str, business: dict[str, Any]) -> dict[str, Any]:
        root = business.get("data") if isinstance(business.get("data"), dict) else business
        if skill_id == "TTFUND_BOND_MARKET":
            modules = root.get("module_1") if isinstance(root, dict) else None
            module = modules[0] if isinstance(modules, list) and modules and isinstance(modules[0], dict) else {}
            return cls._compact(
                {
                    "update_time": module.get("updateTime") or root.get("updateTime"),
                    "markdown": module.get("markdown"),
                    "text_summary": module.get("textSummary"),
                    "market": module.get("orgData"),
                }
            )
        if skill_id == "TTFUND_MACRO_DATA" and isinstance(root, dict):
            frequencies = []
            for frequency in root.get("frequencies") or []:
                if not isinstance(frequency, dict):
                    continue
                records = frequency.get("记录") if isinstance(frequency.get("记录"), list) else []
                frequencies.append({**frequency, "记录": records[-8:]})
            return cls._compact({**root, "frequencies": frequencies})
        return cls._compact(root if isinstance(root, dict) else business)

    @classmethod
    def _compact(cls, value: Any, depth: int = 0) -> Any:
        if depth >= 6:
            return "[内容已折叠]"
        if isinstance(value, dict):
            return {str(key): cls._compact(item, depth + 1) for key, item in list(value.items())[:80]}
        if isinstance(value, list):
            return [cls._compact(item, depth + 1) for item in value[:30]]
        if isinstance(value, str) and len(value) > 5000:
            return value[:5000] + "…"
        return value

    @staticmethod
    def _find_as_of(payload: dict[str, Any]) -> str | None:
        for key in ("as_of", "update_time", "updateTime", "snapshot_time", "date", "quote_timestamp"):
            value = payload.get(key)
            if value is not None and not isinstance(value, (dict, list)):
                return str(value)
        return None

    @staticmethod
    def _summary(skill_id: str, payload: dict[str, Any]) -> str:
        if skill_id == "TTFUND_BOND_MARKET":
            return str(payload.get("text_summary") or "已获取最新债市晴雨表")[:1000]
        labels = {
            "TTFUND_GOLD_INFO": "已获取黄金行情、宏观风险指标和黄金资讯",
            "TTFUND_MACRO_DATA": "已获取最近六个月中国宏观数据",
            "TTFUND_STOCK_PRICE_QUERY": "已获取最新股票行情",
        }
        return labels.get(skill_id, "已获取最新资料")

    @staticmethod
    def _now() -> str:
        return datetime.now(UTC).isoformat()
