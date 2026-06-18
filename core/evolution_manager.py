"""云璃人格插件 - 进化系统管理器

负责 Darwin 进化系统 + Phase 2 模式发现：
    /云璃进化 baseline / evolve / report / status / apply / apply_dry_run
    /云璃进化 analyze / discoveries / accept / reject / rules / logstats

由 YunliPersonaPlugin.cmd_darwin 装饰器入口委派调用。

所有共享 state 通过 self.plugin 访问（_safe_create_task / _send_segmented /
_get_user_id / config / context / _get_log_collector / apply_asset_to_runtime 等）。
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import logging
import time
from pathlib import Path
from typing import List, Optional, TYPE_CHECKING

from astrbot.api.event import AstrMessageEvent
from astrbot.api.message_components import Plain
from astrbot.core.message.message_event_result import MessageChain

from ..evolution.darwin_evolve import DarwinEvolution, load_baseline
from ..evolution.log_collector import LogCollector
from ..evolution.pattern_discovery import PatternDiscovery
from ..evolution.rule_generator import RuleGenerator
from ..evolution.provider_factory import get_default_provider, describe_provider

from .logging_helpers import get_logger, bind_context

if TYPE_CHECKING:
    from ..main import YunliPersonaPlugin


logger = get_logger(__name__)


class YunliEvolutionManager:
    """Darwin 进化系统管理器

    入口方法（由主插件以装饰器方式调用）：
        - cmd_darwin(event): /云璃进化 子命令分发

    私有方法（仅内部使用）：
        - _get_darwin / _get_log_collector / _get_pattern_discovery / _get_rule_generator
        - _run_darwin_baseline / _run_darwin_evolve / _run_phase2_analyze
        - _check_darwin_auto_trigger / _send_text
    """

    def __init__(self, plugin: "YunliPersonaPlugin"):
        self.plugin = plugin

    # ========== 延迟初始化的子系统 ==========

    def _get_darwin(self) -> DarwinEvolution:
        """获取 Darwin 进化引擎（延迟初始化 provider，使用 AstrBot 默认 LLM）"""
        plugin = self.plugin
        if plugin._darwin is None:
            provider, source = get_default_provider(plugin.context, "[云璃进化]")
            logger.info(
                "初始化 DarwinEvolution，来源: %s, 详情: %s",
                source, describe_provider(plugin.context, provider),
                extra={"source": source},
            )
            plugin._darwin = DarwinEvolution(
                provider=provider,
                config=plugin.config,
                log_callback=lambda msg: logger.info(msg),
            )
        return plugin._darwin

    def _get_log_collector(self) -> LogCollector:
        plugin = self.plugin
        if plugin._log_collector is None:
            plugin._log_collector = LogCollector(
                sample_rate=plugin.config.get("phase2_log_sample_rate", 0.1),
                enabled=plugin.config.get("phase2_enabled", False),
            )
        return plugin._log_collector

    def _get_pattern_discovery(self) -> PatternDiscovery:
        plugin = self.plugin
        if plugin._pattern_discovery is None:
            provider, source = get_default_provider(plugin.context, "[云璃Phase2]")
            logger.info(
                "初始化 PatternDiscovery，来源: %s, 详情: %s",
                source, describe_provider(plugin.context, provider),
            )
            plugin._pattern_discovery = PatternDiscovery(
                provider=provider,
                log_collector=self._get_log_collector(),
                config=plugin.config,
                log_callback=lambda msg: logger.info(msg),
            )
        return plugin._pattern_discovery

    def _get_rule_generator(self) -> RuleGenerator:
        plugin = self.plugin
        if plugin._rule_generator is None:
            plugin._rule_generator = RuleGenerator(
                log_callback=lambda msg: logger.info(msg),
            )
        return plugin._rule_generator

    # ========== /云璃进化 命令入口 ==========

    async def cmd_darwin(self, event: AstrMessageEvent):
        """Darwin 进化系统命令"""
        # 指标：cmd_darwin 总耗时
        with self.plugin.metrics.measure("cmd_duration", tag="darwin"):
            async for result in self._cmd_darwin_impl(event):
                yield result

    async def _cmd_darwin_impl(self, event: AstrMessageEvent):
        """cmd_darwin 的实际实现（被 measure() 包裹以统计耗时）"""
        plugin = self.plugin
        message = event.message_str or ""

        # 解析子命令
        sub_cmd = (
            message.replace("/云璃进化", "").replace("云璃进化", "", 1).strip().lower()
        )
        # 指标：记录子命令触发（用于观察各子命令使用频率）
        plugin.metrics.increment("darwin_cmd_total", tag=sub_cmd or "help")
        # 结构化日志：绑定 Darwin 进化上下文
        sender_id = plugin._get_user_id(event)
        group_id = plugin._get_group_id(event)
        bind_context(
            scope=f"darwin:{sub_cmd or 'help'}:{group_id}:{sender_id}" if group_id or sender_id else f"darwin:{sub_cmd or 'help'}",
            user_id=sender_id or None,
            group_id=group_id or None,
        )

        # 检查是否启用
        if not plugin.config.get("evolution_enabled", False):
            yield event.plain_result(
                "[Darwin进化] 未启用。请在插件设置中开启 evolution_enabled。"
            )
            return

        # 检查白名单（QQ号权限控制）
        admin_qqs_str = plugin.config.get("evolution_admin_qqs", "").strip()
        if admin_qqs_str:
            admin_qqs = {qq.strip() for qq in admin_qqs_str.split(",") if qq.strip()}
            if sender_id not in admin_qqs:
                # 静默拒绝——不暴露命令存在性
                logger.warning(
                    "[Darwin进化] 非白名单用户 %s 尝试触发命令", sender_id,
                )
                return

        if sub_cmd in ("", "help", "帮助"):
            yield event.plain_result(
                "【Darwin 进化系统】\n"
                "/云璃进化 baseline — 建立基线评分\n"
                "/云璃进化 evolve [资产名] — 运行进化循环\n"
                "/云璃进化 report — 查看评分报告\n"
                "/云璃进化 status — 查看进化状态\n"
                "/云璃进化 apply — 应用进化资产到运行时\n"
                "/云璃进化 apply_dry_run — 预览 apply 后的 diff（不修改任何文件）\n"
                "/云璃进化 apply_status — 查看资产应用状态\n"
                "—— Phase 2 模式发现 ——\n"
                "/云璃进化 analyze — 分析日志发现模式\n"
                "/云璃进化 discoveries — 查看发现的模式\n"
                "/云璃进化 accept <id> — 接受模式并生成规则\n"
                "/云璃进化 reject <id> — 拒绝模式\n"
                "/云璃进化 rules — 查看规则提案\n"
                "/云璃进化 logstats — 查看日志采集统计"
            )
            return

        if sub_cmd == "baseline":
            yield event.plain_result("[Darwin进化] 开始建立基线，这可能需要几分钟…")
            plugin._safe_create_task(self._run_darwin_baseline(event))
            return

        if sub_cmd.startswith("evolve"):
            # 解析资产名：/云璃进化 evolve system_prompt
            parts = sub_cmd.split()
            target = parts[1] if len(parts) > 1 else None
            if target and target not in (
                "system_prompt", "review_rules", "filter_rules",
                "emotion_templates", "language_style",
            ):
                yield event.plain_result(f"[Darwin进化] 未知资产: {target}")
                return
            target_assets = [target] if target else None
            yield event.plain_result(
                f"[Darwin进化] 开始进化{'资产: ' + target if target else '全部资产'}，"
                f"这可能需要5-10分钟…"
            )
            plugin._safe_create_task(self._run_darwin_evolve(event, target_assets))
            return

        if sub_cmd == "report":
            try:
                darwin = self._get_darwin()
                report = darwin.run_report()
                # 分段发送（报告可能较长）
                async for result in plugin._send_segmented(event, report):
                    yield result
            except Exception as e:
                logger.exception("Darwin进化: 生成报告失败")
                yield event.plain_result(f"[Darwin进化] 生成报告失败: {e}")
            return

        if sub_cmd == "status":
            try:
                baseline = load_baseline()
                if baseline:
                    lines = ["[Darwin进化] 状态："]
                    for name, data in baseline.items():
                        lines.append(f"  {name}: {data['total_score']}/100")
                    yield event.plain_result("\n".join(lines))
                else:
                    yield event.plain_result(
                        "[Darwin进化] 状态：尚未建立基线，请先运行 /云璃进化 baseline"
                    )
            except Exception as e:
                logger.exception("Darwin进化: 读取状态失败")
                yield event.plain_result(f"[Darwin进化] 读取状态失败: {e}")
            return

        # ── Phase 2 命令 ──

        if sub_cmd == "analyze":
            if not plugin.config.get("phase2_enabled", False):
                yield event.plain_result(
                    "[Darwin进化] Phase2未启用。请在插件设置中开启 phase2_enabled。"
                )
                return
            yield event.plain_result("[Darwin进化] 开始分析对话日志，发现模式…")
            plugin._safe_create_task(self._run_phase2_analyze(event))
            return

        if sub_cmd == "rules":
            if not plugin.config.get("phase2_enabled", False):
                yield event.plain_result("[Darwin进化] Phase2未启用。")
                return
            try:
                report = self._get_rule_generator().get_report()
                async for result in plugin._send_segmented(event, report):
                    yield result
            except Exception as e:
                logger.exception("Darwin进化: 获取规则提案失败")
                yield event.plain_result(f"[Darwin进化] 获取规则提案失败: {e}")
            return

        if sub_cmd.startswith("accept"):
            parts = sub_cmd.split()
            if len(parts) < 2:
                yield event.plain_result(
                    "[Darwin进化] 请指定模式ID: /云璃进化 accept pat_xxx"
                )
                return
            pat_id = parts[1]
            try:
                self._get_pattern_discovery().accept(pat_id)
                # 生成规则提案
                patterns = self._get_pattern_discovery().load_all()
                accepted = [p for p in patterns if p.pattern_id == pat_id and p.accepted]
                if accepted:
                    self._get_rule_generator().generate(accepted)
                    yield event.plain_result(
                        f"[Darwin进化] 已接受模式 {pat_id}，并生成规则提案。"
                        f"使用 /云璃进化 rules 查看。"
                    )
                else:
                    yield event.plain_result(
                        f"[Darwin进化] 未找到模式 {pat_id}，请先运行 /云璃进化 analyze。"
                    )
            except Exception as e:
                logger.exception("Darwin进化: 接受模式失败")
                yield event.plain_result(f"[Darwin进化] 接受模式失败: {e}")
            return

        if sub_cmd.startswith("reject"):
            parts = sub_cmd.split()
            if len(parts) < 2:
                yield event.plain_result(
                    "[Darwin进化] 请指定模式ID: /云璃进化 reject pat_xxx"
                )
                return
            try:
                self._get_pattern_discovery().reject(parts[1])
                yield event.plain_result(f"[Darwin进化] 已拒绝模式 {parts[1]}。")
            except Exception as e:
                logger.exception("Darwin进化: 拒绝模式失败")
                yield event.plain_result(f"[Darwin进化] 拒绝模式失败: {e}")
            return

        if sub_cmd == "discoveries":
            if not plugin.config.get("phase2_enabled", False):
                yield event.plain_result("[Darwin进化] Phase2未启用。")
                return
            try:
                patterns = self._get_pattern_discovery().load_all()
                if not patterns:
                    yield event.plain_result(
                        "[Darwin进化] 暂无发现。请先运行 /云璃进化 analyze。"
                    )
                    return
                lines = [f"[Darwin进化] 共 {len(patterns)} 条发现："]
                for p in patterns:
                    status = (
                        "已接受" if p.accepted
                        else ("已拒绝" if p.reviewed else "待审核")
                    )
                    lines.append(
                        f"  [{p.severity}] [{status}] {p.category}: "
                        f"{p.description[:50]} (ID: {p.pattern_id})"
                    )
                yield event.plain_result("\n".join(lines))
            except Exception as e:
                logger.exception("Darwin进化: 读取发现列表失败")
                yield event.plain_result(f"[Darwin进化] 读取发现列表失败: {e}")
            return

        if sub_cmd == "logstats":
            if not plugin.config.get("phase2_enabled", False):
                yield event.plain_result("[Darwin进化] Phase2未启用。")
                return
            try:
                stats = self._get_log_collector().get_stats()
                yield event.plain_result(
                    f"[Darwin进化] 日志采集统计:\n"
                    f"  已采集: {stats['total_collected']} 条\n"
                    f"  当前文件: {stats['current_file']}\n"
                    f"  当前计数: {stats['current_count']}\n"
                    f"  采样率: {stats['sample_rate']}\n"
                    f"  日志文件数: {stats['log_files']}"
                )
            except Exception as e:
                logger.exception("Darwin进化: 获取日志统计失败")
                yield event.plain_result(f"[Darwin进化] 获取日志统计失败: {e}")
            return

        # ── apply: 应用进化后的资产到运行时 ──
        if sub_cmd == "apply":
            if not plugin.config.get("evolution_enabled", False):
                yield event.plain_result("[Darwin进化] 未启用。")
                return
            try:
                from ..evolution.asset_bridge import (
                    apply_asset_to_runtime,
                    get_apply_status,
                )
                plugin_dir = Path(__file__).resolve().parent
                status = get_apply_status(plugin_dir)
                if isinstance(status, dict) and "error" in status:
                    yield event.plain_result(f"[Darwin进化] {status['error']}")
                    return

                # 默认启用 runtime overlay —— 让 LLM 改进的资产在进程内立即生效
                results = []
                overlay_note = ""
                apply_count = 0
                fail_count = 0
                for asset_name, info in status.items():
                    if info["evolved"] and not info["applied"]:
                        ok, msg = apply_asset_to_runtime(
                            asset_name, plugin_dir, write_overlay=True,
                        )
                        results.append(f"{'[OK]' if ok else '[FAIL]'} {msg}")
                        if ok:
                            apply_count += 1
                            self.plugin.metrics.increment("darwin_apply_total", tag="success")
                        else:
                            fail_count += 1
                            self.plugin.metrics.increment("darwin_apply_total", tag="failure")
                    elif info["applied"]:
                        results.append(f"[SKIP] {asset_name}: 已应用，无需重复")
                self.plugin.metrics.timing("darwin_apply_duration_ms", apply_count * 100)

                unhooked = [
                    name for name, info in status.items()
                    if info.get("evolved")
                    and not info.get("runtime_hook")
                ]
                if unhooked:
                    overlay_note = (
                        "\n提示: 以下资产暂无运行时 hook，"
                        "overlay 已写入但需 reload 模块或重启才生效: "
                        + ", ".join(unhooked)
                    )

                if not results:
                    yield event.plain_result(
                        "[Darwin进化] 没有可应用的进化资产。请先运行 /云璃进化 baseline → evolve。"
                    )
                else:
                    yield event.plain_result(
                        "[Darwin进化] 应用结果:\n" + "\n".join(results) + overlay_note
                    )
            except Exception as e:
                logger.exception("Darwin进化: 应用失败")
                yield event.plain_result(f"[Darwin进化] 应用失败: {e}")
            return

        # ── apply_dry_run: 预览 apply 后的 diff ──
        if sub_cmd == "apply_dry_run":
            if not plugin.config.get("evolution_enabled", False):
                yield event.plain_result("[Darwin进化] 未启用。")
                return
            try:
                from ..evolution.asset_bridge import (
                    apply_evolved_assets,
                    get_apply_status,
                )
                plugin_dir = Path(__file__).resolve().parent
                status = get_apply_status(plugin_dir)
                if isinstance(status, dict) and "error" in status:
                    yield event.plain_result(f"[Darwin进化] {status['error']}")
                    return

                results = apply_evolved_assets(plugin_dir, dry_run=True)
                if not results:
                    yield event.plain_result("[Darwin进化] dry-run: 没有可预览的进化资产。")
                    return
                lines = ["[Darwin进化] dry-run 预览（不会修改任何文件）:"]
                for name, (ok, msg) in results.items():
                    lines.append(f"--- {name} ---")
                    lines.append(msg if ok else f"[ERROR] {msg}")
                yield event.plain_result("\n".join(lines))
            except Exception as e:
                logger.exception("Darwin进化: dry-run 失败")
                yield event.plain_result(f"[Darwin进化] dry-run 失败: {e}")
            return

        if sub_cmd == "apply_status":
            try:
                from ..evolution.asset_bridge import (
                    get_apply_status,
                    get_evolved_asset_score,
                )
                plugin_dir = Path(__file__).resolve().parent
                status = get_apply_status(plugin_dir)
                if isinstance(status, dict) and "error" in status:
                    yield event.plain_result(f"[Darwin进化] {status['error']}")
                    return
                lines = ["[Darwin进化] 资产应用状态:"]
                for asset_name, info in status.items():
                    applied = "[已应用]" if info["applied"] else "[未应用]"
                    score = info.get("score", "N/A")
                    lines.append(
                        f"  {applied} {asset_name} ({info['description']}) 评分:{score}"
                    )
                    lines.append(f"    目标: {info['target']}")
                yield event.plain_result("\n".join(lines))
            except Exception as e:
                logger.exception("Darwin进化: 查询状态失败")
                yield event.plain_result(f"[Darwin进化] 查询状态失败: {e}")
            return

        # ── 未识别命令 → 显示帮助 ──
        yield event.plain_result(
            f"[Darwin进化] 未知命令: {sub_cmd}。发送 /云璃进化 help 查看可用命令。"
        )

    # ========== 后台运行函数 ==========

    async def _send_text(self, event: AstrMessageEvent, text: str):
        """安全发送文本到事件（处理 v4.25.2 的 MessageChain 要求）

        在后台任务中，`event.send(string)` 会触发 `'str' object has no attribute 'chain'` 错误，
        因为 AstrBot v4.25.2 的 event.send 期望 MessageChain 对象。
        本方法将纯文本包装为 MessageChain 后再发送，并捕获所有异常避免后台任务崩溃。
        """
        if not text or not text.strip():
            return
        try:
            chain = MessageChain([Plain(text)])
            await event.send(chain)
        except Exception:
            logger.exception("Darwin进化: 发送消息失败")

    async def _run_phase2_analyze(self, event: AstrMessageEvent):
        """后台运行 Phase 2 模式分析"""
        discovery = self._get_pattern_discovery()

        def _run():
            return discovery.discover()

        try:
            loop = asyncio.get_running_loop()
            with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
                patterns = await loop.run_in_executor(pool, _run)

            if not patterns:
                await self._send_text(event, "[Darwin进化] 分析完成，未发现新问题。")
                return

            lines = [f"[Darwin进化] 分析完成，发现 {len(patterns)} 条模式："]
            for p in patterns:
                lines.append(
                    f"  [{p.severity}] "
                    f"{discovery.CATEGORY_NAMES.get(p.category, p.category)}: "
                    f"{p.description[:60]}"
                )
            lines.append(f"\n使用 /云璃进化 discoveries 查看详情")
            lines.append(f"使用 /云璃进化 accept <id> 接受或 /云璃进化 reject <id> 拒绝")
            await self._send_text(event, "\n".join(lines))
        except Exception as e:
            logger.exception("Darwin进化: 分析失败")
            await self._send_text(event, f"[Darwin进化] 分析失败: {e}")

    async def _run_darwin_baseline(self, event: AstrMessageEvent):
        """后台运行基线评估"""
        darwin = self._get_darwin()
        self.plugin.metrics.increment("darwin_baseline_total")

        def _run():
            return darwin.run_baseline()

        try:
            loop = asyncio.get_running_loop()
            with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
                baseline = await loop.run_in_executor(pool, _run)

            lines = ["[Darwin进化] 基线评估完成："]
            for name, data in baseline.items():
                lines.append(f"  {name}: {data['total_score']}/100")
            await self._send_text(event, "\n".join(lines))
        except Exception as e:
            logger.exception("Darwin进化: 基线评估失败")
            await self._send_text(event, f"[Darwin进化] 基线评估失败: {e}")

    async def _run_darwin_evolve(
        self, event: AstrMessageEvent, target_assets: Optional[List[str]] = None
    ):
        """后台运行进化循环"""
        darwin = self._get_darwin()
        self.plugin.metrics.increment("darwin_evolve_total", tag=target_assets[0] if target_assets else "all")

        def _run():
            return darwin.run_evolve(target_assets=target_assets)

        try:
            loop = asyncio.get_running_loop()
            with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
                results = await loop.run_in_executor(pool, _run)

            lines = ["[Darwin进化] 进化循环完成："]
            for name, result in results.items():
                diff = result["final_score"] - result["baseline_score"]
                status = "[OK]" if result["improved"] else "[--]"
                lines.append(
                    f"  {status} {name}: {result['baseline_score']:.1f} -> "
                    f"{result['final_score']:.1f} ({diff:+.1f})"
                )
            lines.append(
                "\n注意：进化结果已保存到 evolution/assets/，需手动审查后写回源代码。"
            )
            await self._send_text(event, "\n".join(lines))
        except Exception as e:
            asset_extra = {"asset": (target_assets[0] if target_assets else "all")}
            logger.exception("Darwin进化: 进化循环失败", extra=asset_extra)
            await self._send_text(event, f"[Darwin进化] 进化循环失败: {e}")

    async def _check_darwin_auto_trigger(self):
        """检查是否需要自动触发进化"""
        plugin = self.plugin
        if not plugin.config.get("evolution_enabled", False):
            return
        if not plugin.config.get("evolution_auto_trigger", False):
            return

        now = time.time()
        interval = plugin.config.get("evolution_trigger_interval_hours", 24) * 3600
        if now - plugin._darwin_last_trigger_time < interval:
            return

        plugin._darwin_last_trigger_time = now
        logger.info("自动触发进化循环")

        darwin = self._get_darwin()

        try:
            loop = asyncio.get_running_loop()
            with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
                await loop.run_in_executor(pool, darwin.run_evolve)
            logger.info("自动进化循环完成")
        except Exception:
            logger.exception("自动进化失败", extra={"trigger": "auto"})
