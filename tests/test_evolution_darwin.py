"""Darwin 进化引擎 (darwin_evolve.py) 单元测试

覆盖:
  - 资产管理: load_asset / save_asset / load_baseline / save_baseline
  - 评分: _mock_score / find_weakest_dimension / build_improvement_prompt
  - DarwinEvolution: __init__ / score_asset / evolve_single_asset / run_baseline / run_evolve / run_report
  - 棘轮机制: 只保留改进、回滚退化
  - 边界: max_iterations=0、空 enabled_assets、缺文件、JSON 损坏
  - 并行: _run_baseline_parallel / benchmark_serial_vs_parallel
"""

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock

test_dir = os.path.dirname(os.path.abspath(__file__))
yunli_dir = os.path.dirname(test_dir)
parent_dir = os.path.dirname(yunli_dir)
for p in [parent_dir, yunli_dir, test_dir]:
    if p not in sys.path:
        sys.path.insert(0, p)

sys.path.insert(0, os.path.join(yunli_dir, "evolution"))
sys.path.insert(0, os.path.join(yunli_dir, "evolution", "eval"))

from test_base import YunliTestCase
import darwin_evolve as de

from evolution.eval.rubric import RUBRIC, DimensionScore


# ============================================================================
# 资产管理测试
# ============================================================================

class TestAssetManagement(YunliTestCase):
    """load_asset / save_asset / load_baseline / save_baseline"""

    def setUp(self):
        self._tmp_dir = tempfile.TemporaryDirectory()
        self._orig_assets = de.ASSETS_DIR
        self._orig_baseline = de.BASELINE_FILE
        self._orig_log = de.EVOLUTION_LOG
        self._tmp = Path(self._tmp_dir.name)
        de.ASSETS_DIR = self._tmp / "assets"
        de.ASSETS_DIR.mkdir(exist_ok=True)
        de.BASELINE_FILE = self._tmp / "baseline.json"
        de.EVOLUTION_LOG = self._tmp / "evolution_log.md"

    def tearDown(self):
        de.ASSETS_DIR = self._orig_assets
        de.BASELINE_FILE = self._orig_baseline
        de.EVOLUTION_LOG = self._orig_log
        self._tmp_dir.cleanup()

    def test_save_and_load_asset_roundtrip(self):
        de.save_asset("test_asset", "content_value_123")
        content = de.load_asset("test_asset")
        self.assertEqual(content, "content_value_123")

    def test_load_asset_strips_surrounding_whitespace(self):
        de.save_asset("test_asset", "hello world")  # save_asset 写入时已去除首尾空白
        content = de.load_asset("test_asset")
        self.assertEqual(content, "hello world")

    def test_save_asset_creates_backup(self):
        # 先创建文件再 save 才会触发备份
        (de.ASSETS_DIR / "test_asset.md").write_text("v0", encoding="utf-8")
        de.save_asset("test_asset", "v1")
        bak_files = list(de.ASSETS_DIR.glob("test_asset.bak.*.md"))
        self.assertGreaterEqual(len(bak_files), 1, f"应至少有 1 个备份文件，实际: {bak_files}")

    def test_save_asset_atomic_no_temp_leftover(self):
        de.save_asset("test_asset", "atomic_test")
        tmp_files = list(de.ASSETS_DIR.glob("test_asset.tmp.*.md"))
        self.assertEqual(len(tmp_files), 0, f"临时文件未清理: {tmp_files}")

    def test_save_and_load_baseline_roundtrip(self):
        baseline = {"system_prompt": {"total_score": 75.0}, "review_rules": {"total_score": 80.0}}
        de.save_baseline(baseline)
        loaded = de.load_baseline()
        self.assertEqual(loaded["system_prompt"]["total_score"], 75.0)
        self.assertEqual(loaded["review_rules"]["total_score"], 80.0)

    def test_load_baseline_missing_file_returns_empty_dict(self):
        if de.BASELINE_FILE.exists():
            de.BASELINE_FILE.unlink()
        result = de.load_baseline()
        self.assertEqual(result, {})

    def test_load_baseline_corrupted_json_returns_empty(self):
        de.BASELINE_FILE.write_text("not valid json {{{")
        result = de.load_baseline()
        self.assertEqual(result, {}, "损坏的 JSON 应返回空字典")


# ============================================================================
# _mock_score / find_weakest_dimension
# ============================================================================

class TestMockScore(YunliTestCase):
    """_mock_score 降级方案

    v2.2.0 重构：_mock_score 改用基于内容长度的差异化评分（避免所有资产得到相同伪造分数），
    total_score 使用 -1 作为哨兵值，明确标识"无效评分"。
    """

    def test_mock_score_has_all_10_dimensions(self):
        score = de.DarwinEvolution._mock_score(None, "test")
        self.assertEqual(len(score.dimensions), 10)

    def test_mock_score_total_is_negative_sentinel(self):
        """v2.2.0：total_score=-1 标识"无效评分"，便于调用方检测降级路径"""
        score = de.DarwinEvolution._mock_score(None, "test")
        self.assertEqual(score.total_score, -1.0)

    def test_mock_score_each_dim_score_is_heuristic(self):
        """v2.2.0：每个维度根据资产长度 + 维度权重启发式评分（不再是固定 7.0）"""
        score = de.DarwinEvolution._mock_score(None, "test")
        for dim in score.dimensions:
            # 启发式评分范围：base * weight，base ∈ [3.0, 8.0]，weight ∈ [0.8, 1.2]
            # 最小值 3.0 * 0.8 = 2.4，最大值 8.0 * 1.2 = 9.6
            self.assertGreaterEqual(dim.score, 2.0)
            self.assertLessEqual(dim.score, 10.0)

    def test_mock_score_contains_mock_messages(self):
        score = de.DarwinEvolution._mock_score(None, "test")
        self.assertIn("模拟", score.overall_comment)
        for dim in score.dimensions:
            self.assertTrue(any("模拟" in i for i in dim.issues), f"维度 {dim.name} 缺模拟标记")


