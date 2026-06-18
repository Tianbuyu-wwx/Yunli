"""测试 utils.py — JSON 提取工具"""
import json
import sys
from pathlib import Path

# 添加父目录到 sys.path 以支持独立运行
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from utils import extract_json_from_response


def test_extract_plain_json():
    """纯 JSON 字符串"""
    result = extract_json_from_response('{"key": "value", "num": 42}')
    assert result is not None
    assert result["key"] == "value"
    assert result["num"] == 42


def test_extract_json_code_block():
    """```json 包裹的 JSON"""
    text = '```json\n{"key": "value"}\n```'
    result = extract_json_from_response(text)
    assert result is not None
    assert result["key"] == "value"


def test_extract_plain_code_block():
    """``` 包裹的 JSON"""
    text = '```\n{"key": "value"}\n```'
    result = extract_json_from_response(text)
    assert result is not None
    assert result["key"] == "value"


def test_extract_complex_json():
    """包含嵌套和数组的 JSON"""
    text = '''```json
{
  "dimensions": [
    {"dim_key": "char", "score": 8.5, "issues": ["问题1"], "suggestions": ["建议1"]},
    {"dim_key": "style", "score": 7.0, "issues": [], "suggestions": ["建议2"]}
  ],
  "overall_comment": "测试评语"
}
```'''
    result = extract_json_from_response(text)
    assert result is not None
    assert len(result["dimensions"]) == 2
    assert result["dimensions"][0]["score"] == 8.5
    assert result["overall_comment"] == "测试评语"


def test_extract_invalid_json():
    """无效 JSON 返回 None"""
    result = extract_json_from_response("这不是JSON")
    assert result is None


def test_extract_empty_string():
    """空字符串返回 None"""
    result = extract_json_from_response("")
    assert result is None


def test_extract_json_with_text_surrounding():
    """JSON 前后有文字"""
    text = '以下是评分结果：\n```json\n{"score": 9}\n```\n评分完毕。'
    result = extract_json_from_response(text)
    assert result is not None
    assert result["score"] == 9


def test_extract_nested_arrays():
    """嵌套数组"""
    text = '{"data": [[1, 2], [3, 4]]}'
    result = extract_json_from_response(text)
    assert result is not None
    assert result["data"] == [[1, 2], [3, 4]]


if __name__ == "__main__":
    # 运行所有测试
    tests = [
        test_extract_plain_json,
        test_extract_json_code_block,
        test_extract_plain_code_block,
        test_extract_complex_json,
        test_extract_invalid_json,
        test_extract_empty_string,
        test_extract_json_with_text_surrounding,
        test_extract_nested_arrays,
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
        except Exception as e:
            print(f"  ERROR {test.__name__}: {e}")
            failed += 1

    print(f"\n结果: {passed} 通过, {failed} 失败, {len(tests)} 总计")
    sys.exit(0 if failed == 0 else 1)