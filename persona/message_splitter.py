"""消息切分器 — 简化的延迟增强层

不再做复杂的智能切分（Markdown块识别、URL保护、软短句合并等），
仅作为 AstrBot 自带分段的增强补充：提供段间延迟和思考停顿。

切分策略：
1. 按空行切分（段落边界）
2. 按句子切分（避免单条过长）
3. 合并过短片段
4. 限制最大段数 + 计算延迟
"""

import random
import re
from typing import Dict, List


class MessageSplitter:
    """消息切分器 - 简化版，作为 AstrBot 分段的增强补充

    核心能力：段间延迟 + 思考停顿 + 段落句子切分。
    """

    SENTENCE_ENDINGS = '。！？.!?~…'

    def __init__(self, config: dict = None):
        self.config = config or {}
        self.max_segment_length = self.config.get('max_segment_length', 80)
        self.min_segment_length = self.config.get('min_segment_length', 10)
        self.enable_typing_delay = self.config.get('enable_typing_delay', True)
        self.base_delay = self.config.get('base_delay', 1.5)
        self.delay_per_char = self.config.get('delay_per_char', 0.04)
        self.max_delay = self.config.get('max_delay', 4.0)
        self.enable_thinking_pause = self.config.get('enable_thinking_pause', True)
        self.thinking_pause_prob = self.config.get('thinking_pause_prob', 0.3)
        self.max_segments = self.config.get('max_segments', 2)

    def split(self, text: str) -> List[Dict[str, any]]:
        """切分消息为多个片段

        纯文本切分：空行 + 句子边界，不做任何保护/检测。
        """
        if not text or not text.strip():
            return []

        # 1. 段落级切分（按空行）
        segments = self._split_by_paragraphs(text)

        # 2. 超长段落按句子再次切分
        segments = self._split_long_segments(segments)

        # 3. 合并过短片段
        segments = self._merge_short_segments(segments)

        # 4. 限制最大段数
        segments = self._limit_max_segments(segments)

        # 5. 计算延迟
        result = []
        for i, seg in enumerate(segments):
            delay = self._calculate_delay(seg, i, len(segments))
            result.append({
                'text': seg,
                'delay': delay,
                'is_last': i == len(segments) - 1,
            })

        return result

    def _split_by_paragraphs(self, text: str) -> List[str]:
        """按空行切分为段落"""
        paragraphs = re.split(r'\n\s*\n', text)
        result = []
        for para in paragraphs:
            stripped = para.strip()
            if stripped:
                result.append(stripped)
        return result if result else [text.strip()]

    def _split_long_segments(self, segments: List[str]) -> List[str]:
        """将超长片段按句子切分"""
        result = []
        for seg in segments:
            if len(seg) <= self.max_segment_length:
                result.append(seg)
                continue
            result.extend(self._split_by_sentences(seg))
        return result

    def _split_by_sentences(self, text: str) -> List[str]:
        """按句子切分

        如果单句仍然超长（无标点），按固定长度截断保底。
        """
        sentences = self._split_into_sentences(text)
        current = ''
        result = []
        for sent in sentences:
            if len(current) + len(sent) <= self.max_segment_length:
                current += sent
            else:
                if current:
                    result.append(current)
                if len(sent) > self.max_segment_length:
                    # 极端情况：单句超长，按逗号再分
                    sub_parts = re.split(r'[，,；;]', sent)
                    part_buf = ''
                    for part in sub_parts:
                        if len(part_buf) + len(part) + 1 <= self.max_segment_length:
                            part_buf = (part_buf + '，' + part) if part_buf else part
                        else:
                            if part_buf:
                                result.append(part_buf)
                            # 子段仍然超长（无逗号），按固定长度切分保底
                            if len(part) > self.max_segment_length:
                                for i in range(0, len(part), self.max_segment_length):
                                    result.append(part[i:i + self.max_segment_length])
                            else:
                                part_buf = part
                    if part_buf:
                        result.append(part_buf)
                else:
                    current = sent
        if current:
            result.append(current)
        return result

    def _split_into_sentences(self, text: str) -> List[str]:
        """将文本切分为句子"""
        pattern = f'[^{self.SENTENCE_ENDINGS}]*[{self.SENTENCE_ENDINGS}]+'
        sentences = re.findall(pattern, text)
        remaining = re.sub(pattern, '', text)
        if remaining.strip():
            sentences.append(remaining)
        return sentences if sentences else [text]

    def _merge_short_segments(self, segments: List[str]) -> List[str]:
        """合并过短片段"""
        if not segments:
            return segments

        result = []
        current = segments[0]

        for seg in segments[1:]:
            if (len(current) < self.min_segment_length
                    and len(current) + len(seg) <= self.max_segment_length):
                current = self._join_segment_pair(current, seg)
            else:
                result.append(current)
                current = seg

        result.append(current)
        return result

    def _limit_max_segments(self, segments: List[str]) -> List[str]:
        """限制最大段数"""
        if not segments or len(segments) <= self.max_segments:
            return segments
        kept = segments[:self.max_segments - 1]
        tail = segments[self.max_segments - 1]
        for item in segments[self.max_segments:]:
            tail = self._join_segment_pair(tail, item)
        return kept + [tail]

    def _join_segment_pair(self, left: str, right: str) -> str:
        """智能连接两段文本"""
        left = str(left or "").strip()
        right = str(right or "").strip()
        if not left:
            return right
        if not right:
            return left
        if re.search(r"[！？!?]$", left):
            return f"{left} {right}"
        softened = re.sub(r"[。…~～]+$", "，", left)
        softened = re.sub(r"[!?！？]+$", "，", softened)
        if not re.search(r"[，,、\s]$", softened):
            softened += "，"
        return f"{softened}{right}"

    def _calculate_delay(self, text: str, index: int, total: int) -> float:
        """计算发送延迟（模拟真人打字节奏）

        短消息策略下的延迟特点：
        - 首段快速发出（模拟看到消息就回）
        - 后续段有明显的"打字思考"间隔
        """
        if not self.enable_typing_delay:
            return 0.0
        typing_time = len(text) * self.delay_per_char
        delay = self.base_delay + typing_time
        delay = min(delay, self.max_delay)
        delay *= random.uniform(0.8, 1.2)
        if index == 0:
            delay *= 0.2  # 首段几乎无延迟，快速回应
        else:
            delay *= 1.0  # 后续段保留完整延迟，模拟打字思考
        if len(text) <= 10:
            delay *= 0.5
        return delay

    def get_thinking_pause(self, seg_text: str = "", is_first: bool = False) -> str:
        """获取思考停顿文本"""
        if not self.enable_thinking_pause:
            return ''
        if is_first:
            return ''
        if random.random() < self.thinking_pause_prob:
            if len(seg_text) <= 15:
                pauses = ['…', '嗯…', '啊…']
            elif len(seg_text) >= 80:
                pauses = ['…让我想想…', '…嗯，怎么说呢…', '…']
            else:
                pauses = ['…', '…嗯…', '…对了…', '…']
            return random.choice(pauses)
        return ''