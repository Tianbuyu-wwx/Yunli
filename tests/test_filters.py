"""yunli.persona.filters 模块的单元测试"""

import sys
import os
import unittest

# 路径设置：tests/ 目录用于 import test_base，yunli/ 父目录用于 import yunli.xxx
test_dir = os.path.dirname(os.path.abspath(__file__))          # .../yunli/tests
yunli_dir = os.path.dirname(test_dir)                           # .../yunli
parent_dir = os.path.dirname(yunli_dir)                         # .../yunli 的父目录
for p in [parent_dir, yunli_dir, test_dir]:
    if p not in sys.path:
        sys.path.insert(0, p)

from test_base import setup_test_path
setup_test_path()

from yunli.persona import filters
from test_base import YunliTestCase


# ============================================================================
# is_emoji
# ============================================================================

class TestIsEmoji(YunliTestCase):
    """测试 is_emoji 单字符检测"""

    def test_is_emoji_returns_true_for_emoji(self):
        """常见 emoji 字符应返回 True"""
        self.assertTrue(filters.is_emoji("😊"))
        self.assertTrue(filters.is_emoji("🎉"))
        self.assertTrue(filters.is_emoji("🚀"))
        self.assertTrue(filters.is_emoji("❤"))

    def test_is_emoji_returns_false_for_text(self):
        """普通文字和符号应返回 False"""
        self.assertFalse(filters.is_emoji("a"))
        self.assertFalse(filters.is_emoji("中"))
        self.assertFalse(filters.is_emoji("。"))
        self.assertFalse(filters.is_emoji(" "))

    def test_is_emoji_edge_cases(self):
        """边界字符"""
        # 刚好在 EMOJI_RANGES 边界上的字符
        self.assertFalse(filters.is_emoji(chr(0x2701)))  # 2702 开始
        self.assertTrue(filters.is_emoji(chr(0x2702)))   # 装饰符号起始
        self.assertTrue(filters.is_emoji(chr(0x27B0)))   # 装饰符号结束
        self.assertFalse(filters.is_emoji(chr(0x27B1)))  # 27B0 之后


# ============================================================================
# filter_emoji
# ============================================================================

class TestFilterEmoji(YunliTestCase):
    """测试 filter_emoji 表情过滤"""

    def test_filter_emoji_removes_common_emoji(self):
        """过滤常见 emoji 表情符号"""
        self.assertEqual(filters.filter_emoji("你好😊世界"), "你好世界")
        self.assertEqual(filters.filter_emoji("🎉🎊庆祝"), "庆祝")
        self.assertEqual(filters.filter_emoji("🚀发射"), "发射")

    def test_filter_emoji_handles_no_emoji(self):
        """无 emoji 的文本保持不变"""
        self.assertEqual(filters.filter_emoji("你好世界"), "你好世界")
        self.assertEqual(filters.filter_emoji("Hello World"), "Hello World")
        self.assertEqual(filters.filter_emoji("测试123！"), "测试123！")

    def test_filter_emoji_handles_empty_string(self):
        """空字符串返回空字符串"""
        self.assertEqual(filters.filter_emoji(""), "")
        self.assertEqual(filters.filter_emoji("  "), "  ")

    def test_filter_emoji_removes_multiple_emoji(self):
        """连续多个 emoji 全部过滤"""
        self.assertEqual(filters.filter_emoji("😊😊😊"), "")


# ============================================================================
# filter_action_words
# ============================================================================

class TestFilterActionWords(YunliTestCase):
    """测试 filter_action_words 动作词过滤"""

    def test_filter_action_words_removes_asterisk_marked(self):
        """移除 *动作词* 格式（通过 _ACTION_MARKED_PATTERNS）"""
        # "笑了笑" 在 ACTION_WORDS 中，*笑了笑* 会被 _ACTION_MARKED_PATTERNS 匹配
        result = filters.filter_action_words("她*笑了笑*说道")
        self.assertNotIn("笑了笑", result)

    def test_filter_action_words_removes_paren_formats(self):
        """移除括号格式的动作词如 (笑)、（无奈）"""
        result = filters.filter_action_words("他(笑)了一下")
        self.assertNotIn("(笑)", result)

        result2 = filters.filter_action_words("她（无奈）地摇头")
        self.assertNotIn("（无奈）", result2)

    def test_filter_action_words_no_action(self):
        """无动作词时文本不变"""
        text = "今天天气真好"
        self.assertEqual(filters.filter_action_words(text), text)

    def test_filter_action_words_handles_nested_mixed(self):
        """混合多个动作词"""
        text = "他*笑了笑*然后（无奈）地*摇了摇头*"
        result = filters.filter_action_words(text)
        self.assertNotIn("笑了笑", result)
        self.assertNotIn("（无奈）", result)
        self.assertNotIn("摇了摇头", result)

    def test_filter_action_words_empty_string(self):
        """空字符串返回空字符串"""
        self.assertEqual(filters.filter_action_words(""), "")
        self.assertEqual(filters.filter_action_words("  "), "  ")


