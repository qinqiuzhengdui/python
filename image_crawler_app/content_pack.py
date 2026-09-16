"""
PageContentPack（dashi schema v2）装配领域模块。

两类内容包：
1. 确定性内容包（build_content_pack）：固定模板文案，不依赖大模型，保证链路永远可用；
2. AI 内容包（build_ai_content_pack）：封面/正文/结尾的叙事文案由 Kimi 生成，
   但"图序/文件大小"等数值型事实仍由本地真实数据装配——这是 12 套主题布局命中数的
   实证要求（纯文本 items 在 theme06/08 命中不足 3 套模板），且能防止模型编造指标。

依赖方向：本模块只依赖标准库，可被 ppt_service / ai_pipeline 导入，反向不成立。

@example
    pack = build_ai_content_pack("NOWRE 现客", "街头潮流图集", media_items, kimi_copy)
    Path("page-content-pack.json").write_text(json.dumps(pack, ensure_ascii=False))
"""

from __future__ import annotations

from typing import Any

# ---- 投影槽位长度约束（软截断，防止 Kimi 写出溢出版式的长句） ----
_LIMIT_CORE = 60        # coreMessage
_LIMIT_TITLE_FULL = 24
_LIMIT_TITLE_SHORT = 12
_LIMIT_SUMMARY_FULL = 40
_LIMIT_SUMMARY_SHORT = 14
_LIMIT_LABEL = 8        # items.label / highlight.label
_LIMIT_DETAIL_FULL = 28
_LIMIT_DETAIL_SHORT = 14


def _clip(text: Any, fallback: str, max_len: int) -> str:
    """
    规整模型输出的文本字段：去空白、空值回退、按字符数截断。

    Args:
        text: 模型给的原始值（容错非字符串）
        fallback: 为空/非法时使用的确定性文案
        max_len: 最大字符数

    Returns:
        可直接放入内容包的非空字符串
    """
    cleaned = str(text or "").strip().replace("\n", " ")
    if not cleaned:
        return fallback
    return cleaned[:max_len]


def _cover_items(image_count: int) -> list[dict]:
    """封面页议程事实（3 条，命中最多的封面布局族）。"""
    return [
        {
            "id": "cover-1",
            "label": "精选图集",
            "detail": {"full": f"共收录 {image_count} 张高清图片", "short": f"{image_count} 张图片"},
        },
        {
            "id": "cover-2",
            "label": "高清原图",
            "detail": {"full": "来自目标网页的原始大图", "short": "原始大图"},
        },
        {
            "id": "cover-3",
            "label": "一键成册",
            "detail": {"full": "自动排版生成可编辑 PPT", "short": "自动排版"},
        },
    ]


def _metric_items(index: int, size_bytes: int) -> list[dict]:
    """
    正文页数值型事实（图序 + 文件大小）。

    Why 保留真实指标：带 value/displayValue/unit 的 items 在 12 个主题下
    命中布局数显著更高（实测 min≥5），文件大小取真实字节，杜绝模型编造。
    """
    if size_bytes >= 1024 * 1024:
        display, unit = f"{size_bytes / 1024 / 1024:.1f}", "MB"
    else:
        display, unit = f"{max(1, size_bytes // 1024)}", "KB"
    return [
        {
            "id": f"body-{index}-1",
            "label": "图序",
            "value": index,
            "displayValue": f"{index:02d}",
        },
        {
            "id": f"body-{index}-2",
            "label": "文件大小",
            "value": round(float(display), 2),
            "displayValue": display,
            "unit": unit,
        },
    ]


def _quality_item(index: int) -> dict:
    """正文页第三条事实的确定性兜底（Kimi 亮点缺失时使用）。"""
    return {
        "id": f"body-{index}-3",
        "label": "画质",
        "detail": {"full": "原图分辨率直出", "short": "原图直出"},
    }


