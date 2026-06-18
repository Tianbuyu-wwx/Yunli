"""Darwin 进化系统 端到端集成测试

覆盖:
  - 完整 baseline → evolve → apply 流程（mock LLM）
  - Phase 2: log → analyze → discover → accept → generate 流程
  - 多资产并发处理
  - 数据一致性（baseline.json ↔ assets ↔ runtime）
  - 异常恢复（缺文件、损坏JSON、不存在的资产）
  - 棘轮机制端到端验证
"""

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

test_dir = os.path.dirname(os.path.abspath(__file__))
yunli_dir = os.path.dirname(test_dir)
parent_dir = os.path.dirname(yunli_dir)
for p in [parent_dir, yunli_dir, test_dir]:
    if p not in sys.path:
        sys.path.insert(0, p)

sys.path.insert(0, os.path.join(yunli_dir, "evolution"))

from test_base import YunliTestCase
import darwin_evolve as de
from evolution.eval.rubric import RUBRIC, DimensionScore

# 无日志模式：抑制所有 print 输出
_QUIET = lambda _: None


class TestDarwinEndToEndFlow(YunliTestCase):
    """完整 baseline → evolve → apply 流程"""

    def setUp(self):
        self._tmp_dir = tempfile.TemporaryDirectory()
        self._tmp = Path(self._tmp_dir.name)

        # 重定向所有路径常量
        self._orig_assets = de.ASSETS_DIR
        self._orig_baseline = de.BASELINE_FILE
        self._orig_log = de.EVOLUTION_LOG

        de.ASSETS_DIR = self._tmp / "assets"
        de.ASSETS_DIR.mkdir(exist_ok=True)
        de.BASELINE_FILE = self._tmp / "baseline.json"
        de.EVOLUTION_LOG = self._tmp / "evolution_log.md"

        # 创建测试资产
        for name in ["system_prompt", "review_rules"]:
            de.save_asset(name, f"<{name}> 测试资产内容 v1</{name}>")

    def tearDown(self):
        de.ASSETS_DIR = self._orig_assets
        de.BASELINE_FILE = self._orig_baseline
        de.EVOLUTION_LOG = self._orig_log
        self._tmp_dir.cleanup()

    def test_full_baseline_evolve_report_cycle(self):
        """完整周期: baseline → evolve → report"""
        engine = de.DarwinEvolution(provider=None, config={
            "evolution_assets": ["system_prompt", "review_rules"],
        }, log_callback=_QUIET)

        # Phase 1: baseline
        baseline = engine.run_baseline()
        self.assertIn("system_prompt", baseline)
        self.assertIn("review_rules", baseline)
        self.assertTrue(de.BASELINE_FILE.exists())

        # Phase 2: evolve
        results = engine.run_evolve(max_iterations=1)
        self.assertEqual(len(results), 2)

        # Phase 3: report
        report = engine.run_report()
        self.assertIn("system_prompt", report)
        self.assertIn("review_rules", report)

    def test_baseline_is_persisted(self):
        """v2.2.0：mock baseline 时 total_score=-1（哨兵值）"""
        engine = de.DarwinEvolution(provider=None, config={
            "evolution_assets": ["system_prompt"],
        }, log_callback=_QUIET)
        engine.run_baseline()

        # baseline 文件应该在磁盘上
        self.assertTrue(de.BASELINE_FILE.exists())
        data = json.loads(de.BASELINE_FILE.read_text(encoding="utf-8"))
        self.assertIn("system_prompt", data)
        # v2.2.0：mock 评分时 total_score=-1（哨兵值，表示"无效评分"）
        self.assertEqual(data["system_prompt"]["total_score"], -1.0)

    def test_evolution_log_is_created(self):
        engine = de.DarwinEvolution(provider=None, config={
            "evolution_assets": ["system_prompt"],
        }, log_callback=_QUIET)
        engine.run_baseline()
        self.assertTrue(de.EVOLUTION_LOG.exists())
        content = de.EVOLUTION_LOG.read_text(encoding="utf-8")
        self.assertIn("基线评估", content)

    def test_asset_content_unchanged_after_mock_evolve(self):
        """mock LLM 不会真正改进，资产内容应保持不变"""
        original = de.load_asset("system_prompt")
        engine = de.DarwinEvolution(provider=None, config={
            "evolution_assets": ["system_prompt"],
        }, log_callback=_QUIET)
        engine.evolve_single_asset("system_prompt", max_iterations=1)
        after = de.load_asset("system_prompt")
        self.assertEqual(original, after, "mock evolve 不应改变资产内容")


