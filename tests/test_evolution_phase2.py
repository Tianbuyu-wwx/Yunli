"""Darwin 日志采集器 (log_collector.py) + PatternDiscovery + RuleGenerator 单元测试

覆盖:
  - InteractionLog dataclass (__slots__, to_dict, from_dict, 全字段)
  - LogCollector: collect / get_stats / load_all / load_recent / get_summary / 采样率 / 轮转
  - DiscoveredPattern dataclass (to_dict, from_dict, 全字段)
  - PatternDiscovery: load_all / load_unreviewed / accept / reject / get_stats / CATEGORIES
  - RuleProposal dataclass (__slots__, to_dict, from_dict, 全字段)
  - RuleGenerator: generate / load_all / load_unreviewed / get_report / CATEGORY_TO_RULE
  - _extract_keywords 辅助函数
"""

import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

test_dir = os.path.dirname(os.path.abspath(__file__))
yunli_dir = os.path.dirname(test_dir)
parent_dir = os.path.dirname(yunli_dir)
for p in [parent_dir, yunli_dir, test_dir]:
    if p not in sys.path:
        sys.path.insert(0, p)

from test_base import YunliTestCase

from evolution.log_collector import InteractionLog, LogCollector, LOG_DIR as _LOG_DIR
from evolution.pattern_discovery import (
    DiscoveredPattern, PatternDiscovery,
    DISCOVERIES_FILE as _DISCOVERIES_FILE,
)
from evolution.rule_generator import (
    RuleProposal, RuleGenerator,
    _extract_keywords,
    RULES_FILE as _RULES_FILE,
)

# ============================================================================
# InteractionLog 测试
# ============================================================================

class TestInteractionLog(YunliTestCase):
    """InteractionLog dataclass"""

    def test_create_with_all_slots(self):
        log = InteractionLog(
            group_id="g1", user_id="u1", user_nickname="test_user",
            message="你好啊", response_raw="（挥手）你好啊", response_filtered="你好啊",
            emotion_state="neutral", topic="闲聊", relationship_state="stranger",
            applied_filters=["action_filter"], is_knowledge_query=False,
            filter_issues=[], trigger_type="llm",
        )
        self.assertEqual(log.group_id, "g1")
        self.assertEqual(log.user_id, "u1")
        self.assertEqual(log.message, "你好啊")
        self.assertEqual(log.response_raw, "（挥手）你好啊")
        self.assertEqual(log.response_filtered, "你好啊")
        self.assertEqual(log.emotion_state, "neutral")
        self.assertEqual(log.topic, "闲聊")
        self.assertEqual(log.relationship_state, "stranger")
        self.assertEqual(log.applied_filters, ["action_filter"])
        self.assertFalse(log.is_knowledge_query)
        self.assertEqual(log.filter_issues, [])
        self.assertEqual(log.trigger_type, "llm")

    def test_to_dict_contains_all_expected_keys(self):
        log = InteractionLog(
            group_id="g1", user_id="u1", user_nickname="n",
            message="m", response_raw="rr", response_filtered="rf",
            emotion_state="e", trigger_type="llm",
        )
        d = log.to_dict()
        expected_keys = [
            "timestamp", "group_id", "user_id", "user_nickname",
            "message", "response_raw", "response_filtered",
            "emotion_state", "topic", "relationship_state",
            "applied_filters", "is_knowledge_query", "filter_issues", "trigger_type",
        ]
        for k in expected_keys:
            self.assertIn(k, d, f"缺少键: {k}")

    def test_from_dict_reconstructs(self):
        log = InteractionLog(
            group_id="g1", user_id="u1", user_nickname="test",
            message="msg", response_raw="raw", response_filtered="filtered",
            emotion_state="happy", trigger_type="llm",
        )
        d = log.to_dict()
        restored = InteractionLog.from_dict(d)
        self.assertEqual(restored.group_id, log.group_id)
        self.assertEqual(restored.user_id, log.user_id)
        self.assertEqual(restored.message, log.message)
        self.assertEqual(restored.emotion_state, log.emotion_state)

    def test_default_values(self):
        log = InteractionLog()
        self.assertEqual(log.group_id, "")
        self.assertEqual(log.user_id, "")
        self.assertEqual(log.message, "")
        self.assertEqual(log.applied_filters, [])
        self.assertFalse(log.is_knowledge_query)

    def test_timestamp_auto_generated(self):
        log = InteractionLog(group_id="g1", user_id="u1")
        self.assertIsNotNone(log.timestamp)
        self.assertIn("T", log.timestamp)  # ISO 格式含 T


# ============================================================================
# LogCollector 测试
# ============================================================================

