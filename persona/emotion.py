import random
from typing import Dict, List, Optional

# Darwin 进化后的情感触发器占位（运行时通过 overlay 动态生效）
EMOTION_TRIGGERS = """
"""

from . import filters
from .config import (
    SWORD_KEYWORDS,
    FOOD_KEYWORDS,
    PLAY_KEYWORDS,
    RELATIONSHIP_MODES,
)


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

    # state_history 最大保留条数，防止内存泄漏
    MAX_STATE_HISTORY = 100

    def __init__(self, config: dict = None):
        self.config = config or {}
        self.current_state = "neutral"
        self.state_history = []
        self.intensity = 0.5
        self.state_duration = 0  # 当前状态持续时间
        self.max_duration = self.config.get("emotion_max_duration", 3)  # 最大持续轮数

        # ========== 复合情感支持（Phase 1 P1 改进） ==========
        # 复合情感：除主情感外，可同时存在副情感（如"开心+傲娇"）
        self.compound_emotions: Dict[str, float] = {}  # 副情感标签 -> 强度
        self.compound_max = self.config.get("emotion_compound_max", 3)  # 最多保留几个副情感
        # 情感平滑过渡：新状态 = 旧状态 * blend_weight + 新状态 * (1 - blend_weight)
        self.blend_weight = self.config.get("emotion_blend_weight", 0.3)  # 旧状态保留权重（0~1，越大越平滑）

        # 实例级 transitions，支持运行时 overlay 覆盖目标情感
        self.transitions = dict(self.TRANSITIONS)
        self._load_emotion_overlay()

    def _load_emotion_overlay(self):
        """从运行时 overlay 加载进化后的情感触发器映射

        overlay 格式（纯文本）：
            # 注释
            trigger_name -> target_emotion
        """
        try:
            from ..evolution import asset_bridge
            overlay_text = asset_bridge.get_runtime_overlay("emotion_templates")
            if not overlay_text:
                return
        except (ImportError, OSError):
            return

        for line in overlay_text.splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if "->" not in line:
                continue
            parts = line.split("->", 1)
            if len(parts) != 2:
                continue
            trigger = parts[0].strip()
            target = parts[1].strip()
            if trigger and target:
                self.transitions[trigger] = target

    def _record_history(self, state: str, duration: int):
        """记录状态历史（带容量限制）"""
        self.state_history.append({"state": state, "duration": duration})
        # 超限时裁剪，只保留最近的记录
        if len(self.state_history) > self.MAX_STATE_HISTORY:
            self.state_history = self.state_history[-self.MAX_STATE_HISTORY:]

    def transition(self, trigger: str, context: str = ""):
        """根据触发器转换情感状态

        支持复合情感：
        - 若新状态与当前状态不同，新状态成为主情感，旧主情感降级为副情感
        - 副情感在 auto_decay 中衰减更快
        - 平滑过渡：新状态保留部分旧状态的特征
        """
        new_state = self.transitions.get(trigger, "neutral")

        if new_state != self.current_state:
            # 将旧主情感降级为副情感（带衰减权重）
            if self.current_state != "neutral":
                old_intensity = self.intensity * self.blend_weight
                if old_intensity > 0.1:
                    # 清理旧副情感，降级旧主情感为副情感
                    self.compound_emotions[self.current_state] = old_intensity
                    # 保持最多 compound_max 个副情感
                    if len(self.compound_emotions) > self.compound_max:
                        sorted_emotions = sorted(
                            self.compound_emotions.items(), key=lambda x: x[1], reverse=True
                        )
                        self.compound_emotions = dict(sorted_emotions[:self.compound_max])

            self._record_history(self.current_state, self.state_duration)
            self.current_state = new_state
            self.state_duration = 0
            # 平滑过渡：新状态强度 = 基础强度 + 旧状态残余
            base_intensity = self.EMOTION_STATES[new_state]["intensity"]
            residual = self.intensity * self.blend_weight * 0.3
            self.intensity = min(1.0, base_intensity + residual)
        else:
            # 情感惯性：连续处于同一情感状态时增强强度
            self.intensity = min(1.0, self.intensity + 0.1)
            self.state_duration += 1

    def get_compound_state_description(self) -> str:
        """获取复合情感状态描述

        返回格式: "主情感(副情感1,副情感2) 强度X.XX"
        如: "excited(tsundere) 强度0.75"
        """
        state_info = self.EMOTION_STATES.get(
            self.current_state, self.EMOTION_STATES["neutral"]
        )
        main_desc = f"{self.current_state}({state_info['description']}"

        # 追加活跃的副情感（强度 > 0.1）
        active_compounds = [
            f"{k}:{v:.2f}"
            for k, v in self.compound_emotions.items()
            if v > 0.1
        ]
        if active_compounds:
            main_desc += f", +{'+'.join(active_compounds)}"

        main_desc += f", 强度{self.intensity:.2f})"
        return main_desc

    def auto_decay(self):
        """情感自然衰减（指数衰减，更自然）

        复合情感版：主情感和副情感同时衰减，副情感衰减更快。
        """
        self.state_duration += 1

        # 主情感衰减
        if self.current_state != "neutral":
            self.intensity *= 0.75
            if self.intensity < 0.15:
                self._record_history(self.current_state, self.state_duration)
                self.current_state = "neutral"
                self.state_duration = 0
                self.intensity = self.EMOTION_STATES["neutral"]["intensity"]

        # 副情感衰减（更快，因子 0.5）
        decayed_compounds = {}
        for emotion, intensity in self.compound_emotions.items():
            new_intensity = intensity * 0.5
            if new_intensity > 0.05:  # 太弱就丢弃
                decayed_compounds[emotion] = new_intensity
        self.compound_emotions = decayed_compounds

    def get_current_state_description(self) -> str:
        """获取当前状态描述"""
        state_info = self.EMOTION_STATES.get(
            self.current_state, self.EMOTION_STATES["neutral"]
        )
        return f"{self.current_state}({state_info['description']}, 强度{self.intensity:.2f})"

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

    # 情感触发器关键词映射（类常量，避免 detect_trigger/detect_triggers 重复定义）
    _EMOTION_TRIGGERS = {
        "sword_mentioned": SWORD_KEYWORDS,
        "food_mentioned": FOOD_KEYWORDS,
        "praised": [
            "厉害", "强", "棒", "帅", "可爱", "喜欢", "爱你",
            "牛", "大神", "太强了", "好厉害", "佩服", "崇拜",
            "天才", "无敌", "最强", "第一", "优秀", "出色",
        ],
        "thanked": [
            "谢谢", "感谢", "多亏", "帮大忙",
            "多谢", "感恩", "辛苦了", "还好有你", "靠你了",
        ],
        "insulted": [
            "笨", "蠢", "弱", "菜", "废物", "垃圾",
            "没用", "废柴", "弱鸡", "不行", "差劲", "太弱",
        ],
        "sad_topic": [
            "死", "离别", "失去", "痛苦", "悲伤", "哭",
            "难过", "伤心", "流泪", "心碎", "绝望", "崩溃",
            "失恋", "分手", "孤独", "寂寞", "想哭",
        ],
        "mission_mentioned": [
            "魔剑", "任务", "使命", "猎剑", "责任",
            "战斗", "敌人", "讨伐", "守护", "保护", "歼灭",
        ],
        "new_thing": [
            "新", "第一次", "没见过", "是什么", "介绍一下",
            "好奇", "没听过", "告诉我", "这是啥", "什么东东",
        ],
        "boring_chat": [
            "无聊", "没事", "随便", "发呆",
            "没劲", "无趣", "好闲", "没事干", "不知道干嘛",
        ],
        "joke_made": PLAY_KEYWORDS,
    }

    def detect_trigger(self, text: str) -> Optional[str]:
        """从文本中检测情感触发器（返回首个匹配，扩展版：同义词覆盖）"""
        for trigger, keywords in self._EMOTION_TRIGGERS.items():
            if any(kw in text for kw in keywords):
                return trigger
        return None

    def detect_triggers(self, text: str) -> List[str]:
        """从文本中检测所有情感触发器（复合情感检测，扩展版）"""
        return [
            trigger for trigger, keywords in self._EMOTION_TRIGGERS.items()
            if any(kw in text for kw in keywords)
        ]

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
