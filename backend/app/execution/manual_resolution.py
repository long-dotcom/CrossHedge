"""
人工平仓介入模块
================

处理需要人工介入的平仓场景：
- 判断对冲组是否处于 ``manual_intervention`` 状态
- 构建人工平仓腿（``ManualCloseLeg``）
- 检测"无可平仓位"类错误结果，支持自动结案

当自动平仓出现单边成交或找不到可平仓位时，
系统会将对冲组标记为 ``manual_intervention``，
由本模块提供人工介入所需的辅助逻辑。

使用方式::

    from app.execution.manual_resolution import (
        is_manual_intervention_group,
        manual_close_legs,
        all_results_are_no_close_position,
    )
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from app.core.type_utils import safe_float


# "无可平仓位"错误消息标记集合
# 当适配器返回的错误消息包含以下任一关键词时，
# 认为该结果是"无可平仓位"而非真正的执行失败
NO_CLOSE_POSITION_MARKERS = (
    "未找到可平仓持仓",
    "没有可平仓真实仓位",
    "没有可平仓位",
    "no position",
    "No position",
)


@dataclass(frozen=True)
class ManualCloseLeg:
    """人工平仓腿 —— 描述一条需要人工执行的平仓指令。

    属性:
        platform: 目标平台标识（如 "hyperliquid" / "mt5"）。
        adapter: 对应的交易所适配器实例。
        symbol: 统一品种代码。
        venue_symbol: 交易所侧实际品种代码。
        side: 平仓方向（"buy" / "sell"）。
        quantity: 平仓数量。
        order_type: 订单类型（如 "market" / "limit"）。
    """
    platform: str
    adapter: Any
    symbol: str
    venue_symbol: str
    side: str
    quantity: float
    order_type: str


def is_manual_intervention_group(group: Any) -> bool:
    """判断对冲组是否处于需要人工介入的状态。

    参数:
        group: 对冲组对象（ORM 或任意含 status 属性的对象）。

    返回:
        ``True`` 表示该对冲组状态为 ``manual_intervention``。
    """
    return str(getattr(group, "status", "") or "") == "manual_intervention"


def manual_close_legs(
    *,
    group: Any,
    mapping: Any,
    leg_a_adapter: Any,
    leg_b_adapter: Any,
    leg_a_side: str,
    leg_b_side: str,
    leg_a_quantity: float,
    leg_b_quantity: float,
) -> list[ManualCloseLeg]:
    """根据对冲组和品种映射构建人工平仓腿列表。

    仅当对应腿的数量 > 0 时才生成该腿的平仓指令。

    参数:
        group: 对冲组对象，需含 ``symbol`` 属性。
        mapping: 品种映射对象，需含腿 A/B 的 venue、venue_symbol 及订单类型信息。
        leg_a_adapter: 腿 A 的交易所适配器。
        leg_b_adapter: 腿 B 的交易所适配器。
        leg_a_side: 腿 A 平仓方向。
        leg_b_side: 腿 B 平仓方向。
        leg_a_quantity: 腿 A 平仓数量。
        leg_b_quantity: 腿 B 平仓数量。

    返回:
        需要执行的平仓腿列表（0~2 条）。
    """
    legs: list[ManualCloseLeg] = []
    if leg_a_quantity > 0:
        legs.append(ManualCloseLeg(
            platform=mapping.leg_a_venue,
            adapter=leg_a_adapter,
            symbol=group.symbol,
            venue_symbol=mapping.leg_a_venue_symbol,
            side=leg_a_side,
            quantity=leg_a_quantity,
            order_type=mapping.hl_close_order_type,
        ))
    if leg_b_quantity > 0:
        legs.append(ManualCloseLeg(
            platform=mapping.leg_b_venue,
            adapter=leg_b_adapter,
            symbol=group.symbol,
            venue_symbol=mapping.mt5_symbol,
            side=leg_b_side,
            quantity=leg_b_quantity,
            order_type=mapping.mt5_close_order_type,
        ))
    return legs


def all_results_are_no_close_position(results: list[Any]) -> bool:
    """判断所有执行结果是否均为"无可平仓位"。

    用于人工介入场景：如果所有腿的平仓结果都是"找不到可平仓位"，
    则可以安全地将对冲组标记为已平仓（无需实际平仓）。

    参数:
        results: 各腿平仓执行结果列表。

    返回:
        ``True`` 表示所有结果都是"无可平仓位"类型。
    """
    if not results:
        return False
    return all(is_no_close_position_result(result) for result in results)


def is_no_close_position_result(result: Any) -> bool:
    """判断单条执行结果是否为"无可平仓位"类型。

    排除逻辑：
    - 如果结果成功且有实际成交 → 不是"无可平仓位"
    - 如果结果处于待成交状态 → 不是"无可平仓位"
    - 最后检查错误消息是否包含"无可平仓位"标记

    参数:
        result: 适配器返回的平仓执行结果。

    返回:
        ``True`` 表示该结果为"无可平仓位"类型。
    """
    # 成功且有成交 → 不是"无可平仓位"
    if bool(getattr(result, "success", False)) and safe_float(getattr(result, "filled_quantity", 0.0)) > 0:
        return False
    # 待成交状态 → 不是"无可平仓位"
    if str(getattr(result, "status", "") or "") in {"accepted", "submitted", "pending", "open", "new"}:
        return False
    # 检查错误消息是否包含"无可平仓位"标记
    message = str(getattr(result, "error_message", "") or "")
    return any(marker in message for marker in NO_CLOSE_POSITION_MARKERS)


def no_residual_position_close_reason(reason: str) -> str:
    """生成"无可平仓位"结案原因描述。

    参数:
        reason: 原始平仓原因。

    返回:
        格式化的结案原因字符串。
    """
    return f"人工介入结案，无可平仓位: {reason}"
