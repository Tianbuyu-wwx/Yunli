"""Darwin 评分执行器

使用 LLM 对提取的文本资产进行 10 维度评分。
参考 Darwin Skill v2.0：评分 AI 与写 skill 的 AI 分开，避免"自己考自己"偏差。
"""

import json

from typing import Dict, List, Optional
from .rubric import (
    RUBRIC, DimensionScore, AssetScore, get_dimension_names,
    get_dimension_weight,
)

# 预计算总权重（避免用 LLM 返回的维度计算，引入偏差）
_RUBRIC_TOTAL_WEIGHT = sum(d["weight"] for d in RUBRIC.values())


def build_scoring_prompt(asset_name: str, asset_content: str) -> str:
    """构建评分 prompt —— 让 LLM 以独立评委身份对资产进行 10 维度评分"""

    dimension_descriptions = []
    for dim_key, dim_info in RUBRIC.items():
        desc = f"### {dim_info['name']}（权重 {dim_info['weight']}）\n"
        desc += f"评分标准：{dim_info['description']}\n"
        desc += "\n".join([f"- {k}分：{v}" for k, v in dim_info['scoring_guide'].items()])
        desc += f"\n常见失败模式：\n" + "\n".join([f"- {fm}" for fm in dim_info['failure_modes']])
        dimension_descriptions.append(desc)

    dimensions_text = "\n\n".join(dimension_descriptions)

    prompt = f"""你是一个独立的 AI 文本质量评审专家，正在对一份角色扮演 AI 的配置文本进行评分。

被评资产名称：{asset_name}

=== 被评文本内容 ===
{asset_content}
=== 文本结束 ===

请从以下 10 个维度对这份文本进行评分（每维度 0-10 分），并给出具体问题和改进建议。

{dimensions_text}

请以 JSON 格式输出评分结果，格式如下：
```json
{{
  "dimensions": [
    {{
      "dim_key": "维度英文key",
      "score": 分数,
      "issues": ["问题1", "问题2"],
      "suggestions": ["建议1", "建议2"]
    }}
  ],
  "overall_comment": "综合评价，不超过200字"
}}
```

注意：
1. 评分要客观严格，不要因为文本看起来不错就给高分
2. 每个维度必须有至少一条 issue 或至少一条 suggestion（不能空着）
3. 分数要精确到小数点后一位
4. 只输出 JSON，不要输出其他内容
"""

    return prompt


def parse_scoring_response(response: str, asset_name: str, asset_path: str) -> AssetScore:
    """解析 LLM 评分响应，构建 AssetScore 对象"""
    # 使用统一 JSON 提取工具函数
    try:
        from ..utils import extract_json_from_response
    except ImportError:
        try:
            from evolution.utils import extract_json_from_response
        except ImportError:
            from utils import extract_json_from_response

    data = extract_json_from_response(response)
    if data is None:
        raise json.JSONDecodeError("无法从 LLM 响应中提取 JSON", response, 0)

    dimensions = []
    for dim_data in data["dimensions"]:
        dim_key = dim_data["dim_key"]
        dim_info = RUBRIC.get(dim_key, {})
        dimensions.append(DimensionScore(
            name=dim_info.get("name", dim_key),
            score=float(dim_data["score"]),
            max_score=10.0,
            issues=dim_data.get("issues", []),
            suggestions=dim_data.get("suggestions", []),
        ))

    # 计算加权总分（归一化到 100 分制）
    # 使用预计算的 RUBRIC 总权重，避免因 LLM 遗漏维度导致的偏差
    weighted_sum = sum(
        float(dim_data["score"]) * RUBRIC[dim_data["dim_key"]]["weight"]
        for dim_data in data["dimensions"]
        if dim_data["dim_key"] in RUBRIC
    )
    if _RUBRIC_TOTAL_WEIGHT > 0:
        total_score = (weighted_sum / _RUBRIC_TOTAL_WEIGHT) * 10  # 10分制 × 10 = 100分制
    else:
        total_score = 0.0

    return AssetScore(
        asset_name=asset_name,
        asset_path=asset_path,
        dimensions=dimensions,
        total_score=round(total_score, 1),
        overall_comment=data.get("overall_comment", ""),
    )


def format_score_report(asset_score: AssetScore) -> str:
    """格式化评分报告为可读文本"""
    lines = [
        f"========== {asset_score.asset_name} 评分报告 ==========",
        f"总分：{asset_score.total_score}/100",
        f"综合评语：{asset_score.overall_comment}",
        "",
        "各维度详情：",
    ]

    for dim in asset_score.dimensions:
        # 在 RUBRIC 中查找此维度的 key
        dim_key = next((k for k, v in RUBRIC.items() if v["name"] == dim.name), None)
        weight = RUBRIC[dim_key]["weight"] if dim_key else 1.0
        lines.append(f"  [{dim.name}] {dim.score}/10 (权重 {weight})")
        if dim.issues:
            for issue in dim.issues:
                lines.append(f"    [ISSUE] {issue}")
        if dim.suggestions:
            for suggestion in dim.suggestions:
                lines.append(f"    [TIP] {suggestion}")
        lines.append("")

    return "\n".join(lines)


def compare_scores(old_score: AssetScore, new_score: AssetScore) -> Dict:
    """比较两次评分，判断是否改进"""
    diff = new_score.total_score - old_score.total_score
    dim_diffs = {}

    old_dim_map = {d.name: d.score for d in old_score.dimensions}
    for dim in new_score.dimensions:
        old_s = old_dim_map.get(dim.name, 0)
        dim_diffs[dim.name] = dim.score - old_s

    return {
        "total_diff": round(diff, 1),
        "improved": diff > 0,
        "dimension_diffs": dim_diffs,
        "improved_dims": [k for k, v in dim_diffs.items() if v > 0],
        "regressed_dims": [k for k, v in dim_diffs.items() if v < 0],
    }


def format_compare_report(comparison: Dict) -> str:
    """格式化比较报告"""
    lines = [
        f"========== 评分对比 ==========",
        f"总分变化：{comparison['total_diff']:+.1f}",
        f"是否改进：{'是' if comparison['improved'] else '否（棘轮将回滚）'}",
        "",
    ]
    if comparison["improved_dims"]:
        lines.append("改进维度：")
        for dim in comparison["improved_dims"]:
            lines.append(f"  + {dim}: {comparison['dimension_diffs'][dim]:+.1f}")
    if comparison["regressed_dims"]:
        lines.append("退化维度：")
        for dim in comparison["regressed_dims"]:
            lines.append(f"  - {dim}: {comparison['dimension_diffs'][dim]:+.1f}")
    return "\n".join(lines)