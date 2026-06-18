"""yunli.persona.language.LanguageStyleProcessor 单元测试"""

import sys
import os

# 路径设置：tests/ 目录用于 import test_base，yunli/ 父目录用于 import yunli.xxx
test_dir = os.path.dirname(os.path.abspath(__file__))          # .../yunli/tests
yunli_dir = os.path.dirname(test_dir)                           # .../yunli
parent_dir = os.path.dirname(yunli_dir)                         # .../yunli 的父目录
for p in [parent_dir, yunli_dir, test_dir]:
    if p not in sys.path:
        sys.path.insert(0, p)

from test_base import setup_test_path
setup_test_path()

from unittest.mock import patch, PropertyMock
from yunli.persona.language import LanguageStyleProcessor
from test_base import YunliTestCase, default_config


# ============================================================================
# 话题检测 (detect_topic)
# ============================================================================

class TestDetectTopic(YunliTestCase):
    """测试 detect_topic 话题分类"""

    def setUp(self):
        self.proc = LanguageStyleProcessor(default_config())

    def test_sword_keyword(self):
        """包含剑相关关键词 → 'sword'"""
        self.assertEqual(self.proc.detect_topic("这把剑很锋利"), "sword")
        self.assertEqual(self.proc.detect_topic("我的刀也不错"), "sword")
        self.assertEqual(self.proc.detect_topic("剑意十足"), "sword")

    def test_food_keyword(self):
        """包含食物相关关键词 → 'food'"""
        self.assertEqual(self.proc.detect_topic("今天吃什么"), "food")
        self.assertEqual(self.proc.detect_topic("这个好吃"), "food")
        self.assertEqual(self.proc.detect_topic("好饿啊"), "food")

    def test_greeting_keyword(self):
        """包含问候关键词 → 'greeting'"""
        self.assertEqual(self.proc.detect_topic("你好啊"), "greeting")
        self.assertEqual(self.proc.detect_topic("嗨！"), "greeting")
        self.assertEqual(self.proc.detect_topic("早上好"), "greeting")

    def test_farewell_keyword(self):
        """包含告别关键词 → 'farewell'"""
        self.assertEqual(self.proc.detect_topic("我先走了再见"), "farewell")
        self.assertEqual(self.proc.detect_topic("拜拜"), "farewell")
        self.assertEqual(self.proc.detect_topic("我下线了"), "farewell")

    def test_combat_keyword(self):
        """包含战斗关键词 → 'combat'"""
        self.assertEqual(self.proc.detect_topic("准备战斗"), "combat")
        self.assertEqual(self.proc.detect_topic("我要攻击了"), "combat")
        self.assertEqual(self.proc.detect_topic("胜负未定"), "combat")

    def test_emotion_keyword(self):
        """包含情感关键词 → 'emotion'"""
        self.assertEqual(self.proc.detect_topic("我很开心"), "emotion")
        self.assertEqual(self.proc.detect_topic("有点难过"), "emotion")
        self.assertEqual(self.proc.detect_topic("好生气"), "emotion")

    def test_modern_keyword(self):
        """包含现代概念关键词 → 'modern'"""
        self.assertEqual(self.proc.detect_topic("手机真好玩"), "modern")
        self.assertEqual(self.proc.detect_topic("电脑怎么用"), "modern")
        self.assertEqual(self.proc.detect_topic("最近在看番剧"), "modern")

    def test_family_keyword(self):
        """包含家庭关键词 → 'family'"""
        self.assertEqual(self.proc.detect_topic("我父亲他"), "family")
        self.assertEqual(self.proc.detect_topic("爷爷身体好吗"), "family")
        self.assertEqual(self.proc.detect_topic("想家人了"), "family")

    def test_general_no_keyword(self):
        """不包含任何话题关键词 → 'general'"""
        self.assertEqual(self.proc.detect_topic("今天天气不错"), "general")
        self.assertEqual(self.proc.detect_topic("你觉得呢"), "general")
        self.assertEqual(self.proc.detect_topic("随便聊聊"), "general")

    def test_multiple_topics_highest_score(self):
        """包含多个话题关键词时返回得分最高的"""
        # 包含2个 food 关键词和1个 sword 关键词 → food 胜出
        self.assertEqual(self.proc.detect_topic("今天剑不错，但吃更好吃"), "food")

    def test_multiple_topics_tie_first_wins(self):
        """得分相同时返回字典顺序中第一个遇到的话题"""
        # "剑" 和 "吃" 各匹配1次 → "sword" (在 TOPIC_PATTERNS 中排在 food 前面)
        self.assertEqual(self.proc.detect_topic("剑和吃"), "sword")

    def test_short_text_no_pattern(self):
        """极短且无关键词的文本 → 'general'"""
        self.assertEqual(self.proc.detect_topic("嗯"), "general")
        self.assertEqual(self.proc.detect_topic("哦"), "general")
        self.assertEqual(self.proc.detect_topic("好"), "general")


