from __future__ import annotations

import ast
import tomllib
from pathlib import Path


def test_all_source_modules_declare_explicit_exports() -> None:
    missing_exports = []
    for path in sorted(Path("src/content_pipeline").rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        if _all_assignment(tree) is None:
            missing_exports.append(path.as_posix())

    assert missing_exports == []


def test_all_source_module_exports_are_plain_unique_string_lists() -> None:
    invalid_exports = []
    for path in sorted(Path("src/content_pipeline").rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        all_assignments = _all_assignments(tree)
        if len(all_assignments) != 1:
            invalid_exports.append(f"{path.as_posix()}:assignments={len(all_assignments)}")
            continue

        value = ast.literal_eval(all_assignments[0].value)
        if not isinstance(value, list):
            invalid_exports.append(f"{path.as_posix()}:not-list")
            continue
        if any(not isinstance(name, str) for name in value):
            invalid_exports.append(f"{path.as_posix()}:non-string-name")
        duplicate_names = sorted({name for name in value if value.count(name) > 1})
        for name in duplicate_names:
            invalid_exports.append(f"{path.as_posix()}:duplicate:{name}")

    assert invalid_exports == []


def test_all_source_module_exports_reference_defined_names() -> None:
    invalid_exports = []
    for path in sorted(Path("src/content_pipeline").rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        all_assignment = _all_assignment(tree)
        assert all_assignment is not None
        exported_names = ast.literal_eval(all_assignment.value)
        defined_names = _top_level_defined_names(tree)
        for name in exported_names:
            if name not in defined_names:
                invalid_exports.append(f"{path.as_posix()}:{name}")

    assert invalid_exports == []


def _all_assignments(tree: ast.Module) -> list[ast.Assign]:
    return [
        node
        for node in tree.body
        if isinstance(node, ast.Assign) and any(_is_all_target(target) for target in node.targets)
    ]


def _all_assignment(tree: ast.Module) -> ast.Assign | None:
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(_is_all_target(target) for target in node.targets):
            return node
    return None


def _is_all_target(node: ast.expr) -> bool:
    return isinstance(node, ast.Name) and node.id == "__all__"


def _top_level_defined_names(tree: ast.Module) -> set[str]:
    names: set[str] = set()
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            names.add(node.name)
        elif isinstance(node, (ast.Import, ast.ImportFrom)):
            for alias in node.names:
                names.add(alias.asname or alias.name.split(".", maxsplit=1)[0])
        elif isinstance(node, ast.Assign):
            names.update(_assigned_names(node.targets))
        elif isinstance(node, ast.AnnAssign):
            names.update(_assigned_names([node.target]))
    return names


def _assigned_names(targets: list[ast.expr]) -> set[str]:
    names: set[str] = set()
    for target in targets:
        if isinstance(target, ast.Name):
            names.add(target.id)
        elif isinstance(target, (ast.Tuple, ast.List)):
            names.update(_assigned_names(list(target.elts)))
    return names


def test_project_metadata_is_ready_for_distribution() -> None:
    config = tomllib.loads(Path("pyproject.toml").read_text(encoding="utf-8"))
    project = config["project"]

    assert project["name"] == "ai-pipeline"
    assert project["description"]
    assert project["readme"] == "README.md"
    assert Path(project["readme"]).is_file()
    assert project["requires-python"] == ">=3.11"
    assert project["keywords"] == ["automation", "content-pipeline", "social-media", "video"]
    assert "Framework :: FastAPI" in project["classifiers"]
    assert "Programming Language :: Python :: 3.11" in project["classifiers"]
    assert "Topic :: Multimedia :: Video" in project["classifiers"]


def test_manifest_excludes_python_bytecode_from_sdist_sources() -> None:
    manifest = Path("MANIFEST.in").read_text(encoding="utf-8").splitlines()

    assert "global-exclude *.py[cod]" in manifest


def test_python_runtime_contract_is_consistent_across_delivery_surfaces() -> None:
    config = tomllib.loads(Path("pyproject.toml").read_text(encoding="utf-8"))
    readme = Path("README.md").read_text(encoding="utf-8")
    workflow = Path(".github/workflows/quality.yml").read_text(encoding="utf-8")
    dockerfile = Path("Dockerfile").read_text(encoding="utf-8")
    dev_dockerfile = Path("Dockerfile.dev").read_text(encoding="utf-8")

    assert config["project"]["requires-python"] == ">=3.11"
    assert config["tool"]["ruff"]["target-version"] == "py311"
    assert "Python **3.11 or newer**" in readme
    assert 'python-version: "3.11"' in workflow
    assert dockerfile.startswith("FROM python:3.11-slim\n")
    assert dev_dockerfile.startswith("FROM python:3.11-slim\n")


def test_root_package_exports_version_contract() -> None:
    import content_pipeline

    config = tomllib.loads(Path("pyproject.toml").read_text(encoding="utf-8"))

    assert content_pipeline.__all__ == ["__version__"]
    assert content_pipeline.__version__ == config["project"]["version"]


def test_pipeline_common_module_exports_internal_contract() -> None:
    import content_pipeline._pipeline_common as pipeline_common
    from content_pipeline._pipeline_common import _group_signature, _group_title, _partial_reasons

    assert pipeline_common.__all__ == ["_group_signature", "_group_title", "_partial_reasons"]
    assert _group_signature is pipeline_common._group_signature
    assert _group_title is pipeline_common._group_title
    assert _partial_reasons is pipeline_common._partial_reasons


def test_storage_job_store_facade_exports_public_contract() -> None:
    import content_pipeline.job_store as root_job_store
    import content_pipeline.storage as package_storage
    import content_pipeline.storage.job_store as facade_job_store
    from content_pipeline.job_store import STORE_VERSION, JobDeleteConflictError, JobStore, utc_now
    from content_pipeline.storage import STORE_VERSION as PACKAGE_STORE_VERSION
    from content_pipeline.storage import JobDeleteConflictError as PackageJobDeleteConflictError
    from content_pipeline.storage import JobStore as PackageJobStore
    from content_pipeline.storage import utc_now as package_utc_now
    from content_pipeline.storage.job_store import JobDeleteConflictError as FacadeJobDeleteConflictError
    from content_pipeline.storage.job_store import JobStore as FacadeJobStore

    assert root_job_store.__all__ == facade_job_store.__all__ == package_storage.__all__
    assert FacadeJobStore is JobStore
    assert FacadeJobDeleteConflictError is JobDeleteConflictError
    assert PackageJobStore is JobStore
    assert PackageJobDeleteConflictError is JobDeleteConflictError
    assert PACKAGE_STORE_VERSION == STORE_VERSION
    assert package_utc_now is utc_now


def test_adapters_package_exports_facade_contracts() -> None:
    from content_pipeline.adapters import (
        SOURCE_IMAGE_SUFFIXES,
        GeminiMcpClient,
        call_gemini_skill,
        call_mpt,
        call_sau_target,
        call_sau_upload,
        generate_many,
        photo_process_contract,
        scan_source_images,
    )
    from content_pipeline.adapters.gemini import GeminiMcpClient as FacadeGeminiMcpClient
    from content_pipeline.adapters.gemini import call_gemini_skill as facade_call_gemini_skill
    from content_pipeline.adapters.gemini import generate_many as facade_generate_many
    from content_pipeline.adapters.mpt import call_mpt as facade_call_mpt
    from content_pipeline.adapters.photo_process import SOURCE_IMAGE_SUFFIXES as FACADE_SOURCE_IMAGE_SUFFIXES
    from content_pipeline.adapters.photo_process import photo_process_contract as facade_photo_process_contract
    from content_pipeline.adapters.photo_process import scan_source_images as facade_scan_source_images
    from content_pipeline.adapters.sau import call_sau_target as facade_call_sau_target
    from content_pipeline.adapters.sau import call_sau_upload as facade_call_sau_upload

    assert GeminiMcpClient is FacadeGeminiMcpClient
    assert call_gemini_skill is facade_call_gemini_skill
    assert generate_many is facade_generate_many
    assert call_mpt is facade_call_mpt
    assert SOURCE_IMAGE_SUFFIXES is FACADE_SOURCE_IMAGE_SUFFIXES
    assert photo_process_contract is facade_photo_process_contract
    assert scan_source_images is facade_scan_source_images
    assert call_sau_target is facade_call_sau_target
    assert call_sau_upload is facade_call_sau_upload


def test_pipeline_anime_module_exports_registered_pipeline_contract() -> None:
    import content_pipeline.pipelines.anime as anime
    from content_pipeline.pipelines import PIPELINE_REGISTRY
    from content_pipeline.pipelines.anime import run_anime_pipeline

    assert anime.__all__ == ["run_anime_pipeline"]
    assert run_anime_pipeline is anime.run_anime_pipeline
    assert PIPELINE_REGISTRY["anime"] is run_anime_pipeline


def test_tools_gemini_modules_export_public_contracts() -> None:
    import content_pipeline.adapters.gemini as facade_gemini
    import content_pipeline.tools.gemini_client as gemini_client
    import content_pipeline.tools.gemini_mcp_client as gemini_mcp_client
    from content_pipeline.tools.gemini_client import call_gemini_skill
    from content_pipeline.tools.gemini_mcp_client import GeminiMcpClient, generate_many

    assert facade_gemini.__all__ == ["GeminiMcpClient", "call_gemini_skill", "generate_many"]
    assert gemini_client.__all__ == ["call_gemini_skill"]
    assert gemini_mcp_client.__all__ == ["GeminiMcpClient", "generate_many"]
    assert call_gemini_skill is gemini_client.call_gemini_skill
    assert GeminiMcpClient is gemini_mcp_client.GeminiMcpClient
    assert generate_many is gemini_mcp_client.generate_many


def test_tools_photo_process_client_module_exports_public_contract() -> None:
    import content_pipeline.adapters.photo_process as facade_photo_process
    import content_pipeline.tools.photo_process_client as photo_process_client
    from content_pipeline.tools.photo_process_client import (
        SOURCE_IMAGE_SUFFIXES,
        archive_source,
        photo_process_contract,
        run_photo_process_adapter,
        scan_source_images,
    )

    assert (
        photo_process_client.__all__
        == facade_photo_process.__all__
        == [
            "SOURCE_IMAGE_SUFFIXES",
            "archive_source",
            "photo_process_contract",
            "run_photo_process_adapter",
            "scan_source_images",
        ]
    )
    assert SOURCE_IMAGE_SUFFIXES is photo_process_client.SOURCE_IMAGE_SUFFIXES
    assert archive_source is photo_process_client.archive_source
    assert photo_process_contract is photo_process_client.photo_process_contract
    assert run_photo_process_adapter is photo_process_client.run_photo_process_adapter
    assert scan_source_images is photo_process_client.scan_source_images


def test_tools_mpt_modules_export_public_contracts() -> None:
    import content_pipeline.adapters.mpt as facade_mpt
    import content_pipeline.tools.finance_mpt_client as finance_mpt_client
    import content_pipeline.tools.mpt_client as mpt_client
    import content_pipeline.tools.narrated_mpt_client as narrated_mpt_client
    from content_pipeline.tools.finance_mpt_client import FinanceMptResult, call_finance_mpt
    from content_pipeline.tools.mpt_client import call_mpt
    from content_pipeline.tools.narrated_mpt_client import (
        NarratedMptResult,
        call_narrated_mpt,
        looks_like_file_reference,
        validate_spoken_subtitle,
    )

    assert facade_mpt.__all__ == [
        "FinanceMptResult",
        "NarratedMptResult",
        "call_finance_mpt",
        "call_mpt",
        "call_narrated_mpt",
    ]
    assert mpt_client.__all__ == ["call_mpt"]
    assert finance_mpt_client.__all__ == ["FinanceMptResult", "call_finance_mpt"]
    assert narrated_mpt_client.__all__ == [
        "NarratedMptResult",
        "call_narrated_mpt",
        "looks_like_file_reference",
        "validate_spoken_subtitle",
    ]
    assert call_mpt is mpt_client.call_mpt
    assert FinanceMptResult is finance_mpt_client.FinanceMptResult
    assert FinanceMptResult is NarratedMptResult
    assert call_finance_mpt is finance_mpt_client.call_finance_mpt
    assert NarratedMptResult is narrated_mpt_client.NarratedMptResult
    assert call_narrated_mpt is narrated_mpt_client.call_narrated_mpt
    assert looks_like_file_reference is narrated_mpt_client.looks_like_file_reference
    assert validate_spoken_subtitle is narrated_mpt_client.validate_spoken_subtitle


def test_tools_slideshow_and_briefing_modules_export_public_contracts() -> None:
    import content_pipeline.tools.briefing_script_writer as briefing_script_writer
    import content_pipeline.tools.slideshow_client as slideshow_client
    from content_pipeline.tools.briefing_script_writer import (
        FINANCE_PROMPT_VERSION,
        PROMPT_VERSION,
        rewrite_ai_briefing_script_with_llm,
        rewrite_finance_script_with_llm,
    )
    from content_pipeline.tools.slideshow_client import SLIDESHOW_RENDER_VERSION, choose_bgm, render_slideshow

    assert slideshow_client.__all__ == ["SLIDESHOW_RENDER_VERSION", "choose_bgm", "render_slideshow"]
    assert briefing_script_writer.__all__ == [
        "FINANCE_PROMPT_VERSION",
        "PROMPT_VERSION",
        "rewrite_ai_briefing_script_with_llm",
        "rewrite_finance_script_with_llm",
    ]
    assert SLIDESHOW_RENDER_VERSION == slideshow_client.SLIDESHOW_RENDER_VERSION
    assert choose_bgm is slideshow_client.choose_bgm
    assert render_slideshow is slideshow_client.render_slideshow
    assert FINANCE_PROMPT_VERSION == briefing_script_writer.FINANCE_PROMPT_VERSION
    assert PROMPT_VERSION == briefing_script_writer.PROMPT_VERSION
    assert rewrite_ai_briefing_script_with_llm is briefing_script_writer.rewrite_ai_briefing_script_with_llm
    assert rewrite_finance_script_with_llm is briefing_script_writer.rewrite_finance_script_with_llm


def test_api_package_exports_application_contract() -> None:
    import content_pipeline.api as package_api
    import content_pipeline.api.app as app_module
    from content_pipeline.api import create_app
    from content_pipeline.api.app import create_app as app_create_app

    assert package_api.__all__ == app_module.__all__ == ["create_app"]
    assert create_app is app_create_app


def test_api_artifacts_module_exports_public_contract() -> None:
    import content_pipeline.api.artifacts as artifacts
    from content_pipeline.api.artifacts import RegisteredArtifact, registered_artifacts

    assert artifacts.__all__ == ["RegisteredArtifact", "registered_artifacts"]
    assert RegisteredArtifact is artifacts.RegisteredArtifact
    assert registered_artifacts is artifacts.registered_artifacts


def test_api_content_module_exports_public_contract() -> None:
    import content_pipeline.api.content as content
    from content_pipeline.api.content import build_content_router

    assert content.__all__ == ["build_content_router"]
    assert build_content_router is content.build_content_router


def test_api_network_module_exports_public_contract() -> None:
    import content_pipeline.api.network as network
    from content_pipeline.api.network import (
        ensure_bind_address_is_local,
        is_loopback_host,
        origin_matches,
        parse_ip_address,
        parse_networks,
        peer_is_allowed,
        secrets_compare,
        validate_bind_configuration,
    )

    assert network.__all__ == [
        "ensure_bind_address_is_local",
        "is_loopback_host",
        "origin_matches",
        "parse_ip_address",
        "parse_networks",
        "peer_is_allowed",
        "secrets_compare",
        "validate_bind_configuration",
    ]
    assert ensure_bind_address_is_local is network.ensure_bind_address_is_local
    assert is_loopback_host is network.is_loopback_host
    assert origin_matches is network.origin_matches
    assert parse_ip_address is network.parse_ip_address
    assert parse_networks is network.parse_networks
    assert peer_is_allowed is network.peer_is_allowed
    assert secrets_compare is network.secrets_compare
    assert validate_bind_configuration is network.validate_bind_configuration


def test_api_security_module_exports_public_contract() -> None:
    import content_pipeline.api.security as security
    from content_pipeline.api.security import SessionData, WebSecurity

    assert security.__all__ == ["SessionData", "WebSecurity"]
    assert SessionData is security.SessionData
    assert WebSecurity is security.WebSecurity


def test_api_ui_schema_module_exports_public_contract() -> None:
    import content_pipeline.api.ui_schema as ui_schema
    from content_pipeline.api.ui_schema import (
        build_ui_pipelines,
        sanitized_error_items,
        sanitized_validation_errors,
        validate_task_for_ui,
    )

    assert ui_schema.__all__ == [
        "build_ui_pipelines",
        "sanitized_error_items",
        "sanitized_validation_errors",
        "validate_task_for_ui",
    ]
    assert build_ui_pipelines is ui_schema.build_ui_pipelines
    assert sanitized_error_items is ui_schema.sanitized_error_items
    assert sanitized_validation_errors is ui_schema.sanitized_validation_errors
    assert validate_task_for_ui is ui_schema.validate_task_for_ui


def test_cli_package_exports_entrypoint_contract() -> None:
    import importlib

    import content_pipeline.cli as package_cli
    from content_pipeline.cli import main
    from content_pipeline.cli.main import main as cli_main

    cli_module = importlib.import_module("content_pipeline.cli.main")

    assert package_cli.__all__ == cli_module.__all__ == ["main"]
    assert main is cli_main


def test_cli_capabilities_module_exports_public_contract() -> None:
    import content_pipeline.cli.capabilities as capabilities
    from content_pipeline.cli.capabilities import run_capabilities

    assert capabilities.__all__ == ["run_capabilities"]
    assert run_capabilities is capabilities.run_capabilities


def test_cli_doctor_module_exports_public_contract() -> None:
    import content_pipeline.cli.doctor as doctor
    from content_pipeline.cli.doctor import run_doctor, run_doctor_bundle

    assert doctor.__all__ == ["run_doctor", "run_doctor_bundle"]
    assert run_doctor is doctor.run_doctor
    assert run_doctor_bundle is doctor.run_doctor_bundle


def test_core_package_exports_settings_contract() -> None:
    import content_pipeline.core as core
    import content_pipeline.core.settings as facade_settings
    import content_pipeline.settings as root_settings
    from content_pipeline.core import Settings, load_settings
    from content_pipeline.core.settings import Settings as FacadeSettings
    from content_pipeline.core.settings import load_settings as facade_load_settings
    from content_pipeline.settings import Settings as RootSettings
    from content_pipeline.settings import load_settings as root_load_settings

    assert root_settings.__all__ == facade_settings.__all__ == ["Settings", "load_settings"]
    assert core.__all__ == [
        "ConfigError",
        "ExternalToolError",
        "MediaValidationError",
        "PipelineError",
        "PrivateVisibilityUnsupportedError",
        "Settings",
        "UnknownContentTypeError",
        "load_settings",
    ]
    assert Settings is RootSettings
    assert Settings is FacadeSettings
    assert load_settings is root_load_settings
    assert load_settings is facade_load_settings


def test_core_models_facade_exports_public_contract() -> None:
    import content_pipeline.core.models as facade_models
    import content_pipeline.models as root_models
    from content_pipeline.core.models import (
        AdapterResult,
        AiArtParams,
        ArtifactSet,
        ContentType,
        CoverParams,
        DeferredPublishInput,
        ErrorCode,
        JobSnapshot,
        JobStatus,
        PipelineStep,
        PublishTarget,
        TaskInput,
        deferred_publish_fingerprint,
        task_fingerprint,
    )
    from content_pipeline.models import AdapterResult as RootAdapterResult
    from content_pipeline.models import AiArtParams as RootAiArtParams
    from content_pipeline.models import ArtifactSet as RootArtifactSet
    from content_pipeline.models import ContentType as RootContentType
    from content_pipeline.models import CoverParams as RootCoverParams
    from content_pipeline.models import DeferredPublishInput as RootDeferredPublishInput
    from content_pipeline.models import ErrorCode as RootErrorCode
    from content_pipeline.models import JobSnapshot as RootJobSnapshot
    from content_pipeline.models import JobStatus as RootJobStatus
    from content_pipeline.models import PipelineStep as RootPipelineStep
    from content_pipeline.models import PublishTarget as RootPublishTarget
    from content_pipeline.models import TaskInput as RootTaskInput
    from content_pipeline.models import deferred_publish_fingerprint as root_deferred_publish_fingerprint
    from content_pipeline.models import task_fingerprint as root_task_fingerprint

    assert root_models.__all__ == facade_models.__all__
    assert AdapterResult is RootAdapterResult
    assert AiArtParams is RootAiArtParams
    assert ArtifactSet is RootArtifactSet
    assert ContentType is RootContentType
    assert CoverParams is RootCoverParams
    assert DeferredPublishInput is RootDeferredPublishInput
    assert ErrorCode is RootErrorCode
    assert JobSnapshot is RootJobSnapshot
    assert JobStatus is RootJobStatus
    assert PipelineStep is RootPipelineStep
    assert PublishTarget is RootPublishTarget
    assert TaskInput is RootTaskInput
    assert deferred_publish_fingerprint is root_deferred_publish_fingerprint
    assert task_fingerprint is root_task_fingerprint


def test_core_package_exports_error_contract() -> None:
    import content_pipeline.core.errors as facade_errors
    import content_pipeline.errors as root_errors
    from content_pipeline.core import (
        ConfigError,
        ExternalToolError,
        MediaValidationError,
        PipelineError,
        PrivateVisibilityUnsupportedError,
        UnknownContentTypeError,
    )
    from content_pipeline.core.errors import ConfigError as FacadeConfigError
    from content_pipeline.core.errors import ExternalToolError as FacadeExternalToolError
    from content_pipeline.core.errors import MediaValidationError as FacadeMediaValidationError
    from content_pipeline.core.errors import PipelineError as FacadePipelineError
    from content_pipeline.core.errors import (
        PrivateVisibilityUnsupportedError as FacadePrivateVisibilityUnsupportedError,
    )
    from content_pipeline.core.errors import UnknownContentTypeError as FacadeUnknownContentTypeError
    from content_pipeline.errors import ConfigError as RootConfigError
    from content_pipeline.errors import ExternalToolError as RootExternalToolError
    from content_pipeline.errors import MediaValidationError as RootMediaValidationError
    from content_pipeline.errors import PipelineError as RootPipelineError
    from content_pipeline.errors import PrivateVisibilityUnsupportedError as RootPrivateVisibilityUnsupportedError
    from content_pipeline.errors import UnknownContentTypeError as RootUnknownContentTypeError

    assert root_errors.__all__ == facade_errors.__all__
    assert PipelineError is RootPipelineError
    assert PipelineError is FacadePipelineError
    assert ConfigError is RootConfigError
    assert ConfigError is FacadeConfigError
    assert ExternalToolError is RootExternalToolError
    assert ExternalToolError is FacadeExternalToolError
    assert MediaValidationError is RootMediaValidationError
    assert MediaValidationError is FacadeMediaValidationError
    assert PrivateVisibilityUnsupportedError is RootPrivateVisibilityUnsupportedError
    assert PrivateVisibilityUnsupportedError is FacadePrivateVisibilityUnsupportedError
    assert UnknownContentTypeError is RootUnknownContentTypeError
    assert UnknownContentTypeError is FacadeUnknownContentTypeError


def test_core_publishing_package_exports_policy_contract() -> None:
    import content_pipeline.core.publishing as package_publishing
    import content_pipeline.core.publishing.policy as policy
    from content_pipeline.core.publishing import (
        BILIBILI,
        DOUYIN,
        KUAISHOU,
        PLATFORM_POLICIES,
        XIAOHONGSHU,
        PlatformPolicy,
        get_policy,
        validate_publish_request,
    )
    from content_pipeline.core.publishing.policy import BILIBILI as POLICY_BILIBILI
    from content_pipeline.core.publishing.policy import DOUYIN as POLICY_DOUYIN
    from content_pipeline.core.publishing.policy import KUAISHOU as POLICY_KUAISHOU
    from content_pipeline.core.publishing.policy import PLATFORM_POLICIES as POLICY_PLATFORM_POLICIES
    from content_pipeline.core.publishing.policy import XIAOHONGSHU as POLICY_XIAOHONGSHU
    from content_pipeline.core.publishing.policy import PlatformPolicy as PolicyPlatformPolicy
    from content_pipeline.core.publishing.policy import get_policy as policy_get_policy
    from content_pipeline.core.publishing.policy import validate_publish_request as policy_validate_publish_request

    assert package_publishing.__all__ == policy.__all__
    assert BILIBILI is POLICY_BILIBILI
    assert DOUYIN is POLICY_DOUYIN
    assert KUAISHOU is POLICY_KUAISHOU
    assert PLATFORM_POLICIES is POLICY_PLATFORM_POLICIES
    assert XIAOHONGSHU is POLICY_XIAOHONGSHU
    assert PlatformPolicy is PolicyPlatformPolicy
    assert get_policy is policy_get_policy
    assert validate_publish_request is policy_validate_publish_request
    assert get_policy("xiaohongshu") is XIAOHONGSHU
    assert "tencent" not in PLATFORM_POLICIES


def test_pipelines_package_exports_registry_contract() -> None:
    import content_pipeline.pipelines as package_pipelines
    import content_pipeline.pipelines.registry as registry
    from content_pipeline.pipelines import (
        PIPELINE_METADATA,
        PIPELINE_REGISTRY,
        PipelineContext,
        PipelineFunc,
        PipelineMeta,
        get_pipeline,
        get_pipeline_meta,
        list_pipelines,
        register,
    )
    from content_pipeline.pipelines.registry import PIPELINE_METADATA as REGISTRY_METADATA
    from content_pipeline.pipelines.registry import PIPELINE_REGISTRY as REGISTRY_PIPELINES
    from content_pipeline.pipelines.registry import PipelineContext as RegistryPipelineContext
    from content_pipeline.pipelines.registry import PipelineFunc as RegistryPipelineFunc
    from content_pipeline.pipelines.registry import PipelineMeta as RegistryPipelineMeta
    from content_pipeline.pipelines.registry import get_pipeline as registry_get_pipeline
    from content_pipeline.pipelines.registry import get_pipeline_meta as registry_get_pipeline_meta
    from content_pipeline.pipelines.registry import list_pipelines as registry_list_pipelines
    from content_pipeline.pipelines.registry import register as registry_register

    assert package_pipelines.__all__ == registry.__all__
    assert PIPELINE_METADATA is REGISTRY_METADATA
    assert PIPELINE_REGISTRY is REGISTRY_PIPELINES
    assert PipelineContext is RegistryPipelineContext
    assert PipelineFunc is RegistryPipelineFunc
    assert PipelineMeta is RegistryPipelineMeta
    assert get_pipeline is registry_get_pipeline
    assert get_pipeline_meta is registry_get_pipeline_meta
    assert list_pipelines is registry_list_pipelines
    assert register is registry_register
    assert "anime" in PIPELINE_REGISTRY
    assert get_pipeline("anime") is PIPELINE_REGISTRY["anime"]


def test_profiles_module_exports_public_contract() -> None:
    import content_pipeline.profiles as profiles
    from content_pipeline.profiles import ImageGenProfile, Profile, UploadProfile, VideoGenProfile, load_profile

    assert profiles.__all__ == ["ImageGenProfile", "Profile", "UploadProfile", "VideoGenProfile", "load_profile"]
    assert ImageGenProfile.__name__ == "ImageGenProfile"
    assert Profile.__name__ == "Profile"
    assert UploadProfile.__name__ == "UploadProfile"
    assert VideoGenProfile.__name__ == "VideoGenProfile"
    assert callable(load_profile)


def test_router_module_exports_public_contract() -> None:
    import content_pipeline.router as router
    from content_pipeline.router import route_task

    assert router.__all__ == ["route_task"]
    assert route_task is router.route_task


def test_pipeline_config_module_exports_public_contract() -> None:
    import content_pipeline.pipeline_config as pipeline_config
    from content_pipeline.pipeline_config import apply_pipeline_defaults, load_pipeline_defaults

    assert pipeline_config.__all__ == [
        "apply_pipeline_defaults",
        "load_effective_pipeline_defaults",
        "load_pipeline_defaults",
        "save_pipeline_defaults_override",
    ]
    assert apply_pipeline_defaults is pipeline_config.apply_pipeline_defaults
    assert load_pipeline_defaults is pipeline_config.load_pipeline_defaults


def test_task_validation_module_exports_public_contract() -> None:
    import content_pipeline.task_validation as task_validation
    from content_pipeline.task_validation import PARAM_MODELS, AnimeParams, validate_task_params

    assert task_validation.__all__ == ["AnimeParams", "PARAM_MODELS", "validate_task_params"]
    assert AnimeParams is task_validation.AnimeParams
    assert PARAM_MODELS is task_validation.PARAM_MODELS
    assert validate_task_params is task_validation.validate_task_params


def test_diagnostics_module_exports_entrypoint_contract() -> None:
    import content_pipeline.diagnostics as diagnostics
    from content_pipeline.diagnostics import main

    assert diagnostics.__all__ == ["main"]
    assert main is diagnostics.main


def test_rendering_module_exports_public_contract() -> None:
    import content_pipeline.rendering as rendering
    from content_pipeline.rendering import render_template

    assert rendering.__all__ == ["render_template"]
    assert render_template is rendering.render_template


def test_grouping_module_exports_public_contract() -> None:
    import content_pipeline.grouping as grouping
    from content_pipeline.grouping import group_by_prefix

    assert grouping.__all__ == ["group_by_prefix"]
    assert group_by_prefix is grouping.group_by_prefix


def test_orchestrator_module_exports_public_contract() -> None:
    import content_pipeline.orchestrator as orchestrator
    from content_pipeline.orchestrator import Orchestrator, main, run_task_file

    assert orchestrator.__all__ == ["Orchestrator", "main", "run_task_file"]
    assert Orchestrator is orchestrator.Orchestrator
    assert main is orchestrator.main
    assert run_task_file is orchestrator.run_task_file


def test_deferred_publishing_module_exports_public_contract() -> None:
    import content_pipeline.deferred_publishing as deferred_publishing
    from content_pipeline.deferred_publishing import (
        ACTIVE_PUBLICATION_STATUSES,
        PUBLICATION_STATE_LABELS,
        SUPPORTED_SCRIPT_VIDEO_TARGETS,
        PublicationEligibilityError,
        is_content_studio_job,
        list_publish_account_options,
        publication_readiness,
        publication_summary,
        resolve_guarded_video,
        run_deferred_publication,
        sha256_file,
        validate_deferred_publish_request,
    )

    assert deferred_publishing.__all__ == [
        "ACTIVE_PUBLICATION_STATUSES",
        "PUBLICATION_STATE_LABELS",
        "SUPPORTED_SCRIPT_VIDEO_TARGETS",
        "PublicationEligibilityError",
        "is_content_studio_job",
        "list_publish_account_options",
        "publication_readiness",
        "publication_summary",
        "resolve_guarded_video",
        "run_deferred_publication",
        "sha256_file",
        "validate_deferred_publish_request",
    ]
    assert ACTIVE_PUBLICATION_STATUSES is deferred_publishing.ACTIVE_PUBLICATION_STATUSES
    assert PUBLICATION_STATE_LABELS is deferred_publishing.PUBLICATION_STATE_LABELS
    assert SUPPORTED_SCRIPT_VIDEO_TARGETS is deferred_publishing.SUPPORTED_SCRIPT_VIDEO_TARGETS
    assert PublicationEligibilityError is deferred_publishing.PublicationEligibilityError
    assert is_content_studio_job is deferred_publishing.is_content_studio_job
    assert list_publish_account_options is deferred_publishing.list_publish_account_options
    assert publication_readiness is deferred_publishing.publication_readiness
    assert publication_summary is deferred_publishing.publication_summary
    assert resolve_guarded_video is deferred_publishing.resolve_guarded_video
    assert run_deferred_publication is deferred_publishing.run_deferred_publication
    assert sha256_file is deferred_publishing.sha256_file
    assert validate_deferred_publish_request is deferred_publishing.validate_deferred_publish_request


def test_ai_art_pipeline_module_exports_public_contract() -> None:
    import content_pipeline.ai_art_pipeline as ai_art_pipeline
    from content_pipeline.ai_art_pipeline import run_ai_art_pipeline

    assert ai_art_pipeline.__all__ == ["run_ai_art_pipeline"]
    assert run_ai_art_pipeline is ai_art_pipeline.run_ai_art_pipeline


def test_grouped_anime_pipeline_module_exports_public_contract() -> None:
    import content_pipeline.grouped_anime_pipeline as grouped_anime_pipeline
    from content_pipeline.grouped_anime_pipeline import (
        MPT_VISUAL_ONLY_PLACEHOLDER,
        run_grouped_anime_pipeline,
    )

    assert grouped_anime_pipeline.__all__ == [
        "MPT_VISUAL_ONLY_PLACEHOLDER",
        "run_grouped_anime_pipeline",
    ]
    assert MPT_VISUAL_ONLY_PLACEHOLDER is grouped_anime_pipeline.MPT_VISUAL_ONLY_PLACEHOLDER
    assert run_grouped_anime_pipeline is grouped_anime_pipeline.run_grouped_anime_pipeline


def test_japanese_pipeline_module_exports_public_contract() -> None:
    import content_pipeline.japanese_pipeline as japanese_pipeline
    from content_pipeline.japanese_pipeline import JAPANESE_TARGET_GEM_URL, run_japanese_pipeline

    assert japanese_pipeline.__all__ == ["JAPANESE_TARGET_GEM_URL", "run_japanese_pipeline"]
    assert JAPANESE_TARGET_GEM_URL is japanese_pipeline.JAPANESE_TARGET_GEM_URL
    assert run_japanese_pipeline is japanese_pipeline.run_japanese_pipeline


def test_script_video_pipeline_module_exports_public_contract() -> None:
    import content_pipeline.script_video_pipeline as script_video_pipeline
    from content_pipeline.script_video_pipeline import run_script_video_pipeline

    assert script_video_pipeline.__all__ == ["run_script_video_pipeline"]
    assert run_script_video_pipeline is script_video_pipeline.run_script_video_pipeline


def test_xhs_image_note_pipeline_module_exports_public_contract() -> None:
    import content_pipeline.xhs_image_note_pipeline as xhs_image_note_pipeline
    from content_pipeline.xhs_image_note_pipeline import run_xhs_image_note_pipeline

    assert xhs_image_note_pipeline.__all__ == ["run_xhs_image_note_pipeline"]
    assert run_xhs_image_note_pipeline is xhs_image_note_pipeline.run_xhs_image_note_pipeline


def test_ai_briefing_runner_module_exports_public_contract() -> None:
    import content_pipeline.ai_briefing_runner as ai_briefing_runner
    from content_pipeline.ai_briefing_runner import main

    assert ai_briefing_runner.__all__ == ["main"]
    assert main is ai_briefing_runner.main


def test_ai_briefing_pipeline_module_exports_public_contract() -> None:
    import content_pipeline.ai_briefing_pipeline as ai_briefing_pipeline
    from content_pipeline.ai_briefing_pipeline import (
        acquire_run_lock,
        build_90_second_briefing_script,
        build_briefing_script_material,
        build_publish_title,
        build_video_title,
        normalize_date,
        normalize_handoff,
        release_run_lock,
        run_ai_briefing_pipeline,
        validate_ai_briefing_narration,
        wait_for_daily_inputs,
    )

    assert ai_briefing_pipeline.__all__ == [
        "acquire_run_lock",
        "build_90_second_briefing_script",
        "build_briefing_script_material",
        "build_publish_title",
        "build_video_title",
        "normalize_date",
        "normalize_handoff",
        "release_run_lock",
        "run_ai_briefing_pipeline",
        "validate_ai_briefing_narration",
        "wait_for_daily_inputs",
    ]
    assert acquire_run_lock is ai_briefing_pipeline.acquire_run_lock
    assert build_90_second_briefing_script is ai_briefing_pipeline.build_90_second_briefing_script
    assert build_briefing_script_material is ai_briefing_pipeline.build_briefing_script_material
    assert build_publish_title is ai_briefing_pipeline.build_publish_title
    assert build_video_title is ai_briefing_pipeline.build_video_title
    assert normalize_date is ai_briefing_pipeline.normalize_date
    assert normalize_handoff is ai_briefing_pipeline.normalize_handoff
    assert release_run_lock is ai_briefing_pipeline.release_run_lock
    assert run_ai_briefing_pipeline is ai_briefing_pipeline.run_ai_briefing_pipeline
    assert validate_ai_briefing_narration is ai_briefing_pipeline.validate_ai_briefing_narration
    assert wait_for_daily_inputs is ai_briefing_pipeline.wait_for_daily_inputs


def test_finance_script_module_exports_public_contract() -> None:
    import content_pipeline.finance_script as finance_script
    from content_pipeline.finance_script import (
        CONTENT_STUDIO_FINANCE_DISCLAIMER,
        CONTENT_STUDIO_MAX_CHARS,
        CONTENT_STUDIO_MIN_CHARS,
        DAILY_FINANCE_DISCLAIMER,
        FINANCE_MAX_CHARS,
        FINANCE_MIN_CHARS,
        FINANCE_RISK_NOTE,
        FINANCE_SECTION_LABELS,
        FinanceScriptError,
        build_daily_markdown_script,
        build_structured_finance_script,
        clean_markdown,
        trim_complete,
        validate_daily_finance_script,
    )

    assert finance_script.__all__ == [
        "CONTENT_STUDIO_FINANCE_DISCLAIMER",
        "CONTENT_STUDIO_MAX_CHARS",
        "CONTENT_STUDIO_MIN_CHARS",
        "DAILY_FINANCE_DISCLAIMER",
        "FINANCE_MAX_CHARS",
        "FINANCE_MIN_CHARS",
        "FINANCE_RISK_NOTE",
        "FINANCE_SECTION_LABELS",
        "FinanceScriptError",
        "build_daily_markdown_script",
        "build_structured_finance_script",
        "clean_markdown",
        "trim_complete",
        "validate_daily_finance_script",
    ]
    assert CONTENT_STUDIO_FINANCE_DISCLAIMER is finance_script.CONTENT_STUDIO_FINANCE_DISCLAIMER
    assert CONTENT_STUDIO_MAX_CHARS is finance_script.CONTENT_STUDIO_MAX_CHARS
    assert CONTENT_STUDIO_MIN_CHARS is finance_script.CONTENT_STUDIO_MIN_CHARS
    assert DAILY_FINANCE_DISCLAIMER is finance_script.DAILY_FINANCE_DISCLAIMER
    assert FINANCE_MAX_CHARS is finance_script.FINANCE_MAX_CHARS
    assert FINANCE_MIN_CHARS is finance_script.FINANCE_MIN_CHARS
    assert FINANCE_RISK_NOTE is finance_script.FINANCE_RISK_NOTE
    assert FINANCE_SECTION_LABELS is finance_script.FINANCE_SECTION_LABELS
    assert FinanceScriptError is finance_script.FinanceScriptError
    assert build_daily_markdown_script is finance_script.build_daily_markdown_script
    assert build_structured_finance_script is finance_script.build_structured_finance_script
    assert clean_markdown is finance_script.clean_markdown
    assert trim_complete is finance_script.trim_complete
    assert validate_daily_finance_script is finance_script.validate_daily_finance_script


def test_finance_pipeline_module_exports_public_contract() -> None:
    import content_pipeline.finance_pipeline as finance_pipeline
    from content_pipeline.finance_pipeline import (
        DISCLAIMER,
        FINANCE_NARRATION_MAX_CHARS,
        FINANCE_SECTION_LABELS,
        RISK_NOTE,
        build_90_second_script,
        build_finance_script_material,
        extract_response,
        run_finance_pipeline,
        select_daily_markdown,
        validate_finance_narration,
    )

    assert finance_pipeline.__all__ == [
        "DISCLAIMER",
        "FINANCE_NARRATION_MAX_CHARS",
        "FINANCE_SECTION_LABELS",
        "RISK_NOTE",
        "build_90_second_script",
        "build_finance_script_material",
        "extract_response",
        "run_finance_pipeline",
        "select_daily_markdown",
        "validate_finance_narration",
    ]
    assert DISCLAIMER is finance_pipeline.DISCLAIMER
    assert FINANCE_NARRATION_MAX_CHARS is finance_pipeline.FINANCE_NARRATION_MAX_CHARS
    assert FINANCE_SECTION_LABELS is finance_pipeline.FINANCE_SECTION_LABELS
    assert RISK_NOTE is finance_pipeline.RISK_NOTE
    assert build_90_second_script is finance_pipeline.build_90_second_script
    assert build_finance_script_material is finance_pipeline.build_finance_script_material
    assert extract_response is finance_pipeline.extract_response
    assert run_finance_pipeline is finance_pipeline.run_finance_pipeline
    assert select_daily_markdown is finance_pipeline.select_daily_markdown
    assert validate_finance_narration is finance_pipeline.validate_finance_narration


def test_mcp_server_module_exports_tool_contract() -> None:
    import content_pipeline.mcp_server as mcp_server
    from content_pipeline.mcp_server import (
        generate_images,
        get_external_tool_contracts,
        get_job_events,
        get_status,
        list_capabilities,
        list_jobs,
        list_profiles,
        main,
        open_photo_process_debug,
        process_ai_art,
        process_ai_art_async,
        process_japanese_images,
        render_video,
        run_finance_video_async,
        run_task_async,
        run_task_sync,
        start_task,
        submit_task,
    )

    assert mcp_server.__all__ == [
        "generate_images",
        "get_external_tool_contracts",
        "get_job_events",
        "get_status",
        "list_capabilities",
        "list_jobs",
        "list_profiles",
        "main",
        "open_photo_process_debug",
        "process_ai_art",
        "process_ai_art_async",
        "process_japanese_images",
        "render_video",
        "run_finance_video_async",
        "run_task_async",
        "run_task_sync",
        "start_task",
        "submit_task",
    ]
    assert generate_images is mcp_server.generate_images
    assert get_external_tool_contracts is mcp_server.get_external_tool_contracts
    assert get_job_events is mcp_server.get_job_events
    assert get_status is mcp_server.get_status
    assert list_capabilities is mcp_server.list_capabilities
    assert list_jobs is mcp_server.list_jobs
    assert list_profiles is mcp_server.list_profiles
    assert main is mcp_server.main
    assert open_photo_process_debug is mcp_server.open_photo_process_debug
    assert process_ai_art is mcp_server.process_ai_art
    assert process_ai_art_async is mcp_server.process_ai_art_async
    assert process_japanese_images is mcp_server.process_japanese_images
    assert render_video is mcp_server.render_video
    assert run_finance_video_async is mcp_server.run_finance_video_async
    assert run_task_async is mcp_server.run_task_async
    assert run_task_sync is mcp_server.run_task_sync
    assert start_task is mcp_server.start_task
    assert submit_task is mcp_server.submit_task


def test_photo_process_debug_module_exports_public_contract() -> None:
    import content_pipeline.photo_process_debug as photo_process_debug
    from content_pipeline.photo_process_debug import (
        DEFAULT_DEBUG_URL,
        DEFAULT_DESKTOP_HELPER_URL,
        SNAPSHOT_RELATIVE_PATH,
        VISIBLE_WINDOW_TIMEOUT_SECONDS,
        DebugMode,
        inspect_chrome_profile_processes,
        inspect_visible_chrome_windows,
        main,
        open_photo_process_debug,
        open_photo_process_desktop_debug,
    )

    assert photo_process_debug.__all__ == [
        "DEFAULT_DEBUG_URL",
        "DEFAULT_DESKTOP_HELPER_URL",
        "DebugMode",
        "SNAPSHOT_RELATIVE_PATH",
        "VISIBLE_WINDOW_TIMEOUT_SECONDS",
        "inspect_chrome_profile_processes",
        "inspect_visible_chrome_windows",
        "main",
        "open_photo_process_debug",
        "open_photo_process_desktop_debug",
    ]
    assert DEFAULT_DEBUG_URL is photo_process_debug.DEFAULT_DEBUG_URL
    assert DEFAULT_DESKTOP_HELPER_URL is photo_process_debug.DEFAULT_DESKTOP_HELPER_URL
    assert DebugMode is photo_process_debug.DebugMode
    assert SNAPSHOT_RELATIVE_PATH is photo_process_debug.SNAPSHOT_RELATIVE_PATH
    assert VISIBLE_WINDOW_TIMEOUT_SECONDS is photo_process_debug.VISIBLE_WINDOW_TIMEOUT_SECONDS
    assert inspect_chrome_profile_processes is photo_process_debug.inspect_chrome_profile_processes
    assert inspect_visible_chrome_windows is photo_process_debug.inspect_visible_chrome_windows
    assert main is photo_process_debug.main
    assert open_photo_process_debug is photo_process_debug.open_photo_process_debug
    assert open_photo_process_desktop_debug is photo_process_debug.open_photo_process_desktop_debug


def test_desktop_browser_helper_module_exports_public_contract() -> None:
    import content_pipeline.desktop_browser_helper as desktop_browser_helper
    from content_pipeline.desktop_browser_helper import (
        DEFAULT_CHROME_PATHS,
        DEFAULT_HOST,
        DEFAULT_PORT,
        BrowserHelperHandler,
        find_chrome,
        main,
        open_visible_chrome,
        serve,
    )

    assert desktop_browser_helper.__all__ == [
        "BrowserHelperHandler",
        "DEFAULT_CHROME_PATHS",
        "DEFAULT_HOST",
        "DEFAULT_PORT",
        "find_chrome",
        "main",
        "open_visible_chrome",
        "serve",
    ]
    assert BrowserHelperHandler is desktop_browser_helper.BrowserHelperHandler
    assert DEFAULT_CHROME_PATHS is desktop_browser_helper.DEFAULT_CHROME_PATHS
    assert DEFAULT_HOST is desktop_browser_helper.DEFAULT_HOST
    assert DEFAULT_PORT is desktop_browser_helper.DEFAULT_PORT
    assert find_chrome is desktop_browser_helper.find_chrome
    assert main is desktop_browser_helper.main
    assert open_visible_chrome is desktop_browser_helper.open_visible_chrome
    assert serve is desktop_browser_helper.serve


def test_tools_package_exports_common_contract() -> None:
    import content_pipeline.tools as package_tools
    import content_pipeline.tools.audio_client as audio_client
    import content_pipeline.tools.common as common
    from content_pipeline.tools import prepare_music_track, run_command
    from content_pipeline.tools.audio_client import prepare_music_track as audio_prepare_music_track
    from content_pipeline.tools.common import mpt_task_id as common_mpt_task_id
    from content_pipeline.tools.common import run_command as common_run_command

    assert package_tools.__all__ == ["prepare_music_track", "run_command"]
    assert audio_client.__all__ == ["prepare_music_track"]
    assert common.__all__ == ["mpt_task_id", "run_command"]
    assert prepare_music_track is audio_prepare_music_track
    assert prepare_music_track is audio_client.prepare_music_track
    assert run_command is common_run_command
    assert run_command is common.run_command
    assert common_mpt_task_id is common.mpt_task_id


def test_tools_sau_client_module_exports_public_contract() -> None:
    import content_pipeline.tools.sau_client as sau_client
    from content_pipeline.tools.sau_client import call_sau_target, call_sau_upload, call_sau_xiaohongshu_note

    assert sau_client.__all__ == ["call_sau_target", "call_sau_upload", "call_sau_xiaohongshu_note"]
    assert call_sau_target is sau_client.call_sau_target
    assert call_sau_upload is sau_client.call_sau_upload
    assert call_sau_xiaohongshu_note is sau_client.call_sau_xiaohongshu_note


def test_validation_package_exports_media_contract() -> None:
    import content_pipeline.media_validation as root_media_validation
    import content_pipeline.validation as package_validation
    import content_pipeline.validation.media as facade_media
    from content_pipeline.media_validation import validate_images as root_validate_images
    from content_pipeline.media_validation import validate_video as root_validate_video
    from content_pipeline.validation import validate_images, validate_video
    from content_pipeline.validation.media import validate_images as facade_validate_images
    from content_pipeline.validation.media import validate_video as facade_validate_video

    assert root_media_validation.__all__ == facade_media.__all__ == package_validation.__all__
    assert validate_images is root_validate_images
    assert validate_images is facade_validate_images
    assert validate_video is root_validate_video
    assert validate_video is facade_validate_video


def test_web_package_exports_static_asset_contract() -> None:
    from importlib.resources import files

    from content_pipeline.web import WEB_STATIC_DIR

    static = files("content_pipeline.web").joinpath("static")

    assert WEB_STATIC_DIR.name == "static"
    assert WEB_STATIC_DIR.is_dir()
    assert static == WEB_STATIC_DIR
    assert WEB_STATIC_DIR.joinpath("index.html").is_file()


def test_content_studio_package_exports_public_contract() -> None:
    from content_pipeline.content_studio import (
        ContentDraft,
        ContentDraftStore,
        ContentStudioService,
        CreateDraftInput,
        DraftConflictError,
        DraftCorruptError,
        DraftNotFoundError,
        DraftRevisionInput,
        GenerateVideoInput,
    )
    from content_pipeline.content_studio.models import ContentDraft as ModelContentDraft
    from content_pipeline.content_studio.models import CreateDraftInput as ModelCreateDraftInput
    from content_pipeline.content_studio.models import DraftRevisionInput as ModelDraftRevisionInput
    from content_pipeline.content_studio.models import GenerateVideoInput as ModelGenerateVideoInput
    from content_pipeline.content_studio.service import ContentStudioService as ServiceContentStudioService
    from content_pipeline.content_studio.store import ContentDraftStore as StoreContentDraftStore
    from content_pipeline.content_studio.store import DraftConflictError as StoreDraftConflictError
    from content_pipeline.content_studio.store import DraftCorruptError as StoreDraftCorruptError
    from content_pipeline.content_studio.store import DraftNotFoundError as StoreDraftNotFoundError

    assert ContentDraft is ModelContentDraft
    assert ContentDraftStore is StoreContentDraftStore
    assert ContentStudioService is ServiceContentStudioService
    assert CreateDraftInput is ModelCreateDraftInput
    assert DraftConflictError is StoreDraftConflictError
    assert DraftCorruptError is StoreDraftCorruptError
    assert DraftNotFoundError is StoreDraftNotFoundError
    assert DraftRevisionInput is ModelDraftRevisionInput
    assert GenerateVideoInput is ModelGenerateVideoInput


def test_content_studio_models_module_exports_public_contract() -> None:
    import content_pipeline.content_studio.models as models
    from content_pipeline.content_studio.models import (
        ContentDraft,
        CreateDraftInput,
        DraftRevisionInput,
        DraftStatus,
        GenerateVideoInput,
        ResearchSource,
        SourceStatus,
        UpdateDraftInput,
    )

    assert models.__all__ == [
        "ContentDraft",
        "CreateDraftInput",
        "DraftRevisionInput",
        "DraftStatus",
        "GenerateVideoInput",
        "ResearchSource",
        "SourceStatus",
        "UpdateDraftInput",
    ]
    assert ContentDraft is models.ContentDraft
    assert CreateDraftInput is models.CreateDraftInput
    assert DraftRevisionInput is models.DraftRevisionInput
    assert DraftStatus is models.DraftStatus
    assert GenerateVideoInput is models.GenerateVideoInput
    assert ResearchSource is models.ResearchSource
    assert SourceStatus is models.SourceStatus
    assert UpdateDraftInput is models.UpdateDraftInput


def test_content_studio_service_module_exports_public_contract() -> None:
    import content_pipeline.content_studio.service as service
    from content_pipeline.content_studio.service import ContentStudioService

    assert service.__all__ == ["ContentStudioService"]
    assert ContentStudioService is service.ContentStudioService


def test_content_studio_store_module_exports_public_contract() -> None:
    import content_pipeline.content_studio.store as store
    from content_pipeline.content_studio.store import (
        ContentDraftStore,
        DraftConflictError,
        DraftCorruptError,
        DraftNotFoundError,
    )

    assert store.__all__ == ["ContentDraftStore", "DraftConflictError", "DraftCorruptError", "DraftNotFoundError"]
    assert ContentDraftStore is store.ContentDraftStore
    assert DraftConflictError is store.DraftConflictError
    assert DraftCorruptError is store.DraftCorruptError
    assert DraftNotFoundError is store.DraftNotFoundError


def test_content_studio_ttskill_client_module_exports_public_contract() -> None:
    import content_pipeline.content_studio.ttskill_client as ttskill_client
    from content_pipeline.content_studio.ttskill_client import TTSkillAuthError, TTSkillClient, TTSkillError

    assert ttskill_client.__all__ == ["TTSkillAuthError", "TTSkillClient", "TTSkillError"]
    assert TTSkillAuthError is ttskill_client.TTSkillAuthError
    assert TTSkillClient is ttskill_client.TTSkillClient
    assert TTSkillError is ttskill_client.TTSkillError


def test_web_test_dependencies_include_httpx2() -> None:
    config = tomllib.loads(Path("pyproject.toml").read_text(encoding="utf-8"))

    test_dependencies = config["project"]["optional-dependencies"]["test"]

    assert "httpx>=0.27.0" in test_dependencies
    assert "httpx2>=0.28.0" in test_dependencies
