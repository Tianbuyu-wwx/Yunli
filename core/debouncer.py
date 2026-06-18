"""云璃插件 - 消息防抖模块

首条消息立即处理，同用户短时间内的后续消息合并处理。
"""
import asyncio
import time
import logging
from typing import Any, Callable, Dict, Optional

from .utils import merge_messages

logger = logging.getLogger(__name__)


class MessageDebouncer:
    """消息防抖器

    首条消息立即处理；同 scope（群号+用户ID）在防抖窗口内的
    后续消息合并为一条处理。新消息会重置窗口计时器。

    Args:
        debounce_seconds: 防抖窗口（秒），窗口期内新消息合并
        max_wait_seconds: 最大等待时间（秒），超过则强制触发
        on_flush: 回调函数，签名 async def on_flush(scope, event, req)
    """

    def __init__(
        self,
        debounce_seconds: float = 3.0,
        max_wait_seconds: float = 8.0,
        on_flush: Optional[Callable] = None,
        on_individual_message: Optional[Callable] = None,
    ):
        self._debounce_seconds = debounce_seconds
        self._max_wait_seconds = max_wait_seconds
        self._on_flush = on_flush
        # 合并消息时，对每条原始消息单独调用此回调（用于记忆提取）
        # 避免"我喜欢猫"+"我喜欢狗"合并后只提取出一条偏好
        # 签名: async def on_individual_message(event, req)
        self._on_individual_message = on_individual_message
        # 缓冲区: scope -> {events, reqs, task, first_ts}
        self._buffer: Dict[str, Dict] = {}
        # 上次立即处理时间: scope -> timestamp
        self._last_process_time: Dict[str, float] = {}
        # P2-5 修复：缓冲区 TTL，超过 5 分钟的缓冲区强制清理
        self._buffer_ttl_seconds = 300
        self._lock = asyncio.Lock()

    async def handle_message(self, scope: str, event: Any, req: Any) -> bool:
        """处理消息

        Args:
            scope: 去重范围（如 "group_id:user_id"）
            event: AstrMessageEvent
            req: ProviderRequest

        Returns:
            True  → 消息已缓冲，调用方应不再继续处理
            False → 消息应立即处理（首条或窗口已过期）
        """
        if self._debounce_seconds <= 0:
            return False

        async with self._lock:
            now = time.time()

            # P2-5 修复：清理超过 TTL 的过期缓冲区，避免内存泄漏
            expired_scopes = [
                s for s, buf in self._buffer.items()
                if now - buf.get("first_ts", now) > self._buffer_ttl_seconds
            ]
            for s in expired_scopes:
                old_task = self._buffer[s].get("task")
                if old_task and not old_task.done():
                    old_task.cancel()
                del self._buffer[s]
                self._last_process_time.pop(s, None)
                logger.debug("清理过期防抖缓冲区: %s", s)

            last_ts = self._last_process_time.get(scope, 0)

            # 窗口已过期（距上次处理超过 debounce_seconds）→ 立即处理
            if now - last_ts > self._debounce_seconds:
                # 注意：不在此更新 _last_process_time，由调用方在处理成功后
                # 调用 mark_processed() 更新，避免处理失败超时导致下条消息被错误防抖
                self._buffer.pop(scope, None)
                return False

            # 窗口期内 → 缓冲合并
            buffer = self._buffer.get(scope)
            if buffer is None:
                self._buffer[scope] = {
                    "events": [event],
                    "reqs": [req],
                    "first_ts": now,
                    "task": asyncio.create_task(self._process_window(scope)),
                }
            else:
                buffer["events"].append(event)
                buffer["reqs"].append(req)
                old_task = buffer["task"]
                if old_task and not old_task.done():
                    old_task.cancel()
                buffer["task"] = asyncio.create_task(self._process_window(scope))

            return True

    async def _process_window(self, scope: str):
        """处理防抖窗口：等待后合并发送"""
        try:
            await asyncio.sleep(self._debounce_seconds)

            async with self._lock:
                buffer = self._buffer.pop(scope, None)
                if not buffer:
                    return
                events = buffer["events"]
                reqs = buffer["reqs"]

            if not events or not reqs or not self._on_flush:
                return

            # 合并前：对每条原始消息单独触发记忆提取
            # 避免"我喜欢猫"+"我喜欢狗"合并后只提取出一条偏好
            if self._on_individual_message and len(events) > 1:
                for evt, req in zip(events, reqs):
                    try:
                        await self._on_individual_message(evt, req)
                    except Exception as e:
                        logger.debug("单条消息记忆提取失败: %s", e)

            # 合并消息内容
            primary_event = events[0]
            primary_req = reqs[0]

            if len(events) > 1:
                messages = [evt.message_str or "" for evt in events if evt.message_str]
                merged = merge_messages(messages)
                original_prompt = primary_req.prompt or ""
                if isinstance(original_prompt, str):
                    primary_req.prompt = original_prompt + "\n\n" + merged
                elif isinstance(original_prompt, list):
                    primary_req.prompt = original_prompt + [merged]

            # 调用回调处理
            await self._on_flush(scope, primary_event, primary_req)

            # 回调成功后才更新最后处理时间
            self._last_process_time[scope] = time.time()

            # 标记其他请求为已合并
            for req in reqs[1:]:
                req._yunli_debounce_merged = True

        except asyncio.CancelledError:
            pass
        except Exception as e:
            logger.error("防抖处理失败: %s", e)
            try:
                async with self._lock:
                    buffer = self._buffer.pop(scope, None)
                    if buffer and buffer.get("events") and self._on_flush:
                        await self._on_flush(
                            scope, buffer["events"][0], buffer["reqs"][0]
                        )
                        self._last_process_time[scope] = time.time()
            except Exception as e2:
                logger.error("防抖兜底处理失败: %s", e2)

    def is_buffered(self, req: Any) -> bool:
        """检查请求是否已被防抖缓冲（调用方检查后应跳过处理）"""
        return getattr(req, '_yunli_debounce_buffered', False)

    def is_merged(self, req: Any) -> bool:
        """检查请求是否已被合并（来自其他消息的合并）"""
        return getattr(req, '_yunli_debounce_merged', False)

    def mark_processed(self, scope: str):
        """标记 scope 的消息已成功处理，记录处理时间戳

        必须在调用方成功完成消息处理后调用，确保处理失败不会错误地阻止下一条消息。
        """
        self._last_process_time[scope] = time.time()

    def clear(self):
        """清理所有缓冲"""
        self._buffer.clear()