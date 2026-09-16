"""
Kimi（Moonshot 开放平台）异步 API 网关。

职责边界（SRP）：
- 只负责 HTTP 通信：鉴权、超时、限流/5xx 重试、错误码分流、消息封装；
- 不包含任何"筛图/写文案"的业务提示词（见 ai_pipeline.py）；
- 不被任何底层工具反向依赖。

接口为 OpenAI 兼容协议：POST {base_url}/chat/completions。
视觉请求的 message.content 必须是对象数组（image_url + text parts），
不可把数组序列化成字符串（官方文档明确非标准格式不保证生效）。

@example
    async with KimiClient.from_settings() as client:
        text = await client.chat_text("你是助手", "用一句话介绍杭州", "kimi-k3")
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import httpx

from config import Settings, settings

# 可通过 enable_thinking=false 关闭思考的模型前缀
_THINK_TOGGLE_PREFIXES = ("kimi-k2.6", "kimi-k2.5")
# 仅思考模型（通过 reasoning_effort 控制强度）
_THINKING_ONLY_PREFIXES = ("kimi-k3", "kimi-k2.7")

_RETRY_STATUSES = {429, 500, 502, 503, 504}


class KimiError(RuntimeError):
    """Kimi API 调用过程中的可预期错误（鉴权/参数/限流/网络）。"""

    def __init__(self, message: str, *, status_code: int | None = None):
        super().__init__(message)
        self.status_code = status_code


def extract_json_block(text: str) -> Any:
    """
    从模型回复中提取第一个完整 JSON 值（对象或数组）。

    Why 不做正则/字符过滤：模型常带 ```json 代码围栏或前后解释文字，
    且内容含中文；过滤非 ASCII 会直接破坏合法 JSON（历史教训）。
    这里用"括号配平 + 字符串转义感知"的扫描定位完整片段，再交给 json.loads。

    Args:
        text: 模型原始回复文本

    Returns:
        解析后的 dict / list

    Raises:
        KimiError: 未找到 JSON 或 JSON 不合法
    """
    cleaned = text.strip().removeprefix("```json").removeprefix("```").removesuffix("```").strip()
    start = min(
        (cleaned.find(ch) for ch in "[{" if cleaned.find(ch) >= 0),
        default=-1,
    )
    if start < 0:
        raise KimiError(f"模型回复中未找到 JSON 片段: {text[:300]}")

    open_ch, close_ch = cleaned[start], ("}" if cleaned[start] == "{" else "]")
    depth = 0
    in_string = False
    escaped = False
    for index in range(start, len(cleaned)):
        ch = cleaned[index]
        if in_string:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                in_string = False
            continue
        if ch == '"':
            in_string = True
        elif ch == open_ch:
            depth += 1
        elif ch == close_ch:
            depth -= 1
            if depth == 0:
                fragment = cleaned[start:index + 1]
                try:
                    return json.loads(fragment)
                except json.JSONDecodeError as exc:
                    raise KimiError(f"JSON 片段解析失败: {exc}; 片段前 200 字: {fragment[:200]}") from exc
    raise KimiError(f"JSON 片段括号不完整: {cleaned[:300]}")


