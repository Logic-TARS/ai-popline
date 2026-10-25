import json
from pathlib import Path

from content_pipeline.analytics import import_metrics, summarize_metrics


def test_import_metrics_normalizes_aliases_and_calculates_summary(tmp_path: Path) -> None:
    source = tmp_path / "metrics.json"
    source.write_text(
        json.dumps(
            [
                {
                    "作品名称": "科创50反弹",
                    "平台": "抖音",
                    "内容线": "finance",
                    "时长版本": "short_30_60",
                    "日期": "2026-11-01",
                    "播放": "1,000",
                    "点赞": 10,
                    "分享": 5,
                    "评论": 5,
                    "收藏": 10,
                    "涨粉": 4,
                }
            ],
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    database = tmp_path / "metrics.sqlite3"

    assert import_metrics(source, database)["rows"] == 1
    summary = summarize_metrics(database)
    group = summary["groups"][0]
    assert group["views"] == 1000
    assert group["engagement_rate"] == 0.03
    assert group["follows_per_1000"] == 4.0


def test_import_metrics_rejects_unknown_platform(tmp_path: Path) -> None:
    source = tmp_path / "metrics.json"
    source.write_text(json.dumps([{"作品名称": "x", "平台": "微博", "内容线": "finance"}], ensure_ascii=False), encoding="utf-8")

    try:
        import_metrics(source, tmp_path / "metrics.sqlite3")
    except ValueError as exc:
        assert "unsupported platform" in str(exc)
    else:
        raise AssertionError("expected unsupported platform error")