class TestDarwinPhase2EndToEnd(YunliTestCase):
    """Phase 2 端到端: log → discover → accept → generate → report"""

    def setUp(self):
        self._tmp_dir = tempfile.TemporaryDirectory()
        self._tmp = Path(self._tmp_dir.name)

        from evolution.log_collector import InteractionLog, LogCollector
        from evolution.pattern_discovery import DiscoveredPattern, PatternDiscovery
        from evolution.rule_generator import RuleProposal, RuleGenerator

        self.InteractionLog = InteractionLog
        self.LogCollector = LogCollector
        self.DiscoveredPattern = DiscoveredPattern
        self.PatternDiscovery = PatternDiscovery
        self.RuleProposal = RuleProposal
        self.RuleGenerator = RuleGenerator

        # 创建独立的 Phase2 组件（patch LOG_DIR 到临时目录）
        import evolution.log_collector as lc_mod
        self._orig_lc_log_dir = lc_mod.LOG_DIR
        lc_mod.LOG_DIR = self._tmp / "logs"
        lc_mod.LOG_DIR.mkdir(parents=True, exist_ok=True)
        self._lc_log_dir = lc_mod.LOG_DIR

        self.log_collector = LogCollector(
            sample_rate=1.0,
            max_records=100,
        )
        self.pattern_disc = PatternDiscovery(provider=None)
        self._disc_file = self._tmp / "discoveries.json"
        self.rule_gen = RuleGenerator(provider=None, log_callback=lambda _: None)
        self._rules_file = self._tmp / "pending_rules.json"

    def tearDown(self):
        # 恢复 LOG_DIR
        import evolution.log_collector as lc_mod
        lc_mod.LOG_DIR = self._orig_lc_log_dir
        self._tmp_dir.cleanup()

    def _add_log(self, group_id, user_id, message, response_raw, response_filtered, trigger="llm"):
        log = self.InteractionLog(
            group_id=group_id, user_id=user_id, user_nickname="test_user",
            message=message, response_raw=response_raw,
            response_filtered=response_filtered, emotion_state="neutral",
            trigger_type=trigger,
        )
        self.log_collector.collect(log)

    def test_full_phase2_workflow(self):
        """完整 Phase 2 工作流"""
        # Step 1: 采集日志
        self._add_log("g1", "u1", "你好", "（思考）你好啊", "你好啊")
        self._add_log("g1", "u1", "你是谁", "我是云璃", "我是云璃")
        self._add_log("g1", "u2", "再见", "（挥手）再见", "再见")
        stats = self.log_collector.get_stats()
        self.assertEqual(stats["total_collected"], 3)

        # Step 2: 检查 JSONL 格式
        log_files = list(self._lc_log_dir.glob("interactions_*.jsonl"))
        self.assertGreater(len(log_files), 0)

        # Step 3: 创建发现的模式
        pat = self.DiscoveredPattern(
            pattern_id="pat_fe_001",
            category="filter_escape",
            description="用户输出含（思考）格式注入",
            severity="high",
            examples=["（思考）你好啊"],
            suggested_fix="过滤中文括号内容",
            accepted=False,
            reviewed=False,
        )
        import evolution.pattern_discovery as pd_mod
        with patch("evolution.pattern_discovery.DISCOVERIES_FILE", self._disc_file):
            self.pattern_disc._save([pat])
            loaded = self.pattern_disc.load_all()
            self.assertEqual(len(loaded), 1)

        # Step 4: 接受模式 → 生成规则
        with patch("evolution.pattern_discovery.DISCOVERIES_FILE", self._disc_file):
            self.pattern_disc.accept("pat_fe_001")
            accepted = [p for p in self.pattern_disc.load_all() if p.accepted]
            self.assertEqual(len(accepted), 1)

            rp = self.RuleProposal(
                rule_id="rule_001",
                rule_type="filter_regex",
                source_pattern_id="pat_fe_001",
                target_file="persona/filters.py",
                rule_content={"type": "filter_regex", "pattern": r"（.*?）", "keywords": ["思考"]},
                apply_instructions="在 filters.py 中添加过滤规则",
            )
            import evolution.rule_generator as rg_mod
            with patch("evolution.rule_generator.RULES_FILE", self._rules_file):
                self.rule_gen._save([rp])
                rules = self.rule_gen.load_all()
                self.assertEqual(len(rules), 1)
                self.assertEqual(rules[0].rule_type, "filter_regex")

        # Step 5: 获取报告
        with patch("evolution.rule_generator.RULES_FILE", self._rules_file):
            report = self.rule_gen.get_report()
            self.assertIn("filter_regex", report)


