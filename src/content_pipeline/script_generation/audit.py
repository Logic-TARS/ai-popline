from __future__ import annotations

import json
from pathlib import Path

from content_pipeline.script_generation.models import GenerationAttempt, ScriptGenerationAudit

__all__ = ["persist_script_audit", "record_script_attempt_event", "record_script_generation_events"]


def persist_script_audit(job_dir: Path, audit: ScriptGenerationAudit) -> dict[str, Path]:
    script_dir = job_dir / "script"
    script_dir.mkdir(parents=True, exist_ok=True)
    paths = {
        "plan": script_dir / "plan.json",
        "attempts": script_dir / "attempts.json",
        "quality": script_dir / "quality-report.json",
        "audit": script_dir / "audit.json",
    }
    _write_json(paths["plan"], audit.plan.model_dump(mode="json") if audit.plan else {})
    _write_json(paths["attempts"], {"attempts": [item.model_dump(mode="json") for item in audit.attempts]})
    _write_json(paths["quality"], audit.quality_report.model_dump(mode="json"))
    _write_json(paths["audit"], audit.model_dump(mode="json"))
    return paths


def record_script_attempt_event(store, task_id: str, attempt: GenerationAttempt) -> None:
    payload = {
        "stage": attempt.stage,
        "index": attempt.index,
        "prompt_version": attempt.prompt_version,
        "success": attempt.error is None,
    }
    if attempt.error:
        payload["error"] = attempt.error[:500]
    store.event(task_id, f"script_{attempt.stage}", payload)
    if attempt.quality_report:
        store.event(
            task_id,
            "script_quality_checked",
            {
                "stage": attempt.stage,
                "index": attempt.index,
                "passed": attempt.quality_report.passed,
                "gate": attempt.quality_report.gate,
                "character_count": attempt.quality_report.character_count,
                "issue_codes": [item.code for item in attempt.quality_report.issues],
            },
        )


def record_script_generation_events(store, task_id: str, audit: ScriptGenerationAudit) -> None:
    if audit.plan:
        store.event(
            task_id,
            "script_plan_generated",
            {
                "main_fact_id": audit.plan.main_fact_id,
                "selected_fact_ids": audit.plan.required_facts,
                "prompt_version": audit.plan_prompt_version
                or next((item.prompt_version for item in audit.attempts if item.stage == "plan"), audit.prompt_version),
                "quality_policy_version": audit.quality_policy_version,
                "prompt_fingerprints": audit.prompt_fingerprints,
                "within_recommended_length": audit.within_recommended_length,
            },
        )
    for attempt in audit.attempts:
        record_script_attempt_event(store, task_id, attempt)


def _write_json(path: Path, payload: dict) -> None:
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
