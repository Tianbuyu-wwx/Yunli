import random
import time
from typing import Dict, Optional

from . import filters
from .language import SWORD_KEYWORDS, FOOD_KEYWORDS, PLAY_KEYWORDS, INTIMACY_KEYWORDS, HELP_KEYWORDS


class EmotionStateMachine:
    """云璃情感状态机"""

    EMOTION_STATES = {
        "neutral": {"intensity": 0.3, "description": "平静"},
        "excited": {"intensity": 0.8, "description": "兴奋"},
        "annoyed": {"intensity": 0.6, "description": "不耐烦"},
        "tsundere": {"intensity": 0.5, "description": "傲娇"},
        "sad_guarded": {"intensity": 0.4, "description": "悲伤但掩饰"},
        "serious": {"intensity": 0.7, "description": "认真"},
        "curious": {"intensity": 0.6, "description": "好奇"},
        "happy": {"intensity": 0.7, "description": "开心"},
        "bored": {"intensity": 0.3, "description": "无聊"},
    }

    TRANSITIONS = {
        "sword_mentioned": "excited",
        "food_mentioned": "happy",
        "praised": "tsundere",
        "thanked": "tsundere",
        "insulted": "annoyed",
        "sad_topic": "sad_guarded",
        "mission_mentioned": "serious",
        "new_thing": "curious",
        "boring_chat": "bored",
        "joke_made": "happy",
    }

    def __init__(self, config: dict = None):
        self.config = config or {}
        self.current_state = "neutral"
        self.state_history = []
        self.intensity = 0.5
        self.state_duration = 0  # 当前状态持续时间
        self.max_duration = self.config.get("emotion_max_duration", 3)  # 最大持续轮数

    def transition(self, trigger: str, context: str = ""):
        """根据触发器转换情感状态"""
        new_state = self.TRANSITIONS.get(trigger, "neutral")

        if new_state != self.current_state:
            self.state_history.append(
                {"state": self.current_state, "duration": self.state_duration}
            )
            self.current_state = new_state
            self.state_duration = 0
            self.intensity = self.EMOTION_STATES[new_state]["intensity"]
        else:
            self.state_duration += 1

    def auto_decay(self):
        """情感自然衰减"""
        self.state_duration += 1
        if self.state_duration >= self.max_duration and self.current_state != "neutral":
            self.state_history.append(
                {"state": self.current_state, "duration": self.state_duration}
            )
            self.current_state = "neutral"
            self.state_duration = 0
            self.intensity = self.EMOTION_STATES["neutral"]["intensity"]

    def get_current_state_description(self) -> str:
        """获取当前状态描述"""
        state_info = self.EMOTION_STATES.get(
            self.current_state, self.EMOTION_STATES["neutral"]
        )
        return f"{self.current_state}({state_info['description']}, 强度{state_info['intensity']})"

    def get_emotion_for_response(self, db) -> Dict:
        """获取用于响应的情感数据"""
        templates = db.query_emotion_templates(self.current_state, limit=10)

        prefixes = [t for t in templates if t.get("template_type") == "prefix"]
        suffixes = [t for t in templates if t.get("template_type") == "suffix"]
        standalones = [t for t in templates if t.get("template_type") == "standalone"]

        return {
            "state": self.current_state,
            "intensity": self.intensity,
            "prefix": random.choice(prefixes)["content"] if prefixes else "",
            "suffix": random.choice(suffixes)["content"] if suffixes else "",
            "standalone": random.choice(standalones)["content"] if standalones else "",
        }

    def detect_trigger(self, text: str) -> Optional[str]:
        """从文本中检测情感触发器"""
        triggers = {
            "sword_mentioned": SWORD_KEYWORDS,
            "food_mentioned": FOOD_KEYWORDS,
            "praised": ["厉害", "强", "棒", "帅", "可爱", "喜欢", "爱你"],
            "thanked": ["谢谢", "感谢", "多亏", "帮大忙"],
            "insulted": ["笨", "蠢", "弱", "菜", "废物", "垃圾"],
            "sad_topic": ["死", "离别", "失去", "痛苦", "悲伤", "哭"],
            "mission_mentioned": ["魔剑", "任务", "使命", "猎剑", "责任"],
            "new_thing": ["新", "第一次", "没见过", "是什么", "介绍一下"],
            "boring_chat": ["无聊", "没事", "随便", "发呆"],
            "joke_made": PLAY_KEYWORDS,
        }

        for trigger, keywords in triggers.items():
            if any(kw in text for kw in keywords):
                return trigger
        return None

    def inject_emotion(self, text: str, db) -> str:
        """根据当前情感状态润色文本

        只通过纯文字语气词和标点微妙地体现情绪，
        禁止输出任何动作描述、表情符号或情感标签。
        """
        emotion_data = self.get_emotion_for_response(db)

        if emotion_data["state"] == "neutral":
            return text

        # 根据情感状态添加前缀/后缀（纯文字语气词）
        prefix = emotion_data.get("prefix", "")
        suffix = emotion_data.get("suffix", "")

        # 情感波动：根据连续对话情绪强度变化
        intensity_modifier = 1.0
        if len(self.state_history) >= 2:
            recent_states = [h["state"] for h in self.state_history[-3:]]
            if len(set(recent_states)) > 1:
                intensity_modifier = random.uniform(0.6, 1.4)

        adjusted_intensity = min(emotion_data["intensity"] * intensity_modifier, 1.0)

        # 只添加纯文字语气词前缀（如"哼""哈"），严格过滤任何动作描述
        if prefix and random.random() < adjusted_intensity:
            # 同时检查是否包含任何括号或星号（动作描述格式）
            has_action_format = any(c in prefix for c in "*【】()<>")
            # 使用公共配置过滤禁用词
            has_forbidden_word = any(forbidden in prefix for forbidden in filters.FORBIDDEN_PARTICLE_WORDS)
            # 检查是否已包含相同语气词（避免重复，如"哼，哼"）
            prefix_clean = prefix.strip("，,。.！!？? ")
            text_start = text[:10] if len(text) >= 10 else text
            has_duplicate = prefix_clean and any(
                text_start.startswith(p) for p in [prefix_clean, prefix_clean + "，", prefix_clean + ","]
            )
            if not has_action_format and not has_forbidden_word and not has_duplicate and len(prefix.strip()) <= 10:
                text = prefix + text

        if suffix and random.random() < adjusted_intensity * 0.7:
            # 同时检查是否包含任何括号或星号（动作描述格式）
            has_action_format = any(c in suffix for c in "*【】()<>")
            # 使用公共配置过滤禁用词
            has_forbidden_word = any(forbidden in suffix for forbidden in filters.FORBIDDEN_PARTICLE_WORDS)
            # 检查是否已包含相同语气词（避免重复）
            suffix_clean = suffix.strip("，,。.！!？? ")
            text_end = text[-10:] if len(text) >= 10 else text
            has_duplicate = suffix_clean and any(
                text_end.endswith(p) for p in [suffix_clean, "，" + suffix_clean, "," + suffix_clean]
            )
            if not has_action_format and not has_forbidden_word and not has_duplicate and len(suffix.strip()) <= 10:
                text = text + suffix

        # 情绪传染：如果很兴奋或很生气，可能重复标点（仅限文字标点）
        if adjusted_intensity > 0.7 and random.random() < 0.2:
            text = text.replace("！", "！！", 1)

        return text


