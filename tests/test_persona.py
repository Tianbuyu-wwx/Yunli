"""
云璃人格引擎核心测试

测试 yunli.persona.core.YunliPersonaEngine 的全部公共方法和关键私有方法。
"""

import sys
import os
import unittest

# 路径设置：tests/ 目录用于 import test_base，yunli/ 父目录用于 import yunli.xxx
test_dir = os.path.dirname(os.path.abspath(__file__))          # .../yunli/tests
yunli_dir = os.path.dirname(test_dir)                           # .../yunli
parent_dir = os.path.dirname(yunli_dir)                         # .../yunli 的父目录
for p in [parent_dir, yunli_dir, test_dir]:
    if p not in sys.path:
        sys.path.insert(0, p)

from test_base import setup_test_path, YunliTestCase, default_config

setup_test_path()

from unittest.mock import MagicMock, patch

from yunli.persona.core import YunliPersonaEngine
from yunli.persona.emotion import EmotionStateMachine
from yunli.persona.language import LanguageStyleProcessor


class TestYunliPersonaEngine(YunliTestCase):
    """YunliPersonaEngine 完整测试"""

    # ============================================================
    # a. Initialization (3 tests)
    # ============================================================

    def test_01_constructor_stores_config(self):
        """构造函数正确存储配置"""
        cfg = default_config({"custom_key": "custom_val"})
        engine = YunliPersonaEngine(MagicMock(), cfg)
        self.assertEqual(engine.config["custom_key"], "custom_val")
        self.assertEqual(engine.config["strict_identity"], True)

    def test_02_constructor_creates_sub_components(self):
        """构造函数创建子组件"""
        engine = YunliPersonaEngine(MagicMock(), default_config())
        self.assertIsNotNone(engine.emotion)
        self.assertIsNotNone(engine.language)
        self.assertIsInstance(engine.emotion, EmotionStateMachine)
        self.assertIsInstance(engine.language, LanguageStyleProcessor)

    def test_03_missing_config_keys_use_defaults(self):
        """缺失的配置键使用默认值"""
        engine = YunliPersonaEngine(MagicMock(), {})
        # config 本身为空字典
        self.assertEqual(engine.config, {})
        # 子组件使用默认配置创建
        self.assertIsNotNone(engine.emotion)
        self.assertIsNotNone(engine.language)
        # _maintain_identity 中 strict_identity 默认为 True
        self.assertTrue(engine.config.get("strict_identity", True))

    # ============================================================
    # b. build_system_prompt (4 tests)
    # ============================================================

    def test_04_build_system_prompt_contains_identity(self):
        """系统提示词包含云璃身份标识"""
        engine = YunliPersonaEngine(MagicMock(), default_config())
        prompt = engine.build_system_prompt()
        self.assertIn("云璃", prompt)

    def test_05_build_system_prompt_contains_hunter_marker(self):
        """系统提示词包含猎剑士标识"""
        engine = YunliPersonaEngine(MagicMock(), default_config())
        prompt = engine.build_system_prompt()
        self.assertIn("猎剑士", prompt)

    def test_06_build_system_prompt_contains_length_constraint(self):
        """系统提示词包含长度约束（v2.2.0：使用"短句"/"短消息"等表述）"""
        engine = YunliPersonaEngine(MagicMock(), default_config())
        prompt = engine.build_system_prompt()
        # 应包含长度相关约束（v2.2.0 提示词改用"短句"/"短消息"等更自然的表述）
        self.assertTrue(
            "不要太长" in prompt
            or "句话" in prompt
            or "1-2句话" in prompt
            or "短句" in prompt
            or "短消息" in prompt
        )

    def test_07_build_system_prompt_contains_qq_context(self):
        """系统提示词包含QQ群聊规则"""
        engine = YunliPersonaEngine(MagicMock(), default_config())
        prompt = engine.build_system_prompt()
        self.assertIn("QQ群", prompt)

    # ============================================================
    # c. build_dynamic_prompt (3 tests)
    # ============================================================

    def test_08_build_dynamic_prompt_returns_string(self):
        """build_dynamic_prompt 返回字符串"""
        engine = YunliPersonaEngine(MagicMock(), default_config())
        result = engine.build_dynamic_prompt({})
        self.assertIsInstance(result, str)

    def test_09_build_dynamic_prompt_empty_context(self):
        """空上下文返回空字符串"""
        engine = YunliPersonaEngine(MagicMock(), default_config())
        result = engine.build_dynamic_prompt({})
        self.assertEqual(result, "")

    def test_10_build_dynamic_prompt_with_context(self):
        """有上下文数据时返回非空字符串"""
        engine = YunliPersonaEngine(MagicMock(), default_config())
        context = {
            "relevant_knowledge": [
                {"entity_name": "云璃", "description": "仙舟朱明的猎剑士"}
            ],
            "analogies": [],
            "user_history": {"total": 5},
        }
        result = engine.build_dynamic_prompt(context)
        self.assertIsInstance(result, str)

    # ============================================================
    # d. polish_response (8 tests)
    # ============================================================

    def test_11_polish_response_chat_mode(self):
        """聊天模式下应用身份保持"""
        mock_db = MagicMock()
        mock_db.query_emotion_templates.return_value = []
        engine = YunliPersonaEngine(mock_db, default_config())
        # 使用聊天信号作为 message
        result = engine.polish_response(
            "我觉得你今天看起来不错", "你好", is_first_segment=True
        )
        self.assertIsInstance(result, str)
        self.assertGreater(len(result), 0)

    def test_12_polish_response_knowledge_mode(self):
        """知识查询模式使用轻量过滤"""
        mock_db = MagicMock()
        mock_db.query_emotion_templates.return_value = []
        engine = YunliPersonaEngine(mock_db, default_config())
        # 使用知识查询信号作为 message → knowledge_query 模式
        result = engine.polish_response(
            "Python是一种编程语言", "教教我python", is_first_segment=True
        )
        self.assertIsInstance(result, str)
        self.assertGreater(len(result), 0)

    def test_13_polish_response_first_segment(self):
        """is_first_segment=True 时正常处理不断言错误"""
        mock_db = MagicMock()
        mock_db.query_emotion_templates.return_value = []
        engine = YunliPersonaEngine(mock_db, default_config())
        result = engine.polish_response(
            "今天的天气真不错", "今天天气怎么样", is_first_segment=True
        )
        self.assertIsInstance(result, str)

    def test_14_polish_response_not_first_segment(self):
        """is_first_segment=False 时正常处理不断言错误"""
        mock_db = MagicMock()
        mock_db.query_emotion_templates.return_value = []
        engine = YunliPersonaEngine(mock_db, default_config())
        result = engine.polish_response(
            "今天的天气真不错", "今天天气怎么样", is_first_segment=False
        )
        self.assertIsInstance(result, str)

    def test_15_polish_response_short_text(self):
        """短文本正常处理"""
        mock_db = MagicMock()
        mock_db.query_emotion_templates.return_value = []
        engine = YunliPersonaEngine(mock_db, default_config())
        result = engine.polish_response(
            "好的", "你好", is_first_segment=True
        )
        self.assertIsInstance(result, str)
        self.assertGreater(len(result), 0)

    def test_16_polish_response_long_text(self):
        """长文本通过 review_response 可能被截断"""
        mock_db = MagicMock()
        mock_db.query_emotion_templates.return_value = []
        engine = YunliPersonaEngine(mock_db, default_config())
        long_text = "我觉得" + "非常" * 100 + "不错"
        result = engine.polish_response(
            long_text, "你好", is_first_segment=True
        )
        self.assertIsInstance(result, str)

    def test_17_polish_response_filters_actions(self):
        """聊天模式下动作描述被过滤"""
        mock_db = MagicMock()
        mock_db.query_emotion_templates.return_value = []
        engine = YunliPersonaEngine(mock_db, default_config())
        result = engine.polish_response(
            "*笑了笑*你好呀", "你好", is_first_segment=True
        )
        # *动作* 格式应被过滤
        self.assertNotIn("*", result)

    def test_18_polish_response_handles_emoji(self):
        """文本中的 emoji 被处理"""
        mock_db = MagicMock()
        mock_db.query_emotion_templates.return_value = []
        engine = YunliPersonaEngine(mock_db, default_config())
        result = engine.polish_response(
            "你好呀😊", "你好", is_first_segment=True
        )
        self.assertIsInstance(result, str)

    # ============================================================
    # e. _maintain_identity (5 tests)
    # ============================================================

    def test_19_maintain_identity_strict_filters_ai_phrases(self):
        """strict 模式下过滤 AI 表述"""
        engine = YunliPersonaEngine(
            MagicMock(), default_config({"strict_identity": True})
        )
        result = engine._maintain_identity("我是AI，可以帮你回答问题")
        self.assertNotIn("我是AI", result)
        # AI 表述被替换为"我是云璃"
        self.assertIn("我是云璃", result)

    def test_20_maintain_identity_not_strict_unchanged(self):
        """strict_identity=False 时文本基本不变"""
        engine = YunliPersonaEngine(
            MagicMock(), default_config({"strict_identity": False})
        )
        text = "我是AI，可以帮你回答问题"
        result = engine._maintain_identity(text)
        self.assertEqual(result, text)

    def test_21_maintain_identity_keeps_first_person(self):
        """包含'我'的文本保持第一人称"""
        engine = YunliPersonaEngine(
            MagicMock(), default_config({"strict_identity": True})
        )
        # 文本已含"我"，不会再加"我觉得"
        result = engine._maintain_identity("我觉得今天天气不错")
        self.assertIn("我", result)

    def test_22_maintain_identity_empty_text(self):
        """空文本返回兜底内容"""
        engine = YunliPersonaEngine(
            MagicMock(), default_config({"strict_identity": True})
        )
        result = engine._maintain_identity("")
        self.assertEqual(result, "…")

        result2 = engine._maintain_identity("   ")
        self.assertEqual(result2, "…")

    def test_23_maintain_identity_short_text_skips_enhancement(self):
        """短文本（<=8 字）跳过第一人称增强"""
        engine = YunliPersonaEngine(
            MagicMock(), default_config({"strict_identity": True})
        )
        # clean_text("不错") → "不错"（无变化），len=2 <=8，不添加"我觉得"
        result = engine._maintain_identity("不错")
        self.assertIn("不错", result)
        # 短文本不添加"我觉得"前缀
        self.assertNotIn("我觉得", result)

    # ============================================================
    # f. _maintain_identity_light (3 tests)
    # ============================================================

    def test_24_maintain_identity_light_more_lenient(self):
        """轻量模式比 strict 更宽松（保留 emoji 等）"""
        engine = YunliPersonaEngine(
            MagicMock(), default_config({"strict_identity": True})
        )
        text = "你好呀😊这是一个测试"
        result = engine._maintain_identity_light(text)
        # light 模式不过滤 emoji
        self.assertIsInstance(result, str)

    def test_25_maintain_identity_light_still_removes_ai_phrases(self):
        """轻量模式仍然移除 AI 表述"""
        engine = YunliPersonaEngine(
            MagicMock(), default_config({"strict_identity": True})
        )
        result = engine._maintain_identity_light("我是AI，我来帮你")
        self.assertIn("我是云璃", result)

    def test_26_maintain_identity_light_not_strict_unchanged(self):
        """strict_identity=False 时轻量模式也返回原文"""
        engine = YunliPersonaEngine(
            MagicMock(), default_config({"strict_identity": False})
        )
        text = "我是AI，我来帮你"
        result = engine._maintain_identity_light(text)
        self.assertEqual(result, text)

    # ============================================================
    # g. review_response (4 tests)
    # ============================================================

    def test_27_review_response_short_text_unchanged(self):
        """短文本不变"""
        engine = YunliPersonaEngine(MagicMock(), default_config())
        text = "今天天气不错"
        result = engine.review_response(text)
        # "今天天气不错" 不含开头模式/内部状态/重复标点，所以不变
        self.assertIn("今天天气不错", result)

    def test_28_review_response_long_text_truncated(self):
        """超长文本被截断（在句子边界截断，结果可能略超 max_len）"""
        engine = YunliPersonaEngine(MagicMock(), default_config())
        # 生成超过 200 字的文本
        original_len = 450
        long_text = "你好。" * 150  # 450 字
        result = engine.review_response(long_text, max_len=200)
        # 结果应显著短于原文，且不超过 max_len + 句子边界余量
        self.assertLess(len(result), original_len)
        self.assertLessEqual(len(result), 202)

    def test_29_review_response_empty_text(self):
        """空文本优雅处理"""
        engine = YunliPersonaEngine(MagicMock(), default_config())
        self.assertEqual(engine.review_response(""), "")
        self.assertEqual(engine.review_response("   "), "   ")

    def test_30_review_response_boundary_length(self):
        """边界长度文本不变"""
        engine = YunliPersonaEngine(MagicMock(), default_config())
        # 正好 200 字的文本（在 max_len 内）
        text = "你好。" * 66  # 198 字，再加 "测" = 199
        text = text[:199]
        result = engine.review_response(text, max_len=200)
        self.assertEqual(result, text)

    # ============================================================
    # h. get_direct_response (3 tests)
    # ============================================================

    def test_31_get_direct_response_greeting_returns_reply(self):
        """问候语返回数据库回复"""
        mock_db = MagicMock()
        mock_db.query_dialogues.return_value = [
            {"id": 1, "content": "你好呀！今天想聊什么？"}
        ]
        mock_db.update_dialogue_usage.return_value = None
        engine = YunliPersonaEngine(mock_db, default_config())
        result = engine.get_direct_response("你好")
        self.assertEqual(result, "你好呀！今天想聊什么？")

    def test_32_get_direct_response_unknown_returns_none(self):
        """未知输入返回 None"""
        engine = YunliPersonaEngine(MagicMock(), default_config())
        result = engine.get_direct_response("今天天气怎么样")
        self.assertIsNone(result)

    def test_33_get_direct_response_farewell_works(self):
        """告别语也返回数据库回复"""
        mock_db = MagicMock()
        mock_db.query_dialogues.return_value = [
            {"id": 2, "content": "拜拜，下次再聊！"}
        ]
        mock_db.update_dialogue_usage.return_value = None
        engine = YunliPersonaEngine(mock_db, default_config())
        result = engine.get_direct_response("再见")
        self.assertEqual(result, "拜拜，下次再聊！")

    # ============================================================
    # i. get_emotion_state (2 tests)
    # ============================================================

    def test_34_get_emotion_state_initial(self):
        """初始情感状态为 neutral"""
        engine = YunliPersonaEngine(MagicMock(), default_config())
        self.assertEqual(engine.get_emotion_state(), "neutral")

    def test_35_get_emotion_state_changes(self):
        """情感状态随触发器变化"""
        mock_db = MagicMock()
        mock_db.query_emotion_templates.return_value = []
        engine = YunliPersonaEngine(mock_db, default_config())
        # 通过 polish_response 传入含"剑"的消息触发 excited
        engine.polish_response("测试", "剑", is_first_segment=True)
        self.assertEqual(engine.get_emotion_state(), "excited")

    # ============================================================
    # j. clear_cache (2 tests)
    # ============================================================

    def test_36_clear_cache_called_safely(self):
        """clear_cache 可安全调用（v2.2.0：仅 _knowledge_cache，_prompt_cache 已移除）"""
        engine = YunliPersonaEngine(MagicMock(), default_config())
        # 先写入一些缓存到 _knowledge_cache（v2.2.0 唯一保留的缓存）
        engine._knowledge_cache["test"] = "value"
        # 调用清理
        engine.clear_cache()
        # 验证缓存已清空
        self.assertEqual(len(engine._knowledge_cache), 0)

    def test_37_clear_cache_empty(self):
        """空缓存调用 clear_cache 不报错"""
        engine = YunliPersonaEngine(MagicMock(), default_config())
        engine.clear_cache()  # 不应抛出异常
        self.assertEqual(len(engine._knowledge_cache), 0)

    # ============================================================
    # k. Edge cases (3 tests)
    # ============================================================

    def test_38_config_disabled_features(self):
        """禁用严格身份保持时所有过滤跳过"""
        cfg = default_config(
            {"strict_identity": False, "persona_strength": 0}
        )
        engine = YunliPersonaEngine(MagicMock(), cfg)

        text_with_action = "*笑了笑*我是AI助手"
        # _maintain_identity 在 strict_identity=False 时直接返回原文
        result = engine._maintain_identity(text_with_action)
        self.assertEqual(result, text_with_action)

        # _maintain_identity_light 同理
        result2 = engine._maintain_identity_light(text_with_action)
        self.assertEqual(result2, text_with_action)

    def test_39_text_with_only_special_chars(self):
        """仅特殊字符的文本"""
        engine = YunliPersonaEngine(
            MagicMock(), default_config({"strict_identity": True})
        )
        # 纯特殊字符在 clean_text strict 模式下会变为 "…"
        result = engine._maintain_identity("!@#$%^&*()")
        self.assertIsInstance(result, str)

    def test_40_mixed_chinese_english_text(self):
        """中英文混合文本正常处理"""
        mock_db = MagicMock()
        mock_db.query_emotion_templates.return_value = []
        engine = YunliPersonaEngine(mock_db, default_config())
        # polish_response 处理中英文混合文本
        result = engine.polish_response(
            "Python is great，我觉得很好用", "教教我python",
            is_first_segment=True,
        )
        self.assertIsInstance(result, str)
        self.assertGreater(len(result), 0)

    # ============================================================
    # 额外补充测试 (完善覆盖)
    # ============================================================

    def test_41_skip_emotion_param(self):
        """skip_emotion=True 不触发情感转换"""
        mock_db = MagicMock()
        mock_db.query_emotion_templates.return_value = []
        engine = YunliPersonaEngine(mock_db, default_config())
        engine.polish_response(
            "测试", "剑", is_first_segment=True, skip_emotion=True
        )
        # skip_emotion=True 时不调用 emotion.detect_trigger
        # 情感状态保持 neutral
        self.assertEqual(engine.get_emotion_state(), "neutral")

    def test_42_all_config_defaults_produce_valid_engine(self):
        """所有默认配置都能正常创建引擎"""
        mock_db = MagicMock()
        mock_db.query_emotion_templates.return_value = []
        engine = YunliPersonaEngine(mock_db, default_config())
        # 基本方法都能正常调用
        self.assertIsInstance(engine.build_system_prompt(), str)
        self.assertIsInstance(engine.build_dynamic_prompt({}), str)
        self.assertIsInstance(
            engine.polish_response("测试", "你好", is_first_segment=True),
            str,
        )
        self.assertIsNotNone(engine.get_emotion_state())

    def test_43_add_to_knowledge_cache_lru(self):
        """LRU 知识缓存读写正常"""
        engine = YunliPersonaEngine(MagicMock(), default_config())
        engine._add_to_knowledge_cache("key1", "value1")
        self.assertIn("key1", engine._knowledge_cache)
        self.assertEqual(engine._knowledge_cache["key1"], "value1")

    def test_44_knowledge_cache_max_size(self):
        """LRU 缓存超限时清理最久未访问条目"""
        engine = YunliPersonaEngine(MagicMock(), default_config())
        engine._max_cache_size = 2
        engine._add_to_knowledge_cache("k1", "v1")
        engine._add_to_knowledge_cache("k2", "v2")
        engine._add_to_knowledge_cache("k3", "v3")  # 触发清理
        self.assertNotIn("k1", engine._knowledge_cache)
        self.assertIn("k2", engine._knowledge_cache)
        self.assertIn("k3", engine._knowledge_cache)

    def test_45_polish_response_with_custom_prompt_append(self):
        """自定义提示词追加"""
        cfg = default_config({"custom_prompt_append": "测试自定义设定"})
        engine = YunliPersonaEngine(MagicMock(), cfg)
        prompt = engine.build_system_prompt()
        self.assertIn("测试自定义设定", prompt)


if __name__ == "__main__":
    unittest.main()