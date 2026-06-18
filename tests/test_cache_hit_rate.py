"""
云璃插件 - LLM 上下文缓存命中率测试

验证 v1.7.0 缓存友好重构的正确性：
1. 静态 system_prompt → DeepSeek Context Caching 可命中
2. 动态内容注入 req.prompt → 不影响 system_prompt 缓存
3. 确定性方法（替代 random）→ 相同输入产生相同输出
4. 兼容旧版框架 → req 无 prompt 属性时回退
"""

import os
import sys
import asyncio
import hashlib
import tempfile
import shutil
from unittest.mock import MagicMock, patch, AsyncMock

# ─── 路径设置 ───────────────────────────────────────────────
from tests.test_base import setup_test_path, setup_astrbot_mocks, YunliTestCase, default_config

setup_astrbot_mocks()
import astrbot.api.event
astrbot.api.event.filter.on_llm_request = lambda **kwargs: lambda f: f
astrbot.api.event.filter.on_llm_response = lambda **kwargs: lambda f: f
astrbot.api.event.filter.command = lambda *args, **kwargs: lambda f: f
setup_test_path()

from yunli.main import YunliPersonaPlugin
from yunli.core.request_context import RequestContext
from yunli.core.context_builder import ContextBuilder


# ===================================================================
# 辅助：创建带 mock 依赖的 ContextBuilder 实例
# ===================================================================
def make_mock_context_builder():
    """创建带有 mock 依赖的 ContextBuilder 实例"""
    mock_db = MagicMock()
    mock_engine = MagicMock()
    mock_engine.language = MagicMock()
    mock_engine.language.detect_query_mode.return_value = "chat"
    mock_rel = MagicMock()
    return ContextBuilder(mock_db, mock_engine, mock_rel)


