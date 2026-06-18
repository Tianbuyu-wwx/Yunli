"""云璃人格 - 关系状态机

从 emotion.py 拆分而来。RelationshipManager 负责感知用户边界和互动温度，
动态调节回复策略；与 EmotionStateMachine（情感状态机）职责不同，独立维护。

关系模式：
- normal: 正常互动
- backoff: 用户表达了边界（别烦/闭嘴/吵），需要收敛
- careful: 用户情绪低落（烦/累/难受），需要温和
- warming: 互动升温（哈哈/贴贴/喜欢），可以更自然亲近

每个用户独立维护关系状态，按 group_id + user_id 存储。
状态会自动衰减回 normal。
"""

import time
from typing import Dict, List, Optional

from .config import (
    INTIMACY_KEYWORDS,
    PLAY_KEYWORDS,
    HELP_KEYWORDS,
    RELATIONSHIP_MODES,
)


class RelationshipManager:
    """关系状态机 - 感知用户边界和互动温度，动态调节回复策略"""

    # 向后兼容：保留类属性引用，避免外部通过 RelationshipManager.RELATIONSHIP_MODES 访问失效
    # 实际定义已迁移至 persona/config.py
    RELATIONSHIP_MODES = RELATIONSHIP_MODES

    # 边界信号词（触发 backoff）
    BOUNDARY_KEYWORDS = [
        "别烦", "闭嘴", "吵死了", "太吵", "别说了", "够了",
        "滚", "走开", "不要你管", "别管我", "烦死了",
        "能不能安静", "你能不能别", "少说话",
    ]

    # 情绪低落信号词（触发 careful）
    CAREFUL_KEYWORDS = [
        "好烦", "好累", "难受", "心累", "emo", "抑郁",
        "不想说话", "不想聊", "心情不好", "不开心",
        "压力好大", "好难", "撑不住了", "崩溃",
    ]

    # 升温信号词（触发 warming）
    WARMING_KEYWORDS = [
        "哈哈", "贴贴", "喜欢", "想你了", "爱你",
        "好可爱", "最棒", "嘿嘿", "么么", "抱抱",
        "好开心", "太好了", "真好",
    ]

    def __init__(self, config: dict = None):
        self.config = config or {}
        # 用户关系状态存储: (group_id, user_id) -> {"mode": str, "entered_at": float}
        self._user_states: Dict[tuple, Dict] = {}
        # 自定义衰减时间倍数
        self._decay_multiplier = self.config.get("relationship_decay_multiplier", 1.0)

    def detect_intent(self, text: str) -> Optional[str]:
        """从用户消息中检测关系意图信号

        Returns:
            "backoff" / "careful" / "warming" / None
        """
        # 优先检测边界信号（最高优先级，一旦触发立即收敛）
        for kw in self.BOUNDARY_KEYWORDS:
            if kw in text:
                return "backoff"

        # 检测情绪低落信号
        for kw in self.CAREFUL_KEYWORDS:
            if kw in text:
                return "careful"

        # 检测升温信号
        for kw in self.WARMING_KEYWORDS:
            if kw in text:
                return "warming"

        return None

    def detect_user_intent(self, text: str) -> str:
        """细粒度用户意图分析

        在关系意图基础上增加更多意图类型，用于影响情感状态和回复模式。

        Returns:
            "boundary" / "comfort" / "play" / "intimacy" / "help" / "chat"
        """
        # 1. 边界意图（最高优先级）
        for kw in self.BOUNDARY_KEYWORDS:
            if kw in text:
                return "boundary"

        # 2. 情绪低落意图
        for kw in self.CAREFUL_KEYWORDS:
            if kw in text:
                return "comfort"

        # 3. 亲密/撒娇意图（引用共享常量）
        for kw in INTIMACY_KEYWORDS:
            if kw in text:
                return "intimacy"

        # 4. 玩乐意图（引用共享常量）
        for kw in PLAY_KEYWORDS:
            if kw in text:
                return "play"

        # 5. 求助意图（引用共享常量）
        for kw in HELP_KEYWORDS:
            if kw in text:
                return "help"

        # 6. 默认闲聊
        return "chat"

    # 意图到情感触发器的映射
    INTENT_TO_EMOTION_TRIGGER = {
        "boundary": "insulted",      # 边界 → 不耐烦
        "comfort": "sad_topic",      # 情绪低落 → 悲伤掩饰
        "play": "joke_made",         # 玩乐 → 开心
        "intimacy": "praised",       # 亲密 → 傲娇
        "help": "mission_mentioned", # 求助 → 认真
        "chat": None,                # 闲聊 → 不触发
    }

    def update(self, group_id: str, user_id: str, text: str) -> str:
        """根据用户消息更新关系状态

        Returns:
            当前关系模式名称
        """
        key = (group_id, user_id)
        intent = self.detect_intent(text)

        if intent and intent in self.RELATIONSHIP_MODES:
            self._user_states[key] = {
                "mode": intent,
                "entered_at": time.time(),
            }

        return self.get_mode(group_id, user_id)

    def get_mode(self, group_id: str, user_id: str) -> str:
        """获取当前关系模式（含自动衰减检查）"""
        key = (group_id, user_id)
        state = self._user_states.get(key)

        if not state:
            return "normal"

        mode = state["mode"]
        mode_config = self.RELATIONSHIP_MODES[mode]
        decay_seconds = mode_config["decay_seconds"] * self._decay_multiplier

        # 检查是否已衰减
        if decay_seconds > 0:
            elapsed = time.time() - state["entered_at"]
            if elapsed >= decay_seconds:
                # 自动衰减回 normal
                del self._user_states[key]
                return "normal"

        return mode

    def get_hint(self, group_id: str, user_id: str) -> str:
        """获取当前关系模式的提示词"""
        mode = self.get_mode(group_id, user_id)
        return self.RELATIONSHIP_MODES[mode]["hint"]

    def get_reply_length_limit(self, group_id: str, user_id: str) -> Optional[int]:
        """获取当前关系模式的回复长度限制

        Returns:
            字符数上限，None 表示不限制
        """
        mode = self.get_mode(group_id, user_id)
        return self.RELATIONSHIP_MODES[mode]["reply_length_limit"]

    def get_particle_multiplier(self, group_id: str, user_id: str) -> float:
        """获取当前关系模式的语气词概率倍率

        Returns:
            倍率值（0.0-2.0），用于调整 _add_emotion_particles 的 base_probability
        """
        if not group_id or not user_id:
            return 1.0
        mode = self.get_mode(group_id, user_id)
        return self.RELATIONSHIP_MODES[mode].get("particle_multiplier", 1.0)

    def get_emoji_multiplier(self, group_id: str, user_id: str) -> float:
        """获取当前关系模式的颜文字概率倍率

        Returns:
            倍率值（0.0-2.0），用于调整 _add_qq_emoji 的判定概率
        """
        if not group_id or not user_id:
            return 1.0
        mode = self.get_mode(group_id, user_id)
        return self.RELATIONSHIP_MODES[mode].get("emoji_multiplier", 1.0)

    def force_set_mode(self, group_id: str, user_id: str, mode: str):
        """强制设置关系模式（用于测试或管理命令）"""
        if mode in self.RELATIONSHIP_MODES:
            key = (group_id, user_id)
            if mode == "normal":
                self._user_states.pop(key, None)
            else:
                self._user_states[key] = {
                    "mode": mode,
                    "entered_at": time.time(),
                }

    def cleanup_expired(self):
        """清理所有已衰减的关系状态"""
        now = time.time()
        expired_keys = []
        for key, state in self._user_states.items():
            mode = state["mode"]
            decay_seconds = self.RELATIONSHIP_MODES[mode]["decay_seconds"] * self._decay_multiplier
            if decay_seconds > 0 and (now - state["entered_at"]) >= decay_seconds:
                expired_keys.append(key)
        for key in expired_keys:
            del self._user_states[key]
