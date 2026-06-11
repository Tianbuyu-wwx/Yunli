"""云璃人格插件 - 公共过滤配置与文本清理工具

集中管理所有文本过滤相关的配置和函数，避免多处维护导致不一致。
作为过滤逻辑的单一数据源，提供分层过滤接口。
"""

import re
from typing import Tuple, Dict, List

# ========== 共享正则表达式（预编译，避免重复编译开销）==========

KAOMOJI_LIKE_PATTERN = re.compile(
    r"\*[\^\▽\＾\｀\∀\ω\・\｡\^_\-\=\>\<\;\:\'\"\~\．\*\°]{2,8}\*"
)
MARKDOWN_BOLD_PATTERN = re.compile(r'\*\*[^*]+\*\*')
MARKDOWN_ITALIC_PATTERN = re.compile(r'\*[^*\s][^*]*\*')
ACTION_ASTERISK_PATTERN = re.compile(r"\*[\u4e00-\u9fff\u3040-\u309f\u30a0-\u30ff]+\*")
ACTION_ANGLE_PATTERN = re.compile(r"<[^>]+>")
EMPTY_BRACKET_PATTERN = re.compile(r"[\(\[\{（【｛][\s,，\.。！？~～]*[\)\]\}）】｝]")
LONE_OPEN_BRACKET_PATTERN = re.compile(r"(?<=[\s,，\.。！？~～])[\(\[\{（【｛](?=[\s,，\.。！？~～])")
LONE_CLOSE_BRACKET_PATTERN = re.compile(r"(?<=[\s,，\.。！？~～])[\)\]\}）】｝](?=[\s,，\.。！？~～])")
MULTI_SPACE_PATTERN = re.compile(r" +")
MULTI_PUNCT_PATTERN = re.compile(r"[。！？，]{2,}")
LEADING_PUNCT_PATTERN = re.compile(r"^[ ,，\.。！？~～]*")
TRAILING_PUNCT_PATTERN = re.compile(r"[ ,，\.。！？~～]*$")
CODE_BLOCK_PATTERN = re.compile(r'`[^`]+`')

# ========== 动作描述过滤列表 ==========
# 用于过滤AI生成的动作描述，保持纯文字对话体验
ACTION_WORDS = [
    # 中文动作词（常见AI输出）
    "挠了挠头", "摊了摊手", "耸了耸肩", "摇了摇头", "点了点头",
    "叹了口气", "笑了笑", "皱了皱眉", "瞪大了眼睛", "捂住了嘴",
    "眨了眨眼", "抱住了胳膊", "叉起了腰", "转过了身", "低下了头",
    "抬起了头", "伸了个懒腰", "打了个哈欠", "鼓起了腮帮子",
    "摸了摸鼻子", "清了清嗓子", "压低了声音", "提高了音量",
    "歪了歪头", "挑了挑眉", "咬了咬嘴唇", "握紧了拳头",
    "松开了手", "背过了手", "转过了眼睛", "移开了视线",
    "靠近了", "退后了", "蹲下了", "站了起来", "坐了下来",
    "躺了下来", "跳了起来", "跑了过去", "走了过来",
    "摸了摸头", "拍了拍肩", "拉了拉手", "推了推",
    "踢了踢", "扔了扔", "接了接", "挡了挡",
    "笑了", "哭了", "怒了", "惊了", "愣了",
    # 中文动作词（括号格式）
    "（笑）", "（哭）", "（怒）", "（惊）", "（愣）",
    "（害羞）", "（尴尬）", "（无奈）", "（得意）", "（慌张）",
    "(笑)", "(哭)", "(怒)", "(惊)", "(愣)",
    "(害羞)", "(尴尬)", "(无奈)", "(得意)", "(慌张)",
    # 日文动作词
    "照れる", "怒る", "笑う", "泣く", "驚く", "呆れる",
    "頬を染める", "目を丸くする", "腕を組む", "肩をすくめる",
    "ため息をつく", "口元を上げる", "眉をひそめる", "顔を背ける",
]

FORBIDDEN_PARTICLE_WORDS = [
    "挠头", "摊手", "耸肩", "摇头", "点头", "叹气",
    "表情", "动作", "状态",
    "照れる", "怒る", "笑う", "泣く", "驚く",
]

# ========== 纯符号颜文字过滤列表 ==========
SYMBOL_KAOMOJI_PATTERNS = [
    r"\b(orz|OTL|owo|uwu|qwq|qaq|OvO|UvU|0v0|>_<|TAT|QAQ)\b",
]

# ========== AI身份相关表述 ==========
AI_PHRASES = [
    "我是AI", "我是人工智能", "我是语言模型", "我是机器人",
    "我没有感情", "我只是程序", "我的训练数据",
]

