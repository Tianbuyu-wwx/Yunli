"""云璃人格 - 共享配置数据源

集中存放被 emotion/language/qq_behavior 三方共享的配置常量，
消除运行时延迟导入（循环依赖隐患）。

迁移自：
  - language.py 的关键词常量（SWORD_KEYWORDS 等 7 个）
  - language.py 的 LanguageStyleProcessor.STYLE_MODULATION
  - emotion.py 的 RelationshipManager.RELATIONSHIP_MODES
"""

from typing import Dict, List, Optional


# ========== 关键词常量（原 language.py L13-62）==========
# 其他模块通过 from .config import ... 引用，避免关键词在多个模块中独立维护导致不一致

# 剑相关关键词（扩展版：包含同义词和变体，降低漏检率）
SWORD_KEYWORDS: List[str] = [
    "剑", "刀", "武器", "锻", "老铁", "魔剑",
    "剑意", "剑心", "剑法", "剑器", "剑术", "剑道",
    "兵刃", "利器", "宝剑", "利剑", "名剑", "神兵",
    "铸剑", "熔剑", "锻剑", "磨剑", "淬炼",
    "砍", "劈", "刺", "斩", "削",
]

# 食物相关关键词（扩展版：增加更多食物类型和表达）
FOOD_KEYWORDS: List[str] = [
    "吃", "饭", "食物", "琼实", "鸟串", "肉龙",
    "糖藕", "饿", "零食", "好吃", "美味",
    "美食", "佳肴", "料理", "烹饪", "口味", "尝",
    "火锅", "烧烤", "奶茶", "蛋糕", "面包", "甜点",
    "水果", "冰淇淋", "巧克力", "炸鸡", "拉面",
    "馋", "饱", "饿死", "开饭", "干饭", "大餐",
]

# 云璃相关关键词
YUNLI_KEYWORDS: List[str] = ["云璃", "yunli", "猎剑士", "小云璃", "云璃酱"]

# 玩乐/玩笑关键词（扩展版）
PLAY_KEYWORDS: List[str] = [
    "哈哈", "好玩", "搞笑", "逗", "梗", "嘿嘿", "乐", "嘻嘻",
    "笑死", "好好笑", "太搞笑了", "笑喷", "笑cry",
    "有趣", "有意思", "真逗", "幽默", "整活",
]

# 亲密关键词（扩展版）
INTIMACY_KEYWORDS: List[str] = [
    "贴贴", "想你了", "爱你", "么么", "抱抱", "亲亲", "蹭蹭",
    "好可爱", "最棒", "真好", "好耶", "太棒了",
    "喜欢", "最喜欢", "好喜欢", "好想你",
]

# 求助关键词（扩展版）
HELP_KEYWORDS: List[str] = [
    "帮我", "求助", "请教", "教教我", "怎么做", "怎么办", "不会",
    "救命", "帮帮忙", "指点", "指导", "带我",
    "搞不懂", "不明白", "不理解", "看不懂", "听不懂",
]

# 现代概念关键词（用于类比查询）
MODERN_TERMS: List[str] = [
    "手机", "电脑", "游戏", "网络", "直播", "视频",
    "外卖", "快递", "二次元", "微信", "QQ", "抖音",
    "b站", "淘宝", "京东", "支付宝", "地铁", "高铁",
    "空调", "冰箱", "洗衣机", "电视", "电影", "音乐",
    "拍照", "扫码",
]


# ========== 关系模式定义（原 emotion.py RelationshipManager.RELATIONSHIP_MODES）==========
# 关系模式：normal/backoff/careful/warming，每种模式定义回复长度限制、提示词、衰减时间、语气词/颜文字倍率
RELATIONSHIP_MODES: Dict[str, Dict] = {
    "normal": {
        "reply_length_limit": None,       # 不限制
        "hint": "",                        # 无特殊提示
        "decay_seconds": 0,               # 不衰减
        "particle_multiplier": 1.0,       # 语气词概率倍率（正常）
        "emoji_multiplier": 1.0,          # 颜文字概率倍率（正常）
    },
    "backoff": {
        "reply_length_limit": 40,          # 回复要短
        "hint": "用户可能在表达边界，回复要短、低压，不要追问，不要主动找话题",
        "decay_seconds": 6 * 3600,         # 6小时后自动恢复
        "particle_multiplier": 0.3,        # 几乎不加语气词
        "emoji_multiplier": 0.2,           # 几乎不加颜文字
    },
    "careful": {
        "reply_length_limit": 80,          # 回复适中
        "hint": "用户可能有压力或情绪低落，先接住情绪，少讲道理，语气温和",
        "decay_seconds": 2 * 3600,         # 2小时后自动恢复
        "particle_multiplier": 0.5,        # 减少语气词，避免显得轻浮
        "emoji_multiplier": 0.5,           # 减少颜文字
    },
    "warming": {
        "reply_length_limit": None,        # 不限制
        "hint": "互动有升温，可以更自然亲近一点，偶尔开开玩笑",
        "decay_seconds": 30 * 60,          # 30分钟后自然回落
        "particle_multiplier": 1.2,        # 稍微多一点语气词
        "emoji_multiplier": 1.2,           # 稍微多一点颜文字
    },
}


# ========== 风格调制表（原 language.py LanguageStyleProcessor.STYLE_MODULATION）==========
# 根据关系模式动态调整：毒舌程度、剑类比频率、食物兴奋度、傲娇强度、语气词概率
# 值域 0.0~1.0，在 apply_style 中根据 relationship_mode 查表应用
STYLE_MODULATION: Dict[str, Dict[str, float]] = {
    "normal": {
        "tsundere_intensity": 0.5,
        "sword_analogy_rate": 0.3,
        "food_excitement": 0.7,
        "teasing_rate": 0.2,
        "verbose_bonus": 0.0,
        "kaomoji_bonus": 0.0,
    },
    "warming": {
        "tsundere_intensity": 0.7,
        "sword_analogy_rate": 0.4,
        "food_excitement": 0.8,
        "teasing_rate": 0.35,
        "verbose_bonus": 0.15,
        "kaomoji_bonus": 0.1,
    },
    "careful": {
        "tsundere_intensity": 0.2,
        "sword_analogy_rate": 0.1,
        "food_excitement": 0.3,
        "teasing_rate": 0.05,
        "verbose_bonus": -0.1,
        "kaomoji_bonus": -0.05,
    },
    "backoff": {
        "tsundere_intensity": 0.1,
        "sword_analogy_rate": 0.05,
        "food_excitement": 0.1,
        "teasing_rate": 0.0,
        "verbose_bonus": -0.2,
        "kaomoji_bonus": -0.1,
    },
}
