"""@云璃激活后完整响应链路模拟测试

验证从用户 @云璃 → on_request → 提示词注入 → LLM 响应 → on_response →
人格润色 → QQ 格式化 → 分段切分 → 后续片段发送 的完整链路。

测试目标：
1. 验证 @云璃激活后 on_request 正确注入提示词（system_prompt 非空）
2. 验证 RequestContext 被正确附着到 event._yunli_ctx
3. 验证 rel_mode 从 ctx 读取（P0-1 修复点）
4. 验证 on_response 调用 persona_engine.polish_response（含 is_first_segment=True）
5. 验证 on_response 调用 qq_behavior.format_for_qq
6. 验证分段切分后首段写入 response.completion_text
7. 验证后续片段通过 _send_remaining_segments 发送（含 is_first_segment=False）
8. 验证 thread_tracker 不再被同步到 RequestContext（S4 修复点）

架构简化覆盖：
- S1: RelationshipManager 从 relationship.py 导入
- S2: persona 内部无运行时延迟导入
- S3: AtDetector 从 utils.py 导入
- S4: RequestContext 无 last_user_message 等字段
- P0-1: rel_mode 从 event._yunli_ctx 读取
"""

import os
import sys
import asyncio
import tempfile
import shutil
from unittest.mock import MagicMock, patch, AsyncMock, call

# ─── 路径设置 ───────────────────────────────────────────────
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

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
from yunli.core.utils import AtDetector  # S3: AtDetector 从 utils 导入
from yunli.persona.relationship import RelationshipManager  # S1: 从 relationship 导入
from yunli.persona.config import RELATIONSHIP_MODES  # S2: 从 config 导入


