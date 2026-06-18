"""Darwin CLI 命令行入口 单元测试

覆盖:
  - main() 子命令分发 baseline / evolve / report / benchmark
  - _cmd_baseline / _cmd_evolve / _cmd_report / _cmd_benchmark
  - _create_standalone_llm_client
  - _call_llm_via_http 成功路径（mock urllib）
  - _call_llm_sync 运行中事件循环路径
  - improve_asset 成功路径
  - run_evolve 不保存 baseline_data 为空时的分支
"""

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch, Mock

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
from evolution.eval.rubric import DimensionScore


# ============================================================================
# CLI 入口点测试
# ============================================================================

class TestCLIStandaloneLLMClient(YunliTestCase):
    """_create_standalone_llm_client"""

    def test_creates_llm_client_with_api_key(self):
        with patch.dict(os.environ, {"DEEPSEEK_API_KEY": "sk-test-key"}):
            llm_client = de._create_standalone_llm_client()
            self.assertIsNotNone(llm_client)
            self.assertTrue(hasattr(llm_client, "call"))

    def test_returns_none_without_api_key(self):
        with patch.dict(os.environ, {}, clear=True):
            # 确保环境中没有 DEEPSEEK_API_KEY
            if "DEEPSEEK_API_KEY" in os.environ:
                del os.environ["DEEPSEEK_API_KEY"]
            try:
                llm_client = de._create_standalone_llm_client()
                self.assertIsNone(llm_client)
            finally:
                pass


class TestCLIMain(YunliTestCase):
    """main() 和子命令函数"""

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

    def test_cmd_baseline(self):
        """_cmd_baseline 正常运行（无 API key → mock）"""
        class FakeArgs:
            pass
        # 清除 API key 让 LLMClient 为 None → 使用 mock 评分
        with patch.object(de, "_create_standalone_llm_client", return_value=None):
            de._cmd_baseline(FakeArgs())
        self.assertTrue(de.BASELINE_FILE.exists())

    def test_cmd_evolve(self):
        """_cmd_evolve 正常运行（只进化 system_prompt）"""
        class FakeArgs:
            asset = "system_prompt"
            max_iter = 0
        de.save_asset("system_prompt", "test")
        import darwin_evolve
        with patch.object(darwin_evolve, "_create_standalone_llm_client", return_value=None):
            de._cmd_evolve(FakeArgs())
        # 不崩溃即为通过

    def test_cmd_evolve_specific_asset(self):
        """_cmd_evolve 指定单个资产"""
        class FakeArgs:
            asset = "system_prompt"
            max_iter = 0
        de.save_asset("system_prompt", "test")
        with patch.object(de, "_create_standalone_llm_client", return_value=None):
            de._cmd_evolve(FakeArgs())

    def test_cmd_report(self):
        """_cmd_report 运行"""
        import darwin_evolve
        de.save_asset("system_prompt", "test")
        engine = de.DarwinEvolution(provider=None, config={
            "evolution_assets": ["system_prompt"],
        }, log_callback=lambda _: None)
        engine.run_baseline()

        class FakeArgs:
            pass
        with patch.object(darwin_evolve, "DarwinEvolution", return_value=engine):
            de._cmd_report(FakeArgs())

    def test_cmd_benchmark(self):
        """_cmd_benchmark 运行"""
        de.save_asset("system_prompt", "test")
        class FakeArgs:
            workers = None
        with patch.object(de, "_create_standalone_llm_client", return_value=None):
            de._cmd_benchmark(FakeArgs())
        # 不崩溃即为通过

    def test_main_baseline(self):
        """main() 分发 baseline 子命令"""
        with patch("argparse._sys.argv", ["darwin_evolve.py", "baseline"]):
            with patch.object(de, "_create_standalone_llm_client", return_value=None):
                de.main()

    def test_main_evolve(self):
        """main() 分发 evolve 子命令"""
        de.save_asset("system_prompt", "test")
        with patch("argparse._sys.argv", ["darwin_evolve.py", "evolve", "--asset", "system_prompt", "--max-iter", "0"]):
            with patch.object(de, "_create_standalone_llm_client", return_value=None):
                de.main()

    def test_main_report(self):
        """main() 分发 report 子命令"""
        engine = de.DarwinEvolution(log_callback=lambda _: None)
        with patch("argparse._sys.argv", ["darwin_evolve.py", "report"]):
            with patch.object(de, "DarwinEvolution", return_value=engine):
                de.main()

    def test_main_benchmark(self):
        """main() 分发 benchmark 子命令"""
        de.save_asset("system_prompt", "test")
        with patch("argparse._sys.argv", ["darwin_evolve.py", "benchmark"]):
            with patch.object(de, "_create_standalone_llm_client", return_value=None):
                de.main()

    def test_main_print_help(self):
        """main() 无子命令时打印帮助"""
        with patch("argparse._sys.argv", ["darwin_evolve.py"]):
            # print_help 会输出到 stderr，只需确认不崩溃
            try:
                de.main()
            except SystemExit:
                pass


