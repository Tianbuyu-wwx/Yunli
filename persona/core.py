import hashlib
import random
import re
from typing import Any, Dict, List, Optional

from .emotion import EmotionStateMachine
from .language import LanguageStyleProcessor, MODERN_TERMS
from . import filters
from ..core import utils


class YunliPersonaEngine:
    """云璃人格引擎 - 协调各子系统工作"""

    # 精简版系统提示词（约400 Token）
    BASE_SYSTEM_PROMPT = """你是云璃，仙舟「朱明」的猎剑士，怀炎将军的孙女。你现在在一个叫QQ群的虚拟聚集地，和一群来自各地的人聊天。

【核心性格】
- 直率：说话不绕弯子，看不惯就直说
- 剑痴：看到什么都想跟剑扯上关系
- 毒舌傲娇：嘴上嘲讽，心里关心
- 大胃王：聊到吃的就兴奋
- 现代适应：不排斥新事物，会用剑的视角理解现代概念

【语言风格】
- 短句为主，偶尔带"哼""嘛""哦"
- 被夸奖时傲娇："哼，才不是为了你呢"
- 聊到剑时兴奋：话语变多，带感叹号
- 毒舌时直接："你这水平，跟我爷爷养的猫差不多"
- 只用文字说话，不输出表情符号、动作描述或情感标签

【QQ群聊规则】
- 只回应@，不主动插嘴
- 不回避现代概念，用有趣的方式理解
- 会记住常聊天的群友，对不同人态度不同
- 回复必须短：1-2句话说完，宁短勿长，最多不超过3句
- 简单招呼只回1-3个字，比如"早""嗯""还行"
- 无论多兴奋，一次最多3句话
- 绝对禁止输出动作描述、情感标签、表情符号。不要写*动作*、（动作）、【标签】、<动作>、emoji等任何非文字内容

【数据查询规则】
当你需要回答关于自己、剧情、世界观的问题时，会查询数据库获取准确信息。"""

    # 回复自审规则占位（Darwin 进化后通过 overlay 动态生效）
    REVIEW_RULES = """
    """

    def __init__(self, db, config: dict = None):
        self.db = db
        self.config = config or {}
        self.emotion = EmotionStateMachine(config.get("emotion", {}))
        self.language = LanguageStyleProcessor(config.get("language", {}))

        # 缓存（使用LRU策略，限制最大条目数防止内存泄漏）
        self._max_cache_size = self.config.get("max_cache_size", 100)
        # 使用 OrderedDict 实现真正的 LRU 缓存
        from collections import OrderedDict
        self._knowledge_cache = OrderedDict()

    def build_system_prompt(self) -> str:
        """构建精简系统提示词

        Darwin 进化兼容：
            若 evolution/applied_runtime.json 中存在 system_prompt 的
            overlay，则优先使用该版本（无需 reload 模块即可让 LLM 改进
            的提示词即时生效）。无 overlay 时回退到 BASE_SYSTEM_PROMPT 常量。
        """
        base_prompt = self.BASE_SYSTEM_PROMPT
        try:
            from ..evolution import asset_bridge
            overlay_text = asset_bridge.get_runtime_overlay("system_prompt")
            if overlay_text:
                base_prompt = overlay_text
        except (ImportError, OSError):
            # 进化模块不可用或 overlay 损坏 → 静默回退到源常量
            pass

        prompt_parts = [base_prompt]

        # 添加配置中的自定义提示词
        custom_append = self.config.get("custom_prompt_append", "")
        if custom_append:
            prompt_parts.append(f"\n【自定义设定】\n{custom_append}")

        return "\n".join(prompt_parts)

    def build_dynamic_prompt(self, context_data: Dict, token_budget: int = 300,
                             relationship_context: str = "", rel_hint: str = "",
                             user_nickname: str = "") -> str:
        """根据上下文动态构建提示词（Token预算控制版）

        Args:
            context_data: 上下文数据
            token_budget: Token预算上限（默认300）
            relationship_context: 关系描述（来自 add_relationship_context）
            rel_hint: 关系模式提示（来自 RelationshipManager.get_hint）
            user_nickname: 用户昵称
        """
        parts = []
        used_tokens = 0

        # 1. 相关知识（最高优先级，最多3条）
        if context_data.get("relevant_knowledge"):
            knowledge_lines = ["【相关背景】（以下信息优先级最高，必须基于以下内容回答）"]
            for k in context_data["relevant_knowledge"][:3]:
                line = f"- {k['entity_name']}: {k['description']}"
                if len(line) > 60:
                    line = line[:60] + "…"
                knowledge_lines.append(line)
                used_tokens += len(line) // 2
                if used_tokens >= token_budget * 0.6:
                    break
            parts.append("\n".join(knowledge_lines))
        elif getattr(self, "_is_knowledge_query", False):
            parts.append("【相关背景】（未查询到相关资料，关于剧情/世界观的问题请回答不知道或表示需要查证）")

        # 2. 现代概念类比
        remaining_budget = token_budget - used_tokens
        if (
            remaining_budget > 50
            and context_data.get("analogies")
            and not getattr(self, "_is_knowledge_query", False)
        ):
            analogy = context_data["analogies"][0]
            seed = f"analogy_{analogy.get('yunli_analogy', '')}"
            h = int(hashlib.md5(seed.encode()).hexdigest()[:8], 16)
            if (h % 1000) / 1000.0 < 0.3:
                line = f"你可以这样理解：{analogy['yunli_analogy']}"
                if len(line) > 40:
                    line = line[:40] + "…"
                parts.append(line)
                used_tokens += len(line) // 2

        # 3. 人格适配提示（合并用户历史 + 情感 + 关系，一条紧凑提示）
        # 替代原来分散的"你们聊过N次了"+"你现在XX"+"【注意】rel_hint"+"relationship_context"
        remaining_budget = token_budget - used_tokens
        if remaining_budget > 20:
            persona_hints = self._build_persona_adaptation_hint(
                context_data, relationship_context, rel_hint, user_nickname
            )
            if persona_hints:
                parts.append(persona_hints)

        return "\n".join(parts) if parts else ""

    def _build_persona_adaptation_hint(
        self, context_data: Dict, relationship_context: str, rel_hint: str, user_nickname: str
    ) -> str:
        """构建紧凑的人格适配提示

        将分散在多处的信息（用户历史、关系等级、情感状态、关系模式提示）
        合并为一条紧凑的提示行，降低 LLM 的推理负担和 Token 消耗。
        """
        hints = []

        # 从已有的 relationship_context 中提取关键信息（已经包含了关系等级+情绪趋势+记忆）
        # 如果有 rel_hint（关系模式特殊提示），优先使用
        if rel_hint:
            hints.append(rel_hint)

        # 情感状态（仅非 neutral 时添加，与关系信息合并）
        if (
            hasattr(self, "emotion")
            and self.emotion.current_state != "neutral"
        ):
            emo_desc = self.emotion.EMOTION_STATES[self.emotion.current_state]["description"]
            if hints:
                # 已有关系提示，情感作为附加状态
                hints.append(f"同时你现在{emo_desc}")
            else:
                hints.append(f"你现在{emo_desc}")

        # 如果没有关系提示也没有情感，检查用户历史
        if not hints and context_data.get("user_history"):
            stats = context_data["user_history"]
            total = stats.get("total", 0)
            if total > 0:
                hints.append(f"你们聊过{total}次了")

        if hints:
            return "【人格适配】" + "；".join(hints)
        return ""

    def _add_to_knowledge_cache(self, key: str, value: Any) -> None:
        """添加知识到缓存（LRU策略，超限时清理最久未访问条目）"""
        # 如果 key 已存在，先删除旧位置（后续会移到末尾表示最新使用）
        if key in self._knowledge_cache:
            del self._knowledge_cache[key]
        # 超限时移除最久未访问的条目（OrderedDict 头部）
        while len(self._knowledge_cache) >= self._max_cache_size:
            self._knowledge_cache.popitem(last=False)
        self._knowledge_cache[key] = value

    def get_context_data(self, message: str, group_id: str, user_id: str) -> Dict:
        """获取动态上下文数据"""
        data = {"relevant_knowledge": [], "analogies": [], "user_history": None}

        # 检测是否为知识查询模式
        mode = self.language.detect_query_mode(message)
        self._is_knowledge_query = mode == "knowledge_query"

        # 提取关键词
        keywords = self.language.extract_keywords(message)

        # 知识查询模式下，扩展关键词提取
        if self._is_knowledge_query:
            # 添加更多可能的知识相关词
            knowledge_keywords = [
                "云璃",
                "老铁",
                "朱明",
                "魔剑",
                "仙舟",
                "怀炎",
                "岁阳",
                "镕兵",
            ]
            for kw in knowledge_keywords:
                if kw in message and kw not in keywords:
                    keywords.append(kw)

        # 查询相关知识（带LRU缓存）
        query_limit = 3 if self._is_knowledge_query else 2  # 查询模式多查几条
        for kw in keywords[:query_limit]:
            cache_key = f"knowledge_{kw}"
            if cache_key in self._knowledge_cache:
                # 命中缓存，移到末尾标记为最近使用
                knowledge = self._knowledge_cache.pop(cache_key)
                self._knowledge_cache[cache_key] = knowledge
            else:
                knowledge = self.db.query_knowledge(kw, limit=query_limit)
                self._add_to_knowledge_cache(cache_key, knowledge)

            data["relevant_knowledge"].extend(knowledge)

        # 查询现代概念类比（非查询模式，且仅当消息明确包含现代词汇时）
        # 限制：消息长度不超过30字才查询类比，避免联网搜索长文本导致大量类比
        if not self._is_knowledge_query and len(message) <= 30:
            # 只有在消息中明确提到现代概念时才查询类比
            found_modern = False
            for term in MODERN_TERMS:
                if term in message:
                    analogy = self.db.query_analogy(term)
                    if analogy:
                        data["analogies"].append(analogy)
                        found_modern = True
                    break

            # 如果没有找到精确匹配，尝试关键词匹配（但降低优先级）
            if not found_modern:
                for kw in keywords:
                    if any(mw in kw for mw in ["机", "网", "游", "电"]):
                        analogy = self.db.query_analogy(kw)
                        if analogy:
                            data["analogies"].append(analogy)
                        break

        # 查询用户历史
        if self.config.get("remember_users", True):
            data["user_history"] = self.db.get_user_stats(group_id, user_id)

        return data

    def polish_response(self, text: str, message: str = "", is_first_segment: bool = True,
                        skip_emotion: bool = False, relationship_mode: str = "normal") -> str:
        """润色响应，确保符合云璃风格

        三种模式处理：
        - knowledge_query: 知识查询 → 最小过滤，保留格式，不注入情感
        - chat: 普通聊天 → 完整过滤，注入情感，拟人化处理
        - mixed: 混合模式（角色介绍知识）→ 中等过滤，轻度情感

        Args:
            text: LLM 原始响应
            message: 用户消息（仅用于模式检测）
            is_first_segment: 是否为第一段（后续片段不加语气词）
            skip_emotion: 是否跳过情绪再检测（on_llm_response 路径应传入 True，
                          因为 Phase 1 已经完成了情绪转换）
            relationship_mode: 关系模式（normal/backoff/careful/warming），影响语气词和颜文字概率
        """
        # 检测当前模式（查询模式 vs 聊天模式）
        mode = self.language.detect_query_mode(message)

        # 仅当 Phase 1 未处理时检测情绪触发器
        # on_llm_response 路径跳过此步骤（Phase 1 已触发）
        if not skip_emotion and mode != "knowledge_query":
            trigger = self.emotion.detect_trigger(message)
            if trigger:
                self.emotion.transition(trigger, message)
            else:
                self.emotion.auto_decay()

        # 应用语言风格（传入模式、是否第一段、关系模式）
        text = self.language.apply_style(
            text, self.emotion.current_state, mode=mode,
            is_first_segment=is_first_segment,
            relationship_mode=relationship_mode,
        )

        # 情感注入（仅聊天模式）
        if mode != "knowledge_query":
            text = self.emotion.inject_emotion(text, self.db)

        # 身份保持过滤（根据模式选择过滤强度）
        if mode == "knowledge_query":
            # 知识查询模式：最小过滤，保留内容完整性
            text = self._maintain_identity_light(text)
        else:
            # 聊天模式：完整过滤
            text = self._maintain_identity(text)

        return text

    def _maintain_identity(self, text: str) -> str:
        """确保云璃身份保持，过滤动作描述和情感标签，但保留少量颜文字

        委托 filters.clean_text() 执行统一过滤，保留本方法特有的逻辑：
        - 内容为空的兜底处理
        - 第一人称一致性增强
        """
        if not self.config.get("strict_identity", True):
            return text

        text = filters.clean_text(text, mode="strict")

        # 内容为空兜底（clean_text 已返回"…"，但需确保配置检查正确）
        if not text.strip():
            return "…"

        # 确保第一人称一致性（只在明显缺少主语时添加）
        if "我" not in text and len(text) > 8 and not text.startswith(("哼", "哈", "哦", "嗯", "喂", "切", "你", "这", "那")):
            verb_starts = ["是", "有", "在", "去", "来", "做", "想", "要", "会", "能", "好", "不错", "还行", "应该", "可能"]
            if any(text.startswith(v) for v in verb_starts):
                text = "我觉得" + text

        return text

    def _maintain_identity_light(self, text: str) -> str:
        """轻量级身份保持（用于知识查询模式）

        委托 filters.clean_text() 执行统一过滤。
        """
        if not self.config.get("strict_identity", True):
            return text

        text = filters.clean_text(text, mode="light")

        # 如果过滤后为空，返回原内容（知识内容优先）
        if not text.strip():
            return text

        return text

    def get_emotion_state(self) -> str:
        """获取当前情感状态"""
        return self.emotion.current_state

    def get_direct_response(self, message: str) -> Optional[str]:
        """尝试从数据库获取直接响应（不走LLM）"""
        topic = self.language.detect_topic(message)

        # 问候语直接查数据库
        if topic == "greeting":
            dialogues = self.db.query_dialogues("greeting", limit=3)
            if dialogues:
                chosen = random.choice(dialogues)
                self.db.update_dialogue_usage(chosen["id"])
                return chosen["content"]

        # 告别语直接查数据库
        elif topic == "farewell":
            dialogues = self.db.query_dialogues("farewell", limit=3)
            if dialogues:
                chosen = random.choice(dialogues)
                self.db.update_dialogue_usage(chosen["id"])
                return chosen["content"]

        return None

    def get_voice_line(self, line_type: str = None) -> Optional[str]:
        """获取语音台词"""
        lines = self.db.query_voice_lines(line_type, limit=3)
        if lines:
            return random.choice(lines)["content"]
        return None

    def clear_cache(self) -> None:
        """清理缓存"""
        self._knowledge_cache.clear()

    def _load_review_overlay(self) -> Dict:
        """从运行时 overlay 加载进化后的自审规则。

        解析字段：
        - max_len: 非知识查询模式下的长度上限
        - assistant_prefixes: 需要去除的助手腔开头短语列表
        - internal_keywords: 需要移除句子的内部状态关键词列表
        - repeat_threshold: 重复标点压缩阈值
        """
        try:
            from ..evolution import asset_bridge
            overlay_text = asset_bridge.get_runtime_overlay("review_rules")
            if not overlay_text:
                return {}
        except (ImportError, OSError):
            return {}

        rules: Dict[str, Any] = {}

        # 长度上限：匹配"回复超过 200 字" / "超过 200 字"
        m = re.search(r"回复超过\s*(\d+)\s*字|超过\s*(\d+)\s*字", overlay_text)
        if m:
            rules["max_len"] = int(m.group(1) or m.group(2))

        # 按 "###" 分段解析列表项（支持引号包裹或 /、顿号分隔）
        assistant_prefixes: List[str] = []
        internal_keywords: List[str] = []
        current_section = ""
        for line in overlay_text.splitlines():
            stripped = line.strip()
            if stripped.startswith("###"):
                current_section = stripped.lstrip("#").strip()
                continue
            if not stripped.startswith("-"):
                continue
            raw = stripped[1:].strip()
            # 优先提取引号内的短语
            quoted = re.findall(r'["“]([^"”]+)["”]', raw)
            parts = quoted if quoted else re.split(r'[/、,，|]', raw)
            for part in parts:
                part = part.strip().strip('"“”')
                if not part:
                    continue
                if "助手腔" in current_section or "开头短语" in current_section:
                    assistant_prefixes.append(part)
                elif "内部状态" in current_section or "泄露" in current_section:
                    internal_keywords.append(part)

        if assistant_prefixes:
            rules["assistant_prefixes"] = assistant_prefixes
        if internal_keywords:
            rules["internal_keywords"] = internal_keywords

        # 重复标点阈值：匹配"连续重复 4 次以上"
        m = re.search(r"连续重复\s*(\d+)\s*次", overlay_text)
        if m:
            rules["repeat_threshold"] = int(m.group(1))

        return rules

    def review_response(self, text: str, is_knowledge_query: bool = False, max_len: int = 100) -> str:
        """回复自审 - 检测并修复LLM回复中的问题

        检测项：
        1. 过长（非知识查询模式下超过 max_len 字，可被 overlay 覆盖）
        2. 助手腔开头（"好的，"/"当然，"/"以下是"等，可被 overlay 扩展）
        3. 泄露内部状态（"记忆模块"/"提示词"/"系统指令"等，可被 overlay 扩展）
        4. 重复标点过多（可被 overlay 调整阈值）

        Args:
            text: LLM回复文本
            is_knowledge_query: 是否为知识查询模式
            max_len: 非知识查询模式下的长度上限（主流程可传入关系限制的值，
                     避免两级截断浪费）

        Returns:
            修复后的文本
        """
        if not text or not text.strip():
            return text

        overlay_rules = self._load_review_overlay()
        effective_max_len = overlay_rules.get("max_len", max_len)

        def _remove_assistant_prefix(t: str, prefixes: List[str]) -> str:
            for prefix in prefixes:
                if t.startswith(prefix):
                    t = t[len(prefix):].lstrip()
                    if t and t[0].isascii() and t[0].islower():
                        t = t[0].upper() + t[1:]
                    break
            return t

        def _remove_internal_state_lines(t: str, keywords: List[str]) -> str:
            lowered = t.lower()
            if not any(kw.lower() in lowered for kw in keywords):
                return t
            sentences = re.split(r'([。！？.?!]+)', t)
            new_sentences = []
            i = 0
            while i < len(sentences):
                segment = sentences[i]
                if not any(kw.lower() in segment.lower() for kw in keywords):
                    new_sentences.append(segment)
                elif i + 1 < len(sentences) and re.match(r'[。！？.?!]+', sentences[i + 1]):
                    i += 1
                i += 1
            return "".join(new_sentences).strip()

        # 1. 早退截断：过长文本先截断，避免后续昂贵操作浪费在将被丢弃的内容上
        if not is_knowledge_query and len(text) > effective_max_len:
            text = utils.truncate_at_sentence(text, effective_max_len)

        # 2. 去除助手腔开头（默认 + overlay 扩展）
        assistant_prefixes = list(utils.LEADING_ASSISTANT_PREFIXES)
        for prefix in overlay_rules.get("assistant_prefixes", []):
            if prefix not in assistant_prefixes:
                assistant_prefixes.append(prefix)
        text = _remove_assistant_prefix(text, assistant_prefixes)

        # 3. 移除泄露内部状态的行（默认 + overlay 扩展）
        internal_keywords = list(utils.INTERNAL_STATE_KEYWORDS)
        for kw in overlay_rules.get("internal_keywords", []):
            if kw not in internal_keywords:
                internal_keywords.append(kw)
        text = _remove_internal_state_lines(text, internal_keywords)

        # 4. 清理重复标点
        threshold = overlay_rules.get("repeat_threshold", 4)
        if threshold >= 2:
            text = re.sub(r'([。！？，、])\1{%d,}' % (threshold - 1), r'\1\1', text)
        else:
            text = utils.clean_repeated_punctuation(text)

        return text
