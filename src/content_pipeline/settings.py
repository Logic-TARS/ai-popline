from __future__ import annotations

from pathlib import Path

from pydantic import AliasChoices, Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

__all__ = ["Settings", "load_settings"]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        frozen=True,
        populate_by_name=True,
    )

    # Core
    data_dir: Path = Field(
        default=Path("output"),
        validation_alias=AliasChoices("DATA_DIR", "PIPELINE_DATA_DIR"),
    )
    profiles_dir: Path = Field(
        default=Path("profiles"),
        validation_alias=AliasChoices("PROFILES_DIR", "PIPELINE_PROFILES_DIR"),
    )
    pipeline_defaults_file: Path = Field(
        default=Path("config/pipeline.defaults.yaml"),
        validation_alias=AliasChoices("PIPELINE_DEFAULTS_FILE", "PIPELINE_CONFIG_FILE"),
    )
    publish_defaults_file: Path = Field(
        default=Path("config/publish.defaults.yaml"),
        validation_alias=AliasChoices("PUBLISH_DEFAULTS_FILE", "PUBLISH_POLICY_FILE"),
    )

    # Gemini
    gemini_skill_dir: Path = Path(r"G:\Job\gemini-skill")
    gemini_image_command: str = ""

    # Photo-Process
    photo_process_dir: Path = Path(r"G:\Job\Photo-Process")
    photo_process_python: Path = Path(r"G:\Job\Photo-Process\.venv311\Scripts\python.exe")
    # Optional localhost Photo-Process Browser Worker endpoint.
    # Empty keeps the legacy one-shot CLI adapter for backwards compatibility.
    photo_process_worker_url: str = ""

    # Cover-Forge localhost sidecar
    cover_forge_url: str = "http://127.0.0.1:3000"
    cover_forge_timeout_seconds: int = Field(default=60, ge=5, le=300)

    # MoneyPrinterTurbo
    mpt_dir: Path = Path(r"G:\Job\MoneyPrinterTurbo")
    mpt_python: Path = Path(r"G:\Job\MoneyPrinterTurbo\.venv\Scripts\python.exe")
    ai_art_bgm_dir: Path = Path(r"G:\Job\MoneyPrinterTurbo\resource\songs")

    # social-auto-upload
    sau_dir: Path = Path(r"G:\Job\social-auto-upload")
    sau_exe: Path = Path(r"G:\Job\social-auto-upload\.venv\Scripts\sau.exe")
    sau_bilibili_private_args: str = ""
    # SAU 账号中心 Bridge（server/bridge_server.py），用于发布账号下拉选项
    sau_bridge_url: str = "http://127.0.0.1:5800"
    sau_bridge_token: SecretStr = SecretStr("")

    # Finance / AI Briefing / content research
    finance_md_dir: Path = Path(r"G:\Job\Automation-Output\sajin\fund-daily")
    ai_briefing_dir: Path = Path(r"G:\Hermes-Output\每日AI简报")
    ttskill_command: str = "ttskill"
    ttskill_timeout_seconds: int = Field(default=120, ge=10, le=600)

    # Script writing LLM. Empty values inherit the Router LLM settings.
    script_llm_base_url: str = ""
    script_llm_api_key: str = ""
    script_llm_model: str = ""
    script_llm_timeout_seconds: int = Field(default=120, ge=5, le=600)

    # Router LLM (optional — keyword fallback when absent).
    router_llm_base_url: str = ""
    router_llm_api_key: str = ""
    router_llm_model: str = ""

    # Web / ZeroTier access. Remote access stays disabled with these defaults.
    web_bind_host: str = "127.0.0.1"
    web_port: int = Field(default=8080, ge=1, le=65535)
    web_allowed_networks: str = ""
    web_allowed_hosts: str = "127.0.0.1,localhost,::1"
    web_allowed_origins: str = "http://127.0.0.1:8080,http://localhost:8080"
    web_auth_required: bool = False
    web_admin_token: SecretStr = SecretStr("")
    web_api_token: SecretStr = SecretStr("")
    web_session_secret: SecretStr = SecretStr("")
    web_session_ttl_seconds: int = Field(default=604800, ge=300, le=604800)
    web_publish_enabled: bool = False
    web_publish_reauth_seconds: int = Field(default=604800, ge=60, le=604800)
    web_tls_certfile: Path | None = None
    web_tls_keyfile: Path | None = None
    web_allow_zerotier_http: bool = False
    # Tree swept by the console "cache maintenance" action. Relative paths follow
    # the data_dir convention, so the backend cleans the tree it was started from.
    cache_cleanup_root: Path = Field(
        default=Path("."),
        validation_alias=AliasChoices("CACHE_CLEANUP_ROOT"),
    )

    # Environment tag
    env: str = "development"

    @staticmethod
    def _split_csv(value: str) -> tuple[str, ...]:
        return tuple(item.strip() for item in value.split(",") if item.strip())

    @property
    def allowed_networks(self) -> tuple[str, ...]:
        return self._split_csv(self.web_allowed_networks)

    @property
    def allowed_hosts(self) -> tuple[str, ...]:
        return self._split_csv(self.web_allowed_hosts)

    @property
    def allowed_origins(self) -> tuple[str, ...]:
        return self._split_csv(self.web_allowed_origins)

    def validate_tool_paths(self) -> list[str]:
        """Return a list of diagnostic warnings. Empty list = all clear."""
        warnings: list[str] = []

        # Check paths that should exist
        path_checks: list[tuple[str, Path]] = [
            ("data_dir", self.data_dir),
            ("profiles_dir", self.profiles_dir),
            ("gemini_skill_dir", self.gemini_skill_dir),
            ("photo_process_dir", self.photo_process_dir),
            ("mpt_dir", self.mpt_dir),
            ("sau_dir", self.sau_dir),
            ("ai_art_bgm_dir", self.ai_art_bgm_dir),
            ("finance_md_dir", self.finance_md_dir),
            ("ai_briefing_dir", self.ai_briefing_dir),
        ]
        for name, path in path_checks:
            if not path.exists():
                warnings.append(f"[{name}] path does not exist: {path}")

        # Check executables
        exe_checks: list[tuple[str, Path]] = [
            ("photo_process_python", self.photo_process_python),
            ("mpt_python", self.mpt_python),
            ("sau_exe", self.sau_exe),
        ]
        for name, exe_path in exe_checks:
            if not exe_path.exists():
                warnings.append(f"[{name}] executable does not exist: {exe_path}")

        # Publish safety checks
        if not self.sau_bilibili_private_args:
            warnings.append("[sau_bilibili_private_args] not configured — Bilibili uploads will be blocked")

        return warnings

    def get_tool_status(self) -> dict[str, dict[str, object]]:
        """Return a structured status map for all external tools. Used by doctor."""
        tools: dict[str, dict[str, object]] = {}

        # Gemini
        gemini_ok = self.gemini_skill_dir.exists()
        tools["gemini"] = {
            "ok": gemini_ok,
            "dir": str(self.gemini_skill_dir),
            "mode": "mcp" if gemini_ok else ("cli" if self.gemini_image_command else "unavailable"),
        }

        # Photo-Process
        pp_ok = self.photo_process_dir.exists() and self.photo_process_python.exists()
        tools["photo_process"] = {
            "ok": pp_ok,
            "dir": str(self.photo_process_dir),
            "python": str(self.photo_process_python),
            "worker_url": self.photo_process_worker_url or None,
        }

        # MoneyPrinterTurbo
        mpt_ok = self.mpt_dir.exists() and self.mpt_python.exists()
        tools["mpt"] = {
            "ok": mpt_ok,
            "dir": str(self.mpt_dir),
            "python": str(self.mpt_python),
        }

        # social-auto-upload
        sau_ok = self.sau_dir.exists() and self.sau_exe.exists()
        tools["sau"] = {
            "ok": sau_ok,
            "dir": str(self.sau_dir),
            "exe": str(self.sau_exe),
            "bilibili_private_configured": bool(self.sau_bilibili_private_args),
        }

        return tools


def load_settings() -> Settings:
    return Settings()
