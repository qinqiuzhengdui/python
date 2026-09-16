"""
豆包 Seed 2.1 多模态 responses 接口测试脚本。

支持两种模式：
  1. 图片理解：输入图片 + 文本问题，返回文字描述
  2. 文生图：输入文本，返回生成的图片（自动保存到本地）

用法:
    $env:ARK_API_KEY = "your_api_key"

    # 文生图（默认）
    python ark_responses_test.py

    # 图片理解
    python ark_responses_test.py --understand
"""

import argparse
import base64
import json
import os
import re
import sys

import requests

# ---------------------------------------------------------------------------
# 常量配置
# ---------------------------------------------------------------------------
ARK_API_BASE = "https://ark.cn-beijing.volces.com/api/v3"
RESPONSES_PATH = "/responses"

MODEL = "doubao-seed-2-1-pro-260628"

# 官方示例图片
DEMO_IMAGE_URL = "https://ark-project.tos-cn-beijing.volces.com/doc_image/ark_demo_img_1.png"

TIMEOUT = 120  # 秒；文生图耗时较长


def get_api_key() -> str:
    """
    从环境变量 ARK_API_KEY 读取 API Key。

    Returns:
        API Key 字符串

    Raises:
        SystemExit: 环境变量未设置时退出
    """
    api_key = os.environ.get("ARK_API_KEY")
    if not api_key:
        raise SystemExit(
            "未找到 ARK_API_KEY 环境变量。\n"
            "请先设置：\n"
            '    $env:ARK_API_KEY = "你的API Key"'
        )
    return api_key


def build_headers(api_key: str) -> dict:
    """
    构造请求头。

    Args:
        api_key: 火山方舟 API Key

    Returns:
        请求头字典
    """
    return {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {api_key}",
    }


def post_responses(api_key: str, payload: dict) -> dict:
    """
    调用 /responses 接口。

    Args:
        api_key: 火山方舟 API Key
        payload: 请求体

    Returns:
        响应 JSON

    Raises:
        requests.HTTPError: 请求失败时抛出（含错误详情）
    """
    url = ARK_API_BASE + RESPONSES_PATH
    response = requests.post(url, headers=build_headers(api_key), json=payload, timeout=TIMEOUT)
    if not response.ok:
        raise requests.HTTPError(f"HTTP {response.status_code}: {response.text}")
    return response.json()


# ---------------------------------------------------------------------------
# 模式一：图片理解
# ---------------------------------------------------------------------------
def build_understand_payload(image_url: str, question: str) -> dict:
    """
    构造图片理解请求体（对应你提供的 curl 参数）。

    Args:
        image_url: 输入图片 URL
        question: 针对图片的问题

    Returns:
        请求体字典
    """
    return {
        "model": MODEL,
        "input": [
            {
                "role": "user",
                "content": [
                    {"type": "input_image", "image_url": image_url},
                    {"type": "input_text", "text": question},
                ],
            }
        ],
    }


def print_understanding_result(data: dict) -> None:
    """
    打印图片理解的文字输出。

    Args:
        data: /responses 响应 JSON
    """
    print("\n===== 图片理解结果 =====")
    outputs = data.get("output", [])
    found_text = False
    for item in outputs:
        if item.get("type") == "output_text":
            found_text = True
            print(item.get("text", ""))
    if not found_text:
        print(json.dumps(data, ensure_ascii=False, indent=2))


def run_understand(api_key: str) -> None:
    """执行图片理解测试。"""
    print(">>> 提交图片理解请求...")
    data = post_responses(api_key, build_understand_payload(DEMO_IMAGE_URL, "你看见了什么？"))
    print_understanding_result(data)


# ---------------------------------------------------------------------------
# 模式二：文生图
# ---------------------------------------------------------------------------
def build_generate_payload(text: str) -> dict:
    """
    构造文生图请求体。

    注意：doubao-seed-2-1-pro 的 /responses 接口不接受 output 字段，
    模型会根据提示词自动调用文生图能力。

    Args:
        text: 图像描述提示词

    Returns:
        请求体字典
    """
    return {
        "model": MODEL,
        "input": [
            {
                "role": "user",
                "content": [
                    {"type": "input_text", "text": text},
                ],
            }
        ],
    }