# ============================================================================
# filter_ai_phrases
# ============================================================================

class TestFilterAiPhrases(YunliTestCase):
    """测试 filter_ai_phrases AI 表述过滤"""

    def test_filter_ai_phrases_replaces_ai_identity(self):
        """"我是AI" 类表述应被替换为 '我是云璃'"""
        tests = [
            ("我是AI", "我是云璃"),
            ("我是人工智能", "我是云璃"),
            ("我是语言模型", "我是云璃"),
            ("我是机器人", "我是云璃"),
            ("我没有感情", "我是云璃"),
            ("我只是程序", "我是云璃"),
            ("我的训练数据", "我是云璃"),
        ]
        for original, expected in tests:
            self.assertEqual(filters.filter_ai_phrases(original), expected)

    def test_filter_ai_phrases_normal_text(self):
        """不含 AI 表述的文本保持不变"""
        text = "你好，今天天气不错"
        self.assertEqual(filters.filter_ai_phrases(text), text)
        self.assertEqual(filters.filter_ai_phrases(""), "")

    def test_filter_ai_phrases_replaces_in_context(self):
        """AI 表述在句子中被替换"""
        text = "我是AI助手，可以帮你解答问题"
        result = filters.filter_ai_phrases(text)
        self.assertIn("我是云璃", result)
        self.assertNotIn("我是AI", result)

    def test_filter_ai_phrases_custom_replacement(self):
        """支持自定义替换文本"""
        text = "我是AI"
        result = filters.filter_ai_phrases(text, replacement="我是人类")
        self.assertEqual(result, "我是人类")


# ============================================================================
# FormatProtector
# ============================================================================

class TestFormatProtector(YunliTestCase):
    """测试 FormatProtector 格式保护/恢复"""

    def test_protect_and_restore_markdown_bold(self):
        """保护并恢复 Markdown 粗体"""
        protector = filters.FormatProtector()
        text = "Hello **world** here"
        protected = protector.protect(text, "bold", filters.MARKDOWN_BOLD_PATTERN)
        self.assertNotIn("**world**", protected)
        self.assertIn("\x00bold0\x00", protected)
        restored = protector.restore(protected, "bold")
        self.assertEqual(restored, text)

    def test_protect_and_restore_code_blocks(self):
        """保护并恢复代码块"""
        protector = filters.FormatProtector()
        text = "Use `code` here"
        protected = protector.protect(text, "code", filters.CODE_BLOCK_PATTERN)
        self.assertNotIn("`code`", protected)
        self.assertIn("\x00code0\x00", protected)
        restored = protector.restore(protected, "code")
        self.assertEqual(restored, text)

    def test_multiple_protections(self):
        """多种格式同时保护并 FILO 恢复"""
        protector = filters.FormatProtector()
        text = "**bold** and `code`"
        protected = protector.protect(text, "markdown_bold", filters.MARKDOWN_BOLD_PATTERN)
        protected = protector.protect(protected, "code", filters.CODE_BLOCK_PATTERN)
        self.assertIn("\x00markdown_bold0\x00", protected)
        self.assertIn("\x00code0\x00", protected)
        restored = protector.restore_all(protected)
        self.assertEqual(restored, text)

    def test_restore_with_modified_content(self):
        """修改占位符周围内容后仍能正确恢复"""
        protector = filters.FormatProtector()
        text = "**important**"
        protected = protector.protect(text, "bold", filters.MARKDOWN_BOLD_PATTERN)
        modified = protected.replace("\x00bold0\x00", "(\x00bold0\x00)")
        restored = protector.restore(modified, "bold")
        self.assertEqual(restored, "(**important**)")

    def test_clear_and_reuse(self):
        """清除后可以重复使用保护器"""
        protector = filters.FormatProtector()
        protector.protect("**bold**", "bold", filters.MARKDOWN_BOLD_PATTERN)
        protector.clear()
        self.assertEqual(protector._protected, {})
        text = "`code`"
        protected = protector.protect(text, "code", filters.CODE_BLOCK_PATTERN)
        self.assertIn("\x00code0\x00", protected)
        self.assertEqual(protector.restore(protected, "code"), "`code`")


# ============================================================================
# clean_text — strict mode
# ============================================================================