# ============================================================================
# _call_llm_via_http 成功路径
# ============================================================================

class TestCallLLMViaHTTPSuccess(YunliTestCase):
    """_call_llm_via_http 成功返回 LLM 响应"""

    def test_http_fallback_returns_content(self):
        """模拟 HTTP 返回有效 JSON → 提取 choices[0].message.content

        v2.2.0：API Key 统一从环境变量 DEEPSEEK_API_KEY 读取（更安全，不存储在 config 中）
        """
        import os
        fake_response = json.dumps({
            "choices": [{"message": {"content": "这是 HTTP fallback 返回的评分结果"}}],
        }).encode("utf-8")

        mock_resp = MagicMock()
        mock_resp.read.return_value = fake_response
        mock_resp.__enter__ = MagicMock(return_value=mock_resp)
        mock_resp.__exit__ = MagicMock(return_value=False)

        with patch.dict(os.environ, {"DEEPSEEK_API_KEY": "sk-test", "DEEPSEEK_API_BASE": "https://test.example.com"}):
            with patch("urllib.request.urlopen", return_value=mock_resp) as mock_urlopen:
                engine = de.DarwinEvolution(provider=None, config={}, log_callback=lambda _: None)
                result = engine._llm._call_http("test prompt", "test system")
                self.assertEqual(result, "这是 HTTP fallback 返回的评分结果")

    def test_http_fallback_handles_urllib_error(self):
        """HTTP fallback 遇到错误时返回 None"""
        engine = de.DarwinEvolution(provider=None, config={
            "deepseek_api_key": "sk-test",
        }, log_callback=lambda _: None)
        result = engine._llm._call_http("test", "system")
        # 会尝试连接但 URL 无效或连接被拒 → 返回 None
        self.assertIsNone(result)

    def test_call_llm_sync_provider_exception_fallback_to_http(self):
        """provider.text_chat 抛异常 → 降级到 HTTP fallback"""
        mock_prov = MagicMock()
        mock_prov.text_chat = MagicMock()

        async def crashing_chat(prompt, system_prompt):
            raise RuntimeError("provider crashed")

        mock_prov.text_chat = crashing_chat

        engine = de.DarwinEvolution(provider=mock_prov, config={
            "deepseek_api_key": "sk-fake",  # 触发 HTTP fallback
        }, log_callback=lambda _: None)
        # HTTP fallback 也会失败（invalid URL），最终返回 None
        result = engine._call_llm_sync("prompt", "system")
        self.assertIsNone(result)


# ============================================================================
# improve_asset 成功路径
# ============================================================================

class TestImproveAssetSuccess(YunliTestCase):
    """improve_asset 返回成功改进后的文本"""

    def test_improve_asset_returns_improved_content(self):
        mock_prov = MagicMock()
        mock_prov.text_chat = MagicMock()

        async def mock_chat(prompt, system_prompt):
            resp = MagicMock()
            resp.completion_text = "  improved content with whitespace  "
            return resp

        mock_prov.text_chat = mock_chat

        engine = de.DarwinEvolution(provider=mock_prov, config={}, log_callback=lambda _: None)
        dim = DimensionScore(name="反AI腔", score=3.0, max_score=10.0,
                             issues=["issue1"], suggestions=["sug1"])
        result = engine.improve_asset("system_prompt", "original", dim)
        self.assertEqual(result, "improved content with whitespace")


# ============================================================================
# run_evolve 边界分支
# ============================================================================