class TestDarwinResilience(YunliTestCase):
    """异常恢复与边界测试"""

    def setUp(self):
        self._tmp_dir = tempfile.TemporaryDirectory()
        self._tmp = Path(self._tmp_dir.name)

        self._orig_assets = de.ASSETS_DIR
        self._orig_baseline = de.BASELINE_FILE
        self._orig_log = de.EVOLUTION_LOG

        de.ASSETS_DIR = self._tmp / "assets"
        de.ASSETS_DIR.mkdir(exist_ok=True)
        de.BASELINE_FILE = self._tmp / "baseline.json"
        de.EVOLUTION_LOG = self._tmp / "evolution_log.md"

    def tearDown(self):
        de.ASSETS_DIR = self._orig_assets
        de.BASELINE_FILE = self._orig_baseline
        de.EVOLUTION_LOG = self._orig_log
        self._tmp_dir.cleanup()

    def test_baseline_with_no_assets_no_crash(self):
        engine = de.DarwinEvolution(provider=None, config={
            "evolution_assets": ["system_prompt"],
        }, log_callback=_QUIET)
        baseline = engine.run_baseline()
        self.assertEqual(baseline, {}, "无资产时基线应为空字典")

    def test_evolve_nonexistent_asset_no_crash(self):
        """不存在的资产在 init 时被过滤掉，不应崩溃"""
        engine = de.DarwinEvolution(provider=None, config={
            "evolution_assets": ["nonexistent"],
        }, log_callback=_QUIET)
        # nonexistent 不在 ASSET_NAMES 中，被过滤后 enabled_assets = []
        self.assertEqual(engine.enabled_assets, [])
        results = engine.run_evolve(max_iterations=0)
        self.assertEqual(results, {})

    def test_report_with_corrupted_baseline_no_crash(self):
        de.BASELINE_FILE.write_text("{corrupted json")
        engine = de.DarwinEvolution(provider=None, config={}, log_callback=_QUIET)
        report = engine.run_report()
        self.assertIn("未找到基线数据", report)

    def test_report_with_empty_baseline_no_crash(self):
        de.BASELINE_FILE.write_text("{}")
        engine = de.DarwinEvolution(provider=None, config={}, log_callback=_QUIET)
        report = engine.run_report()
        self.assertIn("未找到", report)

    def test_multi_asset_baseline_consistency(self):
        """多资产基线数据一致性验证"""
        for name in de.ASSET_NAMES[:3]:
            de.save_asset(name, f"content of {name}")

        engine = de.DarwinEvolution(provider=None, config={
            "evolution_assets": de.ASSET_NAMES[:3],
        }, log_callback=_QUIET)
        baseline = engine.run_baseline()

        for name in de.ASSET_NAMES[:3]:
            self.assertIn(name, baseline, f"基线缺少资产: {name}")
            self.assertIn("total_score", baseline[name])
            self.assertIn("dimensions", baseline[name])
            self.assertIn("scored_at", baseline[name])

    def test_ten_dimensions_in_baseline(self):
        """每个资产的基线评分必须包含全部 10 个维度"""
        de.save_asset("system_prompt", "content")
        engine = de.DarwinEvolution(provider=None, config={
            "evolution_assets": ["system_prompt"],
        }, log_callback=_QUIET)
        baseline = engine.run_baseline()
        dims = baseline["system_prompt"]["dimensions"]
        self.assertEqual(len(dims), 10, f"期望 10 维度，实际 {len(dims)}")