# ========== 情感标签正则模式 ==========
EMOTION_LABEL_PATTERN = (
    r"【(?:心情|情感|状态|情绪|笑|哭|怒|惊|愣|害羞|尴尬|无奈|"
    r"得意|慌张|语音|系统|提示|注意|你理解|互动|相关背景知识)[^】]*】"
)
EMOTION_LABEL_PATTERN_FALLBACK = r"【[笑哭怒惊愣害羞尴尬无奈得意慌张语音系统提示注意理解互动背景知识]+】"

# 预编译情感标签（避免每次调用 re.sub 时重新编译）
_EMOTION_LABEL_RE = re.compile(EMOTION_LABEL_PATTERN)
_EMOTION_LABEL_FALLBACK_RE = re.compile(EMOTION_LABEL_PATTERN_FALLBACK)

# ========== 动作词预编译模式 ==========
# 预编译所有动作词的正则，避免 filter_action_words 中每次调用都 re.escape + re.compile
_ACTION_MARKED_PATTERNS: List[re.Pattern] = []
_ACTION_INDEPENDENT_PATTERNS: List[re.Pattern] = []
for _action in ACTION_WORDS:
    if not _action:
        continue
    _escaped = re.escape(_action)
    # 独立出现模式
    if _action.startswith("(") or _action.startswith("（"):
        _ACTION_INDEPENDENT_PATTERNS.append(re.compile(_escaped))
    else:
        _ACTION_INDEPENDENT_PATTERNS.append(re.compile(
            rf"(?<![\u4e00-\u9fa5a-zA-Z0-9]){_escaped}"
            r"(?![\u4e00-\u9fa5a-zA-Z0-9])"
        ))
    # *动作* / <动作> 标记模式（括号格式不需要）
    if not _action.startswith("(") and not _action.startswith("（"):
        _ACTION_MARKED_PATTERNS.append(re.compile(rf"[\*<]\s*{_escaped}\s*[\*>]"))

# ========== 符号颜文字预编译模式 ==========
_SYMBOL_KAOMOJI_RES = [re.compile(p, re.IGNORECASE) for p in SYMBOL_KAOMOJI_PATTERNS]

# ========== Emoji Unicode 范围 ==========
EMOJI_RANGES = [
    (0x1F600, 0x1F64F),  # 表情符号
    (0x1F300, 0x1F5FF),  # 符号和象形文字
    (0x1F680, 0x1F6FF),  # 交通和地图符号
    (0x1F1E0, 0x1F1FF),  # 国旗
    (0x2702, 0x27B0),    # 装饰符号
    (0x1F900, 0x1F9FF),  # 补充符号
    (0x1FA00, 0x1FA6F),  # 象棋符号等
]


# ========== 统一格式保护/恢复工具 ==========

class FormatProtector:
    """格式保护器 — 保护/恢复文本中的特殊格式

    core.py 和 language.py 中原本各自独立实现了保护/恢复逻辑，
    统一由此类管理，支持 FILO 顺序的嵌套保护。
    """

    def __init__(self):
        self._protected: Dict[str, List[str]] = {}

    def protect(self, text: str, fmt_name: str, pattern: re.Pattern) -> str:
        """保护匹配 pattern 的内容，替换为占位符"""
        if fmt_name not in self._protected:
            self._protected[fmt_name] = []
        store = self._protected[fmt_name]

        def replacer(m):
            store.append(m.group(0))
            return f"\x00{fmt_name}{len(store)-1}\x00"

        return pattern.sub(replacer, text)

    def restore(self, text: str, fmt_name: str) -> str:
        """恢复指定格式的占位符为原始内容"""
        store = self._protected.get(fmt_name, [])
        for i, original in enumerate(store):
            text = text.replace(f"\x00{fmt_name}{i}\x00", original)
        return text

    def restore_all(self, text: str) -> str:
        """从最后保护的格式开始恢复（FILO 顺序）"""
        for fmt_name in reversed(list(self._protected.keys())):
            text = self.restore(text, fmt_name)
        return text

    def clear(self):
        self._protected.clear()


# ========== 底层过滤函数 ==========

def is_emoji(char: str) -> bool:
    """检查单个字符是否为emoji表情符号"""
    code = ord(char)
    return any(start <= code <= end for start, end in EMOJI_RANGES)


def filter_emoji(text: str) -> str:
    """过滤文本中的所有emoji表情符号"""
    return ''.join(char for char in text if not is_emoji(char))


def filter_action_words(text: str) -> str:
    """过滤文本中的动作描述词（使用预编译正则）"""
    for pattern in _ACTION_MARKED_PATTERNS:
        text = pattern.sub("", text)
    for pattern in _ACTION_INDEPENDENT_PATTERNS:
        text = pattern.sub("", text)
    return text


def filter_ai_phrases(text: str, replacement: str = "我是云璃") -> str:
    """过滤AI相关表述，替换为指定文本"""
    for phrase in AI_PHRASES:
        if phrase in text:
            text = text.replace(phrase, replacement)
    return text


# ========== 高层统一过滤接口 ==========

def _filter_action_formats(text: str, protector: FormatProtector) -> str:
    """过滤 *动作*、<动作> 格式（已保护颜文字和Markdown后调用）"""
    text = ACTION_ASTERISK_PATTERN.sub("", text)
    text = ACTION_ANGLE_PATTERN.sub("", text)
    return text


