"""云璃插件 - 上下文构建模块

从 main.py 中提取的上下文构建逻辑：
- 环境感知（时间/节假日/农历/节气）
- 群聊上下文（活跃话题 + 群氛围）
- 关系上下文（用户关系 + 记忆）
- 知识相关的记忆召回
"""

import random
import re
from datetime import datetime, timedelta
from typing import Dict, List, Optional

from .utils import estimate_tokens


class ContextBuilder:
    """上下文构建器

    负责构建注入 LLM 系统提示词的各类上下文信息。
    解耦 main.py 中 ~400 行的上下文构建逻辑。
    """

    def __init__(self, db, persona_engine, relationship, config: dict = None):
        self.db = db
        self.persona_engine = persona_engine
        self.relationship = relationship
        self.config = config or {}

        # 环境感知配置
        self._enable_environment = self.config.get("enable_environment_perception", True)
        self._enable_holiday = self.config.get("enable_holiday_perception", True)
        self._enable_lunar = self.config.get("enable_lunar_perception", True)
        self._enable_solar_term = self.config.get("enable_solar_term_perception", True)
        self._timezone = self.config.get("environment_perception_timezone", "Asia/Shanghai")

        # 尝试导入农历/节气库（可选依赖）
        try:
            import chinese_calendar as calendar_cn
            self._calendar_cn = calendar_cn
        except Exception:
            self._calendar_cn = None

        try:
            from lunarcalendar import Converter, Solar
            self._Converter = Converter
            self._Solar = Solar
        except Exception:
            self._Converter = None
            self._Solar = None

    # ========== 环境感知 ==========

    def format_environment_perception(self) -> str:
        """格式化环境感知信息（时间/节假日/农历/节气）"""
        if not self._enable_environment:
            return ""

        current = self._environment_now()
        lines = [
            "【环境感知】",
            f"当前时间：{current.strftime('%Y-%m-%d %H:%M')} 周{'一二三四五六日'[current.weekday()]}",
        ]

        holiday = self._format_holiday(current)
        if holiday:
            lines.append(f"日期语境：{holiday}")

        lunar = self._format_lunar(current)
        if lunar:
            lines.append(f"农历：{lunar}")

        solar_term = self._format_solar_term(current)
        if solar_term:
            lines.append(f"节气：{solar_term}")

        return "\n".join(lines)

    def _environment_now(self) -> datetime:
        import zoneinfo
        tz_name = self._timezone or "Asia/Shanghai"
        try:
            return datetime.now(zoneinfo.ZoneInfo(tz_name))
        except Exception:
            return datetime.now()

    def _format_holiday(self, current: datetime) -> str:
        if not self._enable_holiday:
            return ""

        weekday = "一二三四五六日"[current.weekday()]
        parts = [f"周{weekday}"]

        if self._calendar_cn is not None:
            try:
                if self._calendar_cn.is_holiday(current.date()):
                    name = self._calendar_cn.get_holiday_detail(current.date())[1]
                    parts.append(name or "节假日")
                elif self._calendar_cn.is_workday(current.date()):
                    parts.append("工作日")
                else:
                    parts.append("休息日")
            except Exception:
                parts.append("周末" if current.weekday() >= 5 else "工作日")
        else:
            parts.append("周末" if current.weekday() >= 5 else "工作日")

        hour = current.hour
        if 5 <= hour < 11:
            parts.append("上午")
        elif 11 <= hour < 14:
            parts.append("中午")
        elif 14 <= hour < 18:
            parts.append("下午")
        elif 18 <= hour < 22:
            parts.append("晚上")
        else:
            parts.append("深夜")

        return "、".join(parts)

    def _format_lunar(self, current: datetime) -> str:
        if not self._enable_lunar or self._Converter is None or self._Solar is None:
            return ""

        try:
            lunar = self._Converter.Solar2Lunar(self._Solar(current.year, current.month, current.day))
            month = int(getattr(lunar, "month", 1))
            day = int(getattr(lunar, "day", 1))
            is_leap = bool(getattr(lunar, "isleap", False))

            _LUNAR_MONTH = ["正", "二", "三", "四", "五", "六", "七", "八", "九", "十", "冬", "腊"]
            _LUNAR_DAY = [
                "初一", "初二", "初三", "初四", "初五", "初六", "初七", "初八", "初九", "初十",
                "十一", "十二", "十三", "十四", "十五", "十六", "十七", "十八", "十九", "二十",
                "廿一", "廿二", "廿三", "廿四", "廿五", "廿六", "廿七", "廿八", "廿九", "三十",
            ]

            month_name = _LUNAR_MONTH[max(0, min(11, month - 1))]
            day_name = _LUNAR_DAY[max(0, min(29, day - 1))]
            leap_str = "闰" if is_leap else ""

            return f"{leap_str}{month_name}月{day_name}"
        except Exception:
            return ""

    def _format_solar_term(self, current: datetime) -> str:
        if not self._enable_solar_term:
            return ""

        _SOLAR_TERMS = {
            (2, 4): "立春", (2, 19): "雨水", (3, 6): "惊蛰", (3, 21): "春分",
            (4, 5): "清明", (4, 20): "谷雨", (5, 6): "立夏", (5, 21): "小满",
            (6, 6): "芒种", (6, 21): "夏至", (7, 7): "小暑", (7, 23): "大暑",
            (8, 8): "立秋", (8, 23): "处暑", (9, 8): "白露", (9, 23): "秋分",
            (10, 8): "寒露", (10, 23): "霜降", (11, 7): "立冬", (11, 22): "小雪",
            (12, 7): "大雪", (12, 22): "冬至", (1, 6): "小寒", (1, 20): "大寒",
        }

        today = (current.month, current.day)
        if today in _SOLAR_TERMS:
            return _SOLAR_TERMS[today]

        current_date = current.date()
        for offset in range(1, 5):
            next_day = current_date + timedelta(days=offset)
            name = _SOLAR_TERMS.get((next_day.month, next_day.day))
            if name:
                return f"{offset}天后{name}"

        return ""

    # ========== 群聊上下文 ==========

    def build_chat_context(
        self, group_id: str, atmosphere_text: str = "", token_budget: int = 200
    ) -> str:
        """构建群聊上下文（Token预算控制版）"""
        if not group_id:
            return ""

        context_parts = []
        used_tokens = 0

        # 获取活跃话题
        active_topic = self.db.get_active_topic(group_id)

        # 智能合并活跃话题 + 群氛围
        if active_topic:
            topic_name = active_topic.get("topic", "")
            msg_count = active_topic.get("message_count", 0)

            atmosphere_topics = []
            if atmosphere_text and "常聊" in atmosphere_text:
                match = re.search(r"常聊(.+?)相关的话题", atmosphere_text)
                if match:
                    atmosphere_topics = [t.strip() for t in match.group(1).split("、")]

            if topic_name in atmosphere_topics:
                other_topics = [t for t in atmosphere_topics if t != topic_name]
                if other_topics:
                    merged = f"【群聊氛围】大家在聊{topic_name}（{msg_count}条消息），这个群最近也常聊{'、'.join(other_topics)}"
                else:
                    merged = f"【群聊氛围】大家在聊{topic_name}（{msg_count}条消息）"
                context_parts.append(merged)
                used_tokens += estimate_tokens(merged)
            else:
                topic_text = f"【当前话题】大家在聊{topic_name}（{msg_count}条消息）"
                context_parts.append(topic_text)
                used_tokens += estimate_tokens(topic_text)

                if used_tokens < token_budget - 30 and atmosphere_text:
                    context_parts.append(atmosphere_text)
                    used_tokens += estimate_tokens(atmosphere_text)

        elif atmosphere_text:
            context_parts.append(atmosphere_text)
            used_tokens += estimate_tokens(atmosphere_text)

        # 群聊摘要（仅在无活跃话题时添加）
        if not active_topic and used_tokens < token_budget - 50:
            latest_summary = self.db.get_latest_summary(group_id)
            if latest_summary:
                summary = latest_summary.get("summary", "")
                if summary:
                    max_summary_len = (token_budget - used_tokens - 10) * 2
                    if len(summary) > max_summary_len:
                        summary = summary[:max_summary_len] + "…"
                    context_parts.append(f"【之前聊过】{summary}")

        return "\n\n".join(context_parts) if context_parts else ""

    # ========== 关系上下文 ==========

    def get_user_relationship(self, group_id: str, user_id: str) -> str:
        """获取用户关系等级"""
        stats = self.db.get_user_stats(group_id, user_id)
        total = stats.get("total", 0)
        if total >= 50:
            return "老朋友"
        elif total >= 20:
            return "熟人"
        elif total >= 5:
            return "认识"
        else:
            return "陌生人"

    def add_relationship_context(
        self, group_id: str, user_id: str, user_nickname: str, message: str = "",
        token_budget: int = 150
    ) -> str:
        """添加关系上下文到提示词"""
        if not group_id or not user_id:
            return ""

        relationship = self.get_user_relationship(group_id, user_id)
        emotion_trend = self.db.get_user_emotion_trend(group_id, user_id)

        context_parts = []
        used_tokens = 0

        # 1. 关系描述
        rel_descriptions = {
            "老朋友": f"你和{user_nickname}很熟了，经常聊天",
            "熟人": f"你和{user_nickname}聊过几次，算是认识",
            "认识": f"你记得{user_nickname}，但还不算太熟",
            "陌生人": f"{user_nickname}好像是第一次跟你说话",
        }
        if relationship in rel_descriptions:
            rel_text = rel_descriptions[relationship]
            context_parts.append(rel_text)
            used_tokens += estimate_tokens(rel_text)

        # 2. 情绪趋势
        if used_tokens < token_budget - 30:
            emotion_descriptions = {
                "annoyed": f"{user_nickname}最近好像心情不太好",
                "excited": f"{user_nickname}最近挺活跃的",
                "happy": f"{user_nickname}最近心情不错",
                "sad_guarded": f"{user_nickname}最近有点低落",
            }
            if emotion_trend in emotion_descriptions:
                context_parts.append(emotion_descriptions[emotion_trend])
                used_tokens += 10

        # 3. 用户记忆
        remaining_budget = token_budget - used_tokens
        if remaining_budget > 40:
            mem_limit = min(3, max(1, remaining_budget // 40))
            important_memories = self.get_relevant_memories(
                group_id, user_id, message, min_confidence=7, limit=mem_limit
            )
            if important_memories:
                memory_lines = self.build_natural_memory_text(
                    user_nickname, important_memories, current_user_id=user_id
                )
                if memory_lines:
                    context_parts.extend(memory_lines)

        return "\n" + "\n".join(context_parts) if context_parts else ""

    def get_relevant_memories(
        self, group_id: str, user_id: str, message: str,
        min_confidence: int = 7, limit: int = 3,
        include_group_memories: bool = True
    ) -> List[Dict]:
        """获取与当前消息相关的记忆（相关性动态召回）"""
        candidates = self.db.get_important_memories(
            group_id, user_id, min_confidence=min_confidence, limit=limit * 3
        )

        group_candidates = []
        if include_group_memories and group_id:
            group_candidates = self.db.get_group_memories(
                group_id, min_confidence=min_confidence, limit=limit * 2,
                exclude_user_id=user_id
            )

        # 提取消息关键词
        message_keywords = set()
        topic_keywords_map = {
            "food": ["吃", "饭", "食物", "零食", "饿", "美食", "好吃", "火锅", "辣", "甜", "烧烤", "奶茶"],
            "sword": ["剑", "刀", "武器", "锻", "老铁", "魔剑", "战斗", "打"],
            "game": ["游戏", "玩", "打", "开黑", "排位", "王者", "原神", "星铁"],
            "work": ["工作", "上班", "加班", "老板", "同事", "项目", "开会"],
            "study": ["学习", "考试", "作业", "复习", "挂科", "课程", "专业"],
            "daily": ["今天", "昨天", "明天", "周末", "最近", "早上", "晚上"],
            "emotion": ["开心", "难过", "累", "烦", "兴奋", "无聊", "郁闷"],
        }
        for keywords in topic_keywords_map.values():
            if any(kw in message for kw in keywords):
                message_keywords.update(keywords)

        # 评分
        def _score_memory(mem, is_group_memory=False):
            content = mem.get("content", "")
            score = mem.get("confidence", 5) * 2
            if message_keywords:
                overlap = sum(1 for kw in message_keywords if kw in content)
                score += overlap * 10

            created_at = mem.get("created_at", "")
            if created_at:
                try:
                    mem_time = datetime.strptime(created_at, "%Y-%m-%d %H:%M:%S")
                    days_old = (datetime.now() - mem_time).days
                    if days_old < 7:
                        score += 5
                    elif days_old < 30:
                        score += 2
                except Exception:
                    pass

            score += mem.get("access_count", 1)
            if is_group_memory:
                score *= 0.7
            return score

        scored = []
        for mem in candidates:
            scored.append((_score_memory(mem, False), mem))
        for mem in group_candidates:
            scored.append((_score_memory(mem, True), mem))

        scored.sort(key=lambda x: x[0], reverse=True)

        selected = []
        for _, mem in scored[:limit]:
            self.db.access_memory(mem["id"])
            selected.append(mem)

        return selected

    # ========== 记忆文本构建 ==========

    @staticmethod
    def is_memory_fuzzy(access_count: int, created_at: str) -> bool:
        """判断记忆是否应该被模糊化（模拟遗忘）"""
        if access_count >= 5:
            return False
        if not created_at:
            return False
        try:
            mem_time = datetime.strptime(created_at, "%Y-%m-%d %H:%M:%S")
            days_old = (datetime.now() - mem_time).days
        except Exception:
            return False

        if access_count <= 1 and days_old > 7:
            return random.random() < 0.5
        elif access_count <= 2 and days_old > 30:
            return random.random() < 0.3
        return False

    def build_natural_memory_text(
        self, user_nickname: str, memories: List[Dict], current_user_id: str = ""
    ) -> List[str]:
        """将记忆转换为自然语言描述"""
        lines = []
        own_memories = {"preference": [], "fact": [], "event": []}
        group_memories = {}

        for mem in memories:
            mem_type = mem.get("memory_type", "")
            content = mem.get("content", "")
            mem_user_id = mem.get("user_id", "")
            mem_user_nickname = mem.get("user_nickname", "")
            if mem_user_nickname:
                mem_display_name = mem_user_nickname
            else:
                mem_display_name = f"群友{mem_user_id[:4]}" if mem_user_id else "群友"

            if len(content) > 15:
                content = content[:15] + "…"

            is_fuzzy = self.is_memory_fuzzy(
                mem.get("access_count", 1), mem.get("created_at", "")
            )
            item = (content, is_fuzzy, mem_display_name)

            if not current_user_id or not mem_user_id or mem_user_id == current_user_id:
                target = own_memories
            else:
                if mem_display_name not in group_memories:
                    group_memories[mem_display_name] = {"preference": [], "fact": [], "event": []}
                target = group_memories[mem_display_name]

            if mem_type == "preference":
                target["preference"].append(item)
            elif mem_type == "fact":
                target["fact"].append(item)
            elif mem_type == "event":
                target["event"].append(item)

        lines.extend(self._build_memory_lines_for_user(user_nickname, own_memories, is_own=True))

        if group_memories:
            def _calc_score(mem_dict):
                score = 0
                for mem_type, items in mem_dict.items():
                    type_weights = {"preference": 3, "fact": 2, "event": 1}
                    score += type_weights.get(mem_type, 1) * len(items)
                    for item in items:
                        if item[1]:
                            score -= 0.5
                return score

            sorted_groupmates = sorted(
                group_memories.keys(), key=lambda k: _calc_score(group_memories[k]), reverse=True
            )[:2]

            for groupmate_name in sorted_groupmates:
                if sum(len(v) for v in group_memories[groupmate_name].values()) > 0:
                    lines.extend(
                        self._build_memory_lines_for_user(
                            groupmate_name, group_memories[groupmate_name], is_own=False
                        )
                    )

        return lines

    def _build_memory_lines_for_user(
        self, nickname: str, mem_dict: Dict, is_own: bool = True
    ) -> List[str]:
        """为特定用户构建记忆描述行"""
        lines = []

        if mem_dict.get("preference"):
            valid_items = [item for item in mem_dict["preference"]
                           if isinstance(item, (list, tuple)) and len(item) >= 3]
            if valid_items:
                contents = [item[0] for item in valid_items if item[0]]
                any_fuzzy = any(item[1] for item in valid_items if len(item) > 1)
                if contents:
                    if is_own:
                        if any_fuzzy and random.random() < 0.4:
                            templates = [
                                f"你好像听{nickname}提过{'、'.join(contents)}…记不清了",
                                f"{nickname}是不是说过喜欢{'、'.join(contents)}来着？",
                            ]
                        else:
                            templates = [
                                f"你记得{nickname}好像对{'、'.join(contents)}挺感兴趣的",
                                f"{nickname}之前提过喜欢{'、'.join(contents)}",
                                f"你隐约记得{nickname}好像说过喜欢{'、'.join(contents)}",
                            ]
                    else:
                        if any_fuzzy and random.random() < 0.4:
                            templates = [
                                f"你好像听{nickname}提过{'、'.join(contents)}…",
                                f"{nickname}是不是说过喜欢{'、'.join(contents)}来着？",
                            ]
                        else:
                            templates = [
                                f"你记得{nickname}好像对{'、'.join(contents)}挺感兴趣的",
                                f"{nickname}之前提过喜欢{'、'.join(contents)}",
                            ]
                    lines.append(random.choice(templates))

        if mem_dict.get("fact"):
            valid_items = [item for item in mem_dict["fact"]
                           if isinstance(item, (list, tuple)) and len(item) >= 3]
            if valid_items:
                contents = [item[0] for item in valid_items if item[0]]
                any_fuzzy = any(item[1] for item in valid_items if len(item) > 1)
                if contents:
                    base = "、".join(contents)
                    if is_own:
                        if any_fuzzy and random.random() < 0.4:
                            templates = [f"{nickname}好像是{base}…对吧？",
                                         f"你隐约记得{nickname}提过自己是{base}"]
                        else:
                            templates = [f"{nickname}好像是{base}",
                                         f"你记得{nickname}提过自己是{base}"]
                    else:
                        if any_fuzzy and random.random() < 0.4:
                            templates = [f"{nickname}好像是{base}…",
                                         f"你隐约记得{nickname}提过自己是{base}"]
                        else:
                            templates = [f"{nickname}好像是{base}",
                                         f"你记得{nickname}提过自己是{base}"]
                    lines.append(random.choice(templates))

        if mem_dict.get("event"):
            valid_items = [item for item in mem_dict["event"]
                           if isinstance(item, (list, tuple)) and len(item) >= 3]
            if valid_items:
                contents = [item[0] for item in valid_items if item[0]]
                any_fuzzy = any(item[1] for item in valid_items if len(item) > 1)
                if contents:
                    base = "、".join(contents)
                    if is_own:
                        if any_fuzzy and random.random() < 0.4:
                            templates = [f"你们是不是一起{base}过来着？",
                                         f"你隐约记得上次和{nickname}一起{base}"]
                        else:
                            templates = [f"你们之前一起{base}来着",
                                         f"你记得上次和{nickname}一起{base}"]
                    else:
                        if any_fuzzy and random.random() < 0.4:
                            templates = [f"{nickname}好像{base}来着…",
                                         f"你隐约记得{nickname}提过{base}"]
                        else:
                            templates = [f"{nickname}好像{base}来着",
                                         f"你记得{nickname}提过{base}"]
                    lines.append(random.choice(templates))

        return lines