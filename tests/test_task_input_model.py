import pytest
from pydantic import ValidationError

from content_pipeline.models import (
    AiArtParams,
    AiBriefingParams,
    CoverParams,
    DeferredPublishInput,
    FinanceParams,
    GroupedAnimeParams,
    JapaneseParams,
    PublishTarget,
    RouteResult,
    ScriptVideoParams,
    TaskInput,
    XhsImageNoteParams,
)


def test_task_input_normalizes_description_topic_and_publish_account() -> None:
    task = TaskInput(
        description="  生成视频  ",
        topic="  今日主题  ",
        publish_targets=[{"platform": "douyin", "account": "  账号  "}],
    )

    assert task.description == "生成视频"
    assert task.topic == "今日主题"
    assert task.publish_targets[0].account == "账号"


def test_content_experiment_params_normalize_duration_variants() -> None:
    finance = FinanceParams.model_validate({"script_mode": "short_30_60", "experiment_id": " exp-1 "})
    long_ai = AiBriefingParams.model_validate({"script_mode": "long_1_3_minutes"})
    legacy = FinanceParams.model_validate({"script_mode": "90_seconds"})

    assert finance.strategy_profile == "finance_conversion"
    assert finance.experiment_id == "exp-1"
    assert finance.duration_variant == "short_30_60"
    assert long_ai.strategy_profile == "ai_reach"
    assert long_ai.duration_variant == "long_1_3_minutes"
    assert legacy.duration_variant == "legacy_90_seconds"


def test_task_input_normalizes_blank_topic_to_none() -> None:
    task = TaskInput(description="生成视频", topic="   ")

    assert task.topic is None


def test_route_result_normalizes_and_rejects_blank_topic() -> None:
    route = RouteResult(content_type="anime", topic="  周五下班  ")

    assert route.topic == "周五下班"
    with pytest.raises(ValidationError):
        RouteResult(content_type="anime", topic="   ")