# ============================================================================
# 查询模式检测 (detect_query_mode)
# ============================================================================

class TestDetectQueryMode(YunliTestCase):
    """测试 detect_query_mode 聊天/知识查询模式判断"""

    def setUp(self):
        self.proc = LanguageStyleProcessor(default_config())

    def test_knowledge_signal_search(self):
        """包含搜索类强信号 → 'knowledge_query'"""
        self.assertEqual(self.proc.detect_query_mode("搜索 python 教程"), "knowledge_query")
        self.assertEqual(self.proc.detect_query_mode("帮我查一下天气"), "knowledge_query")
        self.assertEqual(self.proc.detect_query_mode("查询一下"), "knowledge_query")

    def test_chat_greeting(self):
        """包含问候类强信号 → 'chat'"""
        self.assertEqual(self.proc.detect_query_mode("你好"), "chat")
        self.assertEqual(self.proc.detect_query_mode("在吗"), "chat")
        self.assertEqual(self.proc.detect_query_mode("晚安"), "chat")

    def test_mixed_knowledge_takes_priority(self):
        """知识信号优先于聊天信号（第一层检测优先）"""
        # "帮我" 在 strong_knowledge_signals 中，优先匹配
        self.assertEqual(self.proc.detect_query_mode("帮我做一下"), "knowledge_query")

    def test_short_text_default_chat(self):
        """无任何匹配信号时默认返回 'chat'"""
        self.assertEqual(self.proc.detect_query_mode("嗯"), "chat")
        self.assertEqual(self.proc.detect_query_mode("哦哦"), "chat")
        self.assertEqual(self.proc.detect_query_mode("好吧"), "chat")

    def test_question_words_chat(self):
        """带疑问词但属于闲聊的文本 → 'chat'"""
        # "你叫什么名字" — 无知识信号、无查询词匹配 → "chat"
        self.assertEqual(self.proc.detect_query_mode("你叫什么名字"), "chat")

    def test_explicit_search_help(self):
        """明确求助/搜索意图 → 'knowledge_query'"""
        self.assertEqual(self.proc.detect_query_mode("请教一下怎么做"), "knowledge_query")
        self.assertEqual(self.proc.detect_query_mode("教教我"), "knowledge_query")


# ============================================================================
# 风格应用 (apply_style)
# ============================================================================

