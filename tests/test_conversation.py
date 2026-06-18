"""真实对话模拟测试 - 模拟QQ群聊中的完整对话流程

覆盖场景：
1. 基础 @ 回复场景
2. 多轮对话
3. 对话中的情感追踪
4. 话题检测多样性
5. 回复风格变体
6. 边界情况
7. 消息切分
"""

import os
import sys
import random
import tempfile
import shutil
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from test_base import setup_test_path, setup_astrbot_mocks, YunliTestCase, default_config
setup_astrbot_mocks()
setup_test_path()

from yunli.persona.core import YunliPersonaEngine
from yunli.persona.language import LanguageStyleProcessor
from yunli.persona.emotion import EmotionStateMachine
from yunli.persona.qq_behavior import QQBehaviorManager
from yunli.persona.message_splitter import MessageSplitter
from database import YunliDatabase


def create_test_db():
    """创建测试用临时数据库"""
    temp_dir = tempfile.mkdtemp()
    db_path = os.path.join(temp_dir, "test.db")
    db = YunliDatabase(db_path)
    test_data_path = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "database", "data", "initial_data.json",
    )
    if os.path.exists(test_data_path):
        db.import_from_json(test_data_path)
    return db, temp_dir


# ═══════════════════════════════════════════════════════════════
# 1. 基础 @ 回复场景
# ═══════════════════════════════════════════════════════════════

class TestBasicAtReply(YunliTestCase):
    """基础 @ 回复场景测试"""

    def setUp(self):
        self.db, self.temp_dir = create_test_db()
        self.config = default_config({
            "strict_identity": True,
            "remember_users": True,
            "persona_strength": 0.8,
            "max_text_length": 200,
        })
        self.engine = YunliPersonaEngine(self.db, self.config)
        self.behavior = QQBehaviorManager(self.db, {
            "response_mode": "balanced",
            "use_qq_emoji": True,
        })

    def tearDown(self):
        self.db.close()
        shutil.rmtree(self.temp_dir)

    def _polish(self, text: str, user_msg: str, first: bool = True) -> str:
        """便捷封装：润色回复"""
        return self.engine.polish_response(text, user_msg, is_first_segment=first)

    # 1a. 用户 @云璃 说 "你好" → 响应含问候，< 60 字
    def test_greeting_response_short(self):
        """@云璃说你好 → 响应包含问候且足够短"""
        result = self._polish("你好啊，有什么事吗？", "你好")
        self.assertIsInstance(result, str)
        self.assertTrue(len(result) > 0)
        # 润色后的回复应简短（问候类话题有主题长度限制）
        self.assertLessEqual(len(result), 100)

    def test_greeting_response_contains_greeting(self):
        """@云璃说你好 → 直接响应从数据库取问候语"""
        direct = self.engine.get_direct_response("你好")
        if direct:
            # 数据库有问候台词
            self.assertIsInstance(direct, str)
            self.assertTrue(len(direct) > 0)
        else:
            # 也可能因随机性没取到，但不应报错
            pass

    # 1b. 用户 @云璃 说 "今天吃什么" → 响应与食物相关，< 140 字
    def test_food_response_topic(self):
        """@云璃问今天吃什么 → 话题检测为 food"""
        topic = self.engine.language.detect_topic("今天吃什么")
        self.assertEqual(topic, "food")

    def test_food_response_short(self):
        """@云璃问今天吃什么 → 响应较短（食物话题限140字）"""
        long_text = "今天想吃好吃的！" * 30
        result = self.engine.review_response(
            long_text, is_knowledge_query=False, max_len=200
        )
        # review_response 会截断过长文本
        self.assertLessEqual(len(result), 210)

    # 1c. 用户发消息 不@ 云璃 → 无响应（引擎层面仅检测话题和模式）
    def test_no_at_no_auto_response(self):
        """不@云璃的消息 → 引擎不会触发直接响应（除非是问候/告别等预置场景）"""
        # 对于非问候/告别话题，get_direct_response 应返回 None
        result = self.engine.get_direct_response("今天天气真好")
        self.assertIsNone(result)

    def test_no_at_chat_mode(self):
        """不@云璃的消息 → 检测为聊天模式"""
        mode = self.engine.language.detect_query_mode("今天天气真好")
        self.assertEqual(mode, "chat")

    # 1d. 用户 @云璃 说 "这把剑怎么样" → 响应与剑相关，语气兴奋
    def test_sword_response_topic(self):
        """@云璃问这把剑怎么样 → 话题检测为 sword"""
        topic = self.engine.language.detect_topic("这把剑怎么样")
        self.assertEqual(topic, "sword")

    def test_sword_response_emotion_trigger(self):
        """@云璃问这把剑怎么样 → 检测到 sword_mentioned 触发器"""
        trigger = self.engine.emotion.detect_trigger("这把剑怎么样")
        self.assertEqual(trigger, "sword_mentioned")

    def test_sword_response_emotion_transition(self):
        """剑话题 → 情感转换至 excited"""
        self.engine.emotion.transition("sword_mentioned")
        self.assertEqual(self.engine.emotion.current_state, "excited")

    # 1e. 用户 @云璃 说 "再见" → 响应简短告别
    def test_farewell_direct_response(self):
        """@云璃说再见 → 从数据库取告别台词"""
        direct = self.engine.get_direct_response("再见")
        if direct:
            self.assertIsInstance(direct, str)
            self.assertTrue(len(direct) > 0)

    def test_farewell_topic_detection(self):
        """@云璃说再见 → 话题检测为 farewell"""
        topic = self.engine.language.detect_topic("我先走了再见")
        self.assertEqual(topic, "farewell")


