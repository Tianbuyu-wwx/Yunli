"""LLM 驱动的模式发现引擎

从对话日志中自动发现 6 类模式：
1. filter_escape   — 过滤逃逸：穿越了7层过滤的异常输出
2. emotion_miss    — 情感误判：触发错误情感或遗漏触发
3. style_gap       — 风格偏移：不符合云璃人设的回复
4. boundary_violation — 边界违规：跨越了行为边界
5. tone_inconsistency — 语气不一致：同类场景下语气波动
6. new_topic       — 新话题：当前系统未覆盖但用户频繁提及的话题
"""

import asyncio
import json
import os
import time
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional

from .log_collector import LogCollector, InteractionLog


EVOLUTION_DIR = Path(__file__).resolve().parent
DISCOVERIES_DIR = EVOLUTION_DIR / "discoveries"
DISCOVERIES_FILE = DISCOVERIES_DIR / "discovered_patterns.json"


class DiscoveredPattern:
    __slots__ = ("pattern_id", "category", "severity", "description",
                 "examples", "suggested_fix", "confidence",
                 "discovered_at", "reviewed", "accepted")

    def __init__(self, **kwargs):
        self.pattern_id = kwargs.get("pattern_id", f"pat_{int(time.time())}")
        self.category = kwargs.get("category", "")
        self.severity = kwargs.get("severity", "medium")
        self.description = kwargs.get("description", "")
        self.examples = kwargs.get("examples", [])
        self.suggested_fix = kwargs.get("suggested_fix", "")
        self.confidence = kwargs.get("confidence", 0.5)
        self.discovered_at = kwargs.get("discovered_at", datetime.now().isoformat())
        self.reviewed = kwargs.get("reviewed", False)
        self.accepted = kwargs.get("accepted", False)

    def to_dict(self) -> Dict:
        return {k: getattr(self, k) for k in self.__slots__}

    @classmethod
    def from_dict(cls, data: Dict) -> "DiscoveredPattern":
        return cls(**data)


