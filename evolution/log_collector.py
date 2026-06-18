"""对话日志结构化采集器

从 yunli 插件的互动日志中采集结构化对话样本，供 Phase 2 模式发现使用。

采集维度：
- 用户消息原始文本
- LLM 原始响应（过滤前）
- 应用了哪些过滤规则
- 触发的情感状态
- 检测到的话题
- 关系状态
- 时间戳

设计原则：
- 零侵入：在 _log_interaction 钩子中采集，不影响主流程
- 采样率：可配置采样比例，避免全量采集导致存储膨胀
- 轮转存储：每个日志文件最多保留 N 条记录，自动轮转
"""

import json
import os
import time
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional


# =============================================================================
# 配置
# =============================================================================

EVOLUTION_DIR = Path(__file__).resolve().parent
LOG_DIR = EVOLUTION_DIR / "logs"
MAX_RECORDS_PER_FILE = 200  # 单文件最多记录数
MAX_LOG_FILES = 5           # 最多保留的日志文件数


# =============================================================================
# 数据模型
# =============================================================================

class InteractionLog:
    """单条互动记录"""

    __slots__ = (
        "timestamp", "group_id", "user_id", "user_nickname",
        "message", "response_raw", "response_filtered",
        "emotion_state", "topic", "relationship_state",
        "applied_filters", "is_knowledge_query",
        "filter_issues", "trigger_type",
    )

    def __init__(self, **kwargs):
        self.timestamp = kwargs.get("timestamp", datetime.now().isoformat())
        self.group_id = kwargs.get("group_id", "")
        self.user_id = kwargs.get("user_id", "")
        self.user_nickname = kwargs.get("user_nickname", "")
        self.message = kwargs.get("message", "")
        self.response_raw = kwargs.get("response_raw", "")
        self.response_filtered = kwargs.get("response_filtered", "")
        self.emotion_state = kwargs.get("emotion_state", "")
        self.topic = kwargs.get("topic", "")
        self.relationship_state = kwargs.get("relationship_state", "")
        self.applied_filters = kwargs.get("applied_filters", [])
        self.is_knowledge_query = kwargs.get("is_knowledge_query", False)
        self.filter_issues = kwargs.get("filter_issues", [])
        self.trigger_type = kwargs.get("trigger_type", "")

    def to_dict(self) -> Dict:
        return {
            "timestamp": self.timestamp,
            "group_id": self.group_id,
            "user_id": self.user_id,
            "user_nickname": self.user_nickname,
            "message": self.message,
            "response_raw": self.response_raw,
            "response_filtered": self.response_filtered,
            "emotion_state": self.emotion_state,
            "topic": self.topic,
            "relationship_state": self.relationship_state,
            "applied_filters": self.applied_filters,
            "is_knowledge_query": self.is_knowledge_query,
            "filter_issues": self.filter_issues,
            "trigger_type": self.trigger_type,
        }

    @classmethod
    def from_dict(cls, data: Dict) -> "InteractionLog":
        return cls(**data)


