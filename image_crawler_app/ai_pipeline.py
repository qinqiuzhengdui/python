"""
Kimi 驱动的 AI 管线：图片筛选 → 文案生成。

与其他模块的分工：
- kimi_client.py：只做 HTTP；本模块负责"问什么、怎么解析、怎么挑选、失败如何兜底"；
- content_pack.py：把文案合并成 dashi 内容包；本模块只产出中间文案结构；
- ppt_service.py：本地脚手架/渲染/导出，与本模块互不依赖（由 server.py 编排）。

流程：
    assessments = await assess_images(client, paths, title, goal)   # kimi-k2.6 视觉
    kept, dropped = choose_kept(assessments)
    copy = await generate_page_copy(client, model, title, goal, kept)  # kimi-k3 文本

@example
    async with KimiClient.from_settings() as client:
        assessed = await assess_images(client, paths, "NOWRE", "街头潮流")
        kept, dropped = choose_kept(assessed)
        copy = await generate_page_copy(client, "kimi-k3", "NOWRE", "街头潮流", kept)
"""

from __future__ import annotations

import json
import logging
from dataclasses import asdict, dataclass
from pathlib import Path

from config import settings
from image_utils import encode_image_data_url
from kimi_client import KimiClient, KimiError, extract_json_block

logger = logging.getLogger("ai_pipeline")

# 模型分类 → 无论模型如何打 keep 都强制剔除的类别（图标/二维码/破损图没有入册价值）
_HARD_DROP_CATEGORIES = {"icon", "logo", "qrcode", "broken"}
_VALID_CATEGORIES = {
    "photo", "illustration", "poster", "screenshot",
    "logo", "icon", "banner", "qrcode", "duplicate", "broken", "other",
}


@dataclass
class ImageAssessment:
    """单张图片的 Kimi 评估结果。"""

    name: str
    keep: bool
    score: int                 # 0-100，综合清晰度与内容价值
    category: str              # photo/illustration/poster/...
    reason: str                # 保留/剔除的简短依据（面向用户展示）
    description: str           # 15-30 字客观画面描述，供文案阶段使用

    def to_dict(self) -> dict:
        """序列化为可缓存/可返回 JSON 的字典。"""
        return asdict(self)


# ---------------------------------------------------------------------------
# 阶段一：Kimi 视觉筛图
# ---------------------------------------------------------------------------

async def assess_images(
    client: KimiClient,
    image_paths: list[Path],
    title: str,
    goal: str,
    *,
    batch_size: int | None = None,
    filter_mode: str | None = None,
) -> list[ImageAssessment]:
    """
    分批调用视觉模型评估全部图片，返回与入参同序的评估列表。

    Args:
        client: 已初始化的 KimiClient
        image_paths: 已下载图片的绝对路径（同序结果）
        title: 图集标题（帮助模型理解页面语境）
        goal: 核心目标文案（可为空）
        batch_size: 每请求图片数，默认取 settings.vision_batch_size
        filter_mode: lenient/strict，默认取 settings.filter_mode

    Returns:
        ImageAssessment 列表（顺序与 image_paths 完全一致）

    Raises:
        KimiError: 所有批次均失败（单张/单批异常已做宽容兜底）
    """
    batch_size = batch_size or settings.vision_batch_size
    filter_mode = (filter_mode or settings.filter_mode).lower()
    if not image_paths:
        return []

    assessments: list[ImageAssessment | None] = [None] * len(image_paths)
    for start in range(0, len(image_paths), batch_size):
        batch = image_paths[start:start + batch_size]
        data_urls = [encode_image_data_url(path) for path in batch]
        prompt = _build_assess_prompt(len(batch), title, goal, filter_mode, start)
        rows = await _chat_assess_json(client, data_urls, prompt, retry=1)

        for local_index, raw in enumerate(rows):
            global_index = start + local_index
            assessments[global_index] = _coerce_assessment(
                image_paths[global_index].name, raw, local_index
            )

    # 理论上不会触发（每槽位均有 coerce 兜底），最后一道防线保证同序非空
    return [
        item or ImageAssessment(path.name, True, 60, "other", "评估缺失，保守保留", "")
        for item, path in zip(assessments, image_paths)
    ]