class PatternDiscovery:
    CATEGORIES = ["filter_escape", "emotion_miss", "style_gap", "boundary_violation", "tone_inconsistency", "new_topic"]
    CATEGORY_NAMES = {"filter_escape": "过滤逃逸", "emotion_miss": "情感误判", "style_gap": "风格偏移", "boundary_violation": "边界违规", "tone_inconsistency": "语气不一致", "new_topic": "新话题发现"}

    def __init__(self, provider=None, log_collector: LogCollector = None, config: dict = None, log_callback=None):
        self.provider = provider
        self.log_collector = log_collector or LogCollector()
        self.config = config or {}
        self._log = log_callback or print
        DISCOVERIES_DIR.mkdir(parents=True, exist_ok=True)

        # 统一 LLM 客户端（含超时保护 + HTTP fallback）
        try:
            from .llm_client import LLMClient
        except ImportError:
            from evolution.llm_client import LLMClient
        self._llm = LLMClient(provider, config, log_callback)

    def _call_llm(self, prompt: str, system_prompt: str = "") -> Optional[str]:
        """同步调用 LLM（委托给 LLMClient）"""
        return self._llm.call(prompt, system_prompt)

    def _build_analyze_prompt(self, logs: List[InteractionLog], category: str) -> str:
        name = self.CATEGORY_NAMES[category]
        lines = [f"## {name}分析\n你是一个AI对话质量分析专家。请分析以下云璃QQ群聊的对话日志，找出" + name + "问题。\n"]
        lines.append("### 对话日志样本（最近%d条）" % len(logs))
        for i, log in enumerate(logs[:50]):
            lines.append(f"[{i+1}] 用户: {log.message[:80]}")
            lines.append(f"    云璃: {log.response_filtered[:80]}")
            lines.append(f"    情感: {log.emotion_state} | 话题: {log.topic} | 过滤: {log.applied_filters}")
        lines.append(f"\n请以JSON格式输出发现的模式列表（最多5条），格式：")
        lines.append('```json\n[{"category": "' + category + '", "severity": "low|medium|high|critical", "description": "问题描述", "examples": ["示例1", "示例2"], "suggested_fix": "建议修复方案", "confidence": 0.0-1.0}]\n```')
        return "\n".join(lines)

    def discover(self, category: str = None, hours: float = 24) -> List[DiscoveredPattern]:
        """发现对话日志中的模式

        当 config 中 evolution_parallel_workers > 0 时，6 个类别的 LLM 分析
        将使用线程池并行执行，加速比约 6x。
        """
        logs = self.log_collector.load_recent(hours=hours)
        if len(logs) < 10:
            self._log(f"  日志不足（{len(logs)}条），跳过分析")
            return []
        categories = [category] if category else self.CATEGORIES

        # 提取 JSON 工具函数（提前导入）
        try:
            from .utils import extract_json_from_response
        except ImportError:
            from evolution.utils import extract_json_from_response

        parallel_workers = self.config.get("evolution_parallel_workers", 0)

        if parallel_workers > 0 and len(categories) > 1:
            # 并行模式：6 个类别同时分析
            patterns = self._discover_parallel(categories, logs, extract_json_from_response, parallel_workers)
        else:
            # 串行模式：逐个类别分析
            patterns = self._discover_serial(categories, logs, extract_json_from_response)

        if patterns:
            self._save(patterns)
        return patterns

    def _discover_serial(self, categories: list, logs: list,
                         extract_json_fn) -> List[DiscoveredPattern]:
        """串行分析各类别"""
        patterns = []
        for cat in categories:
            self._log(f"  分析类别: {self.CATEGORY_NAMES[cat]}")
            found = self._analyze_category(cat, logs, extract_json_fn)
            patterns.extend(found)
        return patterns

    def _discover_parallel(self, categories: list, logs: list,
                           extract_json_fn, max_workers: int) -> List[DiscoveredPattern]:
        """并行分析各类别（线程池）"""
        from concurrent.futures import ThreadPoolExecutor, as_completed

        patterns = []
        self._log(f"  并行分析 {len(categories)} 个类别（{max_workers} 线程）")

        with ThreadPoolExecutor(max_workers=min(max_workers, len(categories))) as executor:
            future_to_cat = {}
            for cat in categories:
                future = executor.submit(self._analyze_category, cat, logs, extract_json_fn)
                future_to_cat[future] = cat

            for future in as_completed(future_to_cat):
                cat = future_to_cat[future]
                try:
                    found = future.result()
                    patterns.extend(found)
                except Exception as e:
                    self._log(f"  [{self.CATEGORY_NAMES[cat]}] 分析异常: {e}")

        return patterns

    def _analyze_category(self, category: str, logs: list,
                          extract_json_fn) -> List[DiscoveredPattern]:
        """分析单个类别（线程工作函数）"""
        self._log(f"  分析类别: {self.CATEGORY_NAMES[category]}")
        prompt = self._build_analyze_prompt(logs, category)
        response = self._call_llm(prompt, "你是一个专业的对话质量分析师。")
        if not response:
            self._log(f"    [跳过] LLM不可用")
            return []
        try:
            data = extract_json_fn(response)
            if data is None:
                self._log(f"    [解析失败] JSON 提取为空")
                return []
            patterns = []
            for item in data:
                pattern = DiscoveredPattern(category=item.get("category", category), severity=item.get("severity", "medium"), description=item.get("description", ""), examples=item.get("examples", []), suggested_fix=item.get("suggested_fix", ""), confidence=float(item.get("confidence", 0.5)))
                patterns.append(pattern)
                self._log(f"    发现: [{pattern.severity}] {pattern.description[:60]}")
            return patterns
        except Exception as e:
            self._log(f"    [解析失败] {e}")
            return []

    def _save(self, patterns: List[DiscoveredPattern]):
        """保存发现到文件（线程安全：原子 read-modify-write）"""
        try:
            from ._locks import discovery_lock
        except ImportError:
            from evolution._locks import discovery_lock

        with discovery_lock:
            existing = self.load_all()
            existing_ids = {p.pattern_id for p in existing}
            new_patterns = [p for p in patterns if p.pattern_id not in existing_ids]
            all_patterns = existing + new_patterns
            DISCOVERIES_FILE.write_text(json.dumps([p.to_dict() for p in all_patterns], ensure_ascii=False, indent=2), encoding="utf-8")
        self._log(f"  已保存 {len(new_patterns)} 条新发现（共 {len(all_patterns)} 条）")

    def load_all(self) -> List[DiscoveredPattern]:
        if not DISCOVERIES_FILE.exists():
            return []
        return [DiscoveredPattern.from_dict(item) for item in json.loads(DISCOVERIES_FILE.read_text(encoding="utf-8"))]

    def load_unreviewed(self) -> List[DiscoveredPattern]:
        return [p for p in self.load_all() if not p.reviewed]

    def _set_review_state(self, pattern_id: str, accepted: bool, action_name: str = "review"):
        """设置模式的审核状态（线程安全）

        accept/reject 的共用实现，仅 accepted 布尔值不同。
        """
        try:
            from ._locks import discovery_lock
        except ImportError:
            from evolution._locks import discovery_lock

        with discovery_lock:
            patterns = self.load_all()
            found = False
            for p in patterns:
                if p.pattern_id == pattern_id:
                    p.reviewed = True
                    p.accepted = accepted
                    found = True
                    break
            if found:
                DISCOVERIES_FILE.write_text(
                    json.dumps([p.to_dict() for p in patterns], ensure_ascii=False, indent=2),
                    encoding="utf-8",
                )
            else:
                self._log(f"  [警告] {action_name}: 未找到模式 {pattern_id}")

    def accept(self, pattern_id: str):
        """接受模式"""
        self._set_review_state(pattern_id, accepted=True, action_name="accept")

    def reject(self, pattern_id: str):
        """拒绝模式"""
        self._set_review_state(pattern_id, accepted=False, action_name="reject")

    def get_stats(self) -> Dict:
        patterns = self.load_all()
        cats = {}
        for p in patterns:
            cats[p.category] = cats.get(p.category, 0) + 1
        return {"total": len(patterns), "unreviewed": len(self.load_unreviewed()), "by_category": cats, "avg_confidence": sum(p.confidence for p in patterns) / len(patterns) if patterns else 0}