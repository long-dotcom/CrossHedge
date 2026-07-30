"""执行手续费口径工具。

系统内部统一使用“正数表示成本、负数表示返佣”的金额语义。MT5 的
``deal.commission`` / ``deal.fee`` 通常以负数表示扣费，其他原生 venue
则通常直接返回正数手续费，因此必须在写入执行账本前完成归一化。
"""

from __future__ import annotations

from app.core.type_utils import safe_float


def commission_cost(venue: str, value: object) -> float:
    """把 venue 原始 commission 转换为非负手续费成本。"""
    amount = safe_float(value)
    if str(venue or "").strip().lower() == "mt5":
        # 不同 Broker/测试适配器的符号约定并不一致；MT5 commission 字段在
        # 本系统只表示交易成本，因此在入账边界统一取绝对值。
        return abs(amount)
    return max(amount, 0.0)
