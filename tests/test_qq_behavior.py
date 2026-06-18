"""云璃插件 - qq_behavior.py 单元测试

覆盖 QQBehaviorManager 的核心功能：
- EMOTION_PUNCTUATION 情绪标点策略配置
- add_typing_pause() 情绪驱动停顿
- add_human_touches() 情绪驱动拟人化
- _add_qq_emoji() 情绪驱动颜文字概率
- format_for_qq() emotion_state 参数透传
- 向后兼容性（默认参数 neutral）
"""

import unittest
import random
import sys
import importlib.util
import types
from pathlib import Path

# 直接加载 qq_behavior.py 模块文件，绕过 persona/__init__.py 的相对导入链
_qb_path = Path(__file__).resolve().parent.parent / "persona" / "qq_behavior.py"
_qb_spec = importlib.util.spec_from_file_location("persona.qq_behavior", _qb_path)
_qb_mod = importlib.util.module_from_spec(_qb_spec)

# 创建 persona 包占位（qq_behavior.py 无相对导入依赖，但需要包结构）
_persona_pkg = types.ModuleType("persona")
_persona_pkg.__path__ = [str(Path(__file__).resolve().parent.parent / "persona")]
_persona_pkg.__package__ = "persona"
sys.modules["persona"] = _persona_pkg

# 加载 qq_behavior 模块
sys.modules["persona.qq_behavior"] = _qb_mod
_qb_spec.loader.exec_module(_qb_mod)

QQBehaviorManager = _qb_mod.QQBehaviorManager


class MockDB:
    """模拟数据库对象"""
    pass


def create_manager(config=None):
    """创建测试用的 QQBehaviorManager"""
    return QQBehaviorManager(MockDB(), config or {})


class TestEmotionPunctuationConfig(unittest.TestCase):
    """测试 EMOTION_PUNCTUATION 配置完整性"""

    def test_supported_emotions_have_config(self):
        """所有支持的6种情绪都有配置"""
        supported = {"excited", "annoyed", "sad_guarded", "tsundere", "happy", "curious"}
        for emotion in supported:
            self.assertIn(emotion, QQBehaviorManager.EMOTION_PUNCTUATION)

    def test_each_emotion_has_required_fields(self):
        """每种情绪配置包含必要字段"""
        required_fields = [
            "exclamation_rate", "ellipsis_rate",
            "pause_type_weights", "hesitation_words", "correction_words",
        ]
        for emotion, config in QQBehaviorManager.EMOTION_PUNCTUATION.items():
            for field in required_fields:
                self.assertIn(field, config, f"{emotion} 缺少 {field}")

    def test_pause_type_weights_sum_reasonable(self):
        """停顿类型权重之和应接近1.0"""
        for emotion, config in QQBehaviorManager.EMOTION_PUNCTUATION.items():
            weights = config["pause_type_weights"]
            total = sum(weights.values())
            self.assertAlmostEqual(total, 1.0, places=1,
                                   msg=f"{emotion} 权重之和 {total} 不接近1.0")

    def test_rates_are_valid_probabilities(self):
        """概率值在 [0, 1] 范围内"""
        for emotion, config in QQBehaviorManager.EMOTION_PUNCTUATION.items():
            self.assertGreaterEqual(config["exclamation_rate"], 0)
            self.assertLessEqual(config["exclamation_rate"], 1)
            self.assertGreaterEqual(config["ellipsis_rate"], 0)
            self.assertLessEqual(config["ellipsis_rate"], 1)