# ===================================================================
# 一、system_prompt 静态性测试（核心）
# ===================================================================
class TestSystemPromptStability(YunliTestCase):
    """测试 system_prompt 在多次注入后保持不变（缓存命中的前提）"""

    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.context = MagicMock(spec=[])
        self.context.data_dir = self.temp_dir
        self.context.config = MagicMock(spec=[])
        if hasattr(self.context.config, 'platform_settings'):
            delattr(self.context.config, 'platform_settings')

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
        self.plugin = YunliPersonaPlugin(self.context, default_config())
        self.plugin._log_interaction = AsyncMock()

    def _start_patcher(self, target, attr_name):
        patcher = patch(target)
        mock_class = patcher.start()
        mock_instance = MagicMock()
        mock_class.return_value = mock_instance
        self._patchers.append(patcher)
        setattr(self, attr_name, mock_instance)
        setattr(self, attr_name + '_class', mock_class)

    def _configure_mocks(self):
        self.mock_db.query_dialogues.return_value = []
        self.mock_db.get_pending_loops.return_value = []
        self.mock_db.flush_logs = MagicMock()
        self.mock_db.close = MagicMock()
        self.mock_db.log_interaction = MagicMock()

        self.mock_engine.build_system_prompt.return_value = "你是云璃..."
        self.mock_engine.build_dynamic_prompt.return_value = ""
        self.mock_engine.get_context_data.return_value = {
            "relevant_knowledge": [], "analogies": [], "user_history": None,
        }
        self.mock_engine.emotion = MagicMock()
        self.mock_engine.emotion.current_state = "neutral"
        self.mock_engine.emotion.transition = MagicMock()
        self.mock_engine.language = MagicMock()
        self.mock_engine.language.detect_query_mode.return_value = "chat"
        # calculate_impulse 返回空 dict → 跳过 SpeechImpulse 注入
        # （真实使用中 calculate_impulse 返回 {"max_chars": int, "hint": str, ...}）
        # 测试场景下用空 dict 模拟"无 impulse 引导"的简洁情况
        self.mock_engine.language.calculate_impulse.return_value = {}
        self.mock_engine.polish_response.side_effect = lambda text, *a, **kw: text
        self.mock_engine.review_response.side_effect = lambda text, *a, **kw: text
        self.mock_engine.get_direct_response.return_value = None

        self.mock_qq_behavior.format_for_qq.side_effect = lambda text, *a, **kw: text
        self.mock_qq_behavior.should_skip_punctuation.return_value = False
        self.mock_qq_behavior.add_typing_pause.side_effect = lambda text: text
        self.mock_qq_behavior.add_human_touches.side_effect = lambda text: text

        self.mock_relationship.get_reply_length_limit.return_value = None
        self.mock_relationship.get_hint.return_value = None
        self.mock_relationship.update = MagicMock()
        self.mock_relationship.detect_user_intent.return_value = None
        self.mock_relationship.INTENT_TO_EMOTION_TRIGGER = {}

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
        self.mock_ctx_builder.detect_social_scene.return_value = ""

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

    def _make_event(self, message_str="测试消息", group_id="123456", user_id="789012",
                    user_nickname="测试用户"):
        event = MagicMock()
        event.message_str = message_str
        event.get_group_id.return_value = group_id
        event.get_sender_id.return_value = user_id
        event.get_sender_name.return_value = user_nickname
        event.plain_result = MagicMock(return_value=message_str)
        event.is_at_me = MagicMock(return_value=False)
        return event

    def _make_req(self, system_prompt=None, prompt=""):
        req = MagicMock()
        req.system_prompt = system_prompt
        req.prompt = prompt
        return req

    # ── 核心测试 ────────────────────────────────────────────

    def test_system_prompt_stable_across_messages(self):
        """同一群聊中不同消息 → system_prompt 完全一致（缓存可命中）"""
        sys_prompts = []

        async def run_one(message):
            event = self._make_event(message_str=message)
            req = self._make_req(system_prompt=None, prompt=message)
            await self.plugin.on_llm_request(event, req)
            sys_prompts.append(req.system_prompt)
            return req

        async def run_all():
            await run_one("你好云璃")
            await run_one("今天天气怎么样")
            await run_one("什么是剑术")

        asyncio.run(run_all())

        self.assertEqual(len(sys_prompts), 3)
        self.assertEqual(sys_prompts[0], sys_prompts[1])
        self.assertEqual(sys_prompts[1], sys_prompts[2])
        self.assertIn("你是云璃", sys_prompts[0])

    def test_system_prompt_no_dynamic_content(self):
        """system_prompt 不包含时变内容（时间、场景信号等）"""
        self.mock_ctx_builder.format_environment_perception.return_value = (
            "当前时间：2026-06-12 14:35 周五\n日期语境：周五、下午"
        )
        self.mock_perception_class.format_scene_description.return_value = "当前消息直接@了你"
        self.mock_engine.build_dynamic_prompt.return_value = "关于剑术的知识：..."

        async def run():
            event = self._make_event(message_str="你好")
            req = self._make_req(system_prompt=None, prompt="你好")
            await self.plugin.on_llm_request(event, req)
            return req

        req = asyncio.run(run())

        sp = req.system_prompt or ""
        self.assertNotIn("当前时间", sp)
        self.assertNotIn("2026-06-12", sp)
        self.assertNotIn("日期语境", sp)
        self.assertNotIn("当前消息直接@了你", sp)
        self.assertNotIn("关于剑术的知识", sp)
        self.assertIn("你是云璃", sp)

    def test_dynamic_content_in_user_prompt(self):
        """动态内容应注入到 req.prompt（用户消息前缀）而非 system_prompt"""
        self.mock_ctx_builder.format_environment_perception.return_value = (
            "当前时间：2026-06-12 14:35 周五"
        )
        self.mock_engine.build_dynamic_prompt.return_value = "相关知识：..."

        async def run():
            event = self._make_event(message_str="你好云璃")
            req = self._make_req(system_prompt=None, prompt="你好云璃")
            await self.plugin.on_llm_request(event, req)
            return req

        req = asyncio.run(run())

        prompt = req.prompt or ""
        self.assertIn("[当前上下文]", prompt)
        self.assertIn("[用户消息]", prompt)
        self.assertIn("当前时间：2026-06-12 14:35 周五", prompt)
        self.assertIn("相关知识：...", prompt)
        self.assertIn("你好云璃", prompt)

    def test_user_prompt_preserves_original_message(self):
        """req.prompt 包含原始用户消息，且在 [用户消息] 标记之后"""
        original = "今天天气不错，适合练剑"
        self.mock_engine.build_dynamic_prompt.return_value = "相关知识：剑术基础"

        async def run():
            event = self._make_event(message_str=original)
            req = self._make_req(system_prompt=None, prompt=original)
            await self.plugin.on_llm_request(event, req)
            return req

        req = asyncio.run(run())
        self.assertIn(original, req.prompt)
        user_msg_pos = req.prompt.index("[用户消息]")
        self.assertIn(original, req.prompt[user_msg_pos:])

    def test_fallback_when_no_prompt_attr(self):
        """req 无 prompt 属性 → 回退到 system_prompt 注入（兼容旧框架）"""
        self.mock_ctx_builder.format_environment_perception.return_value = (
            "当前时间：2026-06-12 14:35 周五"
        )

        async def run():
            event = self._make_event(message_str="你好")
            req = MagicMock()
            req.system_prompt = None
            del req.prompt
            await self.plugin.on_llm_request(event, req)
            return req

        req = asyncio.run(run())

        self.assertIn("你是云璃", req.system_prompt)
        self.assertIn("当前时间", req.system_prompt)

    def test_existing_system_prompt_appended(self):
        """已有 system_prompt 时正确追加（与其他插件共存）"""
        async def run():
            event = self._make_event(message_str="你好")
            req = self._make_req(
                system_prompt="其他插件的提示词",
                prompt="你好",
            )
            await self.plugin.on_llm_request(event, req)
            return req

        req = asyncio.run(run())

        self.assertIn("其他插件的提示词", req.system_prompt)
        self.assertIn("[[YUNLI_BOUNDARY]]", req.system_prompt)
        self.assertIn("你是云璃", req.system_prompt)
        boundary_pos = req.system_prompt.index("[[YUNLI_BOUNDARY]]")
        after_boundary = req.system_prompt[boundary_pos:]
        self.assertNotIn("当前时间", after_boundary)

    def test_debounce_does_not_inject(self):
        """防抖缓冲中 → 不注入，system_prompt 保持 None"""
        self.mock_debouncer.handle_message = AsyncMock(return_value=True)

        async def run():
            event = self._make_event(message_str="测试")
            req = self._make_req(system_prompt=None, prompt="测试")
            await self.plugin.on_llm_request(event, req)
            return req

        req = asyncio.run(run())
        self.assertIsNone(req.system_prompt)

    def test_empty_dynamic_context_prompt_unchanged(self):
        """动态上下文为空 → req.prompt 仍可能被注入（v2.2.0 thread_tracker 总是注入对话线程）

        v2.2.0 重构：对话线程追踪是必选的 dynamic part，无法 mock 掉。
        测试目标改为：验证原始消息"你好"始终在 req.prompt 中（即便上下文被注入也不丢失）。
        """
        async def run():
            event = self._make_event(message_str="你好")
            req = self._make_req(system_prompt=None, prompt="你好")
            await self.plugin.on_llm_request(event, req)
            return req

        req = asyncio.run(run())
        # 原始消息必须保留
        self.assertIn("你好", req.prompt)
        # v2.2.0：thread_tracker 会注入 [用户消息] 标记
        self.assertIn("[用户消息]", req.prompt)