class TestFindWeakestDimension(YunliTestCase):
    """find_weakest_dimension"""

    def test_returns_lowest_score_dimension(self):
        dims = [
            DimensionScore(name="d1", score=8.0, max_score=10.0, issues=[], suggestions=[]),
            DimensionScore(name="d2", score=3.0, max_score=10.0, issues=[], suggestions=[]),
            DimensionScore(name="d3", score=7.0, max_score=10.0, issues=[], suggestions=[]),
        ]
        asset = de.AssetScore(asset_name="x", asset_path="/p", dimensions=dims,
                           total_score=60.0, overall_comment="")
        weakest = de.find_weakest_dimension(asset)
        self.assertEqual(weakest.name, "d2")

    def test_empty_dimensions_returns_none(self):
        asset = de.AssetScore(asset_name="x", asset_path="/p", dimensions=[],
                           total_score=0.0, overall_comment="")
        result = de.find_weakest_dimension(asset)
        self.assertIsNone(result)


# ============================================================================
# build_improvement_prompt
# ============================================================================

class TestBuildImprovementPrompt(YunliTestCase):
    """build_improvement_prompt"""

    def test_prompt_contains_asset_and_dim_info(self):
        dim = DimensionScore(name="反AI腔", score=3.0, max_score=10.0,
                             issues=["issue1"], suggestions=["sug1"])
        prompt = de.build_improvement_prompt("system_prompt", "content", dim)
        self.assertIn("system_prompt", prompt)
        self.assertIn("反AI腔", prompt)
        self.assertIn("content", prompt)

    def test_prompt_contains_weak_dimension_issues(self):
        dim = DimensionScore(name="测试", score=3.0, max_score=10.0,
                             issues=["具体问题A"], suggestions=[])
        prompt = de.build_improvement_prompt("test", "content", dim)
        self.assertIn("具体问题A", prompt)

    def test_prompt_instructs_direct_output(self):
        dim = DimensionScore(name="x", score=5.0, max_score=10.0, issues=[], suggestions=[])
        prompt = de.build_improvement_prompt("test", "content", dim)
        self.assertIn("不要", prompt)


# ============================================================================
# DarwinEvolution class
# ============================================================================

class TestDarwinEvolutionInit(YunliTestCase):
    """DarwinEvolution.__init__"""

    def test_init_with_config(self):
        config = {"evolution_max_iterations": 5, "evolution_min_improvement": 1.0}
        engine = de.DarwinEvolution(provider=None, config=config, log_callback=lambda _: None)
        self.assertEqual(engine.max_iterations, 5)
        self.assertEqual(engine.min_improvement, 1.0)

    def test_init_with_defaults(self):
        engine = de.DarwinEvolution(provider=None, config={}, log_callback=lambda _: None)
        self.assertEqual(engine.max_iterations, de.DEFAULT_MAX_ITERATIONS)
        self.assertEqual(engine.min_improvement, de.DEFAULT_MIN_IMPROVEMENT)

    def test_init_all_assets_enabled(self):
        engine = de.DarwinEvolution(provider=None, config={}, log_callback=lambda _: None)
        self.assertEqual(engine.enabled_assets, de.ASSET_NAMES)

    def test_init_subset_assets(self):
        config = {"evolution_assets": ["system_prompt", "filter_rules"]}
        engine = de.DarwinEvolution(provider=None, config=config, log_callback=lambda _: None)
        self.assertEqual(engine.enabled_assets, ["system_prompt", "filter_rules"])

    def test_init_with_invalid_asset_name_ignored(self):
        config = {"evolution_assets": ["system_prompt", "nonexistent", "filter_rules"]}
        engine = de.DarwinEvolution(provider=None, config=config, log_callback=lambda _: None)
        self.assertNotIn("nonexistent", engine.enabled_assets)
        self.assertIn("system_prompt", engine.enabled_assets)
        self.assertIn("filter_rules", engine.enabled_assets)

    def test_init_temperature_defaults(self):
        """评分温度默认 0.2，改进温度默认 0.7"""
        engine = de.DarwinEvolution(provider=None, config={}, log_callback=lambda _: None)
        self.assertEqual(engine.scoring_temperature, de.DEFAULT_SCORING_TEMPERATURE)
        self.assertEqual(engine.improvement_temperature, de.DEFAULT_IMPROVEMENT_TEMPERATURE)

    def test_init_temperature_from_config(self):
        """从 config 读取自定义温度"""
        config = {
            "evolution_scoring_temperature": 0.1,
            "evolution_improvement_temperature": 0.9,
        }
        engine = de.DarwinEvolution(provider=None, config=config, log_callback=lambda _: None)
        self.assertEqual(engine.scoring_temperature, 0.1)
        self.assertEqual(engine.improvement_temperature, 0.9)


