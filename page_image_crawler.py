"""
网页图片批量下载工具（Playwright 实现，功能等价于 AixDownloader 的图片批量下载）。

支持两种模式：
1. 单页模式：只下载当前页面上的全部图片；
2. 帖子女集模式：自动跟进首页中的文章/帖子链接（article/h1-h3 内锚点），
   逐篇进入详情页下载完整图集，按帖子分目录保存。

原理：
1. 用无头 Chromium 打开目标页面；
2. 自动滚动页面，触发懒加载（IntersectionObserver / loading="lazy"）图片；
3. 提取 <img>、CSS background、data-bg 等属性中的图片地址；
4. 用 requests 批量下载到本地目录。

用法:
    # 单页模式
    python page_image_crawler.py --url "https://www.thesartorialist.com/" --output sartorialist_images

    # 帖子女集模式：跟进首页中最多 10 篇帖子，下载每篇完整图集
    python page_image_crawler.py --url "https://www.thesartorialist.com/" --output sartorialist_images --max-posts 10
"""

import argparse
import asyncio
import json
import re
from pathlib import Path
from urllib.parse import urljoin, urlparse

import requests
from playwright.async_api import async_playwright

# 模拟真实浏览器的请求头，避免被 CDN/WAF 拦截
HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                   "AppleWebKit/537.36 (KHTML, like Gecko) "
                   "Chrome/126.0.0.0 Safari/537.36"),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/*,*/*;q=0.8",
    "Accept-Language": "zh-CN,zh;q=0.9",
}

# img 标签上常见的懒加载属性（不同站点实现不同，全部兜底提取）
IMG_ATTRS = ["data-src", "data-original",
             "data-lazy-src", "data-echo", "data-url", "data-hi-res-src"]

# 图片路径特征：带扩展名，或 CDN 常见路径段（如 /img/、puls-img/）
IMG_EXT_RE = re.compile(r"\.(jpe?g|png|webp|gif|avif|bmp|svg)(\?|$)", re.I)

SAFE_NAME_RE = re.compile(r"[^\w.\-]+")

# URL 路径中出现这些段时，视为站点导航/功能页而非内容帖
NON_POST_SEGMENTS = {"category", "tag", "page", "author", "about", "contact",
                     "shop", "privacy", "terms", "feed", "wp-content",
                     "wp-json", "login", "account", "cart"}


def normalize_urls(page_url: str, urls: list[str]) -> list[str]:
    """
    将页面内提取的原始地址转为绝对 URL 并去重、过滤。

    Args:
        page_url: 当前页面 URL，用于拼接相对路径
        urls: 从页面提取的图片地址（可能含相对路径、srcset 等）

    Returns:
        去重后的绝对图片 URL 列表
    """
    seen: set[str] = set()
    result: list[str] = []
    for raw in urls:
        url = urljoin(page_url, raw.strip())
        if not url.startswith("http"):
            continue
        if url in seen:
            continue
        path = urlparse(url).path.lower()
        # 只保留图片特征路径，过滤 srcset 链式变换拆出的碎片（如 w_320/q_auto:good）
        if not (IMG_EXT_RE.search(path) or "/img/" in path or "puls-img" in path):
            continue
        seen.add(url)
        result.append(url)
    return result


async def extract_image_urls_from_page(page) -> list[str]:
    """
    从已加载的页面中提取所有图片 URL（含 srcset、懒加载属性、CSS 背景图）。

    Args:
        page: Playwright 的 Page 对象

    Returns:
        图片 URL 列表（未归一化）
    """
    urls = await page.evaluate(
        """() => {
            const urls = new Set();
            const push = (u) => { if (u && u.trim()) urls.add(u.trim()); };
            const attrs = %s;
            // 帖子详情页只有一个 article：限定正文提取，避免相关推荐图；
            // 列表/首页有多个 article（每条帖子一个）：用全页提取全部缩略图。
            const articles = document.querySelectorAll("article");
            const scope = articles.length === 1 ? articles[0] : document;
            scope.querySelectorAll("img").forEach((img) => {
                // currentSrc 是浏览器从 srcset/picture 中选中的最终地址，优先取
                for (const a of ["currentSrc", "src", ...attrs]) {
                    const v = img.getAttribute(a) || img.currentSrc;
                    push(v);
                }
            });
            // 部分站点用 CSS 背景图或 data-bg 等属性存放图片，一并兜底
            scope.querySelectorAll("*").forEach((el) => {
                const style = el.getAttribute("style");
                if (style) {
                    const m = style.match(/url\\(["']?([^"')]+)["']?\\)/i);
                    if (m) push(m[1]);
                }
                for (const a of ["data-bg", "data-background", "data-bgimage"]) {
                    const v = el.getAttribute(a);
                    if (v && !v.startsWith("rgb")) push(v);
                }
            });
            return Array.from(urls);
        }"""
        % json.dumps(IMG_ATTRS)
    )
    return urls


