"""统一 LLM 调用客户端

消除 darwin_evolve.py 和 pattern_discovery.py 中的 LLM 调用代码克隆，
统一 Provider 调用、HTTP fallback、超时保护和响应解析。

职责：
1. Provider.text_chat 调用（含事件循环适配 + temperature 支持）
2. HTTP API fallback（DeepSeek 兼容）
3. 超时保护（TimeoutError 捕获 + coroutine 取消）
4. 响应文本提取（completion_text / str / 兜底）

安全：
- API Key 仅从环境变量读取，不存储在 config 字典中
- 日志中对 API Key 脱敏处理
"""

import asyncio
import inspect
import json
import os
import threading
import urllib.error
from typing import Optional


def _mask_api_key(key: str) -> str:
    """对 API Key 脱敏：仅显示前 4 位和后 2 位，中间用 *** 替代

    Examples:
        "sk-1234567890abcdef" → "sk-1***ef"
        "short" → "sh***t"
        "" → "<empty>"
    """
    if not key:
        return "<empty>"
    if len(key) <= 6:
        return f"{key[:2]}***"
    return f"{key[:4]}***{key[-2:]}"


class LLMClient:
    """统一 LLM 调用接口

    使用方式：
        client = LLMClient(provider, config, log_callback)
        response = client.call("prompt", "system", temperature=0.2)
    """

    def __init__(self, provider=None, config: dict = None, log_callback=None):
        self.provider = provider
        self.config = config or {}
        self._log = log_callback or print

        # 独立的线程事件循环，用于在任意线程中安全调用异步 provider
        # 避免在已有事件循环的线程中调用 asyncio.run() 导致崩溃
        self._loop = None
        self._loop_thread = None
        self._loop_ready = threading.Event()

    def _ensure_loop(self):
        """确保后台事件循环已启动（惰性初始化，线程安全）"""
        if self._loop is not None and self._loop.is_running():
            return

        def _run_loop():
            self._loop = asyncio.new_event_loop()
            asyncio.set_event_loop(self._loop)
            self._loop_ready.set()
            self._loop.run_forever()

        self._loop_thread = threading.Thread(
            target=_run_loop, daemon=True, name="LLMClient-EventLoop",
        )
        self._loop_thread.start()
        self._loop_ready.wait(timeout=self.config.get("llm_event_loop_startup_timeout", 5))

    def _shutdown_loop(self):
        """关闭后台事件循环（可选，daemon 线程会随主线程退出）"""
        if self._loop is not None and self._loop.is_running():
            self._loop.call_soon_threadsafe(self._loop.stop)
            if self._loop_thread is not None:
                self._loop_thread.join(timeout=self.config.get("llm_event_loop_shutdown_timeout", 3))
            self._loop = None

    @property
    def timeout(self) -> int:
        """LLM 调用超时时间（秒），可配置"""
        return self.config.get("evolution_llm_timeout", 120)

    @property
    def max_tokens(self) -> int:
        """LLM 最大输出 token 数，可配置"""
        return self.config.get("evolution_max_tokens", 4096)

    @property
    def default_model(self) -> str:
        """默认模型名称，可配置"""
        return self.config.get("evolution_model", "deepseek-chat")

    def call(self, prompt: str, system_prompt: str = "",
             temperature: float = None) -> Optional[str]:
        """同步调用 LLM

        调用策略：
        1. 优先用 AstrBot provider（支持 temperature 参数）
        2. 若 provider 不可用，回退到 HTTP API
        3. 若都不可用，返回 None

        Args:
            prompt: 用户提示词
            system_prompt: 系统提示词
            temperature: 温度参数（评分 0.2 / 改进 0.7 / 探索 0.9）
        """
        if self.provider is None:
            self._log("  [LLMClient] provider 为空，尝试 HTTP fallback")
            return self._call_http(prompt, system_prompt, temperature)

        if not hasattr(self.provider, "text_chat"):
            self._log(f"  [LLMClient] provider ({type(self.provider).__name__}) 没有 text_chat 方法")
            return self._call_http(prompt, system_prompt, temperature)

        try:
            response = self._invoke_provider(prompt, system_prompt, temperature)
            return self._extract_text(response)

        except (RuntimeError, asyncio.TimeoutError, TypeError, ValueError) as e:
            self._log(f"  [LLMClient] provider.text_chat 失败: {type(e).__name__}: {e}")
            return self._call_http(prompt, system_prompt, temperature)

    def _invoke_provider(self, prompt: str, system_prompt: str,
                         temperature: float = None):
        """调用 provider.text_chat，自动适配同步/异步 provider

        兼容：
        - async provider.text_chat(...) → 在后台事件循环中执行
        - sync provider.text_chat(...) → 直接同步调用
        """
        # 判断 provider.text_chat 是协程函数还是普通函数
        text_chat = self.provider.text_chat
        is_async = inspect.iscoroutinefunction(text_chat)

        if not is_async:
            # 同步 provider：直接调用
            try:
                return text_chat(
                    prompt=prompt,
                    system_prompt=system_prompt,
                    temperature=temperature,
                )
            except TypeError:
                # 不支持 temperature 参数，降级
                return text_chat(
                    prompt=prompt,
                    system_prompt=system_prompt,
                )

        # 异步 provider：在后台事件循环中执行
        async def _call():
            try:
                return await text_chat(
                    prompt=prompt,
                    system_prompt=system_prompt,
                    temperature=temperature,
                )
            except TypeError:
                # provider 不接受 temperature 参数，降级
                return await text_chat(
                    prompt=prompt,
                    system_prompt=system_prompt,
                )

        return self._call_async_safe(_call())

    def _call_async_safe(self, coro):
        """在后台事件循环中安全执行协程，带超时保护

        策略：
        1. 如果当前线程有运行中的事件循环，使用自有后台循环避免死锁
        2. 如果当前线程无循环，使用自有的后台事件循环执行
        3. 两种方式都有超时保护
        """
        import concurrent.futures

        # 检测当前线程是否有运行中的事件循环
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            pass  # 当前线程无事件循环
        else:
            self._log("  [LLMClient] 检测到事件循环线程内调用，使用后台循环避免死锁")

        # 使用自有的后台事件循环（唯一安全路径）
        self._ensure_loop()
        if self._loop is None or not self._loop.is_running():
            self._log("  [LLMClient] 后台事件循环启动失败，回退到 HTTP")
            return None

        future = asyncio.run_coroutine_threadsafe(coro, self._loop)
        try:
            return future.result(timeout=self.timeout)
        except concurrent.futures.TimeoutError:
            self._log(f"  [LLMClient] 调用超时 ({self.timeout}s)，取消任务")
            future.cancel()
            return None
        except (RuntimeError, asyncio.CancelledError, concurrent.futures.CancelledError) as e:
            self._log(f"  [LLMClient] 异步调用异常: {type(e).__name__}: {e}")
            raise

    def _call_http(self, prompt: str, system_prompt: str = "",
                   temperature: float = None) -> Optional[str]:
        """通过 HTTP API 直接调用 LLM（fallback）

        安全：API Key 仅从环境变量读取，不存储在 config 中。
        """
        api_key = os.environ.get("DEEPSEEK_API_KEY", "")
        api_base = os.environ.get("DEEPSEEK_API_BASE", "https://api.deepseek.com")

        if not api_key:
            self._log("  [LLMClient] 未配置 DEEPSEEK_API_KEY 环境变量，跳过 HTTP fallback")
            return None

        self._log(f"  [LLMClient] HTTP fallback: key={_mask_api_key(api_key)}, base={api_base}")

        try:
            import urllib.request

            messages = []
            if system_prompt:
                messages.append({"role": "system", "content": system_prompt})
            messages.append({"role": "user", "content": prompt})

            body = json.dumps({
                "model": self.default_model,
                "messages": messages,
                "temperature": temperature if temperature is not None else 0.3,
                "max_tokens": self.max_tokens,
            }, ensure_ascii=False)

            req = urllib.request.Request(
                f"{api_base}/chat/completions",
                data=body.encode("utf-8"),
                headers={
                    "Content-Type": "application/json",
                    "Authorization": f"Bearer {api_key}",
                },
            )
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                data = json.loads(resp.read().decode("utf-8"))
                return data["choices"][0]["message"]["content"]

        except (urllib.error.URLError, OSError, json.JSONDecodeError, KeyError, ValueError) as e:
            # 确保异常信息中不包含 API Key
            safe_msg = str(e)
            if api_key in safe_msg:
                safe_msg = safe_msg.replace(api_key, _mask_api_key(api_key))
            self._log(f"  [LLMClient] HTTP fallback 失败: {safe_msg}")
            return None

    @staticmethod
    def _extract_text(response) -> Optional[str]:
        """从 provider 响应中提取文本"""
        if response is None:
            return None
        if hasattr(response, "completion_text"):
            return response.completion_text or None
        if isinstance(response, str):
            return response
        return str(response) if response else None