def _closing_items() -> list[dict]:
    """结尾页事实（2 条，命中最多的结尾布局族）。"""
    return [
        {
            "id": "closing-1",
            "label": "感谢观看",
            "detail": {"full": "谢谢您的浏览", "short": "谢谢浏览"},
        },
        {
            "id": "closing-2",
            "label": "期待再见",
            "detail": {"full": "期待下一次精彩分享", "short": "下次见"},
        },
    ]


def build_content_pack(title: str, goal: str, media_items: list[dict]) -> dict:
    """
    构造确定性内容包（封面 + 每图一页 + 结尾页）。

    Args:
        title: PPT 标题
        goal: 整份 PPT 的核心目标文案
        media_items: stage_media 返回的 media 条目（含 src/mime/size）

    Returns:
        {"pages": [...]} 内容计划 JSON
    """
    pages = [
        {
            "id": "page-1",
            "presentation": {
                "pageIntent": "cover",
                "coreMessage": goal,
                "title": {"full": title, "short": title},
                "summary": {"full": goal, "short": goal},
                "items": _cover_items(len(media_items)),
            },
        }
    ]
    for index, media in enumerate(media_items, start=1):
        items = _metric_items(index, media.get("size", 0))
        items.append(_quality_item(index))
        pages.append(
            {
                "id": f"page-{index + 1}",
                "presentation": {
                    "pageIntent": "body",
                    "coreMessage": f"图集欣赏 {index}",
                    "title": {"full": f"图集欣赏 {index}", "short": f"图 {index}"},
                    "summary": {"full": "来自目标网页的高清图片", "short": "高清图片"},
                    "items": items,
                    "media": [_media_without_internal(media)],
                },
            }
        )
    pages.append(
        {
            "id": f"page-{len(media_items) + 2}",
            "presentation": {
                "pageIntent": "closing",
                "coreMessage": "感谢观看",
                "title": {"full": "感谢观看", "short": "感谢观看"},
                "summary": {"full": goal, "short": "谢谢观看"},
                "items": _closing_items(),
            },
        }
    )
    return {"pages": pages}


def build_ai_content_pack(
    title: str,
    goal: str,
    media_items: list[dict],
    copy: dict,
) -> dict:
    """
    用 Kimi 文案 + 真实数值指标合并出内容包。

    Args:
        title: PPT 标题（用户输入或域名推导）
        goal: 核心目标文案
        media_items: stage_media 返回的 media 条目（顺序即正文页顺序）
        copy: ai_pipeline.generate_page_copy 的结构化输出，包含
              cover/pages/climming 三段文案；缺字段时逐级回退确定性文案

    Returns:
        {"pages": [...]} 内容计划 JSON
    """
    cover_copy = copy.get("cover") or {}
    page_copies = copy.get("pages") or []
    closing_copy = copy.get("closing") or {}

    pages: list[dict] = [_build_cover_page(title, goal, len(media_items), cover_copy)]

    for index, media in enumerate(media_items, start=1):
        page_copy = page_copies[index - 1] if index - 1 < len(page_copies) else {}
        pages.append(_build_body_page(index, media, page_copy))

    pages.append(_build_closing_page(goal, len(media_items), closing_copy))
    return {"pages": pages}


def _media_without_internal(media: dict) -> dict:
    """
    剥离仅供本地装配使用的内部字段（如 size），输出 schema 认可的 media 条目。

    Why: normalizePageContentPack 会忽略未知字段，但显式过滤可让产物 JSON 更干净，
    也避免内部字段泄漏到 deck 目录的内容计划文件中。
    """
    allowed = {"src", "kind", "type", "alt"}
    return {key: value for key, value in media.items() if key in allowed and value is not None}