class TestCleanTextStrict(YunliTestCase):
    """测试 clean_text strict 模式 — 完整过滤"""

    def test_strict_filters_action_asterisk(self):
        """过滤 *中文* 格式的动作描述"""
        result = filters.clean_text("Hello *微笑* world", mode="strict")
        self.assertNotIn("*微笑*", result)
        self.assertIn("Hello", result)
        self.assertIn("world", result)

    def test_strict_filters_action_angle(self):
        """过滤 <动作> 格式"""
        result = filters.clean_text("她<挥了挥手>说道", mode="strict")
        self.assertNotIn("<挥了挥手>", result)

    def test_strict_filters_emotion_labels(self):
        """过滤 【心情】类情感标签"""
        result = filters.clean_text("今天真开心【心情很好】", mode="strict")
        self.assertNotIn("【心情很好】", result)

    def test_strict_filters_emoji(self):
        """过滤 emoji 表情"""
        result = filters.clean_text("你好😊世界", mode="strict")
        self.assertNotIn("😊", result)

    def test_strict_filters_ai_phrases(self):
        """过滤 AI 相关表述"""
        result = filters.clean_text("我是AI助手", mode="strict")
        self.assertIn("我是云璃", result)
        self.assertNotIn("我是AI", result)

    def test_strict_returns_ellipsis_for_empty_content(self):
        """内容全部被过滤后返回 '…'"""
        result = filters.clean_text("*微笑*【心情很好】😊", mode="strict")
        self.assertEqual(result, "…")

    def test_strict_preserves_normal_text(self):
        """纯文本主要内容保持不变（严格模式会清理多余标点）"""
        text = "你好，今天天气真不错！"
        result = filters.clean_text(text, mode="strict")
        self.assertIn("你好，今天天气真不错", result)

    def test_strict_mixed_content(self):
        """混合内容：正常文字 + 动作 + emoji 被过滤，剩余内容保留"""
        result = filters.clean_text("Hello *微笑* 世界😊 你好", mode="strict")
        self.assertIn("Hello", result)
        self.assertIn("世界", result)
        self.assertIn("你好", result)
        self.assertNotIn("*微笑*", result)
        self.assertNotIn("😊", result)


# ============================================================================
# clean_text — light mode
# ============================================================================

class TestCleanTextLight(YunliTestCase):
    """测试 clean_text light 模式 — 轻量过滤"""

    def test_light_filters_ai_phrases(self):
        """AI 表述被替换"""
        result = filters.clean_text("我是AI助手", mode="light")
        self.assertIn("我是云璃", result)
        self.assertNotIn("我是AI", result)

    def test_light_filters_action_formats(self):
        """动作格式 *action* 仍被过滤"""
        result = filters.clean_text("Hello *微笑* world", mode="light")
        self.assertNotIn("*微笑*", result)

    def test_light_keeps_emoji(self):
        """emoji 在 light 模式下保留"""
        result = filters.clean_text("你好😊世界", mode="light")
        self.assertIn("😊", result)

    def test_light_preserves_normal_text(self):
        """纯文本保持不变"""
        text = "今天天气真不错"
        result = filters.clean_text(text, mode="light")
        self.assertEqual(result, text)

    def test_light_normalizes_spaces(self):
        """多余空格被合并"""
        result = filters.clean_text("hello   world", mode="light")
        self.assertEqual(result, "hello world")


# ============================================================================
# clean_text — format mode
# ============================================================================

class TestCleanTextFormat(YunliTestCase):
    """测试 clean_text format 模式 — 仅格式清理"""

    def test_format_filters_action_formats(self):
        """动作格式被过滤"""
        result = filters.clean_text("*微笑* 你好", mode="format")
        self.assertNotIn("*微笑*", result)

    def test_format_keeps_ai_phrases(self):
        """AI 表述在 format 模式下保留（不做替换）"""
        text = "我是AI助手"
        result = filters.clean_text(text, mode="format")
        self.assertIn("我是AI", result)

    def test_format_cleans_brackets_and_spaces(self):
        """括号和多余空格被清理"""
        result = filters.clean_text("你好  ，世界", mode="format")
        self.assertNotIn("  ", result)

    def test_format_removes_emoji(self):
        """emoji 被过滤"""
        result = filters.clean_text("你好😊世界", mode="format")
        self.assertNotIn("😊", result)


# ============================================================================
# clean_text — edge cases
# ============================================================================