# ===================================================================
# 二、确定性方法测试（静态方法，无需实例化 ContextBuilder）
# ===================================================================
class TestDeterministicMethods(YunliTestCase):
    """测试 ContextBuilder 的静态确定性方法"""

    def test_deterministic_choice_consistent(self):
        """_deterministic_choice：相同种子总是返回相同结果"""
        items = ["选项A", "选项B", "选项C"]
        seed = "test_seed_123"

        results = [ContextBuilder._deterministic_choice(items, seed) for _ in range(50)]
        self.assertEqual(len(set(results)), 1)

    def test_deterministic_choice_different_seeds(self):
        """_deterministic_choice：不同种子可以返回不同结果"""
        items = ["A", "B", "C", "D", "E"]

        results = set()
        for i in range(100):
            result = ContextBuilder._deterministic_choice(items, f"seed_{i}")
            results.add(result)

        self.assertGreater(len(results), 1,
                          "100个不同种子应产生至少2种不同选择")

    def test_deterministic_choice_single_item(self):
        """_deterministic_choice：单元素列表始终返回该元素"""
        result = ContextBuilder._deterministic_choice(["唯一"], "any_seed")
        self.assertEqual(result, "唯一")

    def test_deterministic_probability_consistent(self):
        """_deterministic_probability：相同种子总是返回相同布尔值"""
        seed = "consistent_test"
        results = [ContextBuilder._deterministic_probability(seed, 0.5) for _ in range(50)]
        self.assertEqual(len(set(results)), 1)

    def test_deterministic_probability_distribution(self):
        """_deterministic_probability：1000 个种子的大致分布接近理论值"""
        true_count = sum(
            1 for i in range(1000)
            if ContextBuilder._deterministic_probability(f"dist_{i}", 0.3)
        )

        ratio = true_count / 1000.0
        self.assertGreaterEqual(ratio, 0.25)
        self.assertLessEqual(ratio, 0.35)

    def test_deterministic_probability_threshold_zero(self):
        """_deterministic_probability：threshold=0 始终返回 False"""
        results = [ContextBuilder._deterministic_probability(f"z_{i}", 0.0) for i in range(100)]
        self.assertTrue(all(not r for r in results))

    def test_deterministic_probability_threshold_one(self):
        """_deterministic_probability：threshold=1 始终返回 True"""
        results = [ContextBuilder._deterministic_probability(f"o_{i}", 1.0) for i in range(100)]
        self.assertTrue(all(results))

    def test_is_memory_fuzzy_deterministic(self):
        """is_memory_fuzzy：相同参数总是返回相同结果"""
        results = [ContextBuilder.is_memory_fuzzy(0, "2026-05-01 12:00:00", "测试") for _ in range(20)]
        self.assertEqual(len(set(results)), 1)

    def test_is_memory_fuzzy_high_access_not_fuzzy(self):
        """is_memory_fuzzy：高访问次数 → False"""
        self.assertFalse(ContextBuilder.is_memory_fuzzy(10, "2024-01-01 12:00:00", "常访问"))

    def test_is_memory_fuzzy_recent_not_fuzzy(self):
        """is_memory_fuzzy：最近记忆 → False"""
        from datetime import datetime
        today = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        self.assertFalse(ContextBuilder.is_memory_fuzzy(1, today, "新记忆"))

    def test_is_memory_fuzzy_no_date(self):
        """is_memory_fuzzy：无日期 → False"""
        self.assertFalse(ContextBuilder.is_memory_fuzzy(1, "", "无日期"))


