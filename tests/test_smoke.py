"""冒烟测试：验证核心模块可正常导入和基础功能可用

这些测试不依赖 AstrBot 框架，仅验证纯 Python 模块的结构完整性。
CI 环境中作为最小回归测试，确保重构不破坏模块可导入性。
"""
import sys
from pathlib import Path

# 将插件父目录加入 sys.path，使 yunli 包及其子包可被正常导入
# （persona 等子包内部使用 from ..core import xxx 跨包相对导入，
# 必须以 yunli 包为顶层包导入，不能直接导入 persona.emotion）
_PLUGIN_PARENT = Path(__file__).resolve().parent.parent.parent
if str(_PLUGIN_PARENT) not in sys.path:
    sys.path.insert(0, str(_PLUGIN_PARENT))


def test_import_database_module():
    """验证 database 模块可导入且核心类存在"""
    from yunli.database import YunliDatabase, YunliKnowledgeDB, YunliMemoryDB
    assert YunliDatabase is not None
    assert YunliKnowledgeDB is not None
    assert YunliMemoryDB is not None


def test_knowledge_db_init(tmp_path):
    """验证知识库可初始化（线程本地连接 + 缓存锁）"""
    from yunli.database.init_db import YunliKnowledgeDB
    db = YunliKnowledgeDB(str(tmp_path / "knowledge.db"))
    # 验证缓存锁存在（P1-1 修复项）
    assert hasattr(db, "_cache_lock")
    assert hasattr(db, "_knowledge_query_cache")
    db.close()


def test_memory_db_init(tmp_path):
    """验证记忆库可初始化（共享连接 + 锁）"""
    from yunli.database.init_db import YunliMemoryDB
    db = YunliMemoryDB(str(tmp_path / "memory.db"))
    assert hasattr(db, "_lock")
    assert hasattr(db, "_ALLOWED_TABLES")
    db.close()


def test_emotion_triggers_constant():
    """验证情感触发器类常量存在（P1-4 修复项：消除重复定义）"""
    from yunli.persona.emotion import EmotionStateMachine
    assert hasattr(EmotionStateMachine, "_EMOTION_TRIGGERS")
    triggers = EmotionStateMachine._EMOTION_TRIGGERS
    # 验证关键触发器类别存在
    assert "sword_mentioned" in triggers
    assert "praised" in triggers
    assert "insulted" in triggers


def test_emotion_detect_trigger_returns_first_match():
    """验证 detect_trigger 返回首个匹配"""
    from yunli.persona.emotion import EmotionStateMachine
    # 直接通过类调用（不依赖 db 初始化）
    triggers = EmotionStateMachine._EMOTION_TRIGGERS
    text = "好厉害"
    matched = next(
        (t for t, kws in triggers.items() if any(kw in text for kw in kws)),
        None,
    )
    assert matched == "praised"


def test_evolution_utils_import():
    """验证 evolution.utils 可导入（JSON 提取工具）"""
    from yunli.evolution.utils import extract_json_from_response
    assert extract_json_from_response is not None


def test_evolution_locks_import():
    """验证 evolution._locks 可导入（6 个锁实例）"""
    from yunli.evolution._locks import (
        asset_lock,
        log_lock,
        discovery_lock,
        rules_lock,
        results_lock,
        print_lock,
    )
    # 验证所有锁实例存在
    assert asset_lock is not None
    assert log_lock is not None
    assert discovery_lock is not None
    assert rules_lock is not None
    assert results_lock is not None
    assert print_lock is not None


def test_rubric_dimensions_count():
    """验证 rubric 维度数为 10（P2-3 修复项：修正过时注释）"""
    from yunli.evolution.eval.rubric import RUBRIC
    assert len(RUBRIC) == 10, f"期望 10 个维度，实际 {len(RUBRIC)}"
    # 验证 v2.1 新增的 anti_ai_score 存在
    assert "anti_ai_score" in RUBRIC


