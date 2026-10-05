"""存储仓库模块"""

from .mysql_repository import MySQLRepository
from .redis_repository import RedisRepository

__all__ = ["MySQLRepository", "RedisRepository"]
