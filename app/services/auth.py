"""认证服务 - 密码哈希、Session管理（支持 Redis 降级）"""

import logging
import secrets
import bcrypt
import threading
import os
from datetime import datetime, timedelta
from typing import Optional, Dict

from sqlalchemy import select

from app.models.admin import Admin
from app.models.base import get_session_context
from app.repositories.redis_repository import RedisRepository

logger = logging.getLogger(__name__)

# Session 配置
SESSION_PREFIX = "session:"
SESSION_EXPIRE_SECONDS = 24 * 60 * 60  # 24小时


class MemorySessionStore:
    """内存 Session 存储（用于 Redis 不可用时的降级）
    
    注意: 这个存储只在单实例模式下工作，多进程部署时会话不共享
    """
    
    def __init__(self, max_sessions: int = 1000):
        self._sessions: Dict[str, dict] = {}
        self._expiry: Dict[str, datetime] = {}
        self._lock = threading.RLock()
        self._max_sessions = max_sessions
    
    def set(self, key: str, data: dict, expire_seconds: int):
        """存储 Session"""
        with self._lock:
            # 清理过期 Session
            self._cleanup_expired()
            
            # 限制最大 Session 数量
            if len(self._sessions) >= self._max_sessions:
                self._evict_oldest()
            
            self._sessions[key] = data
            self._expiry[key] = datetime.utcnow() + timedelta(seconds=expire_seconds)
    
    def get(self, key: str) -> Optional[dict]:
        """获取 Session"""
        with self._lock:
            if key not in self._sessions:
                return None
            
            # 检查是否过期
            if datetime.utcnow() > self._expiry.get(key, datetime.min):
                self._delete(key)
                return None
            
            return self._sessions[key]
    
    def delete(self, key: str) -> bool:
        """删除 Session"""
        with self._lock:
            return self._delete(key)
    
    def _delete(self, key: str) -> bool:
        """内部删除（无锁）"""
        if key in self._sessions:
            del self._sessions[key]
            self._expiry.pop(key, None)
            return True
        return False
    
    def _cleanup_expired(self):
        """清理过期 Session"""
        now = datetime.utcnow()
        expired_keys = [k for k, exp in self._expiry.items() if exp < now]
        for key in expired_keys:
            self._delete(key)
    
    def _evict_oldest(self):
        """淘汰最旧的 Session"""
        if not self._expiry:
            return
        oldest_key = min(self._expiry.keys(), key=lambda k: self._expiry[k])
        self._delete(oldest_key)


# 全局内存 Session 存储
_memory_session_store = MemorySessionStore()


