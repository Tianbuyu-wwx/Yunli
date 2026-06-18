"""结构化日志辅助 — 无第三方依赖，基于 stdlib contextvars + logging

提供轻量级结构化日志能力：
  - ContextFilter: 把 contextvars 中的 scope/user_id/group_id 注入到 LogRecord
  - bind_context / reset_bind: 在异步任务入口绑定日志上下文（per-task）
  - get_logger: 获取带 ContextFilter 的 logger，确保每条 record 自动带结构化字段

设计原则：
  - 不引入第三方依赖（无 structlog / loguru）
  - 最小化对现有 logger.xxx() 调用的改动（不重写，仅注入 filter）
  - 字段通过 LogRecord 属性传递，formatter 可选地引用 record.scope 等
  - contextvar 自动随 asyncio 任务隔离，不影响并发安全
"""
from __future__ import annotations

import contextvars
import logging
from typing import Any, Dict, List, Optional, Tuple, Union

# 三个 contextvar 持有当前请求的上下文信息（按 asyncio 任务自动隔离）
_scope_ctx: contextvars.ContextVar[Optional[str]] = contextvars.ContextVar(
    "yunli_scope", default=None
)
_user_ctx: contextvars.ContextVar[Optional[str]] = contextvars.ContextVar(
    "yunli_user_id", default=None
)
_group_ctx: contextvars.ContextVar[Optional[str]] = contextvars.ContextVar(
    "yunli_group_id", default=None
)


class ContextFilter(logging.Filter):
    """把 contextvars 中的 scope/user_id/group_id 注入到 LogRecord

    使用方式：在 logger 上 addFilter(this) 后，每条日志的 record
    都会自动获得 record.scope / record.user_id / record.group_id 三个属性，
    formatter 中可直接引用，例如：
        fmt = "%(asctime)s [%(scope)s] [%(user_id)s] %(message)s"
    """

    def filter(self, record: logging.LogRecord) -> bool:
        # 缺省值用 "-" 便于日志格式对齐与过滤（区别于 None）
        record.scope = _scope_ctx.get() or "-"
        record.user_id = _user_ctx.get() or "-"
        record.group_id = _group_ctx.get() or "-"
        return True


# bind_context 返回的 token 列表项类型： (key, Token)
_TokenEntry = Tuple[str, contextvars.Token]


def bind_context(
    *,
    scope: Optional[str] = None,
    user_id: Optional[str] = None,
    group_id: Optional[str] = None,
) -> List[_TokenEntry]:
    """绑定当前异步任务的日志上下文。返回 token 列表用于 reset_bind。

    只绑定传入的非 None 字段；未传字段保持 contextvars 中的原值不变。
    应在异步任务入口调用，并在退出前调用 reset_bind 恢复。
    """
    tokens: List[_TokenEntry] = []
    if scope is not None:
        tokens.append(("scope", _scope_ctx.set(scope)))
    if user_id is not None:
        tokens.append(("user_id", _user_ctx.set(user_id)))
    if group_id is not None:
        tokens.append(("group_id", _group_ctx.set(group_id)))
    return tokens


def reset_bind(tokens: Optional[List[_TokenEntry]]) -> None:
    """恢复 bind_context 之前的值（按 LIFO 逆序 reset）"""
    if not tokens:
        return
    for key, token in reversed(tokens):
        if key == "scope":
            _scope_ctx.reset(token)
        elif key == "user_id":
            _user_ctx.reset(token)
        elif key == "group_id":
            _group_ctx.reset(token)


def get_logger(name: str) -> logging.Logger:
    """获取带 ContextFilter 的 logger，确保每个 record 含 scope/user_id/group_id

    同一 logger 多次调用安全：仅在尚未挂载 ContextFilter 时才 addFilter。
    """
    logger = logging.getLogger(name)
    if not any(isinstance(f, ContextFilter) for f in logger.filters):
        logger.addFilter(ContextFilter())
    return logger


__all__ = [
    "ContextFilter",
    "bind_context",
    "reset_bind",
    "get_logger",
]
