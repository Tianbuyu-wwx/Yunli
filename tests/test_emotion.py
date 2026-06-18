"""云璃插件 - emotion.py 单元测试

覆盖 EmotionStateMachine 的核心功能：
- transition() 情感惯性
- auto_decay() 指数衰减
- detect_triggers() 复合情感检测
- detect_trigger() 向后兼容
- get_current_state_description() 实时强度显示
"""

import unittest
import sys
import importlib.util
import types
from pathlib import Path

# ============================================================
# 模块加载基础设施：绕过 persona/__init__.py 的相对导入链
# persona/__init__.py 会触发 from ..core import utils，在测试环境中不可用
# 策略：先创建 persona 包占位，再逐个加载依赖模块
# ============================================================

_plugin_root = Path(__file__).resolve().parent.parent
_persona_dir = _plugin_root / "persona"

# 1. 创建 persona 包占位（不执行 __init__.py）
_persona_pkg = types.ModuleType("persona")
_persona_pkg.__path__ = [str(_persona_dir)]
_persona_pkg.__package__ = "persona"
_persona_pkg.__file__ = str(_persona_dir / "__init__.py")
sys.modules["persona"] = _persona_pkg

# 2. 加载 filters 模块（无相对导入依赖）
_filters_spec = importlib.util.spec_from_file_location(
    "persona.filters", _persona_dir / "filters.py"
)
_filters_mod = importlib.util.module_from_spec(_filters_spec)
sys.modules["persona.filters"] = _filters_mod
_filters_spec.loader.exec_module(_filters_mod)
# 将 filters 挂到 persona 包上
_persona_pkg.filters = _filters_mod

# 3. 加载 language 模块（依赖 from . import filters，已预加载）
_lang_spec = importlib.util.spec_from_file_location(
    "persona.language", _persona_dir / "language.py"
)
_lang_mod = importlib.util.module_from_spec(_lang_spec)
sys.modules["persona.language"] = _lang_mod
_lang_spec.loader.exec_module(_lang_mod)

# 4. 加载 emotion 模块（依赖 from .language import ... 和 from . import filters）
_emotion_spec = importlib.util.spec_from_file_location(
    "persona.emotion", _persona_dir / "emotion.py"
)
_emotion_mod = importlib.util.module_from_spec(_emotion_spec)
sys.modules["persona.emotion"] = _emotion_mod
_emotion_spec.loader.exec_module(_emotion_mod)

EmotionStateMachine = _emotion_mod.EmotionStateMachine