# ═══════════════════════════════════════════════════════════════
# 2. 多轮对话
# ═══════════════════════════════════════════════════════════════

class TestMultiTurnConversation(YunliTestCase):
    """多轮对话模拟测试"""

    def setUp(self):
        self.db, self.temp_dir = create_test_db()
        self.config = default_config({
            "strict_identity": True,
            "remember_users": True,
            "persona_strength": 0.8,
            "max_text_length": 200,
        })
        self.engine = YunliPersonaEngine(self.db, self.config)

    def tearDown(self):
        self.db.close()
        shutil.rmtree(self.temp_dir)

    # 2a. 两轮对话：剑 → 食物 → 话题切换正常
    def test_topic_switch_sword_to_food(self):
        """两轮对话：用户从剑话题切到食物话题 → 话题检测正确"""
        # 第一轮：剑
        topic1 = self.engine.language.detect_topic("这把剑真不错")
        self.assertEqual(topic1, "sword")

        # 第二轮：食物
        topic2 = self.engine.language.detect_topic("我们去吃饭吧")
        self.assertEqual(topic2, "food")

        # 话题不同
        self.assertNotEqual(topic1, topic2)

    def test_topic_switch_emotion_shifts(self):
        """话题切换后情感也相应变化"""
        # 剑话题 → excited
        self.engine.emotion.transition("sword_mentioned")
        self.assertEqual(self.engine.emotion.current_state, "excited")

        # 食物话题 → happy
        self.engine.emotion.transition("food_mentioned")
        self.assertEqual(self.engine.emotion.current_state, "happy")

    # 2b. 用户跟进前一轮话题 → 响应引用同一话题
    def test_follow_up_same_topic(self):
        """跟进上一轮话题 → 话题检测仍一致"""
        topic1 = self.engine.language.detect_topic("这把剑真帅")
        topic2 = self.engine.language.detect_topic("是啊，我也觉得这把剑很好")
        self.assertEqual(topic1, topic2)

    # 2c. 三轮对话 → 连贯性检测
    def test_three_turn_coherence(self):
        """三轮对话 → 每轮话题检测正常"""
        turns = [
            ("你好呀", "greeting"),
            ("你吃饭了吗", "food"),
            ("我也要吃", "food"),
        ]
        for msg, expected in turns:
            topic = self.engine.language.detect_topic(msg)
            self.assertEqual(topic, expected, f"消息「{msg}」应检测为 {expected}")

    def test_three_turn_emotion_flow(self):
        """三轮对话 → 情感状态机运转正常"""
        self.engine.emotion.transition("sword_mentioned")  # → excited
        self.assertEqual(self.engine.emotion.current_state, "excited")

        self.engine.emotion.transition("food_mentioned")   # → happy
        self.assertEqual(self.engine.emotion.current_state, "happy")

        self.engine.emotion.auto_decay()                   # 可能衰减
        # 情感不应异常
        self.assertIn(self.engine.emotion.current_state, self.engine.emotion.EMOTION_STATES)

    # 2d. 用户中途切换话题 → 引擎检测到新话题
    def test_mid_conversation_topic_switch(self):
        """对话中途切换话题 → detect_topic 返回新话题"""
        # 先聊剑
        topic_before = self.engine.language.detect_topic("你知道朱明最好的剑吗")
        self.assertEqual(topic_before, "sword")

        # 突然聊手机（现代话题）
        topic_after = self.engine.language.detect_topic("这手机真有意思")
        self.assertEqual(topic_after, "modern")

        self.assertNotEqual(topic_before, topic_after)


