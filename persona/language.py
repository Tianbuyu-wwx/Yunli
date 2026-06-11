import random
import re
from typing import Dict, List, Optional

from . import filters


# ========== 共享关键词常量（单一数据源）==========
# 其他模块（emotion.py、qq_behavior.py）通过 from .language import ... 引用
# 避免关键词在多个模块中独立维护导致不一致

# 剑相关关键词
SWORD_KEYWORDS = [
    "剑", "刀", "武器", "锻", "老铁", "魔剑",
    "剑意", "剑心", "剑法", "剑器",
]

# 食物相关关键词
FOOD_KEYWORDS = [
    "吃", "饭", "食物", "琼实", "鸟串", "肉龙",
    "糖藕", "饿", "零食", "好吃", "美味",
]

# 云璃相关关键词
YUNLI_KEYWORDS = ["云璃", "yunli", "猎剑士", "小云璃"]

# 玩乐/玩笑关键词
PLAY_KEYWORDS = ["哈哈", "好玩", "搞笑", "逗", "梗", "嘿嘿", "乐", "嘻嘻"]

# 亲密关键词
INTIMACY_KEYWORDS = ["贴贴", "想你了", "爱你", "么么", "抱抱", "亲亲", "蹭蹭"]

# 求助关键词
HELP_KEYWORDS = ["帮我", "求助", "请教", "教教我", "怎么做", "怎么办", "不会"]

# 现代概念关键词（用于类比查询）
MODERN_TERMS = [
    "手机", "电脑", "游戏", "网络", "直播", "视频",
    "外卖", "快递", "二次元", "微信", "QQ", "抖音",
    "b站", "淘宝", "京东", "支付宝", "地铁", "高铁",
    "空调", "冰箱", "洗衣机", "电视", "电影", "音乐",
    "拍照", "扫码",
]


