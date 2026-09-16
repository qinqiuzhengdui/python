"""
PPT 生成业务层：调用本地 dashi-ppt-skill 项目，把爬取的图片一键生成可编辑 PPT。

工作流（严格对应 dashi-ppt-skill 的 goal-scaffold 标准流程）：
1. media:stage              —— 把爬取图片复制进 deck 的 ppt/assets/user-media/ 并返回相对路径
2. 生成 page-content-pack.json（PageContentPack schema v2）
3. goal:scaffold            —— 由内容计划生成 goal.json（每页 3 template + 1 bespoke）
4. 将 variantOutputMode 改为 "selected-only"，导出时每逻辑页只出 1 页（而非 4N 页）
5. validate:goal-spec       —— 校验 goal 结构
6. render:goal              —— 渲染 ppt/index.html
7. validate:swiss           —— 校验渲染产物结构
8. export:pptx --selected-only —— 导出可编辑 PPTX

依赖：
- Node.js 20+ / npm（dashi-ppt-skill 项目已自带 package.json，首次调用自动装依赖）
- 本模块只调用 dashi-ppt-skill 的本地命令，不联网生成内容。

@example
    from ppt_service import generate_ppt

    result = generate_ppt(
        deck_dir=Path("static/_ppt/demo"),
        title="网页图片图集",
        goal="从目标网页抓取的高清图片",
        theme="theme01",
        image_paths=[Path("static/crawled/a.jpg")],
    )
    print(result["pptx_path"], result["html_url"])
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import uuid
from pathlib import Path

# ---------------------------------------------------------------------------
# 常量与配置（集中管理，避免散落在业务代码中）
# ---------------------------------------------------------------------------

# dashi-ppt-skill 内置生成器项目根目录
PPT_SKILL_PROJECT = (
    Path(__file__).resolve().parents[1]
    / "dashi-ppt-skill"
    / "skills"
    / "dashi-ppt"
    / "project"
)

# 可选主题（对应 SKILL.md 的 12 种视觉风格）
THEMES = [
    "theme01",  # 轻拟态风
    "theme02",  # 炫光紫绿风
    "theme03",  # 深浅代码风
    "theme04",  # 玻璃糖果风
    "theme05",  # 色谱图表风
    "theme06",  # 深色图谱风
    "theme07",  # 冷白调研风
    "theme08",  # 黑金实验风
    "theme09",  # 深蓝杂志风
    "theme10",  # 金色指数风
    "theme11",  # 高能增长风
    "theme12",  # 声波霓虹风
]

# 图片扩展名 -> MIME 类型
_MIME_BY_EXT = {
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png": "image/png",
    ".webp": "image/webp",
    ".gif": "image/gif",
    ".avif": "image/avif",
    ".svg": "image/svg+xml",
}

# 校验输出中的“真实残留”关键词（默认文案/占位符残留，与主题无关时不可交付）
_RESIDUE_MARKERS = ("残留", "占位")

# 单次 PPT 可承载的最大图片数（页数 = 图片数 + 封面 + 结尾）
MAX_PPT_IMAGES = 30

# 子进程超时（秒）：media:stage / scaffold / render 都很慢，导出还要启动浏览器
_CMD_TIMEOUT = 900


# ---------------------------------------------------------------------------
# 基础工具
# ---------------------------------------------------------------------------

class PptError(RuntimeError):
    """PPT 生成过程中的可预期错误。"""


def _npm_cmd() -> str:
    """定位 npm 可执行文件（Windows 上为 npm.cmd）。"""
    npm = shutil.which("npm")
    if not npm:
        raise PptError("未找到 npm，请先安装 Node.js 20+")
    return npm


def _run(args: list[str], cwd: Path) -> str:
    """
    运行子进程命令并返回 stdout；失败时抛出带输出的 PptError。

    Args:
        args: 命令参数列表
        cwd: 子进程工作目录（同时是 npm 的 INIT_CWD，脚本相对路径按它解析）

    Returns:
        命令 stdout 文本

    Raises:
        PptError: 命令返回非零退出码
    """
    proc = subprocess.run(
        args,
        cwd=str(cwd),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=_CMD_TIMEOUT,
    )
    if proc.returncode != 0:
        raise PptError(
            f"命令失败（exit {proc.returncode}）: {' '.join(args)}\n"
            f"--- stdout ---\n{proc.stdout}\n--- stderr ---\n{proc.stderr}"
        )
    return proc.stdout


def _npm_run(script: str, script_args: list[str], cwd: Path) -> str:
    """
    在 dashi-ppt-skill 项目里执行 npm run <script> -- <args>。

    Args:
        script: package.json 里的脚本名
        script_args: 传给脚本的参数（-- 之后）
        cwd: 调用方工作目录（INIT_CWD）

    Returns:
        脚本 stdout
    """
    return _run(
        [_npm_cmd(), "--prefix", str(PPT_SKILL_PROJECT), "run", script, "--", *script_args],
        cwd,
    )


def _extract_json_after_banner(output: str) -> dict:
    """
    从 npm run 输出中提取最后一个 JSON 对象。

    npm 会在 stdout 前打印生命周期 banner（"> dashi-ppt-runtime@..." 等），
    真正的 JSON 从首个 '{' 开始。

    Args:
        output: 命令 stdout

    Returns:
        解析后的 JSON 对象
    """
    start = output.find("{")
    if start < 0:
        raise PptError(f"无法从命令输出中解析 JSON: {output[:500]}")
    try:
        return json.loads(output[start:])
    except json.JSONDecodeError as exc:
        raise PptError(f"命令输出 JSON 解析失败: {exc}") from exc


def _mime_for(path: Path) -> str:
    """按扩展名推断图片 MIME 类型。"""
    return _MIME_BY_EXT.get(path.suffix.lower(), "image/jpeg")


# ---------------------------------------------------------------------------
# 核心流程
# ---------------------------------------------------------------------------

def stage_media(deck_dir: Path, image_paths: list[Path]) -> list[dict]:
    """
    调用 media:stage 把图片复制进 deck 的 ppt/assets/user-media/。

    Args:
        deck_dir: 本次 deck 输出目录
        image_paths: 待入册图片的绝对路径

    Returns:
        media 条目列表：[{"src": 相对路径, "mime": MIME, "alt": 描述}]

    Raises:
        PptError: 有图片不存在 / 类型不支持 / 命令失败
    """
    for path in image_paths:
        if not path.is_file():
            raise PptError(f"图片不存在: {path}")
    deck_dir.mkdir(parents=True, exist_ok=True)
    output = _npm_run(
        "media:stage",
        [str(deck_dir), *(str(p) for p in image_paths)],
        deck_dir,
    )
    data = _extract_json_after_banner(output)
    items = data.get("items") or []
    if not items:
        raise PptError("media:stage 未返回任何图片条目")
    staged = []
    for i, item in enumerate(items):
        size = image_paths[i].stat().st_size
        staged.append(
            {
                "src": item["relative"],
                "kind": item.get("kind", "image"),
                "type": item.get("mime", _mime_for(Path(image_paths[i]))),
                "alt": f"图片 {i + 1}",
                # 仅用于构建正文页“文件大小”事实，PageContentPack 校验会忽略未知字段
                "size": size,
            }
        )
    return staged


def build_content_pack(
    title: str,
    goal: str,
    media_items: list[dict],
) -> dict:
    """
    按 PageContentPack schema 生成内容计划（封面 + 每图一页 + 结尾页）。

    内容形状基于对 12 个主题的实证探测（layout-query 命中数）确定：
    - 封面/结尾页必须携带 items（3 条议程事实），命中数最高；携带 media 反而
      排除无媒体槽的封面布局，因此封面不挂媒体。
    - 正文页必须同时携带 media（单图）+ items（3 条事实）：只有 media 时多数
      主题命中不足 3 套模板（theme05/12 为 0），加 items 后命中数翻数倍。

    Args:
        title: PPT 标题
        goal: 整份 PPT 的核心目标文案
        media_items: stage_media 返回的 media 条目

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
        pages.append(
            {
                "id": f"page-{index + 1}",
                "presentation": {
                    "pageIntent": "body",
                    "coreMessage": f"图集欣赏 {index}",
                    "title": {"full": f"图集欣赏 {index}", "short": f"图 {index}"},
                    "summary": {"full": "来自目标网页的高清图片", "short": "高清图片"},
                    "items": _body_items(index, media.get("size", 0)),
                    "media": [media],
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


def _body_items(index: int, size_bytes: int) -> list[dict]:
    """
    正文页事实（3 条，含数值指标）。

    带 value/displayValue/unit 的指标型 items 在 12 个主题下命中布局数显著更高
    （实测 min≥5，而纯文本 items 在 theme06/08 命中不足 3 套模板）。

    Args:
        index: 当前图片序号（从 1 开始）
        size_bytes: 图片文件字节数（真实数据，用于“文件大小”指标）

    Returns:
        items 列表
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
        {
            "id": f"body-{index}-3",
            "label": "画质",
            "detail": {"full": "原图分辨率直出", "short": "原图直出"},
        },
    ]


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


def scaffold_goal(
    deck_dir: Path,
    title: str,
    goal: str,
    theme: str,
    page_count: int,
    content_pack_path: Path,
    goal_path: Path,
) -> None:
    """
    运行 goal:scaffold 生成 schema v2 goal.json，并把导出模式改为 selected-only。

    Args:
        deck_dir: 本次 deck 输出目录
        title: PPT 标题
        goal: 核心目标文案
        theme: 主题包（theme01~theme12）
        page_count: 逻辑页数
        content_pack_path: page-content-pack.json 路径
        goal_path: 输出 goal.json 路径

    Raises:
        PptError: scaffold 失败
    """
    seed = f"{theme}-{uuid.uuid4().hex[:6]}"
    _npm_run(
        "goal:scaffold",
        [
            "--title", title,
            "--goal", goal,
            "--theme", theme,
            "--pages", str(page_count),
            "--layout-variants", "3",
            "--content-plan", str(content_pack_path),
            "--seed", seed,
            "--chunk-size", "5",
            "--out", str(goal_path),
        ],
        deck_dir,
    )
    # 导出时每逻辑页只保留选中的 v1 方案，避免 4N 页的对比模式刷屏
    with goal_path.open("r", encoding="utf-8") as handle:
        spec = json.load(handle)
    spec["variantOutputMode"] = "selected-only"
    with goal_path.open("w", encoding="utf-8") as handle:
        json.dump(spec, handle, ensure_ascii=False, indent=2)


def validate_and_render(deck_dir: Path, goal_path: Path, output_html: Path) -> None:
    """
    依次执行 validate:goal-spec、render:goal、validate:swiss。

    Args:
        deck_dir: 本次 deck 输出目录
        goal_path: goal.json 路径
        output_html: 渲染目标 ppt/index.html

    Raises:
        PptError: 任一校验或渲染失败
    """
    _npm_run("validate:goal-spec", [str(goal_path)], deck_dir)
    _npm_run("render:goal", [str(goal_path), str(output_html)], deck_dir)
    _npm_run("validate:swiss", [str(output_html)], deck_dir)


def check_copy_residue(deck_dir: Path, goal_path: Path, output_html: Path) -> list[str]:
    """
    运行 validate:goal-copy 并区分“真实文案残留”与“投影槽位误报”。

    结构投影 deck 中，未绑定的模板槽由运行时置空，校验脚本无法感知投影，
    会把空槽报成“未覆写模板文案槽”的误报；而默认演示文案/占位符残留是
    真实质量问题，必须拦截。

    Args:
        deck_dir: 本次 deck 输出目录
        goal_path: goal.json 路径
        output_html: 渲染后的 ppt/index.html 路径

    Returns:
        真实残留错误列表（空列表表示干净）
    """
    try:
        _npm_run("validate:goal-copy", [str(goal_path), str(output_html)], deck_dir)
        return []
    except PptError as exc:
        message = str(exc)
        return [line.strip() for line in message.splitlines() if any(marker in line for marker in _RESIDUE_MARKERS)]


def export_pptx(deck_dir: Path, ppt_dir: Path, out_file: Path) -> None:
    """
    导出可编辑 PPTX（selected-only，N 页）。

    Args:
        deck_dir: 本次 deck 输出目录
        ppt_dir: 渲染后的 ppt 目录（含 index.html）
        out_file: 输出 .pptx 路径

    Raises:
        PptError: 导出失败
    """
    _npm_run("export:pptx", [str(ppt_dir), str(out_file), "--selected-only"], deck_dir)


def generate_ppt(
    deck_dir: Path,
    title: str,
    goal: str,
    theme: str,
    image_paths: list[Path],
) -> dict:
    """
    一键生成 PPT：编排 media:stage → scaffold → 校验/渲染 → 导出 PPTX。

    Args:
        deck_dir: 本次 deck 输出目录（最终产物与 pptx 都在其中）
        title: PPT 标题
        goal: 核心目标文案
        theme: 主题包（theme01~theme12）
        image_paths: 待入册图片绝对路径列表

    Returns:
        {
          "pptx_path": 最终 .pptx 绝对路径,
          "html_path": 可预览的 ppt/index.html 绝对路径,
          "page_count": 逻辑页数,
          "image_count": 图片张数,
        }

    Raises:
        PptError: 任意一步失败（错误信息含具体命令输出）
    """
    theme = theme if theme in THEMES else "theme01"
    image_paths = list(image_paths)[:MAX_PPT_IMAGES]
    if not image_paths:
        raise PptError("没有可用图片，请先完成爬取")

    deck_dir.mkdir(parents=True, exist_ok=True)
    media_items = stage_media(deck_dir, image_paths)

    title = title.strip() or "网页图片图集"
    goal = goal.strip() or "从目标网页抓取的高清图片图集"

    content_pack_path = deck_dir / "page-content-pack.json"
    content_pack_path.write_text(
        json.dumps(build_content_pack(title, goal, media_items), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    page_count = len(media_items) + 2
    goal_path = deck_dir / "goal.json"
    scaffold_goal(deck_dir, title, goal, theme, page_count, content_pack_path, goal_path)

    output_html = deck_dir / "ppt" / "index.html"
    validate_and_render(deck_dir, goal_path, output_html)

    # 文案残留检查：仅拦截真实默认文案残留，投影空槽误报忽略
    residue = check_copy_residue(deck_dir, goal_path, output_html)
    if residue:
        raise PptError("渲染产物存在默认文案残留，拒绝交付:\n" + "\n".join(residue))

    pptx_path = deck_dir / f"{deck_dir.name}.pptx"
    export_pptx(deck_dir, output_html.parent, pptx_path)

    return {
        "pptx_path": str(pptx_path),
        "html_path": str(output_html),
        "page_count": page_count,
        "image_count": len(media_items),
    }


def derive_default_title(page_url: str) -> str:
    """
    由网页 URL 推导默认 PPT 标题（去掉协议与路径）。

    Args:
        page_url: 爬取来源网址

    Returns:
        标题文本，例如 "diction-style.com"
    """
    cleaned = re.sub(r"^https?://", "", page_url).split("/")[0]
    return cleaned or "网页图片图集"