class AuthService:
    """认证服务（支持 Redis 降级到内存 Session）"""
    
    def __init__(self, redis_repo: Optional[RedisRepository] = None):
        self.redis_repo = redis_repo or RedisRepository()
        self._redis_available = True  # Redis 可用标志
    
    async def _check_redis(self) -> bool:
        """检查 Redis 是否可用"""
        try:
            ok, _ = await self.redis_repo.ping()
            self._redis_available = ok
            return ok
        except Exception:
            self._redis_available = False
            return False
    
    @staticmethod
    def hash_password(password: str) -> str:
        """生成密码哈希"""
        password_bytes = password.encode('utf-8')
        salt = bcrypt.gensalt()
        return bcrypt.hashpw(password_bytes, salt).decode('utf-8')
    
    @staticmethod
    def verify_password(plain_password: str, hashed_password: str) -> bool:
        """验证密码"""
        try:
            password_bytes = plain_password.encode('utf-8')
            hashed_bytes = hashed_password.encode('utf-8')
            return bcrypt.checkpw(password_bytes, hashed_bytes)
        except Exception:
            return False
    
    async def authenticate(self, username: str, password: str) -> Optional[Admin]:
        """
        验证用户凭据
        返回: Admin 对象（成功）或 None（失败）
        """
        try:
            async with get_session_context() as session:
                result = await session.execute(
                    select(Admin).where(
                        Admin.username == username,
                        Admin.is_active == True
                    )
                )
                admin = result.scalar_one_or_none()
                
                if admin and self.verify_password(password, admin.password_hash):
                    # 更新最后登录时间
                    admin.last_login = datetime.utcnow()
                    await session.commit()
                    logger.info(f"管理员登录成功: {username}")
                    return admin
        except Exception as e:
            logger.warning(f"数据库认证异常，尝试使用环境变量进行降级验证: {e}")
            
            # 降级方案：检查环境变量中的管理员账户
            env_username = os.getenv("ADMIN_USERNAME", "admin")
            env_password = os.getenv("ADMIN_PASSWORD")
            
            # 注意：仅当环境变量中配置了密码时才允许降级登录
            if env_password and username == env_username and password == env_password:
                logger.info(f"管理员通过环境变量认证成功 (内存模式): {username}")
                # 返回一个临时的 Admin 对象用于会话创建
                return Admin(
                    id=0,
                    username=username,
                    is_active=True,
                    created_at=datetime.utcnow()
                )
            
        logger.warning(f"登录失败: {username}")
        return None
    
    async def create_session(self, admin_id: int, username: str) -> str:
        """
        创建登录会话
        
        优先使用 Redis，Redis 不可用时降级到内存 Session。
        返回: session_id
        """
        session_id = secrets.token_urlsafe(32)
        session_key = f"{SESSION_PREFIX}{session_id}"
        
        session_data = {
            "admin_id": str(admin_id),
            "username": username,
            "created_at": datetime.utcnow().isoformat(),
        }
        
        # 尝试存储到 Redis
        try:
            if self._redis_available:
                await self.redis_repo.hset(session_key, session_data)
                await self.redis_repo.expire(session_key, SESSION_EXPIRE_SECONDS)
                logger.info(f"会话已创建 (Redis): 管理员={username}")
                return session_id
        except Exception as e:
            logger.warning(f"Redis 创建会话失败，降级到内存: {e}")
            self._redis_available = False
        
        # 降级到内存 Session
        _memory_session_store.set(session_key, session_data, SESSION_EXPIRE_SECONDS)
        logger.info(f"会话已创建 (内存): 管理员={username}（Redis 降级）")
        return session_id
    
    async def get_session(self, session_id: str) -> Optional[dict]:
        """
        获取会话信息
        
        同时检查 Redis 和内存 Session。
        返回: session 数据字典 或 None
        """
        session_key = f"{SESSION_PREFIX}{session_id}"
        
        # 先尝试从 Redis 获取
        try:
            if self._redis_available:
                session_data = await self.redis_repo.hgetall(session_key)
                if session_data:
                    return session_data
        except Exception as e:
            logger.warning(f"Redis 获取会话失败: {e}")
            self._redis_available = False
        
        # 尝试从内存获取
        memory_data = _memory_session_store.get(session_key)
        if memory_data:
            return memory_data
        
        return None
    
    async def destroy_session(self, session_id: str) -> bool:
        """销毁会话（同时清除 Redis 和内存）"""
        session_key = f"{SESSION_PREFIX}{session_id}"
        destroyed = False
        
        # 尝试从 Redis 删除
        try:
            if self._redis_available:
                result = await self.redis_repo.delete(session_key)
                if result > 0:
                    destroyed = True
        except Exception as e:
            logger.warning(f"Redis 删除会话失败: {e}")
        
        # 同时从内存删除
        if _memory_session_store.delete(session_key):
            destroyed = True
        
        logger.info(f"会话已销毁: {session_id[:8]}...")
        return destroyed
    
    async def get_admin_by_id(self, admin_id: int) -> Optional[Admin]:
        """根据ID获取管理员信息"""
        try:
            async with get_session_context() as session:
                result = await session.execute(
                    select(Admin).where(Admin.id == admin_id)
                )
                return result.scalar_one_or_none()
        except Exception as e:
            logger.warning(f"获取管理员信息失败 (数据库不可用): {e}")
            # 如果是内存模式下的管理员 (ID=0)
            if admin_id == 0:
                env_username = os.getenv("ADMIN_USERNAME", "admin")
                return Admin(
                    id=0,
                    username=env_username,
                    is_active=True,
                    created_at=datetime.utcnow()
                )
            return None
    
    async def change_password(
        self, 
        admin_id: int, 
        old_password: str, 
        new_password: str
    ) -> tuple[bool, str]:
        """
        修改管理员密码
        
        Args:
            admin_id: 管理员ID
            old_password: 旧密码
            new_password: 新密码
        
        Returns:
            (是否成功, 错误信息)
        """
        try:
            async with get_session_context() as session:
                result = await session.execute(
                    select(Admin).where(Admin.id == admin_id)
                )
                admin = result.scalar_one_or_none()
                
                if not admin:
                    return False, "用户不存在"
                
                # 验证旧密码
                if not self.verify_password(old_password, admin.password_hash):
                    logger.warning(f"修改密码失败：旧密码错误，用户={admin.username}")
                    return False, "旧密码错误"
                
                # 验证新密码长度
                if len(new_password) < 6:
                    return False, "新密码长度不能少于6位"
                
                # 更新密码
                admin.password_hash = self.hash_password(new_password)
                await session.commit()
                
                logger.info(f"管理员密码已修改: {admin.username}")
                return True, "密码修改成功"
        except Exception as e:
            logger.error(f"修改密码失败 (数据库异常): {e}")
            return False, "数据库异常，暂时无法修改密码"
    
    async def update_admin_info(
        self,
        admin_id: int,
        username: str = None,
        email: str = None,
    ) -> tuple[bool, str]:
        """
        更新管理员信息
        
        Args:
            admin_id: 管理员ID
            username: 新用户名（可选）
            email: 新邮箱（可选）
        
        Returns:
            (是否成功, 错误信息)
        """
        try:
            async with get_session_context() as session:
                result = await session.execute(
                    select(Admin).where(Admin.id == admin_id)
                )
                admin = result.scalar_one_or_none()
                
                if not admin:
                    return False, "用户不存在"
                
                # 检查用户名是否已被占用
                if username and username != admin.username:
                    existing = await session.execute(
                        select(Admin).where(Admin.username == username)
                    )
                    if existing.scalar_one_or_none():
                        return False, "用户名已被占用"
                    admin.username = username
                
                if email is not None:
                    admin.email = email
                
                await session.commit()
                logger.info(f"管理员信息已更新: {admin.username}")
                return True, "信息更新成功"
        except Exception as e:
            logger.error(f"更新管理员信息失败 (数据库异常): {e}")
            return False, "数据库异常，暂时无法更新信息"


