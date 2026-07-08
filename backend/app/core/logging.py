"""
统一日志模块

使用 loguru 替代标准库 logging，提供统一的日志格式、控制台彩色输出和文件轮转。
消除源项目中 loguru 与 logging 标准库混用的问题。

使用方式::

    from app.core.logging import get_logger

    logger = get_logger(__name__)
    logger.info("模块已启动")
"""

from __future__ import annotations

import sys
from pathlib import Path

from loguru import logger


# ---------------------------------------------------------------------------
# 内部状态：防止重复初始化
# ---------------------------------------------------------------------------

_initialized = False


def setup_logging(environment: str, log_dir: Path | None = None) -> None:
    """初始化全局日志配置。

    应在应用启动时调用一次（例如 FastAPI startup 事件中）。

    参数:
        environment: 当前运行环境，如 ``"local"`` / ``"dev"`` / ``"production"``。
            本地环境使用 DEBUG 级别，生产环境使用 INFO 级别。
        log_dir: 日志文件输出目录。为 ``None`` 时仅输出到控制台，不写文件。
    """
    global _initialized
    if _initialized:
        return

    is_local = environment.strip().lower() in {"local", "dev", "development", "test", "testing"}

    # 移除 loguru 默认 handler，从零开始配置
    logger.remove()

    # ------------------------------------------------------------------
    # 控制台：彩色文本输出
    # ------------------------------------------------------------------
    console_level = "DEBUG" if is_local else "INFO"
    logger.add(
        sys.stderr,
        level=console_level,
        format=(
            "<green>{time:YYYY-MM-DD HH:mm:ss.SSS}</green> | "
            "<level>{level: <8}</level> | "
            "<cyan>{name}</cyan>:<cyan>{function}</cyan>:<cyan>{line}</cyan> | "
            "<level>{message}</level>"
        ),
        colorize=True,
        backtrace=True,
        diagnose=is_local,
    )

    # ------------------------------------------------------------------
    # 文件：JSON 格式，按天轮转
    # ------------------------------------------------------------------
    if log_dir is not None:
        log_dir.mkdir(parents=True, exist_ok=True)

        # 全量日志文件 —— 按天轮转，保留 30 天，压缩为 gz
        logger.add(
            str(log_dir / "app_{time:YYYY-MM-DD}.log"),
            level="DEBUG",
            format="{time:YYYY-MM-DD HH:mm:ss.SSS} | {level: <8} | {name}:{function}:{line} | {message}",
            rotation="00:00",       # 每天午夜轮转
            retention="30 days",    # 保留 30 天
            compression="gz",       # 压缩旧文件
            serialize=True,         # JSON 格式输出
            encoding="utf-8",
            enqueue=True,           # 线程安全：日志消息先入队列再由后台线程写入
        )

        # 错误专用文件 —— 仅收集 ERROR 及以上级别，保留 60 天
        logger.add(
            str(log_dir / "error_{time:YYYY-MM-DD}.log"),
            level="ERROR",
            format="{time:YYYY-MM-DD HH:mm:ss.SSS} | {level: <8} | {name}:{function}:{line} | {message}",
            rotation="00:00",
            retention="60 days",
            compression="gz",
            serialize=True,
            encoding="utf-8",
            enqueue=True,
        )

    _initialized = True
    logger.info("日志系统初始化完成: environment={}, log_dir={}", environment, log_dir)


def get_logger(name: str):
    """返回一个绑定了模块名的 loguru logger。

    参数:
        name: 通常传入 ``__name__``，日志中会自动包含模块路径。

    返回:
        已绑定 ``name`` 的 loguru logger 实例。

    使用示例::

        from app.core.logging import get_logger

        logger = get_logger(__name__)
        logger.info("这是一条带模块名的日志")
    """
    return logger.bind(name=name)