class TestDarwinProviderIntegration(YunliTestCase):
    """provider 集成测试（mock provider）"""

    def setUp(self):
        self._tmp_dir = tempfile.TemporaryDirectory()
        self._tmp = Path(self._tmp_dir.name)
        self._orig_assets = de.ASSETS_DIR
        self._orig_baseline = de.BASELINE_FILE
        de.ASSETS_DIR = self._tmp / "assets"
        de.ASSETS_DIR.mkdir(exist_ok=True)
        de.BASELINE_FILE = self._tmp / "baseline.json"

    def tearDown(self):
        de.ASSETS_DIR = self._orig_assets
        de.BASELINE_FILE = self._orig_baseline
        self._tmp_dir.cleanup()

    def test_score_with_mock_provider(self):
        """mock provider 返回有效评分"""
        mock_provider = unittest.mock.MagicMock()
        # provider.text_chat 是 async 方法
        import asyncio

        async def mock_text_chat(prompt, system_prompt):
            return mock_provider.return_value

        mock_provider.text_chat = mock_text_chat

        # 构造评分 JSON
        dims = [{"dim_key": k, "score": 8.0, "issues": [], "suggestions": []} for k in RUBRIC]
        response = json.dumps({"dimensions": dims, "overall_comment": "excellent"})
        mock_provider.return_value = type("LLMResponse", (), {"completion_text": response})()

        de.save_asset("system_prompt", "test content")
        engine = de.DarwinEvolution(provider=mock_provider, config={
            "evolution_assets": ["system_prompt"],
        }, log_callback=_QUIET)

        score = engine.score_asset("system_prompt", "test content")
        # 所有维度 8.0 分，总分应接近 80
        self.assertIsInstance(score, de.AssetScore)
        self.assertAlmostEqual(score.total_score, 80.0, delta=2.0)

    def test_score_with_provider_returning_string(self):
        """provider 返回纯字符串而非 LLMResponse 对象"""
        mock_provider = unittest.mock.MagicMock()

        async def mock_text_chat(prompt, system_prompt):
            return mock_provider.return_value

        mock_provider.text_chat = mock_text_chat

        dims = [{"dim_key": k, "score": 7.5, "issues": [], "suggestions": []} for k in RUBRIC]
        mock_provider.return_value = json.dumps({"dimensions": dims, "overall_comment": "ok"})

        de.save_asset("system_prompt", "test")
        engine = de.DarwinEvolution(provider=mock_provider, config={
            "evolution_assets": ["system_prompt"],
        }, log_callback=_QUIET)

        score = engine.score_asset("system_prompt", "test")
        self.assertAlmostEqual(score.total_score, 75.0, delta=2.0)

    def test_score_with_provider_returning_none_falls_back_to_mock(self):
        """provider 返回 None → 降级到 mock 评分（v2.2.0：total_score=-1 哨兵值）"""
        mock_provider = unittest.mock.MagicMock()

        async def mock_text_chat(prompt, system_prompt):
            return None

        mock_provider.text_chat = mock_text_chat

        engine = de.DarwinEvolution(provider=mock_provider, config={}, log_callback=_QUIET)
        score = engine.score_asset("test", "content")
        # v2.2.0：mock 评分时 total_score=-1（哨兵值）
        self.assertEqual(score.total_score, -1.0)
        self.assertIn("模拟", score.overall_comment)


if __name__ == "__main__":
    unittest.main()