class TestEmotionTransition(unittest.TestCase):
    """测试 transition() 方法：状态转换 + 情感惯性

    v2.2.0 重构：transition 时计算 residual = old_intensity * blend_weight * 0.3
    然后新 intensity = base_intensity + residual（min 1.0）。
    blend_weight = 0.3，初始 intensity = 0.5。
    """

    # v2.2.0 helper: 计算 transition 后的预期 intensity
    def _expected_intensity(self, base, old=0.5, blend=0.3):
        """base + old*blend*0.3, capped at 1.0"""
        return min(1.0, base + old * blend * 0.3)

    def setUp(self):
        self.esm = EmotionStateMachine()

    def test_basic_transition_changes_state(self):
        """基本状态转换：neutral → excited"""
        self.assertEqual(self.esm.current_state, "neutral")
        self.esm.transition("sword_mentioned")
        self.assertEqual(self.esm.current_state, "excited")
        # v2.2.0: 0.8 (base) + 0.5 (initial) * 0.3 (blend) * 0.3 = 0.845
        self.assertAlmostEqual(self.esm.intensity, self._expected_intensity(0.8))

    def test_transition_records_history(self):
        """状态转换时记录历史"""
        self.esm.transition("sword_mentioned")
        self.assertEqual(len(self.esm.state_history), 1)
        self.assertEqual(self.esm.state_history[0]["state"], "neutral")

    def test_emotion_inertia_same_state(self):
        """情感惯性：连续同一触发器增强 intensity"""
        self.esm.transition("sword_mentioned")  # neutral → excited
        # 第一次 transition: 0.8 + 0.5*0.3*0.3 = 0.845
        first = self._expected_intensity(0.8)
        self.assertAlmostEqual(self.esm.intensity, first)

        self.esm.transition("sword_mentioned")  # excited → excited, 惯性+0.1
        # 惯性分支: min(1.0, 0.845 + 0.1) = 0.945
        self.assertAlmostEqual(self.esm.intensity, min(1.0, first + 0.1))
        self.assertEqual(self.esm.state_duration, 1)

        self.esm.transition("sword_mentioned")  # 再来一次，capped at 1.0
        self.assertAlmostEqual(self.esm.intensity, 1.0)
        self.assertEqual(self.esm.state_duration, 2)

    def test_emotion_inertia_capped_at_1(self):
        """情感惯性上限为 1.0"""
        self.esm.transition("sword_mentioned")
        self.esm.transition("sword_mentioned")
        self.esm.transition("sword_mentioned")
        self.esm.transition("sword_mentioned")  # min(1.1, 1.0) = 1.0
        self.assertAlmostEqual(self.esm.intensity, 1.0)

    def test_different_trigger_resets_intensity(self):
        """不同触发器重置 intensity 为新状态（基础 + 残余）"""
        self.esm.transition("sword_mentioned")  # excited
        self.esm.transition("sword_mentioned")  # 惯性
        # 两次 transition 后的实际 intensity（不是直接 0.9）
        # 第一次: 0.8 + 0.5*0.3*0.3 = 0.845
        # 第二次（惯性）: min(1.0, 0.845 + 0.1) = 0.945
        before_third = 0.945
        self.esm.transition("insulted")  # annoyed
        self.assertEqual(self.esm.current_state, "annoyed")
        # v2.2.0: annoyed base=0.6 + residual = 0.6 + 0.945*0.3*0.3 = 0.68505
        expected = self._expected_intensity(0.6, old=before_third)
        self.assertAlmostEqual(self.esm.intensity, expected, places=6)
        self.assertEqual(self.esm.state_duration, 0)

    def test_unknown_trigger_goes_neutral(self):
        """未知触发器转到 neutral"""
        self.esm.transition("sword_mentioned")  # excited
        # v2.2.0: 第一次 transition intensity = 0.845
        before_unknown = 0.845
        self.esm.transition("nonexistent_trigger")
        self.assertEqual(self.esm.current_state, "neutral")
        # neutral base=0.3 + residual from excited
        expected = self._expected_intensity(0.3, old=before_unknown)
        self.assertAlmostEqual(self.esm.intensity, expected, places=6)

    def test_state_duration_increments_on_same_state(self):
        """同一状态下 state_duration 递增"""
        self.esm.transition("praised")  # tsundere
        self.assertEqual(self.esm.state_duration, 0)
        self.esm.transition("praised")  # 惯性
        self.assertEqual(self.esm.state_duration, 1)
        self.esm.transition("praised")
        self.assertEqual(self.esm.state_duration, 2)