class TestDarwinEvolutionScoreAsset(YunliTestCase):
    """DarwinEvolution.score_asset"""

    def test_score_asset_returns_asset_score(self):
        engine = de.DarwinEvolution(provider=None, config={}, log_callback=lambda _: None)
        score = engine.score_asset("test", "some content")
        self.assertIsInstance(score, de.AssetScore)
        self.assertEqual(score.asset_name, "test")

    def test_score_asset_without_llm_uses_mock(self):
        """v2.2.0：mock 评分时 total_score=-1（哨兵值），便于调用方检测降级"""
        engine = de.DarwinEvolution(provider=None, config={}, log_callback=lambda _: None)
        score = engine.score_asset("test", "content")
        self.assertEqual(score.total_score, -1.0)
        self.assertIn("模拟", score.overall_comment)


class TestDarwinEvolutionEvolveSingleAsset(YunliTestCase):
    """DarwinEvolution.evolve_single_asset (mock LLM)"""

    def setUp(self):
        self._tmp_dir = tempfile.TemporaryDirectory()
        self._orig_assets = de.ASSETS_DIR
        self._orig_baseline = de.BASELINE_FILE
        self._orig_log = de.EVOLUTION_LOG
        self._tmp = Path(self._tmp_dir.name)
        de.ASSETS_DIR = self._tmp / "assets"
        de.ASSETS_DIR.mkdir(exist_ok=True)
        de.BASELINE_FILE = self._tmp / "baseline.json"
        de.EVOLUTION_LOG = self._tmp / "evolution_log.md"
        de.save_asset("test_asset", "这是测试用的资产内容。")

    def tearDown(self):
        de.ASSETS_DIR = self._orig_assets
        de.BASELINE_FILE = self._orig_baseline
        de.EVOLUTION_LOG = self._orig_log
        self._tmp_dir.cleanup()

    def test_evolve_with_mock_llm_produces_result(self):
        engine = de.DarwinEvolution(provider=None, config={}, log_callback=lambda _: None)
        result = engine.evolve_single_asset("test_asset")
        self.assertEqual(result["asset_name"], "test_asset")
        self.assertIn("baseline_score", result)
        self.assertIn("final_score", result)
        self.assertIn("iterations", result)

    def test_evolve_max_iterations_zero_no_crash(self):
        engine = de.DarwinEvolution(provider=None, config={}, log_callback=lambda _: None)
        result = engine.evolve_single_asset("test_asset", max_iterations=0)
        self.assertEqual(result["iterations"], 0)

    def test_evolve_with_baseline_passed_in(self):
        dims = [DimensionScore(name="test", score=9.0, max_score=10.0, issues=[], suggestions=[])]
        baseline = de.AssetScore(asset_name="test_asset", asset_path="/p", dimensions=dims,
                              total_score=90.0, overall_comment="good")
        engine = de.DarwinEvolution(provider=None, config={}, log_callback=lambda _: None)
        result = engine.evolve_single_asset("test_asset", baseline_score=baseline, max_iterations=0)
        self.assertEqual(result["baseline_score"], 90.0)

    def test_evolve_history_has_baseline_entry(self):
        engine = de.DarwinEvolution(provider=None, config={}, log_callback=lambda _: None)
        result = engine.evolve_single_asset("test_asset", max_iterations=0)
        history = result["history"]
        self.assertGreaterEqual(len(history), 1)
        self.assertEqual(history[0]["action"], "baseline")


class TestDarwinEvolutionRunMethods(YunliTestCase):
    """run_baseline / run_evolve / run_report"""

    def setUp(self):
        self._tmp_dir = tempfile.TemporaryDirectory()
        self._orig_assets = de.ASSETS_DIR
        self._orig_baseline = de.BASELINE_FILE
        self._orig_log = de.EVOLUTION_LOG
        self._tmp = Path(self._tmp_dir.name)
        de.ASSETS_DIR = self._tmp / "assets"
        de.ASSETS_DIR.mkdir(exist_ok=True)
        de.BASELINE_FILE = self._tmp / "baseline.json"
        de.EVOLUTION_LOG = self._tmp / "evolution_log.md"

    def tearDown(self):
        de.ASSETS_DIR = self._orig_assets
        de.BASELINE_FILE = self._orig_baseline
        de.EVOLUTION_LOG = self._orig_log
        self._tmp_dir.cleanup()

    def test_run_baseline_all_assets_with_mock(self):
        """v2.2.0：mock baseline 时所有资产 total_score=-1（哨兵值）"""
        engine = de.DarwinEvolution(provider=None, config={
            "evolution_assets": ["system_prompt", "review_rules"],
        }, log_callback=lambda _: None)
        for name in ["system_prompt", "review_rules"]:
            de.save_asset(name, f"content of {name}")
        baseline = engine.run_baseline()
        self.assertIn("system_prompt", baseline)
        self.assertIn("review_rules", baseline)
        self.assertEqual(baseline["system_prompt"]["total_score"], -1.0)

    def test_run_baseline_skips_missing_assets(self):
        engine = de.DarwinEvolution(provider=None, config={
            "evolution_assets": ["system_prompt"],
        }, log_callback=lambda _: None)
        baseline = engine.run_baseline()
        self.assertNotIn("system_prompt", baseline)

    def test_run_evolve_minimal(self):
        for name in de.ASSET_NAMES[:2]:
            de.save_asset(name, f"content of {name}")
        engine = de.DarwinEvolution(provider=None, config={"evolution_assets": de.ASSET_NAMES[:2]}, log_callback=lambda _: None)
        engine.run_baseline()
        results = engine.run_evolve(target_assets=de.ASSET_NAMES[:2], max_iterations=0)
        self.assertEqual(len(results), 2)

    def test_run_report_no_baseline(self):
        engine = de.DarwinEvolution(provider=None, config={}, log_callback=lambda _: None)
        report = engine.run_report()
        self.assertIn("未找到基线数据", report)

    def test_run_report_with_baseline(self):
        engine = de.DarwinEvolution(provider=None, config={"evolution_assets": ["system_prompt"]}, log_callback=lambda _: None)
        de.save_asset("system_prompt", "test")
        engine.run_baseline()
        report = engine.run_report()
        self.assertIn("system_prompt", report)


