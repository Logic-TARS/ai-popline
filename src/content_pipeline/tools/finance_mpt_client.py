from __future__ import annotations

from pathlib import Path

from content_pipeline.settings import Settings
from content_pipeline.tools.narrated_mpt_client import (
    NarratedMptResult,
    call_narrated_mpt,
)

__all__ = ["FinanceMptResult", "call_finance_mpt"]

FinanceMptResult = NarratedMptResult


def call_finance_mpt(
    *,
    publication_date: str,
    title: str,
    script: str,
    output_dir: Path,
    settings: Settings,
    dry_run: bool = False,
    target_duration_seconds: float = 90.0,
    allowed_duration: tuple[float, float] | None = None,
    generation_metadata: dict[str, object] | None = None,
) -> FinanceMptResult:
    return call_narrated_mpt(
        task_name=f"finance-{publication_date.replace('-', '')}",
        title=title,
        script=script,
        output_dir=output_dir,
        settings=settings,
        dry_run=dry_run,
        voice_rate=1.0 if target_duration_seconds == 90.0 else None,
        target_duration_seconds=target_duration_seconds,
        allowed_duration=allowed_duration,
        generation_metadata=generation_metadata,
    )
