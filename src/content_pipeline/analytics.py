"""Local performance import and experiment summaries for generated videos.

The pipeline deliberately keeps platform metrics as an operator-provided import:
platform APIs are not required to generate or publish a video. CSV and JSON are
supported so an exported spreadsheet can be normalized once before import.
"""

from __future__ import annotations

import csv
import json
import sqlite3
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

__all__ = ["PerformanceRecord", "import_metrics", "summarize_metrics"]


@dataclass(frozen=True)
class PerformanceRecord:
    work_key: str
    platform: str
    published_at: str
    content_line: str
    experiment_id: str | None = None
    strategy_profile: str | None = None
    duration_variant: str | None = None
    title: str = ""
    topic: str = ""
    views: int = 0
    likes: int = 0
    shares: int = 0
    comments: int = 0
    favorites: int = 0
    profile_views: int = 0
    follows: int = 0
    five_second_completion: float | None = None
    two_second_bounce: float | None = None
    average_watch_seconds: float | None = None
    visibility_status: str = "public"
    restricted: bool = False
    import_batch: str | None = None
    source_file: str | None = None
    source_row: int | None = None

    @property
    def engagement_rate(self) -> float | None:
        return None if self.views <= 0 else (self.likes + self.shares + self.comments + self.favorites) / self.views

    @property
    def follows_per_1000(self) -> float | None:
        return None if self.views <= 0 else self.follows * 1000 / self.views


ALIASES = {
    "work_key": ("work_key", "task_id", "作品ID", "作品名称", "标题"),
    "platform": ("platform", "平台"),
    "published_at": ("published_at", "发布时间", "日期"),
    "content_line": ("content_line", "内容线", "题材", "分类"),
    "experiment_id": ("experiment_id", "实验编号"),
    "strategy_profile": ("strategy_profile", "策略"),
    "duration_variant": ("duration_variant", "时长版本", "时长"),
    "title": ("title", "作品名称", "标题"),
    "topic": ("topic", "选题"),
    "views": ("views", "播放", "播放量"),
    "likes": ("likes", "点赞"),
    "shares": ("shares", "分享"),
    "comments": ("comments", "评论"),
    "favorites": ("favorites", "收藏"),
    "profile_views": ("profile_views", "主页访问"),
    "follows": ("follows", "涨粉", "新增粉丝"),
    "five_second_completion": ("five_second_completion", "5秒完播", "5秒完播率"),
    "two_second_bounce": ("two_second_bounce", "2秒跳出", "2秒跳出率"),
    "average_watch_seconds": ("average_watch_seconds", "均播时长", "平均播放时长"),
    "visibility_status": ("visibility_status", "状态", "可见性"),
    "restricted": ("restricted", "受限", "restricted"),
}


def _value(row: dict[str, Any], field: str) -> Any:
    for alias in ALIASES[field]:
        if alias in row and str(row[alias]).strip() != "":
            return row[alias]
    return None


def _number(value: Any, *, integer: bool = True) -> int | float:
    if value is None or str(value).strip() in {"", "-", "—"}:
        return 0 if integer else None
    text = str(value).replace(",", "").replace("%", "").strip()
    try:
        return int(float(text)) if integer else float(text)
    except ValueError as exc:
        raise ValueError(f"invalid metric value: {value!r}") from exc


def normalize_row(row: dict[str, Any]) -> PerformanceRecord:
    work_key = str(_value(row, "work_key") or "").strip()
    if not work_key:
        raise ValueError("each performance row requires work_key, task_id, title, or 作品名称")
    platform_raw = str(_value(row, "platform") or "").strip().lower()
    platform = {"抖音": "douyin", "快手": "kuaishou"}.get(platform_raw, platform_raw)
    if platform not in {"douyin", "kuaishou"}:
        raise ValueError(f"unsupported platform: {platform or '(blank)'}")
    content_line = str(_value(row, "content_line") or "").strip()
    content_line = {"基金": "finance", "A股": "finance", "AI": "ai_briefing", "科技": "ai_briefing"}.get(
        content_line, content_line
    )
    if content_line not in {"finance", "ai_briefing", "基金", "A股", "AI", "科技"}:
        raise ValueError(f"content_line must identify finance or ai_briefing: {content_line!r}")
    return PerformanceRecord(
        work_key=work_key,
        platform=platform,
        published_at=str(_value(row, "published_at") or "").strip(),
        content_line=content_line,
        experiment_id=str(_value(row, "experiment_id") or "").strip() or None,
        strategy_profile=str(_value(row, "strategy_profile") or "").strip() or None,
        duration_variant=str(_value(row, "duration_variant") or "").strip() or None,
        title=str(_value(row, "title") or work_key).strip(),
        topic=str(_value(row, "topic") or "").strip(),
        views=int(_number(_value(row, "views"))),
        likes=int(_number(_value(row, "likes"))),
        shares=int(_number(_value(row, "shares"))),
        comments=int(_number(_value(row, "comments"))),
        favorites=int(_number(_value(row, "favorites"))),
        profile_views=int(_number(_value(row, "profile_views"))),
        follows=int(_number(_value(row, "follows"))),
        five_second_completion=_number(_value(row, "five_second_completion"), integer=False),
        two_second_bounce=_number(_value(row, "two_second_bounce"), integer=False),
        average_watch_seconds=_number(_value(row, "average_watch_seconds"), integer=False),
        visibility_status=str(_value(row, "visibility_status") or "public").strip(),
        restricted=str(_value(row, "restricted") or "").strip().lower() in {"1", "true", "yes", "是", "受限"},
    )