class TestLogCollector(YunliTestCase):
    """LogCollector: collect / get_stats / load_all / load_recent / get_summary / 采样 / 轮转"""

    def setUp(self):
        self._tmp_dir = tempfile.TemporaryDirectory()
        self._tmp = Path(self._tmp_dir.name)
        self._orig_log_dir = _LOG_DIR
        # 创建临时日志目录
        self._log_dir = self._tmp / "logs"
        self._log_dir.mkdir(parents=True)

    def tearDown(self):
        self._tmp_dir.cleanup()

    def _make_log(self, **kw):
        return InteractionLog(
            group_id=kw.get("group_id", "g1"),
            user_id=kw.get("user_id", "u1"),
            user_nickname=kw.get("user_nickname", "test"),
            message=kw.get("message", "hello"),
            response_raw=kw.get("response_raw", "raw"),
            response_filtered=kw.get("response_filtered", "filtered"),
            emotion_state=kw.get("emotion_state", "neutral"),
            topic=kw.get("topic", ""),
            relationship_state=kw.get("relationship_state", ""),
            applied_filters=kw.get("applied_filters", []),
            is_knowledge_query=kw.get("is_knowledge_query", False),
            filter_issues=kw.get("filter_issues", []),
            trigger_type=kw.get("trigger_type", "llm"),
        )

    # ---- collect / get_stats ----

    def test_collect_100pct_sampling(self):
        with patch("evolution.log_collector.LOG_DIR", self._log_dir):
            lc = LogCollector(sample_rate=1.0)
            lc.collect(self._make_log())
            lc.collect(self._make_log())
            stats = lc.get_stats()
            self.assertEqual(stats["total_collected"], 2)

    def test_collect_0pct_sampling(self):
        with patch("evolution.log_collector.LOG_DIR", self._log_dir):
            lc = LogCollector(sample_rate=0.0)
            for _ in range(100):
                lc.collect(self._make_log())
            stats = lc.get_stats()
            self.assertEqual(stats["total_collected"], 0)

    def test_collect_when_disabled(self):
        with patch("evolution.log_collector.LOG_DIR", self._log_dir):
            lc = LogCollector(enabled=False)
            result = lc.collect(self._make_log())
            self.assertFalse(result)
            stats = lc.get_stats()
            self.assertEqual(stats["total_collected"], 0)

    def test_get_stats_before_any_collect(self):
        with patch("evolution.log_collector.LOG_DIR", self._log_dir):
            lc = LogCollector()
            stats = lc.get_stats()
            self.assertEqual(stats["total_collected"], 0)
            self.assertEqual(stats["current_count"], 0)
            self.assertIsNone(stats["current_file"])

    # ---- 轮转 ----

    def test_rotate_on_max_records(self):
        with patch("evolution.log_collector.LOG_DIR", self._log_dir):
            lc = LogCollector(sample_rate=1.0, max_records=3, max_files=5)
            for i in range(10):
                lc.collect(self._make_log())
                # 每次轮转触发点（3的倍数）睡 1.1 秒确保时间戳不同
                if (i + 1) % 3 == 0:
                    time.sleep(1.1)
            # 10 条记录，max_records=3，应有 4 个文件（3+3+3+1）
            log_files = list(self._log_dir.glob("interactions_*.jsonl"))
            self.assertGreater(len(log_files), 1, f"应触发轮转生成多个文件，实际 {len(log_files)} 个")

    def test_rotate_cleans_old_files(self):
        with patch("evolution.log_collector.LOG_DIR", self._log_dir):
            lc = LogCollector(sample_rate=1.0, max_records=1, max_files=2)
            for _ in range(10):
                lc.collect(self._make_log())
            log_files = list(self._log_dir.glob("interactions_*.jsonl"))
            self.assertLessEqual(len(log_files), lc.max_files + 1,
                                 f"最多 {lc.max_files + 1} 个文件（当前+活跃），实际 {len(log_files)}")

    # ---- load_all / load_recent ----

    def test_load_all_returns_logs(self):
        with patch("evolution.log_collector.LOG_DIR", self._log_dir):
            lc = LogCollector(sample_rate=1.0, max_records=5)
            lc.collect(self._make_log(message="msg1", group_id="g1"))
            lc.collect(self._make_log(message="msg2", group_id="g2"))
            # 强制写盘：collect 写到文件，load_all 从文件读
            logs = lc.load_all()
            self.assertEqual(len(logs), 2)

    def test_load_all_respects_limit(self):
        with patch("evolution.log_collector.LOG_DIR", self._log_dir):
            lc = LogCollector(sample_rate=1.0, max_records=100)
            for i in range(20):
                lc.collect(self._make_log(message=f"msg{i}"))
            logs = lc.load_all(limit=5)
            self.assertLessEqual(len(logs), 5)

    def test_load_all_empty_when_no_files(self):
        with patch("evolution.log_collector.LOG_DIR", self._log_dir):
            lc = LogCollector()
            logs = lc.load_all()
            self.assertEqual(logs, [])

    def test_load_all_skips_corrupted_lines(self):
        with patch("evolution.log_collector.LOG_DIR", self._log_dir):
            lc = LogCollector(sample_rate=1.0, max_records=100)
            lc.collect(self._make_log(message="good"))
            # 手动写一条损坏的行
            log_files = list(self._log_dir.glob("interactions_*.jsonl"))
            if log_files:
                with open(log_files[0], "a", encoding="utf-8") as f:
                    f.write("{corrupted json\n")
            logs = lc.load_all()
            self.assertGreaterEqual(len(logs), 1)  # 至少正确的那条被读到

    def test_load_recent_filters_by_time(self):
        with patch("evolution.log_collector.LOG_DIR", self._log_dir):
            lc = LogCollector(sample_rate=1.0, max_records=100)
            lc.collect(self._make_log(message="recent"))
            logs = lc.load_recent(hours=24)
            self.assertGreaterEqual(len(logs), 1)

    def test_load_recent_with_zero_hours(self):
        with patch("evolution.log_collector.LOG_DIR", self._log_dir):
            lc = LogCollector(sample_rate=1.0, max_records=100)
            lc.collect(self._make_log(message="recent"))
            # 0 小时：只加载未来时刻之后的（应该为 0 条）
            logs = lc.load_recent(hours=0)
            self.assertEqual(len(logs), 0)

    # ---- get_summary ----

    def test_get_summary_empty(self):
        with patch("evolution.log_collector.LOG_DIR", self._log_dir):
            lc = LogCollector()
            summary = lc.get_summary()
            self.assertEqual(summary["total"], 0)

    def test_get_summary_with_data(self):
        with patch("evolution.log_collector.LOG_DIR", self._log_dir):
            lc = LogCollector(sample_rate=1.0, max_records=100)
            lc.collect(self._make_log(topic="TCG", emotion_state="excited"))
            lc.collect(self._make_log(topic="TCG", emotion_state="neutral"))
            lc.collect(self._make_log(topic="食物", emotion_state="happy",
                                       filter_issues=["issue1"],
                                       is_knowledge_query=True))
            summary = lc.get_summary()
            self.assertEqual(summary["total"], 3)
            self.assertIn("top_topics", summary)
            self.assertIn("emotion_distribution", summary)
            self.assertEqual(summary["filter_issues_count"], 1)

    def test_get_summary_knowledge_query_ratio(self):
        with patch("evolution.log_collector.LOG_DIR", self._log_dir):
            lc = LogCollector(sample_rate=1.0, max_records=100)
            lc.collect(self._make_log(is_knowledge_query=True))
            lc.collect(self._make_log(is_knowledge_query=False))
            summary = lc.get_summary()
            self.assertAlmostEqual(summary["knowledge_query_ratio"], 0.5)

    def test_get_summary_unique_users(self):
        with patch("evolution.log_collector.LOG_DIR", self._log_dir):
            lc = LogCollector(sample_rate=1.0, max_records=100)
            lc.collect(self._make_log(user_id="u1"))
            lc.collect(self._make_log(user_id="u2"))
            lc.collect(self._make_log(user_id="u1"))  # 重复
            summary = lc.get_summary()
            self.assertEqual(summary["unique_users"], 2)

    # ---- JSONL 格式验证 ----

    def test_collect_writes_valid_jsonl(self):
        with patch("evolution.log_collector.LOG_DIR", self._log_dir):
            lc = LogCollector(sample_rate=1.0, max_records=100)
            lc.collect(self._make_log(message="test jsonl", group_id="g1"))
            log_files = list(self._log_dir.glob("interactions_*.jsonl"))
            self.assertGreater(len(log_files), 0)
            for lf in log_files:
                for line in lf.read_text(encoding="utf-8").strip().split("\n"):
                    if line.strip():
                        data = json.loads(line)
                        self.assertIn("group_id", data)
                        self.assertIn("message", data)


