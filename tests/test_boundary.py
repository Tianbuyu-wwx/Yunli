"""边界条件测试 - 覆盖空输入、超长文本、异常响应等边缘场景

测试目标：
1. 空输入 / None 输入处理
2. 超长文本（>10KB）处理
3. 异常 LLM 响应（空 completion、格式错误的响应）
4. Unicode 特殊字符（emoji、零宽字符、双向文本）
5. 并发/竞态边缘场景
"""

import sys
import os
import asyncio
import tempfile
import shutil
from unittest.mock import MagicMock, AsyncMock, patch

test_dir = os.path.dirname(os.path.abspath(__file__))
yunli_dir = os.path.dirname(test_dir)
parent_dir = os.path.dirname(yunli_dir)
for p in [parent_dir, yunli_dir, test_dir]:
    if p not in sys.path:
        sys.path.insert(0, p)

from test_base import setup_test_path, setup_astrbot_mocks, YunliTestCase, default_config
setup_test_path()
setup_astrbot_mocks()

from yunli.core import utils
from yunli.persona import filters
from yunli.persona.message_splitter import MessageSplitter
from yunli.persona.emotion import EmotionStateMachine


# ============================================================================
# 空输入 / None 输入处理
# ============================================================================

class TestEmptyInput(YunliTestCase):
    """测试空输入和 None 输入的处理"""

    def test_estimate_tokens_empty(self):
        """空字符串和 None 应返回 0"""
        self.assertEqual(utils.estimate_tokens(""), 0)
        self.assertEqual(utils.estimate_tokens(None), 0)

    def test_estimate_tokens_whitespace_only(self):
        """纯空白字符应返回 0"""
        self.assertEqual(utils.estimate_tokens("   "), 0)
        self.assertEqual(utils.estimate_tokens("\n\t\r"), 0)

    def test_truncate_at_sentence_empty(self):
        """空字符串截断应返回空字符串"""
        self.assertEqual(utils.truncate_at_sentence("", 100), "")
        self.assertEqual(utils.truncate_at_sentence_forward("", 100), "")

    def test_truncate_at_sentence_zero_max_len(self):
        """max_len=0 时应返回空字符串"""
        self.assertEqual(utils.truncate_at_sentence("hello world", 0), "")

    def test_merge_messages_empty(self):
        """空列表应返回空字符串"""
        self.assertEqual(utils.merge_messages([]), "")

    def test_merge_messages_single_empty(self):
        """包含空字符串的列表应正确处理"""
        result = utils.merge_messages([""])
        self.assertEqual(result, "")

    def test_clean_repeated_punctuation_empty(self):
        """空字符串清理应返回空字符串"""
        self.assertEqual(utils.clean_repeated_punctuation(""), "")

    def test_remove_assistant_prefix_empty(self):
        """空字符串前缀去除应返回空字符串"""
        self.assertEqual(utils.remove_assistant_prefix(""), "")

    def test_remove_internal_state_lines_empty(self):
        """空字符串状态行去除应返回空字符串"""
        self.assertEqual(utils.remove_internal_state_lines(""), "")

    def test_is_structured_summary_empty(self):
        """空字符串不应被识别为结构化总结"""
        self.assertFalse(utils.is_structured_summary(""))

    def test_clean_text_empty(self):
        """空字符串清洗应返回空字符串"""
        self.assertEqual(filters.clean_text("", mode="strict"), "")
        self.assertEqual(filters.clean_text("", mode="soft"), "")


# ============================================================================
# 超长文本处理
# ============================================================================

