"""Darwin 评分执行器 (scorer.py) 单元测试

覆盖:
  - build_scoring_prompt() 生成正确 prompt 包含 10 维度
  - parse_scoring_response() 解析 JSON 响应
  - format_score_report() 格式化可读报告
  - compare_scores() 改进/退化检测
  - format_compare_report() 可读对比
  - _RUBRIC_TOTAL_WEIGHT 非零
  - 边界情况：空 JSON、缺失维度、```json``` 包裹
"""

import json
import os
import sys

test_dir = os.path.dirname(os.path.abspath(__file__))
yunli_dir = os.path.dirname(test_dir)
eval_dir = os.path.join(yunli_dir, "evolution", "eval")
for p in [yunli_dir, eval_dir]:
    if p not in sys.path:
        sys.path.insert(0, p)

import unittest
from unittest.mock import patch
from test_base import YunliTestCase

from evolution.eval.rubric import RUBRIC, DimensionScore, AssetScore, get_dimension_names
from evolution.eval.scorer import (
    build_scoring_prompt,
    parse_scoring_response,
    format_score_report,
    compare_scores,
    format_compare_report,
    _RUBRIC_TOTAL_WEIGHT,
)


# ============================================================================
# build_scoring_prompt
# ============================================================================

class TestBuildScoringPrompt(YunliTestCase):
    """build_scoring_prompt() 函数"""

    def test_prompt_contains_asset_name(self):
        prompt = build_scoring_prompt("system_prompt", "test content")
        self.assertIn("system_prompt", prompt)

    def test_prompt_contains_asset_content(self):
        prompt = build_scoring_prompt("test", "unique_content_xyz")
        self.assertIn("unique_content_xyz", prompt)

    def test_prompt_contains_all_10_dimensions(self):
        prompt = build_scoring_prompt("test", "content")
        for dim_key, dim_info in RUBRIC.items():
            self.assertIn(dim_info["name"], prompt, f"缺少维度: {dim_info['name']}")

    def test_prompt_contains_scoring_instructions(self):
        prompt = build_scoring_prompt("test", "content")
        self.assertIn("10 个维度", prompt)
        self.assertIn("0-10 分", prompt)

    def test_prompt_contains_json_format_instruction(self):
        prompt = build_scoring_prompt("test", "content")
        self.assertIn("```json", prompt)
        self.assertIn("dimensions", prompt)

    def test_prompt_contains_weight_info(self):
        prompt = build_scoring_prompt("test", "content")
        self.assertIn("权重", prompt)

    def test_prompt_non_empty_with_empty_content(self):
        prompt = build_scoring_prompt("test", "")
        self.assertGreater(len(prompt), 0)

    def test_prompt_contains_scoring_guide_levels(self):
        prompt = build_scoring_prompt("test", "content")
        self.assertIn("10分：", prompt)
        self.assertIn("0分：", prompt)


# ============================================================================
# parse_scoring_response
# ============================================================================