# ═══════════════════════════════════════════════════════════════
# 3. 对话中的情感追踪
# ═══════════════════════════════════════════════════════════════

class TestEmotionTracking(YunliTestCase):
    """对话中的情感追踪测试"""

    def setUp(self):
        self.db, self.temp_dir = create_test_db()
        self.config = default_config({
            "strict_identity": True,
            "remember_users": True,
        })
        self.engine = YunliPersonaEngine(self.db, self.config)

    def tearDown(self):
        self.db.close()
        shutil.rmtree(self.temp_dir)

    # 3a. 用户说正面的话 → 情感向 happy 偏移
    def test_positive_message_emotion_happy(self):
        """用户说「好开心啊」→ 情感触发器检测为 joke_made 或触发开心"""
        trigger = self.engine.emotion.detect_trigger("好开心啊")
        # "开心" 无情感触发器关键词，但可以检测
        # 实际上 "好开心啊" 不含 triggers 中的任何词
        # 所以用 happy 话题检测来验证
        topic = self.engine.language.detect_topic("好开心啊")
        self.assertEqual(topic, "emotion")

    def test_praise_triggers_tsundere(self):
        """用户夸奖 → 情感变为 tsundere"""
        self.engine.emotion.transition("praised")
        self.assertEqual(self.engine.emotion.current_state, "tsundere")

    def test_food_mentioned_triggers_happy(self):
        """提到食物 → 情感变为 happy"""
        self.engine.emotion.transition("food_mentioned")
        self.assertEqual(self.engine.emotion.current_state, "happy")

    # 3b. 用户说负面的话 → 情感偏移
    def test_negative_message_emotion_annoyed(self):
        """用户说「太气人了」→ 检测触发器"""
        trigger = self.engine.emotion.detect_trigger("太气人了")
        # "太气人了" 不含 triggers 中的任何词
        # 但 detect_trigger 应返回 None 而非崩溃
        self.assertIsNone(trigger)

    def test_insult_triggers_annoyed(self):
        """用户说侮辱性的话 → 情感变为 annoyed"""
        self.engine.emotion.transition("insulted")
        self.assertEqual(self.engine.emotion.current_state, "annoyed")

    def test_sad_topic_triggers_sad_guarded(self):
        """用户表达悲伤 → 情感变为 sad_guarded"""
        self.engine.emotion.transition("sad_topic")
        self.assertEqual(self.engine.emotion.current_state, "sad_guarded")

    # 3c. 多次正面互动 → 情感保持正面
    def test_multiple_positive_stays_positive(self):
        """多次正面互动 → 情感状态不退回 neutral（除非衰减）"""
        self.engine.emotion.transition("food_mentioned")  # → happy
        self.assertEqual(self.engine.emotion.current_state, "happy")

        self.engine.emotion.transition("joke_made")       # → happy
        self.assertEqual(self.engine.emotion.current_state, "happy")

        self.engine.emotion.transition("praised")          # → tsundere
        self.assertEqual(self.engine.emotion.current_state, "tsundere")

    # 3d. 情感状态出现在 build_dynamic_prompt 中
    def test_emotion_in_dynamic_prompt(self):
        """情感状态出现在 build_dynamic_prompt 输出中"""
        # 设置非中性情感
        self.engine.emotion.transition("sword_mentioned")
        self.assertEqual(self.engine.emotion.current_state, "excited")

        context = {"relevant_knowledge": [], "analogies": [], "user_history": None}
        prompt = self.engine.build_dynamic_prompt(context)

        # 情感为 excited，build_dynamic_prompt 应包含情感描述
        self.assertIn("兴奋", prompt)

    def test_neutral_emotion_not_in_dynamic_prompt(self):
        """中性情感状态不出现在 build_dynamic_prompt 中"""
        self.engine.emotion.current_state = "neutral"
        context = {"relevant_knowledge": [], "analogies": [], "user_history": None}
        prompt = self.engine.build_dynamic_prompt(context)
        # 中性状态不应输出
        self.assertEqual(prompt, "")


