"""
豆包 Seedance 视频生成任务测试脚本。

封装火山方舟（Volcengine Ark）contents/generations/tasks 接口：
  1. POST 创建视频生成任务
  2. GET  查询任务状态，轮询直到成功或失败

用法:
    $env:ARK_API_KEY = "your_api_key"
    python ark_video_test.py
"""

import json
import os
import time

import requests

# ---------------------------------------------------------------------------
# 常量配置
# ---------------------------------------------------------------------------
ARK_API_BASE = "https://ark.cn-beijing.volces.com/api/v3"
TASK_PATH = "/contents/generations/tasks"

MODEL = "doubao-seedance-2-5-260628"

# 官方示例素材（图片 / 视频 / 音频）
REF_IMAGE_1 = "https://ark-project.tos-cn-beijing.volces.com/doc_image/r2v_tea_pic1.jpg"
REF_IMAGE_2 = "https://ark-project.tos-cn-beijing.volces.com/doc_image/r2v_tea_pic2.jpg"
REF_VIDEO = "https://ark-project.tos-cn-beijing.volces.com/doc_video/r2v_tea_video1.mp4"
REF_AUDIO = "https://ark-project.tos-cn-beijing.volces.com/doc_audio/r2v_tea_audio1.mp3"

POLL_INTERVAL = 5      # 轮询间隔（秒）
POLL_TIMEOUT = 600     # 最长等待时间（秒）


def build_prompt() -> str:
    """
    拼接视频生成提示词（第一视角果茶广告）。

    Returns:
        完整的提示词文本
    """
    return (
        "全程使用视频1的第一视角构图，全程使用音频1作为背景音乐。"
        "第一人称视角果茶宣传广告，seedance牌「苹苹安安」苹果果茶限定款；"
        "首帧为图片1，你的手摘下一颗带晨露的阿克苏红苹果，轻脆的苹果碰撞声；"
        "2-4 秒：快速切镜，你的手将苹果块投入雪克杯，加入冰块与茶底，用力摇晃，"
        "冰块碰撞声与摇晃声卡点轻快鼓点，背景音：「鲜切现摇」；"
        "4-6 秒：第一人称成品特写，分层果茶倒入透明杯，你的手轻挤奶盖在顶部铺展，"
        "在杯身贴上粉红包标，镜头拉近看奶盖与果茶的分层纹理；"
        "6-8 秒：第一人称手持举杯，你将图片2中的果茶举到镜头前（模拟递到观众面前的视角），"
        "杯身标签清晰可见，背景音「来一口鲜爽」，尾帧定格为图片2。"
        "背景声音统一为女生音色。"
    )


def build_content() -> list:
    """
    构造多模态 content 参数（文本 + 参考图 + 参考视频 + 参考音频）。

    Returns:
        content 列表
    """
    return [
        {"type": "text", "text": build_prompt()},
        {"type": "image_url", "image_url": {"url": REF_IMAGE_1}, "role": "reference_image"},
        {"type": "image_url", "image_url": {"url": REF_IMAGE_2}, "role": "reference_image"},
        {"type": "video_url", "video_url": {"url": REF_VIDEO}, "role": "reference_video"},
        {"type": "audio_url", "audio_url": {"url": REF_AUDIO}, "role": "reference_audio"},
    ]


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
            '    $env:ARK_API_KEY = "你的API Key"\n'
            "或：\n"
            "    set ARK_API_KEY=你的API Key"
        )
    return api_key


def create_task(api_key: str) -> dict:
    """
    创建视频生成任务（对应 POST /contents/generations/tasks）。

    Args:
        api_key: 火山方舟 API Key

    Returns:
        创建任务的响应 JSON（包含任务 id）

    Raises:
        requests.HTTPError: 请求失败时抛出
    """
    payload = {
        "model": MODEL,
        "content": build_content(),
        "generate_audio": True,
        "ratio": "16:9",
        "duration": 11,
        "watermark": False,  # 用户偏好：关闭水印
    }
    url = ARK_API_BASE + TASK_PATH

    print(">>> 提交视频生成任务...")
    response = requests.post(url, headers=build_headers(api_key), json=payload, timeout=30)
    response.raise_for_status()

    data = response.json()
    print(f"<<< 任务已创建，id = {data.get('id')}")
    return data


