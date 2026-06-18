"""yunli.core.utils 模块的单元测试"""

import sys
import os
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
from test_base import setup_test_path
setup_test_path()

from yunli.core import utils
from test_base import YunliTestCase


# ============================================================================
# estimate_tokens
# ============================================================================

class TestEstimateTokens(YunliTestCase):
    """测试 estimate_tokens Token 估算（v2.2.0：中英区分算法）

    算法：
    - 中文字符（CJK 统一汉字 + 扩展 A 区）：约 1.5 Token/字符
    - 英文字符（数字、字母、标点）：约 0.25 Token/字符（4字符 ≈ 1 Token）
    """

    def test_short_english_text_returns_small_number(self):
        """短英文文本应返回较小的 Token 数（5字符 × 0.25 = 1.25 → 1）"""
        result = utils.estimate_tokens("hello")
        self.assertEqual(result, 1)

    def test_long_english_text_returns_larger_number(self):
        """长英文文本应返回较大的 Token 数（100字符 × 0.25 = 25）"""
        text = "a" * 100
        result = utils.estimate_tokens(text)
        self.assertEqual(result, 25)

    def test_chinese_text_uses_approximate_counting(self):
        """中文文本使用 1.5 Token/字符 估算（10 字符 × 1.5 = 15）"""
        text = "你好世界，今天天气不错"
        # 10 个中文字符（CJK 范围），1 个中文逗号也算
        # "你好世界，今天天气不错" = 10 chars total, all CJK
        result = utils.estimate_tokens(text)
        # 10 × 1.5 = 15
        self.assertEqual(result, 15)

    def test_mixed_text_separates_chinese_and_english(self):
        """混合文本应分别统计中英文"""
        # 2 中文 + 5 英文 = 2*1.5 + 5*0.25 = 3 + 1.25 = 4.25 → 4
        text = "你好 hello"
        result = utils.estimate_tokens(text)
        self.assertEqual(result, 4)

    def test_empty_text_returns_zero(self):
        """空文本应返回 0"""
        self.assertEqual(utils.estimate_tokens(""), 0)
        self.assertEqual(utils.estimate_tokens(None), 0)


# ============================================================================
# truncate_at_sentence
# ============================================================================

class TestTruncateAtSentence(YunliTestCase):
    """测试 truncate_at_sentence 句子边界截断"""

    def test_shorter_than_max_unchanged(self):
        """文本长度小于 max_len 时不应截断"""
        text = "Hello world."
        result = utils.truncate_at_sentence(text, 100)
        self.assertEqual(result, text)

    def test_truncate_at_sentence_boundary(self):
        """文本超过 max_len 时应在句子边界截断"""
        text = "这是一段话。后面还有更多内容需要被截掉。"
        # max_len=12, lookback_start = max(12-15, 0) = 0
        # text[11] = '。' (the first period is at index 5... wait let me count)
        # "这是一段话。后面还有更多内容需要被截掉。"
        # 0=这 1=是 2=一 3=段 4=话 5=。 6=后 7=面 8=还 9=有 10=更 11=多 12=内 13=容 14=需 15=要 16=被 17=截 18=掉 19=。
        # max_len=12, lookback_start = max(12-15, 0) = 0
        # i=12: text[12]='内', not punct
        # i=11: text[11]='多', not punct
        # ... i=5: text[5]='。', punct! truncate_pos=6
        max_len = 12
        result = utils.truncate_at_sentence(text, max_len)
        self.assertEqual(result, "这是一段话。")

    def test_no_sentence_boundary_truncate_at_max(self):
        """没有句子边界标点时应硬截断到 max_len"""
        text = "这是一个没有标点符号的连续长文本段落希望被截断"
        max_len = 10
        result = utils.truncate_at_sentence(text, max_len)
        # No sentence-ending punctuation found, hard truncate at max_len
        self.assertEqual(result, text[:max_len])

    def test_empty_text_returns_empty(self):
        """空文本应返回空字符串"""
        result = utils.truncate_at_sentence("", 10)
        self.assertEqual(result, "")


# ============================================================================
# truncate_at_sentence_forward
# ============================================================================

class TestTruncateAtSentenceForward(YunliTestCase):
    """测试 truncate_at_sentence_forward 前向查找截断"""

    def test_forward_wraps_backward_same_result(self):
        """forward 是 backward 的别名，对相同输入应返回相同结果"""
        text = "这是第一句。这是第二句。这是第三句。"
        result_fwd = utils.truncate_at_sentence_forward(text, 10)
        result_bwd = utils.truncate_at_sentence(text, 10)
        self.assertEqual(result_fwd, result_bwd)

    def test_truncate_at_sentence_boundary(self):
        """应在句子边界截断"""
        text = "Hello world. This is a very long sentence that should be truncated."
        max_len = 20
        result = utils.truncate_at_sentence_forward(text, max_len)
        # text[11]='.' → truncate_pos=12
        self.assertEqual(result, "Hello world.")

    def test_no_sentence_boundary_hard_truncation(self):
        """没有标点时硬截断到 max_len"""
        text = "这是一个完全没有标点符号的长文本段落用于测试前向截断"
        max_len = 10
        result = utils.truncate_at_sentence_forward(text, max_len)
        self.assertEqual(result, text[:max_len])


