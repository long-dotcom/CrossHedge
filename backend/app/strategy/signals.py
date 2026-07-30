"""
信号评估模块
============

定义基础信号结果数据结构。
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
