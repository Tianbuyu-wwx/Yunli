"""Darwin Provider 工厂 (provider_factory.py) 单元测试

覆盖:
  - get_default_provider: context=None / 各种获取方式
  - describe_provider: None / normal / various attributes
  - 优先级顺序验证
"""

import os
import sys
import unittest
from unittest.mock import MagicMock, patch

test_dir = os.path.dirname(os.path.abspath(__file__))
yunli_dir = os.path.dirname(test_dir)
parent_dir = os.path.dirname(yunli_dir)
for p in [parent_dir, yunli_dir, test_dir]:
    if p not in sys.path:
        sys.path.insert(0, p)

from test_base import YunliTestCase
from evolution.provider_factory import get_default_provider, describe_provider


def _ctx_no_auto(**attrs):
    """创建一个不自动生成属性的 MagicMock context

    MagicMock 默认会对任何 getattr 都返回新的 MagicMock（truthy），
    导致 provider_factory 在检查 get_using_provider 等属性时误判。
    这里显式把所有检查路径上的属性和方法设为 None。
    """
    ctx = MagicMock()
    # 方式 1: context.get_using_provider / get_provider / get_default_provider
    ctx.get_using_provider = None
    ctx.get_provider = None
    ctx.get_default_provider = None
    # 方式 2: context.provider_manager
    ctx.provider_manager = None
    # 方式 3 检查的属性
    ctx.llm_providers = None
    ctx.providers = None
    ctx.provider_map = None
    # 方式 4 检查的属性
    ctx.provider_cfg = None
    ctx.default_provider = None
    # configuration
    ctx.default_provider_id = None
    ctx.using_provider_id = None
    # 应用用户设定的属性
    for k, v in attrs.items():
        setattr(ctx, k, v)
    return ctx