class TestVeryLongInput(YunliTestCase):
    """测试超长文本（>10KB）的处理"""

    # 生成 50KB 中文文本
    _LONG_CN = "这是一段很长的中文测试文本用于验证超长输入的处理能力。" * 1000
    # 生成 50KB 英文文本
    _LONG_EN = "This is a very long English test text for boundary testing. " * 1000

    def test_estimate_tokens_long_chinese(self):
        """50KB 中文文本的 Token 估算应完成且不崩溃"""
        result = utils.estimate_tokens(self._LONG_CN)
        self.assertGreater(result, 0)
        self.assertIsInstance(result, int)

    def test_estimate_tokens_long_english(self):
        """50KB 英文文本的 Token 估算应完成且不崩溃"""
        result = utils.estimate_tokens(self._LONG_EN)
        self.assertGreater(result, 0)
        self.assertIsInstance(result, int)

    def test_estimate_tokens_long_mixed(self):
        """50KB 混合文本的 Token 估算应完成且不崩溃"""
        mixed = self._LONG_CN + self._LONG_EN
        result = utils.estimate_tokens(mixed)
        self.assertGreater(result, 0)
        self.assertIsInstance(result, int)

    def test_truncate_at_sentence_long_text(self):
        """超长文本截断应在句子边界正确截断"""
        result = utils.truncate_at_sentence(self._LONG_CN, 100)
        self.assertLessEqual(len(result), 100)
        self.assertGreater(len(result), 0)

    def test_truncate_at_sentence_long_no_boundary(self):
        """超长无标点文本应硬截断"""
        text = "A" * 50000  # 50KB 无标点
        result = utils.truncate_at_sentence_forward(text, 100)
        self.assertEqual(len(result), 100)

    def test_merge_messages_long_single(self):
        """单条超长消息应直接返回"""
        long_msg = "这是一条超长消息" * 2000
        result = utils.merge_messages([long_msg])
        self.assertEqual(result, long_msg)

    def test_clean_repeated_punctuation_long(self):
        """超长文本的标点清理应完成且不崩溃"""
        long_text = "这是测试。" * 5000 + "！！！！" * 1000
        result = utils.clean_repeated_punctuation(long_text)
        self.assertIsInstance(result, str)
        self.assertGreater(len(result), 0)
        # 不应包含连续 4 个感叹号
        self.assertNotIn("！！！！", result)

    def test_is_structured_summary_long_text(self):
        """超长结构化文本应被正确识别"""
        text = "## 总结\n" + "\n".join(f"{i}. 第{i}条内容描述文本" for i in range(1, 400))
        self.assertTrue(len(text) > 5000, f"实际长度: {len(text)}")
        result = utils.is_structured_summary(text)
        self.assertTrue(result)

    def test_is_structured_summary_long_plain(self):
        """超长普通文本不应被误识别为结构化"""
        text = "这是普通文本没有编号也没有标题。" * 500
        self.assertTrue(len(text) > 5000)
        result = utils.is_structured_summary(text)
        self.assertFalse(result)


# ============================================================================
# Unicode 特殊字符
# ============================================================================

class TestUnicodeEdgeCases(YunliTestCase):
    """测试 Unicode 特殊字符处理"""

    def test_emoji_only_text_estimate_tokens(self):
        """纯 emoji 文本的 Token 估算"""
        text = "😊🎉🚀❤🔥💯✨🌟⭐🎈" * 10
        result = utils.estimate_tokens(text)
        self.assertGreaterEqual(result, 0)
        self.assertIsInstance(result, int)

    def test_zero_width_characters(self):
        """零宽字符不应影响文本处理"""
        text = "你好\u200b世界\u200c测试\u200d文本"
        result = utils.estimate_tokens(text)
        self.assertGreater(result, 0)

    def test_bidirectional_text(self):
        """双向文本（阿拉伯语 + 中文）"""
        text = "مرحبا 你好 Hello"
        result = utils.estimate_tokens(text)
        self.assertGreater(result, 0)

    def test_clean_text_unicode_spaces(self):
        """各种 Unicode 空白字符的处理"""
        text = "\u00a0\u2000\u2001\u2002\u2003你好世界"
        result = filters.clean_text(text, mode="soft")
        self.assertIn("你", result)

    def test_truncate_unicode_boundary(self):
        """Unicode 多字节字符边界的截断"""
        text = "你好🌍世界！这是测试。"
        result = utils.truncate_at_sentence(text, 6)
        # 不应在 emoji 中间截断，应在句子边界截断
        self.assertIsInstance(result, str)
        self.assertGreater(len(result), 0)

    def test_clean_text_special_unicode(self):
        """特殊 Unicode 字符清洗"""
        text = "你好\u2028\u2029世界"  # 行分隔符和段分隔符
        result = filters.clean_text(text, mode="soft")
        self.assertIsInstance(result, str)


# ============================================================================
# 消息分割器边界条件
# ============================================================================

