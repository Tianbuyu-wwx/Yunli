"""共享工具函数

消除 darwin_evolve.py、scorer.py、pattern_discovery.py 中的 JSON 提取代码克隆。
"""

import json
from typing import Optional


def extract_json_from_response(text: str) -> Optional[dict]:
    """从 LLM 响应中提取 JSON 对象

    处理以下格式：
    1. 纯 JSON: {"key": "value"} 或 [{"key": "value"}]
    2. ```json 包裹: ```json\\n{"key": "value"}\\n```
    3. ``` 包裹: ```\\n{"key": "value"}\\n```

    Returns:
        解析后的 dict 或 list，或 None（解析失败时）
    """
    json_str = text
    if "```json" in text:
        start = text.find("```json") + 7
        end = text.find("```", start)
        if end == -1:
            end = len(text)
        json_str = text[start:end].strip()
    elif "```" in text:
        start = text.find("```") + 3
        end = text.find("```", start)
        if end == -1:
            end = len(text)
        json_str = text[start:end].strip()
    try:
        return json.loads(json_str)
    except (json.JSONDecodeError, ValueError):
        return None