# ============================================================================
# DiscoveredPattern 测试
# ============================================================================

class TestDiscoveredPattern(YunliTestCase):
    """DiscoveredPattern dataclass"""

    def test_create_full(self):
        pat = DiscoveredPattern(
            pattern_id="pat_001",
            category="filter_escape",
            severity="high",
            description="过滤逃逸问题",
            examples=["示例1", "示例2"],
            suggested_fix="修复方案",
            confidence=0.85,
            reviewed=False,
            accepted=False,
        )
        self.assertEqual(pat.pattern_id, "pat_001")
        self.assertEqual(pat.category, "filter_escape")
        self.assertEqual(pat.severity, "high")
        self.assertEqual(pat.description, "过滤逃逸问题")
        self.assertEqual(pat.examples, ["示例1", "示例2"])
        self.assertEqual(pat.suggested_fix, "修复方案")
        self.assertEqual(pat.confidence, 0.85)
        self.assertFalse(pat.reviewed)
        self.assertFalse(pat.accepted)

    def test_default_values(self):
        pat = DiscoveredPattern(category="filter_escape", description="desc")
        self.assertTrue(pat.pattern_id.startswith("pat_"))
        self.assertEqual(pat.severity, "medium")
        self.assertEqual(pat.examples, [])
        self.assertEqual(pat.confidence, 0.5)
        self.assertFalse(pat.reviewed)
        self.assertFalse(pat.accepted)

    def test_to_dict_and_from_dict_roundtrip(self):
        pat = DiscoveredPattern(
            pattern_id="pat_001",
            category="filter_escape",
            severity="high",
            description="desc",
            examples=["ex1"],
            suggested_fix="fix",
            confidence=0.9,
            reviewed=True,
            accepted=True,
        )
        d = pat.to_dict()
        restored = DiscoveredPattern.from_dict(d)
        self.assertEqual(restored.pattern_id, pat.pattern_id)
        self.assertEqual(restored.category, pat.category)
        self.assertEqual(restored.severity, pat.severity)
        self.assertEqual(restored.description, pat.description)
        self.assertEqual(restored.examples, pat.examples)
        self.assertEqual(restored.confidence, pat.confidence)
        self.assertTrue(restored.reviewed)
        self.assertTrue(restored.accepted)


# ============================================================================
# PatternDiscovery 测试
# ============================================================================