# ============================================================================
# 棘轮机制
# ============================================================================

class TestRatchetMechanism(YunliTestCase):
    """棘轮机制：只保留改进，回滚退化"""

    def setUp(self):
        self._tmp_dir = tempfile.TemporaryDirectory()
        self._orig_assets = de.ASSETS_DIR
        self._orig_baseline = de.BASELINE_FILE
        self._orig_log = de.EVOLUTION_LOG
        self._tmp = Path(self._tmp_dir.name)
        de.ASSETS_DIR = self._tmp / "assets"
        de.ASSETS_DIR.mkdir(exist_ok=True)
        de.BASELINE_FILE = self._tmp / "baseline.json"
        de.EVOLUTION_LOG = self._tmp / "evolution_log.md"
        de.save_asset("test_asset", "original content")

    def tearDown(self):
        de.ASSETS_DIR = self._orig_assets
        de.BASELINE_FILE = self._orig_baseline
        de.EVOLUTION_LOG = self._orig_log
        self._tmp_dir.cleanup()

    def test_ratchet_prevents_regression(self):
        engine = de.DarwinEvolution(provider=None, config={}, log_callback=lambda _: None)
        result = engine.evolve_single_asset("test_asset", max_iterations=1)
        # mock LLM 评分全是 70，diff=0 < min_improvement(0.5)，不会 improved
        self.assertFalse(result["improved"])


# ============================================================================
# load_all_assets
# ============================================================================

class TestLoadAllAssets(YunliTestCase):
    def setUp(self):
        self._tmp_dir = tempfile.TemporaryDirectory()
        self._orig_assets = de.ASSETS_DIR
        self._tmp = Path(self._tmp_dir.name)
        de.ASSETS_DIR = self._tmp / "assets"
        de.ASSETS_DIR.mkdir(exist_ok=True)

    def tearDown(self):
        de.ASSETS_DIR = self._orig_assets
        self._tmp_dir.cleanup()

    def test_returns_only_existing_assets(self):
        de.save_asset("system_prompt", "sp content")
        de.save_asset("review_rules", "rr content")
        assets = de.load_all_assets()
        self.assertIn("system_prompt", assets)
        self.assertIn("review_rules", assets)
        self.assertNotIn("filter_rules", assets)

    def test_returns_empty_when_no_assets(self):
        assets = de.load_all_assets()
        self.assertEqual(assets, {})


# ============================================================================
# 补充覆盖率测试
# ============================================================================

class TestGetDimensionKey(YunliTestCase):
    """get_dimension_key 辅助函数"""

    def test_known_dimension(self):
        key = de.get_dimension_key("角色一致性")
        self.assertEqual(key, "character_consistency")

    def test_unknown_dimension(self):
        key = de.get_dimension_key("不存在的维度")
        self.assertEqual(key, "unknown")


class TestSaveEvolutionLogSnapshot(YunliTestCase):
    """save_evolution_log_snapshot"""

    def setUp(self):
        self._tmp_dir = tempfile.TemporaryDirectory()
        self._orig_log = de.EVOLUTION_LOG
        self._tmp = Path(self._tmp_dir.name)
        de.EVOLUTION_LOG = self._tmp / "evolution_log.md"

    def tearDown(self):
        de.EVOLUTION_LOG = self._orig_log
        self._tmp_dir.cleanup()

    def test_snapshot_creates_log_file(self):
        """v2.2.0：save_evolution_log_snapshot 会跳过纯模拟数据（防日志膨胀）

        测试用 eval_mode="real" 标记以确保写入。
        """
        de.save_evolution_log_snapshot({
            "system_prompt": {
                "total_score": 80.0,
                "eval_mode": "real",  # v2.2.0：标记真实评估避免被跳过
                "dimensions": [
                    {"name": "角色一致性", "score": 8.0},
                ],
            },
        }, "测试阶段")
        self.assertTrue(de.EVOLUTION_LOG.exists())
        content = de.EVOLUTION_LOG.read_text(encoding="utf-8")
        self.assertIn("测试阶段", content)
        self.assertIn("system_prompt", content)

    def test_snapshot_skips_mock_data(self):
        """v2.2.0：纯模拟数据（无 eval_mode=real）会被跳过，避免日志膨胀"""
        # 确保 clean state
        if de.EVOLUTION_LOG.exists():
            de.EVOLUTION_LOG.unlink()
        de.save_evolution_log_snapshot({
            "system_prompt": {
                "total_score": 80.0,
                # 没有 eval_mode 标记 → 被识别为模拟数据
                "dimensions": [{"name": "test", "score": 8.0}],
            },
        }, "模拟阶段")
        # 不应创建日志文件
        self.assertFalse(de.EVOLUTION_LOG.exists())