def _rows(path: Path) -> Iterable[dict[str, Any]]:
    if path.suffix.lower() == ".json":
        payload = json.loads(path.read_text(encoding="utf-8-sig"))
        if not isinstance(payload, list):
            raise ValueError("JSON metrics input must be an array of objects")
        return payload
    if path.suffix.lower() == ".csv":
        with path.open("r", encoding="utf-8-sig", newline="") as handle:
            return list(csv.DictReader(handle))
    if path.suffix.lower() == ".xlsx":
        try:
            from openpyxl import load_workbook
        except ImportError as exc:
            raise ValueError(".xlsx import requires the openpyxl dependency") from exc
        workbook = load_workbook(path, read_only=True, data_only=True)
        sheet = workbook.active
        rows = list(sheet.iter_rows(values_only=True))
        if not rows:
            return []
        headers = [str(value or "").strip() for value in rows[0]]
        return [dict(zip(headers, row, strict=False)) for row in rows[1:] if any(value is not None for value in row)]
    raise ValueError("metrics input must be .xlsx, .csv, or .json")


def _connect(db_path: Path) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(db_path)
    connection.execute(
        """CREATE TABLE IF NOT EXISTS performance_metrics (
            work_key TEXT NOT NULL, platform TEXT NOT NULL, published_at TEXT NOT NULL,
            content_line TEXT NOT NULL, experiment_id TEXT, strategy_profile TEXT,
            duration_variant TEXT, title TEXT NOT NULL, topic TEXT NOT NULL,
            views INTEGER NOT NULL, likes INTEGER NOT NULL, shares INTEGER NOT NULL,
            comments INTEGER NOT NULL, favorites INTEGER NOT NULL, profile_views INTEGER NOT NULL,
            follows INTEGER NOT NULL, five_second_completion REAL, two_second_bounce REAL,
            average_watch_seconds REAL, visibility_status TEXT NOT NULL, restricted INTEGER NOT NULL DEFAULT 0, import_batch TEXT, source_file TEXT, source_row INTEGER, imported_at TEXT NOT NULL,
            PRIMARY KEY (work_key, platform, published_at)
        )"""
    )
    return connection


def import_metrics(input_path: Path, db_path: Path) -> dict[str, int]:
    records = []
    for row in _rows(input_path):
        record = normalize_row(row)
        if not record.published_at:
            raise ValueError("published_at is required for every performance row")
        try:
            datetime.fromisoformat(record.published_at.replace("Z", "+00:00"))
        except ValueError as exc:
            raise ValueError(f"invalid published_at: {record.published_at!r}") from exc
        records.append(record)
    now = datetime.now(timezone.utc).isoformat()
    connection = _connect(db_path)
    try:
        for record in records:
            values = asdict(record)
            values["imported_at"] = now
            values["import_batch"] = now
            values["source_file"] = str(input_path)
            values["source_row"] = len(records)
            connection.execute(
                "INSERT OR REPLACE INTO performance_metrics VALUES (:work_key,:platform,:published_at,:content_line,:experiment_id,:strategy_profile,:duration_variant,:title,:topic,:views,:likes,:shares,:comments,:favorites,:profile_views,:follows,:five_second_completion,:two_second_bounce,:average_watch_seconds,:visibility_status,:restricted,:import_batch,:source_file,:source_row,:imported_at)",
                values,
            )
        connection.commit()
    finally:
        connection.close()
    return {"rows": len(records), "database": str(db_path)}


def summarize_metrics(db_path: Path, *, include_restricted: bool = False, start_date: str | None = None, end_date: str | None = None, experiment_id: str | None = None, platform: str | None = None, content_line: str | None = None) -> dict[str, Any]:
    connection = _connect(db_path)
    try:
        query = "SELECT content_line, duration_variant, platform, views, likes, shares, comments, favorites, follows FROM performance_metrics WHERE 1=1"
        rows = connection.execute(query).fetchall()
    finally:
        connection.close()
    grouped: dict[str, dict[str, Any]] = {}
    for content_line, duration_variant, platform, views, likes, shares, comments, favorites, follows in rows:
        key = "|".join((content_line, duration_variant or "unknown", platform))
        item = grouped.setdefault(key, {"content_line": content_line, "duration_variant": duration_variant, "platform": platform, "works": 0, "views": 0, "engagements": 0, "follows": 0})
        item["works"] += 1
        item["views"] += views
        item["engagements"] += likes + shares + comments + favorites
        item["follows"] += follows
    for item in grouped.values():
        item["engagement_rate"] = item["engagements"] / item["views"] if item["views"] else None
        item["follows_per_1000"] = item["follows"] * 1000 / item["views"] if item["views"] else None
    return {"groups": sorted(grouped.values(), key=lambda item: (item["content_line"], item["duration_variant"] or "", item["platform"]))}
