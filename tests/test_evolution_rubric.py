"""Darwin 10 维度评分 Rubric 模块的单元测试

覆盖：
  - RUBRIC 结构完整性（10 维度 + 必填字段）
  - 权重合法性 & 总分计算
  - get_dimension_names / get_dimension_weight
  - AssetScore / DimensionScore dataclass
  - anti_ai_score 失败模式覆盖（v2.1 核心特性）
"""

import os
import sys

test_dir = os.path.dirname(os.path.abspath(__file__))
yunli_dir = os.path.dirname(test_dir)
parent_dir = os.path.dirname(yunli_dir)
for p in [parent_dir, yunli_dir, test_dir]:
    if p not in sys.path:
        sys.path.insert(0, p)

import unittest
from test_base import YunliTestCase

from evolution.eval.rubric import (
    RUBRIC,
    DimensionScore,
    AssetScore,
    get_dimension_names,
    get_dimension_weight,
    get_max_possible_score,
)


# ============================================================================
# RUBRIC 结构测试
# ============================================================================

class TestRubricStructure(YunliTestCase):
    """RUBRIC 字典的结构完整性"""

    def test_rubric_has_exactly_10_dimensions(self):
        """v2.1 必须是 10 个维度（含 anti_ai_score）"""
        self.assertEqual(len(RUBRIC), 10, f"期望 10 维度，实际 {len(RUBRIC)}")

    def test_every_dimension_has_required_keys(self):
        required = {"name", "weight", "description", "scoring_guide", "failure_modes"}
        for dim_key, dim_info in RUBRIC.items():
            missing = required - set(dim_info.keys())
            self.assertEqual(missing, set(), f"{dim_key} 缺少字段: {missing}")

    def test_every_dimension_has_6_score_levels(self):
        """每个维度应有 10/8/6/4/2/0 六档"""
        for dim_key, dim_info in RUBRIC.items():
            guide = dim_info["scoring_guide"]
            for level in ["10", "8", "6", "4", "2", "0"]:
                self.assertIn(level, guide, f"{dim_key} 缺少 {level} 分档")

    def test_every_dimension_has_failure_modes(self):
        for dim_key, dim_info in RUBRIC.items():
            modes = dim_info["failure_modes"]
            self.assertIsInstance(modes, list)
            self.assertGreater(len(modes), 0, f"{dim_key} 失败模式列表为空")

    def test_anti_ai_score_has_8_failure_modes(self):
        """v2.1 反AI腔维度应有 8 条失败模式"""
        modes = RUBRIC["anti_ai_score"]["failure_modes"]
        self.assertEqual(len(modes), 8, f"anti_ai_score 失败模式期望 8 条，实际 {len(modes)}")

    def test_character_consistency_has_4_failure_modes(self):
        """角色一致性应有 4 条失败模式（过于温柔/长篇大论/过于熟悉/过于主动）"""
        modes = RUBRIC["character_consistency"]["failure_modes"]
        self.assertEqual(len(modes), 4)


# ============================================================================
# 权重测试
# ============================================================================

class TestWeights(YunliTestCase):
    """权重相关测试"""

    def test_all_weights_positive(self):
        """所有权重必须大于 0"""
        for dim_key, dim_info in RUBRIC.items():
            self.assertGreater(dim_info["weight"], 0, f"{dim_key} 权重 {dim_info['weight']} <= 0")

    def test_all_weights_between_05_and_15(self):
        """权重在 0.5-1.5 之间，单个维度不应主导总分"""
        for dim_key, dim_info in RUBRIC.items():
            self.assertGreaterEqual(dim_info["weight"], 0.5, f"{dim_key} 权重 < 0.5")
            self.assertLessEqual(dim_info["weight"], 1.5, f"{dim_key} 权重 > 1.5")

    def test_total_weight_positive(self):
        """总权重大于 0"""
        total = sum(d["weight"] for d in RUBRIC.values())
        self.assertGreater(total, 0)
        self.assertAlmostEqual(total, 10.1, msg=f"总权重 {total} != 10.1（10维每维0.8-1.2）")

    def test_character_consistency_highest_weight(self):
        """角色一致性权重 1.2，应是所有维度中并列最高"""
        weights = [(k, d["weight"]) for k, d in RUBRIC.items()]
        max_w = max(w for _, w in weights)
        self.assertEqual(RUBRIC["character_consistency"]["weight"], max_w)

    def test_token_efficiency_lowest_weight(self):
        """Token 效率权重 0.8，应与风格一致性(0.9)并列最低"""
        weights = [(k, d["weight"]) for k, d in RUBRIC.items()]
        min_w = min(w for _, w in weights)
        self.assertLessEqual(min_w, 0.8)
        self.assertAlmostEqual(RUBRIC["token_efficiency"]["weight"], min_w, delta=0.2)


# ============================================================================
# 辅助函数测试
# ============================================================================