class LanguageStyleProcessor:
    """云璃语言风格处理器"""

    TOPIC_PATTERNS = {
        "sword": list(SWORD_KEYWORDS),
        "food": list(FOOD_KEYWORDS),
        "combat": ["战斗", "打", "敌人", "攻击", "防御", "胜负", "输赢", "比划"],
        "emotion": ["喜欢", "爱", "恨", "难过", "开心", "悲伤", "生气", "高兴"],
        "family": ["父", "母", "爷爷", "怀炎", "家人", "爸妈"],
        "modern": [
            "手机",
            "电脑",
            "游戏",
            "网",
            "直播",
            "视频",
            "外卖",
            "快递",
            "二次元",
            "番剧",
        ],
        "greeting": ["你好", "嗨", "在吗", "早上好", "晚上好", "晚安"],
        "farewell": ["再见", "拜拜", "走了", "下线", "睡觉"],
    }

    # 按话题动态长度限制（聊天模式）
    # 减少字数 → 更少分段 → 降低刷屏概率
    TOPIC_MAX_LENGTH = {
        "greeting": 60,    # 问候/告别 — 简单回应
        "farewell": 60,    # 同上
        "general": 120,    # 日常闲聊 — 一句话足矣
        "combat": 120,     # 战斗话题 — 简短过招
        "emotion": 120,    # 情感话题 — 关心不需要长篇
        "family": 100,     # 家庭话题 — 回避型，短为宜
        "modern": 120,     # 现代概念 — 类比一句带过
        "food": 140,       # 食物 — 可以多说一点点
        "sword": 160,      # 剑 — 剑痴可以多说两句
    }

    # 云璃常用语气词（贴合人设：朱明猎剑士、傲娇、直率、古代少女）
    # 按位置分类：前缀语气词（句首）、后缀语气词（句末）、独立语气词（可单独成句）
    PARTICLES = {
        "neutral": {
            "prefix": ["嗯", "那个", "话说"],  # 偏直率，不拖泥带水
            "suffix": ["呢", "吧", "啊"],  # 保留基础语气词，去掉太软的"嘛""哦""啦"
            "standalone": ["嗯", "这样", "好吧"],  # 简洁回应
        },
        "excited": {
            "prefix": ["哈！", "哦？", "哼哼", "不错嘛"],  # 剑痴的兴奋，带点傲娇
            "suffix": ["哈", "呢", "吧"],  # 兴奋但不过分撒娇
            "standalone": ["好！", "不错", "正合我意"],  # 猎剑士的干脆
        },
        "tsundere": {
            "prefix": ["哼，", "切，", "哼！", "少啰嗦，"],  # 傲娇标准前缀
            "suffix": ["哼", "切", "吧"],  # 嘴硬
            "standalone": ["哼", "随便你", "少得意"],  # 傲娇三连
        },
        "annoyed": {
            "prefix": ["喂，", "我说，", "啧，", "不是"],  # 不耐烦，直率
            "suffix": ["啊", "吧", "啧"],  # 干脆的不耐烦
            "standalone": ["啧", "麻烦", "真是"],  # 猎剑士嫌麻烦
        },
        "curious": {
            "prefix": ["嗯？", "哦？", "咦？", "欸"],  # 好奇但保持警觉
            "suffix": ["吗", "呢", "么"],  # 疑问语气
            "standalone": ["嗯？", "什么", "真的吗"],  # 直率的好奇
        },
        "happy": {
            "prefix": ["哈哈", "嘿嘿", "嗯哼"],  # 开心但不失英气
            "suffix": ["哈", "呢", "吧"],  # 爽朗
            "standalone": ["哈哈", "不错", "正合我意"],  # 猎剑士式开心
        },
        "sad_guarded": {
            "prefix": ["那个", "嗯", "……"],  # 回避型，话少
            "suffix": ["呢", "吧", "啊"],  # 淡淡的
            "standalone": ["嗯", "算了", "没什么"],  # 掩饰悲伤
        },
        "serious": {
            "prefix": ["听着，", "我说，", "注意"],  # 认真时的威严
            "suffix": ["吧", "呢"],  # 沉稳
            "standalone": ["嗯", "明白", "知道了"],  # 干脆利落
        },
        "bored": {
            "prefix": ["唉", "嗯", "那个"],  # 无聊，提不起劲
            "suffix": ["吧", "呢"],  # 敷衍
            "standalone": ["无聊", "没劲", "随便"],  # 猎剑士无聊时更直接
        },
    }

    # 句式调整规则
    STYLE_RULES = {
        "short_sentences": True,  # 短句为主
        "exclamation_marks": True,  # 使用感叹号
        "direct_speech": True,  # 直接表达
        "sword_analogy": True,  # 剑类比
    }

    def __init__(self, config: dict = None):
        self.config = config or {}
        self.rules = {**self.STYLE_RULES, **self.config.get("language_rules", {})}

    def detect_topic(self, text: str) -> str:
        """检测当前话题类型"""
        text = text.lower()
        topic_scores = {}

        for topic, keywords in self.TOPIC_PATTERNS.items():
            score = sum(1 for kw in keywords if kw in text)
            if score > 0:
                topic_scores[topic] = score

        if not topic_scores:
            return "general"

        return max(topic_scores, key=topic_scores.get)

    def detect_query_mode(self, text: str) -> str:
        """检测当前是查询模式还是聊天模式

        采用三级检测策略：
        1. 强信号检测：明确的知识查询或聊天信号
        2. 主题分析：判断用户意图是查知识还是聊角色
        3. 默认兜底：无明确信号时默认聊天模式

        Returns:
            'knowledge_query': 知识查询（现代信息/联网搜索/学科知识）
            'chat': 普通聊天（角色相关内容/日常闲聊）
        """
        text_lower = text.lower()

        # ========== 第一层：强知识信号（几乎一定表示要查知识）==========
        strong_knowledge_signals = [
            # 学习/教学类
            "详细讲讲", "教教我", "怎么学", "学不会", "不太会",
            "不太懂", "不懂", "求助", "请教", "帮我",
            "给我讲", "讲讲", "教程", "攻略", "详解",
            "保姆级", "入门", "基础", "原理", "概念",
            "定义", "公式", "推导", "证明", "计算",
            "步骤", "方法", "例题", "习题", "练习",
            # 学科类
            "数学", "物理", "化学", "生物", "地理", "历史",
            "政治", "经济", "金融", "会计", "法律", "医学",
            "编程", "代码", "算法", "数据结构", "操作系统",
            "计算机网络", "数据库", "前端", "后端", "全栈",
            "人工智能", "机器学习", "深度学习", "大模型",
            # 工具/语言类
            "python", "java", "c++", "c语言", "javascript", "typescript",
            "go语言", "rust", "ruby", "php", "swift", "kotlin",
            "linux", "windows", "macos", "docker", "kubernetes",
            "git", "github", "vscode", "idea", "pycharm",
            "mysql", "postgresql", "mongodb", "redis",
            "react", "vue", "angular", "django", "flask", "spring",
            # 学术类
            "论文", "文献", "研究", "理论", "实验", "数据",
            "分析", "统计", "模型", "假设",
            # 总结/归纳类（长文本输出）
            "总结", "概括", "归纳", "整理", "汇总", "梳理",
            "列出", "列一下", "列举", "罗列",
            "回顾一下", "复盘", "提炼", "提取",
        ]
        if any(s in text_lower for s in strong_knowledge_signals):
            return "knowledge_query"

        # ========== 第二层：强聊天信号（几乎一定表示要聊天）==========
        strong_chat_signals = [
            # 问候类
            "你好", "在吗", "在干嘛", "想你了", "喜欢你",
            "可爱", "漂亮", "好看", "帅", "厉害", "棒",
            "谢谢", "对不起", "抱歉", "辛苦了", "加油",
            "早上好", "晚上好", "晚安", "早安", "再见", "拜拜",
            # 情感类
            "哈哈哈", "嘿嘿", "嘻嘻", "呜呜", "啊啊", "呜呜呜",
            "生气", "开心", "难过", "伤心", "高兴", "兴奋",
            # 日常类
            "吃饭", "睡觉", "玩", "游戏", "视频", "音乐", "电影",
            "饿", "累", "困", "冷", "热", "无聊",
            # 互动类
            "抱抱", "摸摸", "亲亲", "贴贴", "蹭蹭",
            "好耶", "太好", "不错", "可以", "行", "好",
        ]
        if any(s in text_lower for s in strong_chat_signals):
            return "chat"

        # ========== 第三层：主题分析 ==========
        # 扩展的角色关键词
        character_keywords = [
            # 核心角色及变体
            "云璃", "yunli", "云璃酱",
            # 亲人/关系密切者
            "老铁", "怀炎", "爷爷", "祖父",
            # 地点
            "朱明", "仙舟", "罗浮", "曜青", "方壶", "玉阙", "虚陵", "渊野",
            # 武器/能力
            "魔剑", "猎剑", "剑心", "剑法", "剑术", "铸剑", "熔剑",
            # 种族/势力
            "岁阳", "燧皇", "丰饶", "巡猎", "毁灭", "存护", "智识",
            # 物品/食物
            "焰轮", "琼实", "鸟串", "镕兵", "剑炉", "糖葫芦",
            # 事件/组织
            "演武", "仪典", "星天演武", "竞锋舰", "朱明仙舟",
            # 其他角色
            "彦卿", "开拓者", "三月七", "丹恒", "景元", "符玄", "白露",
            "停云", "驭空", "素裳", "罗刹", "镜流", "刃", "卡芙卡",
            "银狼", "流萤", "黄泉", "黑天鹅", "花火", "知更鸟", "星期日",
            # 游戏相关
            "崩坏", "星穹铁道", "星铁", "崩铁", "米哈游", "mihoyo",
        ]

        # 扩展的查询关键词
        modern_query_patterns = [
            # 信息类
            "新闻", "天气", "股票", "汇率", "地图", "翻译",
            "今天", "现在", "最新", "实时", "当前",
            "百度", "谷歌", "搜索", "百科", "维基",
            "多少钱", "价格", "怎么样", "评测", "对比",
            "下载", "安装", "配置", "bug", "报错", "错误",
            "怎么解决", "怎么办", "推荐", "排行", "榜单", "热门",
            # 通用查询
            "是什么", "什么是", "是谁", "在哪里", "为什么",
            "怎么做", "介绍一下", "告诉我", "说说看",
            "解释一下", "什么意思", "怎么", "如何",
            "哪些", "有什么", "有哪些",
            "请问", "咨询", "查询", "查一下",
            "资料", "信息", "知识", "背景", "设定", "来历",
        ]

        has_character = any(kw in text_lower for kw in character_keywords)
        has_query = any(q in text_lower for q in modern_query_patterns)

        # 同时包含角色词和查询词：判断意图
        if has_character and has_query:
            # 检查是否是"关于角色本身"的问题（如：云璃是谁/云璃的剑法）
            # 这类问题保持聊天模式，让角色自己介绍
            for kw in character_keywords:
                if kw in text_lower:
                    # 角色词在查询词之前，通常是关于角色的
                    kw_pos = text_lower.index(kw)
                    for q in modern_query_patterns:
                        if q in text_lower:
                            q_pos = text_lower.index(q)
                            if kw_pos < q_pos:
                                # "云璃的剑法是什么" → 聊天模式
                                return "chat"
            # 查询词在角色词之前，可能是查知识时提到角色
            # "什么是云璃" → 仍然是关于角色的，聊天模式
            return "chat"

        # 只有查询词，没有角色词 → 知识查询
        if has_query and not has_character:
            return "knowledge_query"

        # 只有角色词，没有查询词 → 聊天模式
        if has_character and not has_query:
            return "chat"

        # 什么都没有 → 默认聊天模式
        return "chat"

    def apply_style(
        self, text: str, emotion_state: str = "neutral", mode: str = "auto", is_first_segment: bool = True
    ) -> str:
        """应用云璃语言风格

        三种模式处理：
        - knowledge_query: 知识查询 → 清晰、准确、保留完整信息
        - chat: 普通聊天 → 活泼、有情感、拟人化
        - mixed: 混合模式 → 轻度人格化，保持信息清晰

        Args:
            text: 要处理的文本
            emotion_state: 情感状态
            mode: 模式，'auto'自动检测，'knowledge_query'知识查询，'chat'普通聊天
        """
        # 自动检测模式
        if mode == "auto":
            mode = self.detect_query_mode(text)

        topic = self.detect_topic(text)

        # ========== 知识查询模式：最小化风格干预 ==========
        if mode == "knowledge_query":
            # 只应用知识查询风格（清理格式，不添加情感）
            text = self._apply_knowledge_style(text)
            # 通用规则（保留换行、清理多余空格，但不截断长度）
            text = self._apply_general_rules(text, apply_length_limit=False)
            return text

        # ========== 聊天模式：完整风格处理 ==========
        # 根据话题应用不同风格
        if topic == "sword":
            text = self._apply_sword_enthusiasm(text)
        elif topic == "food":
            text = self._apply_food_lover_tone(text)
        elif topic == "modern":
            text = self._apply_modern_analogy(text)
        elif topic == "family":
            text = self._apply_avoidance(text)
        else:
            text = self._apply_default_style(text)

        # 添加情感语气词（只有第一段加，避免碎嘴）
        text = self._add_emotion_particles(text, emotion_state, topic, is_first_segment)

        # 通用风格调整（传递 is_first_segment 避免多段重复修改）
        text = self._apply_general_rules(text, is_first_segment=is_first_segment, topic=topic)

        return text

    def _apply_knowledge_style(self, text: str) -> str:
        """应用知识查询风格：清晰、准确、保留完整信息"""
        # 去除过多的语气词（保留少量体现性格）
        text = re.sub(r"[！]{2,}", "！", text)  # 多个感叹号合并
        text = re.sub(r"[…]{2,}", "…", text)  # 多个省略号合并

        # 注意：不再截断知识查询内容，由 main.py 中的 max_text_length 统一控制长度
        # 这里只做格式清理，不做内容截断

        return text

    def _apply_sword_enthusiasm(self, text: str) -> str:
        """应用剑话题的兴奋风格（通过语气和内容体现，不添加动作描述）"""
        # 如果文本中没有剑相关词汇，自然引入
        if not any(kw in text for kw in ["剑", "刀", "武器"]):
            text = "说到这个…" + text

        # 增加感叹号体现兴奋
        if "！" not in text and "!" not in text:
            text = text + "！"

        return text

    def _apply_food_lover_tone(self, text: str) -> str:
        """应用食物话题的开心风格"""
        food_exclamations = self.config.get("food_exclamations", [
            "听起来好好吃！",
            "我也想吃！",
            "琼实鸟串才是最好的！",
        ])
        food_exclamation_prob = self.config.get("food_exclamation_probability", 0.3)

        if random.random() < food_exclamation_prob and food_exclamations:
            text = text + random.choice(food_exclamations)

        return text

    def _apply_modern_analogy(self, text: str) -> str:
        """应用现代概念的类比风格"""
        # 保持原有处理，具体类比由数据库提供
        return text

    def _apply_avoidance(self, text: str) -> str:
        """应用回避风格（悲伤话题）- 通过话题转移体现，不描述动作"""
        avoid_phrases = self.config.get("avoid_phrases", [
            "…这个嘛。",
            "不说这个了。",
            "对了，你最近有练剑吗？",
            "…换个话题吧。",
            "嗯…",
        ])
        avoidance_prob = self.config.get("avoidance_probability", 0.7)

        if random.random() < avoidance_prob and avoid_phrases:
            text = random.choice(avoid_phrases) + " " + text

        return text

    def _apply_default_style(self, text: str) -> str:
        """应用默认风格（更自然的句子结构）"""
        # 控制句子长度
        max_default_sentences = self.config.get("max_default_sentences", 3)
        sentences = re.split(r"[。！？\.\!\?]", text)
        sentences = [s.strip() for s in sentences if s.strip()]

        if len(sentences) > max_default_sentences:
            # 如果句子太多，精简一下
            text = "。".join(sentences[:max_default_sentences]) + "。"

        # 拟人化：偶尔在长句中添加停顿（模拟思考）
        if len(text) > 30 and random.random() < 0.15:
            # 在逗号后添加省略号停顿
            comma_positions = [m.start() for m in re.finditer(r"，", text)]
            if comma_positions and len(comma_positions) >= 2:
                # 在第二个逗号后添加停顿
                pos = comma_positions[1]
                text = text[:pos+1] + "…" + text[pos+1:]

        return text

    def _add_emotion_particles(self, text: str, emotion_state: str, topic: str = "", is_first_segment: bool = True) -> str:
        """添加情感语气词（更自然的真人说话模式）

        真人语气词特点：
        - 不是每句话都加，而是偶尔加
        - 位置不固定：可能在开头、结尾，或单独成句
        - 会根据句子长度和内容选择是否添加
        - 短句更倾向于加后缀，长句更倾向于加前缀或停顿

        Args:
            text: 要处理的文本
            emotion_state: 情感状态
            topic: 当前话题，用于控制"哈！"等兴奋前缀的使用频率
            is_first_segment: 是否为多段回复中的第一段（只有第一段加语气词）
        """
        # 非第一段不添加语气词，避免碎嘴
        if not is_first_segment:
            return text

        # 空文本不处理
        text = text.strip()
        if not text:
            return text

        particle_data = self.PARTICLES.get(emotion_state, self.PARTICLES["neutral"])

        # 基础概率（默认0.06，降低以避免过度使用）
        base_probability = self.config.get("emotion_particle_probability", 0.06)

        # 根据句子特征动态调整概率
        text_len = len(text)

        # 短句（1-10字）：降低加语气词概率，避免"呢"单独出现
        if text_len <= 10:
            base_probability *= 0.3
        # 中等长度（11-30字）：正常概率
        elif text_len <= 30:
            base_probability *= 1.0
        # 长句（31-60字）：略降，避免语气词淹没在内容中
        elif text_len <= 60:
            base_probability *= 0.7
        # 超长句（>60字）：提高前缀概率（作为过渡），降低后缀概率
        else:
            base_probability *= 0.8

        # 对于非剑/食物话题，进一步降低兴奋语气词的概率
        if emotion_state == "excited" and topic not in ["sword", "food", "combat"]:
            base_probability *= 0.5

        # 如果文本已经有语气词，大幅降低再添加的概率
        existing_particles = ["呢", "吧", "嘛", "哦", "啦", "哈", "哼", "切", "唉", "嗯"]
        if any(p in text[-5:] for p in existing_particles):  # 检查句末5个字
            base_probability *= 0.2
        if any(p in text[:5] for p in existing_particles):  # 检查句首5个字
            base_probability *= 0.3

        if random.random() >= base_probability:
            return text

        # 选择语气词位置：前缀(30%)、后缀(50%)、独立(20%，仅短回复)
        # 真人更习惯在句末加语气词
        position_roll = random.random()

        if position_roll < 0.3:
            # 前缀（句首）
            prefix_particles = particle_data.get("prefix", [])
            if prefix_particles:
                particle = random.choice(prefix_particles)
                text = self._attach_particle_prefix(text, particle)
        elif position_roll < 0.8:
            # 后缀（句末）- 最常见
            suffix_particles = particle_data.get("suffix", [])
            if suffix_particles:
                particle = random.choice(suffix_particles)
                text = self._attach_particle_suffix(text, particle)
        elif text_len <= 20:
            # 独立语气词（仅短回复，且概率较低）
            standalone_particles = particle_data.get("standalone", [])
            if standalone_particles and random.random() < 0.3:
                particle = random.choice(standalone_particles)
                # 独立语气词放在开头，后面接原句
                text = particle + "，" + text

        return text

    def _attach_particle_prefix(self, text: str, particle: str) -> str:
        """把语气词加在句首，带语法校验"""
        # 单字语气词（呢、吧、嘛）加在句首不符合中文语法，直接跳过
        if particle in ("呢", "吧", "嘛"):
            return text
        # 带标点的语气词（哈！、嗯？、哼，）可以出现在句首
        if any(p in particle for p in "！？，"):
            return particle + text
        # 其他情况（如"哼哼"）也允许，但加顿号更自然
        return particle + "，" + text

    def _attach_particle_suffix(self, text: str, particle: str) -> str:
        """把语气词加在句末，带语法校验"""
        # 如果句末已经有语气词，不再叠加
        if text.endswith(("呢", "吧", "嘛", "哦", "啊", "哼", "哈", "啦", "哟", "么")):
            return text
        # 如果句末是感叹号或问号，语气词插入到标点前面
        # 例如 "有需要随时喊我！" + "呢" -> "有需要随时喊我呢！"
        if text.endswith("！") or text.endswith("!"):
            return text[:-1] + particle + text[-1]
        if text.endswith("？") or text.endswith("?"):
            return text[:-1] + particle + text[-1]
        # 如果句末是句号，替换为语气词+句号更自然
        if text.endswith("。"):
            return text[:-1] + particle + "。"
        # 普通情况直接追加，偶尔加波浪号（更口语化）
        if random.random() < 0.2:
            return text + "~" + particle
        return text + particle

    def _apply_general_rules(self, text: str, apply_length_limit: bool = True,
                              is_first_segment: bool = True, topic: str = "general") -> str:
        """应用通用语言规则：保持纯文字，过滤所有表情和动作

        拟人化增强：
        - 偶尔省略标点（真人打字习惯）
        - 添加自然的口语化替换
        - 模拟打字错误后修正（极低概率）
        - 添加犹豫和停顿

        Args:
            text: 要处理的文本
            apply_length_limit: 是否应用长度限制（知识查询模式应设为False）

        注意：知识查询模式下只清理格式，不做拟人化处理
        """
        # 1. 去除过于正式的表达（更口语化）
        formal_words = {
            "您好": "你好",
            "请问": "",
            "非常抱歉": "对不起啦",
            "非常感谢": "谢了",
            "请问您": "你",
            "您": "你",  # QQ聊天很少用"您"
            "是否": "是不是",
            "能否": "能不能",
            "如何": "怎么",
            "为何": "为什么",
            "之": "的",  # 过于文言
            "亦": "也",
            "乃": "是",
        }
        for formal, casual in formal_words.items():
            text = text.replace(formal, casual)

        # 2. 统一过滤（委托 filters.clean_text，mode='format' 只做格式清理）
        text = filters.clean_text(text, mode="format")

        # 标点与语气词衔接优化
        # 避免 "。嘛" "，呢" 等生硬衔接，改为 "嘛" "呢" 或 "~嘛"
        text = re.sub(r"[。，,]+([嘛呢吧哦啊哼哈]+)", r"\1", text)
        # 如果语气词在句末，偶尔加波浪号更自然
        particle_wave_prob = self.config.get("particle_wave_probability", 0.3)
        if text.endswith(("嘛", "呢", "吧", "哦")) and random.random() < particle_wave_prob:
            text = text[:-1] + "~" + text[-1]

        # 3. 拟人化：偶尔省略句末标点（真人聊天习惯）
        # 只在第一段应用，避免每段都省略标点
        if apply_length_limit and is_first_segment and random.random() < 0.12:
            # 随机省略最后一个标点
            if text and text[-1] in "。！？":
                text = text[:-1]

        # 4. 拟人化：偶尔添加口语化连接词（贴合云璃人设）
        # 只在第一段应用
        # 云璃是直率猎剑士，去掉太现代的"讲真""说实话"，保留"其实""话说"
        if apply_length_limit and is_first_segment and len(text) > 20 and random.random() < 0.04:
            casual_openers = ["其实", "话说"]
            if not any(text.startswith(op) for op in casual_openers):
                opener = random.choice(casual_openers)
                # 只对英文字母首字符应用 lower()，避免破坏中文或其他字符
                first_char = text[0]
                if 'A' <= first_char <= 'Z':
                    first_char = first_char.lower()
                text = opener + "，" + first_char + text[1:]

        # 5. 拟人化：偶尔把"很"换成"挺"（更口语，但保持云璃的直率感）
        # 注意：只在第一段应用，避免多段回复中每段都被修改
        # 云璃是古代少女，"和"→"跟"太现代了，去掉；保留"很"→"挺"
        if apply_length_limit and is_first_segment:
            if random.random() < 0.06:
                text = text.replace("很", "挺", 1)

        # 6. 确保句子不要太长（按话题动态调整，减少分段数）
        # 注意：知识查询模式下的长度限制由 main.py 统一控制（默认1200）
        # 这里只在聊天模式下应用较短的长度限制，按话题精准控制
        if apply_length_limit:
            # 按话题选择长度上限，话题不存在时退回到 120
            topic_max = self.TOPIC_MAX_LENGTH.get(topic, 120)
            # 全局配置作为硬上限
            config_max = self.config.get("max_text_length", 200)
            max_text_length = min(topic_max, config_max)
            if len(text) > max_text_length:
                # 智能句子补全截断：在限制范围内回退到最近的句号
                cut = max_text_length
                for i in range(max_text_length, max(1, max_text_length - 30), -1):
                    if text[i-1] in '。！？.!?\n':
                        cut = i
                        break
                text = text[:cut]

        return text

    def extract_keywords(self, text: str) -> List[str]:
        """从文本中提取关键词"""
        # 早退：消息中不含任何领域关键词则跳过嵌套循环
        if not _ALL_DOMAIN_KEYWORDS or not any(kw in text for kw in _ALL_DOMAIN_KEYWORDS):
            return []
        words = []
        for topic, keywords in self.TOPIC_PATTERNS.items():
            for kw in keywords:
                if kw in text and kw not in words:
                    words.append(kw)
        return words

    def should_use_direct_response(self, text: str) -> bool:
        """判断是否可以直接使用数据库台词响应"""
        topic = self.detect_topic(text)

        # 某些简单场景可以直接用数据库台词
        direct_topics = ["greeting", "farewell", "sword_simple"]

        return topic in direct_topics


# ========== 模块级常量 ==========

# 全量领域关键词集合（模块加载时从 TOPIC_PATTERNS 提取一次，避免每消息重复遍历）
_ALL_DOMAIN_KEYWORDS: set = set()
for _kw_list in LanguageStyleProcessor.TOPIC_PATTERNS.values():
    _ALL_DOMAIN_KEYWORDS.update(_kw_list)
