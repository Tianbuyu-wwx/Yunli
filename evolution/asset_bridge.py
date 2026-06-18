"""进化资产桥接器

让 Darwin 进化后的文本资产回流到运行时代码中。
当前支持 5 类资产 → 运行时模块的映射：
  system_prompt    → persona/core.py 的 BASE_SYSTEM_PROMPT
  review_rules     → persona/core.py 的 REVIEW_RULES
  filter_rules     → persona/filters.py 的过滤规则
  emotion_templates → persona/emotion.py 的情感触发器
  language_style   → persona/qq_behavior.py 的语言风格

设计要点（v2）：
1. **双重写入**：既写入源文件（持久化、git 可追踪），又写入运行时 overlay
   （applied_runtime.json）让进程内立即生效，无需 reload 模块。
2. **运行时 overlay**：`get_runtime_overlay(asset_name)` 由 YunliPersonaEngine
   在构造/调用 build_system_prompt() 时主动查询，可选 opt-in（默认开启，
   但仅在 overlay 中有数据时才覆盖源常量）。
3. **线程安全**：通过 evolution._locks.asset_lock 串行化所有写操作。
4. **dry-run**：dry_run=True 时只生成 diff，不写任何文件。
5. **atomic_write_with_backup**：统一原子的「写临时文件 + 备份 + 替换」流程，
   失败时自动从备份恢复。
"""

import json
import os
import re
import shutil
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Tuple

try:
    from . import code_sandbox
except ImportError:
    import code_sandbox

EVOLUTION_DIR = Path(__file__).resolve().parent
ASSETS_DIR = EVOLUTION_DIR / "assets"
BASELINE_FILE = EVOLUTION_DIR / "baseline.json"
BACKUP_DIR = EVOLUTION_DIR / "applied_backups"
# 运行时 overlay：进程内可即时查询，无需 reload 模块
RUNTIME_OVERLAY_FILE = EVOLUTION_DIR / "applied_runtime.json"

# 资产名 → 运行时文件路径 + 变量名 + 运行时 overlay hook 名称
ASSET_TO_TARGET = {
    "system_prompt": {
        "file": "persona/core.py",
        "var": "BASE_SYSTEM_PROMPT",
        "description": "核心人格提示词",
        "runtime_hook": "persona_system_prompt",
    },
    "review_rules": {
        "file": "persona/core.py",
        "var": "REVIEW_RULES",
        "description": "回复自审规则",
        "runtime_hook": "persona_review_rules",
    },
    "filter_rules": {
        "file": "persona/filters.py",
        "var": "FILTER_RULES",
        "description": "输出过滤规则",
        "runtime_hook": "persona_filter_rules",
    },
    "emotion_templates": {
        "file": "persona/emotion.py",
        "var": "EMOTION_TRIGGERS",
        "description": "情感触发器",
        "runtime_hook": "persona_emotion_templates",
    },
    "language_style": {
        "file": "persona/qq_behavior.py",
        "var": "LANGUAGE_STYLE",
        "description": "语言风格",
        "runtime_hook": "persona_language_style",
    },
}


# ---------------------------------------------------------------------------
# 资产读取 / 状态查询
# ---------------------------------------------------------------------------

def load_evolved_asset(asset_name: str) -> Optional[str]:
    """加载进化后的资产文本"""
    asset_path = ASSETS_DIR / f"{asset_name}.md"
    if not asset_path.exists():
        return None
    return asset_path.read_text(encoding="utf-8")


def is_evolved_asset_available(asset_name: str) -> bool:
    """检查进化后的资产是否存在且比基线新"""
    asset_path = ASSETS_DIR / f"{asset_name}.md"
    if not asset_path.exists():
        return False
    if not BASELINE_FILE.exists():
        return True  # 无基线，有资产就算可用
    baseline = json.loads(BASELINE_FILE.read_text(encoding="utf-8"))
    return asset_name in baseline


def get_evolved_asset_score(asset_name: str) -> Optional[float]:
    """获取进化后资产的评分"""
    if not BASELINE_FILE.exists():
        return None
    baseline = json.loads(BASELINE_FILE.read_text(encoding="utf-8"))
    if asset_name not in baseline:
        return None
    return baseline[asset_name].get("total_score")


def get_asset_target_info(asset_name: str) -> Optional[Dict]:
    """获取资产的运行时目标信息"""
    return ASSET_TO_TARGET.get(asset_name)


# ---------------------------------------------------------------------------
# 运行时 overlay（进程内即时生效，无需 reload）
# ---------------------------------------------------------------------------