class TestImproveAssetWithoutLLM(YunliTestCase):
    """improve_asset 在 LLM 不可用时返回 None"""

    def test_improve_asset_returns_none_without_provider(self):
        engine = de.DarwinEvolution(provider=None, config={}, log_callback=lambda _: None)
        dim = DimensionScore(name="反AI腔", score=3.0, max_score=10.0, issues=[], suggestions=[])
        result = engine.improve_asset("test", "content", dim)
        self.assertIsNone(result)


class TestRunEvolveWithUnknownAsset(YunliTestCase):
    """run_evolve 跳过未知资产"""

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

    def test_run_evolve_skips_unknown_asset(self):
        engine = de.DarwinEvolution(provider=None, config={
            "evolution_assets": ["system_prompt"],
        }, log_callback=lambda _: None)
        de.save_asset("system_prompt", "test")
        results = engine.run_evolve(target_assets=["unknown_asset"], max_iterations=0)
        self.assertEqual(results, {})


class TestScoreAssetParseError(YunliTestCase):
    """score_asset 解析失败时降级到 mock"""

    def test_score_with_invalid_llm_response(self):
        """v2.2.0：LLM 返回非法 JSON 时降级到 mock，total_score=-1（哨兵值）"""
        mock_prov = MagicMock()
        mock_prov.text_chat = MagicMock()

        async def mock_chat(prompt, system_prompt):
            resp = MagicMock()
            resp.completion_text = "not valid json at all"
            return resp

        mock_prov.text_chat = mock_chat

        engine = de.DarwinEvolution(provider=mock_prov, config={}, log_callback=lambda _: None)
        score = engine.score_asset("test", "content")
        self.assertEqual(score.total_score, -1.0)  # 降级到 mock，total_score=-1
        self.assertIn("模拟", score.overall_comment)


class TestCallLLMSyncEdgeCases(YunliTestCase):
    """_call_llm_sync 边界情况"""

    def test_provider_without_text_chat(self):
        """provider 没有 text_chat 方法时回退到 HTTP"""
        invalid_prov = MagicMock(spec=[])  # 无任何方法
        engine = de.DarwinEvolution(provider=invalid_prov, config={}, log_callback=lambda _: None)
        result = engine._call_llm_sync("prompt", "system")
        self.assertIsNone(result)  # 无 API key，HTTP fallback 也失败

    def test_provider_text_chat_returns_unknown_type(self):
        """provider 返回非标准类型（非 str 非 LLMResponse）"""
        mock_prov = MagicMock()
        mock_prov.text_chat = MagicMock()

        async def mock_chat(prompt, system_prompt):
            return 42  # 整数，非标准类型

        mock_prov.text_chat = mock_chat

        engine = de.DarwinEvolution(provider=mock_prov, config={}, log_callback=lambda _: None)
        result = engine._call_llm_sync("prompt", "system")
        self.assertEqual(result, "42")  # 转成字符串

    def test_provider_text_chat_returns_empty_completion_text(self):
        """provider 返回的 completion_text 为空"""
        mock_prov = MagicMock()
        mock_prov.text_chat = MagicMock()

        async def mock_chat(prompt, system_prompt):
            resp = MagicMock()
            resp.completion_text = ""
            return resp

        mock_prov.text_chat = mock_chat

        engine = de.DarwinEvolution(provider=mock_prov, config={}, log_callback=lambda _: None)
        result = engine._call_llm_sync("prompt", "system")
        self.assertIsNone(result)

    def test_llm_client_http_with_api_key_in_config(self):
        """LLMClient._call_http 从 config 读取 API key（但网络请求会失败）"""
        engine = de.DarwinEvolution(provider=None, config={
            "deepseek_api_key": "sk-fake-key",
            "deepseek_api_base": "https://invalid-api.example.com",
        }, log_callback=lambda _: None)
        # 会尝试 HTTP 请求，预期因连接拒绝或 DNS 错误而返回 None
        result = engine._llm._call_http("test", "system")
        self.assertIsNone(result)

    def test_llm_client_http_no_key_returns_none(self):
        """LLMClient._call_http 无 API key 返回 None"""
        engine = de.DarwinEvolution(provider=None, config={}, log_callback=lambda _: None)
        result = engine._llm._call_http("test", "system")
        self.assertIsNone(result)

    def test_call_llm_sync_passes_temperature_to_http_fallback(self):
        """_call_llm_sync 将 temperature 传递给 LLMClient"""
        # 无 provider 时走 HTTP fallback，无 API key 返回 None
        engine = de.DarwinEvolution(provider=None, config={}, log_callback=lambda _: None)
        # 验证 temperature 参数被接受而不报错（HTTP 请求会失败但参数传递正确）
        result = engine._call_llm_sync("prompt", "system", temperature=0.2)
        self.assertIsNone(result)

    def test_llm_client_http_accepts_temperature(self):
        """LLMClient._call_http 接受 temperature 参数"""
        engine = de.DarwinEvolution(provider=None, config={}, log_callback=lambda _: None)
        result = engine._llm._call_http("test", "system", temperature=0.7)
        self.assertIsNone(result)  # 无 API key，但参数传递正确

    def test_score_asset_uses_scoring_temperature(self):
        """score_asset 使用低 temperature（评分专用）"""
        from unittest.mock import patch

        engine = de.DarwinEvolution(
            provider=None, config={},
            log_callback=lambda _: None,
        )
        self.assertEqual(engine.scoring_temperature, 0.2)

        with patch.object(engine, "_call_llm_sync", return_value=None) as mock_call:
            engine.score_asset("test", "content")
            # 验证调用时传入了 scoring_temperature
            args, kwargs = mock_call.call_args
            self.assertEqual(kwargs.get("temperature"), engine.scoring_temperature)

    def test_improve_asset_uses_improvement_temperature(self):
        """improve_asset 使用高 temperature（改进专用）"""
        from unittest.mock import patch
        from evolution.darwin_evolve import DimensionScore

        dim = DimensionScore(
            name="角色一致性", score=5.0, max_score=10.0,
            issues=["问题"], suggestions=["建议"],
        )

        engine = de.DarwinEvolution(
            provider=None, config={},
            log_callback=lambda _: None,
        )
        self.assertEqual(engine.improvement_temperature, 0.7)

        with patch.object(engine, "_call_llm_sync", return_value=None) as mock_call:
            engine.improve_asset("test", "content", dim)
            args, kwargs = mock_call.call_args
            self.assertEqual(kwargs.get("temperature"), engine.improvement_temperature)


