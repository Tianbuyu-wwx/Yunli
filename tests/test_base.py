"""
测试共享基础设施

用法：
    from tests.test_base import YunliTestCase, setup_astrbot_mocks

注意事项：
    - 测试 persona/* 模块不需要 astrbot mock
    - 测试 main.py（YunliPersonaPlugin）必须在导入前调用 setup_astrbot_mocks()
    - 所有导入统一使用 from yunli.xxx import yyy 格式
"""

import os
import sys
import unittest
from typing import Any, Dict, List, Optional
from unittest.mock import MagicMock


def setup_test_path():
    """设置 sys.path，使 from yunli.xxx import yyy 生效

    yunli/ 的父目录必须加入 sys.path，否则 persona/core.py 的
    from ..core import utils 会因越级包而出 ImportError。
    """
    test_dir = os.path.dirname(os.path.abspath(__file__))       # .../yunli/tests
    yunli_dir = os.path.dirname(test_dir)                        # .../yunli
    parent_dir = os.path.dirname(yunli_dir)                      # .../yunli 的父目录
    if parent_dir not in sys.path:
        sys.path.insert(0, parent_dir)
    if yunli_dir not in sys.path:
        sys.path.insert(0, yunli_dir)


_astrbot_mock = None


def setup_astrbot_mocks():
    """初始化 AstrBot mock 模块（幂等，多次调用返回同一实例）

    使用 main.py（YunliPersonaPlugin）的测试必须在导入前调用此函数。
    """
    global _astrbot_mock
    if _astrbot_mock is not None:
        return _astrbot_mock

    astrbot_mock = MagicMock()
    _astrbot_mock = astrbot_mock
    sys.modules['astrbot'] = astrbot_mock
    sys.modules['astrbot.api'] = astrbot_mock.api
    sys.modules['astrbot.api.star'] = astrbot_mock.api.star
    sys.modules['astrbot.api.event'] = astrbot_mock.api.event
    sys.modules['astrbot.api.provider'] = astrbot_mock.api.provider
    sys.modules['astrbot.api.message_components'] = astrbot_mock.api.message_components
    sys.modules['astrbot.core'] = astrbot_mock.core
    sys.modules['astrbot.core.message'] = astrbot_mock.core.message
    sys.modules['astrbot.core.message.message_event_result'] = astrbot_mock.core.message.message_event_result
    sys.modules['astrbot.api.star.star'] = astrbot_mock.api.star.star
    sys.modules['astrbot.core.star'] = astrbot_mock.core.star
    sys.modules['astrbot.core.plugin'] = astrbot_mock.core.plugin

    # 设置 mock 类和装饰器
    class MockStar:
        def __init__(self, context=None):
            self.context = context

    astrbot_mock.api.star.Context = object
    astrbot_mock.api.star.Star = MockStar
    astrbot_mock.api.star.register = lambda *args, **kwargs: lambda cls: cls
    astrbot_mock.api.event.filter = MagicMock()
    astrbot_mock.api.event.AstrMessageEvent = MagicMock
    astrbot_mock.api.provider.ProviderRequest = object
    astrbot_mock.api.provider.LLMResponse = object
    astrbot_mock.api.message_components.At = object
    astrbot_mock.api.message_components.Plain = object
    astrbot_mock.core.message.message_event_result.MessageChain = object
    astrbot_mock.api.star.star = MagicMock()
    astrbot_mock.core.star = MagicMock()
    astrbot_mock.core.plugin = MagicMock()

    return astrbot_mock


def default_config(overrides: Dict[str, Any] = None) -> Dict[str, Any]:
    """创建包含默认值的测试配置"""
    config = {
        "max_segment_length": 180,
        "min_segment_length": 10,
        "enable_typing_delay": True,
        "base_delay": 0.5,
        "delay_per_char": 0.03,
        "max_delay": 3.0,
        "enable_thinking_pause": True,
        "thinking_pause_prob": 0.3,
        "max_segments": 5,
        "strict_identity": True,
        "persona_strength": 0.8,
        "identity_preservation": True,
        "modern_analogy_mode": True,
        "max_text_length": 200,
        "enable_proactive_reply": False,
        "enable_hybrid_memory": True,
    }
    if overrides:
        config.update(overrides)
    return config


class YunliTestCase(unittest.TestCase):
    """所有测试用例的共享基类"""

    maxDiff = None

    @classmethod
    def setUpClass(cls):
        setup_test_path()

    def assertSegmentStructure(self, segments: List[Dict], expected_count: int = None):
        """验证分段结果格式正确"""
        self.assertIsInstance(segments, list)
        for seg in segments:
            self.assertIn('text', seg)
            self.assertIn('delay', seg)
            self.assertIsInstance(seg['text'], str)
            self.assertIsInstance(seg['delay'], (int, float))
            self.assertGreater(len(seg['text'].strip()), 0)
        if expected_count is not None:
            self.assertEqual(len(segments), expected_count)

    def assertTextContains(self, text: str, *expected: str):
        """验证文本包含所有预期子串"""
        for item in expected:
            self.assertIn(item, text, f"文本应包含「{item}」")

    def assertTextNotContains(self, text: str, *unexpected: str):
        """验证文本不包含任何不应出现的子串"""
        for item in unexpected:
            self.assertNotIn(item, text, f"文本不应包含「{item}」")