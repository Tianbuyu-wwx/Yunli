"""云璃插件 - 请求上下文

统一管理 on_llm_request → on_llm_response 完整生命周期状态。
替代 5 个散落的动态属性 (req, prompt_injected, debounce_buffered, debounce_merged, is_knowledge_query)，
提供类型安全的访问方式。
"""

from dataclasses import dataclass, field
from typing import Any, Optional


@dataclass
class RequestContext:
    """云璃请求上下文

    在 on_llm_request 中创建，附着在 event._yunli_ctx 上，
    在 on_llm_response 中读取。确保两个阶段状态一致且可追溯。

    注：短期对话线程追踪（last_user_message/last_yunli_response/thread_turn_count）
    已移除，统一由 ConversationThreadTracker 提供，避免状态冗余。
    """

    # LLM 请求引用
    req: Any

    # 来源信息
    group_id: str = ""
    user_id: str = ""
    user_nickname: str = ""
    scope: str = ""  # group_id:user_id 或 user_id

    # 生命周期标记
    is_prompt_injected: bool = False
    is_debounce_buffered: bool = False
    is_debounce_merged: bool = False
    is_knowledge_query: bool = False

    # 关系模式（由 _do_inject_persona_prompt 设置）
    rel_mode: str = "normal"