def _filter_emotion_labels(text: str) -> str:
    """过滤情感标签（使用预编译正则）"""
    text = _EMOTION_LABEL_RE.sub("", text)
    text = _EMOTION_LABEL_FALLBACK_RE.sub("", text)
    return text


def _filter_symbol_kaomoji(text: str) -> str:
    """过滤纯符号颜文字（使用预编译正则）"""
    for pattern in _SYMBOL_KAOMOJI_RES:
        text = pattern.sub("", text)
    return text


def _clean_brackets_and_spaces(text: str) -> str:
    """清理残留空括号、多余空格和标点"""
    for _ in range(3):
        text = EMPTY_BRACKET_PATTERN.sub("", text)
    text = LONE_OPEN_BRACKET_PATTERN.sub("", text)
    text = LONE_CLOSE_BRACKET_PATTERN.sub("", text)
    text = MULTI_SPACE_PATTERN.sub(" ", text)
    text = MULTI_PUNCT_PATTERN.sub(lambda m: m.group(0)[0], text)
    text = LEADING_PUNCT_PATTERN.sub("", text)
    text = TRAILING_PUNCT_PATTERN.sub("", text)
    return text


def _filter_action_formats_with_protection(text: str) -> Tuple[str, FormatProtector]:
    """通用保护+动作格式过滤
    - 保护颜文字、Markdown **粗体**
    - 过滤 *中文/日文动作*、<动作>
    - 保护 *斜体*（非中文/日文，避免动作被误保）
    - 保护 `代码块`
    - 恢复被保护内容

    注意：操作顺序很关键 — 动作格式过滤必须在 italic 保护之前，
    否则 *微笑* 会被当作斜体保护起来而无法过滤。
    """
    protector = FormatProtector()

    # 第1层保护：颜文字和粗体（不和动作格式冲突）
    text = protector.protect(text, "kaomoji", KAOMOJI_LIKE_PATTERN)
    text = protector.protect(text, "markdown_bold", MARKDOWN_BOLD_PATTERN)

    # 过滤 *纯中文/日文动作*、<动作>（此时斜体还没保护，直接过滤掉）
    text = _filter_action_formats(text, protector)

    # 第2层保护：斜体、代码（动作过滤之后再保护，避免中文动作被误保）
    text = protector.protect(text, "markdown_italic", MARKDOWN_ITALIC_PATTERN)
    text = protector.protect(text, "code", CODE_BLOCK_PATTERN)

    # 恢复（逆序 FILO）
    text = protector.restore_all(text)
    return text, protector


def clean_text(text: str, mode: str = "strict") -> str:
    """统一文本过滤入口

    Args:
        text: 待处理文本
        mode: 过滤模式
            'strict' — 完整过滤（聊天模式用）：
                       AI表述 + 动作格式 + 情感标签 + emoji + 符号颜文字 +
                       动作词 + 括号/空格清理 + 内容为空兜底
            'light'  — 轻量过滤（知识查询模式用）：
                       AI表述 + 动作格式 + 情感标签 + 空格清理
            'format' — 仅格式清理（语言风格处理用）：
                       动作格式 + 情感标签 + emoji + 符号颜文字 +
                       动作词 + 括号/空格清理

    Returns:
        清理后的文本
    """
    if not text:
        return text

    if mode == "strict":
        # 1. AI表述
        text = filter_ai_phrases(text, replacement="我是云璃")

        # 2. 保护 + 动作格式过滤
        text, _ = _filter_action_formats_with_protection(text)

        # 3. 情感标签
        text = _filter_emotion_labels(text)

        # 4. Emoji
        text = filter_emoji(text)

        # 5. 符号颜文字
        text = _filter_symbol_kaomoji(text)

        # 6. 动作词
        text = filter_action_words(text)

        # 7. 括号/空格清理
        text = _clean_brackets_and_spaces(text)

        # 8. 内容为空兜底
        if not text.strip():
            return "…"

        return text

    elif mode == "light":
        # 1. AI表述
        text = filter_ai_phrases(text, replacement="我是云璃")

        # 2. 保护 + 动作格式过滤
        text, _ = _filter_action_formats_with_protection(text)

        # 3. 情感标签
        text = _filter_emotion_labels(text)

        # 4. 空格清理
        text = MULTI_SPACE_PATTERN.sub(" ", text)

        return text

    elif mode == "format":
        # 1. 保护 + 动作格式过滤
        text, _ = _filter_action_formats_with_protection(text)

        # 2. 情感标签
        text = _filter_emotion_labels(text)

        # 3. Emoji
        text = filter_emoji(text)

        # 4. 符号颜文字
        text = _filter_symbol_kaomoji(text)

        # 5. 动作词
        text = filter_action_words(text)

        # 6. 括号/空格清理
        text = _clean_brackets_and_spaces(text)

        return text

    return text