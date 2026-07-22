"""低开销慢操作诊断工具。"""

from __future__ import annotations

from time import perf_counter
from typing import Any

from app.config.settings import get_settings


def elapsed_ms(started: float) -> float:
    """返回从 ``started`` 到当前的单调时钟耗时。"""
    return (perf_counter() - started) * 1000


def slow_operation_threshold_ms() -> float:
    """读取扫描慢操作日志阈值。"""
    return max(float(get_settings().scanner.slow_phase_ms), 0.0)


def log_slow_operation(logger, component: str, operation: str, duration_ms: float, **context: Any) -> None:
    """仅在超过阈值时记录结构化慢操作日志。"""
    if duration_ms < slow_operation_threshold_ms():
        return
    details = ", ".join(
        f"{key}={value}" for key, value in context.items()
        if value is not None and value != ""
    )
    logger.warning(
        "慢操作: component={}, operation={}, duration_ms={:.1f}, context={}",
        component,
        operation,
        duration_ms,
        details or "-",
    )
