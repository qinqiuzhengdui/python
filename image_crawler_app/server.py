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
import json
import uuid
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

import ai_pipeline
from config import settings
from content_pack import build_ai_content_pack
from crawler_service import crawl_page_images
from kimi_client import KimiClient, KimiError
from ppt_service import MAX_PPT_IMAGES, PptError, derive_default_title, generate_ppt

BASE_DIR = Path(__file__).resolve().parent
STATIC_DIR = BASE_DIR / "static"
DOWNLOAD_DIR = STATIC_DIR / "crawled"
PPT_DIR = STATIC_DIR / "_ppt"
MAX_IMAGES_LIMIT = 200  # 单次爬取图片上限，防止页面过大拖垮服务

# 可进入 PPT 的图片扩展名（与 ppt_service._MIME_BY_EXT 保持一致）
_PPT_IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp", ".gif", ".avif", ".svg"}

# Kimi 筛选结果缓存（随 static/crawled 一起在重启时清空，无需持久化）
_AI_CACHE_FILE = DOWNLOAD_DIR / ".ai-cache.json"


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
    use_ai: bool = False  # 是否启用 Kimi 筛图 + AI 文案
    selected: list[str] = []  # Kimi 入选文件名（/api/ai/select-images 返回的 kept 名单）


class PptResponse(BaseModel):
    """PPT 生成响应体。"""

    deck: str  # 本次 deck 目录名
    pptx_url: str  # 可编辑 PPTX 下载地址
    html_url: str  # 在线预览地址
    page_count: int
    image_count: int
    ai: bool  # 本份 PPT 的筛选/文案是否由 Kimi 完成
    message: str


class AiStatusResponse(BaseModel):
    """Kimi 能力状态（供前端决定 AI 开关默认态）。"""

    enabled: bool
    vision_model: str
    text_model: str
    filter_mode: str
    hint: str  # 未配置 Key 时的引导文案


class SelectImagesRequest(BaseModel):
    """Kimi 视觉筛图请求体。"""

    title: str = ""
    goal: str = ""


class AssessmentItem(BaseModel):
    """单张图片的筛选评估。"""

    name: str
    keep: bool
    score: int
    category: str
    reason: str
    description: str


class SelectImagesResponse(BaseModel):
    """筛图结果：kept 按分数降序，dropped 保持爬取原序。"""

    total: int
    kept: list[AssessmentItem]
    dropped: list[AssessmentItem]
    vision_model: str


@asynccontextmanager
async def lifespan(app: FastAPI):
    """启动/关闭时管理共享的 Playwright 浏览器实例。"""
    from playwright.async_api import async_playwright

    playwright = await async_playwright().start()
    app.state.browser = await playwright.chromium.launch(headless=True)
    # Kimi 客户端：Key 未配置时为 None，AI 端点返回明确引导，不阻断服务启动
    app.state.kimi = KimiClient.from_settings() if settings.kimi_enabled else None
    # 清理上一次运行遗留的下载文件
    if DOWNLOAD_DIR.exists():
        for f in DOWNLOAD_DIR.glob("*"):
            f.unlink(missing_ok=True)
    yield
    await app.state.browser.close()
    if app.state.kimi is not None:
        await app.state.kimi.aclose()
    await playwright.stop()


app = FastAPI(title="网页图片爬取工具", lifespan=lifespan)

# 本地工具，允许跨域便于调试
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# 前端文档与脚本/样式的后缀：这些文件更新后必须立即对浏览器生效
_FRONTEND_REVALIDATE_SUFFIXES = (".html", ".js", ".css")


@app.middleware("http")
async def revalidate_frontend_assets(request, call_next):
    """强制前端入口资源在使用前进行缓存协商。

    Why: StaticFiles 默认只返回 Last-Modified/ETag，不返回 Cache-Control，
    浏览器会据此进行"启发式缓存"——可能完全不发请求直接沿用旧副本，
    导致 JS/CSS 修复（典型现象：加载提示完成后不消失）对用户不生效。
    指定 no-cache 后浏览器仍会携带 If-None-Match 协商，文件未变更时
    服务端返回 304，额外开销仅一次轻量请求。图片、PPTX 等大文件不受影响。

    Args:
        request: 传入的 Starlette 请求对象
        call_next: 下一处理层（含静态文件挂载）的调用回调

    Returns:
        对 HTML/JS/CSS 附加 no-cache 响应头后的响应对象
    """
    response = await call_next(request)
    path = request.url.path
    if path == "/" or path.endswith(_FRONTEND_REVALIDATE_SUFFIXES):
        response.headers["Cache-Control"] = "no-cache, must-revalidate"
    return response


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


