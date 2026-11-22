"""应用配置。

设计原则：
- 非敏感配置写入 ``%LOCALAPPDATA%\\WindowsHelperPlus\\config.json``，符合 Windows
  用户级应用的数据存放惯例；
- API Key 不写入该 JSON，而由 :mod:`helper_plus.secrets` 使用 Windows DPAPI 加密保存；
- 环境变量优先，方便临时覆盖和 CI。
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field, replace
import json
import os
from pathlib import Path
import sys
import tempfile
from typing import Any

from .i18n import tr


APP_DIR_NAME = "WindowsHelperPlus"


def _package_root() -> Path:
    """源码运行时是项目根；PyInstaller 运行时是解包目录（_MEIPASS）或 exe 目录。"""
    if getattr(sys, "frozen", False):
        base = getattr(sys, "_MEIPASS", None)
        if base:
            return Path(base)
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parents[1]


PACKAGE_ROOT = _package_root()
DEFAULT_LOCAL_SERVER = str(PACKAGE_ROOT / "runtime" / "llama-b9200" / "llama-server.exe")
DEFAULT_LOCAL_MODEL = str(PACKAGE_ROOT / "models" / "Qwen3-1.7B-Q8_0.gguf")
DEFAULT_BASE_URL = "https://api.deepseek.com"
DEFAULT_MODEL = "deepseek-flash"
DEFAULT_REASONING_EFFORT = "max"

MODEL_OPTIONS: tuple[tuple[str, str], ...] = (
    ("deepseek-flash", "DeepSeek V4.1 Flash（推荐）"),
    ("deepseek-v4-pro", "DeepSeek V4 Pro（更强推理）"),
)
REASONING_EFFORTS: tuple[str, ...] = ("low", "high", "max")

_ENV_KEYS = {
    "api_key": ("DEEPSEEK_API_KEY", "HELPER_PLUS_API_KEY"),
    "base_url": ("DEEPSEEK_BASE_URL", "HELPER_PLUS_BASE_URL"),
    "model": ("HELPER_PLUS_MODEL", "DEEPSEEK_MODEL"),
    "reasoning_effort": ("HELPER_PLUS_REASONING_EFFORT",),
    "ui_language": ("HELPER_PLUS_UI_LANGUAGE",),
}


def app_data_dir() -> Path:
    """返回可写的用户数据目录（可用 HELPER_PLUS_DATA_DIR 覆盖，测试友好）。"""
    override = os.environ.get("HELPER_PLUS_DATA_DIR", "").strip()
    if override:
        return Path(override).expanduser().resolve()
    base = os.environ.get("LOCALAPPDATA") or tempfile.gettempdir()
    return Path(base) / APP_DIR_NAME


def config_path() -> Path:
    return app_data_dir() / "config.json"


def reports_dir() -> Path:
    path = app_data_dir() / "reports"
    path.mkdir(parents=True, exist_ok=True)
    return path


def logs_dir() -> Path:
    path = app_data_dir() / "logs"
    path.mkdir(parents=True, exist_ok=True)
    return path


def default_export_dir() -> Path:
    documents = Path(os.environ.get("USERPROFILE", str(Path.home()))) / "Documents"
    path = documents / "Windows Helper Plus"
    path.mkdir(parents=True, exist_ok=True)
    return path


def env_api_key() -> str:
    for key in _ENV_KEYS["api_key"]:
        value = os.environ.get(key, "").strip()
        if value:
            return value
    return ""


def _env_override(field_name: str) -> str:
    for key in _ENV_KEYS.get(field_name, ()):
        value = os.environ.get(key, "").strip()
        if value:
            return value
    return ""


def _atomic_write_json(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + ".tmp")
    text = json.dumps(data, ensure_ascii=False, indent=2)
    temp.write_text(text, encoding="utf-8")
    os.replace(temp, path)


@dataclass
class AppConfig:
    """非敏感配置。API Key 永远不在此对象中持久化。"""

    base_url: str = DEFAULT_BASE_URL
    model: str = DEFAULT_MODEL
    reasoning_effort: str = DEFAULT_REASONING_EFFORT
    thinking: bool = True
    # 0 = 不设置应用层上限：不发送 max_tokens，由 DeepSeek 服务端/模型决定。
    max_tokens: int = 0
    timeout_seconds: int = 180
    # 0 = 不限制规划步数；UI 仍可随时停止。
    max_rounds: int = 0
    context_budget_chars: int = 700_000
    action_timeout_seconds: int = 3600
    event_hours: int = 72
    event_max: int = 25
    remember_api_key: bool = True
    # 本地 Qwen 上下文官（llama.cpp CPU 运行时）
    local_model_enabled: bool = True
    local_model_server_exe: str = DEFAULT_LOCAL_SERVER
    local_model_path: str = DEFAULT_LOCAL_MODEL
    local_model_port: int = 8124
    local_model_context: int = 8192
    local_model_threads: int = 6
    local_model_timeout_seconds: int = 300
    # trusted：不再用路径隐私边界拦截工具调用；高风险动作仍由用户确认/UAC。
    permission_mode: str = "trusted"
    # UI 语言：zh 中文 / en English；只影响界面文案，不翻译 AI 内容与日志。
    ui_language: str = "zh"
    schema_version: int = field(default=1, repr=False)

    def __post_init__(self) -> None:
        # 环境变量覆盖（不写回磁盘）。
        if value := _env_override("base_url"):
            self.base_url = value
        if value := _env_override("model"):
            self.model = value
        if value := _env_override("reasoning_effort"):
            self.reasoning_effort = value
        if value := _env_override("ui_language"):
            self.ui_language = value
        self.base_url = (self.base_url or DEFAULT_BASE_URL).strip().rstrip("/")
        self.model = (self.model or DEFAULT_MODEL).strip()
        self.reasoning_effort = (self.reasoning_effort or DEFAULT_REASONING_EFFORT).lower()
        self.normalize()
        self.validate()

    def normalize(self) -> None:
        try:
            tokens = int(self.max_tokens)
            self.max_tokens = 0 if tokens <= 0 else max(1024, min(tokens, 384_000))
        except (TypeError, ValueError):
            self.max_tokens = 0
        try:
            self.timeout_seconds = max(30, min(int(self.timeout_seconds), 900))
        except (TypeError, ValueError):
            self.timeout_seconds = 180
        try:
            rounds = int(self.max_rounds)
            self.max_rounds = 0 if rounds <= 0 else min(rounds, 10_000)
        except (TypeError, ValueError):
            self.max_rounds = 0
        try:
            self.context_budget_chars = max(50_000, min(int(self.context_budget_chars), 5_000_000))
        except (TypeError, ValueError):
            self.context_budget_chars = 700_000
        try:
            self.action_timeout_seconds = max(60, min(int(self.action_timeout_seconds), 86_400))
        except (TypeError, ValueError):
            self.action_timeout_seconds = 3600
        try:
            self.local_model_port = max(1024, min(int(self.local_model_port), 65535))
        except (TypeError, ValueError):
            self.local_model_port = 8124
        try:
            self.local_model_context = max(1024, min(int(self.local_model_context), 32768))
        except (TypeError, ValueError):
            self.local_model_context = 8192
        try:
            self.local_model_threads = max(1, min(int(self.local_model_threads), 32))
        except (TypeError, ValueError):
            self.local_model_threads = 6
        try:
            self.local_model_timeout_seconds = max(30, min(int(self.local_model_timeout_seconds), 900))
        except (TypeError, ValueError):
            self.local_model_timeout_seconds = 300
        self.permission_mode = (self.permission_mode or "trusted").strip().lower()
        if self.permission_mode not in {"trusted", "strict"}:
            self.permission_mode = "trusted"
        self.ui_language = (self.ui_language or "zh").strip().lower()
        if self.ui_language not in {"zh", "en"}:
            self.ui_language = "zh"
        try:
            self.event_hours = max(1, min(int(self.event_hours), 720))
        except (TypeError, ValueError):
            self.event_hours = 72
        try:
            self.event_max = max(1, min(int(self.event_max), 100))
        except (TypeError, ValueError):
            self.event_max = 25
        if self.reasoning_effort not in REASONING_EFFORTS:
            self.reasoning_effort = DEFAULT_REASONING_EFFORT

    def validate(self) -> None:
        if not (self.base_url.startswith("https://") or self.base_url.startswith("http://")):
            raise ValueError("Base URL 必须以 http:// 或 https:// 开头。")
        if not self.model:
            raise ValueError("模型名称不能为空。")
        if self.reasoning_effort not in REASONING_EFFORTS:
            raise ValueError("推理强度必须是 low、high 或 max。")

    @property
    def model_label(self) -> str:
        label = tr(dict(MODEL_OPTIONS).get(self.model, self.model))
        effort = {"low": "Low", "high": "High", "max": "Max"}.get(
            self.reasoning_effort, self.reasoning_effort
        )
        return f"{label} · {tr('思考')} {effort}"

    @classmethod
    def load(cls, path: Path | None = None) -> "AppConfig":
        target = path or config_path()
        data: dict[str, Any] = {}
        if target.exists():
            try:
                data = json.loads(target.read_text(encoding="utf-8"))
                if not isinstance(data, dict):
                    data = {}
            except (OSError, ValueError):
                data = {}
        known = {f for f in cls.__dataclass_fields__ if f != "schema_version"}
        values = {k: v for k, v in data.items() if k in known}
        try:
            return cls(**values)
        except (TypeError, ValueError):
            return cls()

    def save(self, path: Path | None = None) -> Path:
        self.normalize()
        self.validate()
        target = path or config_path()
        data = asdict(self)
        _atomic_write_json(target, data)
        return target

    def copy(self, **changes: Any) -> "AppConfig":
        return replace(self, **changes)


def config_summary(config: AppConfig) -> str:
    """用于状态栏/诊断信息的安全摘要（不含 Key）。"""
    return (
        f"model={config.model} effort={config.reasoning_effort} "
        f"thinking={'on' if config.thinking else 'off'} base={config.base_url}"
    )