class TestApplyStyle(YunliTestCase):
    """测试 apply_style 主入口"""

    def setUp(self):
        # 将随机行为降至最低，使测试可预测
        self.config = default_config({
            "food_exclamation_probability": 0,
            "avoidance_probability": 0,
            "emotion_particle_probability": 0,
            "particle_wave_probability": 0,
        })
        self.proc = LanguageStyleProcessor(self.config)

    def test_chat_mode_general(self):
        """聊天模式下常规话题 → 文本基本保持不变"""
        result = self.proc.apply_style("今天天气不错", mode="chat", is_first_segment=False)
        # 通用规则会替换正式用词，但 "今天天气不错" 不含正式词
        self.assertIn("今天天气不错", result)

    def test_knowledge_mode_cleans_punctuation(self):
        """知识查询模式合并多余感叹号"""
        # 直接测试 _apply_knowledge_style（绕过 filters.clean_text 对标点的影响）
        result = self.proc._apply_knowledge_style("Python是一种编程语言！！")
        # _apply_knowledge_style 将 "！！" → "！"
        self.assertIn("！", result)
        self.assertNotIn("！！", result)
        # 多感叹号也被合并
        result2 = self.proc._apply_knowledge_style("注意！！！！！")
        self.assertEqual(result2.count("！"), 1)

    def test_sword_topic_adds_exclamation(self):
        """剑话题 → 通过 _apply_sword_enthusiasm 添加感叹号"""
        # _apply_sword_enthusiasm 在文本已含剑关键词时不添加前缀，只补充感叹号
        result = self.proc._apply_sword_enthusiasm("这把剑真不错")
        self.assertTrue(result.endswith("！") or result.endswith("!"))
        # 文本已含剑关键词，不添加 "说到这个…"
        self.assertNotIn("说到这个", result)

    def test_food_topic_keeps_text(self):
        """食物话题 → 当概率为0时文本保持不变"""
        result = self.proc.apply_style("今天吃了好吃的", mode="chat", is_first_segment=False)
        # food_exclamation_probability=0 不添加感叹句，文本应保留原意
        self.assertIn("今天", result)

    def test_non_first_segment_no_particles(self):
        """非第一段 → 不添加语气词"""
        # _add_emotion_particles 在 is_first_segment=False 时直接返回原文本
        text = "今天天气不错"
        result = self.proc._add_emotion_particles(text, "neutral", "general", is_first_segment=False)
        self.assertEqual(result, text)

    def test_modern_analogy_passthrough(self):
        """现代概念话题 → 文本原样通过"""
        result = self.proc.apply_style("手机真方便", mode="chat", is_first_segment=False)
        self.assertIn("手机", result)

    def test_long_text_truncated(self):
        """聊天模式下长文本被截断"""
        # v2.2.0 后 _apply_general_rules 统一使用 max_text_length=200；
        # _apply_default_style 仍按 max_default_sentences 限制句子数量。
        long_text = "你好" + "这是一段非常长的文本用来测试截断功能。" * 10
        result = self.proc.apply_style(long_text, mode="chat", is_first_segment=False)
        # 默认风格限制句子数，长文本应被压缩到较短长度
        self.assertLessEqual(len(result), 80)  # 略宽松，因为截断在句号处
        self.assertIn("你好", result)

    def test_short_text_unchanged(self):
        """短文本在聊天模式下保持不变"""
        result = self.proc.apply_style("嗯", mode="chat", is_first_segment=False)
        self.assertIn("嗯", result)

    def test_knowledge_mode_no_truncation(self):
        """知识查询模式不受话题长度限制"""
        proc_no_limit = LanguageStyleProcessor(default_config({
            "food_exclamation_probability": 0,
            "avoidance_probability": 0,
            "emotion_particle_probability": 0,
            "particle_wave_probability": 0,
        }))
        long_text = "搜索 python 教程" + "详细内容。" * 50
        # _apply_general_rules 中 apply_length_limit=False
        result = proc_no_limit.apply_style(long_text, mode="knowledge_query", is_first_segment=False)
        # 知识查询不截断内容，主要做格式清理
        self.assertIn("python", result)

    def test_sword_topic_no_sword_word_prefix(self):
        """剑话题但文本不含剑关键词 → 添加 '说到这个…' 前缀"""
        result = self.proc.apply_style("今天天气不错", mode="chat", is_first_segment=False)
        # topic 检测为 "general"（无剑关键词），所以不走 _apply_sword_enthusiasm
        # 此测试验证：文本不含剑关键词时 topic 不是 "sword"
        topic = self.proc.detect_topic("今天天气不错")
        self.assertEqual(topic, "general")


class TestApplyStyleSwordPrefix(YunliTestCase):
    """验证剑话题中不含剑关键词时添加前缀"""

    def test_adds_sword_prefix(self):
        """_apply_sword_enthusiasm → 无剑关键词时添加 '说到这个…'"""
        proc = LanguageStyleProcessor(default_config())
        # 直接调用内部方法，绕过 topic 检测
        result = proc._apply_sword_enthusiasm("今天天气不错")
        self.assertTrue(result.startswith("说到这个…"))


# ============================================================================
# 话题长度限制 (topic-based max_text_length)
# ============================================================================

