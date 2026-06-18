"""云璃插件 - 通用工具函数

集中管理所有纯工具函数，消除 main.py 和 persona/core.py 中的重复代码。
不依赖任何插件内部状态，纯函数或仅有参数依赖。
"""

import re
from typing import List, Optional


def estimate_tokens(text: str) -> int:
    """估算文本 Token 数

    区分中英文字符分别计算：
    - 中文字符：约 1.5 Token/字符
    - 英文/数字/标点：约 0.25 Token/字符（4字符 ≈ 1 Token）

    用于 Token 预算控制，替代各处重复的 len(text) // 2。
    """
    if not text:
        return 0
    chinese_chars = sum(1 for c in text if '\u4e00' <= c <= '\u9fff' or '\u3400' <= c <= '\u4dbf')
    other_chars = len(text) - chinese_chars
    return int(chinese_chars * 1.5 + other_chars * 0.25)


def truncate_at_sentence(text: str, max_len: int, min_lookback: int = 15) -> str:
    """在句子结束标点处截断文本，保持自然

    向后查找最近的句子结束标点进行截断。
    如果没找到合适的截断点，在 max_len 处硬截断。

    Args:
        text: 待截断文本
        max_len: 最大长度
        min_lookback: 最小回溯长度，避免截断太靠近开头

    Returns:
        截断后的文本
    """
    if len(text) <= max_len:
        return text

    truncate_pos = max_len
    lookback_start = max(max_len - min_lookback, 0)
    for i in range(max_len, lookback_start, -1):
        if i < len(text) and text[i] in '。！？.!?…':
            truncate_pos = i + 1
            break

    return text[:truncate_pos]


def truncate_at_sentence_forward(text: str, max_len: int, min_lookback: int = 15) -> str:
    """向前查找句子结束标点截断（别名，兼容不同使用场景）"""
    return truncate_at_sentence(text, max_len, min_lookback)


def merge_messages(messages: List[str], max_messages: int = 10) -> str:
    """合并多条消息（用于消息防抖合并）

    Args:
        messages: 消息文本列表
        max_messages: 最大合并条数

    Returns:
        合并后的消息文本
    """
    if not messages:
        return ""

    if len(messages) <= 1:
        return messages[0]

    # 限制合并条数，避免 prompt 膨胀
    limited = messages[-max_messages:]
    notice = f"[用户连续发送了{len(limited)}条消息，已合并理解]\n"
    return notice + "\n".join(limited)


# ========== 文本清理相关 ==========

LEADING_ASSISTANT_PREFIXES = [
    "好的，", "好的!", "好的。", "好的~",
    "当然，", "当然!", "当然。",
    "以下是", "以下是我", "作为AI", "作为一个AI",
    "我理解", "我明白了", "让我来",
    "没问题，", "没问题!", "没问题。",
    "非常抱歉，", "很抱歉，",
]

INTERNAL_STATE_KEYWORDS = [
    "记忆模块", "提示词", "系统指令", "系统提示",
    "prompt", "system prompt", "配置项",
    "数据库查询", "知识库", "缓存",
    "作为AI助手", "作为人工智能",
]


def remove_assistant_prefix(text: str) -> str:
    """去除 LLM 常见的助手腔开头"""
    for prefix in LEADING_ASSISTANT_PREFIXES:
        if text.startswith(prefix):
            text = text[len(prefix):].lstrip()
            if text and text[0].isascii() and text[0].islower():
                text = text[0].upper() + text[1:]
            break
    return text


def remove_internal_state_lines(text: str) -> str:
    """移除泄露内部状态的行"""
    for kw in INTERNAL_STATE_KEYWORDS:
        if kw.lower() in text.lower():
            sentences = re.split(r'([。！？.?!]+)', text)
            new_sentences = []
            i = 0
            while i < len(sentences):
                segment = sentences[i]
                if not any(kw.lower() in segment.lower() for kw in INTERNAL_STATE_KEYWORDS):
                    new_sentences.append(segment)
                elif i + 1 < len(sentences) and re.match(r'[。！？.?!]+', sentences[i + 1]):
                    i += 1
                i += 1
            text = "".join(new_sentences).strip()
            break
    return text


def clean_repeated_punctuation(text: str) -> str:
    """清理重复标点"""
    return re.sub(r'([。！？，、])\1{3,}', r'\1\1', text)


def is_structured_summary(text: str) -> bool:
    """检测是否为结构化总结（Markdown标题/编号列表/粗体/分隔线）

    用于判断 LLM 输出是否为结构化内容，不同场景下采用不同的过滤策略。
    """
    if len(text) < 100:
        return False
    has_heading = bool(re.search(r'(^|\n)\s*#{2,6}\s+', text))
    has_numbered_list = bool(re.search(r'(^|\n)\s*\d+\.\s+', text))
    has_bold = bool(re.search(r'\*\*[^*]+\*\*', text))
    has_separator = bool(re.search(r'(^|\n)\s*---+\s*(\n|$)', text))
    return has_heading or has_numbered_list or (has_bold and has_numbered_list) or has_separator


class AtDetector:
    """At 检测器

    判断消息是否提到了机器人。
    提供缓存 self_id 的能力，避免每次检测都重复查找。

    从 core/at_detector.py 合并而来（S3 简化项），与 utils.py 定位一致：
    纯检测逻辑 + 单个缓存字段，无内部状态依赖。
    """

    def __init__(self):
        self._cached_self_id: Optional[str] = None

    def set_self_id(self, self_id: str):
        """设置缓存的机器人 ID"""
        self._cached_self_id = self_id

    def get_self_id(self) -> Optional[str]:
        return self._cached_self_id

    def is_at_me(self, event, override_self_id: str = None) -> bool:
        """检测消息是否提到了机器人

        分层检测：
        1. 快速早退：消息文本不含 @ 相关标记
        2. 框架方法 is_at_me()
        3. 遍历消息组件中的 At
        4. 文本匹配 [At:id]

        Args:
            event: AstrMessageEvent
            override_self_id: 可选，临时覆盖缓存的 self_id

        Returns:
            True → 消息提到了机器人
        """
        message = event.message_str or ""

        # 快速早退：不含 @ 相关标记
        if "@" not in message and "[At:" not in message and "At:" not in message:
            return False

        self_id = override_self_id or self._cached_self_id

        # 方法1：框架方法
        is_at_me_fn = getattr(event, 'is_at_me', None)
        if callable(is_at_me_fn) and is_at_me_fn():
            return True

        # 方法2：遍历消息组件
        try:
            chain = getattr(event, "message_obj", None)
            if chain and hasattr(chain, "message"):
                for comp in chain.message:
                    class_name = comp.__class__.__name__.lower()
                    if class_name == "at":
                        qq = str(getattr(comp, "qq", "") or "").strip()
                        if qq.lower() == "all":
                            return True
                        if self_id and qq == self_id:
                            return True
                        # self_id 未初始化时保守处理：不判定为 @ 我
                        # 避免将所有含 @ 的消息都误判为 @ 机器人
        except Exception:
            pass

        # 方法3：文本匹配
        if self_id and f"[At:{self_id}]" in message:
            return True

        return False