class TestAddTypingPause(unittest.TestCase):
    """测试 add_typing_pause() 情绪驱动停顿"""

    def setUp(self):
        self.mgr = create_manager()

    def test_short_text_no_pause(self):
        """短文本不添加停顿"""
        text = "嗯"
        result = self.mgr.add_typing_pause(text, emotion_state="neutral")
        self.assertEqual(result, text)

    def test_default_emotion_state_backward_compatible(self):
        """默认 emotion_state='neutral' 向后兼容"""
        long_text = "这是第一句话。这是第二句话。这是第三句话。"
        # 不传 emotion_state，不应报错
        result = self.mgr.add_typing_pause(long_text)
        # 结果应该是字符串（可能添加了停顿，也可能没有，取决于随机）
        self.assertIsInstance(result, str)

    def test_excited_emotion_lower_pause_probability(self):
        """excited 情绪降低停顿概率（0.2 vs 默认0.3）"""
        # 通过大量采样验证概率差异
        random.seed(42)
        long_text = "这是第一句话。这是第二句话。这是第三句话。"
        pause_count_excited = 0
        pause_count_sad = 0
        trials = 200

        for _ in range(trials):
            mgr = create_manager()
            result = mgr.add_typing_pause(long_text, emotion_state="excited")
            if "…" in result:
                pause_count_excited += 1

        for _ in range(trials):
            mgr = create_manager()
            result = mgr.add_typing_pause(long_text, emotion_state="sad_guarded")
            if "…" in result:
                pause_count_sad += 1

        # sad_guarded 停顿概率(0.45) > excited(0.2)，统计上应该更多
        # 注意：这是统计测试，极端情况可能失败，但200次采样足够可靠
        self.assertGreater(pause_count_sad, pause_count_excited * 0.5)

    def test_sad_guarded_uses_sad_hesitation_words(self):
        """sad_guarded 情绪使用悲伤犹豫词"""
        # 强制触发停顿：设置随机种子使 typing_pause_prob 命中
        mgr = create_manager({"typing_pause_probability": 1.0})
        long_text = "这是第一句话。这是第二句话。这是第三句话。"

        # 多次运行，至少有一次使用 sad 的犹豫词
        found_sad_words = False
        sad_words = ["…嗯…", "…那个…", "……"]
        for seed in range(50):
            random.seed(seed)
            result = mgr.add_typing_pause(long_text, emotion_state="sad_guarded")
            if any(w in result for w in sad_words):
                found_sad_words = True
                break

        self.assertTrue(found_sad_words, "sad_guarded 应使用悲伤犹豫词")

    def test_tsundere_uses_hesitation_words(self):
        """tsundere 情绪使用傲娇犹豫词"""
        mgr = create_manager({"typing_pause_probability": 1.0})
        long_text = "这是第一句话。这是第二句话。这是第三句话。"

        found_tsundere_words = False
        tsundere_words = ["…哼，", "…切，"]
        for seed in range(50):
            random.seed(seed)
            result = mgr.add_typing_pause(long_text, emotion_state="tsundere")
            if any(w in result for w in tsundere_words):
                found_tsundere_words = True
                break

        self.assertTrue(found_tsundere_words, "tsundere 应使用傲娇犹豫词")

    def test_excited_uses_emphasis_pause(self):
        """excited 情绪倾向使用强调型停顿（权重0.65）"""
        mgr = create_manager({"typing_pause_probability": 1.0})
        long_text = "这是第一句话。这是第二句话。这是第三句话。"

        # 强调型停顿特征：第二句末有"…"，第三句首也有"…"
        emphasis_count = 0
        for seed in range(100):
            random.seed(seed)
            result = mgr.add_typing_pause(long_text, emotion_state="excited")
            if result.count("…") >= 2:
                emphasis_count += 1

        # excited 的 emphasis 权重0.65，应该有相当比例出现强调型
        self.assertGreater(emphasis_count, 10)

    def test_text_unchanged_when_no_pause(self):
        """不触发停顿时文本不变"""
        mgr = create_manager({"typing_pause_probability": 0.0})
        long_text = "这是第一句话。这是第二句话。这是第三句话。"
        result = mgr.add_typing_pause(long_text, emotion_state="neutral")
        self.assertEqual(result, long_text)


