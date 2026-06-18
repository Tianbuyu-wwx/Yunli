"""安全沙箱

对 LLM 生成的规则/代码进行静态安全检查，防止恶意代码被写入源文件或执行。

防护层：
1. AST 静态分析：检查 import 是否在白名单内
2. 禁止危险 builtins：exec / compile / open / globals / eval / __import__ 等
3. 代码形态识别：仅当文本看起来像可执行代码时才触发完整校验，
   避免对自然语言或正则字符串误杀
"""

import ast
from typing import Any, Iterable, Tuple

# 允许导入的标准库/安全模块（参考 seele 插件安全沙箱）
ALLOWED_IMPORTS = {
    "requests",
    "json",
    "re",
    "math",
    "datetime",
    "urllib",
    "html",
    "collections",
    "itertools",
    "hashlib",
    "base64",
    "textwrap",
    "string",
    "random",
    "time",
    "typing",
}

# 禁止调用的危险内置函数
FORBIDDEN_BUILTINS = {
    "exec",
    "eval",
    "compile",
    "open",
    "globals",
    "locals",
    "breakpoint",
    "memoryview",
    "__import__",
    "input",
    "getattr",
    "setattr",
    "delattr",
    "exit",
    "quit",
    "vars",
}


def _top_level_module(name: str) -> str:
    """获取模块名的顶层包名"""
    return name.split(".")[0] if name else ""


def validate_code(source: str) -> Tuple[bool, str]:
    """对 Python 源代码进行静态安全检查。

    Returns:
        (True, "") 表示通过；
        (False, reason) 表示存在风险并返回原因。
    """
    try:
        tree = ast.parse(source)
    except SyntaxError as e:
        return False, f"语法错误: {e.msg} (line {e.lineno})"

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if _top_level_module(alias.name) not in ALLOWED_IMPORTS:
                    return False, f"禁止的导入: {alias.name}"

        elif isinstance(node, ast.ImportFrom):
            module = node.module or ""
            if _top_level_module(module) not in ALLOWED_IMPORTS:
                return False, f"禁止的导入来源: {module}"

        elif isinstance(node, ast.Call):
            func = node.func
            if isinstance(func, ast.Name) and func.id in FORBIDDEN_BUILTINS:
                return False, f"禁止调用内置函数: {func.id}"
            if (
                isinstance(func, ast.Attribute)
                and func.attr in FORBIDDEN_BUILTINS
                and isinstance(func.value, ast.Name)
                and func.value.id in ("__builtins__", "builtins")
            ):
                return False, f"禁止调用内置函数: {func.attr}"

        elif isinstance(node, ast.Name):
            if node.id in ("__import__", "__builtins__"):
                return False, f"禁止的标识符: {node.id}"

    return True, ""


def is_likely_code(text: str) -> bool:
    """判断一段文本是否可能是可执行代码（而非自然语言/正则/JSON）。

    实现：尝试用 AST 解析；若解析成功且仅包含字符串字面量或纯标识符表达式
    （如中文自然语言会被解析为标识符序列），则视为数据而非代码。
    出现导入、函数/类定义、调用、赋值、控制流等结构时才视为代码。
    """
    if not isinstance(text, str):
        return False
    text = text.strip()
    if not text:
        return False
    try:
        tree = ast.parse(text)
    except SyntaxError:
        return False

    if not tree.body:
        return False

    # 纯字面量 / f-string 表达式 → 不是代码
    if len(tree.body) == 1:
        node = tree.body[0]
        if isinstance(node, ast.Expr) and isinstance(
            node.value, (ast.Constant, ast.JoinedStr)
        ):
            return False

    # 仅由标识符表达式组成（如中文自然语言）→ 不是代码
    if all(
        isinstance(stmt, ast.Expr) and isinstance(stmt.value, ast.Name)
        for stmt in tree.body
    ):
        return False

    # 出现任何可执行结构 → 视为代码
    for node in ast.walk(tree):
        if isinstance(
            node,
            (
                ast.Import,
                ast.ImportFrom,
                ast.FunctionDef,
                ast.ClassDef,
                ast.Call,
                ast.Assign,
                ast.AugAssign,
                ast.AnnAssign,
                ast.Delete,
                ast.Raise,
                ast.Assert,
                ast.With,
                ast.For,
                ast.While,
                ast.If,
                ast.Try,
                ast.ExceptHandler,
                ast.Lambda,
                ast.DictComp,
                ast.ListComp,
                ast.SetComp,
                ast.GeneratorExp,
                ast.Await,
                ast.Yield,
                ast.YieldFrom,
            ),
        ):
            return True

    # 其他情况（如属性访问、运算表达式）保守视为代码
    return True


def iter_string_values(obj: Any) -> Iterable[str]:
    """递归遍历 dict/list，返回所有字符串叶子节点"""
    if isinstance(obj, str):
        yield obj
    elif isinstance(obj, dict):
        for value in obj.values():
            yield from iter_string_values(value)
    elif isinstance(obj, list):
        for value in obj:
            yield from iter_string_values(value)


def validate_text_if_code(text: str) -> Tuple[bool, str]:
    """如果文本看起来像代码，则执行完整沙箱校验；否则直接通过。"""
    if not is_likely_code(text):
        return True, ""
    return validate_code(text)
