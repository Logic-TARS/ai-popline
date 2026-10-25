from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

from content_pipeline.metadata_generation import (
    RULE_DESCRIPTIONS,
    MetadataGenerationService,
    metadata_audit_paths,
    metadata_audit_summary,
)
from content_pipeline.models import AiBriefingParams, FinanceParams

if TYPE_CHECKING:
    from content_pipeline.pipelines.registry import PipelineContext

__all__ = ["generate_pipeline_metadata"]


def generate_pipeline_metadata(
    ctx: PipelineContext,
    *,
    params: AiBriefingParams | FinanceParams,
    material: dict[str, Any],
    script: str,
    rule_title: str,
) -> tuple[str, str]:
    content_type = "ai_briefing" if isinstance(params, AiBriefingParams) else "finance"
    output_dir = ctx.store.job_dir(ctx.task_id)
    paths = metadata_audit_paths(output_dir)
    try:
        audit = MetadataGenerationService(ctx.settings).generate(
            content_type=content_type,
            material=material,
            script=script,
            rule_title=rule_title,
            rule_description=RULE_DESCRIPTIONS[content_type],
            output_dir=output_dir,
            title=params.title,
            description=params.description,
            title_writer=params.title_writer,
            description_writer=params.description_writer,
            script_writer=params.script_writer,
            failure_policy=params.metadata_failure_policy,
            repair_attempts=params.metadata_repair_attempts,
            gate=params.quality_gate,
            strategy_profile=params.strategy_profile,
            duration_variant=params.duration_variant or "long_1_3_minutes",
            on_event=lambda event, payload: ctx.store.event(ctx.task_id, event, payload),
        )
        return audit["title"]["final_text"], audit["description"]["final_text"]
    finally:
        if paths["audit"].is_file():
            audit = json.loads(paths["audit"].read_text(encoding="utf-8"))
            ctx.artifacts.metadata_audit = metadata_audit_summary(audit)
            ctx.artifacts.metadata_audit_path = paths["audit"]
            for stage in ("title", "description"):
                result = audit.get(stage)
                final_text = result["final_text"] if result and result.get("status") == "succeeded" else None
                setattr(ctx.artifacts, f"final_{stage}", final_text)
            ctx.store.set_artifacts(ctx.task_id, ctx.artifacts)