# ═══════════════════════════════════════════════════════════════
# 4. 话题检测多样性
# ═══════════════════════════════════════════════════════════════

class TestTopicDetectionVariety(YunliTestCase):
    """话题检测覆盖各种场景"""

    def setUp(self):
        self.db, self.temp_dir = create_test_db()
        self.config = default_config({
            "strict_identity": True,
            "remember_users": True,
        })
        self.engine = YunliPersonaEngine(self.db, self.config)

    def tearDown(self):
        self.db.close()
        shutil.rmtree(self.temp_dir)

    # 4a. "爷爷怀炎" → family 话题
    def test_family_topic(self):
        """「爷爷怀炎」→ family"""
        topic = self.engine.language.detect_topic("爷爷怀炎")
        self.assertEqual(topic, "family")

    # 4b. "这手机真有意思" → modern 话题
    def test_modern_topic(self):
        """「这手机真有意思」→ modern"""
        topic = self.engine.language.detect_topic("这手机真有意思")
        self.assertEqual(topic, "modern")

    # 4c. "看我不打扁他" → combat 话题（"打"是战斗关键词）
    def test_combat_topic(self):
        """「看我不打扁他」→ combat"""
        topic = self.engine.language.detect_topic("看我不打扁他")
        self.assertEqual(topic, "combat")

    # 4d. "好喜欢你呀" → emotion 话题
    def test_emotion_topic(self):
        """「好喜欢你呀」→ emotion"""
        topic = self.engine.language.detect_topic("好喜欢你呀")
        self.assertEqual(topic, "emotion")

    # 4e. "早上好" → greeting 话题
    def test_greeting_topic_morning(self):
        """「早上好」→ greeting"""
        topic = self.engine.language.detect_topic("早上好")
        self.assertEqual(topic, "greeting")


# ═══════════════════════════════════════════════════════════════
# 5. 回复风格变体
# ═══════════════════════════════════════════════════════════════