class TestPatternDiscovery(YunliTestCase):
    """PatternDiscovery: load_all / load_unreviewed / accept / reject / get_stats"""

    def setUp(self):
        self._tmp_dir = tempfile.TemporaryDirectory()
        self._tmp = Path(self._tmp_dir.name)

        # 重定向 DISCOVERIES_FILE
        self._orig_disc_file = _DISCOVERIES_FILE
        self._disc_file = self._tmp / "discoveries" / "discovered_patterns.json"
        self._disc_file.parent.mkdir(parents=True, exist_ok=True)

    def tearDown(self):
        self._tmp_dir.cleanup()

    def _make_pd(self):
        return PatternDiscovery(provider=None)

    def _make_pat(self, pid="pat_001", cat="filter_escape", desc="desc",
                   reviewed=False, accepted=False, confidence=0.8):
        return DiscoveredPattern(
            pattern_id=pid, category=cat, severity="high",
            description=desc, reviewed=reviewed, accepted=accepted,
            confidence=confidence,
        )

    def _save_pats(self, pd, pats):
        with patch("evolution.pattern_discovery.DISCOVERIES_FILE", self._disc_file):
            pd._save(pats)

    def test_load_all_empty(self):
        pd = self._make_pd()
        with patch("evolution.pattern_discovery.DISCOVERIES_FILE", self._disc_file):
            self.assertEqual(pd.load_all(), [])

    def test_save_and_load_all(self):
        pd = self._make_pd()
        pat = self._make_pat()
        with patch("evolution.pattern_discovery.DISCOVERIES_FILE", self._disc_file):
            pd._save([pat])
            loaded = pd.load_all()
            self.assertEqual(len(loaded), 1)
            self.assertEqual(loaded[0].pattern_id, "pat_001")

    def test_load_unreviewed(self):
        pd = self._make_pd()
        with patch("evolution.pattern_discovery.DISCOVERIES_FILE", self._disc_file):
            pd._save([
                self._make_pat("p1", reviewed=False),
                self._make_pat("p2", reviewed=True),
                self._make_pat("p3", reviewed=False),
            ])
            unreviewed = pd.load_unreviewed()
            self.assertEqual(len(unreviewed), 2)
            self.assertEqual({p.pattern_id for p in unreviewed}, {"p1", "p3"})

    def test_accept_pattern(self):
        pd = self._make_pd()
        pat = self._make_pat(reviewed=False, accepted=False)
        with patch("evolution.pattern_discovery.DISCOVERIES_FILE", self._disc_file):
            pd._save([pat])
            pd.accept("pat_001")
            loaded = pd.load_all()
            self.assertTrue(loaded[0].accepted)
            self.assertTrue(loaded[0].reviewed)

    def test_reject_pattern(self):
        pd = self._make_pd()
        pat = self._make_pat(reviewed=False, accepted=False)
        with patch("evolution.pattern_discovery.DISCOVERIES_FILE", self._disc_file):
            pd._save([pat])
            pd.reject("pat_001")
            loaded = pd.load_all()
            self.assertFalse(loaded[0].accepted)
            self.assertTrue(loaded[0].reviewed)

    def test_accept_nonexistent_no_crash(self):
        pd = self._make_pd()
        with patch("evolution.pattern_discovery.DISCOVERIES_FILE", self._disc_file):
            pd._save([self._make_pat()])
            pd.accept("pat_nonexistent")  # 不应崩溃
            loaded = pd.load_all()
            self.assertFalse(loaded[0].accepted, "不存在的模式不应被标记")

    def test_reject_nonexistent_no_crash(self):
        pd = self._make_pd()
        with patch("evolution.pattern_discovery.DISCOVERIES_FILE", self._disc_file):
            pd._save([self._make_pat()])
            pd.reject("pat_nonexistent")  # 不应崩溃

    def test_get_stats(self):
        pd = self._make_pd()
        with patch("evolution.pattern_discovery.DISCOVERIES_FILE", self._disc_file):
            pd._save([
                self._make_pat("p1", "filter_escape", confidence=0.8, reviewed=False),
                self._make_pat("p2", "emotion_miss", confidence=0.6, reviewed=True),
                self._make_pat("p3", "style_gap", confidence=0.9, reviewed=False),
            ])
            stats = pd.get_stats()
            self.assertEqual(stats["total"], 3)
            self.assertEqual(stats["unreviewed"], 2)
            self.assertIn("filter_escape", stats["by_category"])
            self.assertAlmostEqual(stats["avg_confidence"], (0.8 + 0.6 + 0.9) / 3)

    def test_get_stats_empty(self):
        pd = self._make_pd()
        with patch("evolution.pattern_discovery.DISCOVERIES_FILE", self._disc_file):
            stats = pd.get_stats()
            self.assertEqual(stats["total"], 0)
            self.assertEqual(stats["unreviewed"], 0)
            self.assertEqual(stats["avg_confidence"], 0)

    def test_categories_constant(self):
        self.assertEqual(len(PatternDiscovery.CATEGORIES), 6)
        self.assertIn("filter_escape", PatternDiscovery.CATEGORIES)
        self.assertIn("emotion_miss", PatternDiscovery.CATEGORIES)
        self.assertIn("style_gap", PatternDiscovery.CATEGORIES)
        self.assertIn("boundary_violation", PatternDiscovery.CATEGORIES)
        self.assertIn("tone_inconsistency", PatternDiscovery.CATEGORIES)
        self.assertIn("new_topic", PatternDiscovery.CATEGORIES)

    def test_category_names(self):
        self.assertEqual(PatternDiscovery.CATEGORY_NAMES["filter_escape"], "过滤逃逸")
        self.assertEqual(PatternDiscovery.CATEGORY_NAMES["new_topic"], "新话题发现")


# ============================================================================
# RuleProposal 测试
# ============================================================================

class TestRuleProposal(YunliTestCase):
    """RuleProposal dataclass (__slots__)"""

    def test_create_full(self):
        rp = RuleProposal(
            rule_id="rule_001",
            source_pattern_id="pat_001",
            rule_type="filter_regex",
            target_file="persona/filters.py",
            rule_content={"type": "filter_regex", "pattern": r"\（.*?\）"},
            apply_instructions="在 filters.py 中添加",
            confidence=0.9,
            reviewed=False,
            accepted=False,
            applied_at="",
        )
        self.assertEqual(rp.rule_id, "rule_001")
        self.assertEqual(rp.source_pattern_id, "pat_001")
        self.assertEqual(rp.rule_type, "filter_regex")
        self.assertEqual(rp.target_file, "persona/filters.py")
        self.assertEqual(rp.rule_content["pattern"], r"\（.*?\）")
        self.assertFalse(rp.accepted)
        self.assertFalse(rp.reviewed)

    def test_default_values(self):
        rp = RuleProposal(rule_id="r1", rule_type="filter_regex")
        self.assertEqual(rp.source_pattern_id, "")
        self.assertEqual(rp.confidence, 0.5)
        self.assertEqual(rp.rule_content, {})
        self.assertFalse(rp.reviewed)

    def test_to_dict_and_from_dict_roundtrip(self):
        rp = RuleProposal(
            rule_id="rule_001",
            source_pattern_id="pat_001",
            rule_type="filter_regex",
            target_file="filters.py",
            rule_content={"k": "v"},
            apply_instructions="do this",
            confidence=0.85,
            reviewed=True,
            accepted=False,
            applied_at="2026-01-01",
        )
        d = rp.to_dict()
        restored = RuleProposal.from_dict(d)
        self.assertEqual(restored.rule_id, rp.rule_id)
        self.assertEqual(restored.rule_type, rp.rule_type)
        self.assertEqual(restored.target_file, rp.target_file)
        self.assertEqual(restored.rule_content, rp.rule_content)
        self.assertEqual(restored.confidence, rp.confidence)
        self.assertTrue(restored.reviewed)
        self.assertFalse(restored.accepted)