async def _chat_assess_json(client: KimiClient, data_urls: list[str], prompt: str, *, retry: int) -> list[dict]:
    """
    发起视觉评估请求并解析为 JSON 数组。

    Why 解析失败再给一次机会：模型偶发输出解释性文字导致首轮 JSON 不完整，
    追加"只输出 JSON"的纠错轮比直接判死整批更稳；两次都失败则抛出由上层兜底。
    """
    last_error: Exception | None = None
    for attempt in range(retry + 1):
        reply = await client.chat_vision(
            data_urls,
            prompt + ("\n再次提醒：只输出 JSON 数组本身，禁止任何解释或 markdown 代码块。" if attempt else ""),
            settings.kimi_vision_model,
        )
        try:
            parsed = extract_json_block(reply)
            if isinstance(parsed, list):
                return [row for row in parsed if isinstance(row, dict)]
            last_error = KimiError("视觉评估返回的不是 JSON 数组")
        except KimiError as exc:
            last_error = exc
    raise KimiError(f"视觉评估结果连续解析失败: {last_error}")


def _build_assess_prompt(batch_count: int, title: str, goal: str, filter_mode: str, offset: int) -> str:
    """
    构造单批评估提示词（图号使用全局序号，避免多批结果无法对齐）。

    Args:
        batch_count: 本批图片数
        title: 图集标题
        goal: 核心目标
        filter_mode: lenient=尽量多保留 / strict=质量优先
        offset: 本批在全量图片中的起始下标（图号 = offset + 局部序号 + 1）
    """
    policy = (
        "筛选尺度宽松：仅当图片属于以下情况时 keep=false："
        "①图标、网站logo、二维码、角标水印；②加载失败、纯色块、严重模糊/马赛克、"
        "主体残缺不可辨认；③与本批前面某张近乎重复（只保留更清晰的一张）。"
        "其余图片（包括横幅、截图、海报、设计图）一律 keep=true。"
        if filter_mode != "strict"
        else "筛选尺度严格（质量优先）：模糊失焦、构图混乱、水印遮挡严重、纯文字小截图、"
             "低质横幅广告、与图集主题无关的图片也要 keep=false，只保留画面质量与内容价值最高的图。"
    )
    index_hint = (
        f"共 {batch_count} 张，编号为图{offset + 1}~图{offset + batch_count}（全局编号，必须原样使用）"
        if offset
        else f"共 {batch_count} 张，编号为图1~图{batch_count}"
    )
    return (
        f"你是网页图片策展编辑。这批图片爬取自网页《{title}》，图集目标：{goal or '网页图片精选'}。"
        f"{index_hint}，图片按顺序给出。\n"
        f"筛选规则：{policy}\n"
        "对每张图输出：\n"
        "- index：图片的全局编号（整数）\n"
        "- keep：布尔值，是否建议入册 PPT\n"
        "- score：0~100 整数，综合清晰度、构图、内容价值\n"
        f"- category：取值之一 {sorted(_VALID_CATEGORIES)}\n"
        "- reason：≤20 字中文依据\n"
        "- description：15~30 字客观画面描述（主体/场景/动作/色调），供后续撰写 PPT 文案使用，"
        "禁止评价和想象；无法辨认时为空字符串\n"
        "只输出 JSON 数组，示例："
        '[{"index":1,"keep":true,"score":86,"category":"photo",'
        '"reason":"清晰的街头人物全身照","description":"一名男子穿白色T恤站在灰色砖墙前"}]'
    )