class LogCollector:
    """对话日志采集器

    Args:
        sample_rate: 采样率 (0.0-1.0)，默认 0.1（10%）
        max_records: 单文件最大记录数，默认 200
        max_files: 最多保留的日志文件数，默认 5
        enabled: 是否启用采集，默认 True
    """

    def __init__(self, sample_rate: float = 0.1,
                 max_records: int = MAX_RECORDS_PER_FILE,
                 max_files: int = MAX_LOG_FILES,
                 enabled: bool = True):
        self.sample_rate = sample_rate
        self.max_records = max_records
        self.max_files = max_files
        self.enabled = enabled

        self._current_file: Optional[Path] = None
        self._current_count = 0
        self._total_collected = 0

        LOG_DIR.mkdir(parents=True, exist_ok=True)

    # ========== 采集 ==========

    def collect(self, log: InteractionLog) -> bool:
        """采集一条互动记录

        Returns:
            True: 已采集，False: 被采样跳过或未启用
        """
        if not self.enabled:
            return False

        # 采样
        if self.sample_rate < 1.0:
            import random
            if random.random() > self.sample_rate:
                return False

        # 写入
        self._append(log)
        return True

    def _append(self, log: InteractionLog):
        """追加一条记录到当前日志文件（线程安全）"""
        try:
            from ._locks import log_lock
        except ImportError:
            from evolution._locks import log_lock

        with log_lock:
            # 轮转检查（在锁内，避免竞态条件）
            if self._current_file is None or self._current_count >= self.max_records:
                self._rotate()

            try:
                with open(self._current_file, "a", encoding="utf-8") as f:
                    f.write(json.dumps(log.to_dict(), ensure_ascii=False) + "\n")
                self._current_count += 1
                self._total_collected += 1
            except (OSError, IOError) as e:
                import logging
                logging.warning(f"[LogCollector] 写入日志文件失败: {e}")
                # 写入失败时不更新计数，下次重试

    def _rotate(self):
        """轮转日志文件"""
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        self._current_file = LOG_DIR / f"interactions_{timestamp}.jsonl"
        self._current_count = 0

        # 清理旧文件
        all_files = sorted(LOG_DIR.glob("interactions_*.jsonl"))
        while len(all_files) > self.max_files:
            oldest = all_files.pop(0)
            try:
                oldest.unlink()
            except OSError:
                pass

    # ========== 读取 ==========

    def load_all(self, limit: int = 500) -> List[InteractionLog]:
        """加载所有日志记录（最近 N 条）"""
        logs = []
        all_files = sorted(LOG_DIR.glob("interactions_*.jsonl"), reverse=True)

        for file_path in all_files:
            try:
                with open(file_path, "r", encoding="utf-8") as f:
                    for line in f:
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            data = json.loads(line)
                            logs.append(InteractionLog.from_dict(data))
                        except json.JSONDecodeError:
                            continue
            except OSError:
                continue

            if len(logs) >= limit:
                break

        return logs[:limit]

    def load_recent(self, hours: float = 24) -> List[InteractionLog]:
        """加载最近 N 小时的日志"""
        cutoff = time.time() - hours * 3600
        all_logs = self.load_all(limit=1000)
        return [
            log for log in all_logs
            if _parse_timestamp(log.timestamp) >= cutoff
        ]

    # ========== 统计 ==========

    def get_stats(self) -> Dict:
        """获取采集统计（线程安全）"""
        try:
            from ._locks import log_lock
        except ImportError:
            from evolution._locks import log_lock

        with log_lock:
            return {
                "total_collected": self._total_collected,
                "current_file": str(self._current_file) if self._current_file else None,
                "current_count": self._current_count,
                "sample_rate": self.sample_rate,
                "enabled": self.enabled,
                "log_files": len(list(LOG_DIR.glob("interactions_*.jsonl"))),
            }

    def get_summary(self) -> Dict:
        """获取采集摘要（用于 LLM 分析前的概览）"""
        logs = self.load_all(limit=500)
        if not logs:
            return {"total": 0}

        topics = {}
        emotions = {}
        filter_issues_total = 0

        for log in logs:
            if log.topic:
                topics[log.topic] = topics.get(log.topic, 0) + 1
            if log.emotion_state:
                emotions[log.emotion_state] = emotions.get(log.emotion_state, 0) + 1
            if log.filter_issues:
                filter_issues_total += len(log.filter_issues)

        return {
            "total": len(logs),
            "time_range": {
                "earliest": min(logs, key=lambda l: l.timestamp).timestamp if logs else None,
                "latest": max(logs, key=lambda l: l.timestamp).timestamp if logs else None,
            },
            "top_topics": sorted(topics.items(), key=lambda x: x[1], reverse=True)[:10],
            "emotion_distribution": emotions,
            "filter_issues_count": filter_issues_total,
            "knowledge_query_ratio": sum(1 for l in logs if l.is_knowledge_query) / len(logs),
            "unique_users": len(set(l.user_id for l in logs if l.user_id)),
        }


def _parse_timestamp(ts: str) -> float:
    """解析时间戳为 epoch 秒"""
    try:
        dt = datetime.fromisoformat(ts)
        return dt.timestamp()
    except (ValueError, TypeError):
        return 0.0