# ============================================================================
# merge_messages
# ============================================================================

class TestMergeMessages(YunliTestCase):
    """测试 merge_messages 消息合并"""

    def test_multiple_messages_merged(self):
        """多条消息应合并为带通知前缀的字符串"""
        messages = ["第一条", "第二条", "第三条"]
        result = utils.merge_messages(messages)
        self.assertIn("[用户连续发送了3条消息，已合并理解]", result)
        self.assertIn("第一条", result)
        self.assertIn("第二条", result)
        self.assertIn("第三条", result)
        # Verify structure: notice + "\n".join(messages)
        expected = "[用户连续发送了3条消息，已合并理解]\n第一条\n第二条\n第三条"
        self.assertEqual(result, expected)

    def test_empty_list_returns_empty_string(self):
        """空列表应返回空字符串"""
        result = utils.merge_messages([])
        self.assertEqual(result, "")

    def test_single_message_unchanged(self):
        """单条消息应直接返回该消息内容"""
        result = utils.merge_messages(["仅此一条"])
        self.assertEqual(result, "仅此一条")

    def test_over_max_messages_truncated(self):
        """超过 max_messages 条消息时只保留最后 10 条"""
        messages = [f"消息{i}" for i in range(15)]
        result = utils.merge_messages(messages)
        # Should only contain the last 10
        self.assertIn("[用户连续发送了10条消息，已合并理解]", result)
        self.assertNotIn("消息0", result)
        self.assertNotIn("消息4", result)
        self.assertIn("消息5", result)
        self.assertIn("消息14", result)


# ============================================================================
# remove_assistant_prefix
# ============================================================================

class TestRemoveAssistantPrefix(YunliTestCase):
    """测试 remove_assistant_prefix 助手前缀去除"""

    def test_known_prefix_removed(self):
        """已知的前缀应被去除"""
        result = utils.remove_assistant_prefix("好的，今天天气不错")
        self.assertEqual(result, "今天天气不错")

    def test_no_prefix_unchanged(self):
        """没有已知前缀的文本应保持不变"""
        text = "普通的文本内容"
        result = utils.remove_assistant_prefix(text)
        self.assertEqual(result, text)

    def test_assistant_colon_not_in_prefixes(self):
        """'assistant:' 不在前缀列表中，不应被去除"""
        text = "assistant: hello"
        result = utils.remove_assistant_prefix(text)
        self.assertEqual(result, text)


# ============================================================================
# remove_internal_state_lines
# ============================================================================

class TestRemoveInternalStateLines(YunliTestCase):
    """测试 remove_internal_state_lines 内部状态行去除"""

    def test_text_with_state_keyword_cleaned(self):
        """包含内部状态关键词的行应被移除"""
        text = "今天天气不错。记忆模块更新了。你好。"
        result = utils.remove_internal_state_lines(text)
        self.assertEqual(result, "今天天气不错。你好。")

    def test_text_without_state_keyword_unchanged(self):
        """不包含关键词的文本应保持不变"""
        text = "今天天气不错。你好呀。"
        result = utils.remove_internal_state_lines(text)
        self.assertEqual(result, text)


# ============================================================================
# clean_repeated_punctuation
# ============================================================================

class TestCleanRepeatedPunctuation(YunliTestCase):
    """测试 clean_repeated_punctuation 重复标点清理"""

    def test_four_or_more_exclamation_cleaned(self):
        """4 个及以上重复的感叹号应缩减为 2 个"""
        # Pattern: ([。！？，、])\1{3,} → needs 4+ total chars to match
        result = utils.clean_repeated_punctuation("！！！！")
        self.assertEqual(result, "！！")

    def test_four_or_more_period_cleaned(self):
        """4 个及以上重复的句号应缩减为 2 个"""
        result = utils.clean_repeated_punctuation("。。。。")
        self.assertEqual(result, "。。")

    def test_normal_punctuation_unchanged(self):
        """正常使用的单个标点应保持不变"""
        text = "你好。你好吗？没问题！"
        result = utils.clean_repeated_punctuation(text)
        self.assertEqual(result, text)


# ============================================================================
# is_structured_summary
# ============================================================================

class TestIsStructuredSummary(YunliTestCase):
    """测试 is_structured_summary 结构化总结检测"""

    def test_numbered_list_returns_true(self):
        """包含编号列表且长度 >= 100 的文本应返回 True"""
        text = "总结如下：\n" + "\n".join(f"{i}. 这是第{i}个要点内容描述" for i in range(1, 6))
        # Ensure text length >= 100
        while len(text) < 100:
            text += " 补充说明文字。"
        result = utils.is_structured_summary(text)
        self.assertTrue(result)

    def test_short_plain_text_returns_false(self):
        """简短普通文本应返回 False"""
        text = "你好，今天天气不错。"
        result = utils.is_structured_summary(text)
        self.assertFalse(result)

    def test_markdown_heading_returns_true(self):
        """包含 Markdown 标题且长度 >= 100 的文本应返回 True"""
        text = "以下是一些内容。\n## 二级标题\n这里是一些详细的描述文字。"
        while len(text) < 100:
            text += " 补充描述内容使文本变长。"
        result = utils.is_structured_summary(text)
        self.assertTrue(result)


if __name__ == "__main__":
    unittest.main()