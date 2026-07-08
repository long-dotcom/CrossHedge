"""
类型转换工具模块

消除源项目中大量重复的 ``getattr(obj, 'field', 0.0) or 0.0`` / ``float(value or 0.0)``
安全转换模式，提供统一的类型安全转换函数。

使用方式::

    from app.core.type_utils import safe_float, safe_int

    price = safe_float(row.price)        # None → 0.0, "" → 0.0, "1.5" → 1.5
    count = safe_int(row.quantity)       # None → 0,   "" → 0,   "3"   → 3
"""

from __future__ import annotations


def safe_float(value, default: float = 0.0) -> float:
    """安全地将任意值转换为浮点数。

    处理以下异常情况：
    - ``None`` → 返回 *default*
    - 空字符串 ``""`` → 返回 *default*
    - 非数字字符串 → 返回 *default*
    - 已经是数字类型 → 正常转换

    参数:
        value: 待转换的值，可以是任意类型。
        default: 转换失败时返回的默认值，默认 ``0.0``。

    返回:
        转换后的浮点数，或 *default*。
    """
    if value is None:
        return default
    if isinstance(value, str):
        value = value.strip()
        if not value:
            return default
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def safe_int(value, default: int = 0) -> int:
    """安全地将任意值转换为整数。

    处理以下异常情况：
    - ``None`` → 返回 *default*
    - 空字符串 ``""`` → 返回 *default*
    - 非数字字符串 → 返回 *default*
    - 浮点数字符串（如 ``"3.14"``） → 先转 float 再截断为 int
    - 已经是数字类型 → 正常转换

    参数:
        value: 待转换的值，可以是任意类型。
        default: 转换失败时返回的默认值，默认 ``0``。

    返回:
        转换后的整数，或 *default*。
    """
    if value is None:
        return default
    if isinstance(value, str):
        value = value.strip()
        if not value:
            return default
    try:
        # 先尝试直接 int() 转换；对浮点数字符串需先 float() 再 int()
        return int(value)
    except (TypeError, ValueError):
        try:
            return int(float(value))
        except (TypeError, ValueError):
            return default