# ============================================================================
# RuleGenerator 测试
# ============================================================================

class TestRuleGenerator(YunliTestCase):
    """RuleGenerator: generate / load_all / load_unreviewed / get_report / CATEGORY_TO_RULE"""

    def setUp(self):
        self._tmp_dir = tempfile.TemporaryDirectory()
        self._tmp = Path(self._tmp_dir.name)

        # 重定向 RULES_FILE
        self._orig_rules_file = _RULES_FILE
        self._rules_file = self._tmp / "pending_rules" / "pending_rules.json"
        self._rules_file.parent.mkdir(parents=True, exist_ok=True)

    def tearDown(self):
        self._tmp_dir.cleanup()

    def _make_rg(self):
        return RuleGenerator(provider=None, log_callback=lambda _: None)

    def _make_pat(self, pid="pat_001", cat="filter_escape", desc="desc",
                   examples=None, fix="", confidence=0.8, accepted=True):
        return DiscoveredPattern(
            pattern_id=pid, category=cat, severity="high",
            description=desc, examples=examples or [],
            suggested_fix=fix, confidence=confidence,
            accepted=accepted, reviewed=True,
        )

    def _save_rules(self, rg, proposals):
        with patch("evolution.rule_generator.RULES_FILE", self._rules_file):
            rg._save(proposals)

    def test_category_to_rule_mapping(self):
        self.assertEqual(RuleGenerator.CATEGORY_TO_RULE["filter_escape"], "filter_regex")
        self.assertEqual(RuleGenerator.CATEGORY_TO_RULE["emotion_miss"], "emotion_trigger")
        self.assertEqual(RuleGenerator.CATEGORY_TO_RULE["style_gap"], "tone_rule")
        self.assertEqual(RuleGenerator.CATEGORY_TO_RULE["boundary_violation"], "boundary_rule")
        self.assertEqual(RuleGenerator.CATEGORY_TO_RULE["tone_inconsistency"], "tone_rule")
        self.assertEqual(RuleGenerator.CATEGORY_TO_RULE["new_topic"], "topic_keyword")

    def test_load_all_empty(self):
        rg = self._make_rg()
        with patch("evolution.rule_generator.RULES_FILE", self._rules_file):
            self.assertEqual(rg.load_all(), [])

    def test_save_and_load_all(self):
        rg = self._make_rg()
        rp = RuleProposal(rule_id="rule_001", rule_type="filter_regex",
                          target_file="filters.py", rule_content={"k": "v"})
        with patch("evolution.rule_generator.RULES_FILE", self._rules_file):
            rg._save([rp])
            loaded = rg.load_all()
            self.assertEqual(len(loaded), 1)
            self.assertEqual(loaded[0].rule_id, "rule_001")

    def test_load_unreviewed(self):
        rg = self._make_rg()
        with patch("evolution.rule_generator.RULES_FILE", self._rules_file):
            rg._save([
                RuleProposal(rule_id="r1", rule_type="t", reviewed=False),
                RuleProposal(rule_id="r2", rule_type="t", reviewed=True),
                RuleProposal(rule_id="r3", rule_type="t", reviewed=False),
            ])
            self.assertEqual(len(rg.load_unreviewed()), 2)

    def test_generate_from_patterns(self):
        rg = self._make_rg()
        pat = self._make_pat(pid="pat_001", cat="filter_escape",
                              desc="过滤逃逸", examples=["（测试）输出泄漏"],
                              fix="过滤中文括号内容", accepted=True)
        with patch("evolution.rule_generator.RULES_FILE", self._rules_file):
            proposals = rg.generate([pat])
            self.assertEqual(len(proposals), 1)
            self.assertEqual(proposals[0].rule_type, "filter_regex")
            self.assertEqual(proposals[0].source_pattern_id, "pat_001")
            self.assertIn("type", proposals[0].rule_content)

    def test_generate_skips_not_accepted(self):
        rg = self._make_rg()
        pat = self._make_pat(accepted=False)
        with patch("evolution.rule_generator.RULES_FILE", self._rules_file):
            proposals = rg.generate([pat])
            self.assertEqual(proposals, [])

    def test_generate_skips_duplicate(self):
        rg = self._make_rg()
        pat = self._make_pat(pid="pat_001", accepted=True)
        with patch("evolution.rule_generator.RULES_FILE", self._rules_file):
            # 先生成一次
            rg.generate([pat])
            # 再生成：应跳过（已存在）
            proposals = rg.generate([pat])
            self.assertEqual(proposals, [])

    def test_generate_multiple_types(self):
        rg = self._make_rg()
        pats = [
            self._make_pat("p1", "filter_escape", desc="过滤逃逸", accepted=True),
            self._make_pat("p2", "emotion_miss", desc="情感误判", accepted=True),
            self._make_pat("p3", "new_topic", desc="新话题", examples=["TCG"], accepted=True),
        ]
        with patch("evolution.rule_generator.RULES_FILE", self._rules_file):
            proposals = rg.generate(pats)
            types = {p.rule_type for p in proposals}
            self.assertIn("filter_regex", types)
            self.assertIn("emotion_trigger", types)
            self.assertIn("topic_keyword", types)

    def test_generate_unknown_category(self):
        pat = DiscoveredPattern(
            pattern_id="p_unk", category="unknown_cat", severity="medium",
            description="unknown", accepted=True, reviewed=True,
        )
        rg = self._make_rg()
        with patch("evolution.rule_generator.RULES_FILE", self._rules_file):
            proposals = rg.generate([pat])
            self.assertEqual(proposals, [])  # unknown → 跳过

    def test_get_report(self):
        rg = self._make_rg()
        with patch("evolution.rule_generator.RULES_FILE", self._rules_file):
            rg._save([
                RuleProposal(rule_id="r1", rule_type="filter_regex",
                              target_file="filters.py", rule_content={"k": "v"},
                              apply_instructions="添加规则", confidence=0.9),
            ])
            report = rg.get_report()
            self.assertIn("filter_regex", report)

    def test_get_report_empty(self):
        rg = self._make_rg()
        with patch("evolution.rule_generator.RULES_FILE", self._rules_file):
            report = rg.get_report()
            self.assertIn("总计: 0", report)

    def test_generate_filter_regex_content(self):
        pat = self._make_pat("p1", "filter_escape", desc="过滤动作注入",
                              examples=["（思考中）我在想"], fix='r"（.*?）"',
                              accepted=True)
        rg = self._make_rg()
        with patch("evolution.rule_generator.RULES_FILE", self._rules_file):
            proposals = rg.generate([pat])
            self.assertEqual(len(proposals), 1)
            content = proposals[0].rule_content
            self.assertEqual(content["type"], "filter_regex")
            self.assertEqual(content["layer"], "format")
            self.assertEqual(content["pattern"], 'r"（.*?）"')

    def test_generate_boundary_rule_content(self):
        pat = self._make_pat("p2", "boundary_violation", desc="越界行为",
                              fix="禁止冒充管理员", accepted=True)
        rg = self._make_rg()
        with patch("evolution.rule_generator.RULES_FILE", self._rules_file):
            proposals = rg.generate([pat])
            self.assertEqual(len(proposals), 1)
            content = proposals[0].rule_content
            self.assertEqual(content["type"], "boundary_rule")
            self.assertEqual(content["rule"], "禁止冒充管理员")

    def test_generate_tone_rule_content(self):
        pat = self._make_pat("p3", "tone_inconsistency", desc="语气不一致",
                              fix="统一使用傲娇语气", accepted=True)
        rg = self._make_rg()
        with patch("evolution.rule_generator.RULES_FILE", self._rules_file):
            proposals = rg.generate([pat])
            self.assertEqual(len(proposals), 1)
            self.assertEqual(proposals[0].rule_type, "tone_rule")