# ===================================================================
# 测试主类
# ===================================================================
class TestFullResponseChain(YunliTestCase):
    """@云璃激活后完整响应链路模拟测试"""

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

        self.mock_engine.build_system_prompt.return_value = "你是云璃，猎剑士。"
        self.mock_engine.build_dynamic_prompt.return_value = "[动态人格上下文]"
        # polish_response 默认透传，便于断言原始文本流向
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

        # 关系模式：默认 normal
        self.mock_relationship.get_reply_length_limit.return_value = None
        self.mock_relationship.get_hint.return_value = None
        self.mock_relationship.get_mode.return_value = "normal"
        self.mock_relationship.update.return_value = "normal"
        self.mock_relationship.get_particle_multiplier.return_value = 1.0
        self.mock_relationship.get_emoji_multiplier.return_value = 1.0

        # 分段器：默认不分段（单段直接返回）
        self.mock_splitter.max_segment_length = 180
        self.mock_splitter.min_segment_length = 10
        self.mock_splitter.enable_thinking_pause = True
        self.mock_splitter.enable_typing_delay = True
        self.mock_splitter.base_delay = 0.5
        self.mock_splitter.delay_per_char = 0.03
        self.mock_splitter.max_delay = 3.0
        self.mock_splitter.thinking_pause_prob = 0.3
        self.mock_splitter.split.return_value = []  # 默认不分段
        self.mock_splitter.get_thinking_pause.return_value = ""

        self.mock_debouncer.handle_message = AsyncMock(return_value=False)
        self.mock_debouncer.mark_processed = MagicMock()

        # AtDetector：默认判定为 @云璃
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
        event.message_str = kwargs.pop('message_str', "@云璃 你好")
        event.get_group_id.return_value = kwargs.pop('group_id', "123456")
        event.get_sender_id.return_value = kwargs.pop('user_id', "789012")
        event.get_sender_name.return_value = kwargs.pop('user_nickname', "测试用户")
        event.plain_result = MagicMock(return_value=kwargs.pop('plain_result', "plain_result"))
        event.is_at_me = MagicMock(return_value=kwargs.pop('is_at_me', True))
        event.send = AsyncMock()
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
        resp.completion_text = kwargs.pop('completion_text', "你好啊，有什么事吗？")
        resp.system_prompt = kwargs.pop('system_prompt', None)
        for k, v in kwargs.items():
            setattr(resp, k, v)
        return resp

    # ===================================================================
    # 测试用例
    # ===================================================================

    # ── 测试 1：@云璃激活 → on_request 注入提示词 ────────────────

    def test_at_activate_triggers_prompt_injection(self):
        """验证 @云璃激活后 on_request 正确注入提示词

        链路：on_request → _should_activate(=True) → _on_request_impl →
              _inject_persona_prompt → _do_inject_persona_prompt → req.system_prompt 被设置
        """
        event = self._make_event(message_str="@云璃 你好")
        req = self._make_req()

        asyncio.run(self.plugin._event_pipeline.on_request(event, req))

        # 断言1：system_prompt 被注入（非空）
        self.assertIsNotNone(req.system_prompt)
        self.assertIn("你是云璃", req.system_prompt)

        # 断言2：RequestContext 被附着到 event._yunli_ctx
        ctx = getattr(event, "_yunli_ctx", None)
        self.assertIsInstance(ctx, RequestContext)
        self.assertTrue(ctx.is_prompt_injected)
        self.assertEqual(ctx.group_id, "123456")
        self.assertEqual(ctx.user_id, "789012")

        # 断言3：rel_mode 被设置（P0-1 修复点验证）
        self.assertIn(ctx.rel_mode, RELATIONSHIP_MODES)

        # 断言4：S4 修复点 - RequestContext 不再含线程字段
        self.assertFalse(hasattr(ctx, "last_user_message"))
        self.assertFalse(hasattr(ctx, "last_yunli_response"))
        self.assertFalse(hasattr(ctx, "thread_turn_count"))

    # ── 测试 2：未@云璃 → 旁听模式，不注入提示词 ────────────────

    def test_no_at_triggers_passive_mode(self):
        """验证未@云璃时进入旁听模式，不注入提示词"""
        self.mock_at_detector.is_at_me.return_value = False
        event = self._make_event(message_str="今天天气真好")
        req = self._make_req()

        asyncio.run(self.plugin._event_pipeline.on_request(event, req))

        # 断言：system_prompt 未被注入
        self.assertIsNone(req.system_prompt)
        # 断言：未附着 RequestContext（getattr 返回 None 而非 MagicMock 自动属性）
        ctx = getattr(event, "_yunli_ctx", None)
        # MagicMock 会自动生成属性，需检查是否为 RequestContext 实例
        self.assertFalse(isinstance(ctx, RequestContext),
                         "旁听模式不应附着 RequestContext")

    # ── 测试 3：on_response 调用人格润色（首段） ────────────────

    def test_on_response_calls_polish_response_first_segment(self):
        """验证 on_response 调用 polish_response 且 is_first_segment=True"""
        event = self._make_event()
        req = self._make_req(system_prompt="你是云璃...")
        self._setup_prompt_injected_event(event, req)
        response = self._make_response(completion_text="你好啊")

        asyncio.run(self.plugin._event_pipeline.on_response(event, response))

        # 断言：polish_response 被调用，且 is_first_segment=True
        self.mock_engine.polish_response.assert_called()
        first_call = self.mock_engine.polish_response.call_args_list[0]
        self.assertEqual(first_call.kwargs.get("is_first_segment"), True)

    # ── 测试 4：on_response 调用 QQ 格式化 ────────────────────

    def test_on_response_calls_format_for_qq(self):
        """验证 on_response 调用 qq_behavior.format_for_qq"""
        event = self._make_event()
        req = self._make_req(system_prompt="你是云璃...")
        self._setup_prompt_injected_event(event, req)
        response = self._make_response(completion_text="你好啊")

        asyncio.run(self.plugin._event_pipeline.on_response(event, response))

        # 断言：format_for_qq 被调用
        self.mock_qq_behavior.format_for_qq.assert_called_once()
        call_args = self.mock_qq_behavior.format_for_qq.call_args
        # 验证传入的 relationship_mode 来自 ctx.rel_mode（P0-1 修复点）
        self.assertIn(call_args.kwargs.get("relationship_mode"), RELATIONSHIP_MODES)

    # ── 测试 5：分段切分 → 首段写入 completion_text ────────────

    def test_segmented_first_segment_written_to_completion_text(self):
        """验证分段切分后首段写入 response.completion_text"""
        event = self._make_event()
        req = self._make_req(system_prompt="你是云璃...")
        self._setup_prompt_injected_event(event, req)

        # 启用分段回复（_is_segmented_reply_enabled 需返回 True）
        self.plugin._is_segmented_reply_enabled = MagicMock(return_value=True)
        # 文本长度需超过 max_segment_length（180）
        long_text = "啊" * 200

        # 模拟 splitter 切出 2 段
        self.mock_splitter.split.return_value = [
            {"text": "第一段内容", "delay": 0.5},
            {"text": "第二段内容", "delay": 0.8},
        ]

        response = self._make_response(completion_text=long_text)
        asyncio.run(self.plugin._event_pipeline.on_response(event, response))

        # 断言：首段写入 completion_text
        self.assertEqual(response.completion_text, "第一段内容")

    # ── 测试 6：后续片段通过 _send_remaining_segments 发送 ─────

    def test_remaining_segments_sent_via_send_remaining(self):
        """验证后续片段通过 _send_remaining_segments 发送，且 is_first_segment=False

        覆盖 P0-1 修复点：rel_mode 从 event._yunli_ctx 读取
        """
        event = self._make_event()
        req = self._make_req(system_prompt="你是云璃...")
        # 设置 rel_mode 为 warming，验证后续段发送时能正确读取
        self._setup_prompt_injected_event(event, req, rel_mode="warming")

        # 启用分段回复
        self.plugin._is_segmented_reply_enabled = MagicMock(return_value=True)
        # 文本长度需超过 max_segment_length（180）
        long_text = "啊" * 200

        # 模拟 splitter 切出 3 段
        self.mock_splitter.split.return_value = [
            {"text": "第一段", "delay": 0.5},
            {"text": "第二段", "delay": 0.8},
            {"text": "第三段", "delay": 0.6},
        ]

        # patch _safe_create_task 以捕获后续片段发送任务
        with patch.object(self.plugin, '_safe_create_task') as mock_safe_create:
            # 让 _safe_create_task 实际执行协程，以便后续段发送逻辑运行
            mock_safe_create.side_effect = lambda coro: asyncio.ensure_future(coro)

            response = self._make_response(completion_text=long_text)
            asyncio.run(self.plugin._event_pipeline.on_response(event, response))

            # 断言：_safe_create_task 被调用（创建后续片段发送任务 + _log_interaction）
            # 至少 1 次调用即可证明后续片段发送任务被创建
            self.assertGreaterEqual(mock_safe_create.call_count, 1,
                                    "应创建后续片段发送任务")

        # 断言：首段写入 completion_text
        self.assertEqual(response.completion_text, "第一段")

        # 断言：polish_response 被调用时 is_first_segment=False（后续段）
        polish_calls = self.mock_engine.polish_response.call_args_list
        # 第 1 次是首段（is_first_segment=True），后续应为 False
        subsequent_calls = [c for c in polish_calls[1:] if c.kwargs.get("is_first_segment") is False]
        self.assertGreaterEqual(len(subsequent_calls), 1,
                                "后续片段应调用 polish_response 且 is_first_segment=False")

        # 断言：后续段发送时 rel_mode=warming（P0-1 修复点验证）
        for call_obj in subsequent_calls:
            self.assertEqual(call_obj.kwargs.get("relationship_mode"), "warming",
                             "后续段应从 ctx 读取 rel_mode=warming")

    # ── 测试 7：未分段时完整文本写入 completion_text ───────────

    def test_no_segmentation_full_text_to_completion(self):
        """验证未启用分段时完整文本写入 completion_text"""
        event = self._make_event()
        req = self._make_req(system_prompt="你是云璃...")
        self._setup_prompt_injected_event(event, req)
        # splitter.split 返回空列表 → 不分段
        self.mock_splitter.split.return_value = []

        full_text = "这是一条完整的回复，不需要分段。"
        response = self._make_response(completion_text=full_text)
        asyncio.run(self.plugin._event_pipeline.on_response(event, response))

        # 断言：完整文本写入 completion_text
        self.assertEqual(response.completion_text, full_text)
        # 断言：event.send 未被调用（无后续片段）
        event.send.assert_not_called()

    # ── 测试 8：关系模式影响回复长度限制 ───────────────────────

    def test_backoff_mode_applies_length_limit(self):
        """验证 backoff 关系模式触发回复长度限制

        注意：review_response 的 max_len 来自 chat_max_text_length 配置（默认 50），
        与关系模式长度限制（get_reply_length_limit）是两个独立步骤。
        本测试验证关系模式限制步骤被触发。
        """
        event = self._make_event()
        req = self._make_req(system_prompt="你是云璃...")
        self._setup_prompt_injected_event(event, req, rel_mode="backoff")

        # backoff 模式限制 40 字符
        self.mock_relationship.get_reply_length_limit.return_value = 40

        # 构造超过 40 字符的文本，触发关系模式长度限制
        long_text = "啊" * 100  # 100 字符
        response = self._make_response(completion_text=long_text)
        asyncio.run(self.plugin._event_pipeline.on_response(event, response))

        # 断言：get_reply_length_limit 被调用（关系模式限制步骤触发）
        self.mock_relationship.get_reply_length_limit.assert_called_once()

        # 断言：最终文本被截断（不超过 40 + 15 的回溯范围）
        final_text = response.completion_text
        self.assertLessEqual(len(final_text), 55,
                             "backoff 模式应将回复截断至约 40 字符")

    # ── 测试 9：知识查询模式跳过分段 ───────────────────────────

    def test_knowledge_query_mode_skips_segmentation(self):
        """验证知识查询模式跳过分段切分"""
        event = self._make_event()
        req = self._make_req(system_prompt="你是云璃...")
        # 标记为知识查询
        self._setup_prompt_injected_event(event, req, is_knowledge_query=True)

        response = self._make_response(completion_text="## 知识条目\n这是详细说明...")
        asyncio.run(self.plugin._event_pipeline.on_response(event, response))

        # 断言：splitter.split 未被调用
        self.mock_splitter.split.assert_not_called()
        # 断言：event.send 未被调用
        event.send.assert_not_called()

    # ── 测试 10：架构简化验证 - 无运行时延迟导入 ────────────────

    def test_no_runtime_delayed_imports_in_persona(self):
        """验证 persona 包内部无运行时延迟导入（S2 修复点）

        扫描 qq_behavior.py 和 language.py 源码，确认无
        `from .emotion import RelationshipManager` 延迟导入。
        """
        import inspect
        from yunli.persona import language, qq_behavior

        lang_src = inspect.getsource(language)
        qq_src = inspect.getsource(qq_behavior)

        self.assertNotIn("from .emotion import RelationshipManager", lang_src,
                         "language.py 不应包含运行时延迟导入 RelationshipManager")
        self.assertNotIn("from .emotion import RelationshipManager", qq_src,
                         "qq_behavior.py 不应包含运行时延迟导入 RelationshipManager")
        self.assertNotIn("from .language import LanguageStyleProcessor", qq_src,
                         "qq_behavior.py 不应包含运行时延迟导入 LanguageStyleProcessor")

    # ── 测试 11：架构简化验证 - AtDetector 从 utils 导入 ────────

    def test_at_detector_imported_from_utils(self):
        """验证 AtDetector 从 utils.py 导入（S3 修复点）"""
        from yunli.core.utils import AtDetector as UtilsAtDetector
        from yunli.core import AtDetector as PackageAtDetector

        # 断言：两者是同一个类
        self.assertIs(UtilsAtDetector, PackageAtDetector)

        # 断言：AtDetector 实例化正常
        detector = UtilsAtDetector()
        detector.set_self_id("99999")
        self.assertEqual(detector.get_self_id(), "99999")

    # ── 测试 12：架构简化验证 - RelationshipManager 从 relationship 导入 ─

    def test_relationship_manager_imported_from_relationship_module(self):
        """验证 RelationshipManager 从 relationship.py 导入（S1 修复点）"""
        from yunli.persona.relationship import RelationshipManager as RelModuleRM
        from yunli.persona import RelationshipManager as PackageRM

        # 断言：两者是同一个类
        self.assertIs(RelModuleRM, PackageRM)

        # 断言：向后兼容类属性仍存在
        self.assertTrue(hasattr(RelModuleRM, "RELATIONSHIP_MODES"))
        self.assertTrue(hasattr(RelModuleRM, "BOUNDARY_KEYWORDS"))
        self.assertTrue(hasattr(RelModuleRM, "detect_intent"))
        self.assertTrue(hasattr(RelModuleRM, "detect_user_intent"))

        # 断言：emotion 模块不再包含 RelationshipManager
        from yunli.persona import emotion
        self.assertFalse(hasattr(emotion, "RelationshipManager"),
                         "emotion.py 不应再包含 RelationshipManager")

    # ── 辅助方法 ──────────────────────────────────────────────

    def _setup_prompt_injected_event(self, event, req=None, rel_mode="normal",
                                     is_knowledge_query=False):
        """模拟已通过 on_llm_request 处理的事件（RequestContext 已附着）"""
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
            rel_mode=rel_mode,
            is_knowledge_query=is_knowledge_query,
        )
        event._yunli_ctx = ctx


if __name__ == "__main__":
    unittest.main(verbosity=2)
