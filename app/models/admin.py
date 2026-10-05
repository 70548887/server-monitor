"""Admin 管理员模型"""

from datetime import datetime
from sqlalchemy import Column, Integer, String, Boolean, DateTime
from app.models.base import Base


class Admin(Base):
    """管理员账户表"""
    __tablename__ = "admins"
    
    id = Column(Integer, primary_key=True, autoincrement=True)
    username = Column(String(50), unique=True, nullable=False, index=True, comment="用户名")
    password_hash = Column(String(128), nullable=False, comment="密码哈希(bcrypt)")
    email = Column(String(100), nullable=True, comment="邮箱")
    is_active = Column(Boolean, default=True, nullable=False, comment="是否启用")
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False, comment="创建时间")
    last_login = Column(DateTime, nullable=True, comment="最后登录时间")
    
    def __repr__(self):
        return f"<Admin(id={self.id}, username={self.username}, is_active={self.is_active})>"