def query_task(api_key: str, task_id: str) -> dict:
    """
    查询任务状态（对应 GET /contents/generations/tasks/{id}）。

    Args:
        api_key: 火山方舟 API Key
        task_id: 任务 id

    Returns:
        任务状态响应 JSON

    Raises:
        requests.HTTPError: 请求失败时抛出
    """
    url = f"{ARK_API_BASE}{TASK_PATH}/{task_id}"
    response = requests.get(url, headers=build_headers(api_key), timeout=30)
    response.raise_for_status()
    return response.json()


def wait_for_completion(api_key: str, task_id: str) -> dict:
    """
    轮询等待任务完成。

    Args:
        api_key: 火山方舟 API Key
        task_id: 任务 id

    Returns:
        最终任务状态 JSON

    Raises:
        TimeoutError: 超过最大等待时间仍未完成
    """
    elapsed = 0
    while elapsed < POLL_TIMEOUT:
        data = query_task(api_key, task_id)
        status = data.get("status")
        print(f"[{elapsed:>4}s] 状态: {status}")

        if status == "succeeded":
            print(">>> 任务成功！")
            return data
        if status == "failed":
            raise RuntimeError(f"任务失败: {data.get('error', '未知错误')}")

        time.sleep(POLL_INTERVAL)
        elapsed += POLL_INTERVAL

    raise TimeoutError(f"等待 {POLL_TIMEOUT}s 后任务仍未完成")


def extract_video_url(result: dict) -> str:
    """
    从任务结果中提取视频下载链接。

    兼容两种常见返回结构：
      - 顶层字段: {"video_url": "..."} / {"url": "..."}
      - content 列表: {"content": [{"type": "video", "url": "..."}]}

    Args:
        result: 任务最终状态 JSON

    Returns:
        视频 URL；未找到时返回空字符串
    """
    candidates = []

    # 1. 顶层直接字段
    for key in ("video_url", "url"):
        value = result.get(key)
        if isinstance(value, str) and value:
            candidates.append(value)

    # 2. content 列表内嵌
    content = result.get("content")
    if isinstance(content, list):
        for item in content:
            if not isinstance(item, dict):
                continue
            if item.get("type") == "video" or "video" in str(item.get("type", "")):
                url = item.get("url") or item.get("video_url")
                if isinstance(url, str) and url:
                    candidates.append(url)

    return candidates[0] if candidates else ""


def save_result(task_id: str, result: dict) -> str:
    """
    将任务结果完整写入本地 JSON 文件。

    Args:
        task_id: 任务 id（用作文件名）
        result: 任务结果 JSON

    Returns:
        保存的文件路径
    """
    file_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), f"result_{task_id}.json")
    with open(file_path, "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=2)
    return file_path


def print_result(task_id: str, result: dict) -> None:
    """
    打印任务最终结果：提取视频链接并保存完整 JSON。

    Args:
        task_id: 任务 id
        result: 任务最终状态 JSON
    """
    print("\n" + "=" * 50)
    print("任务结果")
    print("=" * 50)

    video_url = extract_video_url(result)
    if video_url:
        print(f"视频链接: {video_url}")
    else:
        print("未在响应中直接找到视频链接，完整响应见下方 JSON")

    file_path = save_result(task_id, result)
    print(f"完整响应已保存: {file_path}")

    print("\n===== 完整 JSON =====")
    print(json.dumps(result, ensure_ascii=False, indent=2))


def main() -> None:
    """主流程：创建任务 -> 轮询 -> 输出结果。"""
    api_key = get_api_key()

    task = create_task(api_key)
    task_id = task.get("id")
    if not task_id:
        raise RuntimeError(f"响应中缺少任务 id: {task}")

    result = wait_for_completion(api_key, task_id)
    print_result(task_id, result)


if __name__ == "__main__":
    main()