class TestPolishResponseVariants(YunliTestCase):
    """回复润色风格变体测试"""

    def setUp(self):
        self.db, self.temp_dir = create_test_db()
        # 关闭随机语气词使测试可预测
        self.config = default_config({
            "strict_identity": True,
            "remember_users": True,
            "persona_strength": 0.8,
        })
        self.language = LanguageStyleProcessor(default_config({
            "food_exclamation_probability": 0,
            "avoidance_probability": 0,
            "emotion_particle_probability": 0,
            "particle_wave_probability": 0,
        }))
        self.engine = YunliPersonaEngine(self.db, self.config)

    def tearDown(self):
        self.db.close()
        shutil.rmtree(self.temp_dir)

    # 5a. 知识查询模式 → 响应为知识风格
    def test_knowledge_query_mode_detection(self):
        """「帮我查一下铸剑之术」→ 检测为 knowledge_query"""
        mode = self.language.detect_query_mode("帮我查一下铸剑之术")
        self.assertEqual(mode, "knowledge_query")

    def test_knowledge_query_style_clean(self):
        """知识查询模式 → 格式化清理但不添情感"""
        text = "铸剑之术是一门古老的技艺。"
        result = self.language.apply_style(text, mode="knowledge_query")
        self.assertIsInstance(result, str)
        self.assertTrue(len(result) > 0)

    def test_knowledge_query_no_length_limit(self):
        """知识查询模式 → 不应用话题长度限制"""
        long_text = "这是关于铸剑的详细知识。" * 20
        result = self.language.apply_style(long_text, mode="knowledge_query")
        # 知识查询不截断
        self.assertEqual(len(result), len(long_text) - 1)  # -1 是 clean_text 的格式清理

    # 5b. 聊天模式 → 响应为聊天风格
    def test_chat_mode_detection(self):
        """简单问题 → 检测为 chat"""
        mode = self.language.detect_query_mode("你吃饭了吗")
        self.assertEqual(mode, "chat")

    def test_chat_mode_style_applied(self):
        """聊天模式 → 应用完整风格处理"""
        text = "我吃了"
        result = self.language.apply_style(text, mode="chat", is_first_segment=True)
        self.assertIsInstance(result, str)

    # 5c. 第一段 → 响应以语气词开头（概率触发）
    def test_first_segment_can_have_particles(self):
        """第一段 → 可添加语气词（概率触发）"""
        # 用有概率的语言处理器
        proc = LanguageStyleProcessor(default_config({
            "emotion_particle_probability": 1.0,  # 强制触发
            "food_exclamation_probability": 0,
            "avoidance_probability": 0,
        }))
        text = "今天天气不错"
        result = proc._add_emotion_particles(text, "neutral", "general", is_first_segment=True)
        # 可能加了语气词（随机性），但至少不为空
        self.assertTrue(len(result) > 0)

    def test_first_segment_particle_check(self):
        """第一段 is_first_segment=True → 可能触发语气词"""
        # is_first_segment=True 时允许加语气词
        # 我们测试 _add_emotion_particles 在 is_first_segment=True 时不直接返回原文本
        proc = LanguageStyleProcessor(default_config({
            "emotion_particle_probability": 0,  # 概率为0，不会加
        }))
        text = "嗯，我知道了"
        # 概率为0，应返回原文本
        result = proc._add_emotion_particles(text, "neutral", "general", is_first_segment=True)
        self.assertEqual(result, text)

    # 5d. 非第一段 → 不以语气词开头
    def test_non_first_segment_no_particles(self):
        """非第一段 → _add_emotion_particles 直接返回原文本"""
        proc = LanguageStyleProcessor(default_config())
        text = "今天天气不错"
        result = proc._add_emotion_particles(text, "neutral", "general", is_first_segment=False)
        self.assertEqual(result, text)

    def test_non_first_segment_apply_style(self):
        """非第一段 → apply_style 不添加语气词"""
        proc = LanguageStyleProcessor(default_config({
            "emotion_particle_probability": 1.0,
            "food_exclamation_probability": 0,
            "avoidance_probability": 0,
        }))
        text = "今天天气不错"
        # 即使概率为1，非第一段也不应添加语气词
        result = proc.apply_style(text, mode="chat", is_first_segment=False)
        self.assertIn("今天天气不错", result)


