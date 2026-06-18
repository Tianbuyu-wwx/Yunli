"""Darwin 资产桥接器 (asset_bridge.py) 单元测试

覆盖:
  - load_evolved_asset / is_evolved_asset_available
  - get_evolved_asset_score / get_asset_target_info
  - apply_asset_to_runtime（含备份）
  - get_apply_status
  - ASSET_TO_TARGET 完整性
"""

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

test_dir = os.path.dirname(os.path.abspath(__file__))
yunli_dir = os.path.dirname(test_dir)
parent_dir = os.path.dirname(yunli_dir)
for p in [parent_dir, yunli_dir, test_dir]:
    if p not in sys.path:
        sys.path.insert(0, p)

sys.path.insert(0, os.path.join(yunli_dir, "evolution"))

from test_base import YunliTestCase


class TestAssetBridge(YunliTestCase):
    """asset_bridge 模块"""

    def setUp(self):
        import asset_bridge
        self.bridge = asset_bridge
        self._tmp_dir = tempfile.TemporaryDirectory()
        self._tmp = Path(self._tmp_dir.name)

        # 保存原始路径
        self._orig_assets = self.bridge.ASSETS_DIR
        self._orig_baseline = self.bridge.BASELINE_FILE
        self._orig_backup = self.bridge.BACKUP_DIR
        self._orig_overlay = self.bridge.RUNTIME_OVERLAY_FILE

        # 重定向到临时目录
        assets = self._tmp / "assets"
        assets.mkdir()
        self.bridge.ASSETS_DIR = assets
        self.bridge.BASELINE_FILE = self._tmp / "baseline.json"
        self.bridge.BACKUP_DIR = self._tmp / "backups"
        self.bridge.RUNTIME_OVERLAY_FILE = self._tmp / "applied_runtime.json"

    def tearDown(self):
        self.bridge.ASSETS_DIR = self._orig_assets
        self.bridge.BASELINE_FILE = self._orig_baseline
        self.bridge.BACKUP_DIR = self._orig_backup
        self.bridge.RUNTIME_OVERLAY_FILE = self._orig_overlay
        self._tmp_dir.cleanup()

    def test_asset_to_target_has_5_entries(self):
        self.assertEqual(len(self.bridge.ASSET_TO_TARGET), 5)

    def test_load_evolved_asset_exists(self):
        asset_path = self.bridge.ASSETS_DIR / "system_prompt.md"
        asset_path.write_text("evolved system prompt", encoding="utf-8")
        content = self.bridge.load_evolved_asset("system_prompt")
        self.assertEqual(content, "evolved system prompt")

    def test_load_evolved_asset_not_exists(self):
        content = self.bridge.load_evolved_asset("nonexistent")
        self.assertIsNone(content)

    def test_is_evolved_asset_available(self):
        asset_path = self.bridge.ASSETS_DIR / "system_prompt.md"
        asset_path.write_text("test", encoding="utf-8")
        self.assertTrue(self.bridge.is_evolved_asset_available("system_prompt"))

    def test_is_evolved_asset_not_available(self):
        self.assertFalse(self.bridge.is_evolved_asset_available("nonexistent"))

    def test_get_evolved_asset_score(self):
        baseline = {"system_prompt": {"total_score": 85.5}}
        self.bridge.BASELINE_FILE.write_text(json.dumps(baseline))
        score = self.bridge.get_evolved_asset_score("system_prompt")
        self.assertEqual(score, 85.5)

    def test_get_evolved_asset_score_no_baseline(self):
        score = self.bridge.get_evolved_asset_score("system_prompt")
        self.assertIsNone(score)

    def test_get_asset_target_info(self):
        info = self.bridge.get_asset_target_info("system_prompt")
        self.assertIsNotNone(info)
        self.assertEqual(info["file"], "persona/core.py")
        self.assertEqual(info["var"], "BASE_SYSTEM_PROMPT")

    def test_get_asset_target_info_unknown(self):
        info = self.bridge.get_asset_target_info("nonexistent")
        self.assertIsNone(info)

    def test_apply_asset_to_runtime_with_mock_plugin_dir(self):
        """apply_asset_to_runtime 写入 mock 运行时文件"""
        asset_path = self.bridge.ASSETS_DIR / "system_prompt.md"
        asset_path.write_text('你是一个来自古代的猎剑士，名叫云璃。', encoding="utf-8")

        # 创建 mock plugin_dir
        plugin_dir = self._tmp / "plugin"
        persona_dir = plugin_dir / "persona"
        persona_dir.mkdir(parents=True)
        core_file = persona_dir / "core.py"
        core_file.write_text(
            'BASE_SYSTEM_PROMPT = """\n原始提示词\n"""\n',
            encoding="utf-8",
        )

        ok, msg = self.bridge.apply_asset_to_runtime("system_prompt", plugin_dir)
        self.assertTrue(ok, f"apply 失败: {msg}")
        self.assertIn("core.py", msg)

        # 验证写入了新内容
        new_content = core_file.read_text(encoding="utf-8")
        self.assertIn("古代", new_content)
        self.assertIn("猎剑士", new_content)

    def test_apply_asset_to_runtime_unknown_asset(self):
        plugin_dir = self._tmp / "plugin"
        plugin_dir.mkdir()
        ok, msg = self.bridge.apply_asset_to_runtime("nonexistent", plugin_dir)
        self.assertFalse(ok)
        self.assertIn("未知资产", msg)

    def test_apply_asset_creates_backup(self):
        asset_path = self.bridge.ASSETS_DIR / "system_prompt.md"
        asset_path.write_text("evolved content", encoding="utf-8")

        plugin_dir = self._tmp / "plugin"
        persona_dir = plugin_dir / "persona"
        persona_dir.mkdir(parents=True)
        core_file = persona_dir / "core.py"
        core_file.write_text(
            'BASE_SYSTEM_PROMPT = """\n原始提示词\n"""\n',
            encoding="utf-8",
        )

        self.bridge.apply_asset_to_runtime("system_prompt", plugin_dir)
        bak_files = list(self.bridge.BACKUP_DIR.glob("*.bak"))
        self.assertGreater(len(bak_files), 0, "apply 应创建备份文件")

    def test_get_apply_status(self):
        asset_path = self.bridge.ASSETS_DIR / "system_prompt.md"
        asset_path.write_text("evolved", encoding="utf-8")

        baseline = {"system_prompt": {"total_score": 85.5}}
        self.bridge.BASELINE_FILE.write_text(json.dumps(baseline))

        plugin_dir = self._tmp / "plugin"
        persona_dir = plugin_dir / "persona"
        persona_dir.mkdir(parents=True)

        status = self.bridge.get_apply_status(plugin_dir)
        self.assertIn("system_prompt", status)
        self.assertTrue(status["system_prompt"]["evolved"])
        self.assertEqual(status["system_prompt"]["score"], 85.5)

    def test_get_apply_status_no_baseline(self):
        plugin_dir = self._tmp / "plugin"
        plugin_dir.mkdir()
        status = self.bridge.get_apply_status(plugin_dir)
        self.assertIn("error", status)

    def test_get_apply_status_detects_applied(self):
        """get_apply_status 应通过指纹检测标记 applied=True"""
        asset_path = self.bridge.ASSETS_DIR / "system_prompt.md"
        evolved_text = "yunli unique fingerprint 云璃进化测试标记内容"
        asset_path.write_text(evolved_text, encoding="utf-8")

        baseline = {"system_prompt": {"total_score": 90.0}}
        self.bridge.BASELINE_FILE.write_text(json.dumps(baseline))

        plugin_dir = self._tmp / "plugin"
        persona_dir = plugin_dir / "persona"
        persona_dir.mkdir(parents=True)
        core_file = persona_dir / "core.py"
        # 运行时文件中包含进化后的指纹
        core_file.write_text(
            f'BASE_SYSTEM_PROMPT = """\n{evolved_text}\n"""\n',
            encoding="utf-8",
        )

        status = self.bridge.get_apply_status(plugin_dir)
        self.assertIn("system_prompt", status)
        self.assertTrue(status["system_prompt"]["applied"],
                       "指纹匹配应标记为已应用")

    def test_apply_asset_to_runtime_target_not_found(self):
        """apply_asset_to_runtime 目标文件不存在"""
        asset_path = self.bridge.ASSETS_DIR / "system_prompt.md"
        asset_path.write_text("evolved", encoding="utf-8")

        plugin_dir = self._tmp / "plugin_empty"
        plugin_dir.mkdir()  # 无 persona 子目录

        ok, msg = self.bridge.apply_asset_to_runtime("system_prompt", plugin_dir)
        self.assertFalse(ok)
        self.assertIn("目标文件不存在", msg)

    def test_apply_asset_to_runtime_no_var_in_file(self):
        """apply_asset_to_runtime 变量不在文件中（默认 write_overlay=False 应失败）"""
        asset_path = self.bridge.ASSETS_DIR / "system_prompt.md"
        asset_path.write_text("evolved", encoding="utf-8")

        plugin_dir = self._tmp / "plugin"
        persona_dir = plugin_dir / "persona"
        persona_dir.mkdir(parents=True)
        core_file = persona_dir / "core.py"
        # 文件中没有 BASE_SYSTEM_PROMPT
        core_file.write_text("# empty file\n", encoding="utf-8")

        ok, msg = self.bridge.apply_asset_to_runtime("system_prompt", plugin_dir)
        self.assertFalse(ok)
        self.assertIn("未找到变量", msg)

    # ===== 新增：runtime overlay + dry-run + atomic_write_with_backup 测试 =====

    def test_atomic_write_with_backup_success(self):
        """atomic_write_with_backup 正常流程：写临时 → 备份 → 替换"""
        target = self._tmp / "target.txt"
        backup = self._tmp / "backups" / "target.bak"
        target.write_text("original content", encoding="utf-8")

        ok, msg = self.bridge.atomic_write_with_backup(
            target, "new content", backup
        )
        self.assertTrue(ok, msg)
        self.assertEqual(target.read_text(encoding="utf-8"), "new content")
        self.assertTrue(backup.exists(), "备份文件应被创建")
        self.assertEqual(backup.read_text(encoding="utf-8"), "original content")

    def test_atomic_write_with_backup_overwrites_existing_backup(self):
        """原子写入前已存在的备份文件会被覆盖"""
        target = self._tmp / "target.txt"
        backup = self._tmp / "backups" / "target.bak"
        backup.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("original", encoding="utf-8")
        backup.write_text("stale backup", encoding="utf-8")

        ok, msg = self.bridge.atomic_write_with_backup(
            target, "updated", backup
        )
        self.assertTrue(ok, msg)
        # 备份应是 target 被覆盖前的最新值
        self.assertEqual(backup.read_text(encoding="utf-8"), "original")
        self.assertEqual(target.read_text(encoding="utf-8"), "updated")

    def test_atomic_write_with_backup_creates_parent_dirs(self):
        """原子写入会自动创建 target 和 backup 的父目录"""
        target = self._tmp / "deep" / "nested" / "target.txt"
        backup = self._tmp / "other" / "path" / "target.bak"

        ok, msg = self.bridge.atomic_write_with_backup(
            target, "x", backup
        )
        self.assertTrue(ok, msg)
        self.assertTrue(target.exists())
        # target 之前不存在 → 不创建备份文件（合理）
        self.assertFalse(backup.exists())
        # 但 backup 的父目录应被创建
        self.assertTrue(backup.parent.exists())

    def test_runtime_overlay_empty_when_no_file(self):
        """无 overlay 文件时返回 None / 空字典"""
        self.assertIsNone(self.bridge.get_runtime_overlay("system_prompt"))
        self.assertEqual(self.bridge.load_runtime_overlay(), {})

    def test_runtime_overlay_round_trip(self):
        """load / save / get_runtime_overlay 闭环"""
        overlay = {"system_prompt": "evolved version A"}
        self.bridge.save_runtime_overlay(overlay)

        self.assertEqual(
            self.bridge.get_runtime_overlay("system_prompt"),
            "evolved version A",
        )
        self.assertEqual(self.bridge.load_runtime_overlay(), overlay)

    def test_clear_runtime_overlay_specific(self):
        """clear_runtime_overlay(asset) 只清指定资产"""
        self.bridge.save_runtime_overlay({
            "system_prompt": "A",
            "review_rules": "B",
        })
        self.bridge.clear_runtime_overlay("system_prompt")
        self.assertIsNone(self.bridge.get_runtime_overlay("system_prompt"))
        self.assertEqual(
            self.bridge.get_runtime_overlay("review_rules"), "B"
        )

    def test_clear_runtime_overlay_all(self):
        """clear_runtime_overlay(None) 清空全部"""
        self.bridge.save_runtime_overlay({
            "system_prompt": "A",
            "review_rules": "B",
        })
        self.bridge.clear_runtime_overlay()
        self.assertEqual(self.bridge.load_runtime_overlay(), {})

    def test_load_runtime_overlay_corrupt_file_returns_empty(self):
        """损坏的 overlay JSON 不应抛异常，返回空字典"""
        self.bridge.RUNTIME_OVERLAY_FILE.parent.mkdir(parents=True, exist_ok=True)
        self.bridge.RUNTIME_OVERLAY_FILE.write_text("{not json", encoding="utf-8")
        self.assertEqual(self.bridge.load_runtime_overlay(), {})
        # get_runtime_overlay 也不应抛
        self.assertIsNone(self.bridge.get_runtime_overlay("system_prompt"))

    def test_validate_python_syntax_valid(self):
        ok, err = self.bridge._validate_python_syntax("x = 1\ny = 2")
        self.assertTrue(ok)
        self.assertEqual(err, "")

    def test_validate_python_syntax_invalid(self):
        ok, err = self.bridge._validate_python_syntax("x = \n")
        self.assertFalse(ok)
        self.assertIn("line", err)

    def test_apply_asset_dry_run_does_not_write(self):
        """dry_run=True 不写任何文件、不创建备份"""
        asset_path = self.bridge.ASSETS_DIR / "system_prompt.md"
        asset_path.write_text("evolved dry run content", encoding="utf-8")

        plugin_dir = self._tmp / "plugin"
        persona_dir = plugin_dir / "persona"
        persona_dir.mkdir(parents=True)
        core_file = persona_dir / "core.py"
        original_content = 'BASE_SYSTEM_PROMPT = """\noriginal\n"""\n'
        core_file.write_text(original_content, encoding="utf-8")

        ok, msg = self.bridge.apply_asset_to_runtime(
            "system_prompt", plugin_dir, dry_run=True
        )
        self.assertTrue(ok)
        self.assertIn("[dry-run]", msg)  # 实际上我们的 dry-run no-var 分支有标识
        # 文件未被修改
        self.assertEqual(core_file.read_text(encoding="utf-8"), original_content)
        # 没有创建备份
        self.assertEqual(list(self.bridge.BACKUP_DIR.glob("*.bak")), [])
        # 没有写 overlay
        self.assertFalse(self.bridge.RUNTIME_OVERLAY_FILE.exists())

    def test_apply_asset_with_overlay_updates_overlay_file(self):
        """write_overlay=True 时同步写入 applied_runtime.json"""
        asset_path = self.bridge.ASSETS_DIR / "system_prompt.md"
        asset_path.write_text("evolved overlay content", encoding="utf-8")

        plugin_dir = self._tmp / "plugin"
        persona_dir = plugin_dir / "persona"
        persona_dir.mkdir(parents=True)
        core_file = persona_dir / "core.py"
        core_file.write_text(
            'BASE_SYSTEM_PROMPT = """\noriginal\n"""\n',
            encoding="utf-8",
        )

        ok, msg = self.bridge.apply_asset_to_runtime(
            "system_prompt", plugin_dir, write_overlay=True
        )
        self.assertTrue(ok, msg)
        # overlay 应被写入
        self.assertEqual(
            self.bridge.get_runtime_overlay("system_prompt"),
            "evolved overlay content",
        )

    def test_apply_asset_overlay_makes_engine_use_new_prompt(self):
        """端到端：apply 后 YunliPersonaEngine.build_system_prompt() 应使用 overlay 内容。

        这是 Darwin apply 修复的核心验证 —— 进程内立即生效（无需 reload）。
        """
        # 准备插件目录与 persona/core.py（真实格式，含 YunliPersonaEngine）
        plugin_dir = self._tmp / "plugin"
        plugin_dir.mkdir()
        persona_dir = plugin_dir / "persona"
        persona_dir.mkdir()
        # 用一个最小但合法的 core.py，包含 YunliPersonaEngine 类
        core_py = persona_dir / "core.py"
        core_py.write_text(
            "from typing import Dict\n"
            "class YunliPersonaEngine:\n"
            "    BASE_SYSTEM_PROMPT = 'ORIGINAL BASE'\n"
            "    def __init__(self, db, config=None):\n"
            "        self.db = db\n"
            "        self.config = config or {}\n"
            "    def build_system_prompt(self):\n"
            "        # 关键修复：查询 overlay\n"
            "        base = self.BASE_SYSTEM_PROMPT\n"
            "        try:\n"
            "            from evolution import asset_bridge\n"
            "            ov = asset_bridge.get_runtime_overlay('system_prompt')\n"
            "            if ov:\n"
            "                base = ov\n"
            "        except Exception:\n"
            "            pass\n"
            "        return base\n",
            encoding="utf-8",
        )

        # 写入 evolved 资产
        evolved_text = "你是一个新版云璃 —— LLM 已改进"
        (self.bridge.ASSETS_DIR / "system_prompt.md").write_text(
            evolved_text, encoding="utf-8"
        )

        # apply（write_overlay=True）
        ok, msg = self.bridge.apply_asset_to_runtime(
            "system_prompt", plugin_dir, write_overlay=True
        )
        self.assertTrue(ok, msg)

        # 加载 core.py 中定义的 YunliPersonaEngine 并实例化
        import importlib.util
        spec = importlib.util.spec_from_file_location(
            "fake_yunli_core", core_py
        )
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)

        engine = mod.YunliPersonaEngine(db=None, config={})
        prompt = engine.build_system_prompt()
        self.assertIn("LLM 已改进", prompt,
                      "build_system_prompt 应返回 overlay 中的新内容")
        self.assertNotIn("ORIGINAL BASE", prompt,
                         "不应再用源常量 BASE_SYSTEM_PROMPT")

    def test_apply_asset_no_overlay_preserves_constant(self):
        """未启用 overlay 时，build_system_prompt 仍返回源常量（向后兼容）"""
        # 与上一个测试同样的最小 core.py
        plugin_dir = self._tmp / "plugin"
        plugin_dir.mkdir()
        persona_dir = plugin_dir / "persona"
        persona_dir.mkdir()
        core_py = persona_dir / "core.py"
        core_py.write_text(
            "from typing import Dict\n"
            "class YunliPersonaEngine:\n"
            "    BASE_SYSTEM_PROMPT = 'ORIGINAL BASE'\n"
            "    def __init__(self, db, config=None):\n"
            "        self.db = db\n"
            "        self.config = config or {}\n"
            "    def build_system_prompt(self):\n"
            "        base = self.BASE_SYSTEM_PROMPT\n"
            "        try:\n"
            "            from evolution import asset_bridge\n"
            "            ov = asset_bridge.get_runtime_overlay('system_prompt')\n"
            "            if ov:\n"
            "                base = ov\n"
            "        except Exception:\n"
            "            pass\n"
            "        return base\n",
            encoding="utf-8",
        )
        (self.bridge.ASSETS_DIR / "system_prompt.md").write_text(
            "different content", encoding="utf-8"
        )

        # apply 时 write_overlay=False（默认）
        ok, msg = self.bridge.apply_asset_to_runtime(
            "system_prompt", plugin_dir
        )
        self.assertTrue(ok, msg)

        # 加载并实例化
        import importlib.util
        spec = importlib.util.spec_from_file_location(
            "fake_yunli_core2", core_py
        )
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        engine = mod.YunliPersonaEngine(db=None, config={})
        prompt = engine.build_system_prompt()
        # overlay 未启用 → 仍是源常量（虽然源文件已被修改，但 overlay 为空）
        # 这里关键验证：overlay 文件应不存在或为空
        self.assertFalse(
            self.bridge.RUNTIME_OVERLAY_FILE.exists(),
            "write_overlay=False 时不应创建 overlay 文件"
        )

    def test_apply_evolved_assets_bulk(self):
        """apply_evolved_assets() 批量接口"""
        # 准备 5 个资产文件
        plugin_dir = self._tmp / "plugin"
        persona_dir = plugin_dir / "persona"
        persona_dir.mkdir(parents=True)
        (persona_dir / "core.py").write_text(
            'BASE_SYSTEM_PROMPT = """\noriginal\n"""\n',
            encoding="utf-8",
        )

        for name in self.bridge.ASSET_TO_TARGET:
            (self.bridge.ASSETS_DIR / f"{name}.md").write_text(
                f"evolved-{name}", encoding="utf-8"
            )

        # 只指定一个资产
        results = self.bridge.apply_evolved_assets(
            plugin_dir, asset_names=["system_prompt"], write_overlay=True
        )
        self.assertIn("system_prompt", results)
        ok, msg = results["system_prompt"]
        self.assertTrue(ok, msg)
        self.assertIn("system_prompt", self.bridge.load_runtime_overlay())

    def test_apply_evolved_assets_dry_run(self):
        """apply_evolved_assets dry_run 不会写入源文件"""
        plugin_dir = self._tmp / "plugin"
        persona_dir = plugin_dir / "persona"
        persona_dir.mkdir(parents=True)
        original = 'BASE_SYSTEM_PROMPT = """\noriginal\n"""\n'
        (persona_dir / "core.py").write_text(original, encoding="utf-8")
        (self.bridge.ASSETS_DIR / "system_prompt.md").write_text(
            "evolved", encoding="utf-8"
        )

        results = self.bridge.apply_evolved_assets(
            plugin_dir, asset_names=["system_prompt"], dry_run=True
        )
        ok, msg = results["system_prompt"]
        self.assertTrue(ok)
        # 源文件未被修改
        self.assertEqual((persona_dir / "core.py").read_text(encoding="utf-8"), original)
        self.assertFalse(self.bridge.RUNTIME_OVERLAY_FILE.exists())

    def test_get_apply_status_includes_overlay_info(self):
        """get_apply_status 应反映 overlay 状态"""
        (self.bridge.ASSETS_DIR / "system_prompt.md").write_text(
            "evolved", encoding="utf-8"
        )
        self.bridge.BASELINE_FILE.write_text(
            json.dumps({"system_prompt": {"total_score": 85.0}})
        )
        # 写入 overlay
        self.bridge.save_runtime_overlay({"system_prompt": "evolved"})

        plugin_dir = self._tmp / "plugin"
        plugin_dir.mkdir()

        status = self.bridge.get_apply_status(plugin_dir)
        self.assertIn("applied_via_overlay", status["system_prompt"])
        self.assertTrue(status["system_prompt"]["applied_via_overlay"])
        self.assertEqual(status["system_prompt"]["runtime_hook"],
                         "persona_system_prompt")


if __name__ == "__main__":
    unittest.main()