# 全局认证服务实例
_auth_service: Optional[AuthService] = None


def get_auth_service() -> AuthService:
    """获取认证服务实例"""
    global _auth_service
    if _auth_service is None:
        _auth_service = AuthService()
    return _auth_service


async def create_default_admin(username: str = None, password: str = None):
    """
    创建默认管理员账户（如果不存在）
    
    优先从环境变量读取:
        - ADMIN_USERNAME: 管理员用户名 (默认: admin)
        - ADMIN_PASSWORD: 管理员密码 (必须设置)
    """
    # 优先从环境变量读取
    env_username = os.getenv("ADMIN_USERNAME")
    env_password = os.getenv("ADMIN_PASSWORD")
    
    final_username = username or env_username or "admin"
    final_password = password or env_password
    
    # 如果没有设置密码，跳过创建
    if not final_password:
        logger.warning("未设置默认管理员密码（请设置 ADMIN_PASSWORD 环境变量），跳过创建")
        return None
    
    try:
        async with get_session_context() as session:
            result = await session.execute(
                select(Admin).where(Admin.username == final_username)
            )
            existing = result.scalar_one_or_none()
            
            if existing:
                logger.info(f"默认管理员 '{final_username}' 已存在")
                return existing
            
            # 创建新管理员
            password_hash = AuthService.hash_password(final_password)
            admin = Admin(
                username=final_username,
                password_hash=password_hash,
                email=None,
                is_active=True,
                created_at=datetime.utcnow(),
            )
            session.add(admin)
            await session.commit()
            
            logger.info(f"已创建默认管理员: {final_username}")
            return admin
    except Exception as e:
        logger.warning(f"由于数据库不可用，跳过在数据库中创建默认管理员: {e}")
        return None
