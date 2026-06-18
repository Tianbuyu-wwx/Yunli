"""半自动规则生成器

将 PatternDiscovery 发现的模式转化为可执行规则：
1. filter_escape   → 新过滤正则/词表（写入 filters.py）
2. emotion_miss    → 新情感触发器（写入 emotion.py 或数据库）
3. style_gap       → 语言风格调整建议
4. boundary_violation → 新边界规则
5. tone_inconsistency → 语气词调整
6. new_topic       → 新话题检测规则

"半自动"含义：生成规则提案 + 置信度评分，需人工审核后应用。
"""

import json
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional

from .pattern_discovery import DiscoveredPattern


EVOLUTION_DIR = Path(__file__).resolve().parent
RULES_DIR = EVOLUTION_DIR / "pending_rules"
RULES_FILE = RULES_DIR / "pending_rules.json"


class RuleProposal:
    __slots__ = ("rule_id", "source_pattern_id", "rule_type", "target_file",
                 "rule_content", "apply_instructions", "confidence",
                 "generated_at", "reviewed", "accepted", "applied_at")

    def __init__(self, **kwargs):
        self.rule_id = kwargs.get("rule_id", "")
        self.source_pattern_id = kwargs.get("source_pattern_id", "")
        self.rule_type = kwargs.get("rule_type", "")  # filter_regex / emotion_trigger / topic_keyword / boundary_rule / tone_rule
        self.target_file = kwargs.get("target_file", "")  # filters.py / emotion.py / language.py
        self.rule_content = kwargs.get("rule_content", {})  # 具体规则内容（dict）
        self.apply_instructions = kwargs.get("apply_instructions", "")
        self.confidence = kwargs.get("confidence", 0.5)
        self.generated_at = kwargs.get("generated_at", datetime.now().isoformat())
        self.reviewed = kwargs.get("reviewed", False)
        self.accepted = kwargs.get("accepted", False)
        self.applied_at = kwargs.get("applied_at", "")

    def to_dict(self) -> Dict:
        return {k: getattr(self, k) for k in self.__slots__}

    @classmethod
    def from_dict(cls, data: Dict) -> "RuleProposal":
        return cls(**data)


