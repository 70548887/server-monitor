"""认证与会话存储测试"""

import threading
import time
from datetime import datetime, timedelta

import pytest

from app.services.auth import AuthService, MemorySessionStore


class TestPasswordHashing:
    """密码哈希"""

    def test_hash_password(self):
        hashed = AuthService.hash_password("secret123")
        assert hashed
        assert hashed != "secret123"
        # bcrypt 哈希带算法标识与前缀
        assert hashed.startswith("$2")

    def test_verify_password_correct(self):
        hashed = AuthService.hash_password("secret123")
        assert AuthService.verify_password("secret123", hashed) is True

    def test_verify_password_incorrect(self):
        hashed = AuthService.hash_password("secret123")
        assert AuthService.verify_password("wrong-password", hashed) is False

    def test_same_password_different_hashes(self):
        """同一密码两次哈希结果不同（盐值随机）"""
        h1 = AuthService.hash_password("secret123")
        h2 = AuthService.hash_password("secret123")
        assert h1 != h2
        # 但都能验证通过
        assert AuthService.verify_password("secret123", h1)
        assert AuthService.verify_password("secret123", h2)

    def test_different_passwords_different_hashes(self):
        h1 = AuthService.hash_password("password-a")
        h2 = AuthService.hash_password("password-b")
        assert h1 != h2

    def test_verify_rejects_malformed_hash(self):
        """非法哈希不应抛异常，只返回 False"""
        assert AuthService.verify_password("x", "not-a-bcrypt-hash") is False
        assert AuthService.verify_password("x", "") is False

    def test_unicode_password(self):
        """中文密码应能正常往返"""
        hashed = AuthService.hash_password("密码测试123")
        assert AuthService.verify_password("密码测试123", hashed)


class TestMemorySessionStore:
    """内存 Session 存储"""

    @pytest.fixture
    def store(self):
        return MemorySessionStore()

    def test_set_and_get(self, store):
        store.set("session-1", {"user": "admin"}, expire_seconds=3600)
        data = store.get("session-1")
        assert data == {"user": "admin"}

    def test_get_nonexistent(self, store):
        assert store.get("never-created") is None

    def test_delete(self, store):
        store.set("s1", {"user": "admin"}, 3600)
        assert store.delete("s1") is True
        assert store.get("s1") is None

    def test_delete_nonexistent(self, store):
        assert store.delete("never-created") is False

    def test_expiry(self, store):
        """过期后读取不到"""
        store.set("s1", {"user": "admin"}, expire_seconds=1)
        assert store.get("s1") is not None

        time.sleep(1.1)
        assert store.get("s1") is None

    def test_cleanup_expired(self, store):
        """过期条目会被清理，不残留内存"""
        store.set("short", {"user": "a"}, expire_seconds=1)
        store.set("long", {"user": "b"}, expire_seconds=3600)

        time.sleep(1.1)
        # 触发一次 set 会顺带清理
        store.set("trigger", {"user": "c"}, 3600)

        assert "short" not in store._sessions
        assert "long" in store._sessions

    def test_max_sessions_limit(self):
        """超过上限时淘汰最旧的会话，防止内存无限增长"""
        store = MemorySessionStore(max_sessions=5)

        for i in range(10):
            store.set(f"s{i}", {"user": f"u{i}"}, expire_seconds=3600)
            time.sleep(0.01)  # 保证过期时间可区分

        assert len(store._sessions) <= 5
        # 最新的应保留，最旧的应被淘汰
        assert store.get("s9") is not None
        assert store.get("s0") is None

    def test_thread_safety(self, store):
        """并发读写不丢失、不报错"""
        errors = []

        def worker(prefix):
            try:
                for i in range(50):
                    key = f"{prefix}-{i}"
                    store.set(key, {"n": i}, expire_seconds=3600)
                    assert store.get(key) is not None
            except Exception as e:  # pragma: no cover
                errors.append(e)

        threads = [threading.Thread(target=worker, args=(p,)) for p in ("a", "b", "c")]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert errors == []
        assert len(store._sessions) == 150

    def test_update_existing_session(self, store):
        """覆盖同一 key 不应产生重复条目"""
        store.set("s1", {"v": 1}, 3600)
        store.set("s1", {"v": 2}, 3600)
        assert store.get("s1") == {"v": 2}
        assert len(store._sessions) == 1
