"""业务后端使用的 MT5 Redis Gateway 代理 Connector。"""

from app.venues.mt5.redis_connector import MT5RedisConnector

MT5Connector = MT5RedisConnector

__all__ = ["MT5Connector", "MT5RedisConnector"]