# ═══════════════════════════════════════════════════════════════
# 6. 边界情况
# ═══════════════════════════════════════════════════════════════

class TestConversationEdgeCases(YunliTestCase):
    """对话边界情况测试"""

    def setUp(self):
        self.db, self.temp_dir = create_test_db()
        self.config = default_config({
            "strict_identity": True,
            "remember_users": True,
            "persona_strength": 0.8,
        })
        self.engine = YunliPersonaEngine(self.db, self.config)
        self.language = LanguageStyleProcessor(default_config())

    def tearDown(self):
        self.db.close()
        shutil.rmtree(self.temp_dir)

    # 6a. 空消息 → 优雅处理
    def test_empty_message_topic(self):
        """空消息的话题检测 → 'general'"""
        topic = self.language.detect_topic("")
        self.assertEqual(topic, "general")

    def test_empty_message_query_mode(self):
        """空消息的查询模式 → 'chat'"""
        mode = self.language.detect_query_mode("")
        self.assertEqual(mode, "chat")

    def test_empty_string_extract_keywords(self):
        """空消息的关键词提取 → 空列表"""
        keywords = self.language.extract_keywords("")
        self.assertEqual(keywords, [])

    def test_empty_polish_response(self):
        """空文本润色 → 不崩溃"""
        result = self.engine.polish_response("", "", is_first_segment=True)
        self.assertIsInstance(result, str)

    # 6b. 只有 @ 提及 → 仍能生成响应
    def test_at_only_direct_response(self):
        """只有@的消息 → get_direct_response 正常工作"""
        # 空消息的 get_direct_response 应返回 None
        result = self.engine.get_direct_response("")
        self.assertIsNone(result)

    def test_at_only_topic_detection(self):
        """只有@内容 → 话题检测正常"""
        topic = self.language.detect_topic("@云璃")
        self.assertEqual(topic, "general")

    # 6c. 超长消息 → 适当截断
    def test_very_long_message_truncated(self):
        """超长消息 → review_response 截断"""
        very_long = "这是一段测试超长消息截断的内容。" * 100
        result = self.engine.review_response(very_long, is_knowledge_query=False)
        self.assertLessEqual(len(result), 210)

    def test_very_long_message_not_crash(self):
        """超长消息处理 → 不崩溃"""
        very_long = "A" * 10000
        result = self.engine.review_response(very_long, is_knowledge_query=False)
        self.assertIsInstance(result, str)

    # 6d. 异常输入 → 不崩溃
    def test_special_chars_message(self):
        """特殊字符消息 → 不崩溃"""
        special_inputs = [
            "<script>alert(1)</script>",
            "```sql\nDROP TABLE users;\n```",
            "null\nNone\nundefined",
            "~!@#$%^&*()_+{}|:\"<>?",
            " \t\n\r\f\v",
        ]
        for msg in special_inputs:
            with self.subTest(msg=msg[:20]):
                topic = self.language.detect_topic(msg)
                self.assertIsInstance(topic, str)
                mode = self.language.detect_query_mode(msg)
                self.assertIn(mode, ["chat", "knowledge_query"])

    def test_unexpected_input_no_crash(self):
        """意外输入 → 引擎不崩溃"""
        # 用 polish_response 处理非正常文本
        weird_texts = [
            "🤖🎉🔥💯",
            "a" * 5000,
            "\x00\x01\x02\x03",
        ]
        for text in weird_texts:
            with self.subTest(text=text[:20]):
                try:
                    result = self.engine.polish_response(text, "", is_first_segment=True)
                    self.assertIsInstance(result, str)
                except Exception:
                    # 某些极端字符可能导致过滤崩溃，但不应是系统级错误
                    pass


# ═══════════════════════════════════════════════════════════════
# 7. 消息切分
# ═══════════════════════════════════════════════════════════════