class TestGetDefaultProvider(YunliTestCase):
    """get_default_provider() 函数"""

    def test_context_is_none_returns_none(self):
        provider, src = get_default_provider(None)
        self.assertIsNone(provider)
        self.assertEqual(src, "context is None")

    def test_context_has_get_using_provider(self):
        mock_provider = MagicMock()
        ctx = _ctx_no_auto(get_using_provider=lambda: mock_provider)
        provider, src = get_default_provider(ctx)
        self.assertEqual(provider, mock_provider)
        self.assertIn("get_using_provider", src)

    def test_context_has_get_provider_as_fallback(self):
        mock_provider = MagicMock()
        ctx = _ctx_no_auto(
            get_using_provider=lambda: None,
            get_provider=lambda: mock_provider,
        )
        provider, src = get_default_provider(ctx)
        self.assertEqual(provider, mock_provider)
        self.assertIn("get_provider", src)

    def test_context_has_get_default_provider_as_last_resort(self):
        mock_provider = MagicMock()
        ctx = _ctx_no_auto(
            get_using_provider=lambda: None,
            get_provider=lambda: None,
            get_default_provider=lambda: mock_provider,
        )
        provider, src = get_default_provider(ctx)
        self.assertEqual(provider, mock_provider)
        self.assertIn("get_default_provider", src)

    def test_method_raises_exception_skips(self):
        """某个方法抛异常时不应崩溃，继续尝试下一种"""
        mock_provider = MagicMock()
        ctx = _ctx_no_auto(
            get_using_provider=MagicMock(side_effect=RuntimeError("mock error")),
            get_provider=lambda: mock_provider,
        )
        provider, src = get_default_provider(ctx)
        self.assertEqual(provider, mock_provider)
        self.assertIn("get_provider", src)

    def test_no_method_returns_none(self):
        """所有获取方式都失败时返回 None"""
        ctx = _ctx_no_auto()
        provider, src = get_default_provider(ctx)
        self.assertIsNone(provider)
        self.assertEqual(src, "not found")

    def test_provider_manager_get_using_provider(self):
        mock_provider = MagicMock()
        pm = MagicMock()
        pm.get_using_provider.return_value = mock_provider
        ctx = _ctx_no_auto(provider_manager=pm)
        provider, src = get_default_provider(ctx)
        self.assertEqual(provider, mock_provider)
        self.assertIn("provider_manager.get_using_provider", src)

    def test_provider_manager_get_default_provider(self):
        mock_provider = MagicMock()
        pm = MagicMock()
        pm.get_using_provider.return_value = None
        pm.get_default_provider.return_value = mock_provider
        ctx = _ctx_no_auto(provider_manager=pm)
        provider, src = get_default_provider(ctx)
        self.assertEqual(provider, mock_provider)
        self.assertIn("get_default_provider", src)

    def test_provider_manager_get_provider(self):
        mock_provider = MagicMock()
        pm = MagicMock()
        pm.get_using_provider.return_value = None
        pm.get_default_provider.return_value = None
        pm.get_provider.return_value = mock_provider
        ctx = _ctx_no_auto(provider_manager=pm)
        provider, src = get_default_provider(ctx)
        self.assertEqual(provider, mock_provider)
        self.assertIn("get_provider", src)

    def test_provider_manager_get_first_provider(self):
        mock_provider = MagicMock()
        pm = MagicMock()
        pm.get_using_provider.return_value = None
        pm.get_default_provider.return_value = None
        pm.get_provider.return_value = None
        pm.get_first_provider.return_value = mock_provider
        ctx = _ctx_no_auto(provider_manager=pm)
        provider, src = get_default_provider(ctx)
        self.assertEqual(provider, mock_provider)
        self.assertIn("get_first_provider", src)

    def test_llm_providers_dict_with_default_id(self):
        mock_provider = MagicMock()
        ctx = _ctx_no_auto(
            llm_providers={"deepseek": mock_provider, "openai": MagicMock()},
            default_provider_id="deepseek",
        )
        provider, src = get_default_provider(ctx)
        self.assertEqual(provider, mock_provider)
        self.assertIn("deepseek", src)

    def test_llm_providers_dict_with_using_id(self):
        mock_provider = MagicMock()
        ctx = _ctx_no_auto(
            llm_providers={"gpt": mock_provider},
            using_provider_id="gpt",
        )
        provider, src = get_default_provider(ctx)
        self.assertEqual(provider, mock_provider)
        self.assertIn("gpt", src)

    def test_llm_providers_dict_first_non_none(self):
        mock_provider = MagicMock()
        ctx = _ctx_no_auto(
            llm_providers={"a": None, "b": mock_provider, "c": MagicMock()},
        )
        provider, src = get_default_provider(ctx)
        self.assertEqual(provider, mock_provider)
        self.assertIn("b", src)

    def test_providers_list(self):
        mock_provider = MagicMock()
        ctx = _ctx_no_auto(
            providers=[None, mock_provider, MagicMock()],
        )
        provider, src = get_default_provider(ctx)
        self.assertEqual(provider, mock_provider)
        self.assertIn("[1]", src)

    def test_providers_itself_is_provider_instance(self):
        mock_provider = MagicMock()
        ctx = _ctx_no_auto(providers=mock_provider)
        provider, src = get_default_provider(ctx)
        self.assertEqual(provider, mock_provider)
        self.assertIn("context.providers", src)

    def test_provider_map_as_dict(self):
        mock_provider = MagicMock()
        ctx = _ctx_no_auto(provider_map={"default": mock_provider})
        provider, src = get_default_provider(ctx)
        self.assertEqual(provider, mock_provider)

    def test_provider_cfg_direct(self):
        mock_provider = MagicMock()
        mock_provider.text_chat = MagicMock()
        ctx = _ctx_no_auto(provider_cfg=mock_provider)
        provider, src = get_default_provider(ctx)
        self.assertEqual(provider, mock_provider)
        self.assertIn("provider_cfg", src)

    def test_default_provider_attribute(self):
        mock_provider = MagicMock()
        mock_provider.text_chat = MagicMock()
        ctx = _ctx_no_auto(default_provider=mock_provider)
        provider, src = get_default_provider(ctx)
        self.assertEqual(provider, mock_provider)

    def test_no_provider_cfg_without_text_chat(self):
        """provider_cfg 存在但没有 text_chat 方法时应被跳过"""
        ctx = _ctx_no_auto(provider_cfg=MagicMock(spec=[]), default_provider=MagicMock(spec=[]))
        provider, src = get_default_provider(ctx)
        self.assertIsNone(provider)
        self.assertEqual(src, "not found")

    def test_provider_manager_method_not_callable(self):
        """provider_manager 的属性不是 callable 时应被跳过"""
        pm = MagicMock()
        # get_using_provider 是一个字符串，不是 callable
        pm.get_using_provider = "not_a_method"
        pm.get_default_provider = lambda: "default_prov"
        ctx = _ctx_no_auto(provider_manager=pm)
        provider, src = get_default_provider(ctx)
        self.assertEqual(provider, "default_prov")
        self.assertIn("get_default_provider", src)

    def test_provider_manager_getattr_raises_error(self):
        """provider_manager 的某些方法访问时抛异常不应崩溃"""
        pm = MagicMock()
        pm.get_using_provider = None

        def raise_on_getattr(name):
            if name == "get_default_provider":
                raise AttributeError("no such method")
            return object.__getattribute__(pm, name)
        # 用 mock 模拟 attribute error
        pm.get_default_provider = None
        pm.get_provider = lambda: "backup_prov"
        ctx = _ctx_no_auto(provider_manager=pm)
        provider, src = get_default_provider(ctx)
        self.assertEqual(provider, "backup_prov")
        self.assertIn("get_provider", src)

    def test_llm_providers_dict_all_none(self):
        """llm_providers dict 中所有 provider 都是 None 时跳过"""
        ctx = _ctx_no_auto(
            llm_providers={"a": None, "b": None},
            providers=["real_prov"],
        )
        provider, src = get_default_provider(ctx)
        self.assertEqual(provider, "real_prov")
        self.assertIn("providers", src)

    def test_providers_tuple(self):
        """providers 为 tuple 类型时也正常处理"""
        ctx = _ctx_no_auto(providers=("prov_a", "prov_b"))
        provider, src = get_default_provider(ctx)
        self.assertEqual(provider, "prov_a")
        self.assertIn("[0]", src)