def _coerce_assessment(name: str, raw: dict, local_index: int) -> ImageAssessment:
    """
    把模型单行输出容错规整为 ImageAssessment。

    Why 宽容解析：视觉模型偶发漏项/串号，lenient 策略下错杀图片比放过一张更糟，
    因此异常行一律按"保守保留（60 分、无描述）"处理，由后续评分排序决定去留。
    """
    try:
        index = int(raw.get("index", local_index + 1))
    except (TypeError, ValueError):
        index = local_index + 1
    category = str(raw.get("category", "other")).strip().lower()
    if category not in _VALID_CATEGORIES:
        category = "other"
    try:
        score = max(0, min(100, int(raw.get("score", 60))))
    except (TypeError, ValueError):
        score = 60
    keep = bool(raw.get("keep", True))
    return ImageAssessment(
        name=name,
        keep=keep,
        score=score,
        category=category,
        reason=str(raw.get("reason", "")).strip()[:40],
        description=str(raw.get("description", "")).strip()[:80],
    )


def choose_kept(
    assessments: list[ImageAssessment],
    *,
    max_images: int | None = None,
    filter_mode: str | None = None,
    min_score: int | None = None,
) -> tuple[list[ImageAssessment], list[ImageAssessment]]:
    """
    根据评估结果决定入册名单。

    Args:
        assessments: 全量评估（原始顺序）
        max_images: 入册上限，默认 settings.select_max_images
        filter_mode: lenient/strict
        min_score: lenient 模式下的最低保留分，默认 settings.lenient_min_score

    Returns:
        (kept, dropped)：kept 按分数降序（即最终入册/文案顺序），dropped 保持原序
    """
    max_images = max_images or settings.select_max_images
    filter_mode = (filter_mode or settings.filter_mode).lower()
    min_score = min_score if min_score is not None else settings.lenient_min_score

    keepers: list[ImageAssessment] = []
    for item in assessments:
        if item.category in _HARD_DROP_CATEGORIES:
            continue
        if not item.keep:
            continue
        if filter_mode == "lenient" and item.score < min_score:
            continue
        if filter_mode == "strict" and item.score < 60:
            continue
        keepers.append(item)

    keepers.sort(key=lambda item: item.score, reverse=True)

    # lenient 模式的最终兜底：模型若全盘误杀，退化为"全部保留"，绝不阻断 PPT 生成
    if not keepers and filter_mode != "strict":
        logger.warning("Kimi 未筛出任何图片，lenient 策略退化为全部保留")
        keepers = list(assessments)
        keepers.sort(key=lambda item: item.score, reverse=True)

    dropped_names = {item.name for item in keepers[max_images:]}
    if len(keepers) > max_images:
        logger.info("Kimi 保留 %d 张，超出上限 %d，按分数截取", len(keepers), max_images)
    kept = keepers[:max_images]

    kept_names = {item.name for item in kept}
    dropped = [item for item in assessments if item.name not in kept_names]
    # 因上限被截断的图片补充可读原因
    for item in dropped:
        if item.name in dropped_names and not item.reason:
            item.reason = "超出入册数量上限"
    return kept, dropped


# ---------------------------------------------------------------------------
# 阶段二：Kimi 文本生成页面文案
# ---------------------------------------------------------------------------

_COPY_SYSTEM_PROMPT = (
    "你是资深商业内容编辑，擅长为网页精选图片图集撰写 PPT 页面文案。"
    "你只基于给定的画面描述写作，绝不编造描述中不存在的品牌、数据与事实，"
    "不使用 emoji，不使用夸张宣传语，各页文案不得雷同。"
)