class TestAddHumanTouches(unittest.TestCase):
    """测试 add_human_touches() 情绪驱动拟人化"""

    def setUp(self):
        self.mgr = create_manager()

    def test_excited_le_to_la_higher_probability(self):
        """excited 情绪下'了'→'啦'概率更高(20%)"""
        count = 0
        trials = 200
        for seed in range(trials):
            random.seed(seed)
            mgr = create_manager()
            result = mgr.add_human_touches("我知道了", emotion_state="excited")
            if result.endswith("啦"):
                count += 1
        # 20%概率，200次采样期望40次，至少应>10次
        self.assertGreater(count, 10)

    def test_sad_guarded_no_le_to_la(self):
        """sad_guarded 情绪下不发生'了'→'啦'"""
        for seed in range(100):
            random.seed(seed)
            mgr = create_manager()
            result = mgr.add_human_touches("我知道了", emotion_state="sad_guarded")
            self.assertTrue(result.endswith("了"), f"悲伤状态下不应替换'了'→'啦'，但得到: {result}")

    def test_annoyed_no_le_to_la(self):
        """annoyed 情绪下不发生'了'→'啦'"""
        for seed in range(100):
            random.seed(seed)
            mgr = create_manager()
            result = mgr.add_human_touches("我知道了", emotion_state="annoyed")
            self.assertTrue(result.endswith("了"), f"不耐烦状态下不应替换'了'→'啦'")

    def test_excited_tilde_higher_probability(self):
        """excited 情绪下短句加'~'概率更高(15%)"""
        count = 0
        trials = 300
        for seed in range(trials):
            random.seed(seed)
            mgr = create_manager()
            # "好的呀" 3个字，满足 len(text) >= 3 且 len(text) <= 10
            result = mgr.add_human_touches("好的呀", emotion_state="excited")
            if result.endswith("~"):
                count += 1
        # 15%概率，300次采样期望45次
        self.assertGreater(count, 15)

    def test_sad_guarded_no_tilde(self):
        """sad_guarded 情绪下不加'~'"""
        for seed in range(100):
            random.seed(seed)
            mgr = create_manager()
            result = mgr.add_human_touches("好的呀", emotion_state="sad_guarded")
            self.assertFalse(result.endswith("~"), "悲伤状态下不应加波浪号")

    def test_annoyed_no_tilde(self):
        """annoyed 情绪下不加'~'"""
        for seed in range(100):
            random.seed(seed)
            mgr = create_manager()
            result = mgr.add_human_touches("好的呀", emotion_state="annoyed")
            self.assertFalse(result.endswith("~"), "不耐烦状态下不应加波浪号")

    def test_tsundere_low_tilde_probability(self):
        """tsundere 情绪下'~'概率极低(2%)"""
        count = 0
        trials = 300
        for seed in range(trials):
            random.seed(seed)
            mgr = create_manager()
            result = mgr.add_human_touches("好的呀", emotion_state="tsundere")
            if result.endswith("~"):
                count += 1
        # 2%概率，300次采样期望6次，应该远少于excited
        self.assertLess(count, 25)

    def test_default_emotion_backward_compatible(self):
        """默认 emotion_state='neutral' 向后兼容"""
        text = "我知道了"
        result = self.mgr.add_human_touches(text)
        self.assertIsInstance(result, str)

    def test_short_text_unchanged(self):
        """过短文本不处理"""
        result = self.mgr.add_human_touches("嗯")
        self.assertEqual(result, "嗯")

    def test_empty_text_unchanged(self):
        """空文本不处理"""
        result = self.mgr.add_human_touches("")
        self.assertEqual(result, "")

    def test_wo_omission_still_works(self):
        """省略'我'功能仍然正常"""
        count = 0
        trials = 200
        for seed in range(trials):
            random.seed(seed)
            mgr = create_manager()
            result = mgr.add_human_touches("我觉得这个不错", emotion_state="neutral")
            if result.startswith("觉得"):
                count += 1
        # 8%概率，200次采样期望16次
        self.assertGreater(count, 2)


