"""yunli.core.metrics 模块的单元测试"""

import sys
import os
import time
import json
import threading

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from test_base import setup_test_path, YunliTestCase

setup_test_path()

from yunli.core.metrics import Metrics


class TestCounter(YunliTestCase):
    """Counter: 基础计数"""

    def test_increment_default(self):
        """increment 默认 +1"""
        m = Metrics("test")
        m.increment("foo")
        m.increment("foo")
        m.increment("foo")
        self.assertEqual(m.snapshot()["counters"]["foo"], 3)

    def test_increment_with_value(self):
        """increment 自定义 value"""
        m = Metrics("test")
        m.increment("bar", value=5)
        m.increment("bar", value=10)
        self.assertEqual(m.snapshot()["counters"]["bar"], 15)

    def test_increment_with_tag(self):
        """tag 区分同一指标的细分计数"""
        m = Metrics("test")
        m.increment("evt", tag="success")
        m.increment("evt", tag="success")
        m.increment("evt", tag="failure")
        snap = m.snapshot()
        self.assertEqual(snap["counters"]["evt"], 3)
        self.assertEqual(snap["counters_by_tag"]["evt"]["success"], 2)
        self.assertEqual(snap["counters_by_tag"]["evt"]["failure"], 1)

    def test_increment_rejects_negative(self):
        """负 value 应抛 ValueError"""
        m = Metrics("test")
        with self.assertRaises(ValueError):
            m.increment("foo", value=-1)


class TestTimer(YunliTestCase):
    """Timer: 耗时统计"""

    def test_timing_records_samples(self):
        """timing() 记录样本"""
        m = Metrics("test")
        m.timing("op", 10.5)
        m.timing("op", 20.3)
        m.timing("op", 30.1)
        stats = m.snapshot()["timings"]["op"]
        self.assertEqual(stats["count"], 3)
        self.assertAlmostEqual(stats["min_ms"], 10.5, places=1)
        self.assertAlmostEqual(stats["max_ms"], 30.1, places=1)
        self.assertAlmostEqual(stats["mean_ms"], 20.3, places=1)

    def test_timing_ring_buffer(self):
        """超出 window 自动环形覆盖"""
        m = Metrics("test", timing_window=3)
        m.timing("op", 1.0)
        m.timing("op", 2.0)
        m.timing("op", 3.0)
        m.timing("op", 4.0)  # 应丢弃 1.0
        stats = m.snapshot()["timings"]["op"]
        self.assertEqual(stats["count"], 3)
        self.assertEqual(stats["min_ms"], 2.0)


class TestGauge(YunliTestCase):
    """Gauge: 瞬时值"""

    def test_gauge_set_and_update(self):
        """gauge 覆盖式更新"""
        m = Metrics("test")
        m.gauge("queue_length", 10)
        self.assertEqual(m.snapshot()["gauges"]["queue_length"], 10.0)
        m.gauge("queue_length", 5)
        self.assertEqual(m.snapshot()["gauges"]["queue_length"], 5.0)


class TestMeasureContextManager(YunliTestCase):
    """measure() 上下文管理器"""

    def test_measure_records_duration(self):
        """with measure 应自动记录耗时"""
        m = Metrics("test")
        with m.measure("slow_op"):
            time.sleep(0.05)  # ~50ms，避免在慢机器上 timing 抖动误判
        stats = m.snapshot()["timings"]["slow_op"]
        self.assertEqual(stats["count"], 1)
        # Windows + CI 环境 time.sleep(0.05) 可能波动在 30-200ms
        self.assertGreater(stats["min_ms"], 20.0)

    def test_measure_with_tag_increments_counter(self):
        """传 tag 应同时 increment counter"""
        m = Metrics("test")
        with m.measure("api_call", tag="success"):
            pass
        snap = m.snapshot()
        # timing 记录 + counter
        self.assertIn("api_call", snap["timings"])
        self.assertEqual(snap["counters_by_tag"]["api_call_count"]["success"], 1)

    def test_measure_records_on_exception(self):
        """即使抛异常也应记录耗时"""
        m = Metrics("test")
        try:
            with m.measure("fail_op"):
                raise ValueError("test")
        except ValueError:
            pass
        # 仍应记录
        self.assertIn("fail_op", m.snapshot()["timings"])


