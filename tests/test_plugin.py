"""
云璃人格插件主类测试

测试 YunliPersonaPlugin 的所有公开方法和重要私有方法。
"""

import os
import sys
import asyncio
import tempfile
import shutil
from unittest.mock import MagicMock, patch, AsyncMock

# ─── 路径设置 ───────────────────────────────────────────────
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# ─── 先设置 astrbot mock，再导入 yunli 模块 ────────────────
import unittest

from tests.test_base import setup_test_path, setup_astrbot_mocks, YunliTestCase, default_config

setup_astrbot_mocks()
# 修复 filter 装饰器为恒等装饰，避免 @filter.on_llm_request 将 async 方法替换为 MagicMock
import astrbot.api.event
astrbot.api.event.filter.on_llm_request = lambda **kwargs: lambda f: f
astrbot.api.event.filter.on_llm_response = lambda **kwargs: lambda f: f
astrbot.api.event.filter.command = lambda *args, **kwargs: lambda f: f
setup_test_path()

# 现在安全地导入 yunli 模块
from yunli.main import YunliPersonaPlugin
from yunli.core.request_context import RequestContext


# ===================================================================
# 测试主类
# ===================================================================
class TestYunliPersonaPlugin(YunliTestCase):
    """测试云璃人格插件主类"""

    # ── SetUp / TearDown ────────────────────────────────────────

    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.context = MagicMock(spec=[])
        self.context.data_dir = self.temp_dir
        self.context.config = MagicMock(spec=[])
        if hasattr(self.context.config, 'platform_settings'):
            delattr(self.context.config, 'platform_settings')

        # 启动所有 patcher，替换构造函数依赖
        self._patchers = []
        self._start_patcher('yunli.main.YunliDatabase', 'mock_db')
        self._start_patcher('yunli.main.YunliPersonaEngine', 'mock_engine')
        self._start_patcher('yunli.main.QQBehaviorManager', 'mock_qq_behavior')
        self._start_patcher('yunli.main.RelationshipManager', 'mock_relationship')
        self._start_patcher('yunli.main.MessageSplitter', 'mock_splitter')
        self._start_patcher('yunli.main.MessageDebouncer', 'mock_debouncer')
        self._start_patcher('yunli.main.AtDetector', 'mock_at_detector')
        self._start_patcher('yunli.main.ContextBuilder', 'mock_ctx_builder')
        self._start_patcher('yunli.main.GroupPerception', 'mock_perception')
        self._start_patcher('yunli.main.MemoryManager', 'mock_memory')

        self._configure_mocks()

        self.config = default_config()
        self.plugin = YunliPersonaPlugin(self.context, self.config)
        # _log_interaction 是同步方法，patch 为 AsyncMock 使其返回 coroutine
        self.plugin._log_interaction = AsyncMock()

    def _start_patcher(self, target: str, attr_name: str):
        patcher = patch(target)
        mock_class = patcher.start()
        mock_instance = MagicMock()
        mock_class.return_value = mock_instance
        self._patchers.append(patcher)
        setattr(self, attr_name, mock_instance)
        setattr(self, attr_name + '_class', mock_class)

    def _configure_mocks(self):
        """配置所有 mock 的默认返回值"""
        self.mock_db.query_dialogues.return_value = []

        self.mock_engine.build_system_prompt.return_value = "你是云璃..."
        self.mock_engine.build_dynamic_prompt.return_value = ""
        self.mock_engine.polish_response.side_effect = lambda text, *a, **kw: text
        self.mock_engine.review_response.side_effect = lambda text, *a, **kw: text
        self.mock_engine.get_direct_response.return_value = None
        self.mock_engine.get_context_data.return_value = {
            "relevant_knowledge": [], "analogies": [], "user_history": None,
        }
        self.mock_engine.emotion = MagicMock()
        self.mock_engine.emotion.current_state = "neutral"
        self.mock_engine.emotion.transition = MagicMock()
        self.mock_engine.language = MagicMock()
        self.mock_engine.language.detect_query_mode.return_value = "chat"

        self.mock_qq_behavior.format_for_qq.side_effect = lambda text, *a, **kw: text
        self.mock_qq_behavior.should_skip_punctuation.return_value = False
        self.mock_qq_behavior.add_typing_pause.side_effect = lambda text, *a, **kw: text
        self.mock_qq_behavior.add_human_touches.side_effect = lambda text, *a, **kw: text

        self.mock_relationship.get_reply_length_limit.return_value = None
        self.mock_relationship.get_hint.return_value = None

        self.mock_splitter.max_segment_length = 180
        self.mock_splitter.min_segment_length = 10
        self.mock_splitter.enable_thinking_pause = True
        self.mock_splitter.enable_typing_delay = True
        self.mock_splitter.base_delay = 0.5
        self.mock_splitter.delay_per_char = 0.03
        self.mock_splitter.max_delay = 3.0
        self.mock_splitter.thinking_pause_prob = 0.3
        self.mock_splitter.split.return_value = []
        self.mock_splitter.get_thinking_pause.return_value = ""

        self.mock_debouncer.handle_message = AsyncMock(return_value=False)
        self.mock_debouncer.mark_processed = MagicMock()

        self.mock_at_detector.is_at_me.return_value = True

        self.mock_ctx_builder.format_environment_perception.return_value = ""
        self.mock_ctx_builder.add_relationship_context.return_value = ""
        self.mock_ctx_builder.build_chat_context.return_value = ""
        self.mock_ctx_builder.build_recent_chat_history.return_value = ""

        self.mock_perception_class.extract_scene_signals = MagicMock(return_value={})
        self.mock_perception_class.format_scene_description = MagicMock(return_value="")
        self.mock_perception.get_atmosphere_text = MagicMock(return_value="")
        self.mock_perception.update_atmosphere = MagicMock()
        self.mock_perception.detect_topic = MagicMock()
        self.mock_perception.update_topic_threads = MagicMock()

        self.mock_memory.log_interaction = MagicMock()

    def tearDown(self):
        for patcher in self._patchers:
            patcher.stop()
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    # ── Event / Req / Response 工厂 ──────────────────────────

    def _make_event(self, **kwargs) -> MagicMock:
        event = MagicMock()
        event.message_str = kwargs.pop('message_str', "测试消息")
        event.get_group_id.return_value = kwargs.pop('group_id', "123456")
        event.get_sender_id.return_value = kwargs.pop('user_id', "789012")
        event.get_sender_name.return_value = kwargs.pop('user_nickname', "测试用户")
        event.plain_result = MagicMock(return_value=kwargs.pop('plain_result', "plain_result"))
        event.is_at_me = MagicMock(return_value=kwargs.pop('is_at_me', False))
        for k, v in kwargs.items():
            setattr(event, k, v)
        return event

    def _make_req(self, **kwargs) -> MagicMock:
        req = MagicMock()
        req.system_prompt = kwargs.pop('system_prompt', None)
        req.temperature = kwargs.pop('temperature', None)
        req.prompt = kwargs.pop('prompt', "")
        for k, v in kwargs.items():
            setattr(req, k, v)
        return req

    def _make_response(self, **kwargs) -> MagicMock:
        resp = MagicMock()
        resp.completion_text = kwargs.pop('completion_text', "这是一条测试回复。")
        resp.system_prompt = kwargs.pop('system_prompt', None)
        for k, v in kwargs.items():
            setattr(resp, k, v)
        return resp

    def _setup_prompt_injected_event(self, event, req=None):
        """模拟已通过 on_llm_request 处理的事件"""
        if req is None:
            req = self._make_req(system_prompt="你是云璃...")
        scope = f"{event.get_group_id()}:{event.get_sender_id()}"
        ctx = RequestContext(
            req=req,
            group_id=event.get_group_id() or "",
            user_id=event.get_sender_id() or "",
            user_nickname=event.get_sender_name() or "",
            scope=scope,
            is_prompt_injected=True,
        )
        event._yunli_ctx = ctx
        return event

    # ===================================================================
    # a. 初始化 (4 tests)
    # ===================================================================

    def test_constructor_accepts_context(self):
        """构造函数接受 context 参数"""
        self.assertIsNotNone(self.plugin)
        self.assertEqual(self.plugin.context, self.context)

    def test_constructor_creates_internal_components(self):
        """构造函数创建内部组件"""
        self.assertTrue(hasattr(self.plugin, 'persona_engine'))
        self.assertTrue(hasattr(self.plugin, 'message_splitter'))
        self.assertTrue(hasattr(self.plugin, 'db'))
        self.assertTrue(hasattr(self.plugin, '_debouncer'))
        self.assertTrue(hasattr(self.plugin, '_at_detector'))
        self.assertTrue(hasattr(self.plugin, '_context_builder'))
        self.assertTrue(hasattr(self.plugin, '_group_perception'))
        self.assertTrue(hasattr(self.plugin, '_memory_manager'))
        self.assertTrue(hasattr(self.plugin, '_segment_send_sem'))

    def test_constructor_with_custom_config(self):
        """构造函数使用自定义配置"""
        custom_config = default_config({
            "force_segmented_reply": False,
            "enable_group_scene_perception": False,
        })
        plugin = YunliPersonaPlugin(self.context, custom_config)
        self.assertFalse(plugin.config.get("force_segmented_reply"))
        self.assertFalse(plugin._enable_group_scene_perception)
        self.mock_splitter_class.assert_called()

    def test_constructor_data_dir_creation(self):
        """构造函数确保 data_dir 存在"""
        new_temp = tempfile.mkdtemp()
        shutil.rmtree(new_temp)
        self.context.data_dir = new_temp
        plugin = YunliPersonaPlugin(self.context, default_config())
        self.assertTrue(os.path.isdir(new_temp))
        shutil.rmtree(new_temp, ignore_errors=True)

    # ===================================================================
    # b. _is_segmented_reply_enabled (4 tests)
    # ===================================================================

    def test_segmented_reply_enabled_from_astrbot(self):
        """AstrBot config 中 segmented_reply 启用 → True"""
        self.context.config.platform_settings = {
            'segmented_reply': {'enable': True}
        }
        plugin = YunliPersonaPlugin(self.context, default_config())
        self.assertTrue(plugin._is_segmented_reply_enabled())

    def test_segmented_reply_disabled_in_astrbot(self):
        """AstrBot config 中 segmented_reply 禁用 → 走 fallback"""
        self.context.config.platform_settings = {
            'segmented_reply': {'enable': False}
        }
        plugin = YunliPersonaPlugin(
            self.context, default_config({"force_segmented_reply": False})
        )
        self.assertFalse(plugin._is_segmented_reply_enabled())

    def test_segmented_reply_no_astrbot_setting(self):
        """AstrBot config 无 segmented_reply → 走 fallback"""
        plugin = YunliPersonaPlugin(self.context, default_config())
        self.assertTrue(plugin._is_segmented_reply_enabled())

    def test_segmented_reply_force_enabled(self):
        """force_segmented_reply=True 且 AstrBot 无设置 → True"""
        self.context.config.platform_settings = {}
        plugin = YunliPersonaPlugin(
            self.context, default_config({"force_segmented_reply": True})
        )
        self.assertTrue(plugin._is_segmented_reply_enabled())

    # ===================================================================
    # c. _prepare_segments (5 tests)
    # ===================================================================

    def test_prepare_segments_short_text(self):
        """短文本 → 单个 segment"""
        self.mock_splitter.max_segment_length = 180
        plugin = YunliPersonaPlugin(self.context, default_config())
        text = "这是一条短消息。"
        segments = plugin._prepare_segments(text)
        self.assertEqual(len(segments), 1)
        self.assertEqual(segments[0]['text'], text)
        self.assertEqual(segments[0]['delay'], 0)

    def test_prepare_segments_long_text(self):
        """长文本 → 多个 segment

        v2.2.0：第 2 段起 30% 概率添加连接词（"其实", "话说", "不过" 等）模拟真人补充说话。
        文本断言需要兼容这种随机前缀。
        """
        self.mock_splitter.max_segment_length = 10
        self.mock_splitter.split.return_value = [
            {'text': '第一段。', 'delay': 0.5, 'is_last': False},
            {'text': '第二段。', 'delay': 0.8, 'is_last': True},
        ]
        plugin = YunliPersonaPlugin(self.context, default_config())
        text = "第一段。第二段。第三段。"  # len > 10
        segments = plugin._prepare_segments(text)
        self.assertEqual(len(segments), 2)
        # 第 1 段无连接词前缀
        self.assertEqual(segments[0]['text'], '第一段。')
        self.assertAlmostEqual(segments[0]['delay'], 0.5)
        # 第 2 段可能添加连接词（30%概率），断言时允许前缀或精确匹配
        self.assertTrue(
            segments[1]['text'] == '第二段。' or segments[1]['text'].endswith('第二段。'),
            f"Expected '第二段。' or ...'第二段。', got {segments[1]['text']!r}"
        )
        self.assertAlmostEqual(segments[1]['delay'], 0.8)

    def test_prepare_segments_correct_structure(self):
        """segment 包含正确的 text 和 delay 字段"""
        self.mock_splitter.max_segment_length = 5
        self.mock_splitter.split.return_value = [
            {'text': '段1', 'delay': 0.3, 'is_last': False},
            {'text': '段2', 'delay': 0.6, 'is_last': True},
        ]
        plugin = YunliPersonaPlugin(self.context, default_config())
        text = "段1段2" * 30  # len=120 > 5
        segments = plugin._prepare_segments(text)
        for seg in segments:
            self.assertIn('text', seg)
            self.assertIn('delay', seg)
            self.assertIsInstance(seg['text'], str)
            self.assertIsInstance(seg['delay'], (int, float))

    def test_prepare_segments_empty_text(self):
        """空文本 → 单段空段 delay=0"""
        self.mock_splitter.max_segment_length = 180
        plugin = YunliPersonaPlugin(self.context, default_config())
        segments = plugin._prepare_segments("")
        self.assertEqual(len(segments), 1)
        self.assertEqual(segments[0]['text'], "")
        self.assertEqual(segments[0]['delay'], 0)

    def test_prepare_segments_disabled(self):
        """分段回复禁用 → 单段全文本"""
        if hasattr(self.context.config, 'platform_settings'):
            delattr(self.context.config, 'platform_settings')
        plugin = YunliPersonaPlugin(
            self.context, default_config({"force_segmented_reply": False})
        )
        text = "这是一条较长的消息。" * 30
        segments = plugin._prepare_segments(text)
        self.assertEqual(len(segments), 1)
        self.assertEqual(segments[0]['text'], text)
        self.assertEqual(segments[0]['delay'], 0)

    # ===================================================================
    # d. _send_segmented (4 tests)
    # ===================================================================

    def test_send_segmented_single(self):
        """单段 → 发送一次"""
        plugin = YunliPersonaPlugin(self.context, default_config())
        event = self._make_event()
        text = "只有一段。"

        async def run():
            results = []
            async for result in plugin._send_segmented(event, text):
                results.append(result)
            return results

        results = asyncio.run(run())
        self.assertEqual(len(results), 1)

    def test_send_segmented_multiple(self):
        """多段 → 依次发送"""
        self.mock_splitter.max_segment_length = 5
        self.mock_splitter.split.return_value = [
            {'text': '第一段。', 'delay': 0.0, 'is_last': False},
            {'text': '第二段。', 'delay': 0.0, 'is_last': True},
        ]
        plugin = YunliPersonaPlugin(self.context, default_config())
        event = self._make_event()
        text = "第一段。第二段。"  # len=8 > 5

        async def run():
            results = []
            async for result in plugin._send_segmented(event, text):
                results.append(result)
            return results

        results = asyncio.run(run())
        self.assertEqual(len(results), 2)

    def test_send_segmented_empty(self):
        """空文本 → 不发送"""
        plugin = YunliPersonaPlugin(self.context, default_config())
        event = self._make_event()

        async def run():
            results = []
            async for result in plugin._send_segmented(event, ""):
                results.append(result)
            return results

        results = asyncio.run(run())
        self.assertEqual(len(results), 0)

    def test_send_segmented_timeout_skip(self):
        """超时（但不 sleep）→ 不会阻塞"""
        self.mock_splitter.max_segment_length = 5
        self.mock_splitter.split.return_value = [
            {'text': '段1', 'delay': 100.0, 'is_last': False},
            {'text': '段2', 'delay': 0.0, 'is_last': True},
        ]
        plugin = YunliPersonaPlugin(self.context, default_config())
        plugin._log_interaction = AsyncMock()
        event = self._make_event()
        text = "段1段2段3段4段5"  # len=10 > max_segment_length=5，触发切分

        async def run():
            results = []
            async for result in plugin._send_segmented(event, text):
                results.append(result)
            return results

        results = asyncio.run(run())
        # 首段 delay 100 但 i==0 时不 sleep，直接 yield
        # 第二段 delay 0 也不 sleep
        # 所以两段都发出
        self.assertEqual(len(results), 2)

    # ===================================================================
    # e. _get_group_id / _get_user_id / _get_user_nickname (4 tests)
    # ===================================================================

    def test_get_group_id(self):
        """从事件中提取群号"""
        event = self._make_event(group_id="group999")
        self.assertEqual(self.plugin._get_group_id(event), "group999")

    def test_get_user_id(self):
        """从事件中提取用户 ID"""
        event = self._make_event(user_id="user888")
        self.assertEqual(self.plugin._get_user_id(event), "user888")

    def test_get_user_nickname(self):
        """从事件中提取昵称"""
        event = self._make_event(user_nickname="小明")
        self.assertEqual(self.plugin._get_user_nickname(event), "小明")

    def test_get_ids_fallback(self):
        """事件缺少对应属性 → 返回空字符串"""
        empty_event = self._make_event()
        empty_event.get_group_id.side_effect = AttributeError("no group")
        empty_event.get_sender_id.side_effect = AttributeError("no user")
        empty_event.get_sender_name.side_effect = AttributeError("no name")

        self.assertEqual(self.plugin._get_group_id(empty_event), "")
        self.assertEqual(self.plugin._get_user_id(empty_event), "")
        self.assertEqual(self.plugin._get_user_nickname(empty_event), "")

    # ===================================================================
    # f. _should_activate (4 tests)
    # ===================================================================

    def test_should_activate_true(self):
        """@ 提及 → True"""
        self.mock_at_detector.is_at_me.return_value = True
        event = self._make_event()
        self.assertTrue(self.plugin._should_activate(event))
        self.mock_at_detector.is_at_me.assert_called()

    def test_should_activate_false(self):
        """群聊中无 @ → False"""
        self.mock_at_detector.is_at_me.return_value = False
        event = self._make_event()
        self.assertFalse(self.plugin._should_activate(event))

    def test_should_activate_private_chat(self):
        """私聊 → True（AtDetector 处理）"""
        self.mock_at_detector.is_at_me.return_value = True
        event = self._make_event()
        self.assertTrue(self.plugin._should_activate(event))

    def test_should_activate_delegates_to_at_detector(self):
        """验证委托给 AtDetector.is_at_me"""
        self.mock_at_detector.is_at_me = MagicMock(return_value=True)
        event = self._make_event()
        self.plugin._should_activate(event)
        self.mock_at_detector.is_at_me.assert_called_once()

    # ===================================================================
    # g. on_llm_request (4 tests)
    # ===================================================================

    def test_on_llm_request_sets_system_prompt(self):
        """注入 system_prompt"""
        self.mock_at_detector.is_at_me.return_value = True
        self.mock_debouncer.handle_message = AsyncMock(return_value=False)
        event = self._make_event()
        req = self._make_req(system_prompt=None)

        async def run():
            await self.plugin.on_llm_request(event, req)
            return req

        result = asyncio.run(run())
        self.assertIsNotNone(result.system_prompt)
        self.assertIn("你是云璃", result.system_prompt)

    def test_on_llm_request_preserves_existing_prompt(self):
        """保留已有 system_prompt"""
        self.mock_at_detector.is_at_me.return_value = True
        self.mock_debouncer.handle_message = AsyncMock(return_value=False)
        event = self._make_event()
        req = self._make_req(system_prompt="其他插件的提示词")

        async def run():
            await self.plugin.on_llm_request(event, req)
            return req

        result = asyncio.run(run())
        self.assertIn("其他插件的提示词", result.system_prompt)
        self.assertIn("[[YUNLI_BOUNDARY]]", result.system_prompt)
        self.assertIn("你是云璃", result.system_prompt)

    def test_on_llm_request_skipped_when_not_activated(self):
        """_should_activate 返回 False → 跳过注入"""
        self.mock_at_detector.is_at_me.return_value = False
        event = self._make_event()
        req = self._make_req(system_prompt=None)

        async def run():
            await self.plugin.on_llm_request(event, req)
            return req

        result = asyncio.run(run())
        self.assertIsNone(result.system_prompt)

    def test_on_llm_request_debounced(self):
        """防抖缓冲 → 设置 is_debounce_buffered"""
        self.mock_at_detector.is_at_me.return_value = True
        self.mock_debouncer.handle_message = AsyncMock(return_value=True)
        event = self._make_event()
        req = self._make_req(system_prompt=None)

        async def run():
            await self.plugin.on_llm_request(event, req)
            return event, req

        evt, r = asyncio.run(run())
        ctx = getattr(evt, '_yunli_ctx', None)
        self.assertIsNotNone(ctx)
        self.assertTrue(ctx.is_debounce_buffered)
        self.assertIsNone(r.system_prompt)

    # ===================================================================
    # h. on_llm_response (6 tests)
    # ===================================================================

    def test_on_llm_response_text_polished(self):
        """响应文本被润色"""
        event = self._setup_prompt_injected_event(self._make_event())
        resp = self._make_response(completion_text="原始回复文本")

        async def run():
            await self.plugin.on_llm_response(event, resp)
            return resp

        result = asyncio.run(run())
        self.assertIsNotNone(result.completion_text)

    def test_on_llm_response_no_injection_skip(self):
        """未注入云璃提示词 → 跳过"""
        event = self._make_event()
        # 阻止 MagicMock 自动创建 _yunli_ctx（避免骗取注入判断）
        event._yunli_ctx = None
        resp = self._make_response(completion_text="不应被修改")

        async def run():
            await self.plugin.on_llm_response(event, resp)
            return resp

        result = asyncio.run(run())
        self.assertEqual(result.completion_text, "不应被修改")

    def test_on_llm_response_empty_text(self):
        """空响应 → 直接返回"""
        event = self._setup_prompt_injected_event(self._make_event())
        resp = self._make_response(completion_text="")

        async def run():
            await self.plugin.on_llm_response(event, resp)
            return resp

        result = asyncio.run(run())
        self.assertEqual(result.completion_text, "")

    def test_on_llm_response_none_response(self):
        """None 响应 → 不崩溃"""
        event = self._setup_prompt_injected_event(self._make_event())

        async def run():
            await self.plugin.on_llm_response(event, None)
            return True

        result = asyncio.run(run())
        self.assertTrue(result)

    def test_on_llm_response_knowledge_mode(self):
        """知识查询模式 → 完整输出"""
        event = self._setup_prompt_injected_event(self._make_event())
        event._yunli_ctx.is_knowledge_query = True
        resp = self._make_response(completion_text="关于云璃的详细知识。" * 30)

        async def run():
            await self.plugin.on_llm_response(event, resp)
            return resp

        result = asyncio.run(run())
        self.assertTrue(len(result.completion_text) > 0)

    def test_on_llm_response_debounce_merged(self):
        """防抖合并 → 清空 completion_text"""
        event = self._setup_prompt_injected_event(self._make_event())
        event._yunli_ctx.is_debounce_merged = True
        resp = self._make_response(completion_text="应被清空")

        async def run():
            await self.plugin.on_llm_response(event, resp)
            return resp

        result = asyncio.run(run())
        self.assertEqual(result.completion_text, "")

    # ===================================================================
    # i. 边缘情况 (3 tests)
    # ===================================================================

    def test_edge_case_disabled_features(self):
        """所有功能禁用 → 初始化正常"""
        minimal_config = default_config({
            "force_segmented_reply": False,
            "enable_group_scene_perception": False,
            "enable_high_intensity": False,
            "enable_topic_threads": False,
            "enable_typing_delay": False,
            "enable_thinking_pause": False,
            "use_qq_emoji": False,
        })
        plugin = YunliPersonaPlugin(self.context, minimal_config)
        self.assertIsNotNone(plugin)
        self.assertFalse(plugin.config.get("enable_group_scene_perception"))
        self.assertFalse(plugin.config.get("enable_topic_threads"))

    def test_edge_case_very_long_text(self):
        """极长文本 → prepare_segments 不崩溃"""
        self.mock_splitter.max_segment_length = 180
        self.mock_splitter.split.return_value = [
            {'text': '段A', 'delay': 0.5, 'is_last': False},
            {'text': '段B', 'delay': 0.5, 'is_last': True},
        ]
        plugin = YunliPersonaPlugin(self.context, default_config())
        segments = plugin._prepare_segments("A" * 5000)
        self.assertGreater(len(segments), 0)

    def test_edge_case_multiple_on_llm_response_calls(self):
        """多次连续 on_llm_response 正常"""
        event1 = self._setup_prompt_injected_event(
            self._make_event(message_str="你好")
        )
        event2 = self._setup_prompt_injected_event(
            self._make_event(message_str="再见")
        )
        resp1 = self._make_response(completion_text="你好呀！")
        resp2 = self._make_response(completion_text="再见啦！")

        async def run():
            await self.plugin.on_llm_response(event1, resp1)
            await self.plugin.on_llm_response(event2, resp2)
            return resp1, resp2

        r1, r2 = asyncio.run(run())
        self.assertIsNotNone(r1.completion_text)
        self.assertIsNotNone(r2.completion_text)

    # ===================================================================
    # j. _safe_create_task (1 test)
    # ===================================================================

    def test_safe_create_task_tracks_background_tasks(self):
        """_safe_create_task 记录任务并在完成时移除"""
        async def dummy():
            await asyncio.sleep(0.001)

        async def run():
            task = self.plugin._safe_create_task(dummy())
            self.assertIn(task, self.plugin._background_tasks)
            await asyncio.sleep(0.05)
            self.assertNotIn(task, self.plugin._background_tasks)

        asyncio.run(run())

    # ===================================================================
    # k. _get_cached_self_id (3 tests)
    # ===================================================================

    def test_get_cached_self_id_from_context(self):
        """通过 context.get_self_id() 获取"""
        context = MagicMock(spec=['data_dir', 'config', 'get_self_id'])
        context.data_dir = self.temp_dir
        context.config = MagicMock(spec=[])
        context.get_self_id = MagicMock(return_value="robot123")
        plugin = YunliPersonaPlugin(context, default_config())
        self_id = plugin._get_cached_self_id()
        self.assertEqual(self_id, "robot123")

    def test_get_cached_self_id_from_platform(self):
        """通过 context.platform.account 获取"""
        context = MagicMock(spec=['data_dir', 'config', 'platform'])
        context.data_dir = self.temp_dir
        context.config = MagicMock(spec=[])
        context.platform = MagicMock()
        context.platform.account = MagicMock()
        context.platform.account.u = "robot456"
        plugin = YunliPersonaPlugin(context, default_config())
        self_id = plugin._get_cached_self_id()
        self.assertEqual(self_id, "robot456")

    def test_get_cached_self_id_fallback(self):
        """无法获取 → 空字符串"""
        context = MagicMock(spec=['data_dir', 'config', 'platform'])
        context.data_dir = self.temp_dir
        context.config = MagicMock(spec=[])
        context.platform = MagicMock(spec=[])
        plugin = YunliPersonaPlugin(context, default_config())
        self_id = plugin._get_cached_self_id()
        self.assertEqual(self_id, "")

    # ===================================================================
    # l. __del__ (1 test)
    # ===================================================================

    def test_del_cancels_background_tasks(self):
        """__del__ 取消后台任务"""
        async def slow_task():
            await asyncio.sleep(100)

        task = NotImplemented  # placeholder

        async def run():
            nonlocal task
            task = self.plugin._safe_create_task(slow_task())
            return task

        task = asyncio.run(run())
        self.plugin.__del__()
        self.assertTrue(task.cancelled() or task.done())
        self.assertEqual(len(self.plugin._background_tasks), 0)

    # ===================================================================
    # m. __del__ flush logs (1 test)
    # ===================================================================

    def test_del_flush_logs_and_close_db(self):
        """__del__ 刷新日志并关闭数据库"""
        self.plugin.__del__()
        self.mock_db.flush_logs.assert_called_once()
        self.mock_db.close.assert_called_once()