def _build_cover_page(title: str, goal: str, image_count: int, cover_copy: dict) -> dict:
    """装配封面页：Kimi 议程文案优先，缺失回退确定性模板。"""
    items = _cover_items(image_count)
    ai_items = cover_copy.get("items") if isinstance(cover_copy, dict) else None
    if isinstance(ai_items, list) and ai_items:
        for slot, raw in zip(items, ai_items[:3]):
            if not isinstance(raw, dict):
                continue
            slot["label"] = _clip(raw.get("label"), slot["label"], _LIMIT_LABEL)
            full_fallback = slot["detail"]["full"]
            short_fallback = slot["detail"]["short"]
            slot["detail"] = {
                "full": _clip(raw.get("full") or raw.get("detail"), full_fallback, _LIMIT_DETAIL_FULL),
                "short": _clip(raw.get("short"), short_fallback, _LIMIT_DETAIL_SHORT),
            }

    return {
        "id": "page-1",
        "presentation": {
            "pageIntent": "cover",
            "coreMessage": _clip(cover_copy.get("core_message"), goal, _LIMIT_CORE),
            "title": {"full": title, "short": title},
            "summary": {
                "full": _clip(cover_copy.get("summary_full"), goal, _LIMIT_SUMMARY_FULL),
                "short": _clip(cover_copy.get("summary_short"), goal, _LIMIT_SUMMARY_SHORT),
            },
            "items": items,
        },
    }


def _build_body_page(index: int, media: dict, page_copy: dict) -> dict:
    """
    装配正文页：2 条真实数值指标 + 1 条 Kimi 画面亮点（缺失回退"画质"）。
    """
    fallback_core = f"图集欣赏 {index}"
    items = _metric_items(index, media.get("size", 0))

    highlight = page_copy.get("highlight") if isinstance(page_copy, dict) else None
    if isinstance(highlight, dict) and (highlight.get("full") or highlight.get("label")):
        items.append(
            {
                "id": f"body-{index}-3",
                "label": _clip(highlight.get("label"), "看点", _LIMIT_LABEL),
                "detail": {
                    "full": _clip(highlight.get("full"), "原图分辨率直出", _LIMIT_DETAIL_FULL),
                    "short": _clip(highlight.get("short"), "原图直出", _LIMIT_DETAIL_SHORT),
                },
            }
        )
    else:
        items.append(_quality_item(index))

    return {
        "id": f"page-{index + 1}",
        "presentation": {
            "pageIntent": "body",
            "coreMessage": _clip(page_copy.get("core_message"), fallback_core, _LIMIT_CORE),
            "title": {
                "full": _clip(page_copy.get("title_full"), fallback_core, _LIMIT_TITLE_FULL),
                "short": _clip(page_copy.get("title_short"), f"图 {index}", _LIMIT_TITLE_SHORT),
            },
            "summary": {
                "full": _clip(page_copy.get("summary_full"), "来自目标网页的高清图片", _LIMIT_SUMMARY_FULL),
                "short": _clip(page_copy.get("summary_short"), "高清图片", _LIMIT_SUMMARY_SHORT),
            },
            "items": items,
            "media": [_media_without_internal(media)],
        },
    }


def _build_closing_page(goal: str, image_count: int, closing_copy: dict) -> dict:
    """装配结尾页：Kimi 感谢语优先，议程事实缺失回退模板。"""
    items = _closing_items()
    ai_items = closing_copy.get("items") if isinstance(closing_copy, dict) else None
    if isinstance(ai_items, list) and ai_items:
        for slot, raw in zip(items, ai_items[:2]):
            if not isinstance(raw, dict):
                continue
            slot["label"] = _clip(raw.get("label"), slot["label"], _LIMIT_LABEL)
            slot["detail"] = {
                "full": _clip(raw.get("full") or raw.get("detail"), slot["detail"]["full"], _LIMIT_DETAIL_FULL),
                "short": _clip(raw.get("short"), slot["detail"]["short"], _LIMIT_DETAIL_SHORT),
            }

    return {
        "id": f"page-{image_count + 2}",
        "presentation": {
            "pageIntent": "closing",
            "coreMessage": _clip(closing_copy.get("core_message"), "感谢观看", _LIMIT_CORE),
            "title": {"full": "感谢观看", "short": "感谢观看"},
            "summary": {
                "full": _clip(closing_copy.get("summary_full"), goal, _LIMIT_SUMMARY_FULL),
                "short": _clip(closing_copy.get("summary_short"), "谢谢观看", _LIMIT_SUMMARY_SHORT),
            },
            "items": items,
        },
    }