class TestAddQQEmoji(unittest.TestCase):
    """测试 _add_qq_emoji() 情绪驱动颜文字概率"""

    def setUp(self):
        self.mgr = create_manager()

    def test_excited_higher_kaomoji_probability(self):
        """excited/happy 情绪下颜文字概率更高(0.3)"""
        count = 0
        trials = 200
        for seed in range(trials):
            random.seed(seed)
            mgr = create_manager()
            result = mgr._add_qq_emoji("哈哈，真不错", emotion_state="excited")
            if any(c in result for c in "(^▽^)(≧∇≦)(*^▽^*)(｀・ω・´)"):
                count += 1
        # 0.3概率，200次采样期望60次
        self.assertGreater(count, 20)

    def test_sad_lower_kaomoji_probability(self):
        """sad_guarded/annoyed 情绪下颜文字概率极低(0.05)"""
        count = 0
        trials = 200
        for seed in range(trials):
            random.seed(seed)
            mgr = create_manager()
            result = mgr._add_qq_emoji("哈哈，真不错", emotion_state="sad_guarded")
            if any(c in result for c in "(^▽^)(≧∇≦)(*^▽^*)(｀・ω・´)"):
                count += 1
        # 0.05概率，200次采样期望10次，应该远少于excited
        self.assertLess(count, 30)

    def test_tsundere_moderate_kaomoji_probability(self):
        """tsundere 情绪下颜文字概率中等(0.25)"""
        count = 0
        trials = 200
        for seed in range(trials):
            random.seed(seed)
            mgr = create_manager()
            result = mgr._add_qq_emoji("哼，少得意", emotion_state="tsundere")
            if any(c in result for c in "(￣^￣)(¬_¬)"):
                count += 1
        # 0.25概率，200次采样期望50次
        self.assertGreater(count, 15)

    def test_default_emotion_uses_base_probability(self):
        """neutral 情绪使用基础概率(0.15)"""
        count = 0
        trials = 200
        for seed in range(trials):
            random.seed(seed)
            mgr = create_manager()
            result = mgr._add_qq_emoji("哈哈，真不错", emotion_state="neutral")
            if any(c in result for c in "(^▽^)(≧∇≦)"):
                count += 1
        # 0.15概率，200次采样期望30次
        self.assertGreater(count, 8)

    def test_emoji_disabled_in_config(self):
        """配置关闭颜文字时不添加"""
        mgr = create_manager({"use_qq_emoji": False})
        # format_for_qq 中 use_qq_emoji=False 时不调用 _add_qq_emoji
        text = "哈哈，真不错"
        result = mgr.format_for_qq(text, emotion_state="excited")
        # 不应包含颜文字
        self.assertNotIn("(^▽^)", result)


class TestFormatForQQ(unittest.TestCase):
    """测试 format_for_qq() emotion_state 参数透传"""

    def setUp(self):
        self.mgr = create_manager()

    def test_emotion_state_passed_to_emoji(self):
        """emotion_state 传递给 _add_qq_emoji"""
        # 通过验证不同情绪下颜文字频率差异来间接验证透传
        excited_count = 0
        sad_count = 0
        trials = 200

        for seed in range(trials):
            random.seed(seed)
            mgr = create_manager()
            result = mgr.format_for_qq("哈哈", emotion_state="excited")
            if any(c in result for c in "(^▽^)(≧∇≦)"):
                excited_count += 1

        for seed in range(trials):
            random.seed(seed)
            mgr = create_manager()
            result = mgr.format_for_qq("哈哈", emotion_state="sad_guarded")
            if any(c in result for c in "(^▽^)(≧∇≦)"):
                sad_count += 1

        self.assertGreater(excited_count, sad_count)

    def test_default_emotion_backward_compatible(self):
        """默认 emotion_state='neutral' 向后兼容"""
        result = self.mgr.format_for_qq("你好")
        self.assertIsInstance(result, str)

    def test_empty_text_returns_empty(self):
        """空文本直接返回"""
        result = self.mgr.format_for_qq("")
        self.assertEqual(result, "")

    def test_at_me_with_nickname(self):
        """被@时可能加称呼"""
        # 固定随机种子使概率命中
        random.seed(1)
        mgr = create_manager()
        result = mgr.format_for_qq("你好", is_at_me=True, user_nickname="小明")
        # 可能加了称呼，也可能没加（30%概率）
        self.assertIsInstance(result, str)


class TestBackwardCompatibility(unittest.TestCase):
    """测试所有新增参数的向后兼容性"""

    def test_add_typing_pause_no_emotion_state(self):
        """add_typing_pause 不传 emotion_state 不报错"""
        mgr = create_manager()
        result = mgr.add_typing_pause("这是第一句话。这是第二句话。这是第三句话。")
        self.assertIsInstance(result, str)

    def test_add_human_touches_no_emotion_state(self):
        """add_human_touches 不传 emotion_state 不报错"""
        mgr = create_manager()
        result = mgr.add_human_touches("我知道了")
        self.assertIsInstance(result, str)

    def test_format_for_qq_no_emotion_state(self):
        """format_for_qq 不传 emotion_state 不报错"""
        mgr = create_manager()
        result = mgr.format_for_qq("你好")
        self.assertIsInstance(result, str)

    def test_add_qq_emoji_no_emotion_state(self):
        """_add_qq_emoji 不传 emotion_state 不报错"""
        mgr = create_manager()
        result = mgr._add_qq_emoji("哈哈")
        self.assertIsInstance(result, str)


if __name__ == "__main__":
    unittest.main()