# ===================================================================
# 额外边缘用例（独立类，可独立配置 mock）
# ===================================================================
class TestYunliPersonaPluginEdge(YunliTestCase):
    """额外的极端情况测试"""

    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.context = MagicMock(spec=[])
        self.context.data_dir = self.temp_dir
        self.context.config = MagicMock(spec=[])

        self._patchers = []
        self._start_patcher('yunli.main.YunliDatabase', 'mock_db')
        self._start_patcher('yunli.main.YunliPersonaEngine', 'mock_engine')
        self._start_patcher('yunli.main.QQBehaviorManager', 'mock_qq_behavior')
        self._start_patcher('yunli.main.RelationshipManager', 'mock_relationship')
        self._start_patcher('yunli.main.MessageSplitter', 'mock_splitter')
        self._start_patcher('yunli.main.MessageDebouncer', 'mock_debouncer')
        self._start_patcher('yunli.main.AtDetector', 'mock_at_detector')
        self._start_patcher('yunli.main.ContextBuilder', 'mock_ctx_builder')
        self._start_patcher('yunli.main.GroupPerception', 'mock_perception')
        self._start_patcher('yunli.main.MemoryManager', 'mock_memory')

        self.mock_db.query_dialogues.return_value = []
        self.mock_engine.build_system_prompt.return_value = "你是云璃..."
        self.mock_engine.build_dynamic_prompt.return_value = ""
        self.mock_engine.polish_response.side_effect = lambda text, *a, **kw: text
        self.mock_engine.review_response.side_effect = lambda text, *a, **kw: text
        self.mock_engine.get_context_data.return_value = {
            "relevant_knowledge": [], "analogies": [], "user_history": None,
        }
        self.mock_engine.emotion = MagicMock()
        self.mock_engine.emotion.current_state = "neutral"
        self.mock_engine.language = MagicMock()
        self.mock_engine.language.detect_query_mode.return_value = "chat"
        self.mock_qq_behavior.format_for_qq.side_effect = lambda text, *a, **kw: text
        self.mock_qq_behavior.should_skip_punctuation.return_value = False
        self.mock_qq_behavior.add_typing_pause.side_effect = lambda text, *a, **kw: text
        self.mock_qq_behavior.add_human_touches.side_effect = lambda text, *a, **kw: text
        self.mock_relationship.get_reply_length_limit.return_value = None
        self.mock_relationship.get_hint.return_value = None
        self.mock_splitter.max_segment_length = 180
        self.mock_splitter.enable_thinking_pause = True
        self.mock_splitter.split.return_value = []
        self.mock_splitter.get_thinking_pause.return_value = ""
        self.mock_debouncer.handle_message = AsyncMock(return_value=False)
        self.mock_debouncer.mark_processed = MagicMock()
        self.mock_at_detector.is_at_me.return_value = True
        self.mock_ctx_builder.format_environment_perception.return_value = ""
        self.mock_ctx_builder.add_relationship_context.return_value = ""
        self.mock_ctx_builder.build_chat_context.return_value = ""
        self.mock_ctx_builder.build_recent_chat_history.return_value = ""
        self.mock_perception_class.extract_scene_signals = MagicMock(return_value={})
        self.mock_perception_class.format_scene_description = MagicMock(return_value="")
        self.mock_perception.get_atmosphere_text = MagicMock(return_value="")
        self.mock_memory.log_interaction = MagicMock()

    def _start_patcher(self, target, attr_name):
        patcher = patch(target)
        mock_class = patcher.start()
        mock_instance = MagicMock()
        mock_class.return_value = mock_instance
        self._patchers.append(patcher)
        setattr(self, attr_name, mock_instance)
        setattr(self, attr_name + '_class', mock_class)

    def tearDown(self):
        for patcher in self._patchers:
            patcher.stop()
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_on_llm_request_inject_exception_handled(self):
        """_inject_persona_prompt 异常 → 不崩溃"""
        plugin = YunliPersonaPlugin(self.context, default_config())
        event = MagicMock()
        event.message_str = "测试"
        event.get_group_id.return_value = "g1"
        event.get_sender_id.return_value = "u1"
        event.get_sender_name.return_value = "用户"
        req = MagicMock()
        req.system_prompt = None
        req.prompt = ""

        self.mock_debouncer.handle_message = AsyncMock(return_value=False)
        self.mock_engine.build_system_prompt.side_effect = RuntimeError("模拟异常")

        async def run():
            await plugin.on_llm_request(event, req)
            return True

        result = asyncio.run(run())
        self.assertTrue(result)

    def test_persona_engine_review_called(self):
        """on_llm_response 调用 review_response"""
        plugin = YunliPersonaPlugin(self.context, default_config())
        plugin._log_interaction = AsyncMock()
        event = MagicMock()
        event.message_str = "你好"
        event.get_group_id.return_value = "g1"
        event.get_sender_id.return_value = "u1"
        event.get_sender_name.return_value = "用户"
        event.is_at_me = MagicMock(return_value=True)
        ctx = RequestContext(
            req=MagicMock(), group_id="g1", user_id="u1",
            user_nickname="用户", scope="g1:u1", is_prompt_injected=True,
        )
        event._yunli_ctx = ctx
        resp = MagicMock()
        resp.completion_text = "测试回复"

        # 本地创建的插件需要设置 AsyncMock
        plugin._log_interaction = AsyncMock()

        async def run():
            await plugin.on_llm_response(event, resp)

        asyncio.run(run())
        self.mock_engine.review_response.assert_called()

    def test_on_llm_request_twice_no_duplicate(self):
        """重复调用 → 第二次通过 [[YUNLI_BOUNDARY]] 追加而非覆盖"""
        plugin = YunliPersonaPlugin(self.context, default_config())
        event = MagicMock()
        event.message_str = "测试"
        event.get_group_id.return_value = "g1"
        event.get_sender_id.return_value = "u1"
        event.get_sender_name.return_value = "用户"
        req = MagicMock()
        req.system_prompt = None
        req.prompt = ""

        self.mock_debouncer.handle_message = AsyncMock(return_value=False)

        async def run():
            await plugin.on_llm_request(event, req)
            first = req.system_prompt
            await plugin.on_llm_request(event, req)
            second = req.system_prompt
            return first, second

        first, second = asyncio.run(run())
        # 第一次注入：设置 system_prompt
        self.assertIn("你是云璃", first)
        # 第二次注入：通过 [[YUNLI_BOUNDARY]] 衔接（而非覆盖）
        self.assertIn("[[YUNLI_BOUNDARY]]", second)
        # 第一次的内容保留在第二次中
        self.assertIn(first.strip(), second)

    def test_prepare_segments_thinking_pause_applied(self):
        """非首段应用思考停顿"""
        self.mock_splitter.max_segment_length = 10
        self.mock_splitter.split.return_value = [
            {'text': '段一。', 'delay': 0.5, 'is_last': False},
            {'text': '段二。', 'delay': 0.5, 'is_last': True},
        ]
        self.mock_splitter.enable_thinking_pause = True
        self.mock_splitter.get_thinking_pause.return_value = "…嗯…"
        plugin = YunliPersonaPlugin(self.context, default_config())
        text = "段一。段二。" * 20
        segments = plugin._prepare_segments(text)
        self.assertEqual(len(segments), 2)
        self.assertIn("…嗯…", segments[1]['text'])