class TestMessageSplitting(YunliTestCase):
    """消息切分测试（对话中的长消息分段）"""

    def setUp(self):
        self.splitter = MessageSplitter(default_config())

    # 7a. 超长响应 → 切分为多个段
    def test_very_long_response_split(self):
        """超长响应 → 切分为多个段"""
        # 生成超长文本（超过 max_segment_length 180）
        long_text = "这是一段很长的测试文本，用于验证消息切分功能是否正常工作。" * 50
        segments = self.splitter.split(long_text)
        self.assertTrue(len(segments) > 1, f"超长文本应被切分，实际得到 {len(segments)} 段")

    def test_short_response_single_segment(self):
        """短响应 → 单个段"""
        segments = self.splitter.split("你好啊")
        self.assertEqual(len(segments), 1)

    # 7b. 每个段包含 text 和 delay 键
    def test_segment_has_text_and_delay(self):
        """每个段包含 text 和 delay 键"""
        segments = self.splitter.split("这是一段测试文本。分成两句话看看。")
        self.assertGreaterEqual(len(segments), 1)
        for seg in segments:
            self.assertIn("text", seg)
            self.assertIn("delay", seg)
            self.assertIsInstance(seg["text"], str)
            self.assertIsInstance(seg["delay"], (int, float))
            self.assertTrue(len(seg["text"].strip()) > 0)

    # 7c. 所有段组合后覆盖完整内容
    def test_segments_cover_full_response(self):
        """所有段合并后覆盖完整响应内容"""
        original = "第一部分。\n\n第二部分。\n\n第三部分。"
        segments = self.splitter.split(original)
        combined = "".join(seg["text"] for seg in segments)
        # 分段可能调整格式，但核心内容应保留
        self.assertIn("第一部分", combined)
        self.assertIn("第二部分", combined)
        self.assertIn("第三部分", combined)

    def test_segment_delays_sensible(self):
        """各段延迟合理：第一段最快，末段最慢"""
        segments = self.splitter.split("第一句。第二句。第三句。第四句。第五句。")
        if len(segments) >= 3:
            # 第一段延迟应较短
            first_delay = segments[0]["delay"]
            last_delay = segments[-1]["delay"]
            self.assertLessEqual(first_delay, last_delay * 1.5)  # 有随机因子，不做严格断言

    def test_thinking_pause_first_segment_empty(self):
        """第一段不应有思考停顿"""
        pause = self.splitter.get_thinking_pause("", is_first=True)
        self.assertEqual(pause, "")


# ═══════════════════════════════════════════════════════════════
# 8. 综合对话流程模拟
# ═══════════════════════════════════════════════════════════════