class TestCleanTextEdgeCases(YunliTestCase):
    """测试 clean_text 边界情况"""

    def test_empty_string_all_modes(self):
        """空字符串在所有模式下都返回空字符串"""
        self.assertEqual(filters.clean_text("", mode="strict"), "")
        self.assertEqual(filters.clean_text("", mode="light"), "")
        self.assertEqual(filters.clean_text("", mode="format"), "")

    def test_whitespace_only_strict(self):
        """仅有空白字符时 strict 模式返回 '…'"""
        result = filters.clean_text("   ", mode="strict")
        self.assertEqual(result, "…")

    def test_whitespace_only_light(self):
        """仅有空白字符时 light 模式返回空白"""
        result = filters.clean_text("   ", mode="light")
        self.assertEqual(result, " ")

    def test_whitespace_only_format(self):
        """仅有空白字符时 format 模式返回空（首尾标点被清理）"""
        result = filters.clean_text("   ", mode="format")
        self.assertEqual(result, "")

    def test_long_text(self):
        """较长文本处理"""
        # 用空格分隔的动作模式避免连续星号被误判为 Markdown 粗体
        text = "你好 " + "*微笑* " * 10 + "世界"
        result = filters.clean_text(text, mode="strict")
        self.assertNotIn("*微笑*", result)
        self.assertIn("你好", result)
        self.assertIn("世界", result)

    def test_unknown_mode_returns_original(self):
        """未知模式返回原始文本"""
        text = "你好世界"
        result = filters.clean_text(text, mode="unknown")
        self.assertEqual(result, text)


# ============================================================================
# clean_text — comprehensive (integration)
# ============================================================================

class TestCleanTextIntegration(YunliTestCase):
    """集成测试 —— 模拟真实场景"""

    def test_typical_ai_response_strict(self):
        """模拟 AI 典型回复在 strict 模式下完全净化"""
        text = (
            "*微笑* 你好呀！【心情愉快】"
            "我是AI助手，很高兴认识你😊"
        )
        result = filters.clean_text(text, mode="strict")
        self.assertNotIn("*微笑*", result)
        self.assertNotIn("【心情愉快】", result)
        self.assertNotIn("我是AI", result)
        self.assertNotIn("😊", result)
        self.assertIn("我是云璃", result)

    def test_typical_ai_response_light(self):
        """模拟 AI 典型回复在 light 模式下仅处理部分内容"""
        text = (
            "*微笑* 你好呀！【心情愉快】"
            "我是AI助手，很高兴认识你😊"
        )
        result = filters.clean_text(text, mode="light")
        self.assertNotIn("*微笑*", result)
        self.assertNotIn("【心情愉快】", result)
        self.assertIn("我是云璃", result)
        self.assertNotIn("我是AI", result)
        # light 模式保留 emoji
        self.assertIn("😊", result)

    def test_typical_ai_response_format(self):
        """模拟 AI 典型回复在 format 模式下仅清理格式"""
        text = (
            "*微笑* 你好呀！【心情愉快】"
            "我是AI助手，很高兴认识你😊"
        )
        result = filters.clean_text(text, mode="format")
        self.assertNotIn("*微笑*", result)
        self.assertNotIn("【心情愉快】", result)
        self.assertNotIn("😊", result)
        # format 模式不做 AI 短语替换
        self.assertIn("我是AI", result)


# ============================================================================
# clean_text — specific pattern coverage
# ============================================================================

class TestCleanTextPatterns(YunliTestCase):
    """测试 clean_text 对各种具体正则模式的覆盖"""

    def test_markdown_bold_preserved_in_strict(self):
        """Markdown 粗体在 strict 模式下被保护"""
        text = "这是一段**重要文本**，请记住"
        result = filters.clean_text(text, mode="strict")
        self.assertIn("**重要文本**", result)

    def test_code_block_preserved_in_strict(self):
        """代码块在 strict 模式下被保护"""
        text = "请运行 `print('hello')` 命令"
        result = filters.clean_text(text, mode="strict")
        self.assertIn("`print('hello')`", result)

    def test_kaomoji_protected_in_strict(self):
        """颜文字（如 *^_^*）在 strict 模式下被保护"""
        text = "你好 *^_^* 世界"
        result = filters.clean_text(text, mode="strict")
        self.assertIn("*^_^*", result)

    def test_action_word_in_asterisks_removed(self):
        """动作词以 *动作词* 格式出现时被移除"""
        # "笑了笑" 在 ACTION_WORDS 列表中
        result = filters.clean_text("*笑了笑*", mode="strict")
        self.assertEqual(result, "…")

    def test_symbol_kaomoji_removed_in_strict(self):
        """符号颜文字（如 qwq）在 strict 模式下被移除"""
        result = filters.clean_text("你好 qwq 世界", mode="strict")
        self.assertNotIn("qwq", result)

    def test_emotion_label_fallback_removed(self):
        """兜底情感标签模式匹配"""
        result = filters.clean_text("【笑哭】", mode="strict")
        self.assertEqual(result, "…")


if __name__ == "__main__":
    unittest.main()