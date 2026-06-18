"""测试 eval/scorer.py — 评分解析与比较"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from eval.scorer import (
    parse_scoring_response,
    compare_scores,
    format_score_report,
    format_compare_report,
    AssetScore,
    DimensionScore,
)


def test_parse_scoring_response_basic():
    """基本评分响应解析"""
    response = json.dumps({
        "dimensions": [
            {"dim_key": "character_consistency", "score": 8.5,
             "issues": ["问题1"], "suggestions": ["建议1"]},
            {"dim_key": "anti_ai_score", "score": 7.0,
             "issues": [], "suggestions": ["建议2"]},
        ],
        "overall_comment": "测试评语",
    })
    result = parse_scoring_response(response, "test_asset", "/fake/path")
    assert result.asset_name == "test_asset"
    assert result.asset_path == "/fake/path"
    assert len(result.dimensions) == 2
    assert result.dimensions[0].name == "角色一致性"
    assert result.dimensions[0].score == 8.5
    assert result.overall_comment == "测试评语"


def test_parse_scoring_response_with_json_block():
    """```json 包裹的评分响应"""
    response = '```json\n' + json.dumps({
        "dimensions": [
            {"dim_key": "character_consistency", "score": 9.0,
             "issues": [], "suggestions": []},
        ],
        "overall_comment": "OK",
    }) + '\n```'
    result = parse_scoring_response(response, "test_asset", "/fake/path")
    assert result is not None
    assert result.dimensions[0].score == 9.0


def test_parse_scoring_response_invalid():
    """无效响应抛出异常"""
    try:
        parse_scoring_response("not json", "test", "/fake")
        assert False, "应该抛出异常"
    except json.JSONDecodeError:
        pass


def test_compare_scores_improved():
    """改进后的评分比较"""
    old = AssetScore(
        asset_name="test", asset_path="/fake",
        dimensions=[
            DimensionScore(name="角色一致性", score=7.0, max_score=10, issues=[], suggestions=[]),
            DimensionScore(name="反AI腔", score=6.0, max_score=10, issues=[], suggestions=[]),
        ],
        total_score=65.0, overall_comment="",
    )
    new = AssetScore(
        asset_name="test", asset_path="/fake",
        dimensions=[
            DimensionScore(name="角色一致性", score=8.0, max_score=10, issues=[], suggestions=[]),
            DimensionScore(name="反AI腔", score=5.0, max_score=10, issues=[], suggestions=[]),
        ],
        total_score=70.0, overall_comment="",
    )
    result = compare_scores(old, new)
    assert result["improved"] is True
    assert result["total_diff"] == 5.0
    assert "角色一致性" in result["improved_dims"]
    assert "反AI腔" in result["regressed_dims"]


def test_compare_scores_regressed():
    """退化后的评分比较（棘轮应回滚）"""
    old = AssetScore(
        asset_name="test", asset_path="/fake",
        dimensions=[
            DimensionScore(name="角色一致性", score=8.0, max_score=10, issues=[], suggestions=[]),
        ],
        total_score=80.0, overall_comment="",
    )
    new = AssetScore(
        asset_name="test", asset_path="/fake",
        dimensions=[
            DimensionScore(name="角色一致性", score=5.0, max_score=10, issues=[], suggestions=[]),
        ],
        total_score=50.0, overall_comment="",
    )
    result = compare_scores(old, new)
    assert result["improved"] is False
    assert result["total_diff"] == -30.0
    assert "角色一致性" in result["regressed_dims"]


def test_format_score_report():
    """评分报告格式化"""
    score = AssetScore(
        asset_name="test", asset_path="/fake",
        dimensions=[
            DimensionScore(name="角色一致性", score=8.0, max_score=10,
                          issues=["问题1"], suggestions=["建议1"]),
        ],
        total_score=80.0, overall_comment="OK",
    )
    report = format_score_report(score)
    assert "test" in report
    assert "80.0/100" in report
    assert "角色一致性" in report
    assert "[ISSUE] 问题1" in report
    assert "[TIP] 建议1" in report


def test_format_compare_report():
    """比较报告格式化"""
    comparison = {
        "total_diff": 5.0,
        "improved": True,
        "dimension_diffs": {"角色一致性": 2.0, "反AI腔": -1.0},
        "improved_dims": ["角色一致性"],
        "regressed_dims": ["反AI腔"],
    }
    report = format_compare_report(comparison)
    assert "+5.0" in report
    assert "是" in report
    assert "角色一致性" in report


if __name__ == "__main__":
    tests = [
        test_parse_scoring_response_basic,
        test_parse_scoring_response_with_json_block,
        test_parse_scoring_response_invalid,
        test_compare_scores_improved,
        test_compare_scores_regressed,
        test_format_score_report,
        test_format_compare_report,
    ]

    passed = 0
    failed = 0
    for test in tests:
        try:
            test()
            print(f"  PASS  {test.__name__}")
            passed += 1
        except AssertionError as e:
            print(f"  FAIL  {test.__name__}: {e}")
            failed += 1
            import traceback
            traceback.print_exc()
        except Exception as e:
            print(f"  ERROR {test.__name__}: {e}")
            failed += 1
            import traceback
            traceback.print_exc()

    print(f"\n结果: {passed} 通过, {failed} 失败, {len(tests)} 总计")
    sys.exit(0 if failed == 0 else 1)