"""Admin 管理员模型"""

from sqlalchemy import Column, Integer, String, Boolean, DateTime
from app.models.base import Base
from app.utils.timeutil import utcnow


class Admin(Base):
    """管理员账户表"""
    __tablename__ = "admins"

    id = Column(Integer, primary_key=True, autoincrement=True)
    username = Column(String(50), unique=True, nullable=False, index=True, comment="用户名")
    password_hash = Column(String(128), nullable=False, comment="密码哈希(bcrypt)")
    email = Column(String(100), nullable=True, comment="邮箱")
    is_active = Column(Boolean, default=True, nullable=False, comment="是否启用")
    # 用应用层的 utcnow() 而非 func.now()（MySQL 本地时区）：
    # 时间约定见 app/utils/timeutil.py 的模块文档
    created_at = Column(DateTime, default=utcnow, nullable=False, comment="创建时间(UTC)")
    last_login = Column(DateTime, nullable=True, comment="最后登录时间(UTC)")

    def __repr__(self):
        return f"<Admin(id={self.id}, username={self.username}, is_active={self.is_active})>"