class TestEmotionAutoDecay(unittest.TestCase):
    """测试 auto_decay() 方法：指数衰减

    v2.2.0：transition 后 intensity = base + 0.5*0.3*0.3 = base + 0.045
    所以从 neutral 0.5 → excited 是 0.845，不是 0.8
    """

    def setUp(self):
        self.esm = EmotionStateMachine()

    def test_exponential_decay_reduces_intensity(self):
        """指数衰减：每轮 intensity *= 0.75"""
        self.esm.transition("sword_mentioned")  # excited, intensity=0.845
        before = self.esm.intensity  # 0.845
        self.esm.auto_decay()
        # v2.2.0: 0.845 * 0.75 = 0.63375
        self.assertAlmostEqual(self.esm.intensity, before * 0.75)

    def test_exponential_decay_multiple_rounds(self):
        """多轮指数衰减"""
        self.esm.transition("sword_mentioned")  # 0.845
        before = self.esm.intensity
        self.esm.auto_decay()  # 0.63375
        self.esm.auto_decay()  # 0.47531
        # v2.2.0: 实际值 = before * 0.75 * 0.75
        self.assertAlmostEqual(self.esm.intensity, before * 0.75 * 0.75)

    def test_decay_to_neutral_when_below_threshold(self):
        """intensity 低于 0.15 时回到 neutral"""
        # v2.2.0: transition 后 bored base=0.3 + 0.5*0.3*0.3 = 0.345
        self.esm.transition("boring_chat")  # bored
        # 0.345 * 0.75 = 0.25875
        self.esm.auto_decay()
        self.assertEqual(self.esm.current_state, "bored")
        # 0.25875 * 0.75 = 0.1940625
        self.esm.auto_decay()
        self.assertEqual(self.esm.current_state, "bored")
        # 0.1940625 * 0.75 = 0.1455468... → 低于0.15，回到neutral
        self.esm.auto_decay()
        self.assertEqual(self.esm.current_state, "neutral")
        # v2.2.0: neutral base=0.3 + residual
        # decay 后的旧 intensity: 0.145546875 * 0.75 = 0.10916015625 (这是退出 decay 后的 final)
        # 但 v2.2.0 在回到 neutral 时用新的 residual 计算
        # 由于具体计算依赖 neutral threshold trigger 后的实际 intensity
        # 这里只验证"回到 neutral"即可
        self.assertGreater(self.esm.intensity, 0.0)
        self.assertLessEqual(self.esm.intensity, 0.5)

    def test_neutral_state_no_decay(self):
        """neutral 状态下 auto_decay 不改变 intensity（if 条件不满足）"""
        self.esm.intensity = 0.5
        self.esm.current_state = "neutral"
        self.esm.auto_decay()
        # neutral 状态下 if self.current_state != "neutral" 不满足
        # 所以 intensity 不被 *= 0.75，保持不变
        self.assertAlmostEqual(self.esm.intensity, 0.5)

    def test_decay_increments_state_duration(self):
        """衰减时 state_duration 递增"""
        self.esm.transition("sword_mentioned")
        self.esm.auto_decay()
        self.assertEqual(self.esm.state_duration, 1)
        self.esm.auto_decay()
        self.assertEqual(self.esm.state_duration, 2)

    def test_decay_to_neutral_records_history(self):
        """衰减回 neutral 时记录历史"""
        self.esm.transition("boring_chat")  # bored, 0.3
        # 连续衰减直到回到 neutral
        for _ in range(10):
            self.esm.auto_decay()
        self.assertEqual(self.esm.current_state, "neutral")
        # 应该有历史记录（bored → neutral）
        self.assertTrue(any(h["state"] == "bored" for h in self.esm.state_history))

    def test_high_intensity_takes_longer_to_decay(self):
        """高 intensity 需要更多轮才回到 neutral"""
        self.esm.transition("sword_mentioned")  # 0.8
        decay_count = 0
        while self.esm.current_state != "neutral" and decay_count < 20:
            self.esm.auto_decay()
            decay_count += 1
        # 0.8 * 0.75^n < 0.15 → n >= 7
        self.assertGreater(decay_count, 5)

    def test_excited_full_decay_sequence(self):
        """验证 excited 状态的完整衰减序列

        v2.2.0: transition 后 intensity = 0.845（base 0.8 + residual 0.045）
        """
        self.esm.transition("sword_mentioned")  # 0.845
        expected_intensities = [self.esm.intensity]
        while self.esm.current_state != "neutral":
            self.esm.auto_decay()
            if self.esm.current_state != "neutral":
                expected_intensities.append(self.esm.intensity)

        # 验证每一步都是上一步的 0.75
        for i in range(1, len(expected_intensities)):
            self.assertAlmostEqual(
                expected_intensities[i],
                expected_intensities[i - 1] * 0.75,
                places=6,
            )