# ===================================================================
# 三、确定性记忆文本测试
# ===================================================================
class TestDeterministicMemoryText(YunliTestCase):
    """测试 ContextBuilder 的确定性记忆文本生成"""

    def setUp(self):
        self.builder = make_mock_context_builder()

    def test_build_memory_lines_same_input_same_output(self):
        """相同输入多次调用 → 完全相同的输出"""
        mem_dict = {
            "preference": [("喜欢甜食", False, 100)],
            "fact": [("程序员", False, 200)],
            "event": [("去过黄山", False, 300)],
        }

        results = []
        for _ in range(10):
            lines = self.builder._build_memory_lines_for_user("小明", mem_dict, is_own=True)
            results.append("\n".join(lines))

        self.assertEqual(len(set(results)), 1)

    def test_build_memory_lines_fuzzy_deterministic(self):
        """模糊记忆 → 相同输入产生相同模糊效果"""
        mem_dict = {
            "preference": [("喜欢甜食", True, 100)],
        }

        results = []
        for _ in range(10):
            lines = self.builder._build_memory_lines_for_user("小明", mem_dict, is_own=True)
            results.append("\n".join(lines))

        self.assertEqual(len(set(results)), 1)

    def test_memory_with_empty_dict(self):
        """空记忆字典 → 返回空列表"""
        lines = self.builder._build_memory_lines_for_user("小明", {}, is_own=True)
        self.assertEqual(lines, [])

    def test_memory_fuzzy_and_clear_mixed(self):
        """混合模糊/清晰记忆 → 确定性处理"""
        mem_dict = {
            "preference": [
                ("喜欢甜食", False, 100),
                ("讨厌苦瓜", True, 50),
            ],
        }

        lines = self.builder._build_memory_lines_for_user("小明", mem_dict, is_own=True)
        self.assertGreaterEqual(len(lines), 1)

    def test_memory_with_empty_preference_contents(self):
        """记忆内容为空 → 该类型不会生成行"""
        mem_dict = {
            "preference": [("", False, 100)],
            "fact": [("", False, 200)],
            "event": [("", False, 300)],
        }
        lines = self.builder._build_memory_lines_for_user("小明", mem_dict, is_own=True)
        self.assertEqual(lines, [])

    def test_memory_preserves_nickname_in_output(self):
        """记忆输出包含用户的昵称"""
        mem_dict = {
            "preference": [("喜欢甜食", False, 100)],
        }
        lines = self.builder._build_memory_lines_for_user("小明", mem_dict, is_own=True)
        if lines:
            self.assertIn("小明", lines[0])