def extract_image_items(data: dict) -> list:
    """
    从响应中提取所有生成的图片（data URI 或 URL）。

    兼容三种返回结构：
      - output 块 type=output_image，字段 image_url / image
      - output_text 中内嵌 Markdown 图片链接 ![alt](url)

    Args:
        data: /responses 响应 JSON

    Returns:
        图片字符串列表（data: 开头为 base64，http 开头为 URL）
    """
    items = []
    for item in data.get("output", []):
        if not isinstance(item, dict):
            continue
        if item.get("type") == "output_image":
            img = item.get("image_url") or item.get("image")
            if isinstance(img, str) and img:
                items.append(img)
            elif isinstance(img, dict):
                url = img.get("url")
                if isinstance(url, str) and url:
                    items.append(url)
        elif item.get("type") == "output_text":
            # 解析 Markdown 图片语法 ![alt](url)
            for match in re.finditer(r"!\[[^\]]*\]\(([^)]+)\)", item.get("text", "")):
                url = match.group(1).strip()
                if url:
                    items.append(url)
    return items


def save_base64_image(data_uri: str, save_path: str) -> None:
    """
    将 base64 data URI 解码保存为图片文件。

    Args:
        data_uri: 形如 data:image/png;base64,xxxx 的字符串
        save_path: 保存路径

    Raises:
        ValueError: data URI 格式非法
    """
    match = re.match(r"data:image/(\w+);base64,(.*)", data_uri, re.DOTALL)
    if not match:
        raise ValueError("无法解析 base64 图片数据")
    raw = base64.b64decode(match.group(2))
    with open(save_path, "wb") as f:
        f.write(raw)


def download_image(url: str, save_path: str) -> None:
    """
    下载远程图片到本地。

    Args:
        url: 图片 URL
        save_path: 保存路径
    """
    response = requests.get(url, timeout=TIMEOUT)
    response.raise_for_status()
    with open(save_path, "wb") as f:
        f.write(response.content)


def run_generate(api_key: str, text: str, save_path: str, max_attempts: int = 3) -> None:
    """
    执行文生图测试并保存图片。

    注意：doubao-seed-2-1-pro 的文生图行为不稳定，偶发拒答只返回提示词，
    因此加入重试机制，直到取到图片或达到最大尝试次数。

    Args:
        api_key: 火山方舟 API Key
        text: 图像描述提示词
        save_path: 本地保存路径
        max_attempts: 最大尝试次数
    """
    for attempt in range(1, max_attempts + 1):
        print(f">>> 提交文生图请求（第 {attempt}/{max_attempts} 次）...")
        data = post_responses(api_key, build_generate_payload(text))

        items = extract_image_items(data)
        if items:
            for i, img in enumerate(items):
                path = f"{os.path.splitext(save_path)[0]}_{i}.png" if len(items) > 1 else save_path
                if img.startswith("data:"):
                    save_base64_image(img, path)
                else:
                    download_image(img, path)
                print(f"图片已保存: {path}")
            return

        print(f"第 {attempt} 次未生成图片（模型可能拒答），重试...")

    print(f"重试 {max_attempts} 次后仍未生成图片，请更换提示词或改用 /images/generations 接口。")


# ---------------------------------------------------------------------------
# 主入口
# ---------------------------------------------------------------------------
def main() -> None:
    """命令行入口。"""
    parser = argparse.ArgumentParser(description="豆包 Seed 2.1 responses 接口测试")
    parser.add_argument("--understand", action="store_true", help="图片理解模式（默认文生图）")
    parser.add_argument("--text", default="生成一张图片：一只橘猫在草地上打滚，阳光明媚，写实摄影风格",
                        help="文生图的提示词")
    parser.add_argument("--output", default="seed_output.png", help="文生图保存路径")
    parser.add_argument("--retries", type=int, default=3, help="文生图失败重试次数")
    args = parser.parse_args()

    api_key = get_api_key()

    if args.understand:
        run_understand(api_key)
    else:
        run_generate(api_key, args.text, args.output, args.retries)


if __name__ == "__main__":
    main()