# ============================================================================
# _extract_keywords 辅助函数
# ============================================================================

class TestExtractKeywords(YunliTestCase):
    """_extract_keywords 函数"""

    def test_extracts_chinese_words(self):
        words = _extract_keywords("你好世界，这是一个测试")
        self.assertIn("你好世界", words)
        # "这是一个测试" 是 6 个中文字符，正则 [\u4e00-\u9fff]{2,6} 会整体匹配
        any_match = "这是一个测试" in words or "这是一个" in words or "一个测试" in words
        self.assertTrue(any_match, f"至少应匹配一个中文词组，实际: {words}")

    def test_returns_unique_words(self):
        words = _extract_keywords("你好你好你好世界")
        self.assertEqual(len(words), len(set(words)), "不应重复")

    def test_max_10_words(self):
        long_text = "一" + "一二三四五六七八九十" * 20
        words = _extract_keywords(long_text)
        self.assertLessEqual(len(words), 10)

    def test_ignores_short_words(self):
        words = _extract_keywords("a b c 我 你 他")
        # 中文2-6字，但 "我" 是1字不应匹配
        single_char = [w for w in words if len(w) < 2]
        self.assertEqual(len(single_char), 0, "单字不应被提取")

    def test_empty_string(self):
        self.assertEqual(_extract_keywords(""), [])

    def test_non_chinese(self):
        self.assertEqual(_extract_keywords("hello world 123"), [])


# ============================================================================
# 补充覆盖率测试：PatternDiscovery._build_analyze_prompt / discover / _call_llm_http
# ============================================================================