async def extract_post_links(page, base_url: str) -> list[str]:
    """
    从页面中提取文章/帖子链接（article 或 h1-h3 内的锚点）。

    Args:
        page: Playwright 的 Page 对象
        base_url: 站点根 URL，用于同域过滤

    Returns:
        过滤后的帖子 URL 列表
    """
    raw_links = await page.evaluate(
        """() => {
            const links = new Set();
            ["article a[href]", "h1 a[href]", "h2 a[href]", "h3 a[href]"]
                .forEach((sel) => {
                    document.querySelectorAll(sel).forEach((a) => {
                        if (a.href) links.add(a.href);
                    });
                });
            return Array.from(links);
        }"""
    )

    base_host = urlparse(base_url).netloc
    result: list[str] = []
    seen: set[str] = set()
    for link in raw_links:
        if urlparse(link).netloc != base_host:
            continue
        if link == base_url or link == base_url.rstrip("/") + "/":
            continue
        path = urlparse(link).path.strip("/").lower()
        segments = [s for s in path.split("/") if s]
        if not segments:
            continue
        if any(seg in NON_POST_SEGMENTS for seg in segments):
            continue
        if link in seen:
            continue
        seen.add(link)
        result.append(link)
    return result


async def scroll_to_trigger_lazy_load(page) -> None:
    """
    逐步向下滚动页面，触发懒加载图片（等价于浏览器手动滚屏）。

    Args:
        page: Playwright 的 Page 对象
    """
    await page.evaluate("window.scrollTo(0, 0);")
    for _ in range(60):  # 最多滚 60 屏
        prev_height = await page.evaluate("document.body.scrollHeight")
        await page.mouse.wheel(0, 900)
        await page.wait_for_timeout(250)
        new_height = await page.evaluate("document.body.scrollHeight")
        if new_height <= prev_height:  # 到底了
            break


def download_images(urls: list[str], output_dir: Path, referer: str,
                    limit: int | None = None) -> tuple[int, int]:
    """
    批量下载图片到指定目录。

    Args:
        urls: 图片 URL 列表
        output_dir: 保存目录
        referer: 请求来源页面，部分 CDN 校验该头
        limit: 最多下载张数，None 表示不限制

    Returns:
        (成功数, 失败数)
    """
    output_dir.mkdir(parents=True, exist_ok=True)
    ok = fail = 0
    for idx, url in enumerate(urls, start=1):
        if limit and idx > limit:
            break
        try:
            resp = requests.get(url, headers={**HEADERS, "Referer": referer},
                                timeout=30, stream=True)
            resp.raise_for_status()
            ext = _guess_extension(resp.headers.get("Content-Type", ""),
                                   urlparse(url).path)
            name = SAFE_NAME_RE.sub("_", urlparse(url).path.split("/")[-1])
            if not name:
                name = f"img_{idx}"
            if not name.lower().endswith(ext):
                name += ext
            path = output_dir / name
            size = 0
            with open(path, "wb") as f:
                for chunk in resp.iter_content(chunk_size=8192):
                    f.write(chunk)
                    size += len(chunk)
            ok += 1
            print(f"    [{idx}/{len(urls)}] {name} ({size} bytes)")
        except Exception as e:
            fail += 1
            print(f"    [{idx}/{len(urls)}] 失败: {url} -> {e}")
    return ok, fail


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
    return ".img"


