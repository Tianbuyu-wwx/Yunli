"""Darwin 进化循环主程序

实现花叔 Darwin Skill v2.0 的棘轮机制优化循环：
1. 基线评估：对每个文本资产做 10 维度评分
2. 针对性改进：找出最低分维度，生成改进方案
3. 验证测试：重新评分，对比改进前后
4. 保留/回滚：新分 > 旧分才保留（棘轮机制）

LLM 调用方式：
- 通过 AstrBot 的 provider.text_chat() 调用（与 memory_manager 一致）
- 也支持独立 CLI 运行（需设置环境变量 DEEPSEEK_API_KEY）
"""

import argparse
import asyncio
import json
import logging
import os
import time
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

try:
    # AstrBot 内通过 main.py 导入（包内相对导入）
    from .eval.rubric import RUBRIC
    from .eval.scorer import (
        build_scoring_prompt,
        parse_scoring_response,
        format_score_report,
        compare_scores,
        AssetScore,
        DimensionScore,
    )
except ImportError:
    # CLI 直接运行时（__package__ 为 None）
    from eval.rubric import RUBRIC
    from eval.scorer import (
        build_scoring_prompt,
        parse_scoring_response,
        format_score_report,
        compare_scores,
        AssetScore,
        DimensionScore,
    )


# =============================================================================
# 配置
# =============================================================================

EVOLUTION_DIR = Path(__file__).resolve().parent
ASSETS_DIR = EVOLUTION_DIR / "assets"
BASELINE_FILE = EVOLUTION_DIR / "baseline.json"
EVOLUTION_LOG = EVOLUTION_DIR / "evolution_log.md"
TEST_PROMPTS_FILE = EVOLUTION_DIR / "test_prompts.json"
RESULTS_TSV = EVOLUTION_DIR / "results.tsv"

# 资产列表（与 asset_bridge.py 中的 ASSET_TO_TARGET 键名保持一致）
# 新增资产时需同步更新 ASSET_TO_TARGET 和 assets/ 目录下的 .md 文件
ASSET_NAMES = [
    "system_prompt",
    "review_rules",
    "filter_rules",
    "emotion_templates",
    "language_style",
]

# 默认值
DEFAULT_MAX_ITERATIONS = 3
DEFAULT_MIN_IMPROVEMENT = 0.5

# 并行评估：默认并行线程数
DEFAULT_PARALLEL_WORKERS = 0  # 0=串行, >0=并行线程数

# 独立评分参数：评分用低温度保证客观性，改进用高温度激发创造力
# 参考 Darwin Skill v2.0 "评分AI与写skill的AI分开" 原则
DEFAULT_SCORING_TEMPERATURE = 0.2   # 低温度：客观、一致、非创造性
DEFAULT_IMPROVEMENT_TEMPERATURE = 0.7  # 高温度：探索更多可能性

# 探索性重写：连续改进无效时触发全量重写，突破局部最优
# 参考 Darwin Skill v2.0 "exploratory rewriting" 机制
DEFAULT_EXPLORATORY_THRESHOLD = 3   # 连续 N 轮无改进后触发探索性重写
DEFAULT_EXPLORATORY_TEMPERATURE = 0.9  # 探索性重写温度（更高，最大化多样性）
DEFAULT_EXPLORATORY_CANDIDATES = 3  # 探索性重写生成候选数

# 预计算维度名称→key 映射（避免每次调用重建）
_NAME_TO_KEY = {v["name"]: k for k, v in RUBRIC.items()}

# 备份文件最大保留数量
MAX_BACKUP_FILES = 10


# =============================================================================
# 资产管理
# =============================================================================

def load_asset(asset_name: str) -> str:
    """加载文本资产"""
    file_path = ASSETS_DIR / f"{asset_name}.md"
    if not file_path.exists():
        raise FileNotFoundError(f"资产文件不存在: {file_path}")
    return file_path.read_text(encoding="utf-8")


def load_all_assets() -> Dict[str, str]:
    """加载所有文本资产"""
    assets = {}
    for name in ASSET_NAMES:
        try:
            assets[name] = load_asset(name)
        except FileNotFoundError as e:
            logger.warning(f"警告: {e}")
    return assets


def save_asset(asset_name: str, content: str):
    """保存文本资产（先写临时文件再 rename，保证原子性；线程安全）"""
    try:
        from ._locks import asset_lock
    except ImportError:
        from evolution._locks import asset_lock

    with asset_lock:
        file_path = ASSETS_DIR / f"{asset_name}.md"
        tmp_path = ASSETS_DIR / f"{asset_name}.tmp.{uuid.uuid4().hex[:8]}.md"

        # 先写临时文件
        tmp_path.write_text(content, encoding="utf-8")

        # 备份旧文件（如果存在），使用 uuid 避免同名冲突
        backup_path = ASSETS_DIR / f"{asset_name}.bak.{uuid.uuid4().hex[:8]}.md"
        if file_path.exists():
            if backup_path.exists():
                backup_path.unlink()  # 同名备份已存在则删除
            file_path.rename(backup_path)

        # 原子替换
        tmp_path.rename(file_path)

        logger.info(f"  已保存: {file_path}")
        logger.info(f"  旧版本备份: {backup_path.name}")

        # 清理旧备份文件（保留最近 MAX_BACKUP_FILES 个）
        _cleanup_old_backups(asset_name)


def _cleanup_old_backups(asset_name: str):
    """清理指定资产的旧备份文件，保留最近 MAX_BACKUP_FILES 个"""
    pattern = f"{asset_name}.bak.*.md"
    backups = sorted(ASSETS_DIR.glob(pattern), key=lambda p: p.stat().st_mtime, reverse=True)
    for old in backups[MAX_BACKUP_FILES:]:
        try:
            old.unlink()
        except OSError:
            pass


def load_baseline() -> Optional[Dict]:
    """加载基线评分"""
    if not BASELINE_FILE.exists():
        return {}
    try:
        return json.loads(BASELINE_FILE.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, ValueError):
        return {}