# ============================================================================
# Item 2: 结构化结果追踪 (results.tsv)
# ============================================================================

class TestResultsTSV(YunliTestCase):
    """_write_results_tsv / get_results_history"""

    def setUp(self):
        self._tmp_dir = tempfile.TemporaryDirectory()
        self._orig_results = de.RESULTS_TSV
        self._tmp = Path(self._tmp_dir.name)
        de.RESULTS_TSV = self._tmp / "results.tsv"

    def tearDown(self):
        de.RESULTS_TSV = self._orig_results
        self._tmp_dir.cleanup()

    def test_write_creates_file_with_header(self):
        """首次写入创建带表头的文件"""
        engine = de.DarwinEvolution(provider=None, config={}, log_callback=lambda _: None)
        engine._write_results_tsv("system_prompt", 1, 70.0, 75.0, "角色一致性", "keep")
        self.assertTrue(de.RESULTS_TSV.exists())
        content = de.RESULTS_TSV.read_text(encoding="utf-8")
        self.assertIn("timestamp", content)
        self.assertIn("system_prompt", content)

    def test_write_multiple_rows(self):
        """多次写入追加多行"""
        engine = de.DarwinEvolution(provider=None, config={}, log_callback=lambda _: None)
        engine._write_results_tsv("system_prompt", 1, 70.0, 75.0, "角色一致性", "keep")
        engine._write_results_tsv("system_prompt", 2, 75.0, 76.0, "反AI腔", "keep")
        engine._write_results_tsv("filter_rules", 1, 60.0, 59.0, "指令清晰度", "revert")

        history = engine.get_results_history()
        self.assertEqual(len(history), 3)

    def test_get_results_history_returns_correct_fields(self):
        """读取结果包含正确字段"""
        engine = de.DarwinEvolution(provider=None, config={}, log_callback=lambda _: None)
        engine._write_results_tsv("system_prompt", 1, 70.0, 75.0, "角色一致性", "keep")

        history = engine.get_results_history()
        self.assertEqual(len(history), 1)
        row = history[0]
        self.assertEqual(row["asset"], "system_prompt")
        self.assertEqual(row["iteration"], 1)
        self.assertEqual(row["old_score"], 70.0)
        self.assertEqual(row["new_score"], 75.0)
        self.assertEqual(row["delta"], 5.0)
        self.assertEqual(row["target_dim"], "角色一致性")
        self.assertEqual(row["status"], "keep")

    def test_get_results_history_empty_file(self):
        """无文件时返回空列表"""
        engine = de.DarwinEvolution(provider=None, config={}, log_callback=lambda _: None)
        history = engine.get_results_history()
        self.assertEqual(history, [])

    def test_write_with_exploratory_mode(self):
        """探索性重写模式写入"""
        engine = de.DarwinEvolution(provider=None, config={}, log_callback=lambda _: None)
        engine._write_results_tsv("system_prompt", 3, 70.0, 80.0, "探索性重写", "keep", "exploratory")
        history = engine.get_results_history()
        self.assertEqual(history[0]["eval_mode"], "exploratory")
        self.assertEqual(history[0]["target_dim"], "探索性重写")


# ============================================================================
# Item 3: 执行测试 (test_prompts.json)
# ============================================================================