async def generate_page_copy(
    client: KimiClient,
    title: str,
    goal: str,
    kept: list[ImageAssessment],
) -> dict | None:
    """
    让文本模型一次性产出整份 PPT 的结构化文案。

    Args:
        client: KimiClient
        title: 图集标题
        goal: 核心目标
        kept: 入选图片评估（顺序即页面顺序，description 为写作依据）

    Returns:
        与 content_pack.build_ai_content_pack 对齐的文案字典；
        生成或解析失败时返回 None，由调用方回退确定性内容包（不阻断导出）
    """
    if not kept:
        return None

    image_list = [
        {"序号": index, "画面描述": item.description or "（无描述，按文件名与通用图集语境处理）"}
        for index, item in enumerate(kept, start=1)
    ]
    prompt = f"""请为一份图片图集 PPT 撰写中文文案。
图集标题：{title}
核心目标：{goal or title}
图片清单（JSON，共 {len(kept)} 张）：
{json.dumps(image_list, ensure_ascii=False, indent=2)}

严格按以下 JSON 结构输出，pages 数量必须等于 {len(kept)} 且 index 与图片序号一一对应：
{{
  "cover": {{
    "core_message": "封面主张，≤30字",
    "summary_full": "≤24字导语", "summary_short": "≤10字",
    "items": [
      {{"label": "≤6字议程名", "full": "≤24字事实", "short": "≤10字"}},
      {{"label": "≤6字议程名", "full": "≤24字事实", "short": "≤10字"}},
      {{"label": "≤6字议程名", "full": "≤24字事实", "short": "≤10字"}}
    ]
  }},
  "pages": [
    {{
      "index": 1,
      "core_message": "本页一句话看点，≤30字，必须贴合该图画面描述",
      "title_full": "≤12字小标题", "title_short": "≤6字",
      "summary_full": "≤24字画面描述句", "summary_short": "≤10字",
      "highlight": {{"label": "≤5字关键词", "full": "≤24字客观画面亮点", "short": "≤10字"}}
    }}
  ],
  "closing": {{
    "core_message": "≤20字结束语",
    "summary_full": "≤24字", "summary_short": "≤10字",
    "items": [
      {{"label": "≤6字", "full": "≤24字", "short": "≤10字"}},
      {{"label": "≤6字", "full": "≤24字", "short": "≤10字"}}
    ]
  }}
}}
只输出 JSON 对象本身，不要 markdown 代码块或任何解释。"""

    try:
        reply = await client.chat_text(_COPY_SYSTEM_PROMPT, prompt, settings.kimi_text_model, temperature=1.0)
        parsed = extract_json_block(reply)
        return _normalize_copy(parsed, len(kept))
    except (KimiError, ValueError, TypeError) as exc:
        # 文案失败不应让整个 PPT 失败：记录原因，上层自动回退固定模板
        logger.warning("Kimi 文案生成失败，回退确定性内容包: %s", exc)
        return None


def _normalize_copy(parsed: object, expected_pages: int) -> dict:
    """
    校验并规整文案 JSON：pages 必须为等长列表，按 index 对齐到 1..N。

    Raises:
        ValueError: 结构缺失或页数不匹配
    """
    if not isinstance(parsed, dict):
        raise ValueError("文案输出不是 JSON 对象")
    if not isinstance(parsed.get("cover"), dict) or not isinstance(parsed.get("closing"), dict):
        raise ValueError("缺少 cover 或 closing 段落")
    raw_pages = parsed.get("pages")
    if not isinstance(raw_pages, list) or len(raw_pages) != expected_pages:
        raise ValueError(f"pages 数量不匹配：期望 {expected_pages}，实际 {len(raw_pages) if isinstance(raw_pages, list) else '非数组'}")

    by_index: dict[int, dict] = {}
    for pos, raw in enumerate(raw_pages, start=1):
        if not isinstance(raw, dict):
            raise ValueError(f"第 {pos} 页文案不是对象")
        try:
            idx = int(raw.get("index", pos))
        except (TypeError, ValueError):
            idx = pos
        by_index[idx] = raw

    pages = []
    for index in range(1, expected_pages + 1):
        page = by_index.get(index)
        if page is None:
            raise ValueError(f"缺少第 {index} 页文案")
        pages.append(page)

    return {"cover": parsed["cover"], "pages": pages, "closing": parsed["closing"]}