def save_baseline(scores: Dict):
    """保存基线评分"""
    BASELINE_FILE.write_text(
        json.dumps(scores, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def save_evolution_log_snapshot(scores: Dict, phase: str):
    """保存进化日志快照

    跳过模拟评分数据（所有资产总分相同且无实际 LLM 评估时），避免日志膨胀。
    """
    if not scores:
        return

    # 跳过模拟数据：所有资产分数相同且为常见模拟分数（如 70.0/100）
    score_values = [data.get("total_score", 0) for data in scores.values()]
    if score_values and all(s == score_values[0] for s in score_values):
        # 检查是否有 eval_mode 标记为真实评估
        has_real_eval = any(
            data.get("eval_mode") not in ("mock", None)
            for data in scores.values()
        )
        if not has_real_eval:
            return  # 全是模拟数据，不写入日志

    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with open(EVOLUTION_LOG, "a", encoding="utf-8") as f:
        f.write(f"\n## {phase} — {timestamp}\n\n")
        for asset_name, score_data in scores.items():
            f.write(f"### {asset_name}\n")
            f.write(f"- 总分: {score_data['total_score']}/100\n")
            for dim in score_data.get("dimensions", []):
                f.write(f"  - {dim['name']}: {dim['score']}/10\n")
            f.write("\n")


# =============================================================================
# 辅助函数
# =============================================================================

def get_dimension_key(dim_name: str) -> str:
    """根据中文名称获取维度 key（使用预计算缓存）"""
    return _NAME_TO_KEY.get(dim_name, "unknown")


def find_weakest_dimension(asset_score: AssetScore) -> Optional[DimensionScore]:
    """找出评分最低的维度（考虑权重，选加权后最低的）"""
    weakest = None
    lowest_weighted = float("inf")

    for dim in asset_score.dimensions:
        key = get_dimension_key(dim.name)
        weight = RUBRIC.get(key, {}).get("weight", 1.0)
        weighted = dim.score * weight
        if weighted < lowest_weighted:
            lowest_weighted = weighted
            weakest = dim

    return weakest


def build_improvement_prompt(asset_name: str, asset_content: str,
                              weak_dimension: DimensionScore) -> str:
    """构建改进提示词"""
    return f"""你是云璃角色扮演 AI 的提示词优化专家。现在需要优化一份文本资产中评分最低的维度。

资产名称：{asset_name}

当前资产内容：
```
{asset_content}
```

需要改进的维度：{weak_dimension.name}
该维度当前问题：
{chr(10).join(f'- {issue}' for issue in weak_dimension.issues)}

该维度的评分标准：
{RUBRIC.get(get_dimension_key(weak_dimension.name), {}).get('description', weak_dimension.name)}

请直接输出改进后的完整资产文本。要求：
1. 只改进目标维度相关的内容，其他部分保持不变
2. 改进要具体、可执行，不能只是换换说法
3. 保持 Token 效率，不要增加不必要的冗余
4. 保持与云璃人设的完全一致性
5. 直接输出纯文本，不要加任何解释、标记或代码块

改进后的资产文本："""


def dict_to_asset_score(asset_name: str, bd: dict) -> AssetScore:
    """将基线字典转换为 AssetScore 对象（共享工具函数）

    供 darwin_evolve.run_evolve() 和 parallel_eval.run_evolve_parallel() 共用。
    """
    return AssetScore(
        asset_name=asset_name,
        asset_path=str(ASSETS_DIR / f"{asset_name}.md"),
        dimensions=[
            DimensionScore(
                name=d["name"], score=d["score"], max_score=10.0,
                issues=d.get("issues", []),
                suggestions=d.get("suggestions", []),
            )
            for d in bd["dimensions"]
        ],
        total_score=bd["total_score"],
        overall_comment=bd.get("overall_comment", ""),
    )


# =============================================================================
# DarwinEvolution 类 —— 通过 AstrBot provider 调用 LLM
# =============================================================================

class DarwinEvolution:
    """Darwin 进化引擎

    通过 AstrBot 的 LLM provider 调用模型进行评分和优化。
    使用方式与 memory_manager.py 中的 LLM 调用一致。

    Args:
        provider: AstrBot LLM provider（来自 context.get_provider()）
        config: 进化配置字典，支持以下键：
            - evolution_max_iterations (int): 每资产最大迭代次数，默认 3
            - evolution_min_improvement (float): 最小改进阈值，默认 0.5
            - evolution_assets (list): 要进化的资产列表，默认全部
            - evolution_enabled (bool): 是否启用进化，默认 True
        log_callback: 可选，进度回调函数 (message: str) -> None
    """

    def __init__(self, provider=None, config: dict = None,
                 log_callback=None):
        self.provider = provider
        self.config = config or {}
        self.log_callback = log_callback or print

        self.max_iterations = self.config.get(
            "evolution_max_iterations", DEFAULT_MAX_ITERATIONS
        )
        self.min_improvement = self.config.get(
            "evolution_min_improvement", DEFAULT_MIN_IMPROVEMENT
        )
        # 独立评分参数：评分用低温度，改进用高温度
        self.scoring_temperature = self.config.get(
            "evolution_scoring_temperature", DEFAULT_SCORING_TEMPERATURE
        )
        self.improvement_temperature = self.config.get(
            "evolution_improvement_temperature", DEFAULT_IMPROVEMENT_TEMPERATURE
        )
        # 探索性重写配置
        self.exploratory_threshold = self.config.get(
            "evolution_exploratory_threshold", DEFAULT_EXPLORATORY_THRESHOLD
        )
        self.exploratory_temperature = self.config.get(
            "evolution_exploratory_temperature", DEFAULT_EXPLORATORY_TEMPERATURE
        )
        self.exploratory_candidates = self.config.get(
            "evolution_exploratory_candidates", DEFAULT_EXPLORATORY_CANDIDATES
        )
        self.parallel_workers = self.config.get(
            "evolution_parallel_workers", DEFAULT_PARALLEL_WORKERS
        )
        config_assets = self.config.get("evolution_assets", ASSET_NAMES)
        self.enabled_assets = [a for a in config_assets if a in ASSET_NAMES]
        self.enabled = self.config.get("evolution_enabled", True)

        # 输入校验
        if self.max_iterations < 0:
            raise ValueError(f"evolution_max_iterations 必须 >= 0，当前值: {self.max_iterations}")
        if self.parallel_workers < 0:
            raise ValueError(f"evolution_parallel_workers 必须 >= 0，当前值: {self.parallel_workers}")
        for name, val in [("evolution_scoring_temperature", self.scoring_temperature),
                          ("evolution_improvement_temperature", self.improvement_temperature),
                          ("evolution_exploratory_temperature", self.exploratory_temperature)]:
            if not (0.0 <= val <= 2.0):
                raise ValueError(f"{name} 必须在 [0, 2] 范围内，当前值: {val}")

        # 统一 LLM 客户端（含超时保护 + HTTP fallback）
        # 支持外部注入预创建的 LLMClient（如 CLI 独立运行场景）
        external_llm = self.config.get("evolution_llm_client")
        if external_llm is not None:
            self._llm = external_llm
        else:
            try:
                from .llm_client import LLMClient
            except ImportError:
                from evolution.llm_client import LLMClient
            self._llm = LLMClient(provider, config, log_callback)

    def _log(self, msg: str):
        """线程安全日志输出"""
        try:
            from ._locks import print_lock
        except ImportError:
            from evolution._locks import print_lock
        with print_lock:
            self.log_callback(msg)

    def load_baseline(self) -> Dict:
        """加载基线评分（实例方法，供 parallel_eval 等外部模块调用）"""
        return load_baseline()

    # ========== LLM 调用 ==========

    def _call_llm_sync(self, prompt: str, system_prompt: str = "",
                       temperature: float = None) -> Optional[str]:
        """同步调用 LLM（委托给 LLMClient）

        保留此方法作为向后兼容的薄代理层。
        实际逻辑在 LLMClient.call() 中，包括：
        - Provider → HTTP fallback → None 三级降级
        - 事件循环适配 + 超时保护
        - temperature 参数传递
        """
        return self._llm.call(prompt, system_prompt, temperature)

    # ========== 结构化结果追踪 (Item 2) ==========

    def _write_results_tsv(self, asset_name: str, iteration: int, old_score: float,
                           new_score: float, target_dim: str, status: str,
                           eval_mode: str = "full_test"):
        """追加一行结构化追踪数据到 results.tsv（线程安全）

        参考 Darwin Skill v2.0 的 results.tsv 格式：
        timestamp | asset | iteration | old_score | new_score | delta | target_dim | status | eval_mode
        """
        try:
            from ._locks import results_lock
        except ImportError:
            from evolution._locks import results_lock

        timestamp = datetime.now().isoformat(timespec="seconds")
        delta = round(new_score - old_score, 1)

        with results_lock:
            # 表头（首次写入时），在锁内确保 TOCTOU 安全
            if not RESULTS_TSV.exists():
                RESULTS_TSV.write_text(
                    "timestamp\tasset\titeration\told_score\tnew_score\tdelta\ttarget_dim\tstatus\teval_mode\n",
                    encoding="utf-8",
                )

            row = f"{timestamp}\t{asset_name}\t{iteration}\t{old_score}\t{new_score}\t{delta:+}\t{target_dim}\t{status}\t{eval_mode}\n"
            with open(RESULTS_TSV, "a", encoding="utf-8") as f:
                f.write(row)

    def get_results_history(self) -> List[Dict]:
        """读取 results.tsv 并返回结构化列表"""
        if not RESULTS_TSV.exists():
            return []
        rows = []
        with open(RESULTS_TSV, "r", encoding="utf-8") as f:
            reader = f.readlines()
            if len(reader) < 2:  # 只有表头
                return []
            for line in reader[1:]:
                parts = line.strip().split("\t")
                if len(parts) >= 9:
                    rows.append({
                        "timestamp": parts[0],
                        "asset": parts[1],
                        "iteration": int(parts[2]),
                        "old_score": float(parts[3]),
                        "new_score": float(parts[4]),
                        "delta": float(parts[5]),
                        "target_dim": parts[6],
                        "status": parts[7],
                        "eval_mode": parts[8],
                    })
        return rows

    # ========== 执行测试 (Item 3) ==========

    def _load_test_prompts(self) -> Dict[str, List[Dict]]:
        """加载测试提示词文件

        test_prompts.json 格式:
        {
            "system_prompt": [
                {"prompt": "@云璃 你好", "expect": "应体现直率风格"},
                ...
            ],
            ...
        }
        """
        if not TEST_PROMPTS_FILE.exists():
            return {}
        try:
            return json.loads(TEST_PROMPTS_FILE.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, ValueError):
            return {}

    def _run_test_prompts(self, asset_name: str, asset_content: str) -> Dict:
        """对资产执行测试提示词，衡量实际效果

        将资产内容作为 system_prompt 注入，用测试 prompt 调用 LLM，
        然后让 LLM 评估回复质量。返回测试结果摘要。

        当 parallel_workers > 0 时自动使用线程池并行执行测试用例。

        Returns:
            {
                "passed": int,       # 通过的测试数
                "total": int,        # 总测试数
                "score": float,      # 加权得分 (0-10)
                "details": [...]     # 每个测试的详情
            }
        """
        test_data = self._load_test_prompts()
        asset_tests = test_data.get(asset_name, [])
        if not asset_tests:
            return {"passed": 0, "total": 0, "score": 0.0, "details": [],
                    "note": "无测试用例"}

        # 提取 JSON 工具函数（提前导入，避免在线程工作函数中重复导入）
        try:
            from .utils import extract_json_from_response
        except ImportError:
            from evolution.utils import extract_json_from_response

        def _eval_single_test(i: int, test_case: dict) -> dict:
            """评估单个测试用例（线程工作函数）"""
            prompt = test_case.get("prompt", "")
            expectation = test_case.get("expect", "")

            # 调用 LLM 用资产内容作为 system_prompt 执行测试
            response = self._call_llm_sync(
                prompt=prompt,
                system_prompt=asset_content,
                temperature=self.scoring_temperature,
            )

            if response is None:
                return {
                    "index": i,
                    "prompt": prompt,
                    "expect": expectation,
                    "response": None,
                    "pass": False,
                    "reason": "LLM 不可用",
                }

            # 让 LLM 评估回复是否符合预期
            eval_prompt = (
                f"请评估以下 AI 回复是否符合预期标准。\n\n"
                f"预期标准：{expectation}\n\n"
                f"AI 回复：{response}\n\n"
                f"请以 JSON 格式输出评估结果：\n"
                f'{{"pass": true/false, "reason": "简短理由", "score": 0-10}}'
            )

            eval_response = self._call_llm_sync(
                prompt=eval_prompt,
                system_prompt="你是一个客观的 AI 回复质量评审专家。",
                temperature=self.scoring_temperature,
            )

            if eval_response is None:
                return {
                    "index": i,
                    "prompt": prompt,
                    "expect": expectation,
                    "response": response[:200],
                    "pass": None,
                    "reason": "评估 LLM 不可用",
                }

            try:
                eval_data = extract_json_from_response(eval_response)
                if eval_data is None:
                    raise json.JSONDecodeError("解析失败", eval_response, 0)
                pass_val = eval_data.get("pass", False)
                eval_score = float(eval_data.get("score", 0))
                return {
                    "index": i,
                    "prompt": prompt,
                    "expect": expectation,
                    "response": response[:200],
                    "pass": pass_val,
                    "score": eval_score,
                    "reason": eval_data.get("reason", ""),
                }
            except (json.JSONDecodeError, ValueError, KeyError):
                return {
                    "index": i,
                    "prompt": prompt,
                    "expect": expectation,
                    "response": response[:200],
                    "pass": None,
                    "reason": "评估结果解析失败",
                }

        # 并行或串行执行测试用例
        if self.parallel_workers > 0 and len(asset_tests) > 1:
            from concurrent.futures import ThreadPoolExecutor, as_completed
            results = [None] * len(asset_tests)
            total_score = 0.0
            with ThreadPoolExecutor(max_workers=min(self.parallel_workers, len(asset_tests))) as executor:
                future_to_idx = {}
                for i, test_case in enumerate(asset_tests):
                    future = executor.submit(_eval_single_test, i, test_case)
                    future_to_idx[future] = i
                for future in as_completed(future_to_idx):
                    idx = future_to_idx[future]
                    try:
                        result = future.result()
                        results[idx] = result
                        if result.get("score") is not None:
                            total_score += result["score"]
                    except Exception as e:
                        results[idx] = {
                            "index": idx,
                            "prompt": asset_tests[idx].get("prompt", ""),
                            "expect": asset_tests[idx].get("expect", ""),
                            "response": None,
                            "pass": False,
                            "reason": f"测试执行异常: {e}",
                        }
        else:
            results = []
            total_score = 0.0
            for i, test_case in enumerate(asset_tests):
                result = _eval_single_test(i, test_case)
                results.append(result)
                if result.get("score") is not None:
                    total_score += result["score"]

        passed = sum(1 for r in results if r.get("pass") is True)
        total = len(asset_tests)
        avg_score = round(total_score / max(total, 1), 1) if total > 0 else 0.0

        return {
            "passed": passed,
            "total": total,
            "score": avg_score,
            "details": results,
        }

    # ========== 探索性重写 (Item 4) ==========

    def _exploratory_rewrite(self, asset_name: str, asset_content: str, current_score=None) -> Optional[str]:
        """探索性全量重写：当 hill-climbing 陷入局部最优时触发

        参考 Darwin Skill v2.0 的 exploratory rewriting 机制：
        - 使用极高 temperature (0.9) 生成多个候选版本
        - 对每个候选评分，选最优
        - 如果最优候选比当前好，返回；否则返回 None

        当 parallel_workers > 0 时，候选生成和评分均使用线程池并行执行。

        Returns:
            最优候选文本，或 None（无改进）
        """
        self._log(f"  [探索性重写] 触发全量重写，生成 {self.exploratory_candidates} 个候选版本...")

        prompt = self._build_exploratory_prompt(asset_name, asset_content)
        system_prompt = (
            "你是一个创意无限的 AI 提示词设计师。请大胆重新设计文本，"
            "不要被现有结构束缚，尝试全新的表达方式——但要保持云璃人设的核心不变。"
        )

        # ---- 并行/串行生成候选 ----
        if self.parallel_workers > 0 and self.exploratory_candidates > 1:
            candidates = self._generate_candidates_parallel(
                prompt, system_prompt, self.exploratory_candidates,
            )
        else:
            candidates = self._generate_candidates_serial(
                prompt, system_prompt, self.exploratory_candidates,
            )

        if not candidates:
            self._log(f"  [探索性重写] 未能生成任何候选，放弃")
            return None

        # ---- 并行/串行评分候选 ----
        self._log(f"  [探索性重写] 对 {len(candidates)} 个候选评分...")
        if self.parallel_workers > 0 and len(candidates) > 1:
            scored = self._score_candidates_parallel(asset_name, candidates)
        else:
            scored = self._score_candidates_serial(asset_name, candidates)

        best_candidate, best_score = scored

        # 对当前版本评分以对比（使用调用者传入的 current_score 避免重复评分）
        # 如果调用者未传入，则重新评分
        if current_score is None:
            current_score = self.score_asset(asset_name, asset_content)
        self._log(f"  当前版本: {current_score.total_score}/100")
        self._log(f"  最优候选: {best_score}/100 (变化: {best_score - current_score.total_score:+.1f})")

        if best_score > current_score.total_score:
            self._log(f"  [探索性重写] 找到更优版本 (+{best_score - current_score.total_score:.1f})")
            return best_candidate
        else:
            self._log(f"  [探索性重写] 无改进，放弃")
            return None

    def _generate_candidates_serial(self, prompt: str, system_prompt: str,
                                     count: int) -> List[str]:
        """串行生成候选版本"""
        candidates = []
        for i in range(count):
            self._log(f"    生成候选 {i + 1}/{count}...")
            response = self._call_llm_sync(
                prompt=prompt,
                system_prompt=system_prompt,
                temperature=self.exploratory_temperature,
            )
            if response:
                candidates.append(response.strip())
        return candidates

    def _generate_candidates_parallel(self, prompt: str, system_prompt: str,
                                       count: int) -> List[str]:
        """并行生成候选版本（线程池）"""
        from concurrent.futures import ThreadPoolExecutor, as_completed

        self._log(f"    并行生成 {count} 个候选（{self.parallel_workers} 线程）...")
        candidates = [None] * count

        with ThreadPoolExecutor(max_workers=min(self.parallel_workers, count)) as executor:
            future_to_idx = {}
            for i in range(count):
                future = executor.submit(
                    self._call_llm_sync,
                    prompt=prompt,
                    system_prompt=system_prompt,
                    temperature=self.exploratory_temperature,
                )
                future_to_idx[future] = i

            for future in as_completed(future_to_idx):
                idx = future_to_idx[future]
                try:
                    response = future.result()
                    if response:
                        candidates[idx] = response.strip()
                        self._log(f"    候选 {idx + 1}/{count} 生成完成")
                except Exception as e:
                    self._log(f"    候选 {idx + 1} 生成异常: {e}")

        return [c for c in candidates if c is not None]

    def _score_candidates_serial(self, asset_name: str,
                                  candidates: List[str]) -> Tuple[Optional[str], float]:
        """串行评分候选版本，返回 (最优候选, 最优分数)"""
        best_candidate = None
        best_score = -1.0
        for i, candidate in enumerate(candidates):
            score = self.score_asset(asset_name, candidate)
            self._log(f"    候选 {i + 1}: {score.total_score}/100")
            if score.total_score > best_score:
                best_score = score.total_score
                best_candidate = candidate
        return best_candidate, best_score

    def _score_candidates_parallel(self, asset_name: str,
                                    candidates: List[str]) -> Tuple[Optional[str], float]:
        """并行评分候选版本（线程池），返回 (最优候选, 最优分数)"""
        from concurrent.futures import ThreadPoolExecutor, as_completed

        scores = [None] * len(candidates)

        with ThreadPoolExecutor(max_workers=min(self.parallel_workers, len(candidates))) as executor:
            future_to_idx = {}
            for i, candidate in enumerate(candidates):
                future = executor.submit(self.score_asset, asset_name, candidate)
                future_to_idx[future] = i

            for future in as_completed(future_to_idx):
                idx = future_to_idx[future]
                try:
                    asset_score = future.result()
                    scores[idx] = asset_score
                    self._log(f"    候选 {idx + 1}: {asset_score.total_score}/100")
                except Exception as e:
                    self._log(f"    候选 {idx + 1} 评分异常: {e}")

        best_candidate = None
        best_score = -1.0
        for i, asset_score in enumerate(scores):
            if asset_score is not None and asset_score.total_score > best_score:
                best_score = asset_score.total_score
                best_candidate = candidates[i]

        return best_candidate, best_score

    def _build_exploratory_prompt(self, asset_name: str, asset_content: str) -> str:
        """构建探索性重写提示词（鼓励彻底重构）"""
        return f"""你是一个创意无限的 AI 提示词设计师。请对以下角色扮演 AI 的配置文本进行彻底重构。

资产名称：{asset_name}

当前文本：
```
{asset_content}
```

要求：
1. 保持核心目标不变（云璃角色扮演），但重新组织结构和表达方式
2. 可以尝试不同的段落顺序、不同的语调、不同的示例
3. 可以考虑添加新的约束或移除冗余的约束
4. 保持 Token 效率，总长度不要超过当前文本的 1.5 倍
5. 保持与云璃人设的完全一致性
6. 直接输出纯文本，不要加任何解释、标记或代码块

重构后的文本："""

    # ========== 评分与改进 ==========

    def score_asset(self, asset_name: str, asset_content: str) -> AssetScore:
        """对单个资产进行 10 维度评分（使用低 temperature 保证客观性）"""
        prompt = build_scoring_prompt(asset_name, asset_content)
        system_prompt = "你是一个专业的 AI 文本质量评审专家。请严格按照评分标准进行客观评分。"

        response = self._call_llm_sync(
            prompt, system_prompt,
            temperature=self.scoring_temperature,
        )
        if response is None:
            self._log(f"  [降级] LLM 不可用，使用模拟评分")
            return self._mock_score(asset_name)

        try:
            return parse_scoring_response(
                response, asset_name,
                str(ASSETS_DIR / f"{asset_name}.md"),
            )
        except Exception as e:
            self._log(f"  [解析失败] {e}，使用模拟评分")
            return self._mock_score(asset_name)

    def _mock_score(self, asset_name: str) -> AssetScore:
        """生成模拟评分（LLM 不可用时的降级方案）

        使用基于文本长度和维度权重的启发式差异化评分，
        避免所有资产得到相同的伪造分数。
        total_score 使用 -1 作为哨兵值，明确标识"无效评分"。
        """
        try:
            asset_content = load_asset(asset_name)
            content_len = len(asset_content)
        except Exception:
            content_len = 200  # 默认值

        dims = []
        for dim_key, dim_info in RUBRIC.items():
            weight = dim_info.get("weight", 1.0)
            # 基于内容长度和权重的启发式评分（3-8 分范围）
            base = max(3.0, min(8.0, content_len / 80))
            score = round(base * weight, 1)
            dims.append(DimensionScore(
                name=dim_info["name"],
                score=score,
                max_score=10.0,
                issues=["[模拟评分] 需要接入 LLM API 进行实际评分"],
                suggestions=["[模拟评分] 请配置 DEEPSEEK_API_KEY 或确保 AstrBot provider 可用"],
            ))
        return AssetScore(
            asset_name=asset_name,
            asset_path=str(ASSETS_DIR / f"{asset_name}.md"),
            dimensions=dims,
            total_score=-1.0,  # 哨兵值：标识 LLM 不可用
            overall_comment=f"[模拟评分 - 无效] LLM 不可用，所有维度均为基于启发式的估算值。"
                            f"资产长度: {content_len} 字符。请配置 LLM API 后重新评分。",
        )

    def improve_asset(self, asset_name: str, asset_content: str,
                      weak_dimension: DimensionScore) -> Optional[str]:
        """针对弱维度生成改进后的资产文本（使用高 temperature 激发创造力）"""
        prompt = build_improvement_prompt(asset_name, asset_content, weak_dimension)
        system_prompt = "你是一个专业的 AI 提示词优化专家。请直接输出改进后的文本，不要加任何解释。"

        response = self._call_llm_sync(
            prompt, system_prompt,
            temperature=self.improvement_temperature,
        )
        if response is None:
            self._log(f"  [跳过改进] LLM 不可用")
            return None

        return response.strip()

    # ========== 进化循环 ==========

    def evolve_single_asset(self, asset_name: str,
                            baseline_score: Optional[AssetScore] = None,
                            max_iterations: int = None) -> Dict:
        """对单个资产执行进化循环

        Returns:
            {
                "asset_name": str,
                "baseline_score": float,
                "final_score": float,
                "improved": bool,
                "iterations": int,
                "history": [...]
            }
        """
        if max_iterations is None:
            max_iterations = self.max_iterations

        self._log(f"\n{'='*60}")
        self._log(f"  进化资产: {asset_name}")
        self._log(f"{'='*60}")

        asset_content = load_asset(asset_name)
        history = []

        # Step 1: 基线评估
        self._log(f"  [1/3] 基线评估...")
        if baseline_score is None:
            current_score = self.score_asset(asset_name, asset_content)
        else:
            current_score = baseline_score

        self._log(f"  基线分数: {current_score.total_score}/100")
        history.append({
            "iteration": 0,
            "score": current_score.total_score,
            "dimensions": [
                {"name": d.name, "score": d.score} for d in current_score.dimensions
            ],
            "action": "baseline",
        })

        best_score = current_score
        best_content = asset_content
        last_iteration = 0  # 跟踪最后执行的迭代号
        consecutive_no_improvement = 0  # 连续无改进计数（探索性重写触发条件）

        # Step 2: 迭代改进
        for i in range(1, max_iterations + 1):
            last_iteration = i
            self._log(f"\n  [2/3] 第 {i} 轮改进...")

            # === 探索性重写触发检测 (Item 4) ===
            if consecutive_no_improvement >= self.exploratory_threshold:
                self._log(f"  [探索性重写] 连续 {consecutive_no_improvement} 轮无改进，触发全量重写...")
                exploratory_content = self._exploratory_rewrite(asset_name, best_content, current_score)
                if exploratory_content:
                    # 探索性重写找到更优版本
                    new_score = self.score_asset(asset_name, exploratory_content)
                    comparison = compare_scores(current_score, new_score)
                    self._log(f"  探索性重写后分数: {new_score.total_score}/100 (变化: {comparison['total_diff']:+.1f})")

                    old_score_val = current_score.total_score
                    history.append({
                        "iteration": i,
                        "score": new_score.total_score,
                        "dimensions": [
                            {"name": d.name, "score": d.score} for d in new_score.dimensions
                        ],
                        "dimension_diffs": comparison["dimension_diffs"],
                        "action": "keep" if comparison["improved"] else "revert",
                        "target_dimension": "探索性重写",
                    })
                    self._write_results_tsv(
                        asset_name, i, old_score_val, new_score.total_score,
                        "探索性重写",
                        "keep" if comparison["improved"] else "revert",
                        "exploratory",
                    )

                    if comparison["improved"] and comparison["total_diff"] >= self.min_improvement:
                        self._log(f"  [OK] 探索性重写有效 (+{comparison['total_diff']:.1f})，保留新版本")
                        best_score = new_score
                        best_content = exploratory_content
                        current_score = new_score
                        save_asset(asset_name, best_content)
                        consecutive_no_improvement = 0
                    else:
                        self._log(f"  [REVERT] 探索性重写无效，回滚")
                else:
                    self._log(f"  [探索性重写] 未能产生改进，继续正常迭代")
                consecutive_no_improvement = 0  # 重置计数器（已尝试探索）
                continue

            weakest = find_weakest_dimension(current_score)
            if weakest is None:
                self._log(f"  无法确定最弱维度，跳过")
                break

            self._log(f"  最弱维度: {weakest.name} ({weakest.score}/10)")
            if weakest.issues:
                self._log(f"  问题: {weakest.issues[0]}")

            improved_content = self.improve_asset(
                asset_name, best_content, weakest,
            )
            if improved_content is None:
                self._log(f"  无法生成改进，跳过")
                break

            # Step 3: 验证
            self._log(f"  [3/3] 验证改进...")
            new_score = self.score_asset(asset_name, improved_content)

            comparison = compare_scores(current_score, new_score)
            self._log(f"  新分数: {new_score.total_score}/100 (变化: {comparison['total_diff']:+.1f})")

            old_score_val = current_score.total_score

            history.append({
                "iteration": i,
                "score": new_score.total_score,
                "dimensions": [
                    {"name": d.name, "score": d.score} for d in new_score.dimensions
                ],
                "dimension_diffs": comparison["dimension_diffs"],
                "action": "keep" if comparison["improved"] else "revert",
                "target_dimension": weakest.name,
            })

            # === 结构化追踪 (Item 2) ===
            self._write_results_tsv(
                asset_name, i, old_score_val, new_score.total_score,
                weakest.name,
                "keep" if comparison["improved"] else "revert",
                "full_test",
            )

            # === 执行测试 (Item 3) ===
            test_results = self._run_test_prompts(asset_name, improved_content)
            if test_results["total"] > 0:
                self._log(f"  [测试] {test_results['passed']}/{test_results['total']} 通过, 得分: {test_results['score']}/10")

            # 棘轮机制（含关键维度退化保护）
            # 如果高权重维度退化超过阈值，即使总分提升也拒绝
            critical_regression = False
            if comparison.get("regressed_dims"):
                for dim_diff in comparison["regressed_dims"]:
                    # 高权重维度退化超过 2 分视为严重退化
                    if dim_diff.get("diff", 0) < -2.0:
                        critical_regression = True
                        self._log(f"  [WARN] 关键维度退化: {dim_diff.get('name', '?')} ({dim_diff.get('diff', 0):+.1f})")
                        break

            if comparison["improved"] and comparison["total_diff"] >= self.min_improvement and not critical_regression:
                self._log(f"  [OK] 改进有效 (+{comparison['total_diff']:.1f})，保留新版本")
                best_score = new_score
                best_content = improved_content
                current_score = new_score
                save_asset(asset_name, best_content)
                consecutive_no_improvement = 0
            else:
                self._log(f"  [REVERT] 改进无效或提升不足，回滚到上一版本（棘轮机制）")
                consecutive_no_improvement += 1

        # 最终结果
        final_score = (
            best_score.total_score if best_score
            else (baseline_score.total_score if baseline_score else 0)
        )
        improved = False
        if baseline_score:
            improved = final_score > baseline_score.total_score

        result = {
            "asset_name": asset_name,
            "baseline_score": baseline_score.total_score if baseline_score else final_score,
            "final_score": final_score,
            "improved": improved,
            "iterations": last_iteration,
            "history": history,
        }

        self._log(f"\n  -- 资产 {asset_name} 进化完成 --")
        self._log(f"  基线: {result['baseline_score']}/100 -> 最终: {result['final_score']}/100")
        self._log(f"  改进: {'是' if improved else '否（棘轮阻止了退化）'}")

        return result

    def run_baseline(self) -> Dict:
        """运行基线评估，返回所有资产的评分

        当 evolution_parallel_workers > 0 时自动使用并行评估。
        """
        if self.parallel_workers > 0:
            return self._run_baseline_parallel()

        return self._run_baseline_serial()

    def _run_baseline_serial(self) -> Dict:
        """串行基线评估（原有逻辑）"""
        self._log("=" * 60)
        self._log("  Darwin 进化系统 -- 建立基线")
        self._log("=" * 60)

        assets = load_all_assets()
        baseline = {}

        for asset_name in self.enabled_assets:
            if asset_name not in assets:
                self._log(f"跳过未加载的资产: {asset_name}")
                continue
            self._log(f"\n正在评估: {asset_name}")
            asset_score = self.score_asset(asset_name, assets[asset_name])
            self._log(format_score_report(asset_score))

            baseline[asset_name] = {
                "total_score": asset_score.total_score,
                "overall_comment": asset_score.overall_comment,
                "dimensions": [
                    {
                        "name": d.name,
                        "score": d.score,
                        "issues": d.issues,
                        "suggestions": d.suggestions,
                    }
                    for d in asset_score.dimensions
                ],
                "scored_at": datetime.now().isoformat(),
                "eval_mode": "serial",
            }

        save_baseline(baseline)
        save_evolution_log_snapshot(baseline, "基线评估")

        self._log("\n" + "=" * 60)
        self._log("  基线汇总")
        self._log("=" * 60)
        for name, data in baseline.items():
            self._log(f"  {name}: {data['total_score']}/100")

        self._log(f"\n基线已保存到: {BASELINE_FILE}")
        return baseline

    def _run_baseline_parallel(self) -> Dict:
        """并行基线评估（使用线程池并行评分）"""
        from concurrent.futures import ThreadPoolExecutor, as_completed

        self._log("=" * 60)
        self._log("  Darwin 进化系统 -- 并行基线评估")
        self._log(f"  并行线程数: {self.parallel_workers}")
        self._log("=" * 60)

        assets = load_all_assets()
        tasks = []
        for asset_name in self.enabled_assets:
            if asset_name not in assets:
                self._log(f"跳过未加载的资产: {asset_name}")
                continue
            tasks.append((asset_name, assets[asset_name]))

        if not tasks:
            self._log("无可评估资产")
            return {}

        self._log(f"\n提交 {len(tasks)} 个资产评分任务...")

        baseline = {}
        total_start = time.perf_counter()

        with ThreadPoolExecutor(max_workers=self.parallel_workers) as executor:
            future_to_name = {}
            for asset_name, content in tasks:
                future = executor.submit(self.score_asset, asset_name, content)
                future_to_name[future] = asset_name

            completed = 0
            for future in as_completed(future_to_name):
                asset_name = future_to_name[future]
                try:
                    asset_score = future.result()
                    completed += 1

                    baseline[asset_name] = {
                        "total_score": asset_score.total_score,
                        "overall_comment": asset_score.overall_comment,
                        "dimensions": [
                            {
                                "name": d.name,
                                "score": d.score,
                                "issues": d.issues,
                                "suggestions": d.suggestions,
                            }
                            for d in asset_score.dimensions
                        ],
                        "scored_at": datetime.now().isoformat(),
                        "eval_mode": "parallel",
                    }

                    self._log(f"\n[{completed}/{len(tasks)}] {asset_name}: {asset_score.total_score}/100")
                    self._log(format_score_report(asset_score))

                except Exception as e:
                    completed += 1
                    self._log(f"\n[{completed}/{len(tasks)}] {asset_name} 失败: {e}")

        total_elapsed = time.perf_counter() - total_start

        save_baseline(baseline)
        save_evolution_log_snapshot(baseline, "并行基线评估")

        self._log("\n" + "=" * 60)
        self._log("  并行基线汇总")
        self._log("=" * 60)
        for name, data in baseline.items():
            self._log(f"  {name}: {data['total_score']}/100")

        self._log(f"\n总耗时: {total_elapsed:.1f}s (并行 {self.parallel_workers} 线程)")
        self._log(f"基线已保存到: {BASELINE_FILE}")
        return baseline

    def run_evolve(self, target_assets: List[str] = None,
                   max_iterations: int = None) -> Dict:
        """运行进化循环

        当 parallel_workers > 0 且有多个资产时，各资产的进化循环并行执行。
        每个资产的 evolve_single_asset 独立运行，文件写入由 asset_lock 保护。

        Args:
            target_assets: 要进化的资产列表，None 表示全部
            max_iterations: 最大迭代次数，None 表示使用配置值

        Returns:
            {asset_name: result_dict}
        """
        self._log("=" * 60)
        self._log("  Darwin 进化系统 -- 棘轮优化循环")
        self._log("=" * 60)

        baseline_data = load_baseline()

        if target_assets is None:
            target_assets = self.enabled_assets

        if max_iterations is None:
            max_iterations = self.max_iterations

        # 构建各资产的 baseline_score
        asset_tasks = []  # [(asset_name, baseline_score), ...]
        for asset_name in target_assets:
            if asset_name not in ASSET_NAMES:
                self._log(f"未知资产: {asset_name}，跳过")
                continue

            baseline_score = None
            if baseline_data and asset_name in baseline_data:
                baseline_score = dict_to_asset_score(asset_name, baseline_data[asset_name])
            asset_tasks.append((asset_name, baseline_score))

        # 并行/串行执行进化循环
        if self.parallel_workers > 0 and len(asset_tasks) > 1:
            results = self._run_evolve_parallel(asset_tasks, max_iterations)
        else:
            results = self._run_evolve_serial(asset_tasks, max_iterations)

        # 汇总
        self._log("\n" + "=" * 60)
        self._log("  进化汇总")
        self._log("=" * 60)
        for name, result in results.items():
            diff = result["final_score"] - result["baseline_score"]
            status = "[OK]" if result["improved"] else "[--]"
            self._log(f"  {status} {name}: {result['baseline_score']:.1f} -> {result['final_score']:.1f} ({diff:+.1f})")

        # 保存新基线
        new_baseline = {}
        if baseline_data:
            new_baseline = dict(baseline_data)
        for name, result in results.items():
            if result["improved"]:
                new_baseline[name] = {
                    "total_score": result["final_score"],
                    "overall_comment": baseline_data.get(name, {}).get("overall_comment", "") if baseline_data else "",
                    "dimensions": result["history"][-1]["dimensions"] if result["history"] else [],
                    "scored_at": datetime.now().isoformat(),
                    "eval_mode": "serial",
                }

        if new_baseline:
            save_baseline(new_baseline)
            save_evolution_log_snapshot(new_baseline, "进化后基线")

        return results

    def _run_evolve_serial(self, asset_tasks: List[Tuple[str, Optional[AssetScore]]],
                           max_iterations: int) -> Dict:
        """串行执行多资产进化循环"""
        results = {}
        for asset_name, baseline_score in asset_tasks:
            result = self.evolve_single_asset(
                asset_name, baseline_score, max_iterations,
            )
            results[asset_name] = result
        return results

    def _run_evolve_parallel(self, asset_tasks: List[Tuple[str, Optional[AssetScore]]],
                             max_iterations: int) -> Dict:
        """并行执行多资产进化循环（线程池）

        每个资产的 evolve_single_call 在独立线程中运行，
        文件写入由 asset_lock 保护，日志由 _log 串行输出。
        """
        from concurrent.futures import ThreadPoolExecutor, as_completed

        self._log(f"\n并行进化 {len(asset_tasks)} 个资产（{self.parallel_workers} 线程）...")

        results = {}
        with ThreadPoolExecutor(max_workers=min(self.parallel_workers, len(asset_tasks))) as executor:
            future_to_name = {}
            for asset_name, baseline_score in asset_tasks:
                future = executor.submit(
                    self.evolve_single_asset,
                    asset_name, baseline_score, max_iterations,
                )
                future_to_name[future] = asset_name

            for future in as_completed(future_to_name):
                asset_name = future_to_name[future]
                try:
                    result = future.result()
                    results[asset_name] = result
                    self._log(f"\n  [完成] {asset_name}: {result['final_score']:.1f}/100")
                except Exception as e:
                    self._log(f"\n  [异常] {asset_name} 进化失败: {e}")
                    results[asset_name] = {
                        "asset_name": asset_name,
                        "baseline_score": 0,
                        "final_score": 0,
                        "improved": False,
                        "iterations": 0,
                        "history": [],
                        "error": str(e),
                    }

        return results

    def run_report(self) -> str:
        """生成评分报告文本"""
        baseline_data = load_baseline()
        if not baseline_data:
            return "未找到基线数据，请先运行 baseline"

        lines = ["=" * 60, "  Darwin 评分报告", "=" * 60]

        for asset_name, data in baseline_data.items():
            lines.append(f"\n--- {asset_name} ---")
            lines.append(f"总分: {data['total_score']}/100")
            lines.append(f"评语: {data.get('overall_comment', 'N/A')}")
            lines.append("各维度:")
            for dim in data["dimensions"]:
                score = dim.get("score")
                if score is None:
                    bar = "?" * 10
                    lines.append(f"  [{bar}] {dim.get('name', '?')}: N/A")
                else:
                    bar = "=" * int(score) + "-" * (10 - int(score))
                    lines.append(f"  [{bar}] {dim['name']}: {score}/10")
                if dim.get("issues"):
                    for issue in dim["issues"]:
                        lines.append(f"    [ISSUE] {issue}")

        return "\n".join(lines)


# =============================================================================
# 命令行入口（独立 CLI 运行）
# =============================================================================

def _create_standalone_llm_client(log_callback=None):
    """独立 CLI 运行时创建 LLMClient（使用环境变量）

    安全：API Key 仅从环境变量读取，启动时脱敏打印确认。
    复用统一的 LLMClient，避免与 llm_client.py 的 HTTP fallback 重复实现。
    """
    api_key = os.environ.get("DEEPSEEK_API_KEY", "")
    if not api_key:
        logger.warning("未设置 DEEPSEEK_API_KEY 环境变量，无法使用 LLM")
        return None

    # 脱敏打印，确认 Key 已加载
    masked = f"{api_key[:4]}***{api_key[-2:]}" if len(api_key) > 6 else f"{api_key[:2]}***"
    logger.info(f"API Key 已加载: {masked}")

    try:
        from .llm_client import LLMClient
    except ImportError:
        from evolution.llm_client import LLMClient

    return LLMClient(
        provider=None,
        config={"deepseek_api_key": api_key},
        log_callback=log_callback or print,
    )


def _cmd_baseline(args):
    llm_client = _create_standalone_llm_client(log_callback=print)
    engine = DarwinEvolution(
        provider=None,
        config={"evolution_llm_client": llm_client},
        log_callback=print,
    )
    engine.run_baseline()


def _cmd_evolve(args):
    llm_client = _create_standalone_llm_client(log_callback=print)
    engine = DarwinEvolution(
        provider=None,
        config={
            "evolution_llm_client": llm_client,
            "evolution_max_iterations": args.max_iter,
            "evolution_assets": [args.asset] if args.asset else ASSET_NAMES,
        },
        log_callback=print,
    )
    target = [args.asset] if args.asset else None
    engine.run_evolve(target_assets=target, max_iterations=args.max_iter)


def _cmd_report(args):
    engine = DarwinEvolution(log_callback=print)
    logger.info(engine.run_report())


def benchmark_serial_vs_parallel(provider=None, config: dict = None,
                                   log_callback: Callable = None) -> Dict:
    """基准测试：串行 vs 并行评分性能对比

    Returns:
        {
            "serial_time": float,
            "parallel_time": float,
            "speedup": float,
            "asset_count": int,
            "error": str (可选，无可评估资产时返回),
        }
    """
    quiet = log_callback or (lambda _: None)
    assets = load_all_assets()
    asset_list = [(n, c) for n, c in assets.items() if c]

    if not asset_list:
        return {"error": "无可评估资产"}

    # 串行基准
    engine = DarwinEvolution(provider=provider, config=config, log_callback=quiet)
    t0 = time.perf_counter()
    for name, content in asset_list:
        engine.score_asset(name, content)
    serial_time = time.perf_counter() - t0

    # 并行基准：复用 DarwinEvolution 内置并行能力
    parallel_config = (config or {}).copy()
    parallel_config["evolution_parallel_workers"] = len(asset_list)
    parallel_engine = DarwinEvolution(
        provider=provider, config=parallel_config, log_callback=quiet,
    )
    t0 = time.perf_counter()
    parallel_engine._run_baseline_parallel()
    parallel_time = time.perf_counter() - t0

    speedup = serial_time / parallel_time if parallel_time > 0 else 0

    return {
        "serial_time": round(serial_time, 3),
        "parallel_time": round(parallel_time, 3),
        "speedup": round(speedup, 2),
        "asset_count": len(asset_list),
    }


def _cmd_benchmark(args):
    result = benchmark_serial_vs_parallel(
        provider=None,
        config={"evolution_llm_client": _create_standalone_llm_client(log_callback=print)},
        log_callback=print,
    )
    if "error" in result:
        print(f"[benchmark] 错误: {result['error']}")
        return
    print("\n基准测试结果:")
    print(f"  资产数: {result['asset_count']}")
    print(f"  串行耗时: {result['serial_time']}s")
    print(f"  并行耗时: {result['parallel_time']}s")
    print(f"  加速比: {result['speedup']}x")


def main():
    parser = argparse.ArgumentParser(description="Darwin 进化系统")
    subparsers = parser.add_subparsers(dest="command", help="子命令")

    subparsers.add_parser("baseline", help="建立基线评分")

    evolve_parser = subparsers.add_parser("evolve", help="运行进化循环")
    evolve_parser.add_argument("--asset", type=str, default=None,
                               help="只进化指定资产")
    evolve_parser.add_argument("--max-iter", type=int, default=DEFAULT_MAX_ITERATIONS,
                               help=f"最大迭代次数（默认 {DEFAULT_MAX_ITERATIONS}）")

    subparsers.add_parser("report", help="查看评分报告")

    benchmark_parser = subparsers.add_parser("benchmark", help="串行 vs 并行评分性能对比")
    benchmark_parser.add_argument("--workers", type=int, default=None,
                                  help="并行线程数（默认：资产数）")

    args = parser.parse_args()

    if args.command == "baseline":
        _cmd_baseline(args)
    elif args.command == "evolve":
        _cmd_evolve(args)
    elif args.command == "report":
        _cmd_report(args)
    elif args.command == "benchmark":
        _cmd_benchmark(args)
    else:
        parser.print_help()


if __name__ == "__main__":
    main()