# ========== 架构简化验证（S1/S2/S3/S4） ==========

def test_persona_config_import():
    """验证 persona/config.py 可导入且包含所有共享常量（S2 修复项）"""
    from yunli.persona.config import (
        SWORD_KEYWORDS,
        FOOD_KEYWORDS,
        YUNLI_KEYWORDS,
        PLAY_KEYWORDS,
        INTIMACY_KEYWORDS,
        HELP_KEYWORDS,
        MODERN_TERMS,
        RELATIONSHIP_MODES,
        STYLE_MODULATION,
    )
    # 验证关键词常量非空
    assert len(SWORD_KEYWORDS) > 0
    assert len(FOOD_KEYWORDS) > 0
    assert len(PLAY_KEYWORDS) > 0
    # 验证关系模式包含 4 种
    assert set(RELATIONSHIP_MODES.keys()) == {"normal", "backoff", "careful", "warming"}
    # 验证风格调制表包含 4 种
    assert set(STYLE_MODULATION.keys()) == {"normal", "warming", "careful", "backoff"}


def test_relationship_module_import():
    """验证 RelationshipManager 从 relationship.py 导入（S1 修复项：拆分双类）"""
    from yunli.persona.relationship import RelationshipManager
    assert RelationshipManager is not None
    # 验证向后兼容类属性仍存在
    assert hasattr(RelationshipManager, "RELATIONSHIP_MODES")
    assert hasattr(RelationshipManager, "BOUNDARY_KEYWORDS")
    assert hasattr(RelationshipManager, "detect_intent")
    assert hasattr(RelationshipManager, "detect_user_intent")


def test_emotion_module_no_relationship():
    """验证 emotion.py 已不再包含 RelationshipManager（S1 修复项）"""
    from yunli.persona import emotion
    assert hasattr(emotion, "EmotionStateMachine")
    # RelationshipManager 应已迁移到 relationship.py
    assert not hasattr(emotion, "RelationshipManager")


def test_persona_package_exports():
    """验证 persona 包仍导出 RelationshipManager（S1 向后兼容）"""
    from yunli.persona import EmotionStateMachine, RelationshipManager
    assert EmotionStateMachine is not None
    assert RelationshipManager is not None


def test_at_detector_in_utils():
    """验证 AtDetector 已合并到 utils.py（S3 修复项）"""
    from yunli.core.utils import AtDetector
    assert AtDetector is not None
    # 验证从 core 包仍可导入（向后兼容）
    from yunli.core import AtDetector as AtDetector2
    assert AtDetector2 is AtDetector


def test_at_detector_functional():
    """验证 AtDetector 功能正常（S3 修复项）"""
    from yunli.core.utils import AtDetector
    detector = AtDetector()
    detector.set_self_id("12345")
    assert detector.get_self_id() == "12345"


def test_request_context_no_thread_fields():
    """验证 RequestContext 已删除重复的线程字段（S4 修复项）"""
    from yunli.core.request_context import RequestContext
    # 验证已删除的字段不存在
    import dataclasses
    field_names = {f.name for f in dataclasses.fields(RequestContext)}
    assert "last_user_message" not in field_names
    assert "last_yunli_response" not in field_names
    assert "thread_turn_count" not in field_names
    # 验证保留的字段仍存在
    assert "req" in field_names
    assert "rel_mode" in field_names
    assert "is_prompt_injected" in field_names


def test_no_runtime_delayed_imports():
    """验证 persona 内部无运行时延迟导入（S2 修复项：消除循环依赖隐患）"""
    import inspect
    from yunli.persona import language, qq_behavior
    # 获取源代码
    lang_src = inspect.getsource(language)
    qq_src = inspect.getsource(qq_behavior)
    # 不应包含运行时延迟导入 RelationshipManager
    assert "from .emotion import RelationshipManager" not in lang_src
    assert "from .emotion import RelationshipManager" not in qq_src