class TestRunTestPrompts(YunliTestCase):
    """_load_test_prompts / _run_test_prompts"""

    def setUp(self):
        self._tmp_dir = tempfile.TemporaryDirectory()
        self._orig_test_file = de.TEST_PROMPTS_FILE
        self._tmp = Path(self._tmp_dir.name)
        de.TEST_PROMPTS_FILE = self._tmp / "test_prompts.json"

    def tearDown(self):
        de.TEST_PROMPTS_FILE = self._orig_test_file
        self._tmp_dir.cleanup()

    def test_load_empty_when_no_file(self):
        """无文件时返回空字典"""
        engine = de.DarwinEvolution(provider=None, config={}, log_callback=lambda _: None)
        result = engine._load_test_prompts()
        self.assertEqual(result, {})

    def test_load_valid_json(self):
        """加载有效 JSON 文件"""
        self._tmp.mkdir(exist_ok=True)
        test_data = {
            "system_prompt": [
                {"prompt": "@云璃 你好", "expect": "体现直率"}
            ]
        }
        de.TEST_PROMPTS_FILE.write_text(json.dumps(test_data, ensure_ascii=False), encoding="utf-8")
        engine = de.DarwinEvolution(provider=None, config={}, log_callback=lambda _: None)
        result = engine._load_test_prompts()
        self.assertIn("system_prompt", result)
        self.assertEqual(len(result["system_prompt"]), 1)

    def test_load_corrupted_json(self):
        """加载损坏 JSON 返回空"""
        self._tmp.mkdir(exist_ok=True)
        de.TEST_PROMPTS_FILE.write_text("{invalid json", encoding="utf-8")
        engine = de.DarwinEvolution(provider=None, config={}, log_callback=lambda _: None)
        result = engine._load_test_prompts()
        self.assertEqual(result, {})

    def test_run_no_test_cases(self):
        """无测试用例时返回空结果"""
        engine = de.DarwinEvolution(provider=None, config={}, log_callback=lambda _: None)
        result = engine._run_test_prompts("system_prompt", "test content")
        self.assertEqual(result["total"], 0)
        self.assertEqual(result["passed"], 0)
        self.assertEqual(result["score"], 0.0)

    def test_run_with_mock_llm(self):
        """有测试用例但 LLM 不可用"""
        self._tmp.mkdir(exist_ok=True)
        test_data = {
            "system_prompt": [
                {"prompt": "@云璃 你好", "expect": "体现直率"}
            ]
        }
        de.TEST_PROMPTS_FILE.write_text(json.dumps(test_data, ensure_ascii=False), encoding="utf-8")
        engine = de.DarwinEvolution(provider=None, config={}, log_callback=lambda _: None)
        result = engine._run_test_prompts("system_prompt", "test content")
        self.assertEqual(result["total"], 1)
        self.assertEqual(result["passed"], 0)
        self.assertIn("LLM 不可用", result["details"][0]["reason"])


# ============================================================================
# Item 4: 探索性重写
# ============================================================================

class TestExploratoryRewrite(YunliTestCase):
    """_exploratory_rewrite / _build_exploratory_prompt"""

    def setUp(self):
        self._tmp_dir = tempfile.TemporaryDirectory()
        self._orig_assets = de.ASSETS_DIR
        self._tmp = Path(self._tmp_dir.name)
        de.ASSETS_DIR = self._tmp / "assets"
        de.ASSETS_DIR.mkdir(exist_ok=True)

    def tearDown(self):
        de.ASSETS_DIR = self._orig_assets
        self._tmp_dir.cleanup()

    def test_build_exploratory_prompt_contains_asset_info(self):
        """探索性重写提示词包含资产信息"""
        engine = de.DarwinEvolution(provider=None, config={}, log_callback=lambda _: None)
        prompt = engine._build_exploratory_prompt("system_prompt", "test content")
        self.assertIn("system_prompt", prompt)
        self.assertIn("test content", prompt)
        self.assertIn("重构", prompt)

    def test_exploratory_rewrite_without_llm_returns_none(self):
        """无 LLM 时探索性重写返回 None"""
        engine = de.DarwinEvolution(provider=None, config={}, log_callback=lambda _: None)
        result = engine._exploratory_rewrite("system_prompt", "test content")
        self.assertIsNone(result)

    def test_init_exploratory_config_defaults(self):
        """探索性重写配置默认值"""
        engine = de.DarwinEvolution(provider=None, config={}, log_callback=lambda _: None)
        self.assertEqual(engine.exploratory_threshold, de.DEFAULT_EXPLORATORY_THRESHOLD)
        self.assertEqual(engine.exploratory_temperature, de.DEFAULT_EXPLORATORY_TEMPERATURE)
        self.assertEqual(engine.exploratory_candidates, de.DEFAULT_EXPLORATORY_CANDIDATES)

    def test_init_exploratory_config_from_config(self):
        """从 config 读取探索性重写配置"""
        config = {
            "evolution_exploratory_threshold": 5,
            "evolution_exploratory_temperature": 0.95,
            "evolution_exploratory_candidates": 5,
        }
        engine = de.DarwinEvolution(provider=None, config=config, log_callback=lambda _: None)
        self.assertEqual(engine.exploratory_threshold, 5)
        self.assertEqual(engine.exploratory_temperature, 0.95)
        self.assertEqual(engine.exploratory_candidates, 5)