class RuleGenerator:
    CATEGORY_TO_RULE = {
        "filter_escape": "filter_regex",
        "emotion_miss": "emotion_trigger",
        "style_gap": "tone_rule",
        "boundary_violation": "boundary_rule",
        "tone_inconsistency": "tone_rule",
        "new_topic": "topic_keyword",
    }

    # 默认目标文件映射（可被构造函数覆盖）
    DEFAULT_TARGET_FILES = {
        "filter_regex": "persona/filters.py",
        "emotion_trigger": "persona/emotion.py",
        "topic_keyword": "persona/language.py",
        "boundary_rule": "persona/core.py",
        "tone_rule": "persona/qq_behavior.py",
    }

    def __init__(self, provider=None, log_callback=None,
                 target_files: Optional[Dict[str, str]] = None):
        self.provider = provider
        self._log = log_callback or print
        self._target_files = target_files or self.DEFAULT_TARGET_FILES
        RULES_DIR.mkdir(parents=True, exist_ok=True)

    def generate(self, patterns: List[DiscoveredPattern]) -> List[RuleProposal]:
        proposals = []
        existing = self.load_all()
        existing_ids = {r.source_pattern_id for r in existing}

        for pattern in patterns:
            if pattern.pattern_id in existing_ids:
                continue
            if not pattern.accepted:
                continue

            rule_type = self.CATEGORY_TO_RULE.get(pattern.category, "unknown")
            proposal = self._generate_single(pattern, rule_type)
            if proposal:
                proposals.append(proposal)

        if proposals:
            self._save(existing + proposals)
            self._log(f"  已生成 {len(proposals)} 条规则提案")
        return proposals

    def _generate_single(self, pattern: DiscoveredPattern, rule_type: str) -> Optional[RuleProposal]:
        rule_id = f"rule_{pattern.pattern_id}"

        if rule_type == "filter_regex":
            rule_content = self._gen_filter_regex(pattern)
        elif rule_type == "emotion_trigger":
            rule_content = self._gen_emotion_trigger(pattern)
        elif rule_type == "topic_keyword":
            rule_content = self._gen_topic_keyword(pattern)
        elif rule_type == "boundary_rule":
            rule_content = self._gen_boundary_rule(pattern)
        elif rule_type == "tone_rule":
            rule_content = self._gen_tone_rule(pattern)
        else:
            self._log(f"    [警告] 未知规则类型: {rule_type}（category: {pattern.category}），跳过")
            return None

        proposal = RuleProposal(
            rule_id=rule_id,
            source_pattern_id=pattern.pattern_id,
            rule_type=rule_type,
            target_file=self._get_target_file(rule_type),
            rule_content=rule_content,
            apply_instructions=self._gen_apply_instructions(rule_type, rule_content),
            confidence=pattern.confidence,
        )

        # 安全沙箱：拒绝包含可执行代码注入的规则提案
        if not self._validate_proposal_safety(proposal):
            return None

        return proposal

    def _gen_filter_regex(self, pattern: DiscoveredPattern) -> Dict:
        keywords = []
        for ex in pattern.examples:
            keywords.extend(_extract_keywords(ex))
        # 安全处理：对 LLM 生成的 suggested_fix 进行正则合法性验证
        # 不合法则降级为纯字符串匹配关键词
        raw_pattern = pattern.suggested_fix or ""
        safe_pattern = _validate_regex_or_fallback(raw_pattern)
        return {
            "type": "filter_regex",
            "layer": "format",  # 默认插入到格式过滤层
            "pattern": safe_pattern,
            "keywords": list(set(keywords)),
            "description": pattern.description,
        }

    def _gen_emotion_trigger(self, pattern: DiscoveredPattern) -> Dict:
        keywords = []
        for ex in pattern.examples:
            keywords.extend(_extract_keywords(ex))
        return {
            "type": "emotion_trigger",
            "trigger_name": f"auto_{pattern.pattern_id}",
            "trigger_words": list(set(keywords)),
            "target_emotion": pattern.suggested_fix or "neutral",
            "description": pattern.description,
        }

    def _gen_topic_keyword(self, pattern: DiscoveredPattern) -> Dict:
        keywords = []
        for ex in pattern.examples:
            keywords.extend(_extract_keywords(ex))
        return {
            "type": "topic_keyword",
            "topic_name": f"auto_{pattern.pattern_id}",
            "keywords": list(set(keywords)),
            "max_length": 120,
            "description": pattern.description,
        }

    def _gen_boundary_rule(self, pattern: DiscoveredPattern) -> Dict:
        return {
            "type": "boundary_rule",
            "rule": pattern.suggested_fix,
            "description": pattern.description,
        }

    def _gen_tone_rule(self, pattern: DiscoveredPattern) -> Dict:
        return {
            "type": "tone_rule",
            "rule": pattern.suggested_fix,
            "description": pattern.description,
        }

    def _validate_proposal_safety(self, proposal: RuleProposal) -> bool:
        """安全沙箱：检查规则内容是否包含可执行代码注入。

        仅当文本片段看起来像代码时才进行完整 AST 校验，避免误杀自然语言、
        正则字符串等正常规则内容。
        """
        try:
            from .code_sandbox import iter_string_values, validate_text_if_code
        except ImportError:
            from evolution.code_sandbox import iter_string_values, validate_text_if_code

        for text in iter_string_values(proposal.rule_content):
            ok, reason = validate_text_if_code(text)
            if not ok:
                self._log(
                    f"    [安全沙箱] 拒绝规则 {proposal.rule_id}: {reason}"
                )
                return False
        return True

    def _get_target_file(self, rule_type: str) -> str:
        return self._target_files.get(rule_type, "unknown")

    def _gen_apply_instructions(self, rule_type: str, content: Dict) -> str:
        if rule_type == "filter_regex":
            return f"在 filters.py 的 MODERN_ACTION_WORDS 中添加关键词: {content.get('keywords', [])}"
        elif rule_type == "emotion_trigger":
            return f"在 emotion.py 的 EMOTION_TRIGGERS 中添加触发器: {content.get('trigger_name', '')}"
        elif rule_type == "topic_keyword":
            return f"在 language.py 的 TOPIC_KEYWORDS 中添加话题: {content.get('topic_name', '')}"
        elif rule_type == "boundary_rule":
            return f"在 core.py 的 BASE_SYSTEM_PROMPT 中添加: {content.get('rule', '')}"
        else:
            return "手动审查并添加到对应模块"

    def _save(self, proposals: List[RuleProposal]):
        """保存规则提案到文件（线程安全）"""
        try:
            from ._locks import rules_lock
        except ImportError:
            from evolution._locks import rules_lock

        with rules_lock:
            RULES_FILE.write_text(json.dumps([p.to_dict() for p in proposals], ensure_ascii=False, indent=2), encoding="utf-8")

    def load_all(self) -> List[RuleProposal]:
        if not RULES_FILE.exists():
            return []
        return [RuleProposal.from_dict(item) for item in json.loads(RULES_FILE.read_text(encoding="utf-8"))]

    def load_unreviewed(self) -> List[RuleProposal]:
        return [p for p in self.load_all() if not p.reviewed]

    def get_report(self) -> str:
        proposals = self.load_all()
        unreviewed = self.load_unreviewed()
        lines = []
        lines.append(f"=== 规则提案报告 ===")
        lines.append(f"总计: {len(proposals)} 条（待审核: {len(unreviewed)} 条）\n")
        for p in proposals:
            status = "已接受" if p.accepted else ("已拒绝" if p.reviewed else "待审核")
            lines.append(f"[{p.rule_type}] {status} (置信度: {p.confidence:.0%})")
            lines.append(f"  目标: {p.target_file}")
            lines.append(f"  内容: {json.dumps(p.rule_content, ensure_ascii=False)[:120]}")
            lines.append(f"  操作: {p.apply_instructions[:120]}")
            lines.append("")
        return "\n".join(lines)


def _validate_regex_or_fallback(pattern_str: str) -> str:
    """验证正则表达式合法性，不合法则转义为纯字符串匹配

    防止 LLM 生成的文本直接用作正则导致 ReDoS 或运行时异常。
    """
    if not pattern_str:
        return ""
    import re
    try:
        re.compile(pattern_str)
        # 额外检查：拒绝已知的危险模式（嵌套量词）
        if re.search(r'\([^)]*[+*][^)]*\)[+*]', pattern_str):
            # 嵌套量词模式，可能导致 ReDoS，降级为纯字符串
            return re.escape(pattern_str)
        return pattern_str
    except re.error:
        # 正则不合法，转义为纯字符串匹配
        return re.escape(pattern_str)


def _extract_keywords(text: str) -> List[str]:
    """从文本中提取关键词（简单版：2-6字的中文片段）"""
    import re
    words = re.findall(r'[\u4e00-\u9fff]{2,6}', text)
    return list(set(words))[:10]