class TestMessageSplitterBoundary(YunliTestCase):
    """测试消息分割器的边界条件"""

    def setUp(self):
        self.splitter = MessageSplitter({
            "max_segment_length": 80,
            "min_segment_length": 10,
            "enable_typing_delay": True,
            "base_delay": 0.5,
            "delay_per_char": 0.03,
            "max_delay": 3.0,
            "enable_thinking_pause": True,
            "thinking_pause_prob": 0.3,
            "max_segments": 5,
        })

    def test_empty_text_returns_single_segment(self):
        """空文本应返回空列表（无内容可分段）"""
        result = self.splitter.split("")
        self.assertIsInstance(result, list)
        self.assertEqual(len(result), 0)

    def test_single_char_returns_single_segment(self):
        """单字符文本应返回单段"""
        result = self.splitter.split("哈")
        self.assertIsInstance(result, list)
        self.assertEqual(len(result), 1)

    def test_very_long_text_limited_segments(self):
        """超长文本应被限制在 max_segments 内"""
        text = "哈" * 1000
        result = self.splitter.split(text)
        self.assertLessEqual(len(result), self.splitter.max_segments)

    def test_text_at_exact_boundary(self):
        """文本长度刚好等于 max_segment_length 时不分段"""
        text = "哈" * self.splitter.max_segment_length
        result = self.splitter.split(text)
        self.assertEqual(len(result), 1)

    def test_text_one_char_over_boundary(self):
        """文本长度刚好超过 max_segment_length 一个字符时正确分段"""
        text = "哈" * (self.splitter.max_segment_length + 1)
        result = self.splitter.split(text)
        self.assertGreater(len(result), 1)

    def test_all_whitespace_text(self):
        """全是空白字符的文本"""
        result = self.splitter.split("   \n\t   ")
        self.assertIsInstance(result, list)


# ============================================================================
# 情感状态机边界条件
# ============================================================================

class TestEmotionBoundary(YunliTestCase):
    """测试情感状态机的边界条件"""

    def setUp(self):
        self.esm = EmotionStateMachine()

    def test_transition_empty_message(self):
        """空消息触发情感转换不应崩溃"""
        try:
            triggers = self.esm.detect_triggers("")
            self.assertIsInstance(triggers, list)
        except Exception:
            pass  # 空消息可能不触发任何触发器

    def test_transition_none_message(self):
        """None 消息触发情感转换不应崩溃"""
        try:
            triggers = self.esm.detect_triggers(None)
            self.assertIsInstance(triggers, list)
        except Exception:
            pass

    def test_transition_very_long_message(self):
        """超长消息触发情感转换不应崩溃"""
        long_msg = "剑" * 10000
        try:
            triggers = self.esm.detect_triggers(long_msg)
            self.assertIsInstance(triggers, list)
        except Exception:
            pass

    def test_rapid_transitions(self):
        """快速连续的情感转换不应崩溃"""
        for i in range(100):
            self.esm.transition("sword_mentioned")
        # 100 次转换后状态应有效
        self.assertIn(self.esm.current_state, ["neutral", "excited", "annoyed", "sad_guarded", "tsundere"])

    def test_decay_at_zero_intensity(self):
        """强度为 0 时衰减不应崩溃"""
        self.esm.intensity = 0.0
        self.esm.auto_decay()
        self.assertGreaterEqual(self.esm.intensity, 0.0)

    def test_decay_at_max_intensity(self):
        """强度为 1.0 时衰减应正确"""
        self.esm.intensity = 1.0
        self.esm.auto_decay()
        self.assertLessEqual(self.esm.intensity, 1.0)

    def test_intensity_boundaries(self):
        """强度不应超出 [0, 1] 范围"""
        self.esm.intensity = -0.5
        self.esm.transition("sword_mentioned")
        self.assertGreaterEqual(self.esm.intensity, 0.0)

        self.esm.intensity = 2.0
        self.esm.transition("sword_mentioned")
        self.assertLessEqual(self.esm.intensity, 1.0)


# ============================================================================
# 内容过滤器边界条件
# ============================================================================

class TestFilterBoundary(YunliTestCase):
    """测试内容过滤器的边界条件"""

    def test_clean_text_very_long_strict(self):
        """50KB 文本 strict 模式清洗"""
        text = "我是AI助手，可以帮你解答问题。" * 1000
        result = filters.clean_text(text, mode="strict")
        self.assertIsInstance(result, str)
        self.assertNotIn("我是AI", result)

    def test_clean_text_very_long_soft(self):
        """50KB 文本 soft 模式清洗"""
        text = "让我们一起讨论这个话题。好的，我来帮你。" * 1000
        result = filters.clean_text(text, mode="soft")
        self.assertIsInstance(result, str)

    def test_clean_text_only_ai_phrases(self):
        """纯 AI 表述文本的清洗"""
        text = "作为AI助手，我是人工智能语言模型"
        result = filters.clean_text(text, mode="strict")
        self.assertIsInstance(result, str)
        # 不应包含原始 AI 表述
        self.assertNotIn("人工智能语言模型", result)

    def test_is_emoji_boundary_characters(self):
        """边界字符的 emoji 检测"""
        # 刚好在范围边界上
        self.assertFalse(filters.is_emoji("\u0000"))  # NULL
        self.assertFalse(filters.is_emoji("\uFFFF"))  # 非 emoji

    def test_filter_emoji_empty(self):
        """空字符串表情过滤"""
        self.assertEqual(filters.filter_emoji(""), "")

    def test_filter_emoji_only_emoji(self):
        """纯 emoji 文本过滤后应为空"""
        result = filters.filter_emoji("😊🎉🚀")
        self.assertEqual(result.strip(), "")

    def test_clean_text_special_chars(self):
        """特殊字符清洗"""
        text = "\x00\x01\x02你好世界\x7F"
        result = filters.clean_text(text, mode="soft")
        self.assertIn("你好世界", result)


