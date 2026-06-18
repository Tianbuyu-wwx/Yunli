import json
import random
from typing import Dict

# Darwin 进化后的语言风格占位（运行时通过 overlay 动态生效）
LANGUAGE_STYLE = """
"""

from .config import RELATIONSHIP_MODES, STYLE_MODULATION


class QQBehaviorManager:
    """QQ群聊特殊行为管理器"""

    # 情绪驱动的标点策略
    EMOTION_PUNCTUATION = {
        "excited": {
            "exclamation_rate": 0.6,
            "ellipsis_rate": 0.05,
            "pause_type_weights": {
                "ellipsis": 0.2,
                "hesitation": 0.1,
                "correction": 0.05,
                "emphasis": 0.65,
            },
            "hesitation_words": ["…哈！", "…嗯！"],
            "correction_words": ["…不对，", "…等等，"],
        },
        "annoyed": {
            "exclamation_rate": 0.3,
            "ellipsis_rate": 0.1,
            "pause_type_weights": {
                "ellipsis": 0.3,
                "hesitation": 0.4,
                "correction": 0.2,
                "emphasis": 0.1,
            },
            "hesitation_words": ["…啧…", "…我说…", "…啧"],
            "correction_words": ["…不对，", "…我的意思是…"],
        },
        "sad_guarded": {
            "exclamation_rate": 0.05,
            "ellipsis_rate": 0.5,
            "pause_type_weights": {
                "ellipsis": 0.6,
                "hesitation": 0.3,
                "correction": 0.05,
                "emphasis": 0.05,
            },
            "hesitation_words": ["…嗯…", "…那个…", "……"],
            "correction_words": ["…算了，"],
        },
        "tsundere": {
            "exclamation_rate": 0.4,
            "ellipsis_rate": 0.15,
            "pause_type_weights": {
                "ellipsis": 0.3,
                "hesitation": 0.3,
                "correction": 0.3,
                "emphasis": 0.1,
            },
            "hesitation_words": ["…哼，", "…切，"],
            "correction_words": ["…不对，", "…我才不是…", "…别误会，"],
        },
        "happy": {
            "exclamation_rate": 0.5,
            "ellipsis_rate": 0.08,
            "pause_type_weights": {
                "ellipsis": 0.3,
                "hesitation": 0.2,
                "correction": 0.1,
                "emphasis": 0.4,
            },
            "hesitation_words": ["…嘿嘿", "…嗯哼"],
            "correction_words": ["…不对，", "…等等，"],
        },
        "curious": {
            "exclamation_rate": 0.2,
            "ellipsis_rate": 0.2,
            "pause_type_weights": {
                "ellipsis": 0.3,
                "hesitation": 0.4,
                "correction": 0.1,
                "emphasis": 0.2,
            },
            "hesitation_words": ["…嗯？", "…咦？"],
            "correction_words": ["…等等，"],
        },
        # neutral/serious/bored 使用默认值
    }

    def __init__(self, db, config: dict = None):
        self.db = db
        self.config = config or {}

        # 基础配置
        self.use_qq_emoji = self.config.get("use_qq_emoji", True)

        # 实例级情绪标点策略，支持运行时 overlay 覆盖
        self.emotion_punctuation = self._deep_copy_emotion_punctuation()
        self._load_language_style_overlay()

    def _deep_copy_emotion_punctuation(self) -> Dict:
        """深拷贝类常量，创建实例级可变配置"""
        import copy
        return copy.deepcopy(self.EMOTION_PUNCTUATION)

    def _load_language_style_overlay(self):
        """从运行时 overlay 加载进化后的语言风格配置

        overlay 格式（JSON）：
            {
                "excited": {
                    "exclamation_rate": 0.7,
                    "pause_type_weights": {"ellipsis": 0.2, ...}
                },
                ...
            }
        仅覆盖存在的键，不影响未覆盖的情绪配置。
        """
        try:
            from ..evolution import asset_bridge
            overlay_text = asset_bridge.get_runtime_overlay("language_style")
            if not overlay_text:
                return
            overlay = json.loads(overlay_text)
            if not isinstance(overlay, dict):
                return
        except (ImportError, OSError, json.JSONDecodeError):
            return

        for emotion, overrides in overlay.items():
            if emotion not in self.emotion_punctuation or not isinstance(overrides, dict):
                continue
            self._deep_update(self.emotion_punctuation[emotion], overrides)

    @staticmethod
    def _deep_update(base: Dict, overrides: Dict):
        """递归更新字典，不覆盖整个子字典"""
        for key, val in overrides.items():
            if isinstance(val, dict) and key in base and isinstance(base[key], dict):
                QQBehaviorManager._deep_update(base[key], val)
            else:
                base[key] = val

    def format_for_qq(
        self, text: str, is_at_me: bool = False, user_nickname: str = "",
        emotion_state: str = "neutral", relationship_mode: str = "normal",
    ) -> str:
        """格式化文本适配QQ群聊

        注意：不添加动作描述或情感标签，保持像人的体验。
        长度截断由 review_response 统一处理，不再在此处重复截断。
        """
        if not text:
            return text

        # 添加QQ表情符号（如果配置开启）- 少量，自然
        if self.use_qq_emoji:
            text = self._add_qq_emoji(text, emotion_state, relationship_mode)

        # 如果被@了，可以加上称呼
        if is_at_me and user_nickname and random.random() < 0.3:
            prefixes = [f"@{user_nickname} ", f"{user_nickname}，", ""]
            text = random.choice(prefixes) + text

        return text

    def _add_qq_emoji(self, text: str, emotion_state: str = "neutral",
                      relationship_mode: str = "normal") -> str:
        """根据情绪和关系模式添加颜文字（跨平台兼容，比QQ原生表情代码更自然）"""
        # 使用颜文字替代QQ原生表情代码，确保在所有平台都能正常显示
        kaomoji_map = self.config.get("kaomoji_map", {
            "哼": ["(￣^￣)", "(¬_¬)", "(*￣m￣)"],
            "哈哈": ["(^▽^)", "(≧∇≦)", "(ﾉ´ヮ`)ﾉ*: ･ﾟ"],
            "生气": ["(╬ Ò﹏Ó)", "(>_<)", "(｀ε´)"],
            "惊讶": ["(⊙_⊙)", "(°o°)", "(゜-゜)"],
            "好吃": ["(๑´ڡ`๑)", "(¯﹃¯)", "(๑´ㅂ`๑)"],
            "无聊": ["(￣O￣)", "(¬‿¬)", "( ˘ω˘ )"],
            "开心": ["(*^▽^*)", "(｀・ω・´)", "ヽ(〃･ω･)ﾉ"],
            "难过": ["(´；ω；`)", "(ノ_<。)", "｡･ﾟﾟ･(>д<;)･ﾟﾟ･｡"],
            "得意": ["(￣ω￣)", "(￣▽￣)~*", "(*¯︶¯*)"],
            "害羞": ["(/ω＼)", "(〃∀〃)", "(#^.^#)"],
            "思考": ["(｡･ω･｡)", "(´-ω-`)", "(￣～￣)"],
            "傲娇": ["(￣^￣)ゞ", "(￣ ￣ゞ)", "╮(￣▽￣)╭"],
            "剑": ["(｀・ω・´)⚔", "(▼皿▼)╯", "ヽ(｀⌒´)ノ"],
        })

        # 根据情绪调整颜文字基础概率
        if emotion_state in ("excited", "happy"):
            kaomoji_prob = self.config.get("kaomoji_probability", 0.3)
        elif emotion_state in ("sad_guarded", "annoyed"):
            kaomoji_prob = self.config.get("kaomoji_probability", 0.05)
        elif emotion_state == "tsundere":
            kaomoji_prob = self.config.get("kaomoji_probability", 0.25)
        else:
            kaomoji_prob = self.config.get("kaomoji_probability", 0.15)

        # 根据关系模式调整颜文字概率
        # backoff: 0.2x → 几乎不加; careful: 0.5x → 减少; normal: 1.0x; warming: 1.2x
        rel_multiplier = RELATIONSHIP_MODES.get(
            relationship_mode, {}
        ).get("emoji_multiplier", 1.0)
        kaomoji_prob *= rel_multiplier

        # Phase 1 P0 改进：关系等级驱动的风格调制 - kaomoji_bonus
        # 从共享配置获取额外加成（原通过 LanguageStyleProcessor.STYLE_MODULATION 访问）
        style_mod = STYLE_MODULATION.get(relationship_mode, {})
        kaomoji_bonus = style_mod.get("kaomoji_bonus", 0.0)
        kaomoji_prob = max(0.01, min(0.5, kaomoji_prob + kaomoji_bonus))

        for word, kaomojis in kaomoji_map.items():
            if word in text and random.random() < kaomoji_prob:
                kaomoji = random.choice(kaomojis)
                # 在词后添加颜文字
                text = text.replace(word, word + kaomoji, 1)
                break  # 只添加一个颜文字

        return text

    def get_response_delay(self, text_length: int = 0) -> float:
        """获取响应延迟（模拟真人打字速度）

        Args:
            text_length: 回复文本长度，用于计算打字时间
        """
        # 基础反应时间（看到消息到开始打字）
        base_delay = random.uniform(
            self.config.get("normal_min_delay", 0.8),
            self.config.get("normal_max_delay", 2.0)
        )

        # 模拟打字时间（假设每秒打5-8个字）
        if text_length > 0:
            min_typing_speed = self.config.get("min_typing_speed", 5)
            max_typing_speed = self.config.get("max_typing_speed", 8)
            typing_speed = random.uniform(min_typing_speed, max_typing_speed)
            typing_time = text_length / typing_speed
            # 打字时间有一定随机性，且最长不超过4秒
            max_typing_time = self.config.get("max_typing_time", 4.0)
            typing_time = min(typing_time * random.uniform(0.8, 1.2), max_typing_time)
        else:
            typing_time = 0

        return base_delay + typing_time

    def should_skip_punctuation(self) -> bool:
        """是否偶尔省略标点（真人习惯）"""
        return random.random() < self.config.get("skip_punctuation_probability", 0.15)

    def add_typing_pause(self, text: str, emotion_state: str = "neutral",
                         relationship_mode: str = "normal") -> str:
        """在长消息中添加停顿（模拟真人思考）

        增强拟人化：
        - 添加犹豫型停顿（"嗯…""那个…"）
        - 模拟改口（"不对，我是说…"）
        - 添加思考中的省略号
        - 根据情绪状态和关系模式动态调整停顿类型和概率
        """
        pause_min_length = self.config.get("typing_pause_min_length", 20)
        if len(text) <= pause_min_length:
            return text

        # 随机在句子中添加省略号停顿
        # 注意：按句子结束标点切分，保留原始标点
        import re
        sentences = re.split(r'([。！？.?!]+)', text)
        # 重新组装句子（标点属于前一句）
        assembled = []
        i = 0
        while i < len(sentences):
            if i + 1 < len(sentences) and re.match(r'[。！？.?!]+', sentences[i + 1]):
                assembled.append(sentences[i] + sentences[i + 1])
                i += 2
            else:
                if sentences[i].strip():
                    assembled.append(sentences[i])
                i += 1

        # 根据情绪和关系模式调整停顿概率
        emotion_config = self.emotion_punctuation.get(emotion_state, {})
        if emotion_state in ("excited", "happy"):
            typing_pause_prob = self.config.get("typing_pause_probability", 0.2)
        elif emotion_state in ("sad_guarded", "annoyed"):
            typing_pause_prob = self.config.get("typing_pause_probability", 0.45)
        else:
            typing_pause_prob = self.config.get("typing_pause_probability", 0.3)

        # 根据关系模式进一步调整停顿概率
        # backoff: 减少停顿（简洁快速）; careful: 略微减少; warming: 略微增加
        rel_multiplier = RELATIONSHIP_MODES.get(
            relationship_mode, {}
        ).get("emoji_multiplier", 1.0)
        typing_pause_prob *= rel_multiplier

        pause_min_sentences = self.config.get("typing_pause_min_sentences", 3)
        if len(assembled) >= pause_min_sentences and random.random() < typing_pause_prob:
            # 基于情绪权重的加权随机选择停顿类型
            weights = emotion_config.get("pause_type_weights", {
                "ellipsis": 0.5,
                "hesitation": 0.2,
                "correction": 0.15,
                "emphasis": 0.15,
            })
            pause_types = list(weights.keys())
            pause_weights = list(weights.values())
            chosen_type = random.choices(pause_types, weights=pause_weights, k=1)[0]

            if chosen_type == "ellipsis":
                # 普通省略号停顿
                assembled[1] = assembled[1].rstrip() + "…"
            elif chosen_type == "hesitation":
                # 犹豫型停顿
                hesitations = emotion_config.get("hesitation_words", ["…嗯…", "…那个…", "…啧…"])
                assembled[1] = assembled[1].rstrip() + random.choice(hesitations)
            elif chosen_type == "correction":
                # 改口型停顿
                corrections = emotion_config.get("correction_words", ["…不对，", "…等等，", "…我的意思是…"])
                assembled[1] = assembled[1].rstrip() + random.choice(corrections)
            else:  # emphasis
                # 强调型停顿
                assembled[1] = assembled[1].rstrip() + "…"
                if len(assembled) > 2:
                    assembled[2] = "…" + assembled[2].lstrip()
            text = "".join(assembled)

        return text

    def add_human_touches(self, text: str, emotion_state: str = "neutral",
                         relationship_mode: str = "normal") -> str:
        """添加人类聊天的小习惯（贴合云璃人设）

        云璃特点：直率、不矫情、偶尔傲娇、不太会撒娇
        所以去掉过于软萌的习惯（如"滴""好哦"），保留直率感
        根据情绪状态和关系模式动态调整概率
        """
        if not text or len(text) < 3:
            return text

        # 根据关系模式获取倍率
        rel_mult = RELATIONSHIP_MODES.get(
            relationship_mode, {}
        ).get("emoji_multiplier", 1.0)

        # 根据情绪调整"了"→"啦"概率（受关系模式倍率影响）
        le_to_la_base = self.config.get("le_to_la_probability", 0.08)
        if emotion_state == "excited":
            le_to_la_prob = self.config.get("le_to_la_excited_bonus", 0.2) * rel_mult
        elif emotion_state in ("sad_guarded", "annoyed"):
            le_to_la_prob = 0.0
        elif emotion_state == "tsundere":
            le_to_la_prob = le_to_la_base * rel_mult
        else:
            le_to_la_prob = le_to_la_base * rel_mult

        # 1. 偶尔把句末的"了"换成"啦"（更口语化）
        if text.endswith("了") and random.random() < le_to_la_prob:
            text = text[:-1] + "啦"

        # 根据情绪调整短句加"~"概率（受关系模式倍率影响）
        tilde_base = self.config.get("tilde_probability", 0.05)
        if emotion_state == "excited":
            tilde_prob = self.config.get("tilde_excited_bonus", 0.15) * rel_mult
        elif emotion_state in ("sad_guarded", "annoyed"):
            tilde_prob = 0.0
        elif emotion_state == "tsundere":
            tilde_prob = self.config.get("tilde_tsundere_prob", 0.02) * rel_mult
        else:
            tilde_prob = tilde_base * rel_mult

        # 2. 偶尔在短句后加"~"
        tilde_min_length = self.config.get("tilde_min_length", 10)
        if len(text) <= tilde_min_length and random.random() < tilde_prob:
            if text[-1] not in "~…！？":
                text = text + "~"

        # 3. 省略"我"（云璃说话直接，"我觉得"→"觉得"，概率8%）
        drop_wo_prob = self.config.get("drop_wo_probability", 0.08)
        if text.startswith("我觉得") and random.random() < drop_wo_prob:
            text = text[1:]  # 去掉"我"

        return text

    def should_use_at_reply(self, is_at_me: bool, message: str) -> bool:
        """判断是否使用@回复"""
        # 如果被@了，50%概率@回去
        if is_at_me:
            return random.random() < self.config.get("at_reply_probability", 0.5)
        return False