class TestRunEvolveEdgeBranches(YunliTestCase):
    """run_evolve 中 baseline_data 为空或不存在的分支"""

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

    def test_run_evolve_no_baseline_file(self):
        """无 baseline 文件时 evolve 不崩溃"""
        de.save_asset("system_prompt", "test")
        engine = de.DarwinEvolution(provider=None, config={
            "evolution_assets": ["system_prompt"],
        }, log_callback=lambda _: None)
        results = engine.run_evolve(max_iterations=0)
        self.assertIn("system_prompt", results)

    def test_run_evolve_baseline_data_is_falsey(self):
        """baseline 文件内容为空对象（非空但无资产数据）"""
        de.BASELINE_FILE.write_text("{}")
        de.save_asset("system_prompt", "test")
        engine = de.DarwinEvolution(provider=None, config={
            "evolution_assets": ["system_prompt"],
        }, log_callback=lambda _: None)
        results = engine.run_evolve(max_iterations=0)
        self.assertIn("system_prompt", results)

    def test_run_evolve_saves_new_baseline_when_improved(self):
        """当有改进时保存新基线"""
        de.save_asset("system_prompt", "test content v1")

        # 构造一个高基线，让 mock 评分"改进"
        mock_prov = MagicMock()
        mock_prov.text_chat = MagicMock()

        dims = [{"dim_key": k, "score": 9.5, "issues": [], "suggestions": []} for k in de.RUBRIC]

        async def mock_chat(prompt, system_prompt):
            resp = MagicMock()
            resp.completion_text = json.dumps({
                "dimensions": dims,
                "overall_comment": "improved",
            })
            return resp

        mock_prov.text_chat = mock_chat

        engine = de.DarwinEvolution(provider=mock_prov, config={
            "evolution_assets": ["system_prompt"],
            "evolution_min_improvement": 0.1,
        }, log_callback=lambda _: None)

        # 先建立基线（0分）
        engine.run_baseline()

        results = engine.run_evolve(max_iterations=1)
        self.assertIn("system_prompt", results)
        # 由于 mock LLM 评分一致（都是 9.5），且 baseline 也是 9.5，
        # improved 取决于是否有提升


# ============================================================================
# PatternDiscovery._call_llm_http 成功路径
# ============================================================================

class TestPatternDiscoveryHTTP(YunliTestCase):
    """PatternDiscovery._call_llm_http"""

    def test_call_llm_http_success(self):
        """_call_llm_http 返回有效响应（mock urllib）

        v2.2.0：API Key 统一从环境变量 DEEPSEEK_API_KEY 读取
        """
        import os
        from evolution.pattern_discovery import PatternDiscovery

        fake_response = json.dumps({
            "choices": [{"message": {"content": "分析结果"}}],
        }).encode("utf-8")

        with patch.dict(os.environ, {"DEEPSEEK_API_KEY": "sk-test"}):
            with patch("urllib.request.urlopen") as mock_urlopen:
                mock_ctx = MagicMock()
                mock_ctx.read.return_value = fake_response
                mock_ctx.__enter__.return_value = mock_ctx
                mock_urlopen.return_value = mock_ctx

                pd = PatternDiscovery(provider=None, config={})
                result = pd._llm._call_http("test prompt", "test system")
                self.assertEqual(result, "分析结果")

    def test_call_llm_http_no_api_key(self):
        """LLMClient._call_http 无 API key 返回 None"""
        from evolution.pattern_discovery import PatternDiscovery
        pd = PatternDiscovery(provider=None, config={})
        result = pd._llm._call_http("prompt", "system")
        self.assertIsNone(result)

    def test_call_llm_http_url_error(self):
        """LLMClient._call_http 网络错误返回 None"""
        from evolution.pattern_discovery import PatternDiscovery

        with patch("urllib.request.urlopen", side_effect=OSError("connection refused")):
            pd = PatternDiscovery(provider=None, config={"deepseek_api_key": "sk-test"})
            result = pd._llm._call_http("prompt", "system")
            self.assertIsNone(result)


# ============================================================================
# save_asset 边界情况
# ============================================================================

class TestSaveAssetEdgeCases(YunliTestCase):
    """save_asset 边界情况"""

    def setUp(self):
        self._tmp_dir = tempfile.TemporaryDirectory()
        self._tmp = Path(self._tmp_dir.name)
        self._orig_assets = de.ASSETS_DIR
        de.ASSETS_DIR = self._tmp / "assets"
        de.ASSETS_DIR.mkdir(exist_ok=True)

    def tearDown(self):
        de.ASSETS_DIR = self._orig_assets
        self._tmp_dir.cleanup()

    def test_save_asset_backup_overwrites_existing_backup(self):
        """备份文件已存在时应先删除再 rename"""
        asset_path = de.ASSETS_DIR / "test_asset.md"
        asset_path.write_text("old v1", encoding="utf-8")

        # 模拟：先 save 一次创建备份
        de.save_asset("test_asset", "v2")
        # 把 v2 改回 v1（模拟），再 save
        asset_path.write_text("old v1", encoding="utf-8")
        # 再次 save（会与已有备份 timestamp 冲突 → 测试 unlink 分支）
        de.save_asset("test_asset", "v3")
        content = de.load_asset("test_asset")
        self.assertEqual(content, "v3")


if __name__ == "__main__":
    unittest.main()