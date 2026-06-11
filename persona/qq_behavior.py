import random
from typing import Dict


class QQBehaviorManager:
    """QQ群聊特殊行为管理器"""

    def __init__(self, db, config: dict = None):
        self.db = db
        self.config = config or {}

        # 基础配置
        self.use_qq_emoji = self.config.get("use_qq_emoji", True)

    def format_for_qq(
        self, text: str, is_at_me: bool = False, user_nickname: str = ""
    ) -> str:
        """格式化文本适配QQ群聊

        注意：不添加动作描述或情感标签，保持像人的体验。
        长度截断由 review_response 统一处理，不再在此处重复截断。
        """
        if not text:
            return text

        # 添加QQ表情符号（如果配置开启）- 少量，自然
        if self.use_qq_emoji:
            text = self._add_qq_emoji(text)

        # 如果被@了，可以加上称呼
        if is_at_me and user_nickname and random.random() < 0.3:
            prefixes = [f"@{user_nickname} ", f"{user_nickname}，", ""]
            text = random.choice(prefixes) + text

        return text

    def _add_qq_emoji(self, text: str) -> str:
        """根据情绪添加颜文字（跨平台兼容，比QQ原生表情代码更自然）"""
        # 使用颜文字替代QQ原生表情代码，确保在所有平台都能正常显示
        kaomoji_map = self.config.get("kaomoji_map", {
            "哼": ["(￣^￣)", "(¬_¬)"],
            "哈哈": ["(^▽^)", "(≧∇≦)"],
            "生气": ["(╬ Ò﹏Ó)", "(>_<)"],
            "惊讶": ["(⊙_⊙)", "(°o°)"],
            "好吃": ["(๑´ڡ`๑)", "(¯﹃¯)"],
            "无聊": ["(￣O￣)", "(¬‿¬)"],
            "开心": ["(*^▽^*)", "(｀・ω・´)"],
        })
        kaomoji_prob = self.config.get("kaomoji_probability", 0.15)

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

    def add_typing_pause(self, text: str) -> str:
        """在长消息中添加停顿（模拟真人思考）

        增强拟人化：
        - 添加犹豫型停顿（"嗯…""那个…"）
        - 模拟改口（"不对，我是说…"）
        - 添加思考中的省略号
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

        typing_pause_prob = self.config.get("typing_pause_probability", 0.3)
        if len(assembled) >= 3 and random.random() < typing_pause_prob:
            # 在第二句后添加思考停顿
            pause_type = random.random()
            if pause_type < 0.5:
                # 普通省略号停顿
                assembled[1] = assembled[1].rstrip() + "…"
            elif pause_type < 0.7:
                # 犹豫型停顿（云璃式：直率但不善言辞时的停顿）
                hesitations = ["…嗯…", "…那个…", "…啧…"]
                assembled[1] = assembled[1].rstrip() + random.choice(hesitations)
            elif pause_type < 0.85:
                # 改口型停顿（云璃式：干脆的改口）
                corrections = ["…不对，", "…等等，", "…我的意思是…"]
                assembled[1] = assembled[1].rstrip() + random.choice(corrections)
            else:
                # 强调型停顿
                assembled[1] = assembled[1].rstrip() + "…"
                if len(assembled) > 2:
                    assembled[2] = "…" + assembled[2].lstrip()
            text = "".join(assembled)

        return text

    def add_human_touches(self, text: str) -> str:
        """添加人类聊天的小习惯（贴合云璃人设）

        云璃特点：直率、不矫情、偶尔傲娇、不太会撒娇
        所以去掉过于软萌的习惯（如"滴""好哦"），保留直率感
        """
        if not text or len(text) < 3:
            return text

        # 1. 偶尔把句末的"了"换成"啦"（更口语化，概率8%，保持克制）
        if text.endswith("了") and random.random() < 0.08:
            text = text[:-1] + "啦"

        # 2. 偶尔在短句后加"~"（概率5%，云璃不太会用波浪号撒娇）
        if len(text) <= 10 and random.random() < 0.05:
            if text[-1] not in "~…！？":
                text = text + "~"

        # 3. 省略"我"（云璃说话直接，"我觉得"→"觉得"，概率8%）
        if text.startswith("我觉得") and random.random() < 0.08:
            text = text[1:]  # 去掉"我"

        return text

    def should_use_at_reply(self, is_at_me: bool, message: str) -> bool:
        """判断是否使用@回复"""
        # 如果被@了，50%概率@回去
        if is_at_me:
            return random.random() < self.config.get("at_reply_probability", 0.5)
        return False
