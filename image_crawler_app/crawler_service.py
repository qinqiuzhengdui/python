"""
图片爬取核心逻辑（业务层）。

工作流：
1. 用 Playwright 打开目标页面；
2. 自动滚动触发懒加载；
3. 提取 <img> / CSS background / data-bg 等图片地址；
4. 线程池并发下载到本地目录。

复用 page_image_crawler.py 中的提取逻辑，避免重复实现。
"""

import asyncio
import sys
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from urllib.parse import urlparse

import requests

# 保证从任意工作目录都能导入上级目录的 page_image_crawler.py
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from page_image_crawler import (
    HEADERS,
    extract_image_urls_from_page,
    normalize_urls,
    scroll_to_trigger_lazy_load,
)

# 并发下载线程数
DOWNLOAD_WORKERS = 8


def _guess_extension(content_type: str, url_path: str) -> str:
    """
    根据 Content-Type 或 URL 后缀推断文件扩展名。

    Args:
        content_type: 响应头 Content-Type
        url_path: URL 路径

    Returns:
        扩展名（含点，如 ".jpg"）
    """
    if "jpeg" in content_type or "jpg" in content_type or url_path.endswith(".jpg"):
        return ".jpg"
    if "png" in content_type or url_path.endswith(".png"):
        return ".png"
    if "webp" in content_type or url_path.endswith(".webp"):
        return ".webp"
    if "gif" in content_type or url_path.endswith(".gif"):
        return ".gif"
    if "avif" in content_type or url_path.endswith(".avif"):
        return ".avif"
    if "svg" in content_type or url_path.endswith(".svg"):
        return ".svg"
    return ".img"


def _safe_name(url_path: str) -> str:
    """
    从 URL 路径提取安全的文件名（去除非字母数字字符）。

    Args:
        url_path: URL 的 path 部分

    Returns:
        安全文件名（无扩展名）
    """
    base = urlparse(url_path).path.split("/")[-1]
    return "".join(c if c.isalnum() or c in "._-" else "_" for c in base)[:80]


def _download_one(url: str, dest_dir: Path, referer: str) -> dict:
    """
    下载单张图片到目录。

    Args:
        url: 图片 URL
        dest_dir: 保存目录
        referer: 请求来源页（部分 CDN 校验）

    Returns:
        结果字典 {ok, url, name, size, error}
    """
    try:
        resp = requests.get(url, headers={**HEADERS, "Referer": referer},
                            timeout=30, stream=True)
        resp.raise_for_status()
        ext = _guess_extension(resp.headers.get("Content-Type", ""),
                               urlparse(url).path)
        name = f"{uuid.uuid4().hex[:8]}_{_safe_name(urlparse(url).path)}{ext}"
        size = 0
        with open(dest_dir / name, "wb") as f:
            for chunk in resp.iter_content(chunk_size=8192):
                f.write(chunk)
                size += len(chunk)
        return {"ok": True, "url": url, "name": name, "size": size, "error": None}
    except Exception as e:  # noqa: BLE001 - 单张失败不影响整体
        return {"ok": False, "url": url, "name": None, "size": 0, "error": str(e)}


async def crawl_page_images(browser, url: str, max_images: int,
                            download_dir: Path) -> dict:
    """
    爬取页面的全部图片并下载。

    Args:
        browser: Playwright 浏览器实例（复用，避免重复启动）
        url: 目标网页
        max_images: 最多下载张数
        download_dir: 下载目录

    Returns:
        汇总字典 {page_url, total, images: [...]}
    """
    download_dir.mkdir(parents=True, exist_ok=True)
    context = await browser.new_context(
        user_agent=HEADERS["User-Agent"],
        viewport={"width": 1366, "height": 900},
        locale="zh-CN",
    )
    page = await context.new_page()
    try:
        await page.goto(url, wait_until="domcontentloaded", timeout=60000)
        try:
            await page.wait_for_load_state("networkidle", timeout=45000)
        except Exception:
            pass  # 网络空闲等不到就继续
        await scroll_to_trigger_lazy_load(page)
        raw_urls = await extract_image_urls_from_page(page)
        final_url = page.url
    finally:
        await context.close()

    urls = normalize_urls(final_url, raw_urls)[:max_images]

    tasks = [(u, download_dir, final_url) for u in urls]
    with ThreadPoolExecutor(max_workers=DOWNLOAD_WORKERS) as pool:
        results = list(pool.map(lambda t: _download_one(*t), tasks))

    images = [r for r in results if r["ok"]]
    return {
        "page_url": final_url,
        "total": len(results),
        "ok": len(images),
        "fail": len(results) - len(images),
        "images": images,
    }