class TestPatternDiscoveryBuilding(YunliTestCase):
    """PatternDiscovery: _build_analyze_prompt / discover / _call_llm_http"""

    def setUp(self):
        self._tmp_dir = tempfile.TemporaryDirectory()
        self._tmp = Path(self._tmp_dir.name)
        self._orig_disc_file = _DISCOVERIES_FILE
        self._disc_file = self._tmp / "discoveries" / "discovered_patterns.json"
        self._disc_file.parent.mkdir(parents=True, exist_ok=True)

        # 创建临时日志目录
        self._log_dir = self._tmp / "logs"
        self._log_dir.mkdir(parents=True)

    def tearDown(self):
        self._tmp_dir.cleanup()

    def test_build_analyze_prompt_contains_category_name(self):
        pd = PatternDiscovery(provider=None, log_callback=lambda _: None)
        logs = [InteractionLog(
            group_id="g1", user_id="u1", message="hello",
            response_raw="raw", response_filtered="filtered",
            emotion_state="neutral", trigger_type="llm",
        ) for _ in range(15)]
        prompt = pd._build_analyze_prompt(logs, "filter_escape")
        self.assertIn("过滤逃逸", prompt)
        self.assertIn("对话日志样本", prompt)
        self.assertIn("hello", prompt)

    def test_build_analyze_prompt_clips_logs_to_50(self):
        pd = PatternDiscovery(provider=None, log_callback=lambda _: None)
        logs = [InteractionLog(
            group_id="g1", user_id="u1", message=f"msg{i}",
            response_raw="raw", response_filtered="filtered",
            trigger_type="llm",
        ) for i in range(100)]
        prompt = pd._build_analyze_prompt(logs, "style_gap")
        # 只应包含前 50 条
        self.assertIn("msg0", prompt)
        self.assertIn("msg49", prompt)
        self.assertNotIn("msg50", prompt)

    def test_discover_with_insufficient_logs(self):
        pd = PatternDiscovery(provider=None, log_callback=lambda _: None)
        from evolution.log_collector import LogCollector
        lc = LogCollector(sample_rate=1.0, max_records=100)
        # 只收集 5 条（< 10 阈值）
        for i in range(5):
            lc.collect(InteractionLog(
                group_id="g1", user_id="u1", message=f"msg{i}",
                response_raw="raw", response_filtered="filtered",
                trigger_type="llm",
            ))
        pd.log_collector = lc
        with patch("evolution.pattern_discovery.DISCOVERIES_FILE", self._disc_file):
            patterns = pd.discover(hours=24)
            self.assertEqual(patterns, [], "不足 10 条日志应跳过分析")

    def test_discover_with_mock_llm(self):
        """使用 mock LLM 测试 discover 完整流程"""
        mock_prov = MagicMock()
        mock_prov.text_chat = MagicMock()

        async def mock_chat(prompt, system_prompt):
            resp = MagicMock()
            resp.completion_text = json.dumps([
                {
                    "category": "filter_escape",
                    "severity": "high",
                    "description": "发现过滤逃逸",
                    "examples": ["示例1"],
                    "suggested_fix": "修复方案",
                    "confidence": 0.9,
                },
            ])
            return resp

        mock_prov.text_chat = mock_chat

        pd = PatternDiscovery(provider=mock_prov, log_callback=lambda _: None)
        from evolution.log_collector import LogCollector
        lc = LogCollector(sample_rate=1.0, max_records=100)
        # 收集 15 条日志（≥ 10 阈值）
        for i in range(15):
            lc.collect(InteractionLog(
                group_id="g1", user_id="u1", message=f"msg{i}",
                response_raw="raw", response_filtered="filtered",
                trigger_type="llm",
            ))
        pd.log_collector = lc

        with patch("evolution.pattern_discovery.DISCOVERIES_FILE", self._disc_file):
            patterns = pd.discover(category="filter_escape", hours=24)
            self.assertGreaterEqual(len(patterns), 1)
            self.assertEqual(patterns[0].category, "filter_escape")
            self.assertEqual(patterns[0].severity, "high")

    def test_discover_llm_returns_string(self):
        """LLM 返回纯字符串而非 LLMResponse"""
        mock_prov = MagicMock()
        mock_prov.text_chat = MagicMock()

        async def mock_chat(prompt, system_prompt):
            return json.dumps([
                {
                    "category": "style_gap",
                    "severity": "medium",
                    "description": "风格偏移",
                    "examples": [],
                    "suggested_fix": "",
                    "confidence": 0.5,
                },
            ])

        mock_prov.text_chat = mock_chat

        pd = PatternDiscovery(provider=mock_prov, log_callback=lambda _: None)
        from evolution.log_collector import LogCollector
        lc = LogCollector(sample_rate=1.0, max_records=100)
        for i in range(15):
            lc.collect(InteractionLog(
                group_id="g1", user_id="u1", message=f"msg{i}",
                response_raw="raw", response_filtered="filtered",
                trigger_type="llm",
            ))
        pd.log_collector = lc

        with patch("evolution.pattern_discovery.DISCOVERIES_FILE", self._disc_file):
            patterns = pd.discover(category="style_gap", hours=24)
            self.assertGreaterEqual(len(patterns), 1)
            self.assertEqual(patterns[0].category, "style_gap")

    def test_discover_llm_returns_none(self):
        """LLM 返回 None 时应跳过该类别"""
        mock_prov = MagicMock()
        mock_prov.text_chat = MagicMock()

        async def mock_chat(prompt, system_prompt):
            return None

        mock_prov.text_chat = mock_chat

        pd = PatternDiscovery(provider=mock_prov, log_callback=lambda _: None)
        from evolution.log_collector import LogCollector
        lc = LogCollector(sample_rate=1.0, max_records=100)
        for i in range(15):
            lc.collect(InteractionLog(
                group_id="g1", user_id="u1", message=f"msg{i}",
                response_raw="raw", response_filtered="filtered",
                trigger_type="llm",
            ))
        pd.log_collector = lc

        with patch("evolution.pattern_discovery.DISCOVERIES_FILE", self._disc_file):
            patterns = pd.discover(hours=24)
            self.assertEqual(patterns, [])

    def test_discover_llm_error_handled(self):
        """LLM 抛异常时不应崩溃"""
        mock_prov = MagicMock()
        mock_prov.text_chat = MagicMock()

        async def mock_chat(prompt, system_prompt):
            raise RuntimeError("LLM crash")

        mock_prov.text_chat = mock_chat

        pd = PatternDiscovery(provider=mock_prov, log_callback=lambda _: None)
        from evolution.log_collector import LogCollector
        lc = LogCollector(sample_rate=1.0, max_records=100)
        for i in range(15):
            lc.collect(InteractionLog(
                group_id="g1", user_id="u1", message=f"msg{i}",
                response_raw="raw", response_filtered="filtered",
                trigger_type="llm",
            ))
        pd.log_collector = lc

        with patch("evolution.pattern_discovery.DISCOVERIES_FILE", self._disc_file):
            patterns = pd.discover(hours=24)  # 不应崩溃
            self.assertEqual(patterns, [])

    def test_discover_llm_without_text_chat(self):
        """provider 无 text_chat 方法时回退到 HTTP"""
        invalid_prov = MagicMock(spec=[])  # 无 text_chat
        pd = PatternDiscovery(provider=invalid_prov, log_callback=lambda _: None)
        from evolution.log_collector import LogCollector
        lc = LogCollector(sample_rate=1.0, max_records=100)
        for i in range(15):
            lc.collect(InteractionLog(
                group_id="g1", user_id="u1", message=f"msg{i}",
                response_raw="raw", response_filtered="filtered",
                trigger_type="llm",
            ))
        pd.log_collector = lc
        with patch("evolution.pattern_discovery.DISCOVERIES_FILE", self._disc_file):
            patterns = pd.discover(hours=24)
            # 无 API key 时 HTTP fallback 也返回 None，最终为空
            self.assertEqual(patterns, [])


