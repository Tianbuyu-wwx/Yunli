"""AstrBot LLM Provider 工厂

统一封装"获取默认 LLM provider"的多种方式，确保在 AstrBot v4.25.2 的不同
配置场景下都能拿到一个可用的 provider。

获取顺序（按优先级）：
1. context.get_provider() / context.get_using_provider() — 标准 API
2. context.provider_manager.{get_default_provider, get_using_provider, get_provider} — 旧版本兼容
3. context.llm_providers / context.providers — 字典/列表形式
4. context.provider_cfg / context.default_provider — 配置直查

回退行为：
- 在 AstrBot 命令路径下，provider 必然可用（如果用户配置了 LLM 的话）
- 如果所有方式都拿不到，会记录详细诊断日志，并返回 None
  此时 DarwinEvolution / PatternDiscovery 仍可工作（HTTP fallback / 模拟评分）
"""

import logging
from typing import Any, List, Optional, Tuple

logger = logging.getLogger(__name__)


def get_default_provider(context, log_prefix: str = "[云璃进化]") -> Tuple[Optional[Any], str]:
    """尝试多种方式获取 AstrBot 默认 LLM provider

    Args:
        context: AstrBot Context 对象
        log_prefix: 日志前缀（区分 Darwin / Phase2）

    Returns:
        (provider, source_str)
        - provider: 找到的 provider，未找到时为 None
        - source_str: 描述获取路径的字符串（用于诊断日志）
    """
    if context is None:
        return None, "context is None"

    # 方式 1: context.get_provider() / get_using_provider() — 标准 API
    for method_name in ("get_using_provider", "get_provider", "get_default_provider"):
        method = getattr(context, method_name, None)
        if method is None or not callable(method):
            continue
        try:
            provider = method()
            if provider is not None:
                src = f"context.{method_name}()"
                logger.info(f"{log_prefix} 使用 {src} 获取成功")
                return provider, src
        except Exception as e:
            logger.error(f"{log_prefix} {method_name}() 失败: {e}")

    # 方式 2: context.provider_manager.* — 旧版本兼容
    pm = getattr(context, "provider_manager", None)
    if pm is not None:
        for method_name in ("get_using_provider", "get_default_provider", "get_provider", "get_first_provider"):
            method = getattr(pm, method_name, None)
            if method is None or not callable(method):
                continue
            try:
                provider = method()
                if provider is not None:
                    src = f"context.provider_manager.{method_name}()"
                    logger.info(f"{log_prefix} 使用 {src} 获取成功")
                    return provider, src
            except Exception as e:
                logger.error(f"{log_prefix} provider_manager.{method_name}() 失败: {e}")

    # 方式 3: context.llm_providers / context.providers — 字典/列表形式
    for attr in ("llm_providers", "providers", "provider_map"):
        providers = getattr(context, attr, None)
        if not providers:
            continue
        if isinstance(providers, dict) and providers:
            # 优先选 default_provider_id 对应的，否则取第一个
            default_id = getattr(context, "default_provider_id", None) or getattr(context, "using_provider_id", None)
            if default_id and default_id in providers:
                provider = providers[default_id]
                src = f"context.{attr}['{default_id}']"
                logger.info(f"{log_prefix} 使用 {src} 获取成功")
                return provider, src
            # 取第一个非 None
            for k, v in providers.items():
                if v is not None:
                    src = f"context.{attr}['{k}']"
                    logger.info(f"{log_prefix} 使用 {src} 获取成功")
                    return v, src
        elif isinstance(providers, (list, tuple)) and providers:
            for i, p in enumerate(providers):
                if p is not None:
                    src = f"context.{attr}[{i}]"
                    logger.info(f"{log_prefix} 使用 {src} 获取成功")
                    return p, src
        # providers 本身可能就是一个 provider 实例
        elif providers is not None and not isinstance(providers, (dict, list, tuple)):
            src = f"context.{attr}"
            logger.info(f"{log_prefix} 使用 {src} 获取成功")
            return providers, src

    # 方式 4: 配置直查
    cfg = getattr(context, "provider_cfg", None) or getattr(context, "default_provider", None)
    if cfg is not None and hasattr(cfg, "text_chat"):
        src = "context.provider_cfg"
        logger.info(f"{log_prefix} 使用 {src} 获取成功")
        return cfg, src

    # 全部失败
    logger.warning(f"{log_prefix} 警告: 无法获取任何 LLM provider")
    logger.warning(f"{log_prefix}         请检查 AstrBot 设置 → 模型服务 → 是否已配置 LLM provider")
    logger.debug(f"{log_prefix}         Context 可用属性: {[a for a in dir(context) if not a.startswith('_')][:20]}")
    return None, "not found"


# 可能包含敏感信息的属性名，打印时脱敏处理
_SENSITIVE_ATTRS = frozenset({
    "api_key", "apikey", "key", "secret", "token",
    "api_secret", "access_token", "auth_token",
    "password", "credential", "private_key",
})


def _mask_sensitive(value) -> str:
    """对可能敏感的值进行脱敏"""
    s = str(value)
    if len(s) <= 6:
        return f"{s[:2]}***"
    return f"{s[:4]}***{s[-2:]}"


def describe_provider(context, provider) -> str:
    """生成 provider 的可读描述（用于诊断日志）

    安全：对可能包含 API Key 的属性值进行脱敏处理。
    """
    if provider is None:
        return "None"
    info = []
    info.append(f"type={type(provider).__name__}")
    for attr in ("provider_name", "name", "model_name", "model", "provider_id"):
        try:
            v = getattr(provider, attr, None)
            if v:
                # 脱敏：属性名包含敏感关键词时只显示掩码
                if attr.lower() in _SENSITIVE_ATTRS:
                    info.append(f"{attr}={_mask_sensitive(v)}")
                else:
                    info.append(f"{attr}={v}")
        except Exception:
            pass
    if hasattr(provider, "text_chat"):
        info.append("has_text_chat=True")
    return ", ".join(info)
