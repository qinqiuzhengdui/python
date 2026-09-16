"""
将目标网址渲染为 PDF。

基于 Playwright + Chromium 无头浏览器，可执行页面 JS，
支持需要登录跳转（SSO）的场景（通过 --wait 等待登录完成）。

用法:
    python url_to_pdf.py --url "https://example.com" --output page.pdf

    # 页面需要登录时：打开带界面的浏览器手动登录，登录完成后按回车继续
    python url_to_pdf.py --url "https://example.com" --interactive
"""

import argparse
import asyncio
import re
from urllib.parse import urlparse

from playwright.async_api import async_playwright


def derive_filename(url: str) -> str:
    """
    根据 URL 自动生成 PDF 文件名。

    规则：主机名 + 路径第一段拼接，剔除非法字符。
        例如 https://www.diction-style.com/index/home?channel=20860
        -> diction-style_index.pdf

    Args:
        url: 目标网址

    Returns:
        建议的 PDF 文件名
    """
    parsed = urlparse(url)
    host = parsed.netloc.replace("www.", "")
    segments = [seg for seg in parsed.path.split("/") if seg]
    name = re.sub(r"[^\w\-.]", "_", "_".join([host] + segments[:1]))
    return f"{name or 'page'}.pdf"


async def url_to_pdf(url: str, output: str, interactive: bool = False,
                     wait_seconds: int = 3) -> None:
    """
    将 URL 渲染为 PDF。

    Args:
        url: 目标网址
        output: PDF 输出路径
        interactive: 为 True 时使用有头浏览器，等待手动登录后按回车继续
        wait_seconds: 页面加载后的稳定等待秒数（非交互模式）
    """
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=not interactive)
        context = await browser.new_context(
            viewport={"width": 1280, "height": 800},
            locale="zh-CN",
        )
        page = await context.new_page()

        print(f">>> 打开页面: {url}")
        try:
            await page.goto(url, wait_until="domcontentloaded", timeout=60000)
        except Exception as e:
            print(f"页面加载超时/异常（可能仍在跳转）: {e}")

        if interactive:
            input(">>> 请在弹出的浏览器中完成登录，登录成功后按回车继续...")
        else:
            await asyncio.sleep(wait_seconds)

        # 等待跳转稳定后再渲染
        try:
            await page.wait_for_load_state("networkidle", timeout=30000)
        except Exception:
            print("networkidle 等待超时，使用当前状态渲染")

        print(f">>> 当前最终地址: {page.url}")
        await page.pdf(path=output, format="A4", print_background=True)
        await browser.close()

        print(f"PDF 已生成: {output}")


def main() -> None:
    """命令行入口。"""
    parser = argparse.ArgumentParser(description="URL 转 PDF")
    parser.add_argument("--url", required=True, help="目标网址")
    parser.add_argument("--output", default=None, help="PDF 输出路径（默认按 URL 自动命名）")
    parser.add_argument("--interactive", action="store_true",
                        help="使用有头浏览器，手动登录后按回车继续")
    parser.add_argument("--wait", type=int, default=3, help="页面稳定等待秒数")
    args = parser.parse_args()

    output = args.output or derive_filename(args.url)
    asyncio.run(url_to_pdf(args.url, output, args.interactive, args.wait))


if __name__ == "__main__":
    main()



'''
usage:

# 普通转换（不传 --output 时按 URL 自动命名，如 diction-style_index.pdf）
& "c:/Users/admin/Desktop/python/.venv/Scripts/python.exe" "c:/Users/admin/Desktop/python/url_to_pdf.py" --url "目标网址"

# 指定输出文件名
& "c:/Users/admin/Desktop/python/.venv/Scripts/python.exe" "c:/Users/admin/Desktop/python/url_to_pdf.py" --url "目标网址" --output "我的页面.pdf"

# 页面需要登录时：弹出真实浏览器手动登录，登录后按回车继续
& "c:/Users/admin/Desktop/python/.venv/Scripts/python.exe" "c:/Users/admin/Desktop/python/url_to_pdf.py" --url "目标网址" --interactive
'''





