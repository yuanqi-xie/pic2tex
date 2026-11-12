"""Configuration handling for Pic2TeX.

The config lives in ``%APPDATA%\\Pic2TeX\\config.json``.  Secrets can also be
supplied through environment variables, which take precedence:

* ``DEEPSEEK_API_KEY``   -- API key
* ``DEEPSEEK_BASE_URL``  -- API base URL
* ``DEEPSEEK_MODEL`` / ``PIC2TEX_MODEL`` -- model id
"""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import asdict, dataclass, fields
from pathlib import Path
from typing import Any

APP_NAME = "Pic2TeX"
DEFAULT_BASE_URL = "https://api.deepseek.com"
DEFAULT_MODEL = "deepseek-flash"
DEFAULT_HOTKEY = "ctrl+alt+s"

PROMPT_STYLES = ("math", "document")
WRAPPERS = ("display", "inline", "none")
DETAILS = ("original", "high", "low", "auto")


def app_data_dir() -> Path:
    """Return the per-user data directory (created on demand by callers)."""
    base = os.environ.get("APPDATA") or str(Path.home())
    return Path(base) / APP_NAME


def default_config_path() -> Path:
    return app_data_dir() / "config.json"


@dataclass
class Config:
    """User-adjustable settings.  All fields are JSON serialisable."""

    api_key: str = ""
    base_url: str = DEFAULT_BASE_URL
    model: str = DEFAULT_MODEL
    prompt_style: str = "math"  # math | document
    wrapper: str = "display"  # display | inline | none
    auto_copy: bool = True
    auto_monitor: bool = True
    start_with_current: bool = False
    hotkey_enabled: bool = True
    hotkey: str = DEFAULT_HOTKEY
    timeout: float = 180.0
    max_tokens: int = 8192
    temperature: float = 0.0
    detail: str = "original"
    stream: bool = True
    save_dir: str = ""
    history_limit: int = 30

    # -- effective values (environment overrides) -------------------------
    @property
    def effective_api_key(self) -> str:
        return (os.environ.get("DEEPSEEK_API_KEY") or self.api_key).strip()

    @property
    def effective_base_url(self) -> str:
        url = (os.environ.get("DEEPSEEK_BASE_URL") or self.base_url or DEFAULT_BASE_URL).strip()
        return url.rstrip("/")

    @property
    def effective_model(self) -> str:
        model = (
            os.environ.get("PIC2TEX_MODEL")
            or os.environ.get("DEEPSEEK_MODEL")
            or self.model
            or DEFAULT_MODEL
        )
        return model.strip()

    @property
    def api_key_source(self) -> str:
        if (os.environ.get("DEEPSEEK_API_KEY") or "").strip():
            return "环境变量"
        if self.api_key.strip():
            return "配置文件"
        return "未配置"

    def masked_key(self) -> str:
        key = self.effective_api_key
        if not key:
            return "未配置"
        if len(key) <= 8:
            return "****"
        return f"{key[:4]}…{key[-4:]}"

    def resolved_save_dir(self) -> Path:
        raw = self.save_dir.strip()
        path = Path(raw).expanduser() if raw else Path.home() / "Documents" / APP_NAME
        return path

    # -- validation -------------------------------------------------------
    def validate(self) -> list[str]:
        issues: list[str] = []
        if not self.effective_base_url.startswith(("http://", "https://")):
            issues.append("API 地址必须以 http:// 或 https:// 开头")
        if self.prompt_style not in PROMPT_STYLES:
            issues.append(f"未知的排版风格: {self.prompt_style!r}")
        if self.wrapper not in WRAPPERS:
            issues.append(f"未知的公式包裹方式: {self.wrapper!r}")
        if self.detail not in DETAILS:
            issues.append(f"未知的图片精度: {self.detail!r}")
        try:
            if not 5 <= float(self.timeout) <= 600:
                issues.append("请求超时应在 5–600 秒之间")
        except (TypeError, ValueError):
            issues.append("请求超时必须是数字")
        try:
            if not 256 <= int(self.max_tokens) <= 65536:
                issues.append("最大输出 token 应在 256–65536 之间")
        except (TypeError, ValueError):
            issues.append("最大输出 token 必须是整数")
        try:
            if not 0.0 <= float(self.temperature) <= 2.0:
                issues.append("temperature 应在 0–2 之间")
        except (TypeError, ValueError):
            issues.append("temperature 必须是数字")
        return issues

    # -- persistence ------------------------------------------------------
    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Config":
        known = {f.name for f in fields(cls)}
        clean: dict[str, Any] = {}
        for key, value in (data or {}).items():
            if key not in known:
                continue
            if key in {"timeout", "temperature"}:
                try:
                    value = float(value)
                except (TypeError, ValueError):
                    continue
            elif key == "max_tokens":
                try:
                    value = int(value)
                except (TypeError, ValueError):
                    continue
            elif key in {
                "auto_copy",
                "auto_monitor",
                "start_with_current",
                "hotkey_enabled",
                "stream",
            }:
                value = bool(value)
            elif value is None:
                continue
            clean[key] = value
        return cls(**clean)

    @classmethod
    def load(cls, path: str | os.PathLike | None = None) -> "Config":
        """Load config, falling back to defaults when the file is missing/corrupt."""
        target = Path(path) if path else default_config_path()
        try:
            raw = target.read_text(encoding="utf-8")
            data = json.loads(raw)
            if not isinstance(data, dict):
                raise ValueError("config root must be an object")
            return cls.from_dict(data)
        except FileNotFoundError:
            return cls()
        except (OSError, ValueError, json.JSONDecodeError):
            return cls()

    def save(self, path: str | os.PathLike | None = None) -> Path:
        """Persist atomically (write temp file then replace)."""
        target = Path(path) if path else default_config_path()
        target.parent.mkdir(parents=True, exist_ok=True)
        payload = json.dumps(self.to_dict(), ensure_ascii=False, indent=2) + "\n"
        fd, tmp_name = tempfile.mkstemp(prefix=".config-", suffix=".json", dir=str(target.parent))
        try:
            with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
                handle.write(payload)
            os.replace(tmp_name, target)
        finally:
            if os.path.exists(tmp_name):
                try:
                    os.remove(tmp_name)
                except OSError:
                    pass
        return target