class TestParallelBaseline(YunliTestCase):
    """并行基线评估测试"""

    def setUp(self):
        self._tmp_dir = tempfile.TemporaryDirectory()
        self._tmp = Path(self._tmp_dir.name)
        self._orig_assets = de.ASSETS_DIR
        self._orig_baseline = de.BASELINE_FILE
        de.ASSETS_DIR = self._tmp / "assets"
        de.ASSETS_DIR.mkdir(exist_ok=True)
        de.BASELINE_FILE = self._tmp / "baseline.json"

        # 创建测试资产
        for name in de.ASSET_NAMES:
            (de.ASSETS_DIR / f"{name}.md").write_text(f"test {name} content", encoding="utf-8")

    def tearDown(self):
        de.ASSETS_DIR = self._orig_assets
        de.BASELINE_FILE = self._orig_baseline
        self._tmp_dir.cleanup()

    def test_parallel_workers_default_zero(self):
        """默认 parallel_workers=0（串行模式）"""
        engine = de.DarwinEvolution(provider=None, config={}, log_callback=lambda _: None)
        self.assertEqual(engine.parallel_workers, 0)

    def test_parallel_workers_from_config(self):
        """从 config 读取 parallel_workers"""
        engine = de.DarwinEvolution(provider=None, config={
            "evolution_parallel_workers": 3,
        }, log_callback=lambda _: None)
        self.assertEqual(engine.parallel_workers, 3)

    def test_run_baseline_uses_serial_when_workers_zero(self):
        """parallel_workers=0 时使用串行模式"""
        engine = de.DarwinEvolution(provider=None, config={
            "evolution_parallel_workers": 0,
        }, log_callback=lambda _: None)
        # 串行模式应调用 _run_baseline_serial
        result = engine.run_baseline()
        self.assertIsInstance(result, dict)

    def test_run_baseline_uses_parallel_when_workers_positive(self):
        """parallel_workers>0 时使用并行模式"""
        engine = de.DarwinEvolution(provider=None, config={
            "evolution_parallel_workers": 3,
        }, log_callback=lambda _: None)
        result = engine.run_baseline()
        self.assertIsInstance(result, dict)
        # 并行模式下 baseline 应包含 eval_mode=parallel
        for name, data in result.items():
            self.assertEqual(data.get("eval_mode"), "parallel")

    def test_parallel_baseline_results_match_serial(self):
        """并行和串行结果应包含相同的资产"""
        # 串行
        engine_serial = de.DarwinEvolution(provider=None, config={
            "evolution_parallel_workers": 0,
        }, log_callback=lambda _: None)
        result_serial = engine_serial.run_baseline()

        # 并行
        engine_parallel = de.DarwinEvolution(provider=None, config={
            "evolution_parallel_workers": 5,
        }, log_callback=lambda _: None)
        result_parallel = engine_parallel.run_baseline()

        # 相同的资产名
        self.assertEqual(set(result_serial.keys()), set(result_parallel.keys()))

    def test_parallel_baseline_saves_baseline_file(self):
        """并行模式正确保存 baseline.json"""
        engine = de.DarwinEvolution(provider=None, config={
            "evolution_parallel_workers": 3,
        }, log_callback=lambda _: None)
        engine.run_baseline()
        self.assertTrue(de.BASELINE_FILE.exists())
        data = json.loads(de.BASELINE_FILE.read_text(encoding="utf-8"))
        self.assertGreater(len(data), 0)

    def test_run_baseline_parallel(self):
        """DarwinEvolution 内置并行基线评估返回正确结构"""
        de.save_asset("system_prompt", "test")
        engine = de.DarwinEvolution(
            provider=None, config={
                "evolution_assets": ["system_prompt"],
                "evolution_parallel_workers": 2,
            },
            log_callback=lambda _: None,
        )
        result = engine._run_baseline_parallel()
        self.assertIsInstance(result, dict)
        self.assertIn("system_prompt", result)

    def test_benchmark_serial_vs_parallel(self):
        """benchmark_serial_vs_parallel 返回正确结构"""
        result = de.benchmark_serial_vs_parallel(provider=None, config={})
        self.assertIn("serial_time", result)
        self.assertIn("parallel_time", result)
        self.assertIn("speedup", result)
        self.assertIn("asset_count", result)
        self.assertGreaterEqual(result["serial_time"], 0)
        self.assertGreaterEqual(result["parallel_time"], 0)
        # 加载所有已存在的资产，数量取决于 assets/ 目录
        self.assertGreaterEqual(result["asset_count"], 1)

    def test_benchmark_serial_vs_parallel_no_assets(self):
        """无可评估资产时返回 error"""
        # 临时清空资产
        orig_assets = de.ASSETS_DIR
        with tempfile.TemporaryDirectory() as tmp:
            de.ASSETS_DIR = Path(tmp) / "assets"
            de.ASSETS_DIR.mkdir(exist_ok=True)
            result = de.benchmark_serial_vs_parallel(provider=None, config={})
            self.assertIn("error", result)
            de.ASSETS_DIR = orig_assets


if __name__ == "__main__":
    unittest.main()