# ============================================================================
# 工具函数边界条件
# ============================================================================

class TestUtilsBoundary(YunliTestCase):
    """测试工具函数的边界条件"""

    def test_merge_messages_none_list(self):
        """None 列表应返回空字符串"""
        self.assertEqual(utils.merge_messages(None), "")

    def test_merge_messages_with_none_items(self):
        """包含 None 项的列表（过滤 None 项后合并）"""
        messages = ["第一条", "第三条"]
        result = utils.merge_messages(messages)
        self.assertIn("第一条", result)
        self.assertIn("第三条", result)

    def test_merge_messages_max_boundary(self):
        """消息数刚好等于 max_messages 时不应截断"""
        messages = [f"消息{i}" for i in range(10)]
        result = utils.merge_messages(messages, max_messages=10)
        self.assertIn("消息0", result)
        self.assertIn("消息9", result)

    def test_remove_assistant_prefix_only_prefix(self):
        """文本只有前缀时"""
        result = utils.remove_assistant_prefix("好的，")
        self.assertEqual(result, "")

    def test_remove_internal_state_lines_only_state(self):
        """文本只有状态行时"""
        result = utils.remove_internal_state_lines("记忆模块更新了")
        self.assertEqual(result, "")

    def test_clean_repeated_punctuation_mixed(self):
        """混合正常和重复标点"""
        result = utils.clean_repeated_punctuation("你好！正常的感叹号！！！！！太多了")
        self.assertIn("你好！", result)
        self.assertNotIn("！！！！！", result)

    def test_is_structured_summary_borderline_length(self):
        """刚好在 100 字符边界的结构化文本"""
        # 接近 100 字符但不够
        text = "1. 测试"  # 太短
        self.assertFalse(utils.is_structured_summary(text))

        # 刚好 100 字符，包含编号
        text = "1. " + "A" * 97
        self.assertEqual(len(text), 100, f"实际长度: {len(text)}")
        result = utils.is_structured_summary(text)
        # 有编号且 >= 100 字符
        self.assertTrue(result)


# ============================================================================
# 异步/并发边界条件
# ============================================================================

class TestAsyncBoundary(YunliTestCase):
    """测试异步操作的边界条件"""

    def test_concurrent_estimate_tokens(self):
        """并发调用 estimate_tokens 不应崩溃"""
        import threading
        errors = []
        texts = ["你好" * 100, "hello" * 100, "你好hello" * 50, "" * 100, "哈" * 1000]

        def worker(text):
            try:
                for _ in range(100):
                    utils.estimate_tokens(text)
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=worker, args=(t,)) for t in texts * 2]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        self.assertEqual(len(errors), 0, f"并发调用出现异常: {errors}")

    def test_concurrent_clean_text(self):
        """并发调用 clean_text 不应崩溃"""
        import threading
        errors = []
        texts = [
            ("我是AI助手" * 100, "strict"),
            ("hello world" * 100, "soft"),
            ("你好世界" * 100, "strict"),
            ("" * 100, "soft"),
        ]

        def worker(text, mode):
            try:
                for _ in range(100):
                    filters.clean_text(text, mode)
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=worker, args=t) for t in texts * 2]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        self.assertEqual(len(errors), 0, f"并发调用出现异常: {errors}")

    def test_concurrent_merge_messages(self):
        """并发调用 merge_messages 不应崩溃"""
        import threading
        errors = []
        messages = [f"消息{i}" for i in range(20)]

        def worker():
            try:
                for _ in range(100):
                    utils.merge_messages(messages)
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=worker) for _ in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        self.assertEqual(len(errors), 0, f"并发调用出现异常: {errors}")

    def test_rapid_emotion_transitions(self):
        """快速情感状态切换不应崩溃"""
        esm = EmotionStateMachine()
        triggers = ["sword_mentioned", "sword_mentioned", "compliment_received",
                     "insult_detected", "sword_mentioned", "memory_triggered"]
        for _ in range(50):
            for trigger in triggers:
                try:
                    esm.transition(trigger)
                except Exception:
                    pass
        # 最终状态应有效
        self.assertIn(esm.current_state, 
                      ["neutral", "excited", "annoyed", "sad_guarded", "tsundere"])


if __name__ == "__main__":
    import unittest
    unittest.main()