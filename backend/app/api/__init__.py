"""
API 路由聚合
============

将所有按资源域拆分的路由模块组合为一个统一的 ``APIRouter``，
供 ``main.py`` 的 ``app.include_router()`` 使用。

使用方式::

    from app.api import create_api_router

    app.include_router(create_api_router())
"""

from __future__ import annotations

from fastapi import APIRouter


def create_api_router() -> APIRouter:
    """创建并返回聚合所有子路由的顶层 APIRouter（prefix="/api"）。"""

    api = APIRouter(prefix="/api")

    # --- 认证 ---
    from app.api.auth import router as auth_router
    api.include_router(auth_router, prefix="/auth", tags=["认证"])

    # --- 仪表盘 ---
    from app.api.dashboard import router as dashboard_router
    api.include_router(dashboard_router, prefix="/dashboard", tags=["仪表盘"])

    # --- 行情与市场 ---
    from app.api.markets import router as markets_router
    api.include_router(markets_router, prefix="/markets", tags=["行情"])

    # --- 诊断 ---
    from app.api.diagnostics import router as diagnostics_router
    api.include_router(diagnostics_router, prefix="/diagnostics", tags=["诊断"])

    # --- SSE 流 ---
    from app.api.streaming import router as streaming_router
    api.include_router(streaming_router, prefix="/stream", tags=["实时流"])

    # --- 分析 ---
    from app.api.analytics import router as analytics_router
    api.include_router(analytics_router, prefix="/analytics", tags=["分析"])

    # --- 套利机会 ---
    from app.api.opportunities import router as opportunities_router
    api.include_router(opportunities_router, prefix="/opportunities", tags=["套利机会"])

    # --- 对冲组 ---
    from app.api.hedge_groups import router as hedge_groups_router
    api.include_router(hedge_groups_router, prefix="/hedge-groups", tags=["对冲组"])

    # --- 账户 ---
    from app.api.accounts import router as accounts_router
    api.include_router(accounts_router, prefix="/accounts", tags=["账户"])

    # --- 持仓 ---
    from app.api.positions import router as positions_router
    api.include_router(positions_router, prefix="/positions", tags=["持仓"])

    # --- 执行控制（/execution/*）---
    from app.api.execution import execution_router
    api.include_router(execution_router, prefix="/execution", tags=["执行"])

    # --- 订单 / 成交（/api/orders, /api/fills，根级别）---
    from app.api.execution import orders_fills_router
    api.include_router(orders_fills_router, tags=["执行"])

    # --- 风控 ---
    from app.api.risk import router as risk_router
    api.include_router(risk_router, prefix="/risk", tags=["风控"])

    # --- 系统设置 ---
    from app.api.settings_api import router as settings_router
    api.include_router(settings_router, prefix="/settings", tags=["设置"])

    # --- 日志与告警（/api/logs, /api/alerts，根级别）---
    from app.api.logs import router as logs_router
    api.include_router(logs_router, tags=["日志"])

    return api