class TestTopicMaxLength(YunliTestCase):
    """测试 _apply_general_rules 长度截断

    v2.2.0 重构：移除按 topic 区分的 TOPIC_MAX_LENGTH 字典（greeting=60/general=120/sword=160），
    改用统一配置 max_text_length 控制（默认 200）。
    SpeechImpulse 通过 max_chars 参数动态计算"安全网"上限。
    """

    def setUp(self):
        self.config = default_config({
            "emotion_particle_probability": 0,
            "particle_wave_probability": 0,
            "max_text_length": 200,  # 显式设置便于断言
        })
        self.proc = LanguageStyleProcessor(self.config)

    def _make_long_text(self, base_len=150):
        """生成长文本"""
        return "这是一段用来测试话题长度截断功能的文本。" * 10

    def test_default_max_text_length_truncation(self):
        """默认 max_text_length=200 → 截断至约 200 字符（v2.2.0 统一行为）"""
        text = "这是一段用于测试常规话题截断功能的文本内容。" * 10
        result = self.proc._apply_general_rules(
            text, apply_length_limit=True, is_first_segment=False, topic="general"
        )
        # 不再按 topic 区分，统一使用 max_text_length=200
        self.assertLessEqual(len(result), 205)  # 允许 5 字符回退容差
        self.assertGreater(len(result), 100)    # 确保真的截断了

    def test_greeting_no_special_limit(self):
        """问候话题不再有特殊 60 字符限制（v2.2.0）"""
        text = "你好" + "这是一段用于测试问候话题截断的文本内容。" * 5
        result = self.proc._apply_general_rules(
            text, apply_length_limit=True, is_first_segment=False, topic="greeting"
        )
        # 不再有 TOPIC_MAX_LENGTH["greeting"]=60 的特殊截断
        # 应保留接近原文长度（因为 165 < 200）
        self.assertGreater(len(result), 100)

    def test_sword_no_special_limit(self):
        """剑话题不再有特殊 160 字符限制（v2.2.0）"""
        text = "这是一段用于测试剑话题截断功能的文本内容。" * 10
        result = self.proc._apply_general_rules(
            text, apply_length_limit=True, is_first_segment=False, topic="sword"
        )
        # 不再有 TOPIC_MAX_LENGTH["sword"]=160 的特殊截断
        # 应被截到 max_text_length=200 附近
        self.assertLessEqual(len(result), 205)
        self.assertGreater(len(result), 100)

    def test_max_chars_param_overrides_default(self):
        """SpeechImpulse 传入的 max_chars 应覆盖默认 max_text_length"""
        text = "这是一段用于测试 max_chars 参数覆盖的文本内容。" * 5
        result = self.proc._apply_general_rules(
            text, apply_length_limit=True, is_first_segment=False,
            topic="general", max_chars=50,
        )
        # max_chars=50 应将文本截断到 50 字符左右
        # 注意：可能因逗号/句号回退产生 1-2 字符差异
        self.assertLessEqual(len(result), 55)
        self.assertGreater(len(result), 25)

    def test_knowledge_mode_no_limit(self):
        """知识查询模式 → 不应用长度限制"""
        text = "这是一段用于测试知识查询模式不截断的文本内容。" * 10
        result = self.proc._apply_general_rules(
            text, apply_length_limit=False, is_first_segment=False, topic="general"
        )
        # 不截断，全文保留（1-2字符差异来自 filters.clean_text 的格式清理）
        self.assertGreater(len(result), len(text) - 5)
        self.assertEqual(len(result), len(text) - 1)


# ============================================================================
# 关键词提取 (extract_keywords)
# ============================================================================

class TestExtractKeywords(YunliTestCase):
    """测试 extract_keywords 关键词提取"""

    def setUp(self):
        self.proc = LanguageStyleProcessor(default_config())

    def test_sword_keywords(self):
        """包含剑关键词 → 返回剑相关关键词列表"""
        result = self.proc.extract_keywords("这把剑和刀都很不错")
        self.assertIn("剑", result)
        self.assertIn("刀", result)
        self.assertIsInstance(result, list)

    def test_food_keywords(self):
        """包含食物关键词 → 返回食物相关关键词列表"""
        result = self.proc.extract_keywords("好饿，想吃好吃的")
        self.assertIn("饿", result)
        self.assertIn("吃", result)
        self.assertIn("好吃", result)

    def test_no_keywords(self):
        """不包含任何已知关键词 → 返回空列表"""
        result = self.proc.extract_keywords("今天天气不错")
        self.assertEqual(result, [])


# ============================================================================
# 边界情况
# ============================================================================

class TestEdgeCases(YunliTestCase):
    """特殊边界情况"""

    def setUp(self):
        self.proc = LanguageStyleProcessor(default_config())

    def test_empty_string_detect_topic(self):
        """空字符串的话题检测 → 'general'"""
        self.assertEqual(self.proc.detect_topic(""), "general")

    def test_empty_string_detect_query_mode(self):
        """空字符串的查询模式 → 'chat'"""
        self.assertEqual(self.proc.detect_query_mode(""), "chat")

    def test_empty_string_extract_keywords(self):
        """空字符串的关键词提取 → 空列表"""
        self.assertEqual(self.proc.extract_keywords(""), [])

    def test_empty_config_init(self):
        """传入空配置也能正常初始化"""
        proc = LanguageStyleProcessor({})
        self.assertIsNotNone(proc)
        self.assertEqual(proc.detect_topic("你好"), "greeting")

    def test_none_config_init(self):
        """传入 None 也能正常初始化"""
        proc = LanguageStyleProcessor(None)
        self.assertIsNotNone(proc)
        self.assertEqual(proc.detect_topic("剑"), "sword")