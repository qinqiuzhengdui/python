"""
集中配置层：环境变量 + 本地 .env 文件读取。

所有可调参数（API 地址、模型名、批大小、超时、筛选策略等）统一在此维护，
业务模块只允许读取本模块的常量/单例，禁止散落硬编码。

.env 文件位置：与本文件同目录（image_crawler_app/.env），格式为标准 KEY=VALUE，
以 # 开头的行视为注释。.env 仅用于本机开发，不应提交到代码仓库。

@example
    from config import settings

    if not settings.kimi_enabled:
        raise RuntimeError(settings.missing_key_hint)
    print(settings.kimi_vision_model, settings.kimi_text_model)
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
ENV_FILE = BASE_DIR / ".env"


def _load_dotenv(path: Path) -> None:
    """
    解析极简 .env 文件并注入 os.environ（不覆盖已存在的真实环境变量）。

    Why: 避免引入 python-dotenv 依赖；本项目只需要 KEY=VALUE 这一最小能力。

    Args:
        path: .env 文件路径，不存在时静默跳过
    """
    if not path.is_file():
        return
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


_load_dotenv(ENV_FILE)


def _env_int(key: str, default: int) -> int:
    """读取整数环境变量，非法值回退默认。"""
    try:
        return int(os.getenv(key, str(default)))
    except ValueError:
        return default


def _env_float(key: str, default: float) -> float:
    """读取浮点环境变量，非法值回退默认。"""
    try:
        return float(os.getenv(key, str(default)))
    except ValueError:
        return default


@dataclass(frozen=True)
class Settings:
    """全局运行配置（进程内只读单例）。"""

    # ---- Kimi / Moonshot 开放平台 ----
    kimi_api_base: str = os.getenv("KIMI_API_BASE", "https://api.moonshot.cn/v1")
    kimi_api_key: str = os.getenv("MOONSHOT_API_KEY", "").strip()
    # 筛图（视觉理解）模型：kimi-k2.6 支持关闭思考，批量看图更快更省
    kimi_vision_model: str = os.getenv("KIMI_VISION_MODEL", "kimi-k2.6").strip()
    # 文案生成模型：kimi-k3 旗舰，仅一次文本调用，质量优先
    kimi_text_model: str = os.getenv("KIMI_TEXT_MODEL", "kimi-k3").strip()
    kimi_timeout_seconds: float = _env_float("KIMI_TIMEOUT_SECONDS", 180.0)
    kimi_max_retries: int = _env_int("KIMI_MAX_RETRIES", 2)
    # k3 等仅思考模型的推理强度：low/high/max，筛图与文案属结构化任务，low 足够且更快
    kimi_reasoning_effort: str = os.getenv("KIMI_REASONING_EFFORT", "low").strip().lower()
    # 单次响应 token 上限（结构化 JSON 输出，4096 足够每页文案/每批筛图结果）
    kimi_max_tokens: int = _env_int("KIMI_MAX_TOKENS", 4096)

    # ---- 视觉筛图 ----
    # 每次请求携带的图片数：多图对话控制 token 总量，过大会超时/超限
    vision_batch_size: int = _env_int("KIMI_VISION_BATCH", 6)
    # 入册图片上限（与 dashi 渲染规模共同决定，页数 = 图片数 + 2）
    select_max_images: int = _env_int("KIMI_SELECT_MAX", 30)
    # lenient=尽量多保留（仅剔除损坏/极小/图标二维码等）；strict=质量优先
    filter_mode: str = os.getenv("KIMI_FILTER_MODE", "lenient").strip().lower()
    # lenient 策略下的保留阈值：低于该分才丢弃（满分 100）
    lenient_min_score: int = _env_int("KIMI_LENIENT_MIN_SCORE", 45)

    # ---- 传给视觉模型的缩略图规格（控制 base64 体积与 token 成本） ----
    vision_thumb_max_side: int = _env_int("KIMI_THUMB_MAX_SIDE", 1024)
    vision_thumb_quality: int = _env_int("KIMI_THUMB_QUALITY", 82)

    @property
    def kimi_enabled(self) -> bool:
        """是否已配置可用的 API Key。"""
        return bool(self.kimi_api_key)

    @property
    def missing_key_hint(self) -> str:
        """未配置 Key 时面向用户/前端的引导文案。"""
        return (
            "未配置 MOONSHOT_API_KEY：请在 "
            f"{ENV_FILE} 中写入 MOONSHOT_API_KEY=sk-xxxx 后重启服务"
        )


settings = Settings()