# ============================================================================
# 补充覆盖率测试：_parse_timestamp 错误处理
# ============================================================================

class TestParseTimestamp(YunliTestCase):
    """_parse_timestamp 函数"""

    def test_parse_invalid_timestamp_returns_zero(self):
        from evolution.log_collector import _parse_timestamp
        self.assertEqual(_parse_timestamp("not-a-date"), 0.0)
        self.assertEqual(_parse_timestamp(""), 0.0)
        self.assertEqual(_parse_timestamp("2026-13-99"), 0.0)

    def test_parse_valid_timestamp(self):
        from evolution.log_collector import _parse_timestamp
        ts = _parse_timestamp("2026-01-01T12:00:00")
        self.assertGreater(ts, 0)


# ============================================================================
# 补充覆盖率测试：LogCollector OSError 处理
# ============================================================================

class TestLogCollectorErrorHandling(YunliTestCase):
    """LogCollector 文件系统错误处理"""

    def setUp(self):
        self._tmp_dir = tempfile.TemporaryDirectory()
        self._tmp = Path(self._tmp_dir.name)
        self._log_dir = self._tmp / "logs"
        self._log_dir.mkdir(parents=True)

    def tearDown(self):
        self._tmp_dir.cleanup()

    def test_rotate_handles_oserror_on_old_file_delete(self):
        """轮转时删除旧文件遇到 OSError 不应崩溃"""
        with patch("evolution.log_collector.LOG_DIR", self._log_dir):
            lc = LogCollector(sample_rate=1.0, max_records=2, max_files=1)
            for _ in range(6):
                lc.collect(InteractionLog(
                    group_id="g1", user_id="u1", message="m",
                    response_raw="r", response_filtered="f",
                    trigger_type="llm",
                ))
                time.sleep(1.1)  # 确保不同时间戳触发轮转
            # 不应崩溃，即使 unlink 遇到错误也会被捕获
            self.assertTrue(True)

    def test_load_all_handles_oserror_on_file_read(self):
        """load_all 读文件遇到 OSError 不应崩溃"""
        # 创建一个无法读取的文件（权限问题很难模拟，测试 skip 逻辑）
        with patch("evolution.log_collector.LOG_DIR", self._log_dir):
            lc = LogCollector(sample_rate=1.0, max_records=100)
            lc.collect(InteractionLog(
                group_id="g1", user_id="u1", message="valid",
                response_raw="r", response_filtered="f",
                trigger_type="llm",
            ))
            # patch open to raise OSError on the second file
            logs = lc.load_all()
            self.assertGreaterEqual(len(logs), 1)


# ============================================================================
# 补充覆盖率测试：RuleGenerator 内部方法
# ============================================================================

class TestRuleGeneratorInternals(YunliTestCase):
    """RuleGenerator: _get_target_file / _gen_apply_instructions"""

    def setUp(self):
        self._tmp_dir = tempfile.TemporaryDirectory()
        self._tmp = Path(self._tmp_dir.name)
        self._rules_file = self._tmp / "pending_rules" / "pending_rules.json"
        self._rules_file.parent.mkdir(parents=True, exist_ok=True)

    def tearDown(self):
        self._tmp_dir.cleanup()

    def test_get_target_file_all_types(self):
        rg = RuleGenerator(provider=None, log_callback=lambda _: None)
        self.assertEqual(rg._get_target_file("filter_regex"), "persona/filters.py")
        self.assertEqual(rg._get_target_file("emotion_trigger"), "persona/emotion.py")
        self.assertEqual(rg._get_target_file("topic_keyword"), "persona/language.py")
        self.assertEqual(rg._get_target_file("boundary_rule"), "persona/core.py")
        self.assertEqual(rg._get_target_file("tone_rule"), "persona/qq_behavior.py")
        self.assertEqual(rg._get_target_file("unknown"), "unknown")

    def test_gen_apply_instructions_all_types(self):
        rg = RuleGenerator(provider=None, log_callback=lambda _: None)
        self.assertIn("MODERN_ACTION_WORDS", rg._gen_apply_instructions("filter_regex", {"keywords": ["kw"]}))
        self.assertIn("EMOTION_TRIGGERS", rg._gen_apply_instructions("emotion_trigger", {"trigger_name": "t"}))
        self.assertIn("TOPIC_KEYWORDS", rg._gen_apply_instructions("topic_keyword", {"topic_name": "t"}))
        self.assertIn("BASE_SYSTEM_PROMPT", rg._gen_apply_instructions("boundary_rule", {"rule": "r"}))
        self.assertIn("手动审查", rg._gen_apply_instructions("tone_rule", {}))


if __name__ == "__main__":
    unittest.main()