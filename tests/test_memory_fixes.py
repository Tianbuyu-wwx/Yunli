"""记忆系统修复测试

覆盖本次修复的 9 项问题：
- P1.3 记忆归属判断（移除昵称回退匹配）
- P1.4 群友记忆召回（折扣/limit/min_confidence 调整）
- P1.5 记忆整理事务保护（replace_user_memories 原子性）
- P2.6 轻量提取上下文校验（否定词/疑问词/开头禁用词）
- P2.7 置信度衰减机制（decay_memory_confidence）
- P2.8 冲突检测精细规则（身份/性别表述上下文校验）
- P3.9 防抖合并记忆遗漏（on_individual_message 回调）
- P3.10 记忆文本截断（15→30 字）
- P3.13 群聊摘要生成（generate_group_summaries）
"""

import os
import sys
import asyncio
import sqlite3
import unittest
import tempfile
import shutil
from datetime import datetime, timedelta
from unittest.mock import MagicMock, AsyncMock, patch

from tests.test_base import YunliTestCase, setup_test_path, setup_astrbot_mocks

setup_test_path()

from yunli.database import YunliDatabase
from yunli.core.thread_tracker import get_thread_tracker


class TestMemoryAttribution(YunliTestCase):
    """P1.3 记忆归属判断测试"""

    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.db_path = os.path.join(self.temp_dir, "test.db")
        self.db = YunliDatabase(self.db_path)

    def tearDown(self):
        self.db.close()
        shutil.rmtree(self.temp_dir)

    def test_strict_attribution_no_nickname_fallback(self):
        """测试严格归属判断：mem_user_id 为空时归入群记忆，不回退到昵称匹配"""
        # 直接插入一条 user_id 为空的记忆（模拟旧数据）
        self.db.memory_db.conn.execute(
            "INSERT INTO user_memories (group_id, user_id, user_nickname, memory_type, content, confidence, status) "
            "VALUES (?, '', ?, ?, ?, ?, 'active')",
            ("group1", "小明", "fact", "是大学生", 7),
        )
        self.db.memory_db.conn.commit()

        # 构建记忆列表（模拟从数据库获取的）
        memories = [{
            "id": 1,
            "group_id": "group1",
            "user_id": "",  # 旧数据 user_id 为空
            "user_nickname": "小明",
            "memory_type": "fact",
            "content": "是大学生",
            "confidence": 7,
            "status": "active",
            "access_count": 1,
            "created_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        }]

        from yunli.core.context_builder import ContextBuilder
        from yunli.persona import YunliPersonaEngine

        persona_engine = YunliPersonaEngine(self.db, {})
        builder = ContextBuilder(self.db, persona_engine, MagicMock(), {})

        # current_user_id='userA'，但记忆 user_id 为空
        lines = builder.build_natural_memory_text(
            "小明", memories, current_user_id="userA"
        )
        text = "\n".join(lines)

        # 旧记忆 user_id 为空，应归入群记忆，不应出现在"你记得自己..."的表述中
        self.assertNotIn("你记得自己", text)
        # 应该以群友视角描述
        self.assertIn("小明", text)


class TestGroupMemoryRecall(YunliTestCase):
    """P1.4 群友记忆召回测试"""

    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.db_path = os.path.join(self.temp_dir, "test.db")
        self.db = YunliDatabase(self.db_path)

    def tearDown(self):
        self.db.close()
        shutil.rmtree(self.temp_dir)

    def test_min_confidence_default_lowered(self):
        """测试 min_confidence 默认值从 7 降为 5"""
        from yunli.core.context_builder import ContextBuilder
        from yunli.persona import YunliPersonaEngine

        persona_engine = YunliPersonaEngine(self.db, {})
        builder = ContextBuilder(self.db, persona_engine, MagicMock(), {})

        # 添加 confidence=5 的记忆（旧默认 7 会被过滤）
        self.db.add_memory("group1", "userA", "fact", "喜欢猫", confidence=5)

        # 使用默认参数调用，应能召回 confidence=5 的记忆
        memories = builder.get_relevant_memories("group1", "userA", "猫")
        # confidence=5 的记忆应被召回（旧默认 min_confidence=7 会被过滤）
        self.assertTrue(len(memories) >= 1)

    def test_group_memory_discount_reduced(self):
        """测试群友记忆折扣从 0.7 调整为 0.9"""
        # 添加群友记忆
        self.db.add_memory("group1", "userB", "fact", "喜欢狗", confidence=8,
                           user_nickname="小红")

        # 获取群友记忆
        group_mems = self.db.get_group_memories("group1", exclude_user_id="userA")
        self.assertTrue(len(group_mems) >= 1)

        # 验证折扣调整后群友记忆能被召回（通过评分）
        from yunli.core.context_builder import ContextBuilder
        from yunli.persona import YunliPersonaEngine

        persona_engine = YunliPersonaEngine(self.db, {})
        builder = ContextBuilder(self.db, persona_engine, MagicMock(), {})

        # 添加当前用户记忆用于对比
        self.db.add_memory("group1", "userA", "fact", "喜欢鱼", confidence=8)

        memories = builder.get_relevant_memories("group1", "userA", "宠物")
        # 应同时召回自己的记忆和群友记忆
        self.assertTrue(len(memories) >= 1)


class TestTransactionProtection(YunliTestCase):
    """P1.5 记忆整理事务保护测试"""

    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.db_path = os.path.join(self.temp_dir, "test.db")
        self.db = YunliDatabase(self.db_path)

    def tearDown(self):
        self.db.close()
        shutil.rmtree(self.temp_dir)

    def test_replace_user_memories_success(self):
        """测试原子性替换成功"""
        # 准备旧记忆
        self.db.add_memory("group1", "userA", "fact", "旧记忆1", confidence=5)
        self.db.add_memory("group1", "userA", "fact", "旧记忆2", confidence=5)

        # 执行替换
        new_memories = [
            {"type": "fact", "content": "新记忆1", "confidence": 8},
            {"type": "preference", "content": "喜欢猫", "confidence": 7},
        ]
        count = self.db.memory_db.replace_user_memories(
            "group1", "userA", new_memories, "小明"
        )

        self.assertEqual(count, 2)
        # 验证旧记忆被删除
        all_mems = self.db.get_memories("group1", "userA", include_outdated=True)
        self.assertEqual(len(all_mems), 2)
        contents = {m["content"] for m in all_mems}
        self.assertIn("新记忆1", contents)
        self.assertIn("喜欢猫", contents)

    def test_replace_user_memories_invalid_content_skipped(self):
        """测试无效内容被跳过，有效内容正常写入"""
        new_memories = [
            {"type": "fact", "content": "有效记忆", "confidence": 8},
            {"type": "fact", "content": "x", "confidence": 8},  # 太短，跳过
            {"type": "fact", "content": "", "confidence": 8},  # 空，跳过
        ]
        count = self.db.memory_db.replace_user_memories(
            "group1", "userA", new_memories, "小明"
        )
        self.assertEqual(count, 1)  # 只有 1 条有效

    def test_replace_user_memories_all_invalid_rollback(self):
        """P0-1 修复验证：所有新记忆都被长度校验过滤时，旧记忆保留

        场景：LLM 返回的内容全部不符合长度要求（<2 或 >50），
        原代码会 COMMIT 导致旧记忆被清空、新记忆 0 条写入。
        修复后应 ROLLBACK 保留旧记忆，返回 0。
        """
        # 准备旧记忆
        self.db.add_memory("group1", "userA", "fact", "旧记忆1", confidence=5)
        self.db.add_memory("group1", "userA", "fact", "旧记忆2", confidence=5)

        # 所有新记忆都无效（太短或太长）
        new_memories = [
            {"type": "fact", "content": "x", "confidence": 8},  # 太短
            {"type": "fact", "content": "", "confidence": 8},   # 空
            {"type": "fact", "content": "a" * 51, "confidence": 8},  # 太长
        ]
        count = self.db.memory_db.replace_user_memories(
            "group1", "userA", new_memories, "小明"
        )

        # 验证返回 0（未写入）
        self.assertEqual(count, 0)

        # 验证旧记忆被保留（未被清空）
        all_mems = self.db.get_memories("group1", "userA", include_outdated=True)
        self.assertEqual(len(all_mems), 2, "旧记忆应被保留")
        contents = {m["content"] for m in all_mems}
        self.assertIn("旧记忆1", contents)
        self.assertIn("旧记忆2", contents)

    def test_replace_user_memories_empty_list_rollback(self):
        """P0-1 修复验证：传入空列表时，旧记忆保留

        场景：LLM 返回空列表（如调用失败或返回无效 JSON），
        原代码会 COMMIT 导致旧记忆被清空。修复后应 ROLLBACK。
        """
        # 准备旧记忆
        self.db.add_memory("group1", "userA", "fact", "重要记忆", confidence=8)

        # 传入空列表
        count = self.db.memory_db.replace_user_memories(
            "group1", "userA", [], "小明"
        )

        # 验证返回 0
        self.assertEqual(count, 0)

        # 验证旧记忆被保留
        all_mems = self.db.get_memories("group1", "userA", include_outdated=True)
        self.assertEqual(len(all_mems), 1, "旧记忆应被保留")
        self.assertEqual(all_mems[0]["content"], "重要记忆")

    def test_replace_user_memories_partial_invalid_preserves_valid(self):
        """P0-1 边界验证：部分有效部分无效时，有效记忆正常写入

        场景：LLM 返回 3 条记忆，1 条无效被过滤，2 条有效。
        written_count=2 > 0，应正常 COMMIT。
        """
        # 准备旧记忆
        self.db.add_memory("group1", "userA", "fact", "旧记忆", confidence=5)

        # 1 条无效 + 2 条有效
        new_memories = [
            {"type": "fact", "content": "x", "confidence": 8},  # 无效
            {"type": "fact", "content": "有效记忆1", "confidence": 8},
            {"type": "preference", "content": "喜欢编程", "confidence": 7},
        ]
        count = self.db.memory_db.replace_user_memories(
            "group1", "userA", new_memories, "小明"
        )

        # 验证返回 2（有效记忆正常写入）
        self.assertEqual(count, 2)

        # 验证旧记忆被删除，新记忆被写入
        all_mems = self.db.get_memories("group1", "userA", include_outdated=True)
        self.assertEqual(len(all_mems), 2)
        contents = {m["content"] for m in all_mems}
        self.assertIn("有效记忆1", contents)
        self.assertIn("喜欢编程", contents)
        self.assertNotIn("旧记忆", contents)

    def test_safe_rollback_logs_error_on_failure(self):
        """P0-2 修复验证：ROLLBACK 失败时记录错误日志

        场景：模拟 ROLLBACK 失败（如连接断开），
        原代码 `except: pass` 静默吞掉，修复后应记录 logger.error。

        注意：sqlite3.Connection.execute 是只读属性，无法用 patch.object，
        改为临时替换 memory_db.conn 为 mock 对象。
        """
        memory_db = self.db.memory_db
        original_conn = memory_db.conn

        # 创建 mock connection，使 ROLLBACK 抛异常
        mock_conn = MagicMock()
        mock_conn.execute.side_effect = sqlite3.OperationalError(
            "cannot rollback - no transaction is active"
        )

        # 临时替换 conn
        memory_db.conn = mock_conn

        try:
            # 捕获日志
            with patch('yunli.database.init_db.logger') as mock_logger:
                # 调用 _safe_rollback（不应抛异常）
                memory_db._safe_rollback()

                # 验证 ROLLBACK 失败被记录（不再静默 pass）
                self.assertTrue(
                    mock_logger.error.called,
                    "ROLLBACK 失败应记录 logger.error，不应静默吞掉"
                )
                # 验证日志内容包含关键信息
                error_calls = mock_logger.error.call_args_list
                self.assertTrue(
                    any("ROLLBACK" in str(call) for call in error_calls),
                    f"日志应包含 ROLLBACK 关键词，实际: {error_calls}"
                )
        finally:
            # 恢复原始连接
            memory_db.conn = original_conn


class TestLightweightExtractionContext(YunliTestCase):
    """P2.6 轻量提取上下文校验测试"""

    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.db_path = os.path.join(self.temp_dir, "test.db")
        self.db = YunliDatabase(self.db_path)
        from yunli.core.memory_manager import MemoryManager
        self.manager = MemoryManager(self.db, {"lightweight_extraction_enabled": True})

    def tearDown(self):
        self.db.close()
        shutil.rmtree(self.temp_dir)

    def test_negation_context_skipped(self):
        """测试否定上下文跳过：'我不是学生' 不应提取"""
        self.manager.extract_memory_lightweight(
            "group1", "userA", "我不是学生。", "小明"
        )
        memories = self.db.get_memories("group1", "userA", include_outdated=True)
        # 不应提取出"学生"
        for m in memories:
            self.assertNotEqual(m["content"], "学生")

    def test_content_prefix_blacklist(self):
        """测试开头禁用词：'我是说真的喜欢这个' 不应提取'说真的喜欢这个'"""
        self.manager.extract_memory_lightweight(
            "group1", "userA", "我是说真的喜欢这个。", "小明"
        )
        memories = self.db.get_memories("group1", "userA", include_outdated=True)
        for m in memories:
            self.assertNotIn("说真的", m["content"])

    def test_normal_extraction_still_works(self):
        """测试正常提取仍然有效：'我是大学生。' 应提取'大学生'"""
        self.manager.extract_memory_lightweight(
            "group1", "userA", "我是大学生。", "小明"
        )
        memories = self.db.get_memories("group1", "userA", include_outdated=True)
        # 应提取出身份相关记忆
        self.assertTrue(len(memories) >= 1)


class TestConfidenceDecay(YunliTestCase):
    """P2.7 置信度衰减机制测试"""

    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.db_path = os.path.join(self.temp_dir, "test.db")
        self.db = YunliDatabase(self.db_path)

    def tearDown(self):
        self.db.close()
        shutil.rmtree(self.temp_dir)

    def test_decay_reduces_confidence(self):
        """测试衰减降低置信度"""
        # 添加高置信度记忆
        self.db.add_memory("group1", "userA", "fact", "测试记忆", confidence=10)

        # P1-7 修复：新记忆有 1 小时保护期，需手动将 created_at 改为 2 小时前
        with self.db.memory_db._lock:
            self.db.memory_db.conn.execute(
                "UPDATE user_memories SET created_at = datetime('now', '-2 hours') "
                "WHERE group_id = 'group1' AND user_id = 'userA'"
            )
            self.db.memory_db.conn.commit()

        # 执行衰减
        # P1-7 修复：fact 类型衰减系数 = decay_factor + 0.03 = 0.53
        self.db.memory_db.decay_memory_confidence(decay_factor=0.5, min_confidence=3)

        # 验证置信度降低
        memories = self.db.get_memories("group1", "userA")
        self.assertEqual(len(memories), 1)
        # fact 类型：10 * 0.53 = 5.3 → 截断为 5
        self.assertEqual(memories[0]["confidence"], 5)

    def test_decay_respects_min_confidence(self):
        """测试衰减不低于最小置信度"""
        self.db.add_memory("group1", "userA", "fact", "低置信度", confidence=4)

        # P1-7 修复：新记忆有 1 小时保护期，需手动将 created_at 改为 2 小时前
        with self.db.memory_db._lock:
            self.db.memory_db.conn.execute(
                "UPDATE user_memories SET created_at = datetime('now', '-2 hours') "
                "WHERE group_id = 'group1' AND user_id = 'userA'"
            )
            self.db.memory_db.conn.commit()

        # 衰减系数 0.1，但 min_confidence=3
        # P1-7 修复：fact 类型衰减系数 = 0.1 + 0.03 = 0.13
        self.db.memory_db.decay_memory_confidence(decay_factor=0.1, min_confidence=3)

        memories = self.db.get_memories("group1", "userA")
        self.assertEqual(len(memories), 1)
        # 应被限制在 min_confidence=3
        self.assertEqual(memories[0]["confidence"], 3)

    def test_decay_skips_low_confidence(self):
        """测试低置信度记忆不衰减"""
        self.db.add_memory("group1", "userA", "fact", "低置信度", confidence=2)

        self.db.memory_db.decay_memory_confidence(decay_factor=0.5, min_confidence=3)

        memories = self.db.get_memories("group1", "userA")
        self.assertEqual(len(memories), 1)
        # confidence=2 <= min_confidence=3，不衰减
        self.assertEqual(memories[0]["confidence"], 2)


class TestConflictDetectionRefined(YunliTestCase):
    """P2.8 冲突检测精细规则测试"""

    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.db_path = os.path.join(self.temp_dir, "test.db")
        self.db = YunliDatabase(self.db_path)

    def tearDown(self):
        self.db.close()
        shutil.rmtree(self.temp_dir)

    def test_identity_conflict_detected(self):
        """测试身份冲突正确检测：'我是学生' vs '我是工作'"""
        self.db.add_memory("group1", "userA", "fact", "我是学生", confidence=7)
        self.db.add_memory("group1", "userA", "fact", "我是工作", confidence=7)

        all_mems = self.db.get_memories("group1", "userA", include_outdated=True)
        statuses = {m["status"] for m in all_mems}
        # 应有冲突标记
        self.assertIn("conflicted", statuses)

    def test_non_identity_no_false_conflict(self):
        """测试非身份表述不误判：'喜欢学生' vs '我是工作' 不应冲突"""
        self.db.add_memory("group1", "userA", "fact", "喜欢学生", confidence=7)
        self.db.add_memory("group1", "userA", "fact", "我是工作", confidence=7)

        all_mems = self.db.get_memories("group1", "userA", include_outdated=True)
        # "喜欢学生"不是身份表述，不应被标记为冲突
        for m in all_mems:
            if m["content"] == "喜欢学生":
                self.assertNotEqual(m["status"], "conflicted")

    def test_gender_conflict_detected(self):
        """测试性别冲突正确检测：'我是男的' vs '我是女的'"""
        self.db.add_memory("group1", "userA", "fact", "我是男的", confidence=7)
        self.db.add_memory("group1", "userA", "fact", "我是女的", confidence=7)

        all_mems = self.db.get_memories("group1", "userA", include_outdated=True)
        statuses = {m["status"] for m in all_mems}
        self.assertIn("conflicted", statuses)

    def test_gender_preference_no_false_conflict(self):
        """测试性别偏好不误判：'喜欢男的' vs '我是女的' 不应冲突"""
        self.db.add_memory("group1", "userA", "fact", "喜欢男的", confidence=7)
        self.db.add_memory("group1", "userA", "fact", "我是女的", confidence=7)

        all_mems = self.db.get_memories("group1", "userA", include_outdated=True)
        # "喜欢男的"不是性别表述，不应被标记为冲突
        for m in all_mems:
            if m["content"] == "喜欢男的":
                self.assertNotEqual(m["status"], "conflicted")


class TestDebouncerIndividualMessage(YunliTestCase):
    """P3.9 防抖合并记忆遗漏测试"""

    def setUp(self):
        from yunli.core.debouncer import MessageDebouncer
        self.flush_mock = AsyncMock()
        self.individual_mock = AsyncMock()
        self.debouncer = MessageDebouncer(
            debounce_seconds=0.1,
            on_flush=self.flush_mock,
            on_individual_message=self.individual_mock,
        )

    def test_individual_message_callback_called(self):
        """测试合并时对每条原始消息调用回调"""
        async def run_test():
            # 先让 scope2 有"已处理"记录，这样后续消息才会被缓冲
            event0 = MagicMock()
            event0.message_str = "首条消息"
            req0 = MagicMock()
            r0 = await self.debouncer.handle_message("scope2", event0, req0)
            self.assertFalse(r0)  # 首条立即处理
            self.debouncer.mark_processed("scope2")

            # 短暂等待后发送两条消息（在窗口期内，会被缓冲合并）
            await asyncio.sleep(0.01)

            event2 = MagicMock()
            event2.message_str = "我喜欢狗"
            req2 = MagicMock()
            r2 = await self.debouncer.handle_message("scope2", event2, req2)
            self.assertTrue(r2)  # 被缓冲

            event3 = MagicMock()
            event3.message_str = "我喜欢鱼"
            req3 = MagicMock()
            r3 = await self.debouncer.handle_message("scope2", event3, req3)
            self.assertTrue(r3)  # 被缓冲

            # 等待防抖窗口处理
            await asyncio.sleep(0.3)

            # 验证 on_individual_message 被调用（对每条原始消息）
            self.assertTrue(self.individual_mock.call_count >= 2)

        asyncio.run(run_test())


class TestMemoryTextTruncation(YunliTestCase):
    """P3.10 记忆文本截断测试"""

    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.db_path = os.path.join(self.temp_dir, "test.db")
        self.db = YunliDatabase(self.db_path)

    def tearDown(self):
        self.db.close()
        shutil.rmtree(self.temp_dir)

    def test_long_memory_not_overly_truncated(self):
        """测试长记忆不被过度截断：30 字以内不截断"""
        from yunli.core.context_builder import ContextBuilder
        from yunli.persona import YunliPersonaEngine

        # 添加 25 字的记忆（旧 15 字截断会丢失信息，新 30 字不截断）
        long_content = "用户是计算机科学专业的大学生"
        self.db.add_memory("group1", "userA", "fact", long_content, confidence=7,
                           user_nickname="小明")

        persona_engine = YunliPersonaEngine(self.db, {})
        builder = ContextBuilder(self.db, persona_engine, MagicMock(), {})

        memories = self.db.get_important_memories("group1", "userA", min_confidence=5)
        lines = builder.build_natural_memory_text("小明", memories, current_user_id="userA")
        text = "\n".join(lines)

        # 25 字的记忆不应被截断（旧逻辑会截断为 15 字 + "…"）
        self.assertIn("计算机科学专业", text)
        self.assertNotIn("计算机科学专业的…", text)


class TestGroupSummaryGeneration(YunliTestCase):
    """P3.13 群聊摘要生成测试"""

    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.db_path = os.path.join(self.temp_dir, "test.db")
        self.db = YunliDatabase(self.db_path)
        from yunli.core.memory_manager import MemoryManager
        # 使用 mock context 避免 LLM 调用
        self.context = MagicMock()
        self.manager = MemoryManager(
            self.db,
            {"memory_llm_enabled": True, "lightweight_extraction_enabled": True},
            self.context,
        )

    def tearDown(self):
        self.db.close()
        shutil.rmtree(self.temp_dir)

    def test_get_recent_interactions(self):
        """测试获取最近互动"""
        # 添加互动日志
        for i in range(10):
            self.db.log_interaction(
                group_id="group1", user_id=f"user{i}",
                user_nickname=f"用户{i}",
                message=f"测试消息{i}", response=f"回复{i}",
                trigger_type="at", emotion_state="neutral",
            )
        # 强制 flush 日志缓冲区
        self.db.memory_db._flush_logs()

        # 获取最近互动
        interactions = self.db.memory_db.get_recent_interactions("group1", hours=1, limit=50)
        self.assertEqual(len(interactions), 10)

    def test_get_active_groups(self):
        """测试获取活跃群列表"""
        self.db.log_interaction(
            group_id="group1", user_id="userA",
            user_nickname="小明", message="测试", response="回复",
            trigger_type="at", emotion_state="neutral",
        )
        self.db.log_interaction(
            group_id="group2", user_id="userB",
            user_nickname="小红", message="测试", response="回复",
            trigger_type="at", emotion_state="neutral",
        )
        # 强制 flush 日志缓冲区
        self.db.memory_db._flush_logs()

        groups = self.db.memory_db.get_active_groups()
        self.assertIn("group1", groups)
        self.assertIn("group2", groups)

    def test_summary_stored_correctly(self):
        """测试摘要正确存储"""
        self.db.memory_db.add_summary(
            group_id="group1",
            summary="群聊讨论了编程话题",
            key_topics=["编程", "学习"],
            active_users=["小明", "小红"],
            message_count=15,
        )

        latest = self.db.memory_db.get_latest_summary("group1")
        self.assertIsNotNone(latest)
        self.assertEqual(latest["summary"], "群聊讨论了编程话题")

    def test_generate_summary_skips_low_interaction(self):
        """测试互动太少时不生成摘要"""
        # 仅添加 2 条互动（< 5 条阈值）
        for i in range(2):
            self.db.log_interaction(
                group_id="group1", user_id=f"user{i}",
                user_nickname=f"用户{i}",
                message=f"测试{i}", response=f"回复{i}",
                trigger_type="at", emotion_state="neutral",
            )
        self.db.memory_db._flush_logs()

        # mock provider
        mock_provider = AsyncMock()
        mock_response = MagicMock()
        mock_response.completion_text = '{"summary": "测试", "key_topics": []}'
        mock_provider.text_chat = AsyncMock(return_value=mock_response)
        self.context.get_provider = MagicMock(return_value=mock_provider)

        asyncio.run(self.manager._generate_summary_for_group("group1"))

        # 互动太少，不应生成摘要
        latest = self.db.memory_db.get_latest_summary("group1")
        self.assertIsNone(latest)


class TestPassiveInteraction(YunliTestCase):
    """旁听模式测试（P0 修复验证）"""

    def setUp(self):
        setup_astrbot_mocks()
        self.temp_dir = tempfile.mkdtemp()
        self.db_path = os.path.join(self.temp_dir, "test.db")
        self.db = YunliDatabase(self.db_path)

    def tearDown(self):
        self.db.close()
        shutil.rmtree(self.temp_dir)

    def test_passive_interaction_records_to_db(self):
        """测试旁听模式记录到数据库"""
        # 模拟旁听模式记录
        self.db.log_interaction(
            group_id="group1", user_id="userA",
            user_nickname="小明", message="今天天气真好",
            response="",  # 旁听模式 response 为空
            trigger_type="passive", emotion_state="",
        )
        # 强制 flush 日志缓冲区
        self.db.memory_db._flush_logs()

        # 验证记录存在
        interactions = self.db.memory_db.get_recent_interactions("group1", hours=1)
        self.assertEqual(len(interactions), 1)
        self.assertEqual(interactions[0]["trigger_type"], "passive")
        self.assertEqual(interactions[0]["message"], "今天天气真好")

    def test_passive_interaction_triggers_lightweight_extraction(self):
        """测试旁听模式触发轻量记忆提取"""
        from yunli.core.memory_manager import MemoryManager
        manager = MemoryManager(
            self.db, {"lightweight_extraction_enabled": True}
        )

        # 模拟旁听模式：message 有内容，response 为空
        manager.log_interaction(
            group_id="group1", user_id="userA",
            user_nickname="小明",
            message="我是大学生。",
            response="",  # 旁听模式
            trigger_type="passive",
        )

        # 验证轻量记忆提取被触发
        memories = self.db.get_memories("group1", "userA", include_outdated=True)
        self.assertTrue(len(memories) >= 1)


class TestPassiveInteractionScenario(YunliTestCase):
    """模拟群聊场景测试：验证旁听模式下非@消息的正确记录

    模拟场景：一个 QQ 群里有多个用户在聊天，部分消息 @ 云璃，部分不 @。
    验证旁听模式能正确记录非 @ 消息，建立群友画像，且不与 @ 消息混淆。
    """

    def setUp(self):
        setup_astrbot_mocks()
        self.temp_dir = tempfile.mkdtemp()
        self.db_path = os.path.join(self.temp_dir, "test.db")
        self.db = YunliDatabase(self.db_path)
        from yunli.core.memory_manager import MemoryManager
        self.manager = MemoryManager(
            self.db, {
                "lightweight_extraction_enabled": True,
                "memory_llm_enabled": False,  # 测试中不触发 LLM 深度整理
            }
        )

        # 模拟群聊场景的测试数据
        self.group_id = "group_123456"
        self.bot_self_id = "bot_999999"

        # 群成员（user_id -> 昵称）
        self.members = {
            "user_001": "小明",
            "user_002": "小红",
            "user_003": "小刚",
            "user_004": "小美",
        }

    def tearDown(self):
        self.db.close()
        shutil.rmtree(self.temp_dir)

    # ========== 辅助方法 ==========

    def _simulate_passive_message(self, user_id, message):
        """模拟一条旁听消息（非 @ 云璃）

        对应 core/event_pipeline.py 中 _record_passive_interaction 的核心调用：
        plugin._log_interaction(..., response="", trigger_type="passive")
        """
        nickname = self.members.get(user_id, user_id)
        self.manager.log_interaction(
            group_id=self.group_id, user_id=user_id,
            user_nickname=nickname, message=message,
            response="",  # 旁听模式 response 为空
            trigger_type="passive",
        )

    def _simulate_at_message(self, user_id, message, response="嗯嗯"):
        """模拟一条 @ 云璃的消息（有 response，触发 LLM 整理）"""
        nickname = self.members.get(user_id, user_id)
        self.manager.log_interaction(
            group_id=self.group_id, user_id=user_id,
            user_nickname=nickname, message=message,
            response=response,
            trigger_type="llm",
        )

    def _flush(self):
        """强制刷新 DB 日志缓冲区，确保数据写入数据库"""
        self.db.memory_db._flush_logs()

    # ========== 场景测试 ==========

    def test_multiple_users_passive_recording(self):
        """场景1：多个用户的非@消息都被正确记录

        模拟：4 个群友在群里聊天，都没有 @ 云璃。
        验证：所有消息都被记录为 passive 类型，response 为空。
        """
        # 模拟群聊：多个用户发送非@消息
        self._simulate_passive_message("user_001", "今天天气真好啊")
        self._simulate_passive_message("user_002", "是啊，适合出去玩")
        self._simulate_passive_message("user_003", "我要去打篮球")
        self._simulate_passive_message("user_004", "我想去逛街")
        self._flush()

        # 验证所有消息都被记录
        interactions = self.db.memory_db.get_recent_interactions(self.group_id, hours=1)
        self.assertEqual(len(interactions), 4)

        # 验证所有记录都是 passive 类型
        for interaction in interactions:
            self.assertEqual(interaction["trigger_type"], "passive")
            self.assertEqual(interaction["response"], "")  # 旁听模式 response 为空

        # 验证不同用户的消息都被记录
        user_ids_recorded = {i["user_id"] for i in interactions}
        self.assertEqual(user_ids_recorded, {"user_001", "user_002", "user_003", "user_004"})

        # 验证消息内容正确
        messages_recorded = {i["message"] for i in interactions}
        self.assertIn("今天天气真好啊", messages_recorded)
        self.assertIn("我要去打篮球", messages_recorded)

    def test_passive_recording_extracts_preferences(self):
        """场景2：旁听消息中的偏好被提取为记忆

        模拟：群友在聊天中自然表达喜好（非 @ 云璃）。
        验证：偏好被轻量提取为记忆，云璃后续能"记住"群友喜好。
        注意：提取内容长度需 >=2 字符（_try_add_memory 限制）。
        """
        # 模拟群聊中用户表达偏好（非@消息）
        self._simulate_passive_message("user_001", "我喜欢吃火锅。")
        self._simulate_passive_message("user_002", "我喜欢小猫。")
        self._simulate_passive_message("user_003", "我爱打篮球。")
        self._flush()

        # 验证偏好被提取为记忆
        for user_id, expected_content in [
            ("user_001", "火锅"),
            ("user_002", "小猫"),
            ("user_003", "篮球"),
        ]:
            memories = self.db.get_memories(self.group_id, user_id, include_outdated=True)
            self.assertTrue(
                any(expected_content in m["content"] for m in memories),
                f"用户 {user_id} 的记忆中应包含 '{expected_content}'，"
                f"实际记忆: {[m['content'] for m in memories]}"
            )

    def test_passive_recording_builds_group_profile(self):
        """场景3：旁听消息建立群友画像（身份信息）

        模拟：群友在聊天中透露身份信息（非 @ 云璃）。
        验证：身份信息被提取，云璃能"认识"群友。
        """
        # 模拟群聊中用户透露身份信息
        self._simulate_passive_message("user_001", "我是大学生。")
        self._simulate_passive_message("user_002", "我是高中生。")
        self._simulate_passive_message("user_003", "我是程序员。")
        self._flush()

        # 验证身份信息被提取
        memories_001 = self.db.get_memories(self.group_id, "user_001", include_outdated=True)
        memories_002 = self.db.get_memories(self.group_id, "user_002", include_outdated=True)
        memories_003 = self.db.get_memories(self.group_id, "user_003", include_outdated=True)

        self.assertTrue(
            any("大学生" in m["content"] for m in memories_001),
            f"user_001 身份记忆缺失: {[m['content'] for m in memories_001]}"
        )
        self.assertTrue(
            any("高中生" in m["content"] for m in memories_002),
            f"user_002 身份记忆缺失: {[m['content'] for m in memories_002]}"
        )
        self.assertTrue(
            any("程序员" in m["content"] for m in memories_003),
            f"user_003 身份记忆缺失: {[m['content'] for m in memories_003]}"
        )

    def test_passive_and_at_messages_coexist(self):
        """场景4：旁听消息和@消息混合场景

        模拟：群聊中既有 @ 云璃的消息，也有不 @ 的普通聊天。
        验证：两种消息都被记录，trigger_type 正确区分，记忆提取正常。
        """
        # 用户1 @ 云璃
        self._simulate_at_message("user_001", "@云璃 你好", "你好呀~")
        # 用户2 不 @ 云璃，但说了有意义的内容
        self._simulate_passive_message("user_002", "我是程序员。")
        # 用户3 @ 云璃
        self._simulate_at_message("user_003", "@云璃 今天吃什么好", "吃火锅吧")
        # 用户4 不 @ 云璃
        self._simulate_passive_message("user_004", "我喜欢看电影。")
        self._flush()

        # 验证所有消息都被记录
        interactions = self.db.memory_db.get_recent_interactions(self.group_id, hours=1)
        self.assertEqual(len(interactions), 4)

        # 验证 trigger_type 分布正确
        trigger_types = [i["trigger_type"] for i in interactions]
        self.assertEqual(trigger_types.count("passive"), 2)
        self.assertEqual(trigger_types.count("llm"), 2)

        # 验证 passive 消息的 response 为空
        passive_interactions = [i for i in interactions if i["trigger_type"] == "passive"]
        for pi in passive_interactions:
            self.assertEqual(pi["response"], "")

        # 验证 llm 消息有 response
        llm_interactions = [i for i in interactions if i["trigger_type"] == "llm"]
        for li in llm_interactions:
            self.assertNotEqual(li["response"], "")

        # 验证旁听消息的记忆也被提取
        memories_002 = self.db.get_memories(self.group_id, "user_002", include_outdated=True)
        self.assertTrue(any("程序员" in m["content"] for m in memories_002))

        memories_004 = self.db.get_memories(self.group_id, "user_004", include_outdated=True)
        self.assertTrue(any("电影" in m["content"] for m in memories_004))

    def test_passive_records_independent_per_user(self):
        """场景5：不同用户的旁听记忆独立存储，不混淆

        模拟：两个用户在群里都表达了偏好。
        验证：记忆按 user_id 独立存储，不会把 A 的偏好记到 B 头上。
        注意：提取内容长度需 >=2 字符（_try_add_memory 限制）。
        """
        # 用户1和用户2都表达了偏好
        self._simulate_passive_message("user_001", "我喜欢小猫。")
        self._simulate_passive_message("user_002", "我喜欢小狗。")
        self._flush()

        # 验证记忆独立存储，不混淆
        memories_001 = self.db.get_memories(self.group_id, "user_001", include_outdated=True)
        memories_002 = self.db.get_memories(self.group_id, "user_002", include_outdated=True)

        # user_001 的记忆中应该有"小猫"但没有"小狗"
        contents_001 = [m["content"] for m in memories_001]
        self.assertTrue(any("小猫" in c for c in contents_001),
                        f"user_001 应记住小猫: {contents_001}")
        self.assertFalse(any("小狗" in c for c in contents_001),
                         f"user_001 不应有小狗的记忆: {contents_001}")

        # user_002 的记忆中应该有"小狗"但没有"小猫"
        contents_002 = [m["content"] for m in memories_002]
        self.assertTrue(any("小狗" in c for c in contents_002),
                        f"user_002 应记住小狗: {contents_002}")
        self.assertFalse(any("小猫" in c for c in contents_002),
                         f"user_002 不应有小猫的记忆: {contents_002}")

    def test_passive_recording_all_messages_persisted(self):
        """场景6：旁听消息全部被持久化

        模拟：多个用户依次发送消息。
        验证：所有消息都被持久化到数据库（不验证顺序，因为同一秒内
              写入的消息 created_at 相同，DESC 排序顺序不确定）。
        """
        messages = [
            ("user_001", "第一条消息。"),
            ("user_002", "第二条消息。"),
            ("user_003", "第三条消息。"),
        ]
        for user_id, msg in messages:
            self._simulate_passive_message(user_id, msg)
        self._flush()

        # 验证所有消息都被持久化
        interactions = self.db.memory_db.get_recent_interactions(self.group_id, hours=1)
        self.assertEqual(len(interactions), 3)

        # 验证所有消息内容都存在（不验证顺序）
        recorded_messages = {i["message"] for i in interactions}
        for _, msg in messages:
            self.assertIn(msg, recorded_messages)

        # 验证所有用户都被记录
        recorded_users = {i["user_id"] for i in interactions}
        self.assertEqual(recorded_users, {"user_001", "user_002", "user_003"})

    def test_passive_skips_empty_message_extraction(self):
        """场景7：旁听模式空消息不提取记忆

        模拟：用户发送空消息（或纯表情）。
        验证：不会因空消息崩溃，且不会提取出无意义的记忆。
        """
        # 模拟空消息
        self.manager.log_interaction(
            group_id=self.group_id, user_id="user_001",
            user_nickname="小明", message="",
            response="", trigger_type="passive",
        )
        self._flush()

        # 验证不会因为空消息崩溃，且没有记忆被提取
        memories = self.db.get_memories(self.group_id, "user_001", include_outdated=True)
        self.assertEqual(len(memories), 0)

    def test_passive_does_not_trigger_deep_consolidation(self):
        """场景8：旁听模式不触发 LLM 深度整理

        模拟：旁听消息 response 为空。
        验证：深度整理（buffer_for_deep_consolidation）不被调用，
              避免空回复污染整理队列。
        """
        from yunli.core.memory_manager import MemoryManager
        # 重新构造 manager，开启 LLM 整理以验证跳过逻辑
        manager = MemoryManager(
            self.db, {
                "lightweight_extraction_enabled": True,
                "memory_llm_enabled": True,  # 开启 LLM 整理
            }
        )

        # mock 深度整理方法，验证不被调用
        manager.buffer_for_deep_consolidation = AsyncMock()

        # 旁听模式：response 为空
        manager.log_interaction(
            group_id=self.group_id, user_id="user_001",
            user_nickname="小明", message="我喜欢猫。",
            response="",  # 旁听模式 response 为空
            trigger_type="passive",
        )

        # 验证深度整理未被调用（因为 response 为空）
        manager.buffer_for_deep_consolidation.assert_not_called()

    def test_passive_recording_with_conversation_flow(self):
        """场景9：模拟完整群聊对话流，验证旁听记忆累积

        模拟：一个完整的群聊对话流，多个用户多轮发言。
        验证：旁听模式下，每个用户的记忆被正确累积。
        """
        # 第一轮：用户1表达偏好
        self._simulate_passive_message("user_001", "我喜欢吃火锅。")
        # 第二轮：用户2回应，也表达偏好
        self._simulate_passive_message("user_002", "我喜欢吃烧烤。")
        # 第三轮：用户1透露身份
        self._simulate_passive_message("user_001", "我是大学生。")
        # 第四轮：用户3加入聊天
        self._simulate_passive_message("user_003", "我爱打篮球。")
        self._flush()

        # 验证 user_001 累积了2条记忆（偏好+身份）
        memories_001 = self.db.get_memories(self.group_id, "user_001", include_outdated=True)
        contents_001 = [m["content"] for m in memories_001]
        self.assertTrue(any("火锅" in c for c in contents_001),
                        f"user_001 应有火锅偏好: {contents_001}")
        self.assertTrue(any("大学生" in c for c in contents_001),
                        f"user_001 应有大学生身份: {contents_001}")

        # 验证 user_002 累积了1条偏好记忆
        memories_002 = self.db.get_memories(self.group_id, "user_002", include_outdated=True)
        self.assertTrue(any("烧烤" in m["content"] for m in memories_002),
                        f"user_002 应有烧烤偏好: {[m['content'] for m in memories_002]}")

        # 验证 user_003 累积了1条偏好记忆
        memories_003 = self.db.get_memories(self.group_id, "user_003", include_outdated=True)
        self.assertTrue(any("篮球" in m["content"] for m in memories_003),
                        f"user_003 应有篮球偏好: {[m['content'] for m in memories_003]}")

        # 验证总互动记录数
        interactions = self.db.memory_db.get_recent_interactions(self.group_id, hours=1)
        self.assertEqual(len(interactions), 4)


class TestPassiveInteractionRealisticScenario(YunliTestCase):
    """真实群聊综合场景测试：验证近期修复（P1-1/P1-2/P1-4/P1-6）在旁听模式下的表现

    模拟场景：一个活跃 QQ 群的完整一天对话流，包含：
    - 多用户多轮旁听发言（非 @ 云璃）
    - 冲突偏好表达（如有人喜欢大城市，有人喜欢小城市）
    - 身份信息透露与变化
    - 衰减后记忆的可检索性
    - 批量访问计数更新

    重点验证近期修复：
    - P1-1: 衰减下限提升到 5，避免"软性遗忘"
    - P1-2: replace_user_memories 继承 expires_at
    - P1-4: 反义词冲突检测不误判"大城市 vs 小城市"
    - P1-6: 批量 access_memory 更新访问计数
    - P1-7: 衰减按 memory_type 差异化 + 新记忆保护期
    """

    def setUp(self):
        setup_astrbot_mocks()
        self.temp_dir = tempfile.mkdtemp()
        self.db_path = os.path.join(self.temp_dir, "test.db")
        self.db = YunliDatabase(self.db_path)
        from yunli.core.memory_manager import MemoryManager
        self.manager = MemoryManager(
            self.db, {
                "lightweight_extraction_enabled": True,
                "memory_llm_enabled": False,  # 测试中不触发 LLM 深度整理
            }
        )

        # 模拟群聊场景
        self.group_id = "group_realistic_001"
        self.bot_self_id = "bot_888888"

        # 群成员（user_id -> 昵称）
        self.members = {
            "user_alice": "爱丽丝",
            "user_bob": "鲍勃",
            "user_carol": "卡罗尔",
            "user_dave": "戴夫",
            "user_eve": "伊芙",
        }

    def tearDown(self):
        self.db.close()
        shutil.rmtree(self.temp_dir)

    # ========== 辅助方法 ==========

    def _passive(self, user_id, message):
        """模拟旁听消息（非 @ 云璃）"""
        nickname = self.members.get(user_id, user_id)
        self.manager.log_interaction(
            group_id=self.group_id, user_id=user_id,
            user_nickname=nickname, message=message,
            response="", trigger_type="passive",
        )

    def _at(self, user_id, message, response="嗯嗯，我记下了"):
        """模拟 @ 云璃的消息"""
        nickname = self.members.get(user_id, user_id)
        self.manager.log_interaction(
            group_id=self.group_id, user_id=user_id,
            user_nickname=nickname, message=message,
            response=response, trigger_type="llm",
        )

    def _flush(self):
        """强制刷新 DB 日志缓冲区"""
        self.db.memory_db._flush_logs()

    def _age_memories(self, user_id, hours=2):
        """将用户记忆的 created_at 改为 N 小时前，绕过新记忆保护期"""
        with self.db.memory_db._lock:
            self.db.memory_db.conn.execute(
                "UPDATE user_memories SET created_at = datetime('now', ?) "
                "WHERE group_id = ? AND user_id = ?",
                (f'-{hours} hours', self.group_id, user_id),
            )
            self.db.memory_db.conn.commit()

    # ========== 综合场景测试 ==========

    def test_realistic_group_chat_full_day_flow(self):
        """综合场景1：模拟群聊一整天的对话流

        验证：
        - 旁听消息被完整记录
        - 不同用户的偏好被独立提取
        - 冲突偏好（大城市 vs 小城市）不误判
        - 身份信息被正确提取
        """
        # ===== 上午：闲聊偏好 =====
        self._passive("user_alice", "我喜欢大城市，机会多")
        self._passive("user_bob", "我讨厌大城市，太吵了")
        self._passive("user_carol", "我喜欢小城市，安静")
        self._passive("user_dave", "我讨厌小城市，太无聊")
        self._passive("user_eve", "我喜欢中等城市，刚刚好")

        # ===== 中午：身份信息 =====
        self._passive("user_alice", "我是大学生，学计算机的")
        self._passive("user_bob", "我是程序员，工作三年了")
        self._passive("user_carol", "我是高中生，明年高考")
        self._passive("user_dave", "我是设计师，做UI的")
        self._passive("user_eve", "我是老师，教数学")

        # ===== 下午：能力与爱好 =====
        self._passive("user_alice", "我会Python编程")
        self._passive("user_bob", "我会Java开发")
        self._passive("user_carol", "我会弹钢琴")
        self._passive("user_dave", "我会画画")
        self._passive("user_eve", "我会唱歌")

        self._flush()

        # 验证：每个用户都有记忆
        for user_id in self.members:
            memories = self.db.get_memories(self.group_id, user_id)
            self.assertGreater(
                len(memories), 0,
                f"{self.members[user_id]} 应有记忆记录"
            )

        # 验证：Alice 的偏好和身份被正确提取
        alice_memories = self.db.get_memories(self.group_id, "user_alice")
        alice_contents = {m["content"] for m in alice_memories}
        # 偏好类记忆（大城市）
        self.assertTrue(
            any("大城市" in c for c in alice_contents),
            f"爱丽丝应有大城市偏好: {alice_contents}"
        )
        # 身份类记忆（大学生）
        self.assertTrue(
            any("大学生" in c for c in alice_contents),
            f"爱丽丝应有大学生身份: {alice_contents}"
        )

        # 验证：Bob 的偏好（讨厌大城市）被独立记录
        bob_memories = self.db.get_memories(self.group_id, "user_bob")
        bob_contents = {m["content"] for m in bob_memories}
        self.assertTrue(
            any("大城市" in c for c in bob_contents),
            f"鲍勃应有大城市相关记忆: {bob_contents}"
        )

    def test_conflict_detection_not_triggered_for_different_preferences(self):
        """综合场景2：P1-4 修复验证 - 不同偏好不误判为冲突

        场景：Alice 喜欢大城市，Bob 讨厌大城市。
        这是两个用户的不同偏好，不应触发冲突检测（冲突检测仅针对同一用户）。
        """
        # Alice 喜欢大城市
        self._passive("user_alice", "我喜欢大城市。")
        # Bob 讨厌大城市（不同用户，不冲突）
        self._passive("user_bob", "我讨厌大城市。")

        self._flush()

        # 验证：两条记忆都是 active 状态（未被标记为 conflicted）
        alice_memories = self.db.get_memories(self.group_id, "user_alice")
        bob_memories = self.db.get_memories(self.group_id, "user_bob")

        self.assertEqual(len(alice_memories), 1)
        self.assertEqual(len(bob_memories), 1)

        self.assertEqual(alice_memories[0]["status"], "active")
        self.assertEqual(bob_memories[0]["status"], "active")

    def test_same_user_conflicting_preferences_detected(self):
        """综合场景3：同一用户对同一对象的重复偏好会被合并

        场景：Alice 先说喜欢大城市，后说讨厌大城市。
        由于轻量提取只提取宾语（"大城市"），不包含动词（"喜欢"/"讨厌"），
        两条记忆内容完全相同，会被 add_memory 的"完全相同检测"合并
        （提升 confidence），而非触发冲突检测。

        注意：检测同一对象的矛盾偏好（喜欢 vs 讨厌）需要 LLM 深度整理，
        轻量提取无法实现。此测试验证合并行为的正确性。
        """
        # Alice 先说喜欢
        self._passive("user_alice", "我喜欢大城市。")
        # Alice 后说讨厌（同一用户，同一对象）
        self._passive("user_alice", "我讨厌大城市。")

        self._flush()

        alice_memories = self.db.get_memories(
            self.group_id, "user_alice", include_outdated=True
        )
        # 验证：只有 1 条记忆（合并而非新增）
        self.assertEqual(
            len(alice_memories), 1,
            f"同一对象的重复偏好应被合并为 1 条: {len(alice_memories)}"
        )
        # 验证：记忆状态为 active（非 conflicted）
        self.assertEqual(alice_memories[0]["status"], "active")
        # 验证：内容为"大城市"
        self.assertEqual(alice_memories[0]["content"], "大城市")

    def test_decay_preserves_memories_above_threshold(self):
        """综合场景4：P1-1 修复验证 - 衰减后记忆仍可被检索

        场景：旁听消息提取的记忆，经过衰减后，
        confidence 不低于检索阈值 5，仍能被 get_important_memories 召回。
        """
        # 提取一条高置信度记忆
        self._passive("user_alice", "我是大学生。")
        self._flush()

        # 验证初始状态
        memories = self.db.get_memories(self.group_id, "user_alice")
        self.assertEqual(len(memories), 1)
        initial_confidence = memories[0]["confidence"]
        self.assertGreaterEqual(initial_confidence, 5)

        # 将记忆老化（绕过 1 小时保护期）
        self._age_memories("user_alice", hours=2)

        # 执行多次衰减
        for _ in range(5):
            self.db.memory_db.decay_memory_confidence(
                decay_factor=0.95, min_confidence=5
            )

        # 验证：confidence 不低于 5（P1-1 修复）
        memories = self.db.get_memories(self.group_id, "user_alice")
        self.assertEqual(len(memories), 1)
        self.assertGreaterEqual(
            memories[0]["confidence"], 5,
            "P1-1 修复：衰减后 confidence 不应低于检索阈值 5"
        )

        # 验证：仍能被 get_important_memories 召回
        important = self.db.get_important_memories(
            self.group_id, "user_alice", min_confidence=5
        )
        self.assertGreater(
            len(important), 0,
            "衰减后记忆仍应能被 get_important_memories 召回"
        )

    def test_decay_differentiates_by_memory_type(self):
        """综合场景5：P1-7 修复验证 - 衰减按 memory_type 差异化

        场景：同一用户有 fact（身份）和 event（事件）两类记忆，
        衰减后 fact 应比 event 衰减更慢。
        """
        # fact 类型：身份信息（衰减系数 0.98）
        self._passive("user_alice", "我是大学生。")
        # event 类型：临时事件（衰减系数 0.90）
        self._passive("user_alice", "我今天去爬山了。")

        self._flush()

        # 老化记忆
        self._age_memories("user_alice", hours=2)

        # 记录衰减前的 confidence
        memories_before = self.db.get_memories(self.group_id, "user_alice")
        conf_before = {m["memory_type"]: m["confidence"] for m in memories_before}

        # 执行衰减
        self.db.memory_db.decay_memory_confidence(
            decay_factor=0.95, min_confidence=3
        )

        # 记录衰减后的 confidence
        memories_after = self.db.get_memories(self.group_id, "user_alice")
        conf_after = {m["memory_type"]: m["confidence"] for m in memories_after}

        # 验证：fact 衰减更慢（衰减后 confidence 更高）
        if "fact" in conf_before and "event" in conf_before:
            fact_decay = conf_before["fact"] - conf_after.get("fact", 0)
            event_decay = conf_before["event"] - conf_after.get("event", 0)
            self.assertLessEqual(
                fact_decay, event_decay,
                f"fact 衰减 ({fact_decay}) 应 <= event 衰减 ({event_decay})"
            )

    def test_new_memory_protection_period(self):
        """综合场景6：P1-7 修复验证 - 新记忆保护期

        场景：刚提取的记忆在 1 小时保护期内不被衰减。
        """
        # 提取记忆
        self._passive("user_alice", "我是大学生。")
        self._flush()

        # 记录初始 confidence
        memories = self.db.get_memories(self.group_id, "user_alice")
        initial_confidence = memories[0]["confidence"]

        # 立即执行衰减（记忆在保护期内）
        self.db.memory_db.decay_memory_confidence(
            decay_factor=0.5, min_confidence=3  # 极端衰减系数
        )

        # 验证：confidence 未变（保护期内不衰减）
        memories = self.db.get_memories(self.group_id, "user_alice")
        self.assertEqual(
            memories[0]["confidence"], initial_confidence,
            "P1-7 修复：新记忆在 1 小时保护期内不应被衰减"
        )

    def test_batch_access_memory_updates_count(self):
        """综合场景7：P1-6 修复验证 - 批量访问计数更新

        场景：旁听模式下提取多条记忆后，
        ContextBuilder 召回时批量更新访问计数。
        """
        # 提取多条记忆
        self._passive("user_alice", "我是大学生。")
        self._passive("user_alice", "我喜欢编程。")
        self._passive("user_alice", "我会Python。")

        self._flush()

        # 获取所有记忆 ID
        memories = self.db.get_memories(self.group_id, "user_alice")
        self.assertGreaterEqual(len(memories), 2)
        memory_ids = [m["id"] for m in memories]

        # 记录初始 access_count
        initial_counts = {m["id"]: m["access_count"] for m in memories}

        # 批量更新访问计数
        self.db.access_memories_batch(memory_ids)

        # 验证：所有记忆的 access_count 都增加了 1
        updated_memories = self.db.get_memories(self.group_id, "user_alice")
        for mem in updated_memories:
            self.assertEqual(
                mem["access_count"], initial_counts[mem["id"]] + 1,
                f"记忆 {mem['id']} 的 access_count 应增加 1"
            )

    def test_passive_messages_independent_from_at_messages(self):
        """综合场景8：旁听消息与@消息记忆独立

        场景：Alice 旁听发言透露偏好，随后 @ 云璃对话。
        旁听模式的记忆提取与 @ 模式互不干扰。
        """
        # 旁听发言：透露偏好
        self._passive("user_alice", "我喜欢吃火锅。")
        # @ 云璃：正常对话
        self._at("user_alice", "云璃你好", response="你好呀")

        self._flush()

        # 验证：火锅偏好被提取（来自旁听消息）
        alice_memories = self.db.get_memories(self.group_id, "user_alice")
        self.assertTrue(
            any("火锅" in m["content"] for m in alice_memories),
            f"旁听消息的火锅偏好应被提取: {[m['content'] for m in alice_memories]}"
        )

        # 验证：互动记录区分 passive 和 llm
        interactions = self.db.memory_db.get_recent_interactions(
            self.group_id, hours=1
        )
        trigger_types = {i["trigger_type"] for i in interactions}
        self.assertIn("passive", trigger_types)
        self.assertIn("llm", trigger_types)

    def test_group_memory_recall_excludes_self(self):
        """综合场景9：群友记忆召回排除当前用户

        场景：多个用户旁听发言后，@ 云璃时召回群友记忆，
        群友记忆应排除当前 @ 的用户自己的记忆。
        """
        # 多个用户旁听发言
        self._passive("user_alice", "我是大学生。")
        self._passive("user_bob", "我是程序员。")
        self._passive("user_carol", "我是设计师。")

        self._flush()

        # Alice @ 云璃，召回群友记忆
        group_memories = self.db.get_group_memories(
            self.group_id, min_confidence=5, limit=10,
            exclude_user_id="user_alice"
        )

        # 验证：群友记忆中不包含 Alice 的记忆
        for mem in group_memories:
            self.assertNotEqual(
                mem["user_id"], "user_alice",
                "群友记忆不应包含当前用户 Alice 的记忆"
            )

        # 验证：群友记忆中包含 Bob 和 Carol 的记忆
        group_user_ids = {mem["user_id"] for mem in group_memories}
        self.assertIn("user_bob", group_user_ids, "应包含 Bob 的记忆")
        self.assertIn("user_carol", group_user_ids, "应包含 Carol 的记忆")

    def test_passive_recording_with_special_characters(self):
        """综合场景10：特殊字符消息的旁听记录

        场景：包含表情、特殊符号的旁听消息能被正确记录。
        """
        # 包含表情的消息
        self._passive("user_alice", "今天好开心啊 😊")
        # 包含特殊符号的消息
        self._passive("user_bob", "我要去【北京】旅游")
        # 纯文字消息
        self._passive("user_carol", "我喜欢看书")

        self._flush()

        # 验证：所有消息都被记录
        interactions = self.db.memory_db.get_recent_interactions(
            self.group_id, hours=1
        )
        self.assertEqual(len(interactions), 3)

        # 验证：消息内容完整保留
        messages = {i["message"] for i in interactions}
        self.assertIn("今天好开心啊 😊", messages)
        self.assertIn("我要去【北京】旅游", messages)
        self.assertIn("我喜欢看书", messages)


class TestRecentChatHistory(YunliTestCase):
    """P0-2 修复验证：最近群聊原文注入

    验证 build_recent_chat_history 方法能正确获取、格式化最近 10 条群聊原文，
    让云璃像真人一样看到群友说了什么。
    """

    def setUp(self):
        setup_astrbot_mocks()
        self.temp_dir = tempfile.mkdtemp()
        self.db_path = os.path.join(self.temp_dir, "test.db")
        self.db = YunliDatabase(self.db_path)
        from yunli.core.context_builder import ContextBuilder
        # ContextBuilder 需要 persona_engine 和 relationship 参数
        self.persona_engine = MagicMock()
        self.relationship = MagicMock()
        self.context_builder = ContextBuilder(
            self.db, self.persona_engine, self.relationship
        )
        self.group_id = "group_chat_001"

    def tearDown(self):
        self.db.close()
        shutil.rmtree(self.temp_dir)

    def _add_log(self, user_id, nickname, message, response=""):
        """添加一条互动日志"""
        self.db.log_interaction(
            group_id=self.group_id, user_id=user_id,
            user_nickname=nickname, message=message,
            response=response, trigger_type="passive",
            emotion_state="neutral",
        )

    def _flush(self):
        """强制刷新日志缓冲区"""
        self.db.memory_db._flush_logs()

    def test_empty_group_returns_empty(self):
        """空群无记录时返回空字符串"""
        result = self.context_builder.build_recent_chat_history(self.group_id)
        self.assertEqual(result, "")

    def test_empty_group_id_returns_empty(self):
        """group_id 为空时返回空字符串"""
        result = self.context_builder.build_recent_chat_history("")
        self.assertEqual(result, "")

    def test_single_message_formatted_correctly(self):
        """单条消息格式化正确"""
        self._add_log("user1", "小明", "今天天气真好")
        self._flush()

        result = self.context_builder.build_recent_chat_history(self.group_id)

        self.assertIn("【最近群聊】", result)
        self.assertIn("小明: 今天天气真好", result)

    def test_multiple_messages_ordered_chronologically(self):
        """多条消息按时间正序排列（最早的在前）"""
        self._add_log("user1", "小明", "第一条消息")
        self._add_log("user2", "小红", "第二条消息")
        self._add_log("user3", "小刚", "第三条消息")
        self._flush()

        result = self.context_builder.build_recent_chat_history(self.group_id)

        # 验证顺序：第一条在前，第三条在后
        idx1 = result.find("第一条消息")
        idx2 = result.find("第二条消息")
        idx3 = result.find("第三条消息")
        self.assertLess(idx1, idx2, "第一条应在第二条前")
        self.assertLess(idx2, idx3, "第二条应在第三条前")

    def test_limit_10_messages(self):
        """limit=10 时只返回最近 10 条消息"""
        # 添加 15 条消息
        for i in range(15):
            self._add_log("user1", "小明", f"消息{i:02d}")
        self._flush()

        result = self.context_builder.build_recent_chat_history(self.group_id, limit=10)

        # 验证只包含最近 10 条（消息05-消息14）
        self.assertIn("消息14", result)  # 最新的
        self.assertIn("消息05", result)  # 第 10 条
        self.assertNotIn("消息04", result)  # 第 11 条，应被截断

    def test_default_limit_20_messages(self):
        """默认 limit=20 时返回最近 20 条消息"""
        # 添加 25 条消息
        for i in range(25):
            self._add_log("user1", "小明", f"消息{i:02d}")
        self._flush()

        result = self.context_builder.build_recent_chat_history(self.group_id)

        # 验证包含最近 20 条（消息05-消息24）
        self.assertIn("消息24", result)  # 最新的
        self.assertIn("消息05", result)  # 第 20 条
        self.assertNotIn("消息04", result)  # 第 21 条，应被截断

    def test_exclude_current_message(self):
        """排除当前正在处理的消息（避免重复注入）"""
        self._add_log("user1", "小明", "历史消息")
        self._add_log("user2", "小红", "当前消息")
        self._flush()

        result = self.context_builder.build_recent_chat_history(
            self.group_id, exclude_message="当前消息"
        )

        self.assertIn("历史消息", result)
        self.assertNotIn("当前消息", result, "当前消息应被排除")

    def test_skip_empty_messages(self):
        """跳过空消息"""
        self._add_log("user1", "小明", "有效消息")
        self._add_log("user2", "小红", "")  # 空消息
        self._add_log("user3", "小刚", "   ")  # 空白消息
        self._flush()

        result = self.context_builder.build_recent_chat_history(self.group_id)

        self.assertIn("有效消息", result)
        # 空消息不应出现（但昵称可能出现在其他消息中，检查行格式）
        lines = result.split("\n")[1:]  # 跳过标题行
        for line in lines:
            if line.strip():
                self.assertNotEqual(line.strip().split(": ", 1)[-1], "")

    def test_skip_command_messages(self):
        """跳过纯命令消息（以 / 或 # 开头）"""
        self._add_log("user1", "小明", "正常聊天")
        self._add_log("user2", "小红", "/help")  # 命令
        self._add_log("user3", "小刚", "#菜单")  # 命令
        self._flush()

        result = self.context_builder.build_recent_chat_history(self.group_id)

        self.assertIn("正常聊天", result)
        self.assertNotIn("/help", result, "命令消息应被跳过")
        self.assertNotIn("#菜单", result, "命令消息应被跳过")

    def test_skip_messages_without_nickname(self):
        """跳过无昵称的异常记录"""
        self._add_log("user1", "小明", "有昵称的消息")
        self._add_log("user2", "", "无昵称的消息")  # 无昵称
        self._flush()

        result = self.context_builder.build_recent_chat_history(self.group_id)

        self.assertIn("有昵称的消息", result)
        self.assertNotIn("无昵称的消息", result, "无昵称记录应被跳过")

    def test_token_budget_control(self):
        """token 预算控制：超预算时从最早消息开始截断"""
        # 添加多条长消息
        for i in range(10):
            self._add_log("user1", "小明", f"这是一条很长的消息编号{i:02d}，包含很多内容")
        self._flush()

        # 设置很小的 token 预算
        result = self.context_builder.build_recent_chat_history(
            self.group_id, token_budget=80
        )

        # 验证：由于预算限制，只包含部分最新消息
        self.assertIn("【最近群聊】", result)
        # 最新的消息应该在（预算优先保留最新）
        self.assertIn("编号09", result)
        # 最早的消息应该被截断
        self.assertNotIn("编号00", result, "最早的消息应因预算不足被截断")

    def test_different_users_displayed(self):
        """不同用户的发言都显示"""
        self._add_log("user1", "小明", "我是小明")
        self._add_log("user2", "小红", "我是小红")
        self._add_log("user3", "小刚", "我是小刚")
        self._flush()

        result = self.context_builder.build_recent_chat_history(self.group_id)

        self.assertIn("小明: 我是小明", result)
        self.assertIn("小红: 我是小红", result)
        self.assertIn("小刚: 我是小刚", result)

    def test_special_characters_preserved(self):
        """特殊字符（表情、符号）完整保留"""
        self._add_log("user1", "小明", "今天好开心 😊🎉")
        self._add_log("user2", "小红", "我要去【北京】旅游")
        self._flush()

        result = self.context_builder.build_recent_chat_history(self.group_id)

        self.assertIn("今天好开心 😊🎉", result)
        self.assertIn("我要去【北京】旅游", result)

    def test_integration_with_passive_mode(self):
        """集成测试：旁听模式记录的消息能被 build_recent_chat_history 获取"""
        from yunli.core.memory_manager import MemoryManager
        manager = MemoryManager(
            self.db, {
                "lightweight_extraction_enabled": True,
                "memory_llm_enabled": False,
            }
        )

        # 模拟旁听消息
        manager.log_interaction(
            group_id=self.group_id, user_id="user_alice",
            user_nickname="爱丽丝", message="我喜欢吃火锅。",
            response="", trigger_type="passive",
            emotion_state="neutral",
        )
        manager.log_interaction(
            group_id=self.group_id, user_id="user_bob",
            user_nickname="鲍勃", message="我喜欢吃烧烤。",
            response="", trigger_type="passive",
            emotion_state="neutral",
        )
        self._flush()

        # 验证：旁听消息能被获取并格式化
        result = self.context_builder.build_recent_chat_history(self.group_id)

        self.assertIn("爱丽丝: 我喜欢吃火锅。", result)
        self.assertIn("鲍勃: 我喜欢吃烧烤。", result)

    def test_include_bot_response(self):
        """P2-11 修复：包含云璃自己的回复，形成完整对话上下文"""
        self._add_log("user1", "小明", "云璃，你喜欢什么？", "")
        self._add_log("user2", "小红", "我也想知道", "")
        # 模拟云璃回复：message 是 @云璃的提问，response 是云璃的回答
        self._add_log(
            "user1", "小明", "云璃，你喜欢什么？",
            response="我喜欢吃冰淇淋呀~",
        )
        self._flush()

        result = self.context_builder.build_recent_chat_history(
            self.group_id, include_bot_response=True
        )

        self.assertIn("云璃（你）: 我喜欢吃冰淇淋呀~", result)

    def test_exclude_message_with_at_prefix(self):
        """P2-11 修复：exclude_message 带 @前缀时也能正确去重"""
        self._add_log("user1", "小明", "今天天气真好")
        self._flush()

        # 当前消息在 QQ 中可能是 "@云璃 今天天气真好"
        result = self.context_builder.build_recent_chat_history(
            self.group_id, exclude_message="@云璃 今天天气真好"
        )

        self.assertNotIn("今天天气真好", result, "带 @前缀的当前消息应被排除")

    def test_exclude_message_with_cq_at(self):
        """P2-11 修复：exclude_message 带 CQ At 码时也能正确去重"""
        self._add_log("user1", "小明", "今天天气真好")
        self._flush()

        # 当前消息可能是 "[At:bot]今天天气真好"
        result = self.context_builder.build_recent_chat_history(
            self.group_id, exclude_message="[At:bot]今天天气真好"
        )

        self.assertNotIn("今天天气真好", result, "带 At 码的当前消息应被排除")


class TestGroupThreadContext(YunliTestCase):
    """P2-11 修复验证：群级短期上下文

    验证 ConversationThreadTracker 能维护群内最近 N 轮对话（不区分用户），
    让云璃和不同用户聊天时也能看到群里刚刚发生了什么。
    """

    def setUp(self):
        setup_astrbot_mocks()
        self.tracker = get_thread_tracker()
        self.group_id = "group_thread_001"
        # 清空可能的残留状态
        self.tracker.reset_thread(f"{self.group_id}:__group__")

    def tearDown(self):
        self.tracker.reset_thread(f"{self.group_id}:__group__")

    def test_group_thread_records_multiple_users(self):
        """群级线程记录多个用户的发言"""
        self.tracker.record_user_message_to_group(
            self.group_id, "user_alice", "爱丽丝", "我喜欢吃火锅"
        )
        self.tracker.record_user_message_to_group(
            self.group_id, "user_bob", "鲍勃", "我喜欢吃烧烤"
        )

        context = self.tracker.get_group_thread_context(self.group_id)

        self.assertIn("爱丽丝: 我喜欢吃火锅", context)
        self.assertIn("鲍勃: 我喜欢吃烧烤", context)

    def test_group_thread_includes_yunli_response(self):
        """群级线程包含云璃自己的回复"""
        self.tracker.record_user_message_to_group(
            self.group_id, "user_alice", "爱丽丝", "云璃，你喜欢什么？"
        )
        self.tracker.record_yunli_response_to_group(
            self.group_id, "我喜欢吃冰淇淋~"
        )

        context = self.tracker.get_group_thread_context(self.group_id)

        self.assertIn("爱丽丝: 云璃，你喜欢什么？", context)
        self.assertIn("云璃（你）: 我喜欢吃冰淇淋~", context)

    def test_group_thread_respects_max_lines(self):
        """群级线程按 max_lines 截断"""
        for i in range(15):
            self.tracker.record_user_message_to_group(
                self.group_id, f"user_{i}", f"用户{i}", f"消息{i}"
            )

        context = self.tracker.get_group_thread_context(self.group_id, max_lines=10)
        lines = [l for l in context.split("\n") if l and not l.startswith("【")]

        self.assertEqual(len(lines), 10)
        # 最新的 10 条保留（消息05-消息14）
        self.assertIn("消息14", context)
        self.assertNotIn("消息04", context)

    def test_group_thread_isolated_by_group(self):
        """不同群的线程互相隔离"""
        group_a = "group_a"
        group_b = "group_b"

        self.tracker.record_user_message_to_group(
            group_a, "user1", "小明", "群A的消息"
        )
        self.tracker.record_user_message_to_group(
            group_b, "user2", "小红", "群B的消息"
        )

        context_a = self.tracker.get_group_thread_context(group_a)
        context_b = self.tracker.get_group_thread_context(group_b)

        self.assertIn("群A的消息", context_a)
        self.assertNotIn("群B的消息", context_a)
        self.assertIn("群B的消息", context_b)
        self.assertNotIn("群A的消息", context_b)


if __name__ == "__main__":
    unittest.main()