class RelationshipManager:
    """关系状态机 - 感知用户边界和互动温度，动态调节回复策略

    关系模式：
    - normal: 正常互动
    - backoff: 用户表达了边界（别烦/闭嘴/吵），需要收敛
    - careful: 用户情绪低落（烦/累/难受），需要温和
    - warming: 互动升温（哈哈/贴贴/喜欢），可以更自然亲近

    每个用户独立维护关系状态，按 group_id + user_id 存储。
    状态会自动衰减回 normal。
    """

    RELATIONSHIP_MODES = {
        "normal": {
            "reply_length_limit": None,       # 不限制
            "hint": "",                        # 无特殊提示
            "decay_seconds": 0,               # 不衰减
        },
        "backoff": {
            "reply_length_limit": 40,          # 回复要短
            "hint": "用户可能在表达边界，回复要短、低压，不要追问，不要主动找话题",
            "decay_seconds": 6 * 3600,         # 6小时后自动恢复
        },
        "careful": {
            "reply_length_limit": 80,          # 回复适中
            "hint": "用户可能有压力或情绪低落，先接住情绪，少讲道理，语气温和",
            "decay_seconds": 2 * 3600,         # 2小时后自动恢复
        },
        "warming": {
            "reply_length_limit": None,        # 不限制
            "hint": "互动有升温，可以更自然亲近一点，偶尔开开玩笑",
            "decay_seconds": 30 * 60,          # 30分钟后自然回落
        },
    }

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
