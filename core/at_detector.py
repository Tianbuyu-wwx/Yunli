"""云璃插件 - At 检测器

独立的消息 @ 检测模块，不依赖插件内部状态。
支持多种平台格式：QQ @、AstrBot At 组件、文本 [At:id] 等。
"""

from typing import Optional


class AtDetector:
    """At 检测器

    判断消息是否提到了机器人. 
    提供缓存 self_id 的能力，避免每次检测都重复查找。
    """

    def __init__(self):
        self._cached_self_id: Optional[str] = None

    def set_self_id(self, self_id: str):
        """设置缓存的机器人 ID"""
        self._cached_self_id = self_id

    def get_self_id(self) -> Optional[str]:
        return self._cached_self_id

    def is_at_me(self, event, override_self_id: str = None) -> bool:
        """检测消息是否提到了机器人

        分层检测：
        1. 快速早退：消息文本不含 @ 相关标记
        2. 框架方法 is_at_me()
        3. 遍历消息组件中的 At
        4. 文本匹配 [At:id]

        Args:
            event: AstrMessageEvent
            override_self_id: 可选，临时覆盖缓存的 self_id

        Returns:
            True → 消息提到了机器人
        """
        message = event.message_str or ""

        # 快速早退：不含 @ 相关标记
        if "@" not in message and "[At:" not in message and "At:" not in message:
            return False

        self_id = override_self_id or self._cached_self_id

        # 方法1：框架方法
        is_at_me_fn = getattr(event, 'is_at_me', None)
        if callable(is_at_me_fn) and is_at_me_fn():
            return True

        # 方法2：遍历消息组件
        try:
            chain = getattr(event, "message_obj", None)
            if chain and hasattr(chain, "message"):
                for comp in chain.message:
                    class_name = comp.__class__.__name__.lower()
                    if class_name == "at":
                        qq = str(getattr(comp, "qq", "") or "").strip()
                        if qq.lower() == "all":
                            return True
                        if self_id and qq == self_id:
                            return True
                        if not self_id:
                            return True
        except Exception:
            pass

        # 方法3：文本匹配
        if self_id and f"[At:{self_id}]" in message:
            return True

        return False