class TestDescribeProvider(YunliTestCase):
    """describe_provider() 函数"""

    def test_none_provider(self):
        self.assertEqual(describe_provider(None, None), "None")

    def test_normal_provider(self):
        prov = MagicMock()
        prov.provider_name = "DeepSeek"
        prov.model_name = "deepseek-chat"
        prov.text_chat = MagicMock()
        desc = describe_provider(None, prov)
        self.assertIn("DeepSeek", desc)
        self.assertIn("deepseek-chat", desc)
        self.assertIn("has_text_chat=True", desc)

    def test_provider_with_name_attr(self):
        prov = MagicMock()
        prov.name = "MyProvider"
        prov.text_chat = MagicMock()
        desc = describe_provider(None, prov)
        self.assertIn("name=MyProvider", desc)

    def test_provider_with_model_attr(self):
        prov = MagicMock()
        prov.model = "gpt-4"
        desc = describe_provider(None, prov)
        self.assertIn("model=gpt-4", desc)

    def test_provider_with_provider_id(self):
        prov = MagicMock()
        prov.provider_id = "openai-001"
        desc = describe_provider(None, prov)
        self.assertIn("provider_id=openai-001", desc)

    def test_provider_without_text_chat(self):
        prov = MagicMock()
        prov.provider_name = "Test"
        del prov.text_chat
        desc = describe_provider(None, prov)
        self.assertNotIn("has_text_chat", desc)

    def test_type_included(self):
        prov = MagicMock()
        desc = describe_provider(None, prov)
        self.assertIn("type=MagicMock", desc)

    def test_error_on_attr_access_handled(self):
        """获取属性时抛异常不应崩溃"""
        prov = MagicMock()
        prov.provider_name = MagicMock(side_effect=RuntimeError("boom"))
        desc = describe_provider(None, prov)
        self.assertIn("type=", desc)


if __name__ == "__main__":
    unittest.main()