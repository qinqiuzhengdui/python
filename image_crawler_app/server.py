"""
图片爬取 + 一键生成 PPT 的 Web 服务（控制层）。

FastAPI 应用：
- POST /api/crawl  提交网址，爬取并下载图片，返回结果 JSON
- POST /api/ppt    基于已爬取图片一键生成可编辑 PPTX（dashi-ppt-skill）
- GET  /           静态前端页面
- /crawled/*       已下载图片的静态访问
- /_ppt/*          PPT 产物（pptx / 预览 html）的静态访问

启动:
    uvicorn server:app --host 0.0.0.0 --port 8000
"""

import asyncio
import uuid
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from crawler_service import crawl_page_images
from ppt_service import MAX_PPT_IMAGES, PptError, derive_default_title, generate_ppt

BASE_DIR = Path(__file__).resolve().parent
STATIC_DIR = BASE_DIR / "static"
DOWNLOAD_DIR = STATIC_DIR / "crawled"
PPT_DIR = STATIC_DIR / "_ppt"
MAX_IMAGES_LIMIT = 200  # 单次爬取图片上限，防止页面过大拖垮服务

# 可进入 PPT 的图片扩展名（与 ppt_service._MIME_BY_EXT 保持一致）
_PPT_IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp", ".gif", ".avif", ".svg"}


class CrawlRequest(BaseModel):
    """爬取请求体。"""

    url: str
    max_images: int = 50


class ImageItem(BaseModel):
    """单张图片结果。"""

    url: str
    name: str
    size: int


class CrawlResponse(BaseModel):
    """爬取响应体。"""

    page_url: str
    total: int
    ok: int
    fail: int
    images: list[ImageItem]


class PptRequest(BaseModel):
    """一键生成 PPT 请求体。"""

    title: str = ""  # 留空则按 page_url 推导
    goal: str = ""  # 核心目标文案，留空使用默认
    theme: str = "theme01"  # theme01~theme12
    page_url: str = ""  # 来源网页，用于推导默认标题
    max_images: int = MAX_PPT_IMAGES


class PptResponse(BaseModel):
    """PPT 生成响应体。"""

    deck: str  # 本次 deck 目录名
    pptx_url: str  # 可编辑 PPTX 下载地址
    html_url: str  # 在线预览地址
    page_count: int
    image_count: int
    message: str


@asynccontextmanager
async def lifespan(app: FastAPI):
    """启动/关闭时管理共享的 Playwright 浏览器实例。"""
    from playwright.async_api import async_playwright

    playwright = await async_playwright().start()
    app.state.browser = await playwright.chromium.launch(headless=True)
    # 清理上一次运行遗留的下载文件
    if DOWNLOAD_DIR.exists():
        for f in DOWNLOAD_DIR.glob("*"):
            f.unlink(missing_ok=True)
    yield
    await app.state.browser.close()
    await playwright.stop()


app = FastAPI(title="网页图片爬取工具", lifespan=lifespan)

# 本地工具，允许跨域便于调试
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.post("/api/crawl", response_model=CrawlResponse)
async def crawl(req: CrawlRequest) -> CrawlResponse:
    """
    爬取指定网址的图片并下载。

    Args:
        req: 爬取请求（url + max_images）

    Returns:
        图片列表及统计
    """
    if not req.url.startswith(("http://", "https://")):
        raise HTTPException(status_code=400, detail="请输入以 http:// 或 https:// 开头的网址")
    max_images = min(max(1, req.max_images), MAX_IMAGES_LIMIT)

    try:
        result = await crawl_page_images(
            app.state.browser, req.url, max_images, DOWNLOAD_DIR
        )
    except Exception as e:  # noqa: BLE001 - 统一转为 502 反馈给前端
        raise HTTPException(status_code=502, detail=f"爬取失败: {e}") from e

    return CrawlResponse(
        page_url=result["page_url"],
        total=result["total"],
        ok=result["ok"],
        fail=result["fail"],
        images=[ImageItem(**img) for img in result["images"]],
    )


@app.post("/api/ppt", response_model=PptResponse)
async def create_ppt(req: PptRequest) -> PptResponse:
    """
    一键生成 PPT：把当前已爬取的图片经 dashi-ppt-skill 渲染并导出可编辑 PPTX。

    Args:
        req: PPT 生成请求（标题/核心目标/主题/来源网页）

    Returns:
        PPT 产物下载与预览地址

    Raises:
        HTTPException(400): 尚未爬取任何可用图片
        HTTPException(502): 生成链路任一步失败
    """
    image_files = sorted(
        path
        for path in DOWNLOAD_DIR.iterdir()
        if path.is_file() and path.suffix.lower() in _PPT_IMAGE_EXTENSIONS
    )
    if not image_files:
        raise HTTPException(status_code=400, detail="请先在当前服务中完成图片爬取")

    title = req.title.strip() or derive_default_title(req.page_url)
    goal = req.goal.strip() or f"{title} 精选图片图集"
    theme = req.theme or "theme01"
    images = image_files[: min(max(1, req.max_images), MAX_PPT_IMAGES)]

    deck_name = f"deck-{datetime.now():%Y%m%d-%H%M%S}-{uuid.uuid4().hex[:4]}"
    deck_dir = PPT_DIR / deck_name

    # 生成链路为阻塞子进程（Node 渲染 + 浏览器导出），放入线程池避免阻塞事件循环
    try:
        result = await asyncio.to_thread(
            generate_ppt, deck_dir, title, goal, theme, images
        )
    except PptError as exc:
        raise HTTPException(status_code=502, detail=f"PPT 生成失败: {exc}") from exc

    return PptResponse(
        deck=deck_name,
        pptx_url=f"/_ppt/{deck_name}/{deck_name}.pptx",
        html_url=f"/_ppt/{deck_name}/ppt/index.html",
        page_count=result["page_count"],
        image_count=result["image_count"],
        message="PPT 已生成",
    )


# 静态资源（前端页面 + 已下载图片 + PPT 产物）
app.mount("/", StaticFiles(directory=STATIC_DIR, html=True), name="static")
