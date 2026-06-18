"""轻量级指标收集 — 基于 stdlib threading.Lock + dict，无第三方依赖

提供三种基础指标类型：
  - Counter: 事件计数（消息处理、LLM 调用、记忆提取等）
  - Timer:   耗时统计（on_request / on_response 总耗时等），保留最近 100 样本
  - Gauge:   瞬时值（队列长度、并发数等）

设计原则：
  - 不引入第三方依赖（无 prometheus_client / opentelemetry）
  - 线程安全：所有读写通过 threading.Lock 保护
  - 可序列化：snapshot() 返回纯 dict，可直接 JSON 化
  - 低开销：timing 列表采用环形覆盖（最多 100 样本），防止内存增长
  - 不修改 on_request / on_response 等公开方法签名
"""
from __future__ import annotations

import threading
import time
from collections import defaultdict
from contextlib import contextmanager
from typing import Any, Dict, Iterator, List, Optional


# 默认保留的 timing 样本数（环形覆盖，避免内存无限增长）
_DEFAULT_TIMING_WINDOW = 100


class Metrics:
    """线程安全的指标收集器

    用法：
        metrics = Metrics("yunli_plugin")
        metrics.increment("msg_processed_total")
        metrics.increment("prompt_inject_total", tag="success")
        with metrics.measure("on_request_duration"):
            do_something()
        metrics.gauge("queue_length", 42)
        snap = metrics.snapshot()
    """

    def __init__(self, name: str = "yunli", timing_window: int = _DEFAULT_TIMING_WINDOW):
        self._name = name
        self._counters: Dict[str, int] = defaultdict(int)
        # 按 tag 细分的计数器（同一指标不同 label 的独立计数）
        self._counters_by_tag: Dict[str, Dict[str, int]] = defaultdict(
            lambda: defaultdict(int)
        )
        # 最近 N 个耗时样本（按指标名分组，环形覆盖）
        self._timings: Dict[str, List[float]] = defaultdict(list)
        self._timing_window = timing_window
        # 瞬时值
        self._gauges: Dict[str, float] = {}
        self._lock = threading.Lock()

    # ========== 写入接口 ==========

    def increment(self, name: str, value: int = 1, tag: Optional[str] = None) -> None:
        """增加计数器

        Args:
            name: 指标名（如 "msg_processed_total"）
            value: 增加量（默认 1）
            tag: 可选标签（如 "success" / "timeout" / "failure"）
        """
        if value < 0:
            raise ValueError("increment value must be non-negative")
        with self._lock:
            self._counters[name] += value
            if tag is not None:
                self._counters_by_tag[name][tag] += value

    def timing(self, name: str, duration_ms: float) -> None:
        """记录一次耗时（保留最近 timing_window 个样本，环形覆盖）

        Args:
            name: 指标名（如 "on_request_duration"）
            duration_ms: 耗时（毫秒）
        """
        with self._lock:
            samples = self._timings[name]
            samples.append(float(duration_ms))
            # 环形覆盖：超过窗口大小则丢弃最早样本
            if len(samples) > self._timing_window:
                samples.pop(0)

    def gauge(self, name: str, value: float) -> None:
        """设置瞬时值（覆盖式）

        Args:
            name: 指标名（如 "memory_queue_length"）
            value: 当前值
        """
        with self._lock:
            self._gauges[name] = float(value)

    @contextmanager
    def measure(self, name: str, tag: Optional[str] = None) -> Iterator[None]:
        """自动计时的上下文管理器

        用法：
            with metrics.measure("on_request_duration"):
                do_request()

        行为：
        - 进入时记录 start = time.monotonic()
        - 退出时（无论正常或异常）记录 duration_ms 到 timing
        - 如果传了 tag，会同时 increment "{name}_count" 并打 tag
        """
        start = time.monotonic()
        try:
            yield
        finally:
            duration_ms = (time.monotonic() - start) * 1000.0
            self.timing(name, duration_ms)
            if tag is not None:
                self.increment(f"{name}_count", tag=tag)

    # ========== 读取接口 ==========

    def snapshot(self) -> Dict[str, Any]:
        """返回可序列化的指标快照

        返回纯 dict，可直接 json.dumps。
        timings 字段是按 name 索引的 {count, mean_ms, max_ms, min_ms}。
        """
        with self._lock:
            timings_summary: Dict[str, Dict[str, Any]] = {}
            for name, samples in self._timings.items():
                if not samples:
                    continue
                timings_summary[name] = {
                    "count": len(samples),
                    "mean_ms": sum(samples) / len(samples),
                    "max_ms": max(samples),
                    "min_ms": min(samples),
                }
            return {
                "name": self._name,
                "counters": dict(self._counters),
                "counters_by_tag": {
                    k: dict(v) for k, v in self._counters_by_tag.items()
                },
                "timings": timings_summary,
                "gauges": dict(self._gauges),
            }

    def format_summary(self) -> str:
        """格式化为可读字符串（多行文本，便于日志输出）"""
        snap = self.snapshot()
        lines = [f"[Metrics:{snap['name']}]"]
        if snap["counters"]:
            lines.append("  Counters:")
            for name, value in sorted(snap["counters"].items()):
                tag_info = ""
                if name in snap["counters_by_tag"]:
                    tags = ", ".join(
                        f"{k}={v}"
                        for k, v in sorted(snap["counters_by_tag"][name].items())
                    )
                    tag_info = f" ({tags})"
                lines.append(f"    {name}: {value}{tag_info}")
        if snap["timings"]:
            lines.append("  Timings (ms):")
            for name, stats in sorted(snap["timings"].items()):
                lines.append(
                    f"    {name}: mean={stats['mean_ms']:.1f} "
                    f"max={stats['max_ms']:.1f} min={stats['min_ms']:.1f} "
                    f"(n={stats['count']})"
                )
        if snap["gauges"]:
            lines.append("  Gauges:")
            for name, value in sorted(snap["gauges"].items()):
                lines.append(f"    {name}: {value}")
        return "\n".join(lines)

    def log_summary(self, logger) -> None:
        """将摘要输出到 logger（info 级别）"""
        try:
            logger.info(self.format_summary())
        except Exception:
            # 指标记录失败不应影响主流程
            pass

    def reset(self) -> None:
        """清空所有指标（主要用于测试）"""
        with self._lock:
            self._counters.clear()
            self._counters_by_tag.clear()
            self._timings.clear()
            self._gauges.clear()


__all__ = ["Metrics"]
