"""
图片工具层：把本地已下载图片编码为视觉模型可接收的 base64 data URL。

Why 缩略图：直接发送原图会浪费 token（Kimi 按视觉 token 计费）且容易超时；
统一压缩到最长边 1024、JPEG 质量 82，对"筛图"这种判断类任务画质足够。
SVG 等 Pillow 无法光栅化的格式按原字节兜底（交给模型/API 自行处理或跳过）。

@example
    from image_utils import encode_image_data_url
    data_url = encode_image_data_url(Path("static/crawled/a.jpg"))
    # -> "data:image/jpeg;base64,/9j/4AAQSkZJRg..."
"""

from __future__ import annotations

import base64
import io
from pathlib import Path

from PIL import Image, UnidentifiedImageError

from config import settings

# 扩展名 -> data URL 使用的 MIME（与 ppt_service 的入册类型保持一致）
_MIME_BY_EXT = {
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png": "image/png",
    ".webp": "image/webp",
    ".gif": "image/gif",
    ".avif": "image/avif",
    ".svg": "image/svg+xml",
}

# 缩略图统一输出 JPEG（带透明通道的图先铺白底，避免黑底）
_FALLBACK_MIME = "image/jpeg"


def encode_image_data_url(path: Path) -> str:
    """
    将本地图片压缩编码为 base64 data URL。

    Args:
        path: 本地图片绝对路径

    Returns:
        形如 ``data:image/jpeg;base64,...`` 的数据 URL

    Raises:
        FileNotFoundError: 文件不存在
        OSError: 文件不可读（兜底分支也失败时）
    """
    if not path.is_file():
        raise FileNotFoundError(f"图片不存在: {path}")

    try:
        return _encode_thumbnail(path)
    except (UnidentifiedImageError, OSError, ValueError):
        # SVG、损坏文件、缺少对应解码器（如部分 AVIF）等情况：原样发送
        return _encode_raw(path)


def _encode_thumbnail(path: Path) -> str:
    """用 Pillow 缩放并转码为 JPEG data URL。"""
    with Image.open(path) as img:
        # 统一经 RGBA 合成白底：透明 PNG/ GIF 帧直接转 RGB 会丢失透明通道变成黑底
        rgba = img.convert("RGBA")
        background = Image.new("RGBA", rgba.size, (255, 255, 255, 255))
        background.alpha_composite(rgba)
        rgb = background.convert("RGB")
        # 只缩不放：小图保持原尺寸，避免无意义放大增加体积
        rgb.thumbnail((settings.vision_thumb_max_side, settings.vision_thumb_max_side))
        buffer = io.BytesIO()
        rgb.save(buffer, format="JPEG", quality=settings.vision_thumb_quality, optimize=True)
        payload = base64.b64encode(buffer.getvalue()).decode("ascii")
    return f"data:{_FALLBACK_MIME};base64,{payload}"


def _encode_raw(path: Path) -> str:
    """不做转码，按原始字节与 MIME 编码（Pillow 无法处理的格式兜底）。"""
    mime = _MIME_BY_EXT.get(path.suffix.lower(), "application/octet-stream")
    payload = base64.b64encode(path.read_bytes()).decode("ascii")
    return f"data:{mime};base64,{payload}"