def load_runtime_overlay() -> Dict[str, str]:
    """读取运行时 overlay，返回 {asset_name: text} 字典。

    不存在或损坏时返回空字典（不影响默认行为）。
    """
    if not RUNTIME_OVERLAY_FILE.exists():
        return {}
    try:
        data = json.loads(RUNTIME_OVERLAY_FILE.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            return {}
        # 仅保留字符串值，避免坏数据
        return {k: v for k, v in data.items() if isinstance(v, str)}
    except (json.JSONDecodeError, OSError):
        return {}


def save_runtime_overlay(overlay: Dict[str, str]) -> None:
    """原子写入运行时 overlay。"""
    RUNTIME_OVERLAY_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = RUNTIME_OVERLAY_FILE.with_suffix(".json.tmp")
    try:
        tmp.write_text(
            json.dumps(overlay, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        os.replace(str(tmp), str(RUNTIME_OVERLAY_FILE))
    except OSError:
        if tmp.exists():
            try:
                tmp.unlink()
            except OSError:
                pass
        raise


def get_runtime_overlay(asset_name: str) -> Optional[str]:
    """查询运行时 overlay 中某资产的最新内容。

    返回 None 表示无 overlay，调用方应回退到源文件常量。
    这是面向 YunliPersonaEngine 等运行时代码的查询入口。
    """
    overlay = load_runtime_overlay()
    return overlay.get(asset_name)


def clear_runtime_overlay(asset_name: Optional[str] = None) -> None:
    """清空运行时 overlay。

    Args:
        asset_name: 仅清空指定资产；None 表示清空全部。
    """
    overlay = load_runtime_overlay()
    if asset_name is None:
        if RUNTIME_OVERLAY_FILE.exists():
            RUNTIME_OVERLAY_FILE.unlink()
        return
    if asset_name in overlay:
        del overlay[asset_name]
        save_runtime_overlay(overlay)


# ---------------------------------------------------------------------------
# 原子写入 + 备份
# ---------------------------------------------------------------------------

def atomic_write_with_backup(
    target_path: Path,
    new_content: str,
    backup_path: Path,
) -> Tuple[bool, str]:
    """原子地将 new_content 写入 target_path，写入前先备份原文件到 backup_path。

    Returns:
        (success, message)。失败时自动从 backup_path 恢复。

    实现细节：
      1. 写入临时文件 *.tmp（与目标同目录，确保 os.replace 原子）
      2. shutil.copy2 原文件 → backup_path（保留 mtime）
      3. os.replace(tmp, target)（POSIX 原子，Windows 同卷内也原子）
      4. 任何 OSError → 清理 tmp，若 backup 存在则恢复
    """
    target_path.parent.mkdir(parents=True, exist_ok=True)
    backup_path.parent.mkdir(parents=True, exist_ok=True)

    tmp_path = target_path.with_suffix(target_path.suffix + ".tmp")

    # 1. 先备份
    if target_path.exists():
        try:
            shutil.copy2(target_path, backup_path)
        except OSError as e:
            return False, f"备份失败: {e}"

    # 2. 写临时文件
    try:
        tmp_path.write_text(new_content, encoding="utf-8")
    except OSError as e:
        # 写 tmp 失败不影响原文件
        if tmp_path.exists():
            try:
                tmp_path.unlink()
            except OSError:
                pass
        return False, f"写临时文件失败: {e}"

    # 3. 原子替换
    try:
        os.replace(str(tmp_path), str(target_path))
    except OSError as e:
        # 替换失败 → 清理 tmp，从备份恢复
        if tmp_path.exists():
            try:
                tmp_path.unlink()
            except OSError:
                pass
        if backup_path.exists():
            try:
                shutil.copy2(backup_path, target_path)
            except OSError as restore_err:
                return False, (
                    f"原子替换失败: {e}；且恢复备份失败: {restore_err}"
                )
        return False, f"原子替换失败: {e}，已从备份恢复"

    return True, f"已原子写入（备份: {backup_path.name}）"


# ---------------------------------------------------------------------------
# 语法验证
# ---------------------------------------------------------------------------

def _validate_python_syntax(source: str, hint_path: str = "<source>") -> Tuple[bool, str]:
    """校验 source 是否为合法 Python。"""
    try:
        compile(source, hint_path, "exec")
        return True, ""
    except SyntaxError as e:
        return False, f"line {e.lineno}: {e.msg}"


# ---------------------------------------------------------------------------
# Diff 生成（dry-run 用）
# ---------------------------------------------------------------------------

def _build_diff(asset_name: str, target_file: str, var_name: str,
                old_source: str, new_replacement: str) -> str:
    """生成人类可读的 diff 字符串（unified diff 简化版）。"""
    old_lines = old_source.splitlines()
    new_lines = new_replacement.splitlines()
    # 用最朴素的前缀对比，足够 dry-run 阅读
    diff_lines = [
        f"[dry-run] {asset_name} :: {target_file}::{var_name}",
        f"--- {asset_name} :: {target_file}::{var_name}",
        f"+++ {asset_name} :: {target_file}::{var_name}",
        f"@@ {len(old_lines)} lines → {len(new_lines)} lines @@",
    ]
    # 仅输出变量替换段落（简化：前 8 行）
    preview_old = "\n".join(old_lines[:8])
    preview_new = "\n".join(new_lines[:8])
    diff_lines.append(f"- old (前 8 行): {preview_old!r}")
    diff_lines.append(f"+ new (前 8 行): {preview_new!r}")
    return "\n".join(diff_lines)


# ---------------------------------------------------------------------------
# 核心：apply_asset_to_runtime
# ---------------------------------------------------------------------------

def apply_asset_to_runtime(
    asset_name: str,
    plugin_dir: Path,
    dry_run: bool = False,
    write_overlay: bool = False,  # 默认 False 保持向后兼容
) -> Tuple[bool, str]:
    """将进化后的资产写回运行时（线程安全，含回滚 + dry-run + overlay）。

    Args:
        asset_name: 资产名称
        plugin_dir: 插件根目录（如 d:\\QQ bot\\astrbot\\data\\plugins\\yunli）
        dry_run: True 时只生成 diff，不修改任何文件
        write_overlay: True 时同步更新 applied_runtime.json
            （让进程内立即生效，无需 reload 模块）

    Returns:
        (success, message)。message 在 dry_run 时为 diff 文本。
    """
    if asset_name not in ASSET_TO_TARGET:
        return False, f"未知资产: {asset_name}"

    # 延迟导入锁（避免循环依赖）
    try:
        from ._locks import asset_lock
    except ImportError:
        from evolution._locks import asset_lock

    with asset_lock:
        evolved = load_evolved_asset(asset_name)
        if evolved is None:
            return False, f"进化资产 {asset_name} 不存在，请先运行 /云璃进化 baseline"

        # 安全沙箱：进化资产若包含可执行代码则拒绝应用
        ok, reason = code_sandbox.validate_text_if_code(evolved)
        if not ok:
            return False, f"安全沙箱拒绝 {asset_name}: {reason}"

        target = ASSET_TO_TARGET[asset_name]
        target_path = plugin_dir / target["file"]
        if not target_path.exists():
            return False, f"目标文件不存在: {target_path}"

        var_name = target["var"]
        # 读取目标文件
        original_content = target_path.read_text(encoding="utf-8")

        # AST 定位变量定义行号
        import ast

        try:
            tree = ast.parse(original_content)
        except SyntaxError:
            return False, f"目标文件 {target['file']} 语法错误，无法解析"

        var_node = None
        for node in ast.walk(tree):
            if isinstance(node, ast.Assign):
                for target_node in node.targets:
                    if isinstance(target_node, ast.Name) and target_node.id == var_name:
                        var_node = node
                        break
                if var_node:
                    break

        if var_node is None:
            # 目标文件中没有该常量 —— 向后兼容：默认行为是「失败」，
            # 当用户显式 opt-in overlay 时才能成功（仍跳过源文件写入）。
            if dry_run:
                return True, (
                    f"[dry-run] {asset_name}: 目标文件 {target['file']} 中无变量 {var_name}，"
                    f"无 diff；overlay 计划写入（{len(evolved)} 字符）"
                )
            if not write_overlay:
                # 默认行为：保持原有的失败返回，避免破坏既有行为
                return False, f"在 {target['file']} 中未找到变量 {var_name}"
            # opt-in 路径：跳过源文件，但写入 overlay
            overlay = load_runtime_overlay()
            overlay[asset_name] = evolved
            save_runtime_overlay(overlay)
            return True, (
                f"⚠ {asset_name}: 目标文件 {target['file']} 中无变量 {var_name}，"
                f"已跳过源文件修改并写入 overlay（{len(evolved)} 字符）"
            )

        # 计算替换范围
        start_line = var_node.lineno  # 1-based
        end_line = var_node.end_lineno or start_line
        lines = original_content.split("\n")
        # 保留原行的缩进（关键：原变量赋值在类/模块体内时需要正确缩进）
        original_line = lines[start_line - 1]
        stripped = original_line.lstrip()
        indent = original_line[: len(original_line) - len(stripped)]
        # 拆分替换内容为多行，逐行加缩进（首行沿用原 indent，后续行也沿用）
        replacement_body = f'{var_name} = """\n{evolved}\n"""'
        replacement_lines = [indent + line for line in replacement_body.split("\n")]
        new_lines = lines[: start_line - 1] + replacement_lines + lines[end_line:]
        new_content = "\n".join(new_lines)

        # 语法校验
        ok, err = _validate_python_syntax(new_content, str(target_path))
        if not ok:
            return False, (
                f"替换后语法错误 ({err})，已拒绝写入。"
                f"可能原因：资产内容包含与源文件相同的引号定界符。"
            )

        # Dry-run：仅返回 diff
        if dry_run:
            diff = _build_diff(asset_name, target["file"], var_name, original_content, new_content)
            return True, diff

        # 准备备份路径
        BACKUP_DIR.mkdir(parents=True, exist_ok=True)
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        backup_name = f"{target['file'].replace('/', '_')}.{ts}.bak"
        backup_path = BACKUP_DIR / backup_name

        # 原子写源文件
        ok, write_msg = atomic_write_with_backup(target_path, new_content, backup_path)
        if not ok:
            return False, f"{asset_name} 源文件写入失败: {write_msg}"

        # 写 overlay
        overlay_msg = ""
        if write_overlay:
            try:
                overlay = load_runtime_overlay()
                overlay[asset_name] = evolved
                save_runtime_overlay(overlay)
                runtime_hook_active = bool(target.get("runtime_hook"))
                if runtime_hook_active:
                    overlay_msg = f"；overlay 已激活（{target['runtime_hook']}）"
                else:
                    overlay_msg = (
                        f"；overlay 已写入（{len(evolved)} 字符），"
                        f"但 {asset_name} 暂未接入运行时 hook，"
                        f"需 reload 模块或重启进程方可生效"
                    )
            except OSError as e:
                overlay_msg = f"；overlay 写入失败: {e}"

        return True, (
            f"已应用 {asset_name} → {target['file']}::{var_name}"
            f"（备份: {backup_name}）{overlay_msg}"
        )


# ---------------------------------------------------------------------------
# apply_evolved_assets（任务要求函数名的便捷批量接口）
# ---------------------------------------------------------------------------

def apply_evolved_assets(
    plugin_dir: Path,
    asset_names: Optional[List[str]] = None,
    dry_run: bool = False,
    write_overlay: bool = False,  # 默认 False 保持向后兼容
) -> Dict[str, Tuple[bool, str]]:
    """批量应用进化资产。

    Args:
        plugin_dir: 插件根目录
        asset_names: 要应用的资产列表；None 表示自动从 get_apply_status
            推断（仅 apply 已进化但未应用的资产）
        dry_run: 是否仅预览
        write_overlay: 是否同步更新运行时 overlay

    Returns:
        {asset_name: (success, message)}
    """
    if asset_names is None:
        status = get_apply_status(plugin_dir)
        if isinstance(status, dict) and "error" in status:
            return {}
        # 选所有「已进化」且「未应用」的；dry-run 时也包括已应用的，方便对比
        asset_names = [
            name for name, info in status.items()
            if info.get("evolved") and (dry_run or not info.get("applied"))
        ]

    results: Dict[str, Tuple[bool, str]] = {}
    for name in asset_names:
        results[name] = apply_asset_to_runtime(
            name, plugin_dir, dry_run=dry_run, write_overlay=write_overlay
        )
    return results


# ---------------------------------------------------------------------------
# 状态查询
# ---------------------------------------------------------------------------

def get_apply_status(plugin_dir: Path) -> Dict:
    """获取所有资产的进化状态与是否已应用"""
    status = {}
    if not BASELINE_FILE.exists():
        return {"error": "尚未建立基线，请先运行 /云璃进化 baseline"}

    baseline = json.loads(BASELINE_FILE.read_text(encoding="utf-8"))

    # 预先读 overlay 一次
    overlay = load_runtime_overlay()

    for asset_name in ASSET_TO_TARGET:
        target_info = ASSET_TO_TARGET[asset_name]
        item = {
            "description": target_info["description"],
            "target": f"{target_info['file']}::{target_info['var']}",
            "runtime_hook": target_info.get("runtime_hook"),
            "evolved": is_evolved_asset_available(asset_name),
            "score": get_evolved_asset_score(asset_name),
            "applied": False,
            "applied_via_overlay": asset_name in overlay,
        }

        if asset_name in baseline:
            item["baseline_score"] = baseline[asset_name].get("total_score")

        # 检查是否已应用（通过比较资产文件与运行时代码）
        evolved = load_evolved_asset(asset_name)
        if evolved:
            target_path = plugin_dir / target_info["file"]
            if target_path.exists():
                runtime_content = target_path.read_text(encoding="utf-8")
                # 简单检查：进化后的文本是否出现在运行时代码中
                # 使用前 100 字符作为指纹
                fingerprint = evolved.strip()[:100]
                if fingerprint in runtime_content:
                    item["applied"] = True

        status[asset_name] = item

    return status