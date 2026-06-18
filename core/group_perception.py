"""云璃插件 - 群聊感知模块

从 main.py 中提取的群聊感知逻辑：
- 话题检测（分词映射 + 权重排序）
- 群氛围更新/查询（内存缓存滑动窗口）
- 场景信号提取（@、回复等）
"""

import re
import time
import logging
from collections import defaultdict
from typing import Dict, List, Optional

logger = logging.getLogger(__name__)


class GroupPerception:
    """群聊感知器

    负责话题检测、群氛围维护、场景信号提取。
    解耦 main.py 中的 ~400 行群聊感知逻辑。

    缓存保护：
    - _max_cached_groups: 全局缓存群数量上限，超限时淘汰最久未更新的群
    - _cache_ttl_seconds: 缓存条目 TTL，超时访问自动失效
    """

    # 话题关键词映射（扩展版，支持分词匹配）
    TOPIC_KEYWORDS = {
        "吃": "食物", "饭": "食物", "食物": "食物", "零食": "食物",
        "饿": "食物", "美食": "食物", "好吃": "食物", "火锅": "食物",
        "辣": "食物", "甜": "食物", "烧烤": "食物", "奶茶": "食物",
        "咖啡": "食物", "茶": "食物", "饮料": "食物", "蛋糕": "食物",
        "面包": "食物", "水果": "食物", "肉": "食物", "菜": "食物",
        "餐厅": "食物", "外卖": "食物", "做饭": "食物", "食谱": "食物",
        "剑": "剑", "刀": "剑", "武器": "剑", "锻": "剑",
        "老铁": "剑", "魔剑": "剑", "战斗": "剑", "打": "剑",
        "游戏": "游戏", "玩": "游戏", "开黑": "游戏", "排位": "游戏",
        "王者": "游戏", "原神": "游戏", "星铁": "游戏", "崩坏": "游戏",
        "工作": "工作", "上班": "工作", "加班": "工作", "老板": "工作",
        "同事": "工作", "项目": "工作", "开会": "工作",
        "学习": "学习", "考试": "学习", "作业": "学习", "复习": "学习",
        "挂科": "学习", "课程": "学习", "专业": "学习",
        "今天": "日常", "昨天": "日常", "明天": "日常", "周末": "日常",
        "开心": "情绪", "难过": "情绪", "累": "情绪", "烦": "情绪",
        "兴奋": "情绪", "无聊": "情绪", "生气": "情绪",
        "天气": "天气", "雨": "天气", "雪": "天气", "冷": "天气", "热": "天气",
        "手机": "科技", "电脑": "科技", "AI": "科技", "编程": "科技",
        "猫": "宠物", "狗": "宠物", "宠物": "宠物",
        "音乐": "音乐", "歌": "音乐", "唱歌": "音乐",
        "旅行": "旅行", "旅游": "旅行",
    }

    # 话题权重（用于群氛围统计）
    TOPIC_WEIGHTS = {
        "食物": 1.0, "剑": 1.0, "游戏": 1.0, "工作": 0.9,
        "学习": 0.9, "日常": 0.7, "情绪": 0.8, "天气": 0.6,
        "科技": 0.8, "宠物": 0.9, "音乐": 0.8, "旅行": 0.8,
    }

    def __init__(self, db, config: dict = None):
        self.db = db
        self.config = config or {}
        self._atmosphere_cache: Dict[str, Dict] = {}
        self._topic_thread_cache: Dict[str, list] = {}

        # 缓存保护参数
        self._max_cached_groups = self.config.get("max_cached_groups", 200)
        self._cache_ttl_seconds = self.config.get("cache_ttl_seconds", 3600)

    def _enforce_cache_limit(self, cache_dict: dict, group_id: str):
        """确保缓存字典不超过最大群数量，超限时淘汰最久未更新的条目

        同时清理过期条目（TTL 检查）。
        """
        # TTL 过期清理
        now = time.time()
        expired_keys = [
            gid for gid, val in cache_dict.items()
            if isinstance(val, dict) and (now - val.get("last_updated", 0)) > self._cache_ttl_seconds
        ]
        for gid in expired_keys:
            del cache_dict[gid]

        if group_id in cache_dict:
            return False

        if len(cache_dict) >= self._max_cached_groups:
            # 按最后更新时间排序，淘汰最旧的条目
            oldest = min(
                cache_dict.keys(),
                key=lambda gid: cache_dict[gid].get("last_updated", 0)
                if isinstance(cache_dict[gid], dict)
                else 0,
            )
            del cache_dict[oldest]
        return True

    # 单字关键词黑名单：这些单字在群聊中极易误匹配，仅在多字匹配未命中时作为兜底
    # 且需要满足最低出现次数阈值才生效
    SINGLE_CHAR_BLACKLIST = {"打", "菜", "肉", "茶", "玩", "冷", "热", "累", "烦"}

    def detect_topic(self, message: str) -> str:
        """检测当前话题（增强版分词匹配）

        1. 先尝试最长匹配（2字+关键词优先）
        2. 再尝试单字匹配（排除黑名单中的易误匹配单字）
        3. 返回得分最高的话题
        """
        if not message:
            return ""

        topic_scores = {}

        # 第一轮：多字匹配
        for keyword, topic in self.TOPIC_KEYWORDS.items():
            if len(keyword) >= 2 and keyword in message:
                topic_scores[topic] = topic_scores.get(topic, 0) + len(keyword)

        # 第二轮：单字匹配（仅对未匹配到时进行，排除黑名单单字）
        if not topic_scores:
            for keyword, topic in self.TOPIC_KEYWORDS.items():
                if len(keyword) == 1 and keyword not in self.SINGLE_CHAR_BLACKLIST and keyword in message:
                    topic_scores[topic] = topic_scores.get(topic, 0) + 1

        if not topic_scores:
            return ""

        return max(topic_scores.keys(), key=lambda t: topic_scores[t])

    # ========== 群氛围 ==========

    def update_atmosphere(self, group_id: str, message: str):
        """更新群氛围记忆（内存缓存，轻量统计）"""
        if not group_id or not message:
            return

        topic = self.detect_topic(message)
        if not topic:
            return

        # 缓存上限保护：新群时触发淘汰检查
        self._enforce_cache_limit(self._atmosphere_cache, group_id)

        if group_id not in self._atmosphere_cache:
            self._atmosphere_cache[group_id] = {
                "topics": {},
                "total_messages": 0,
                "last_updated": time.time(),
            }

        cache = self._atmosphere_cache[group_id]
        cache["topics"][topic] = cache["topics"].get(topic, 0) + 1
        cache["total_messages"] += 1
        cache["last_updated"] = time.time()

        # 只保留最近20个话题（滑动窗口）
        if len(cache["topics"]) > 20:
            min_topic = min(cache["topics"].keys(), key=lambda t: cache["topics"][t])
            del cache["topics"][min_topic]

    def get_atmosphere_text(self, group_id: str) -> str:
        """获取群氛围描述文本"""
        cache = self._atmosphere_cache.get(group_id)
        if not cache or cache["total_messages"] < 5:
            return ""

        sorted_topics = sorted(
            cache["topics"].items(), key=lambda x: x[1], reverse=True
        )[:3]

        if not sorted_topics:
            return ""

        topic_names = [t[0] for t in sorted_topics]
        if len(topic_names) == 1:
            return f"这个群最近常聊{topic_names[0]}相关的话题"
        else:
            return f"这个群最近常聊{'、'.join(topic_names)}相关的话题"

    # ========== 话题线程追踪 ==========

    def update_topic_threads(self, group_id: str, sender_id: str, sender_name: str,
                             message: str, ttl_minutes: int = 90, max_threads: int = 8):
        """更新群聊话题线程追踪"""
        if not group_id:
            return
        topic = self.detect_topic(message)
        if not topic:
            return

        # 缓存上限保护：新群时触发淘汰检查
        self._enforce_cache_limit(self._topic_thread_cache, group_id)

        now = time.time()
        ttl = ttl_minutes * 60
        if group_id not in self._topic_thread_cache:
            self._topic_thread_cache[group_id] = []
        threads = self._topic_thread_cache[group_id]
        threads.append((topic, sender_name, now))
        self._topic_thread_cache[group_id] = [
            t for t in threads if now - t[2] < ttl
        ][-max_threads:]

    def get_topic_threads_formatted(self, group_id: str) -> str:
        """格式化话题线程用于提示词（含参与者信息）"""
        if group_id not in self._topic_thread_cache:
            return ""
        threads = self._topic_thread_cache[group_id]
        if not threads:
            return ""
        from collections import Counter
        topic_counts = Counter(t[0] for t in threads)
        hot_topics = topic_counts.most_common(3)

        parts = []
        for topic, count in hot_topics:
            # 提取该话题的参与者
            participants = [t[1] for t in threads if t[0] == topic]
            unique_participants = list(dict.fromkeys(participants))[:3]  # 去重保序，最多3人
            if len(unique_participants) >= 2:
                parts.append(f"{topic}({count}条，{unique_participants[0]}和{unique_participants[1]}在聊)")
            elif len(unique_participants) == 1:
                parts.append(f"{topic}({count}条，{unique_participants[0]}在聊)")
            else:
                parts.append(f"{topic}({count}条)")

        return "当前活跃话题：" + "、".join(parts)

    def build_recent_speakers_summary(self, group_id: str, max_speakers: int = 5) -> str:
        """构建最近发言者摘要（谁在跟谁说话）

        利用话题线程缓存中的发言者信息，生成群聊对话结构感知。
        """
        if group_id not in self._topic_thread_cache:
            return ""
        threads = self._topic_thread_cache[group_id]
        if not threads:
            return ""

        # 取最近的发言记录
        recent = threads[-max_speakers:]
        if len(recent) < 2:
            return ""

        # 构建发言者序列
        speaker_sequence = []
        seen_speakers = []
        for topic, speaker, timestamp in recent:
            if speaker and speaker not in seen_speakers[-3:]:  # 避免同一人连续出现
                speaker_sequence.append(f"{speaker}在聊{topic}")
                seen_speakers.append(speaker)

        if not speaker_sequence:
            return ""

        return "最近群聊动态：" + "，".join(speaker_sequence[-3:])

    # ========== 场景信号提取 ==========

    @staticmethod
    def extract_scene_signals(event) -> dict:
        """提取群聊场景信号（@、回复、提及）"""
        signals = {
            "at_bot": False,
            "at_other": False,
            "at_target_name": "",
            "at_all": False,
            "reply_to_bot": False,
            "reply_to_other": False,
            "reply_target_name": "",
            "mention_bot_name": False,
            "bot_name": "云璃",
        }

        try:
            chain = getattr(event, "message_obj", None)
            components = chain.message if (chain and hasattr(chain, "message")) else []

            self_id = None
            for comp in components:
                class_name = comp.__class__.__name__.lower()

                if class_name == "at":
                    qq = str(getattr(comp, "qq", "") or "").strip()
                    name = getattr(comp, "name", "") or qq

                    if qq.lower() == "all":
                        signals["at_all"] = True
                    elif qq:
                        if self_id is None:
                            self_id = GroupPerception._try_get_self_id(event)
                        if self_id and qq == self_id:
                            signals["at_bot"] = True
                        else:
                            signals["at_other"] = True
                            signals["at_target_name"] = name or qq

                elif class_name == "reply":
                    sender_id = getattr(comp, "sender_id", None) or getattr(comp, "sender", None)
                    if sender_id:
                        if self_id is None:
                            self_id = GroupPerception._try_get_self_id(event)
                        if self_id and str(sender_id) == self_id:
                            signals["reply_to_bot"] = True
                        else:
                            signals["reply_to_other"] = True
                            signals["reply_target_name"] = str(sender_id)[:8]

            message_text = event.message_str or ""
            if "云璃" in message_text or "yunli" in message_text.lower():
                signals["mention_bot_name"] = True

        except Exception as e:
            logger.error("信号提取失败: %s", e)

        return signals

    @staticmethod
    def _try_get_self_id(event) -> str:
        """尝试获取机器人自身QQ号"""
        try:
            context = getattr(event, "_context", None) or getattr(event, "bot_context", None)
            if context:
                if hasattr(context, "get_self_id"):
                    return str(context.get_self_id())
                platform = getattr(context, "platform", None)
                if platform:
                    account = getattr(platform, "account", None)
                    if account:
                        return str(getattr(account, "u", "") or "")
        except Exception:
            pass
        return ""

    @staticmethod
    def format_scene_description(signals: dict) -> str:
        """将场景信号格式化为提示词文本"""
        lines = ["【群聊场景】"]

        if signals.get("at_bot"):
            lines.append("当前消息直接@了你，是在对你说话")
        elif signals.get("reply_to_bot"):
            lines.append("当前消息是回复你的，是在对你说话")
        elif signals.get("mention_bot_name"):
            lines.append(f"当前消息提到了你的名字（{signals.get('bot_name', '云璃')}），可能是在对你说话")
        elif signals.get("at_other"):
            target_name = signals.get("at_target_name", "某人")
            lines.append(f"当前消息@{target_name}，是在和别人说话，你是旁观者")
        elif signals.get("reply_to_other"):
            target_name = signals.get("reply_target_name", "某人")
            lines.append(f"当前消息回复{target_name}，是在和别人说话，你是旁观者")
        else:
            lines.append("当前消息没有特定对象，是群聊广播")

        lines.append(
            "群聊回复原则：被叫到或确实需要回应时再说；短一点，像群友接话；"
            "不要逐条总结群聊，不要当主持人，不要把每个话题都认真升格。"
        )

        return "\n".join(lines)