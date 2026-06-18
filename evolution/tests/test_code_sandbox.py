"""安全沙箱 (code_sandbox.py) 单元测试"""

import os
import sys
import unittest
from pathlib import Path

test_dir = Path(__file__).resolve().parent
yunli_dir = test_dir.parent
parent_dir = yunli_dir.parent
for p in [str(parent_dir), str(yunli_dir), str(test_dir)]:
    if p not in sys.path:
        sys.path.insert(0, p)

sys.path.insert(0, str(yunli_dir / "evolution"))

import code_sandbox


class TestCodeSandbox(unittest.TestCase):
    """安全沙箱静态校验"""

    def test_validate_safe_code_passes(self):
        ok, reason = code_sandbox.validate_code("x = 1 + 2\nprint(x)")
        self.assertTrue(ok)
        self.assertEqual(reason, "")

    def test_validate_forbidden_import(self):
        ok, reason = code_sandbox.validate_code("import os\nos.system('ls')")
        self.assertFalse(ok)
        self.assertIn("禁止的导入", reason)

    def test_validate_forbidden_builtin(self):
        ok, reason = code_sandbox.validate_code("exec('x=1')")
        self.assertFalse(ok)
        self.assertIn("exec", reason)

    def test_validate_eval_forbidden(self):
        ok, reason = code_sandbox.validate_code("eval('1+1')")
        self.assertFalse(ok)
        self.assertIn("eval", reason)

    def test_is_likely_code_recognizes_code(self):
        self.assertTrue(code_sandbox.is_likely_code("import re\nre.match('x', 'y')"))
        self.assertTrue(code_sandbox.is_likely_code("def foo():\n    pass"))

    def test_is_likely_code_natural_language_is_not_code(self):
        self.assertFalse(code_sandbox.is_likely_code("禁止冒充管理员"))

    def test_is_likely_code_regex_string_is_not_code(self):
        self.assertFalse(code_sandbox.is_likely_code('r"（.*?）"'))

    def test_iter_string_values(self):
        data = {"a": ["x", {"b": "y"}], "c": "z"}
        values = list(code_sandbox.iter_string_values(data))
        self.assertEqual(sorted(values), ["x", "y", "z"])

    def test_validate_text_if_code_skips_natural_language(self):
        ok, reason = code_sandbox.validate_text_if_code("统一使用傲娇语气")
        self.assertTrue(ok)
        self.assertEqual(reason, "")

    def test_validate_text_if_code_blocks_malicious_code(self):
        ok, reason = code_sandbox.validate_text_if_code("__import__('os').system('rm -rf /')")
        self.assertFalse(ok)
        self.assertIn("__import__", reason)


if __name__ == "__main__":
    unittest.main()