# ===================================================================
# 四、真实 ContextBuilder 集成测试
# ===================================================================
class TestRealContextBuilderIntegration(YunliTestCase):
    """使用真实 ContextBuilder 实例测试环境感知和缓存稳定性"""

    def setUp(self):
        self.builder = make_mock_context_builder()

    def test_format_environment_perception_stable(self):
        """连续调用 format_environment_perception 输出格式一致"""
        import re
        env1 = self.builder.format_environment_perception()
        env2 = self.builder.format_environment_perception()

        self.assertIn("当前时间", env1)
        self.assertIn("日期语境", env2)

        # 移除时间戳后内容应完全一致
        env1_no_time = re.sub(r'\d{2}:\d{2}', 'HH:MM', env1)
        env2_no_time = re.sub(r'\d{2}:\d{2}', 'HH:MM', env2)
        self.assertEqual(env1_no_time, env2_no_time)

    def test_format_environment_perception_format_valid(self):
        """环境感知输出格式验证"""
        env = self.builder.format_environment_perception()
        import re
        # 应含 "当前时间：YYYY-MM-DD HH:MM 周X"
        self.assertRegex(env, r'当前时间：\d{4}-\d{2}-\d{2} \d{2}:\d{2} 周[一二三四五六日]')


# ===================================================================
# 五、缓存命中率统计测试
# ===================================================================
class TestCacheHitRateMetrics(YunliTestCase):
    """量化验证缓存改进效果"""

    def test_system_prompt_hash_consistency(self):
        """模拟 5 条消息 → system_prompt MD5 hash 完全一致"""
        temp_dir = tempfile.mkdtemp()
        patchers = []
        try:
            context = MagicMock(spec=[])
            context.data_dir = temp_dir
            context.config = MagicMock(spec=[])
            if hasattr(context.config, 'platform_settings'):
                delattr(context.config, 'platform_settings')

            patchers = [
                patch('yunli.main.YunliDatabase'),
                patch('yunli.main.YunliPersonaEngine'),
                patch('yunli.main.QQBehaviorManager'),
                patch('yunli.main.RelationshipManager'),
                patch('yunli.main.MessageSplitter'),
                patch('yunli.main.MessageDebouncer'),
                patch('yunli.main.AtDetector'),
                patch('yunli.main.ContextBuilder'),
                patch('yunli.main.GroupPerception'),
                patch('yunli.main.MemoryManager'),
            ]

            mock_db = patchers[0].start()
            mock_db_inst = MagicMock()
            mock_db.return_value = mock_db_inst
            mock_db_inst.get_pending_loops.return_value = []
            mock_db_inst.query_dialogues.return_value = []

            mock_engine = patchers[1].start()
            mock_engine_inst = MagicMock()
            mock_engine.return_value = mock_engine_inst
            mock_engine_inst.build_system_prompt.return_value = "你是云璃..."
            mock_engine_inst.build_dynamic_prompt.return_value = ""
            mock_engine_inst.get_context_data.return_value = {
                "relevant_knowledge": [], "analogies": [], "user_history": None,
            }
            mock_engine_inst.emotion = MagicMock()
            mock_engine_inst.emotion.current_state = "neutral"
            mock_engine_inst.language = MagicMock()
            mock_engine_inst.language.detect_query_mode.return_value = "chat"

            mock_qq = patchers[2].start()
            mock_qq_inst = MagicMock()
            mock_qq.return_value = mock_qq_inst
            mock_qq_inst.format_for_qq.side_effect = lambda text, *a, **kw: text

            mock_rel = patchers[3].start()
            mock_rel_inst = MagicMock()
            mock_rel.return_value = mock_rel_inst
            mock_rel_inst.get_hint.return_value = None
            mock_rel_inst.get_reply_length_limit.return_value = None
            mock_rel_inst.update = MagicMock()
            mock_rel_inst.detect_user_intent.return_value = None
            mock_rel_inst.INTENT_TO_EMOTION_TRIGGER = {}

            mock_split = patchers[4].start()
            mock_split_inst = MagicMock()
            mock_split.return_value = mock_split_inst
            mock_split_inst.split.return_value = []

            mock_deb = patchers[5].start()
            mock_deb_inst = MagicMock()
            mock_deb.return_value = mock_deb_inst
            mock_deb_inst.handle_message = AsyncMock(return_value=False)
            mock_deb_inst.mark_processed = MagicMock()

            mock_at = patchers[6].start()
            mock_at_inst = MagicMock()
            mock_at.return_value = mock_at_inst
            mock_at_inst.is_at_me.return_value = True

            mock_ctx = patchers[7].start()
            mock_ctx_inst = MagicMock()
            mock_ctx.return_value = mock_ctx_inst
            mock_ctx_inst.format_environment_perception.return_value = ""
            mock_ctx_inst.add_relationship_context.return_value = ""
            mock_ctx_inst.build_chat_context.return_value = ""
            mock_ctx_inst.build_recent_chat_history.return_value = ""

            mock_per = patchers[8].start()
            mock_per_inst = MagicMock()
            mock_per.return_value = mock_per_inst
            mock_per_inst.get_atmosphere_text = MagicMock(return_value="")
            # GroupPerception 类级别静态方法
            mock_per.extract_scene_signals = MagicMock(return_value={})
            mock_per.format_scene_description = MagicMock(return_value="")

            mock_mem = patchers[9].start()
            mock_mem_inst = MagicMock()
            mock_mem.return_value = mock_mem_inst

            plugin = YunliPersonaPlugin(context, default_config())
            plugin._log_interaction = AsyncMock()

            hashes = []

            async def send_message(msg):
                event = MagicMock()
                event.message_str = msg
                event.get_group_id.return_value = "123456"
                event.get_sender_id.return_value = "789012"
                event.get_sender_name.return_value = "用户"
                event.plain_result = MagicMock(return_value=msg)
                event.is_at_me = MagicMock(return_value=False)

                req = MagicMock()
                req.system_prompt = None
                req.prompt = msg

                await plugin.on_llm_request(event, req)
                sp = req.system_prompt or ""
                hashes.append(hashlib.md5(sp.encode()).hexdigest())

            messages = ["你好云璃！", "今天天气真好", "什么是剑术", "你吃饭了吗", "晚安"]

            async def run_all():
                for msg in messages:
                    await send_message(msg)

            asyncio.run(run_all())

            unique_hashes = set(hashes)
            self.assertEqual(
                len(unique_hashes), 1,
                f"预期 1 个唯一 system_prompt hash，实际 {len(unique_hashes)} 个"
            )
        finally:
            for patcher in patchers:
                patcher.stop()
            shutil.rmtree(temp_dir, ignore_errors=True)


if __name__ == '__main__':
    import unittest
    unittest.main()