def _list_crawled_images() -> list[Path]:
    """列出当前已爬取、且格式可入册 PPT 的图片（按文件名排序保证顺序稳定）。"""
    if not DOWNLOAD_DIR.exists():
        return []
    return sorted(
        path
        for path in DOWNLOAD_DIR.iterdir()
        if path.is_file() and path.suffix.lower() in _PPT_IMAGE_EXTENSIONS
    )


def _read_ai_cache() -> dict[str, dict]:
    """
    读取 Kimi 筛选缓存，返回 {文件名: assessment dict}。

    缓存损坏时按空缓存处理——筛图是可重跑的低成本操作，不应因缓存文件报错。
    """
    if not _AI_CACHE_FILE.is_file():
        return {}
    try:
        payload = json.loads(_AI_CACHE_FILE.read_text(encoding="utf-8"))
        items = payload.get("items") if isinstance(payload, dict) else None
        return {item["name"]: item for item in items if isinstance(item, dict) and "name" in item}
    except (json.JSONDecodeError, OSError):
        return {}


def _write_ai_cache(items: list[dict]) -> None:
    """
    把筛选结果整体写入缓存（覆盖旧缓存，与当前 crawled 目录一一对应）。

    Args:
        items: assessment 字典列表（统一为 dict，避免调用点混入领域对象）
    """
    DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)
    payload = {
        "updated_at": datetime.now().isoformat(timespec="seconds"),
        "vision_model": settings.kimi_vision_model,
        "items": items,
    }
    _AI_CACHE_FILE.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def _require_kimi(app) -> KimiClient:
    """取进程级 Kimi 客户端，未配置 Key 时给出可操作的 400 引导。"""
    client = app.state.kimi
    if client is None:
        raise HTTPException(status_code=400, detail=settings.missing_key_hint)
    return client


@app.get("/api/ai/status", response_model=AiStatusResponse)
async def ai_status() -> AiStatusResponse:
    """查询 Kimi AI 能力是否可用及模型配置（前端据此设置开关默认态）。"""
    return AiStatusResponse(
        enabled=settings.kimi_enabled,
        vision_model=settings.kimi_vision_model,
        text_model=settings.kimi_text_model,
        filter_mode=settings.filter_mode,
        hint="" if settings.kimi_enabled else settings.missing_key_hint,
    )


@app.post("/api/ai/select-images", response_model=SelectImagesResponse)
async def select_images(req: SelectImagesRequest) -> SelectImagesResponse:
    """
    阶段一：Kimi 视觉模型批量评估已爬取图片并给出入册名单。

    Args:
        req: 可选的标题/目标，帮助模型理解页面语境

    Returns:
        kept（按分数降序）/ dropped（含原因与画面描述）

    Raises:
        HTTPException(400): 未爬取图片或未配置 Key
        HTTPException(502): Kimi 调用失败
    """
    client = _require_kimi(app)
    image_files = _list_crawled_images()
    if not image_files:
        raise HTTPException(status_code=400, detail="请先在当前服务中完成图片爬取")

    title = req.title.strip() or "网页图片图集"
    goal = req.goal.strip()
    try:
        assessed = await ai_pipeline.assess_images(client, image_files, title, goal)
        kept, dropped = ai_pipeline.choose_kept(assessed)
    except KimiError as exc:
        raise HTTPException(status_code=502, detail=f"Kimi 筛图失败: {exc}") from exc

    _write_ai_cache([item.to_dict() for item in assessed])
    return SelectImagesResponse(
        total=len(assessed),
        kept=[AssessmentItem(**item.to_dict()) for item in kept],
        dropped=[AssessmentItem(**item.to_dict()) for item in dropped],
        vision_model=settings.kimi_vision_model,
    )


