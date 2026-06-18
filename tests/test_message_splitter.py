"""消息切分器测试"""
from tests.test_base import YunliTestCase, setup_test_path, default_config

setup_test_path()

from yunli.persona.message_splitter import MessageSplitter


class TestMessageSplitter(YunliTestCase):
    """测试消息切分器（简化版）"""

    def setUp(self):
        self.splitter = MessageSplitter({
            'max_segment_length': 50,
            'min_segment_length': 5,
            'enable_typing_delay': False,
            'max_segments': 10,  # 测试场景：允许较多段数以验证切分逻辑
        })

    def test_split_short_text(self):
        """测试短文本不切分"""
        text = "你好呀，我是云璃。"
        segments = self.splitter.split(text)
        self.assertEqual(len(segments), 1)
        self.assertEqual(segments[0]['text'], text)

    def test_split_by_empty_lines(self):
        """测试按空行切分"""
        text = "第一段内容。\n\n第二段内容。\n\n第三段内容。"
        segments = self.splitter.split(text)
        self.assertGreaterEqual(len(segments), 2)

    def test_split_long_text(self):
        """测试超长文本切分"""
        text = "哈" * 200
        segments = self.splitter.split(text)
        self.assertTrue(len(segments) > 1)
        for seg in segments:
            self.assertLessEqual(len(seg['text']), 50)

    def test_split_by_sentences(self):
        """测试按句子切分"""
        text = "第一句。第二句。第三句。第四句。第五句。"
        segments = self.splitter.split(text)
        self.assertGreaterEqual(len(segments), 1)

    def test_merge_short_segments(self):
        """测试合并短片段"""
        text = "短。\n\n也短。\n\n还是短。"
        segments = self.splitter.split(text)
        # 短段落应被合并，最终段数少于原始段落数
        self.assertLess(len(segments), 3)

    def test_delay_calculation(self):
        """测试延迟计算"""
        splitter = MessageSplitter({
            'enable_typing_delay': True,
            'base_delay': 0.5,
            'delay_per_char': 0.03,
        })
        delay = splitter._calculate_delay("测试文本", 0, 3)
        self.assertTrue(delay > 0)

    def test_thinking_pause(self):
        """测试思考停顿"""
        # 第一段不应该有停顿
        pause_first = self.splitter.get_thinking_pause("测试文本", is_first=True)
        self.assertEqual(pause_first, '')

        # 非第一段可能有停顿
        pause = self.splitter.get_thinking_pause("测试文本", is_first=False)
        possible_pauses = ['', '…', '嗯…', '啊…', '…嗯…', '…对了…', '…让我想想…', '…嗯，怎么说呢…']
        self.assertIn(pause, possible_pauses)

    def test_limit_max_segments(self):
        """测试最大段数限制"""
        segments = ["第一段。", "第二段。", "第三段。", "第四段。", "第五段。", "第六段。"]
        result = self.splitter._limit_max_segments(segments)
        self.assertLessEqual(len(result), self.splitter.max_segments)

    def test_join_segment_pair(self):
        """测试智能连接"""
        result = self.splitter._join_segment_pair("你好", "世界")
        self.assertIn("你好", result)
        self.assertIn("世界", result)

    def test_empty_text(self):
        """测试空文本"""
        segments = self.splitter.split("")
        self.assertEqual(len(segments), 0)

        segments = self.splitter.split("   ")
        self.assertEqual(len(segments), 0)

    def test_single_sentence(self):
        """测试单句不切分"""
        text = "这是一句完整的话。"
        segments = self.splitter.split(text)
        self.assertEqual(len(segments), 1)


import unittest


if __name__ == "__main__":
    unittest.main()