@pytest.mark.parametrize(
    "payload",
    [
        {"description": ""},
        {"description": "   "},
        {"description": "x" * 501},
        {"description": "ok", "topic": "x" * 201},
        {"description": "ok", "unexpected": True},
        {
            "description": "ok",
            "publish_targets": [{"platform": "douyin", "account": f"account-{index}"} for index in range(5)],
        },
        {"description": "ok", "publish_targets": [{"platform": "douyin", "account": "   "}]},
        {
            "description": "ok",
            "publish_targets": [
                {"platform": "douyin", "account": "a"},
                {"platform": "douyin", "account": "b"},
            ],
        },
        {
            "description": "ok",
            "publish_targets": [{"platform": "douyin", "account": "a", "unexpected": True}],
        },
        {
            "description": "ok",
            "publish_targets": [{"platform": "bilibili", "account": "a", "tid": 0}],
        },
        {"description": "ok", "source_draft_id": "a" * 32, "source_draft_revision": 1},
        {"description": "ok", "origin": "content_studio", "source_draft_id": "a" * 32},
        {"description": "ok", "origin": "content_studio", "source_draft_revision": 1},
    ],
)
def test_task_input_rejects_unsafe_boundaries(payload: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        TaskInput(**payload)


@pytest.mark.parametrize(
    ("params_model", "payload"),
    [
        (
            AiArtParams,
            {"source_dir": ".", "process_name": "动漫图文", "title": "标题", "unexpected": True},
        ),
        (XhsImageNoteParams, {"source_dir": ".", "process_name": "小红书图文", "unexpected": True}),
        (FinanceParams, {"unexpected": True}),
        (ScriptVideoParams, {"title": "标题", "script": "x" * 300, "unexpected": True}),
        (CoverParams, {"title": "标题", "unexpected": True}),
    ],
)
def test_pipeline_params_reject_unexpected_fields(params_model: type, payload: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        params_model.model_validate(payload)


@pytest.mark.parametrize(
    ("params_model", "payload"),
    [
        (AiArtParams, {"source_dir": ".", "process_name": "   ", "title": "标题"}),
        (AiArtParams, {"source_dir": ".", "process_name": "动漫图文", "title": "   "}),
        (XhsImageNoteParams, {"source_dir": ".", "process_name": "   "}),
        (JapaneseParams, {"source_dir": ".", "process_name": "   "}),
    ],
)
def test_image_params_reject_blank_required_text(params_model: type, payload: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        params_model.model_validate(payload)


def test_image_params_normalize_required_text() -> None:
    ai_art = AiArtParams(source_dir=".", process_name="  动漫图文  ", title="  标题  ")
    xhs_note = XhsImageNoteParams(source_dir=".", process_name="  小红书图文  ")
    japanese = JapaneseParams(source_dir=".", process_name="  日语视觉化  ")

    assert ai_art.process_name == "动漫图文"
    assert ai_art.title == "标题"
    assert xhs_note.process_name == "小红书图文"
    assert japanese.process_name == "日语视觉化"


def test_ai_art_params_normalize_publish_fields() -> None:
    request = AiArtParams(
        source_dir=".",
        process_name="动漫图文",
        title="标题",
        description="  AI 绘画作品  ",
        tags=[" #AI绘画 ", "AI绘画", "", "#作品集"],
    )

    assert request.description == "AI 绘画作品"
    assert request.tags == ["AI绘画", "作品集"]


@pytest.mark.parametrize(
    ("params_model", "payload", "expected"),
    [
        (
            AiArtParams,
            {
                "source_dir": ".",
                "process_name": "动漫图文",
                "title": "标题",
                "source_files": [" a.png ", "", "a.png", "b.webp"],
            },
            ["a.png", "b.webp"],
        ),
        (
            XhsImageNoteParams,
            {"source_dir": ".", "process_name": "小红书图文", "source_files": [" one.jpg ", "one.jpg"]},
            ["one.jpg"],
        ),
        (
            JapaneseParams,
            {"source_dir": ".", "source_files": [" one.png ", "two.jpeg"]},
            ["one.png", "two.jpeg"],
        ),
        (
            GroupedAnimeParams,
            {"source_dir": ".", "source_files": [" alpha.png ", "alpha.png", "beta.png"]},
            ["alpha.png", "beta.png"],
        ),
    ],
)
def test_image_params_normalize_source_files(
    params_model: type, payload: dict[str, object], expected: list[str]
) -> None:
    request = params_model.model_validate(payload)

    assert request.source_files == expected


@pytest.mark.parametrize(
    ("params_model", "payload"),
    [
        (
            AiArtParams,
            {"source_dir": ".", "process_name": "动漫图文", "title": "标题", "source_files": ["nested/a.png"]},
        ),
        (XhsImageNoteParams, {"source_dir": ".", "process_name": "小红书图文", "source_files": ["../a.png"]}),
        (JapaneseParams, {"source_dir": ".", "source_files": ["nested/a.png"]}),
        (GroupedAnimeParams, {"source_dir": ".", "source_files": ["../a.png"]}),
    ],
)
def test_image_params_reject_non_plain_source_files(params_model: type, payload: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        params_model.model_validate(payload)


@pytest.mark.parametrize("script", ["短稿", "长" * 1200])
def test_script_video_params_allows_nonblank_scripts_up_to_1200_chars(script: str) -> None:
    request = ScriptVideoParams(title="  今日资讯  ", script=script)

    assert request.title == "今日资讯"
    assert request.script == script


@pytest.mark.parametrize("script", ["", " " * 300, "长" * 1201])
def test_script_video_params_rejects_blank_or_overlong_script(script: str) -> None:
    with pytest.raises(ValidationError):
        ScriptVideoParams(title="今日资讯", script=script)


def test_script_video_params_rejects_blank_title() -> None:
    with pytest.raises(ValidationError):
        ScriptVideoParams(title="   ", script="市场信息。")


def test_cover_params_normalizes_title_and_applies_defaults() -> None:
    params = CoverParams(title="  今日封面  ")

    assert params.title == "今日封面"
    assert params.size == "landscape"
    assert params.template == "default"
    assert params.title_position == "left"
    assert params.background_scale == 1.0
    assert params.background_position_x == 50.0
    assert params.background_position_y == 50.0


@pytest.mark.parametrize(
    "payload",
    [
        {"title": "   "},
        {"title": "长" * 81},
        {"title": "标题", "size": "square"},
        {"title": "标题", "template": "missing"},
        {"title": "标题", "title_position": "right"},
        {"title": "标题", "background_scale": 0.24},
        {"title": "标题", "background_scale": 3.01},
        {"title": "标题", "background_position_x": -1},
        {"title": "标题", "background_position_y": 101},
    ],
)
def test_cover_params_rejects_invalid_boundaries(payload: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        CoverParams.model_validate(payload)


def test_script_video_params_normalize_publish_fields() -> None:
    request = ScriptVideoParams(
        title="今日资讯",
        script="市场信息。" * 80,
        description="  每日市场观察  ",
        tags=[" #市场 ", "市场", "", "#财经"],
        douyin_account="  抖音账号  ",
        kuaishou_account="  快手账号  ",
    )

    assert request.description == "每日市场观察"
    assert request.tags == ["市场", "财经"]
    assert request.douyin_account == "抖音账号"
    assert request.kuaishou_account == "快手账号"


@pytest.mark.parametrize("field", ["douyin_account", "kuaishou_account"])
def test_script_video_params_reject_blank_publish_accounts(field: str) -> None:
    payload = {"title": "今日资讯", "script": "市场信息。" * 80, field: "   "}

    with pytest.raises(ValidationError):
        ScriptVideoParams.model_validate(payload)


def test_finance_params_normalize_text_fields() -> None:
    request = FinanceParams(
        douyin_account="  抖音账号  ",
        kuaishou_account="  快手账号  ",
        title="   ",
        description="  每日基金日报  ",
        tags=[" #基金 ", "基金", "", "#A股"],
    )

    assert request.douyin_account == "抖音账号"
    assert request.kuaishou_account == "快手账号"
    assert request.title is None
    assert request.description == "每日基金日报"
    assert request.tags == ["基金", "A股"]


@pytest.mark.parametrize("field", ["douyin_account", "kuaishou_account"])
def test_finance_params_reject_blank_accounts(field: str) -> None:
    payload = {field: "   "}

    with pytest.raises(ValidationError):
        FinanceParams.model_validate(payload)


@pytest.mark.parametrize(
    ("params_model", "payload"),
    [
        (AiArtParams, {"source_dir": ".", "process_name": "动漫图文", "title": "标题", "tags": ["x" * 31]}),
        (GroupedAnimeParams, {"source_dir": ".", "tags": ["x" * 31]}),
        (FinanceParams, {"tags": ["x" * 31]}),
        (AiBriefingParams, {"tags": ["x" * 31]}),
        (ScriptVideoParams, {"title": "今日资讯", "script": "市场信息。" * 80, "tags": ["x" * 31]}),
    ],
)
def test_feed_params_reject_overlong_tags(params_model: type, payload: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        params_model.model_validate(payload)


def test_grouped_anime_params_normalize_text_fields() -> None:
    request = GroupedAnimeParams(
        source_dir=".",
        title="   ",
        description="  动漫合集  ",
        tags=[" #动漫 ", "动漫", "", "#合集"],
    )

    assert request.title is None
    assert request.description == "动漫合集"
    assert request.tags == ["动漫", "合集"]


def test_ai_briefing_params_normalize_text_fields() -> None:
    request = AiBriefingParams(
        douyin_account="  抖音账号  ",
        kuaishou_account="  快手账号  ",
        title="  AI早报  ",
        description="  今日AI简报  ",
        tags=[" #AI ", "AI", "", "#科技"],
    )

    assert request.douyin_account == "抖音账号"
    assert request.kuaishou_account == "快手账号"
    assert request.title == "AI早报"
    assert request.description == "今日AI简报"
    assert request.tags == ["AI", "科技"]
    assert request.cover_size == "story"
    assert request.cover_template == "ai-poster"
    assert request.cover_title_position == "center"
    assert request.cover_background_scale == 1.0
    assert request.cover_background_position_x == 50.0
    assert request.cover_background_position_y == 50.0


@pytest.mark.parametrize(
    "payload",
    [
        {"cover_size": "square"},
        {"cover_template": "missing"},
        {"cover_title_position": "right"},
        {"cover_background_scale": 0.24},
        {"cover_background_scale": 3.01},
        {"cover_background_position_x": -1},
        {"cover_background_position_y": 101},
    ],
)
def test_ai_briefing_params_reject_invalid_cover_boundaries(payload: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        AiBriefingParams.model_validate(payload)


@pytest.mark.parametrize("field", ["douyin_account", "kuaishou_account"])
def test_ai_briefing_params_reject_blank_required_accounts(field: str) -> None:
    payload = {field: "   "}

    with pytest.raises(ValidationError):
        AiBriefingParams.model_validate(payload)


def test_deferred_publish_input_normalizes_and_rejects_blank_title() -> None:
    request = DeferredPublishInput(
        publish_targets=[{"platform": "douyin", "account": "账号"}],
        title="  今日金融资讯  ",
        description="  每日市场观察  ",
        tags=[" #金融 ", "金融", "", "#市场"],
    )

    assert request.title == "今日金融资讯"
    assert request.description == "每日市场观察"
    assert request.tags == ["金融", "市场"]
    with pytest.raises(ValidationError):
        DeferredPublishInput(publish_targets=[{"platform": "douyin", "account": "账号"}], title="   ")


def test_publish_target_visibility_defaults_to_private() -> None:
    target = PublishTarget(platform="douyin", account="账号")

    assert target.visibility == "private"


def test_publish_target_accepts_explicit_public_visibility() -> None:
    target = PublishTarget(platform="douyin", account="账号", visibility="public")

    assert target.visibility == "public"


def test_publish_target_rejects_invalid_visibility_and_unknown_fields() -> None:
    with pytest.raises(ValidationError):
        PublishTarget(platform="douyin", account="账号", visibility="friends")
    with pytest.raises(ValidationError):
        PublishTarget(platform="douyin", account="账号", visibility="private", unexpected=True)
