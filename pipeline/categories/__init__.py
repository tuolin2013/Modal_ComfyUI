"""
类目注册表：categories/ 下每个模块（_ 开头的和 base 除外）定义一个 CATEGORY，导入时自动注册。

现有类目：tea（代用茶 / 张家界莓茶）、pet_supplement（宠物营养补充剂）。其它内置手册（食品、美妆……）
只有文案和合规，生成策略用通用的 GENERIC。

新增类目（例如「蜂蜜」）：
  1. 复制 categories/tea.py 为 categories/honey.py，改 key、parent 和两部分内容：
     - PLAYBOOK：文案要点、合规词、标签必备项、照片清单、按定位的 15 屏详情页屏序
     - CATEGORY：主图拍什么、每屏找哪类照片、参考图怎么说明、画面要求、提示词示例
  2. 运行 pytest tests/test_categories.py：契约测试会检查屏序是否 15 屏、照片类型和拍法是否存在、
     主图角色名能否对上平台、正则能否编译等
不写代码也可以在界面上新建自定义类目手册（只有文案和合规）：parent 选一个已注册的类目，就沿用它的生成策略。
"""

import importlib
import pkgutil

from .base import GENERIC, Category, RoleRule

REGISTRY: dict[str, Category] = {}


def register(cat: Category) -> Category:
    if cat.key in REGISTRY and REGISTRY[cat.key] is not cat:
        raise ValueError(f"类目 key 重复：{cat.key}")
    REGISTRY[cat.key] = cat
    return cat


def get(key: str | None) -> Category:
    """已注册的类目；没注册的（通用、内置的轻量手册）返回 GENERIC"""
    return REGISTRY.get(str(key or ""), GENERIC)


def registered() -> list[Category]:
    return list(REGISTRY.values())


def _discover() -> None:
    for m in sorted(pkgutil.iter_modules(__path__), key=lambda x: x.name):
        if m.name.startswith("_") or m.name == "base":
            continue
        cat = getattr(importlib.import_module(f"{__name__}.{m.name}"), "CATEGORY", None)
        if isinstance(cat, Category):
            register(cat)


_discover()

__all__ = ["Category", "GENERIC", "REGISTRY", "RoleRule", "get", "register", "registered"]
