"""
类目生成策略：一个类目 = 一个 Category，描述这个类目的主图 / 详情页「怎么出」。

ecom_images（编排）和 asset_library（素材分配）只放通用逻辑，不写任何具体类目的词；
类目之间不同的地方都由 Category 提供：
  - playbook：类目手册（文案要点、合规词、标签必备项、照片清单、按定位的 15 屏详情页屏序），
    由 category_playbook 在 parent 之上合并注册
  - main_overrides：主图角色。平台决定主图有哪几张、什么顺序，类目决定每张拍什么
  - role_rules：每屏 / 每张主图找哪类照片当参考、画面主体是不是必须照实拍画（real）
  - photo_descs / keep_scene_types：参考图逐张怎么跟生图模型说
  - image_rules：每张 AI 画面都要遵守的类目要求（英文，拼进生图提示词；策划时也告诉 LLM）
  - icon_rules / step_pattern：信息图的图标和默认版式
  - scene_example / action_example / art_example：策划提示词里的示例
没写的字段用通用默认值，行为与「通用」类目一致。

本文件只放数据结构，不导入项目里的其它模块（category_playbook 会导入 categories，反过来会循环导入）。
"""

from dataclasses import dataclass, field

# 素材规则：(屏的作用文字正则, 需求)。需求的字段见 asset_library.ROLE_RULES：
#   types 想要的照片类型（越靠前越合适）、product 画面里是否出现产品、photo 照片够好时能否实拍直出、
#   real 画面主体是真实的产品内容（没有对口照片时不出图）、shots 适合的拍法
RoleRule = tuple[str, dict]


@dataclass(frozen=True, eq=False)
class Category:
    key: str                                    # 与类目手册 key 一致，如 "tea"
    parent: str = "general"                     # 最接近的内置类目手册：合规底线、通用要点从它继承
    playbook: dict = field(default_factory=dict)
    # {平台名或 "*": {主图角色名（「：」前）: 新的作用说明}}；平台的写法优先于 "*"。白底图不改
    main_overrides: dict = field(default_factory=dict)
    role_rules: tuple = ()                      # 排在通用规则前面，先命中的生效
    photo_descs: dict = field(default_factory=dict)          # 照片类型 → 给生图模型的英文说明
    keep_scene_types: frozenset = frozenset({"raw_material", "lifestyle"})   # 环境照片：保留真实环境
    image_rules: tuple = ()
    icon_rules: tuple = ()                      # (正则, 图标名)，排在通用规则前面
    step_pattern: str = ""                      # 屏的作用命中时默认用「流程步骤」信息图
    scene_example: str = "a home office desk at 3 pm, overcast window light, a laptop and a stack of papers"
    action_example: str = "单手拿起产品、低头细看、随手放进包里"
    art_example: str = ("soft morning window light from the left; pale oak tabletop and warm off-white linen; "
                        "recurring props: a ceramic dish, a folded linen napkin; 50mm lens, shallow depth of field, "
                        "gentle film grain")
    note_example: str = "「玻璃杯里的琥珀色茶汤」「三袋装礼盒」"      # 照片识别提示词里 note 的示例

    def main_roles(self, platform: str, roles: list[str]) -> list[str]:
        """平台的主图角色 → 本类目的主图角色：按角色名替换作用说明，角色名、张数和顺序不变"""
        over = {**self.main_overrides.get("*", {}), **self.main_overrides.get(platform, {})}
        out = []
        for role in roles:
            head = role.split("：", 1)[0]
            desc = over.get(head)
            out.append(f"{head}：{desc}" if desc and "白底" not in head else role)
        return out

    def photo_desc(self, kind: str, default: str) -> str:
        """这张参考图是什么（给生图模型）：类目可以覆盖（如宠物补充剂的「使用中」是宠物进食）"""
        return self.photo_descs.get(kind) or default


GENERIC = Category(key="general")
