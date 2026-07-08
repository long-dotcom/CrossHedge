"""
信号评估模块
============

定义基础的信号结果数据结构和入场信号评估函数。
根据净利润、年化收益与阈值对比，判定信号状态（rejected / candidate / executable）。
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class SignalResult:
    """信号评估结果。

    属性:
        status: 信号状态 —— ``"rejected"`` / ``"candidate"`` / ``"executable"``
        reason: 判定原因的中文描述
    """
    status: str
    reason: str


def evaluate_signal(
    net_profit: float,
    annualized_return: float,
    min_net_profit: float,
    min_annualized_return: float,
) -> SignalResult:
    """根据净利润和年化收益判定信号是否可执行。

    判定顺序:
    1. 净利润 <= 0 → rejected（扣除成本后无利润）
    2. 净利润 < 最小净利润阈值 → candidate
    3. 年化收益 < 最小年化阈值 → candidate
    4. 以上均通过 → executable

    参数:
        net_profit: 当前净利润
        annualized_return: 当前年化收益率
        min_net_profit: 策略设定的最小净利润阈值
        min_annualized_return: 策略设定的最小年化收益率阈值

    返回:
        SignalResult 包含状态和原因描述
    """
    if net_profit <= 0:
        return SignalResult("rejected", "扣除成本后无利润")
    if net_profit < min_net_profit:
        return SignalResult("candidate", "净利润未达到执行阈值")
    if annualized_return < min_annualized_return:
        return SignalResult("candidate", "年化收益未达到执行阈值")
    return SignalResult("executable", "")
