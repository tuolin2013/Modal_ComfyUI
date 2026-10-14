"""
兼容层：商用标准包的数据已经搬到 categories/ 下（每个类目一个文件，数据 + 生成策略放在一起）。

新代码请直接用 categories：
    import categories
    cat = categories.get("tea")            # 生成策略（主图角色、素材规则、参考图说明、画面要求）
    cat.playbook                           # 类目手册（标签必备项、照片清单、按定位的屏序）

这个文件只是老名字的转发，方便还没改过来的调用方；里面的数据不再单独维护。
"""

from categories import pet_supplement as _pet
from categories import tea as _tea

_TEA = _tea.CATEGORY
_PET = _pet.CATEGORY

# 老代码按这些名字取数据
TEA_PHOTOS = _tea.PHOTOS
PET_PHOTOS = _pet.PHOTOS
TEA_PLANS = _tea.PLANS
PET_PLANS = _pet.PLANS
STANDARD_PACKS = {_TEA.key: _TEA.playbook, _PET.key: _PET.playbook}
DETECT_BEFORE = {_TEA.key: _TEA.parent, _PET.key: _PET.parent}

__all__ = ["DETECT_BEFORE", "PET_PHOTOS", "PET_PLANS", "STANDARD_PACKS", "TEA_PHOTOS", "TEA_PLANS"]