class TestDetectTriggers(unittest.TestCase):
    """测试 detect_triggers() 复合情感检测"""

    def setUp(self):
        self.esm = EmotionStateMachine()

    def test_single_trigger(self):
        """单触发器检测"""
        result = self.esm.detect_triggers("这把剑真厉害")
        self.assertIn("sword_mentioned", result)
        self.assertIn("praised", result)

    def test_multiple_triggers(self):
        """复合触发器检测：同时包含剑和食物"""
        result = self.esm.detect_triggers("吃饭的时候聊剑真棒")
        self.assertIn("sword_mentioned", result)
        self.assertIn("food_mentioned", result)
        self.assertIn("praised", result)
        self.assertGreaterEqual(len(result), 3)

    def test_no_trigger(self):
        """无触发器"""
        result = self.esm.detect_triggers("今天天气不错")
        self.assertEqual(result, [])

    def test_sad_and_insult(self):
        """悲伤+被骂的复合触发"""
        result = self.esm.detect_triggers("你真笨，我好痛苦")
        self.assertIn("insulted", result)
        self.assertIn("sad_topic", result)

    def test_backward_compatible_detect_trigger(self):
        """detect_trigger() 向后兼容：返回第一个匹配"""
        result = self.esm.detect_trigger("这把剑真厉害")
        self.assertIsNotNone(result)
        # 应该返回某个触发器（具体哪个取决于遍历顺序）
        self.assertIn(result, ["sword_mentioned", "praised"])

    def test_detect_trigger_none(self):
        """detect_trigger() 无匹配返回 None"""
        result = self.esm.detect_trigger("今天天气不错")
        self.assertIsNone(result)

    def test_detect_triggers_returns_list(self):
        """detect_triggers() 返回列表"""
        result = self.esm.detect_triggers("剑")
        self.assertIsInstance(result, list)

    def test_play_and_intimacy(self):
        """玩乐+亲密的复合触发"""
        result = self.esm.detect_triggers("哈哈，贴贴")
        self.assertIn("joke_made", result)
        # "贴贴"在INTIMACY_KEYWORDS中，但detect_triggers不检查INTIMACY_KEYWORDS
        # "哈哈"在PLAY_KEYWORDS中，匹配joke_made
        self.assertGreaterEqual(len(result), 1)


class TestGetCurrentStateDescription(unittest.TestCase):
    """测试 get_current_state_description() 实时强度显示"""

    def setUp(self):
        self.esm = EmotionStateMachine()

    def test_neutral_description(self):
        """neutral 状态描述"""
        desc = self.esm.get_current_state_description()
        self.assertIn("neutral", desc)
        self.assertIn("0.50", desc)  # 初始 intensity=0.5

    def test_excited_description_with_inertia(self):
        """惯性后的强度显示

        v2.2.0: transition 后 intensity = 0.8 + 0.5*0.3*0.3 = 0.845
        惯性后: min(1.0, 0.845 + 0.1) = 0.945
        """
        self.esm.transition("sword_mentioned")  # 0.845
        desc = self.esm.get_current_state_description()
        self.assertIn("excited", desc)
        self.assertIn("0.85", desc)  # 0.845 → "0.85" (rounded to 2 decimals)

        self.esm.transition("sword_mentioned")  # 惯性 0.945
        desc = self.esm.get_current_state_description()
        self.assertIn("0.95", desc)  # 0.945 → "0.95"

    def test_description_after_decay(self):
        """衰减后的强度显示

        v2.2.0: transition 0.845 → decay 0.63375 → 描述 "0.63"
        """
        self.esm.transition("sword_mentioned")  # 0.845
        self.esm.auto_decay()  # 0.63375
        desc = self.esm.get_current_state_description()
        self.assertIn("0.63", desc)


class TestStateHistoryCapacity(unittest.TestCase):
    """测试状态历史容量限制"""

    def test_history_does_not_exceed_max(self):
        """历史记录不超过 MAX_STATE_HISTORY"""
        esm = EmotionStateMachine()
        # 快速切换状态 120 次
        triggers = ["sword_mentioned", "food_mentioned", "praised", "insulted", "boring_chat"]
        for i in range(120):
            esm.transition(triggers[i % len(triggers)])

        self.assertLessEqual(len(esm.state_history), esm.MAX_STATE_HISTORY)


if __name__ == "__main__":
    unittest.main()
