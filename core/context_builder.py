"""云璃插件 - 上下文构建模块

从 main.py 中提取的上下文构建逻辑：
- 环境感知（时间/节假日/农历/节气）
- 群聊上下文（活跃话题 + 群氛围）
- 关系上下文（用户关系 + 记忆）
- 知识相关的记忆召回
"""

import hashlib
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

        # 情境推理提示
        situational_hint = self._infer_situational_context(current)
        if situational_hint:
            lines.append(f"情境推断：{situational_hint}")

        # 季节感知（替代天气API，零外部依赖）
        seasonal = self._infer_seasonal_context(current)
        if seasonal:
            lines.append(f"季节体感：{seasonal}")

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

    def _infer_situational_context(self, current: datetime) -> str:
        """基于时间和场景推断社交情境

        覆盖全时段：早高峰 → 上午办公 → 午休 → 下午办公 → 下班 → 晚间 → 睡前 → 深夜 → 凌晨
        """
        hints = []
        weekday = current.weekday()
        hour = current.hour

        # 工作日时段推断
        if weekday < 5:  # 周一到周五
            if 7 <= hour < 9:
                hints.append("大家可能在上班或上学的路上")
            elif 9 <= hour < 12:
                hints.append("大家可能在忙工作或上课")
            elif 12 <= hour < 14:
                hints.append("午休时间，很多人可能在吃饭或休息")
            elif 14 <= hour < 17:
                hints.append("大家可能在下午的工作或学习中")
            elif 17 <= hour < 19:
                hints.append("下班放学时间，有人可能在通勤路上")
            elif 19 <= hour < 22:
                hints.append("晚上休息时间，大家比较放松")
            elif 22 <= hour < 24:
                hints.append("深夜了，还在线的人可能睡不着或加班")
        else:  # 周末
            if 7 <= hour < 9:
                hints.append("周末早上，大多数人还在睡觉")
            elif 9 <= hour < 12:
                hints.append("周末上午，大家可能在睡懒觉或放松")
            elif 12 <= hour < 14:
                hints.append("周末中午，大家可能在吃午饭")
            elif 14 <= hour < 18:
                hints.append("周末下午，大家可能在休息或出去玩")
            elif 18 <= hour < 22:
                hints.append("周末晚上，大家一般在休闲娱乐")
            elif 22 <= hour < 24:
                hints.append("周末深夜，夜猫子还在线")

        # 凌晨推断
        if 0 <= hour < 5:
            hints.append("凌晨了，几乎没人在线")
        elif 5 <= hour < 7:
            hints.append("清晨，可能只有早起的人在线")

        # 特殊时段
        month = current.month
        if month in (6, 12):
            hints.append("可能是考试季，有人可能在备考")
        elif month == 1 and 20 <= current.day <= 31 or month == 2 and current.day <= 10:
            hints.append("可能是春节期间")

        return "；".join(hints) if hints else ""

    def _infer_seasonal_context(self, current: datetime) -> str:
        """基于月份推断季节体感（替代天气API，零外部依赖）"""
        month = current.month
        day = current.day
        if month in (3, 4, 5):
            if month == 5 and day >= 20:
                return "初夏，天气渐热"
            return "春天"
        elif month in (6, 7, 8):
            if month == 8 and day >= 20:
                return "夏末，暑气渐消"
            return "夏天，很热"
        elif month in (9, 10, 11):
            if month == 11 and day >= 20:
                return "初冬，天气转凉"
            return "秋天"
        else:
            if month == 2 and day >= 20:
                return "冬末，春寒料峭"
            return "冬天，很冷"

    # 社交场景关键词 → 常识提示映射表（Phase 1 P1 扩展版）
    SOCIAL_SCENE_KEYWORDS = {
        ("生日",): "有人过生日时，说句生日快乐",
        ("晚安", "睡了", "睡觉", "困了", "去睡了", "熬夜"): "对方说晚安可能要去睡了，别长篇大论",
        ("在忙", "加班", "开会", "有事", "忙", "没空", "等下"): "对方在忙，别追问或打扰",
        ("生病", "不舒服", "头疼", "难受想哭", "发烧", "感冒", "咳嗽"): "对方不舒服，语气温和点，别开玩笑",
        ("考试", "复习", "备考", "期末", "期中", "测验"): "对方在备考，可以鼓励一下但别打扰",
        ("吃饭", "吃饭了", "在吃", "干饭", "外卖", "做饭"): "对方在吃饭，可以简短回应",
        ("谢谢", "感谢", "多谢", "辛苦", "感恩"): "对方在道谢，可以自然回应，不用太正式",
        ("再见", "拜拜", "走了", "下了", "下线", "明天见"): "对方要走了，简短道别即可",
        ("哈哈哈", "笑死", "好好笑", "太搞笑了", "笑喷", "笑cry"): "对方在笑，可以跟着开心的氛围回应",
        ("好烦", "好累", "emo", "心情不好", "自闭", "郁闷", "烦死了"): "对方情绪低落，先接住情绪，别急着讲道理",
        # Phase 1 P1 新增场景
        ("炫耀", "看我", "快看", "晒", "秀", "展示", "看看我的"): "对方在炫耀/分享，可以适当夸赞",
        ("吐槽", "受不了", "无语", "服了", "气死", "坑"): "对方在吐槽，可以跟着共情，别讲道理",
        ("求助", "帮我", "请教", "怎么办", "怎么做", "救命", "不会", "搞不懂"): "对方在求助，认真回答，别太傲娇",
        ("分享", "给你看", "安利", "推荐", "这个好", "发现"): "对方在分享有趣的东西，可以表示好奇",
        ("郁闷", "自闭", "想哭", "难受", "好难", "坚持不住"): "对方情绪低落，需要安慰和鼓励",
        ("庆祝", "恭喜", "成功了", "中了", "拿到了", "通过了"): "对方在庆祝，跟着开心并送上祝贺",
        ("道歉", "对不起", "抱歉", "是我的错", "不好意思"): "对方在道歉，可以大方一点，不必纠结",
        ("无聊", "没事干", "好闲", "发呆", "不知道干嘛"): "对方很无聊，可以主动找话题互动",
        ("早安", "早上好", "早", "醒了", "起床"): "对方刚起床，可以简短问候",
        ("游戏", "打游戏", "开黑", "上分", "排位", "组队"): "对方在玩游戏，可以简短回应，别长篇大论",
    }

    def detect_social_scene(self, message: str) -> str:
        """检测社交场景并返回极简常识提示（~10 Token/条）

        基于消息内容的简单关键词匹配，不依赖 LLM 判断。
        """
        if not message:
            return ""
        for keywords, hint in self.SOCIAL_SCENE_KEYWORDS.items():
            if any(kw in message for kw in keywords):
                return hint
        return ""

    # ========== 群聊上下文 ==========

    def build_chat_context(
        self, group_id: str, atmosphere_text: str = "", token_budget: int = 200,
        group_perception=None
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

        # 最近发言者摘要（感知谁在跟谁说话）
        if group_perception and used_tokens < token_budget - 40:
            speakers_summary = group_perception.build_recent_speakers_summary(group_id)
            if speakers_summary:
                context_parts.append(f"【{speakers_summary}】")
                used_tokens += estimate_tokens(speakers_summary)

        return "\n\n".join(context_parts) if context_parts else ""

    def _normalize_message_for_exclude(
        self, message: str
    ) -> str:
        """标准化消息用于排除比较

        去除 @前缀、CQ 码、前后空白，避免因为格式差异导致重复注入。
        """
        import re
        text = (message or "").strip()
        # 去除 CQ 码（如 [CQ:at,qq=123456]）
        text = re.sub(r"\[CQ:[^\]]+\]", "", text).strip()
        # 去除简化 At 标记（如 [At:bot]）
        text = re.sub(r"\[At:[^\]]+\]", "", text).strip()
        # 去除 @云璃酱 等 @ 前缀
        text = re.sub(r"@[^\s]+", "", text).strip()
        # 去除前后标点
        text = text.strip(".:：，。！？~ \t")
        return text

    def build_recent_chat_history(
        self,
        group_id: str,
        limit: int = 20,
        exclude_message: str = "",
        token_budget: int = 400,
        include_bot_response: bool = True,
    ) -> str:
        """构建最近群聊原文上下文

        P0-2 / P2-11 修复：让云璃能看到群友最近说了什么，像真人一样了解群聊上下文。

        解决问题：原系统仅注入"话题摘要""氛围""记忆"等抽象信息，
        云璃看不到群友的具体发言原文，导致：
        - 无法接梗（不知道群友刚说了什么）
        - 关系错乱（不知道谁在和谁说话）
        - 互动生硬（只能基于抽象话题回应，缺乏具体语境）
        - 切换聊天对象后忘记刚才群里发生的事

        实现方案：
        1. 从 interaction_logs 获取最近 N 条群聊记录
        2. 按时间正序排列（最早的在前，最新的在后，符合阅读习惯）
        3. 格式化为"昵称: 消息"的形式
        4. 排除当前正在处理的消息（避免与 req.prompt 中的用户消息重复）
        5. 排除空消息和纯命令消息
        6. token 预算控制，超限时从最早的消息开始截断
        7. 可选包含云璃自己的回复，让上下文更完整

        Args:
            group_id: 群号
            limit: 获取最近 N 条消息（默认 20 条，P2-11 从 10 提高）
            exclude_message: 当前正在处理的消息内容（避免重复注入）
            token_budget: token 预算（默认 400，P2-11 从 300 提高）
            include_bot_response: 是否包含云璃自己的回复（默认 True，P2-11 新增）

        Returns:
            格式化的群聊原文，如：
            【最近群聊】
            小明: 今天天气真好
            小红: 是啊，想出去玩
            云璃（你）: 我也想去！
            小刚: 我也要去！
            空字符串表示无可用记录
        """
        if not group_id:
            return ""

        # 获取最近 limit*2 条记录（多取一些用于过滤后仍有足够数量）
        # 时间范围设为 2 小时，覆盖一般群聊活跃时段
        recent_logs = self.db.memory_db.get_recent_interactions(
            group_id, hours=2, limit=limit * 2
        )

        if not recent_logs:
            return ""

        # 过滤和清洗
        filtered = []
        exclude_normalized = self._normalize_message_for_exclude(exclude_message)
        for log in recent_logs:
            message = (log.get("message") or "").strip()
            response = (log.get("response") or "").strip()
            nickname = (log.get("user_nickname") or "").strip()
            trigger_type = (log.get("trigger_type") or "").strip()

            # 跳过空消息
            if not message:
                continue
            # 跳过纯命令消息（以 / 或 # 开头）
            if message.startswith(("/", "#")):
                continue

            # 排除当前正在处理的消息（标准化后比较）
            if exclude_normalized:
                msg_normalized = self._normalize_message_for_exclude(message)
                if msg_normalized and msg_normalized == exclude_normalized:
                    continue

            # 跳过无昵称的记录（异常数据）
            if not nickname:
                continue

            filtered.append({
                "nickname": nickname,
                "message": message,
                "response": response,
                "trigger_type": trigger_type,
                "id": log.get("id", 0),
            })

            # P2-11：可选包含云璃自己的回复，形成完整对话上下文
            if include_bot_response and response:
                filtered.append({
                    "nickname": "云璃",
                    "message": response,
                    "role": "yunli",
                    "id": log.get("id", 0) + 0.5,  # 确保回复在原消息之后
                })

        if not filtered:
            return ""

        # 按时间正序排列（最早的在前，最新的在后）
        # 使用 id 排序而非 created_at，因为：
        # 1. id 是自增的，天然反映插入顺序
        # 2. created_at 是秒级精度，同一秒内插入的多条消息顺序不确定
        filtered.sort(key=lambda x: x["id"])

        # 只取最近 limit 条
        filtered = filtered[-limit:]

        # 格式化并控制 token 预算
        # 反向遍历（从最新到最早），优先保留最新的消息
        # 超预算时停止添加更早的消息
        lines = []
        used_tokens = estimate_tokens("【最近群聊】") + 2  # 标题 + 换行
        for item in reversed(filtered):
            role_tag = ""
            if item.get("role") == "yunli":
                role_tag = "（你）"
            line = f"{item['nickname']}{role_tag}: {item['message']}"
            line_tokens = estimate_tokens(line)
            # 预算检查：保留至少 20 token 给后续内容
            if used_tokens + line_tokens > token_budget - 20:
                break  # 超预算，停止添加更早的消息
            lines.append(line)
            used_tokens += line_tokens

        if not lines:
            return ""

        # 反转为正序输出（最早的在前，最新的在后）
        lines.reverse()
        return "【最近群聊】\n" + "\n".join(lines)

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

        # 1. 关系描述（加限定语"从直接互动来看"，避免与AstrBot对话历史中的群聊记录矛盾）
        rel_descriptions = {
            "老朋友": f"从直接互动来看，你和{user_nickname}很熟了，经常聊天",
            "熟人": f"从直接互动来看，你和{user_nickname}聊过几次，算是认识",
            "认识": f"从直接互动来看，你记得{user_nickname}，但还不算太熟",
            "陌生人": f"从直接互动来看，{user_nickname}好像是第一次跟你说话",
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
                group_id, user_id, message, min_confidence=5, limit=mem_limit
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
        min_confidence: int = 5, limit: int = 3,
        include_group_memories: bool = True
    ) -> List[Dict]:
        """获取与当前消息相关的记忆（相关性动态召回）

        注意：min_confidence 默认从 7 降为 5，避免群友记忆因置信度门槛过高而无法召回。
        """
        candidates = self.db.get_important_memories(
            group_id, user_id, min_confidence=min_confidence, limit=limit * 3
        )

        group_candidates = []
        if include_group_memories and group_id:
            # 群友记忆召回数量从 limit*2 提升到 limit*3，扩大群友画像覆盖
            group_candidates = self.db.get_group_memories(
                group_id, min_confidence=min_confidence, limit=limit * 3,
                exclude_user_id=user_id
            )

        # 提取消息关键词（统一使用 GroupPerception.TOPIC_KEYWORDS，避免重复定义）
        from .group_perception import GroupPerception
        message_keywords = set()
        topic_keywords_map = {}
        for kw, topic in GroupPerception.TOPIC_KEYWORDS.items():
            topic_keywords_map.setdefault(topic, []).append(kw)
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
                # 群友记忆折扣从 0.7 调整为 0.9，避免群友记忆几乎无法召回
                score *= 0.9
            return score

        scored = []
        for mem in candidates:
            scored.append((_score_memory(mem, False), mem))
        for mem in group_candidates:
            scored.append((_score_memory(mem, True), mem))

        scored.sort(key=lambda x: x[0], reverse=True)

        selected = []
        # P1-6 修复：批量更新访问次数，替代 N+1 查询
        # 原代码循环调用 access_memory，每条记忆单独 UPDATE + COMMIT
        # 现改为一次批量 UPDATE，减少锁争用和磁盘 I/O
        for _, mem in scored[:limit]:
            selected.append(mem)

        # 批量更新访问计数
        if selected:
            memory_ids = [mem["id"] for mem in selected]
            self.db.access_memories_batch(memory_ids)

        return selected

    # ========== 记忆文本构建 ==========

    @staticmethod
    def _deterministic_choice(items: list, seed: str):
        """确定性选择：基于种子哈希从列表中选一项，相同输入总是返回相同结果"""
        if not items:
            return None
        h = int(hashlib.md5(seed.encode()).hexdigest()[:8], 16)
        return items[h % len(items)]

    @staticmethod
    def _deterministic_probability(seed: str, threshold: float) -> bool:
        """确定性概率判断：基于种子哈希，相同输入总是返回相同结果"""
        h = int(hashlib.md5(seed.encode()).hexdigest()[:8], 16)
        return (h % 1000) / 1000.0 < threshold

    @staticmethod
    def is_memory_fuzzy(access_count: int, created_at: str, content: str = "") -> bool:
        """判断记忆是否应该被模糊化（模拟遗忘，确定性版本）"""
        if access_count >= 5:
            return False
        if not created_at:
            return False
        try:
            mem_time = datetime.strptime(created_at, "%Y-%m-%d %H:%M:%S")
            days_old = (datetime.now() - mem_time).days
        except Exception:
            return False

        # 使用确定性概率替代 random.random()，确保相同输入总是返回相同结果
        seed = f"{content}_{created_at}_{access_count}"
        if access_count <= 1 and days_old > 7:
            return ContextBuilder._deterministic_probability(seed, 0.5)
        elif access_count <= 2 and days_old > 30:
            return ContextBuilder._deterministic_probability(seed, 0.3)
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

            # 记忆文本截断：从 15 字提升到 30 字，避免关键信息丢失
            # 存储时允许 50 字（memory_max_content_length），显示时截断到 30 字
            # 如"用户是计算机科学专业的大学生" 不再被截断为"用户是计算机科学专业的…"
            if len(content) > 30:
                content = content[:30] + "…"

            is_fuzzy = self.is_memory_fuzzy(
                mem.get("access_count", 1), mem.get("created_at", ""), mem.get("content", "")
            )
            item = (content, is_fuzzy, mem_display_name)

            # 严格归属判断：仅当 current_user_id 和 mem_user_id 都非空且相等时，
            # 才归为"自己的记忆"；否则归入群记忆。
            # 移除不可靠的昵称回退匹配（QQ群昵称可重复），避免A的记忆被错误归到B名下。
            # mem_user_id 为空（旧数据）也归入群记忆，避免污染当前用户画像。
            if current_user_id and mem_user_id and mem_user_id == current_user_id:
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
        """为特定用户构建记忆描述行（确定性版本，使用哈希替代 random）

        所有模板均以"你记得"开头，明确区分记忆与对话历史中的原始表述，
        避免LLM同时看到两种来源时产生认知矛盾。
        """
        lines = []

        if mem_dict.get("preference"):
            valid_items = [item for item in mem_dict["preference"]
                           if isinstance(item, (list, tuple)) and len(item) >= 3]
            if valid_items:
                contents = [item[0] for item in valid_items if item[0]]
                any_fuzzy = any(item[1] for item in valid_items if len(item) > 1)
                if contents:
                    seed = f"pref_{nickname}_{'|'.join(contents)}_{is_own}"
                    if is_own:
                        if any_fuzzy and self._deterministic_probability(seed, 0.4):
                            templates = [
                                f"你好像听{nickname}提过{'、'.join(contents)}…记不清了",
                                f"你记得{nickname}是不是说过喜欢{'、'.join(contents)}来着？",
                            ]
                        else:
                            templates = [
                                f"你记得{nickname}好像对{'、'.join(contents)}挺感兴趣的",
                                f"你记得{nickname}之前提过喜欢{'、'.join(contents)}",
                                f"你隐约记得{nickname}好像说过喜欢{'、'.join(contents)}",
                            ]
                    else:
                        if any_fuzzy and self._deterministic_probability(seed, 0.4):
                            templates = [
                                f"你好像听{nickname}提过{'、'.join(contents)}…",
                                f"你记得{nickname}是不是说过喜欢{'、'.join(contents)}来着？",
                            ]
                        else:
                            templates = [
                                f"你记得{nickname}好像对{'、'.join(contents)}挺感兴趣的",
                                f"你记得{nickname}之前提过喜欢{'、'.join(contents)}",
                            ]
                    lines.append(self._deterministic_choice(templates, seed))

        if mem_dict.get("fact"):
            valid_items = [item for item in mem_dict["fact"]
                           if isinstance(item, (list, tuple)) and len(item) >= 3]
            if valid_items:
                contents = [item[0] for item in valid_items if item[0]]
                any_fuzzy = any(item[1] for item in valid_items if len(item) > 1)
                if contents:
                    base = "、".join(contents)
                    seed = f"fact_{nickname}_{base}_{is_own}"
                    if is_own:
                        if any_fuzzy and self._deterministic_probability(seed, 0.4):
                            templates = [f"你记得{nickname}好像是{base}…对吧？",
                                         f"你隐约记得{nickname}提过自己是{base}"]
                        else:
                            templates = [f"你记得{nickname}好像是{base}",
                                         f"你记得{nickname}提过自己是{base}"]
                    else:
                        if any_fuzzy and self._deterministic_probability(seed, 0.4):
                            templates = [f"你记得{nickname}好像是{base}…",
                                         f"你隐约记得{nickname}提过自己是{base}"]
                        else:
                            templates = [f"你记得{nickname}好像是{base}",
                                         f"你记得{nickname}提过自己是{base}"]
                    lines.append(self._deterministic_choice(templates, seed))

        if mem_dict.get("event"):
            valid_items = [item for item in mem_dict["event"]
                           if isinstance(item, (list, tuple)) and len(item) >= 3]
            if valid_items:
                contents = [item[0] for item in valid_items if item[0]]
                any_fuzzy = any(item[1] for item in valid_items if len(item) > 1)
                if contents:
                    base = "、".join(contents)
                    seed = f"event_{nickname}_{base}_{is_own}"
                    if is_own:
                        if any_fuzzy and self._deterministic_probability(seed, 0.4):
                            templates = [f"你记得你们是不是一起{base}过来着？",
                                         f"你隐约记得上次和{nickname}一起{base}"]
                        else:
                            templates = [f"你记得你们之前一起{base}来着",
                                         f"你记得上次和{nickname}一起{base}"]
                    else:
                        if any_fuzzy and self._deterministic_probability(seed, 0.4):
                            templates = [f"你记得{nickname}好像{base}来着…",
                                         f"你隐约记得{nickname}提过{base}"]
                        else:
                            templates = [f"你记得{nickname}好像{base}来着",
                                         f"你记得{nickname}提过{base}"]
                    lines.append(self._deterministic_choice(templates, seed))

        return lines