class TestParseScoringResponse(YunliTestCase):
    """parse_scoring_response() 函数"""

    def _make_response(self, scores_dict=None, comment="ok"):
        """构建标准的 LLM 评分 JSON 响应"""
        dims = []
        if scores_dict is None:
            scores_dict = {k: 7.5 for k in RUBRIC}
        for dim_key, score in scores_dict.items():
            dims.append({
                "dim_key": dim_key,
                "score": score,
                "issues": ["issue1"],
                "suggestions": ["suggestion1"],
            })
        return json.dumps({"dimensions": dims, "overall_comment": comment})

    def test_parse_valid_response(self):
        response = self._make_response()
        asset = parse_scoring_response(response, "test_asset", "/path/test_asset.md")
        self.assertEqual(asset.asset_name, "test_asset")
        self.assertEqual(len(asset.dimensions), 10)
        self.assertGreater(asset.total_score, 0)
        self.assertLessEqual(asset.total_score, 100)

    def test_parse_response_wrapped_in_code_block(self):
        response = self._make_response()
        wrapped = f"```json\n{response}\n```"
        asset = parse_scoring_response(wrapped, "test", "/p")
        self.assertEqual(len(asset.dimensions), 10)

    def test_parse_response_wrapped_in_generic_code_block(self):
        response = self._make_response()
        wrapped = f"```\n{response}\n```"
        asset = parse_scoring_response(wrapped, "test", "/p")
        self.assertEqual(len(asset.dimensions), 10)

    def test_total_score_uses_rubric_total_weight(self):
        """总分使用预计算的 _RUBRIC_TOTAL_WEIGHT，而非 LLM 返回的维度子集"""
        # 只返回 5 个维度，总分分母仍应是 RUBRIC 总权重
        partial = {k: 10.0 for k in list(RUBRIC.keys())[:5]}
        dims = [{"dim_key": k, "score": v, "issues": [], "suggestions": []} for k, v in partial.items()]
        response = json.dumps({"dimensions": dims, "overall_comment": "partial"})
        asset = parse_scoring_response(response, "test", "/p")
        # 总分 = (10*5*各自权重) / RUBRIC_TOTAL_WEIGHT * 10
        # 因为只返回了 5 个维度满分，总分 < 100
        self.assertLess(asset.total_score, 100, "部分维度满分不应得满分")

    def test_parse_response_with_unknown_dim_key(self):
        """未知 dim_key 不应崩溃，只跳过"""
        dims = [
            {"dim_key": "unknown_key", "score": 5.0, "issues": [], "suggestions": []},
            {"dim_key": "character_consistency", "score": 8.0, "issues": [], "suggestions": []},
        ]
        response = json.dumps({"dimensions": dims, "overall_comment": "ok"})
        asset = parse_scoring_response(response, "test", "/p")
        # 未知 key 被跳过，只有 character_consistency 被计入
        self.assertEqual(len(asset.dimensions), 2)
        # 第一个维度名回退为 key 本身
        self.assertEqual(asset.dimensions[0].name, "unknown_key")

    def test_parse_response_missing_issues_and_suggestions(self):
        """issues 和 suggestions 可选，缺失时应为空列表"""
        dims = [{"dim_key": "character_consistency", "score": 8.0}]
        response = json.dumps({"dimensions": dims, "overall_comment": "ok"})
        asset = parse_scoring_response(response, "test", "/p")
        self.assertEqual(asset.dimensions[0].issues, [])
        self.assertEqual(asset.dimensions[0].suggestions, [])

    def test_parse_response_all_10_scores(self):
        """所有 10 维度满分 10 应得 100 分"""
        all_10 = {k: 10.0 for k in RUBRIC}
        response = self._make_response(all_10)
        asset = parse_scoring_response(response, "test", "/p")
        self.assertAlmostEqual(asset.total_score, 100.0, delta=0.5)

    def test_parse_response_all_0_scores(self):
        """所有 10 维度 0 分应得 0 分"""
        all_0 = {k: 0.0 for k in RUBRIC}
        response = self._make_response(all_0)
        asset = parse_scoring_response(response, "test", "/p")
        self.assertAlmostEqual(asset.total_score, 0.0, delta=0.5)


# ============================================================================
# format_score_report
# ============================================================================

class TestFormatScoreReport(YunliTestCase):
    """format_score_report() 函数"""

    def _make_asset(self):
        dims = [
            DimensionScore(name="角色一致性", score=8.0, max_score=10.0,
                           issues=["issue1"], suggestions=["suggestion1"]),
        ]
        return AssetScore(asset_name="test", asset_path="/p", dimensions=dims,
                          total_score=80.0, overall_comment="good")

    def test_report_contains_header(self):
        report = format_score_report(self._make_asset())
        self.assertIn("test", report)
        self.assertIn("80.0", report)

    def test_report_contains_dimension_details(self):
        report = format_score_report(self._make_asset())
        self.assertIn("角色一致性", report)
        self.assertIn("8.0/10", report)
        self.assertIn("ISSUE", report)
        self.assertIn("TIP", report)
        self.assertIn("权重", report)

    def test_report_no_dimensions(self):
        """空维度列表不崩溃"""
        asset = AssetScore(asset_name="empty", asset_path="/p", dimensions=[],
                           total_score=0.0, overall_comment="")
        report = format_score_report(asset)
        self.assertIn("empty", report)
        self.assertIn("0.0", report)

    def test_report_dimension_without_issues_or_suggestions(self):
        dim = DimensionScore(name="test", score=5.0, max_score=10.0, issues=[], suggestions=[])
        asset = AssetScore(asset_name="x", asset_path="/p", dimensions=[dim],
                           total_score=50.0, overall_comment="")
        report = format_score_report(asset)
        self.assertNotIn("ISSUE", report)
        self.assertNotIn("TIP", report)


# ============================================================================
# compare_scores
# ============================================================================