class KimiClient:
    """Moonshot Chat Completions 的轻量异步客户端（一个 httpx 连接池复用全部调用）。"""

    def __init__(
        self,
        api_key: str,
        base_url: str,
        *,
        timeout: float = 180.0,
        max_retries: int = 2,
        max_tokens: int = 4096,
        reasoning_effort: str = "low",
    ) -> None:
        if not api_key:
            raise KimiError("MOONSHOT_API_KEY 为空，无法初始化 KimiClient")
        self._max_retries = max_retries
        self._max_tokens = max_tokens
        self._reasoning_effort = reasoning_effort
        self._http = httpx.AsyncClient(
            base_url=base_url.rstrip("/"),
            timeout=httpx.Timeout(timeout, connect=15.0),
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            # Why 显式 HTTP/2 关闭：部分代理环境下 h2 协商异常会拖慢首请求
            http2=False,
        )

    @classmethod
    def from_settings(cls, conf: Settings = settings) -> "KimiClient":
        """
        从集中配置构造客户端。

        Args:
            conf: 配置单例（默认读 config.settings）

        Returns:
            已初始化的 KimiClient

        Raises:
            KimiError: API Key 未配置
        """
        if not conf.kimi_enabled:
            raise KimiError(conf.missing_key_hint, status_code=401)
        return cls(
            conf.kimi_api_key,
            conf.kimi_api_base,
            timeout=conf.kimi_timeout_seconds,
            max_retries=conf.kimi_max_retries,
            max_tokens=conf.kimi_max_tokens,
            reasoning_effort=conf.kimi_reasoning_effort,
        )

    async def aclose(self) -> None:
        """关闭底层连接池。"""
        await self._http.aclose()

    async def __aenter__(self) -> "KimiClient":
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.aclose()

    async def chat_text(self, system: str, user: str, model: str, *, temperature: float = 1.0) -> str:
        """
        纯文本对话。

        Args:
            system: 系统提示词（角色与约束）
            user: 用户指令
            model: 模型名（如 kimi-k3）
            temperature: 采样温度；Kimi k2.6/k3 系列强制为 1.0，传其他值会 400

        Returns:
            助手回复正文
        """
        messages = [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ]
        return await self._chat(messages, model, temperature=temperature)

    async def chat_vision(
        self,
        image_data_urls: list[str],
        prompt: str,
        model: str,
        *,
        system: str = "你是 Kimi，擅长图片内容理解与结构化分析。",
        temperature: float = 1.0,
    ) -> str:
        """
        多图视觉对话（图片按列表顺序对应提示词中的图 1..N）。

        Args:
            image_data_urls: 图片 base64 data URL 列表
            prompt: 用户指令（提示词中可用"图 1/图 2"指代顺序）
            model: 视觉模型名（如 kimi-k2.6）
            system: 系统提示词
            temperature: 采样温度；Kimi k2.6/k3 系列强制为 1.0，传其他值会 400

        Returns:
            模型回复正文（调用方按约定解析 JSON）
        """
        if not image_data_urls:
            raise KimiError("chat_vision 至少需要 1 张图片")
        content: list[dict[str, Any]] = [
            {"type": "image_url", "image_url": {"url": url}} for url in image_data_urls
        ]
        content.append({"type": "text", "text": prompt})
        messages = [
            {"role": "system", "content": system},
            {"role": "user", "content": content},
        ]
        return await self._chat(messages, model, temperature=temperature)

    async def _chat(self, messages: list[dict], model: str, *, temperature: float) -> str:
        """
        发起一次带重试的 chat/completions 请求。

        Raises:
            KimiError: 4xx 参数/鉴权错误不重试；重试耗尽后的网络/服务端错误
        """
        payload: dict[str, Any] = {
            "model": model,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": self._max_tokens,
        }
        payload.update(self._thinking_options(model))

        last_error: Exception | None = None
        for attempt in range(self._max_retries + 1):
            try:
                response = await self._http.post("/chat/completions", content=json.dumps(payload))
            except (httpx.TimeoutException, httpx.TransportError) as exc:
                last_error = exc
                if attempt < self._max_retries:
                    await asyncio.sleep(1.5 ** attempt)
                    continue
                raise KimiError(f"连接 Kimi API 失败（重试 {self._max_retries} 次）: {exc}") from exc

            if response.status_code < 400:
                return self._extract_content(response.json())

            # 限流/服务端错误：按 Retry-After 或指数退避重试；其余客户端错误立即失败
            if response.status_code in _RETRY_STATUSES and attempt < self._max_retries:
                delay = self._retry_delay(response, attempt)
                last_error = KimiError(
                    f"Kimi API {response.status_code}: {response.text[:300]}",
                    status_code=response.status_code,
                )
                await asyncio.sleep(delay)
                continue
            raise KimiError(
                f"Kimi API 返回 {response.status_code}: {response.text[:500]}",
                status_code=response.status_code,
            )

        raise KimiError(f"Kimi API 重试耗尽: {last_error}")

    def _thinking_options(self, model: str) -> dict[str, str]:
        """
        按模型族返回思考模式参数。

        Why 分支处理：kimi-k2.6 走 enable_thinking=false 提速省费；
        kimi-k3 为仅思考模型，只能用 reasoning_effort 调强度；
        乱传不支持的参数会被 API 以 400 拒绝。
        """
        if model.startswith(_THINK_TOGGLE_PREFIXES):
            return {"enable_thinking": "false"}
        if model.startswith(_THINKING_ONLY_PREFIXES):
            return {"reasoning_effort": self._reasoning_effort}
        return {}

    @staticmethod
    def _retry_delay(response: httpx.Response, attempt: int) -> float:
        """优先尊重 429 的 Retry-After，否则指数退避。"""
        retry_after = response.headers.get("Retry-After")
        if retry_after:
            try:
                return max(0.0, float(retry_after))
            except ValueError:
                pass
        return 1.5 ** attempt * 2.0

    @staticmethod
    def _extract_content(data: dict[str, Any]) -> str:
        """从标准 OpenAI 兼容响应中取回复正文，结构异常时给出明确错误。"""
        try:
            content = data["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as exc:
            raise KimiError(f"Kimi 响应结构异常: {json.dumps(data, ensure_ascii=False)[:500]}") from exc
        if not isinstance(content, str) or not content.strip():
            raise KimiError("Kimi 回复内容为空（可能被安全策略拦截或触发限流）")
        return content
