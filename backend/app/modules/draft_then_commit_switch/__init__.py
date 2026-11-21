"""草稿切换 · draft_then_commit_switch

主链路：检测应用 → 草稿区 → 对撞 → 原子切正式 → 当前镜 → 核证。

落库架构（全局唯一）:
    甲 · 失败草稿留痕为准
      对撞失败正式态完全不动；草稿行保留 failed + 原因；核证只读接口可读失败草稿。

实现见 service.py。路由挂在 /api/switch/*，历史维 /api/scd/apply 为正式写闸（拒绝直改）。
"""

from app.modules.draft_then_commit_switch.service import (  # noqa: F401
    ARCHITECTURE,
    DIRECT_WRITE_BLOCKED,
    SwitchError,
    abandon_switch,
    apply_switch,
    commit_prepared,
    plant_half_switch,
    repair_half_switch,
    switch_proof,
)

__all__ = [
    "ARCHITECTURE",
    "DIRECT_WRITE_BLOCKED",
    "SwitchError",
    "abandon_switch",
    "apply_switch",
    "commit_prepared",
    "plant_half_switch",
    "repair_half_switch",
    "switch_proof",
]