class TestSnapshot(YunliTestCase):
    """snapshot() 输出格式"""

    def test_snapshot_serializable(self):
        """snapshot 应可直接 json.dumps"""
        m = Metrics("test")
        m.increment("a", tag="x")
        m.timing("b", 10.0)
        m.gauge("c", 42.0)
        snap = m.snapshot()
        # 必须能 JSON 化
        s = json.dumps(snap)
        loaded = json.loads(s)
        self.assertEqual(loaded["name"], "test")
        self.assertEqual(loaded["counters"]["a"], 1)
        self.assertEqual(loaded["counters_by_tag"]["a"]["x"], 1)
        self.assertEqual(loaded["timings"]["b"]["count"], 1)
        self.assertEqual(loaded["gauges"]["c"], 42.0)

    def test_snapshot_empty(self):
        """空 metrics 也应能 snapshot"""
        m = Metrics("empty")
        snap = m.snapshot()
        self.assertEqual(snap["counters"], {})
        self.assertEqual(snap["timings"], {})
        self.assertEqual(snap["gauges"], {})


class TestFormatSummary(YunliTestCase):
    """format_summary() 字符串输出"""

    def test_format_summary_contains_sections(self):
        """摘要应包含 Counters/Timings/Gauges 章节"""
        m = Metrics("test_plugin")
        m.increment("foo", tag="success")
        m.timing("bar", 10.0)
        m.gauge("queue", 5)
        summary = m.format_summary()
        self.assertIn("[Metrics:test_plugin]", summary)
        self.assertIn("Counters:", summary)
        self.assertIn("foo: 1", summary)
        self.assertIn("Timings (ms):", summary)
        self.assertIn("bar:", summary)
        self.assertIn("Gauges:", summary)
        self.assertIn("queue: 5", summary)

    def test_format_summary_empty(self):
        """空 metrics 也应输出标题"""
        m = Metrics("empty")
        summary = m.format_summary()
        self.assertIn("[Metrics:empty]", summary)


class TestThreadSafety(YunliTestCase):
    """线程安全验证"""

    def test_concurrent_increment(self):
        """1000 次并发 increment 不应丢数"""
        m = Metrics("test")
        n_threads = 10
        n_per_thread = 1000

        def worker():
            for _ in range(n_per_thread):
                m.increment("counter")

        threads = [threading.Thread(target=worker) for _ in range(n_threads)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        # 应等于 n_threads * n_per_thread
        self.assertEqual(m.snapshot()["counters"]["counter"], n_threads * n_per_thread)


class TestReset(YunliTestCase):
    """reset() 清空所有指标"""

    def test_reset_clears_all(self):
        """reset 应清空 counters / timings / gauges"""
        m = Metrics("test")
        m.increment("a")
        m.timing("b", 10.0)
        m.gauge("c", 5.0)
        m.reset()
        snap = m.snapshot()
        self.assertEqual(snap["counters"], {})
        self.assertEqual(snap["timings"], {})
        self.assertEqual(snap["gauges"], {})


class TestLogSummary(YunliTestCase):
    """log_summary() 输出到 logger"""

    def test_log_summary_calls_logger(self):
        """log_summary 应调用 logger.info"""
        import logging
        from unittest.mock import MagicMock
        m = Metrics("test")
        m.increment("foo", tag="success")
        mock_logger = MagicMock()
        m.log_summary(mock_logger)
        # 应至少调用 1 次 logger.info
        self.assertTrue(mock_logger.info.called)
        # 信息应包含 "Metrics:test"
        call_args = str(mock_logger.info.call_args)
        self.assertIn("Metrics:test", call_args)

    def test_log_summary_swallows_errors(self):
        """logger 抛错时 log_summary 不应抛"""
        m = Metrics("test")
        m.increment("foo")
        # 传入一个抛异常的 logger
        class BadLogger:
            def info(self, *a, **kw):
                raise OSError("disk full")
        # 不应抛
        m.log_summary(BadLogger())


if __name__ == "__main__":
    import unittest
    unittest.main()