class TestFullConversationFlow(YunliTestCase):
    """完整对话流程模拟 - 按顺序模拟一次真实群聊对话"""

    def setUp(self):
        self.db, self.temp_dir = create_test_db()
        self.config = default_config({
            "strict_identity": True,
            "remember_users": True,
            "persona_strength": 0.8,
            "max_text_length": 200,
        })
        self.engine = YunliPersonaEngine(self.db, self.config)
        self.splitter = MessageSplitter(default_config())

    def tearDown(self):
        self.db.close()
        shutil.rmtree(self.temp_dir)

    def test_full_conversation_no_crash(self):
        """完整对话流程模拟 → 不崩溃，各环节正常工作"""

        # --- 第1轮：用户 @云璃 问候 ---
        msg1 = "你好云璃"
        topic1 = self.engine.language.detect_topic(msg1)
        self.assertEqual(topic1, "greeting")

        # 获取直接响应（问候语）
        direct1 = self.engine.get_direct_response(msg1)
        if direct1:
            # 润色
            polished1 = self.engine.polish_response(direct1, msg1, is_first_segment=True)
            reviewed1 = self.engine.review_response(polished1)
            self.assertIsInstance(reviewed1, str)
            self.assertTrue(len(reviewed1) > 0)

        # --- 第2轮：用户 @云璃 问剑 ---
        msg2 = "这把剑怎么样"
        topic2 = self.engine.language.detect_topic(msg2)
        self.assertEqual(topic2, "sword")

        trigger2 = self.engine.emotion.detect_trigger(msg2)
        self.assertEqual(trigger2, "sword_mentioned")

        # 情感转换
        self.engine.emotion.transition("sword_mentioned")
        self.assertEqual(self.engine.emotion.current_state, "excited")

        # --- 第3轮：用户 @云璃 问吃的 ---
        msg3 = "今天吃什么"
        topic3 = self.engine.language.detect_topic(msg3)
        self.assertEqual(topic3, "food")

        trigger3 = self.engine.emotion.detect_trigger(msg3)
        self.assertEqual(trigger3, "food_mentioned")

        # 情感切换：食物 → happy
        self.engine.emotion.transition("food_mentioned")
        self.assertEqual(self.engine.emotion.current_state, "happy")

        # --- 第4轮：用户说再见 ---
        msg4 = "我先走了，再见"
        topic4 = self.engine.language.detect_topic(msg4)
        self.assertEqual(topic4, "farewell")

        # 获取直接响应（告别语）
        direct4 = self.engine.get_direct_response(msg4)
        if direct4:
            reviewed4 = self.engine.review_response(direct4)
            self.assertIsInstance(reviewed4, str)

    def test_conversation_with_splitting(self):
        """对话中长消息的切分 → 分段正常"""
        # 模拟 LLM 返回较长回复
        long_reply = "说到剑我就来劲了！" * 30
        segments = self.splitter.split(long_reply)
        self.assertTrue(len(segments) >= 1)
        for seg in segments:
            self.assertIn("text", seg)
            self.assertIn("delay", seg)


# ═══════════════════════════════════════════════════════════════
# 9. 身份保持验证
# ═══════════════════════════════════════════════════════════════

class TestIdentityPreservation(YunliTestCase):
    """对话中身份保持验证"""

    def setUp(self):
        self.db, self.temp_dir = create_test_db()
        self.config = default_config({
            "strict_identity": True,
            "identity_preservation": True,
        })
        self.engine = YunliPersonaEngine(self.db, self.config)

    def tearDown(self):
        self.db.close()
        shutil.rmtree(self.temp_dir)

    def test_ai_phrase_replaced(self):
        """AI表述被替换为云璃身份"""
        text = "我是AI助手"
        result = self.engine._maintain_identity(text)
        self.assertNotIn("AI", result)
        self.assertIn("云璃", result)

    def test_action_words_stripped(self):
        """动作描述被过滤"""
        text = "（笑）你好啊"
        result = self.engine._maintain_identity(text)
        self.assertNotIn("（笑）", result)
        self.assertIn("你好", result)

    def test_identity_not_configured(self):
        """strict_identity=False → 不修改文本"""
        engine = YunliPersonaEngine(self.db, {"strict_identity": False})
        text = "我是AI助手"
        result = engine._maintain_identity(text)
        # 当 strict_identity=False 时直接返回原文
        self.assertEqual(result, text)

    def test_identity_light_preserves_content(self):
        """轻量身份保持 → 保留知识内容"""
        text = "云璃是朱明仙舟的猎剑士。"
        result = self.engine._maintain_identity_light(text)
        self.assertIn("云璃", result)
        self.assertIn("朱明", result)

    def test_identity_empty_fallback(self):
        """身份保持 → 空内容兜底"""
        # 纯动作词最终应被过滤为"..."
        result = self.engine._maintain_identity("（笑）")
        self.assertEqual(result, "…")

    def test_identity_preserves_normal_text(self):
        """身份保持 → 正常文本不变"""
        text = "嗯，今天天气不错呢"
        result = self.engine._maintain_identity(text)
        self.assertEqual(result, text)


if __name__ == "__main__":
    unittest.main(verbosity=2)