def post_slug(url: str) -> str:
    """
    从帖子 URL 中提取用于目录命名的 slug。

    Args:
        url: 帖子 URL

    Returns:
        目录名（如 "61417DuoC1814"）
    """
    seg = [s for s in urlparse(url).path.strip("/").split("/") if s]
    return SAFE_NAME_RE.sub("_", seg[-1] or "post") if seg else "post"


async def open_page(page, url: str) -> None:
    """
    打开页面并等待稳定（含滚动触发懒加载）。

    Args:
        page: Playwright 的 Page 对象
        url: 要打开的网址
    """
    print(f">>> 打开页面: {url}")
    try:
        await page.goto(url, wait_until="domcontentloaded", timeout=60000)
        await page.wait_for_load_state("networkidle", timeout=45000)
    except Exception as e:
        print(f"  页面加载异常（继续尝试）: {e}")
    await scroll_to_trigger_lazy_load(page)


async def crawl_single_page(url: str, output: str, limit: int | None) -> None:
    """
    单页模式：只下载当前页面全部图片。

    Args:
        url: 目标网页
        output: 图片保存目录
        limit: 下载张数上限
    """
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context(
            user_agent=HEADERS["User-Agent"],
            viewport={"width": 1366, "height": 900},
            locale="zh-CN",
        )
        page = await context.new_page()
        await open_page(page, url)
        raw_urls = await extract_image_urls_from_page(page)
        await browser.close()

    urls = normalize_urls(url, raw_urls)
    print(f">>> 共发现 {len(urls)} 张图片")
    if not urls:
        print("未发现图片，请检查页面是否需要登录或反爬验证。")
        return

    ok, fail = download_images(urls, Path(output), referer=url, limit=limit)
    print(f"完成：成功 {ok} 张，失败 {fail} 张，保存目录: {output}")


async def crawl_post_gallery(url: str, output: str, max_posts: int,
                             limit: int | None) -> None:
    """
    帖子女集模式：跟进首页帖子链接，下载每篇完整图集。

    Args:
        url: 站点首页
        output: 总保存目录（每个帖子一个子目录）
        max_posts: 最多跟进多少篇帖子
        limit: 每篇帖子最多下载张数
    """
    output_dir = Path(output)
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context(
            user_agent=HEADERS["User-Agent"],
            viewport={"width": 1366, "height": 900},
            locale="zh-CN",
        )
        page = await context.new_page()

        await open_page(page, url)
        post_urls = await extract_post_links(page, url)
        await browser.close()

    post_urls = post_urls[:max_posts]
    print(f">>> 发现 {len(post_urls)} 篇帖子链接，开始逐篇下载")

    total_ok = total_fail = 0
    for idx, post_url in enumerate(post_urls, start=1):
        print(f"\n[{idx}/{len(post_urls)}] {post_url}")
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True)
            context = await browser.new_context(
                user_agent=HEADERS["User-Agent"],
                viewport={"width": 1366, "height": 900},
                locale="zh-CN",
            )
            page = await context.new_page()
            await open_page(page, post_url)
            raw_urls = await extract_image_urls_from_page(page)
            await browser.close()

        urls = normalize_urls(post_url, raw_urls)
        print(f"  该帖发现 {len(urls)} 张图片")
        if not urls:
            continue

        post_dir = output_dir / post_slug(post_url)
        ok, fail = download_images(urls, post_dir, referer=post_url, limit=limit)
        total_ok += ok
        total_fail += fail

    print(f"\n全部完成：成功 {total_ok} 张，失败 {total_fail} 张，保存根目录: {output}")


def main() -> None:
    """命令行入口。"""
    parser = argparse.ArgumentParser(description="网页图片批量下载")
    parser.add_argument("--url", required=True, help="目标网页 URL")
    parser.add_argument("--output", default="images", help="图片保存目录")
    parser.add_argument("--limit", type=int, default=None, help="每个页面最多下载张数")
    parser.add_argument("--max-posts", type=int, default=0,
                        help="跟进首页中最多 N 篇帖子下载完整图集（0=仅单页模式）")
    args = parser.parse_args()

    if args.max_posts > 0:
        asyncio.run(crawl_post_gallery(args.url, args.output,
                                       args.max_posts, args.limit))
    else:
        asyncio.run(crawl_single_page(args.url, args.output, args.limit))


if __name__ == "__main__":
    main()