@app.post("/api/ppt", response_model=PptResponse)
async def create_ppt(req: PptRequest) -> PptResponse:
    """
    一键生成 PPT：把当前已爬取的图片经 dashi-ppt-skill 渲染并导出可编辑 PPTX。

    两条路径：
    - use_ai=false（默认）：全部图片 + 确定性模板文案，完全本地；
    - use_ai=true：以 /api/ai/select-images 的 selected 名单为准（未传名单则
      现场筛图），再由 Kimi 文本模型生成每页文案；文案失败自动回退模板，不阻断导出。

    Args:
        req: PPT 生成请求（标题/核心目标/主题/来源网页/AI 开关/入选名单）

    Returns:
        PPT 产物下载与预览地址

    Raises:
        HTTPException(400): 尚未爬取图片 / 名单非法 / 未配置 Key
        HTTPException(502): Kimi 或本地生成链路失败
    """
    image_files = _list_crawled_images()
    if not image_files:
        raise HTTPException(status_code=400, detail="请先在当前服务中完成图片爬取")

    title = req.title.strip() or derive_default_title(req.page_url)
    goal = req.goal.strip() or f"{title} 精选图片图集"
    theme = req.theme or "theme01"
    cap = min(max(1, req.max_images), MAX_PPT_IMAGES)

    content_pack = None
    image_notes: list[str] | None = None
    ai_copy: dict | None = None
    ai_used = False

    if req.use_ai:
        client = _require_kimi(app)
        chosen, assessments = await _resolve_ai_selection(client, req, title, goal, image_files)
        images = chosen[:cap]
        selected_assessments = assessments[:cap]
        image_notes = [item.description for item in selected_assessments]

        # Kimi 文案（纯文本调用，不阻塞工作线程）；失败返回 None，generate_ppt 自动回退模板
        try:
            ai_copy = await ai_pipeline.generate_page_copy(
                client, title, goal, selected_assessments
            )
        except KimiError as exc:
            raise HTTPException(status_code=502, detail=f"Kimi 文案生成失败: {exc}") from exc
        ai_used = True
    else:
        images = image_files[:cap]

    deck_name = f"deck-{datetime.now():%Y%m%d-%H%M%S}-{uuid.uuid4().hex[:4]}"
    deck_dir = PPT_DIR / deck_name

    # 生成链路为阻塞子进程（Node 渲染 + 浏览器导出），放入线程池避免阻塞事件循环。
    # ai_copy 在 to_thread 内 stage 完成后才与真实 media 合并（见 ppt_service）。
    try:
        result = await asyncio.to_thread(
            generate_ppt, deck_dir, title, goal, theme, images,
            content_pack, image_notes, ai_copy,
        )
    except PptError as exc:
        raise HTTPException(status_code=502, detail=f"PPT 生成失败: {exc}") from exc

    return PptResponse(
        deck=deck_name,
        pptx_url=f"/_ppt/{deck_name}/{deck_name}.pptx",
        html_url=f"/_ppt/{deck_name}/ppt/index.html",
        page_count=result["page_count"],
        image_count=result["image_count"],
        ai=ai_used,
        message="PPT 已由 Kimi 辅助生成" if ai_used else "PPT 已生成",
    )


async def _resolve_ai_selection(
    client: KimiClient,
    req: PptRequest,
    title: str,
    goal: str,
    image_files: list[Path],
) -> tuple[list[Path], list[ai_pipeline.ImageAssessment]]:
    """
    确定 AI 路径的最终入册图片及其评估信息（描述用于文案与 alt）。

    两种来源：
    - 前端带 selected 名单（正常交互）：以名单顺序为准，评估信息读筛选缓存；
      缓存缺失的图片现场补评，不重新排序/淘汰。
    - 直接调 API 未带名单：现场对全量图片筛图，按分数取入选名单。

    Returns:
        (入选图片路径列表（即页面顺序）, 同序评估列表)
    """
    cache = _read_ai_cache()

    if req.selected:
        # 防路径穿越：只接受纯文件名，且必须存在于 crawled 目录
        by_name = {path.name: path for path in image_files}
        unknown = [name for name in req.selected if Path(name).name != name or name not in by_name]
        if unknown:
            raise HTTPException(status_code=400, detail=f"入选名单含无效图片: {unknown[:3]}")
        ordered_names = list(dict.fromkeys(req.selected))  # 去重保序
        missing = [name for name in ordered_names if name not in cache]
        if missing:
            assessed = await ai_pipeline.assess_images(
                client, [by_name[name] for name in missing], title, goal
            )
            cache.update({item.name: item.to_dict() for item in assessed})
            _write_ai_cache(list(cache.values()))
        chosen = [by_name[name] for name in ordered_names]
        assessments = [_assessment_from_cache(cache[name]) for name in ordered_names]
        return chosen, assessments

    try:
        assessed = await ai_pipeline.assess_images(client, image_files, title, goal)
    except KimiError as exc:
        raise HTTPException(status_code=502, detail=f"Kimi 筛图失败: {exc}") from exc
    kept, _dropped = ai_pipeline.choose_kept(assessed)
    _write_ai_cache([item.to_dict() for item in assessed])
    if not kept:
        raise HTTPException(status_code=400, detail="Kimi 判定当前图片均不适合入册，可关闭 AI 后重试")
    return [DOWNLOAD_DIR / item.name for item in kept], kept


def _assessment_from_cache(raw: dict) -> ai_pipeline.ImageAssessment:
    """缓存字典 → ImageAssessment（字段缺失时给保守默认，保证旧缓存可读）。"""
    return ai_pipeline.ImageAssessment(
        name=raw.get("name", ""),
        keep=bool(raw.get("keep", True)),
        score=int(raw.get("score", 60)),
        category=str(raw.get("category", "other")),
        reason=str(raw.get("reason", "")),
        description=str(raw.get("description", "")),
    )


# 静态资源（前端页面 + 已下载图片 + PPT 产物）
app.mount("/", StaticFiles(directory=STATIC_DIR, html=True), name="static")
