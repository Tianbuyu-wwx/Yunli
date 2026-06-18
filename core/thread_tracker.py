"""对话线程追踪器

跟踪每个群聊中对每个用户的短期对话线程状态。
用于在动态上下文中注入"上一句说了什么"和"云璃刚才回了什么"，
让 LLM 感知对话的连续性，显著提升连贯性。

线程生命周期：
- 同一 scope 内连续对话累计 thread_turn_count
- 若超过 THREAD_TIMEOUT 秒无新消息，线程自动重置
- 若话题切换，线程 count 也会被重置
"""

import time
from typing import Dict, Optional, Tuple


# 线程超时时间（秒）：超过此时间无新消息则视为线程中断
THREAD_TIMEOUT = 300  # 5 分钟

# 线程最大轮数：超过此轮数后自动衰减历史权重
MAX_THREAD_TURNS = 10

# 群级短期上下文保留的最大轮数
MAX_GROUP_TURNS = 20

# 群级 scope 的特殊后缀，避免与普通 user_id 冲突
GROUP_SCOPE_SUFFIX = "__group__"


class ConversationThreadTracker:
    """对话线程追踪器

    按 scope (group_id:user_id) 维护每个对话线程的最近状态。
    线程状态包括：
    - 用户上一条消息内容
    - 云璃上一条回复内容
    - 对话轮次计数
    - 最后活动时间
    """

    def __init__(self):
        # scope -> {last_user_msg, last_yunli_resp, turn_count, last_active, last_topic}
        self._threads: Dict[str, dict] = {}

    def get_thread(self, scope: str) -> dict:
        """获取指定 scope 的线程状态，自动处理超时重置"""
        now = time.time()
        thread = self._threads.get(scope)

        if thread is None:
            return self._create_empty_thread()

        # 超时检查
        if now - thread.get("last_active", 0) > THREAD_TIMEOUT:
            return self._create_empty_thread()

        return thread

    def record_user_message(
        self, scope: str, message: str, topic: str = ""
    ) -> dict:
        """记录用户消息，返回更新后的线程状态"""
        now = time.time()
        thread = self.get_thread(scope)

        # 话题切换检测：如果新话题与旧话题不同，重置轮次
        old_topic = thread.get("last_topic", "")
        if topic and old_topic and topic != old_topic:
            thread = self._create_empty_thread()

        thread["last_user_msg"] = message
        thread["last_active"] = now
        thread["last_topic"] = topic
        thread["turn_count"] = thread.get("turn_count", 0) + 1

        # 超过最大轮数时衰减（保留最近信息但降低权重）
        if thread["turn_count"] > MAX_THREAD_TURNS:
            thread["turn_count"] = MAX_THREAD_TURNS // 2

        self._threads[scope] = thread
        return thread

    def record_yunli_response(self, scope: str, response: str) -> dict:
        """记录云璃的回复，返回更新后的线程状态"""
        thread = self.get_thread(scope)
        thread["last_yunli_resp"] = response
        thread["last_active"] = time.time()
        self._threads[scope] = thread
        return thread

    def get_thread_context(self, scope: str) -> str:
        """生成线程上下文字符串，用于注入 LLM 提示词"""
        thread = self.get_thread(scope)
        parts = []

        if thread.get("last_user_msg"):
            user_msg = thread["last_user_msg"]
            # 截断过长的消息
            if len(user_msg) > 100:
                user_msg = user_msg[:100] + "…"
            parts.append(f"用户刚才说：{user_msg}")

        if thread.get("last_yunli_resp"):
            yunli_resp = thread["last_yunli_resp"]
            if len(yunli_resp) > 80:
                yunli_resp = yunli_resp[:80] + "…"
            parts.append(f"你刚才回复：{yunli_resp}")

        turn = thread.get("turn_count", 0)
        if turn >= 3:
            parts.append(f"这是本话题的第 {turn} 轮对话，保持风格连贯")

        return "\n".join(parts) if parts else ""

    def record_user_message_to_group(
        self, group_id: str, user_id: str, user_nickname: str, message: str
    ) -> dict:
        """记录用户消息到群级短期上下文

        让云璃在同一个群里和不同用户聊天时，也能看到群里最近发生了什么。
        这与个人线程（group_id:user_id）互补：个人线程保持一对一连续性，
        群级线程保持群内整体连续性。
        """
        scope = f"{group_id}:{GROUP_SCOPE_SUFFIX}"
        now = time.time()
        thread = self.get_thread(scope)

        # 维护最近 N 轮群内对话历史，格式：[(nickname, role, message), ...]
        history = thread.get("group_history", [])
        history.append({
            "nickname": user_nickname or user_id[:4] or "群友",
            "role": "user",
            "message": message,
            "timestamp": now,
        })
        # 只保留最近 MAX_GROUP_TURNS 轮
        if len(history) > MAX_GROUP_TURNS:
            history = history[-MAX_GROUP_TURNS:]

        thread["group_history"] = history
        thread["last_active"] = now
        thread["turn_count"] = thread.get("turn_count", 0) + 1
        self._threads[scope] = thread
        return thread

    def record_yunli_response_to_group(
        self, group_id: str, response: str
    ) -> dict:
        """记录云璃回复到群级短期上下文"""
        scope = f"{group_id}:{GROUP_SCOPE_SUFFIX}"
        now = time.time()
        thread = self.get_thread(scope)

        history = thread.get("group_history", [])
        history.append({
            "nickname": "云璃",
            "role": "yunli",
            "message": response,
            "timestamp": now,
        })
        if len(history) > MAX_GROUP_TURNS:
            history = history[-MAX_GROUP_TURNS:]

        thread["group_history"] = history
        thread["last_active"] = now
        thread["turn_count"] = thread.get("turn_count", 0) + 1
        self._threads[scope] = thread
        return thread

    def get_group_thread_context(self, group_id: str, max_lines: int = 10) -> str:
        """生成群级短期上下文字符串

        返回群里最近 N 轮对话（包含所有群友和云璃自己），
        用于让云璃感知群内刚刚发生了什么。
        """
        scope = f"{group_id}:{GROUP_SCOPE_SUFFIX}"
        thread = self.get_thread(scope)
        history = thread.get("group_history", [])
        if not history:
            return ""

        # 截断过长的消息
        lines = []
        for item in history[-max_lines:]:
            nickname = item.get("nickname", "群友")
            msg = item.get("message", "")
            if len(msg) > 80:
                msg = msg[:80] + "…"
            role_tag = ""
            if item.get("role") == "yunli":
                role_tag = "（你）"
            lines.append(f"{nickname}{role_tag}: {msg}")

        return "【群里最近的话】\n" + "\n".join(lines)

    def reset_thread(self, scope: str):
        """手动重置线程"""
        self._threads.pop(scope, None)

    def _create_empty_thread(self) -> dict:
        return {
            "last_user_msg": "",
            "last_yunli_resp": "",
            "turn_count": 0,
            "last_active": time.time(),
            "last_topic": "",
            "group_history": [],
        }

    def cleanup_stale(self):
        """清理超时的线程状态（可定期调用）"""
        now = time.time()
        stale = [
            scope
            for scope, thread in self._threads.items()
            if now - thread.get("last_active", 0) > THREAD_TIMEOUT * 2
        ]
        for scope in stale:
            self._threads.pop(scope, None)


# 全局单例
_thread_tracker: Optional[ConversationThreadTracker] = None


def get_thread_tracker() -> ConversationThreadTracker:
    """获取全局线程追踪器单例"""
    global _thread_tracker
    if _thread_tracker is None:
        _thread_tracker = ConversationThreadTracker()
    return _thread_tracker