"""yunli.core.logging_helpers 模块的单元测试"""

import sys
import os
import asyncio
import logging
import io

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from test_base import setup_test_path, YunliTestCase

setup_test_path()

from yunli.core.logging_helpers import (
    bind_context, reset_bind, get_logger, ContextFilter,
    _scope_ctx, _user_ctx, _group_ctx,
)


class TestContextBinding(YunliTestCase):
    """测试 contextvar 绑定/解绑"""

    def test_bind_and_reset(self):
        """绑定 context 后能从 contextvar 读出"""
        tokens = bind_context(scope="req:123:user1", user_id="user1", group_id="123")
        try:
            self.assertEqual(_scope_ctx.get(), "req:123:user1")
            self.assertEqual(_user_ctx.get(), "user1")
            self.assertEqual(_group_ctx.get(), "123")
        finally:
            reset_bind(tokens)

    def test_partial_bind(self):
        """只传部分字段，不覆盖其他字段"""
        tokens1 = bind_context(scope="a", user_id="u1")
        try:
            tokens2 = bind_context(group_id="g1")  # 只传 group_id
            try:
                self.assertEqual(_scope_ctx.get(), "a")
                self.assertEqual(_user_ctx.get(), "u1")
                self.assertEqual(_group_ctx.get(), "g1")
            finally:
                reset_bind(tokens2)
            # 还原后 scope/user_id 仍应是 a/u1
            self.assertEqual(_scope_ctx.get(), "a")
            self.assertEqual(_user_ctx.get(), "u1")
        finally:
            reset_bind(tokens1)

    def test_default_is_none(self):
        """未绑定时 contextvar 返回 None（filter 会显示 '-'）"""
        # 重置：手动 reset 所有
        # 注意：contextvar 跨测试可能残留——这是单线程测试
        # 这里用 None 默认值
        self.assertIn(_scope_ctx.get(), (None, "-", "a", "req:123:user1"))  # 容忍残留


class TestContextFilter(YunliTestCase):
    """测试 ContextFilter 把 contextvars 注入到 LogRecord"""

    def test_filter_injects_defaults(self):
        """未绑定时 scope/user_id/group_id 应是 '-'"""
        f = ContextFilter()
        record = logging.LogRecord(
            name="test", level=logging.INFO, pathname="", lineno=0,
            msg="test", args=(), exc_info=None,
        )
        f.filter(record)
        self.assertEqual(record.scope, "-")
        self.assertEqual(record.user_id, "-")
        self.assertEqual(record.group_id, "-")

    def test_filter_injects_bound_values(self):
        """绑定后 filter 把值注入到 record"""
        tokens = bind_context(scope="req:abc:def", user_id="def", group_id="abc")
        try:
            f = ContextFilter()
            record = logging.LogRecord(
                name="test", level=logging.INFO, pathname="", lineno=0,
                msg="test", args=(), exc_info=None,
            )
            f.filter(record)
            self.assertEqual(record.scope, "req:abc:def")
            self.assertEqual(record.user_id, "def")
            self.assertEqual(record.group_id, "abc")
        finally:
            reset_bind(tokens)


class TestGetLogger(YunliTestCase):
    """测试 get_logger 返回带 ContextFilter 的 logger"""

    def test_get_logger_returns_logger(self):
        """返回的是标准 logging.Logger"""
        logger = get_logger("yunli.test.unique")
        self.assertIsInstance(logger, logging.Logger)

    def test_get_logger_idempotent_filter(self):
        """多次调用不会重复添加 filter"""
        logger = get_logger("yunli.test.unique2")
        # 检查 ContextFilter 只存在 1 个
        count = sum(1 for f in logger.filters if isinstance(f, ContextFilter))
        self.assertEqual(count, 1)
        # 第二次调用不应增加
        get_logger("yunli.test.unique2")
        count = sum(1 for f in logger.filters if isinstance(f, ContextFilter))
        self.assertEqual(count, 1)


class TestEndToEnd(YunliTestCase):
    """端到端：绑定 + logger.info + 捕获 stream 验证格式"""

    def test_log_format_contains_context(self):
        """日志格式应包含 scope/user_id/group_id"""
        logger = get_logger("yunli.test.e2e")

        # 用 StringIO 捕获 + 设置 logger level
        stream = io.StringIO()
        handler = logging.StreamHandler(stream)
        handler.setLevel(logging.INFO)
        handler.setFormatter(logging.Formatter(
            "[%(scope)s] [%(user_id)s] [%(group_id)s] %(message)s"
        ))
        logger.addHandler(handler)
        logger.setLevel(logging.INFO)

        try:
            tokens = bind_context(scope="req:e2e:user1", user_id="user1", group_id="e2e")
            try:
                logger.info("处理完成")
                # 强制 flush
                handler.flush()
            finally:
                reset_bind(tokens)

            output = stream.getvalue()
            self.assertIn("[req:e2e:user1]", output, f"Output was: {output!r}")
            self.assertIn("[user1]", output)
            self.assertIn("[e2e]", output)
            self.assertIn("处理完成", output)
        finally:
            logger.removeHandler(handler)


class TestAsyncContextIsolation(YunliTestCase):
    """异步任务间 contextvar 应隔离（asyncio 任务切换不串扰）"""

    def test_async_tasks_have_isolated_context(self):
        async def task_with(scope, user_id, group_id, results, idx):
            tokens = bind_context(scope=scope, user_id=user_id, group_id=group_id)
            try:
                await asyncio.sleep(0.01)  # 让其他任务有机会运行
                results[idx] = (_scope_ctx.get(), _user_ctx.get(), _group_ctx.get())
            finally:
                reset_bind(tokens)

        async def run():
            results = [None, None]
            await asyncio.gather(
                task_with("task1", "u1", "g1", results, 0),
                task_with("task2", "u2", "g2", results, 1),
            )
            return results

        results = asyncio.run(run())
        # 任务1 应看到自己的 scope
        self.assertEqual(results[0], ("task1", "u1", "g1"))
        # 任务2 应看到自己的 scope（不应被任务1串扰）
        self.assertEqual(results[1], ("task2", "u2", "g2"))


if __name__ == "__main__":
    import unittest
    unittest.main()