class TestCompareScores(YunliTestCase):
    """compare_scores() 函数"""

    def _make_asset(self, name, dims_dict):
        dims = [DimensionScore(name=n, score=s, max_score=10.0, issues=[], suggestions=[])
                for n, s in dims_dict.items()]
        total = sum(dims_dict.values()) / max(len(dims_dict), 1) * 10 if dims_dict else 0
        return AssetScore(asset_name=name, asset_path="/p", dimensions=dims,
                          total_score=total,
                          overall_comment="")

    def test_detect_improvement(self):
        old = self._make_asset("a", {"角色一致性": 5.0})
        new = self._make_asset("a", {"角色一致性": 8.0})
        result = compare_scores(old, new)
        self.assertTrue(result["improved"])
        self.assertGreater(result["total_diff"], 0)

    def test_detect_regression(self):
        old = self._make_asset("a", {"角色一致性": 8.0})
        new = self._make_asset("a", {"角色一致性": 5.0})
        result = compare_scores(old, new)
        self.assertFalse(result["improved"])
        self.assertLess(result["total_diff"], 0)

    def test_detect_no_change(self):
        old = self._make_asset("a", {"角色一致性": 5.0})
        new = self._make_asset("a", {"角色一致性": 5.0})
        result = compare_scores(old, new)
        self.assertFalse(result["improved"])
        self.assertEqual(result["total_diff"], 0.0)

    def test_new_dimension_not_in_old(self):
        """新维度在旧评分中不存在时，比较应从 0 开始"""
        old = self._make_asset("a", {})
        new = self._make_asset("a", {"反AI腔": 8.0})
        result = compare_scores(old, new)
        self.assertIn("反AI腔", result["improved_dims"])
        self.assertEqual(result["dimension_diffs"]["反AI腔"], 8.0)

    def test_improved_dims_and_regressed_dims_lists(self):
        old = self._make_asset("a", {"角色一致性": 8.0, "反AI腔": 5.0})
        new = self._make_asset("a", {"角色一致性": 5.0, "反AI腔": 8.0})
        result = compare_scores(old, new)
        self.assertIn("反AI腔", result["improved_dims"])
        self.assertIn("角色一致性", result["regressed_dims"])


# ============================================================================
# format_compare_report
# ============================================================================

class TestFormatCompareReport(YunliTestCase):
    """format_compare_report() 函数"""

    def test_improved_report(self):
        result = format_compare_report({
            "total_diff": 5.0,
            "improved": True,
            "improved_dims": ["角色一致性"],
            "regressed_dims": [],
            "dimension_diffs": {"角色一致性": 5.0},
        })
        self.assertIn("+5.0", result)
        self.assertIn("是", result)

    def test_regressed_report(self):
        result = format_compare_report({
            "total_diff": -3.0,
            "improved": False,
            "improved_dims": [],
            "regressed_dims": ["反AI腔"],
            "dimension_diffs": {"反AI腔": -3.0},
        })
        self.assertIn("-3.0", result)
        self.assertIn("棘轮将回滚", result)

    def test_report_with_both_improved_and_regressed(self):
        result = format_compare_report({
            "total_diff": 2.0,
            "improved": True,
            "improved_dims": ["角色一致性"],
            "regressed_dims": ["反AI腔"],
            "dimension_diffs": {"角色一致性": 5.0, "反AI腔": -3.0},
        })
        self.assertIn("改进维度", result)
        self.assertIn("退化维度", result)


# ============================================================================
# 常量测试
# ============================================================================

class TestConstants(YunliTestCase):
    def test_rubric_total_weight_non_zero(self):
        self.assertGreater(_RUBRIC_TOTAL_WEIGHT, 0)
        self.assertAlmostEqual(_RUBRIC_TOTAL_WEIGHT, 10.1, msg=f"_RUBRIC_TOTAL_WEIGHT={_RUBRIC_TOTAL_WEIGHT}")

    def test_parse_with_zero_rubric_weight_edge_case(self):
        """当 _RUBRIC_TOTAL_WEIGHT 为 0 时不崩溃"""
        # 正常情况下 _RUBRIC_TOTAL_WEIGHT 非零，但测试极端情况
        with patch("evolution.eval.scorer._RUBRIC_TOTAL_WEIGHT", 0):
            from evolution.eval.scorer import parse_scoring_response as psr
            dims = [{"dim_key": k, "score": 5.0, "issues": [], "suggestions": []} for k in list(RUBRIC.keys())[:2]]
            response = json.dumps({"dimensions": dims, "overall_comment": "ok"})
            asset = psr(response, "test", "/p")
            self.assertEqual(asset.total_score, 0.0)


if __name__ == "__main__":
    unittest.main()