class TestHelperFunctions(YunliTestCase):
    """get_dimension_names / get_dimension_weight"""

    def test_get_dimension_names_returns_all_10(self):
        names = get_dimension_names()
        self.assertEqual(len(names), 10)

    def test_get_dimension_names_sorted_by_weight_desc(self):
        names = get_dimension_names()
        weights = [RUBRIC[n]["weight"] for n in names]
        self.assertEqual(weights, sorted(weights, reverse=True),
                         "dimension names 未按权重降序排列")

    def test_get_dimension_weight_returns_correct_value(self):
        for key in RUBRIC:
            with self.subTest(key=key):
                self.assertEqual(get_dimension_weight(key), RUBRIC[key]["weight"])

    def test_get_dimension_weight_raises_key_error_for_unknown(self):
        with self.assertRaises(KeyError):
            get_dimension_weight("nonexistent")


# ============================================================================
# Dataclass 测试
# ============================================================================

class TestDimensionScoreDataclass(YunliTestCase):
    """DimensionScore dataclass"""

    def test_create_dimension_score(self):
        score = DimensionScore(
            name="角色一致性",
            score=8.5,
            max_score=10.0,
            issues=["问题1"],
            suggestions=["建议1"],
        )
        self.assertEqual(score.name, "角色一致性")
        self.assertEqual(score.score, 8.5)
        self.assertEqual(score.max_score, 10.0)
        self.assertEqual(len(score.issues), 1)
        self.assertEqual(len(score.suggestions), 1)

    def test_score_in_range(self):
        """分数应在 0-10 之间"""
        dim = DimensionScore(name="test", score=5.5, max_score=10.0, issues=[], suggestions=[])
        self.assertGreaterEqual(dim.score, 0)
        self.assertLessEqual(dim.score, 10)


class TestAssetScoreDataclass(YunliTestCase):
    """AssetScore dataclass"""

    def test_create_asset_score(self):
        dims = [
            DimensionScore(name="角色一致性", score=8.0, max_score=10.0, issues=[], suggestions=[]),
            DimensionScore(name="反AI腔", score=7.0, max_score=10.0, issues=[], suggestions=[]),
        ]
        asset = AssetScore(
            asset_name="system_prompt",
            asset_path="/path/to/system_prompt.md",
            dimensions=dims,
            total_score=75.0,
            overall_comment="comment",
        )
        self.assertEqual(asset.asset_name, "system_prompt")
        self.assertEqual(len(asset.dimensions), 2)
        self.assertEqual(asset.total_score, 75.0)

    def test_total_score_rounded_correctly(self):
        dims = [
            DimensionScore(name="角色一致性", score=7.5, max_score=10.0, issues=[], suggestions=[]),
        ]
        asset = AssetScore(
            asset_name="test", asset_path="p", dimensions=dims,
            total_score=74.256, overall_comment="")
        self.assertAlmostEqual(asset.total_score, 74.3, delta=0.1)

    def test_dimensions_list_can_take_multiple(self):
        """支持传入 1-10 个维度"""
        dims = [
            DimensionScore(name=n, score=7.0, max_score=10.0, issues=[], suggestions=[])
            for n in ["d1", "d2", "d3"]
        ]
        asset = AssetScore(asset_name="x", asset_path="p", dimensions=dims, total_score=70.0, overall_comment="")
        self.assertEqual(len(asset.dimensions), 3)


# ============================================================================
# anti_ai_score v2.1 特定测试
# ============================================================================

class TestAntiAIScore(YunliTestCase):
    """反AI腔维度 v2.1"""

    REQD_PHRASES = ["停顿", "重复", "自我纠正", "犯错", "不确定", "固执", "忘词", "废话", "跑题"]

    def test_failure_modes_cover_breadth(self):
        """失败模式覆盖所有关键反AI特征"""
        modes_text = " ".join(RUBRIC["anti_ai_score"]["failure_modes"])
        for phrase in self.REQD_PHRASES:
            self.assertIn(phrase, modes_text, f"反AI腔缺少关键特征: {phrase}")

    def test_weight_is_balanced(self):
        """反AI腔权重 = 1.0，平衡约束与增强"""
        self.assertEqual(RUBRIC["anti_ai_score"]["weight"], 1.0)

    def test_10_score_description_emphasizes_imperfection(self):
        """满分描述强调'无意识地说'，即不完美的自然"""
        guide = RUBRIC["anti_ai_score"]["scoring_guide"]["10"]
        self.assertIn("无意识", guide)

    def test_0_score_description_represents_perfect_ai(self):
        """0 分描述强调'完全标准AI语气'，即过于完美的反面教材"""
        guide = RUBRIC["anti_ai_score"]["scoring_guide"]["0"]
        self.assertIn("标准AI", guide)


class TestGetMaxPossibleScore(YunliTestCase):
    """get_max_possible_score 函数"""

    def test_returns_100(self):
        self.assertEqual(get_max_possible_score(), 100)


if __name__ == "__main__":
    unittest.main()