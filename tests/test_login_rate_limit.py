"""登录页访问不得消耗密码提交额度；真实触发限流时保留可用页面。"""
import os
import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from starlette.requests import Request

from app.middleware import security
from app.routes import auth
from app.main import app
from test_routes import make_client


@pytest.fixture
def limiter(monkeypatch):
    store = security.RateLimitStore()
    store._redis_available = False
    monkeypatch.setattr(security, "rate_limit_store", store)
    return store


def login_data():
    return {"username": "test-user", "password": "fixture-only"}


async def test_page_refreshes_do_not_spend_login_submission_budget(limiter, monkeypatch):
    fake_auth = SimpleNamespace(
        authenticate=AsyncMock(return_value=SimpleNamespace(id=42, username="test-user")),
        create_session=AsyncMock(return_value="fixture-session"),
    )
    monkeypatch.setattr(auth, "check_captcha", AsyncMock(return_value=(True, "")))
    app.dependency_overrides[auth.get_auth_service] = lambda: fake_auth
    try:
        async with make_client() as client:
            for _ in range(20):
                assert (await client.get("/login")).status_code == 200
            response = await client.post("/login", data={**login_data(), "captcha_id": "fixture-captcha", "captcha_x": "150"})
            assert response.status_code == 302
            assert response.headers["location"] == "/"
            assert response.cookies["session_id"] == "fixture-session"
        fake_auth.authenticate.assert_awaited_once()
    finally:
        app.dependency_overrides.pop(auth.get_auth_service, None)


async def test_favicon_does_not_redirect_or_count_as_login(limiter):
    async with make_client() as client:
        for _ in range(12):
            response = await client.get("/favicon.ico")
            assert response.status_code == 200
            assert "image/svg+xml" in response.headers["content-type"]
            assert "location" not in response.headers
    assert not limiter._requests


async def test_real_submission_limit_still_blocks_eleventh_attempt(limiter):
    async with make_client() as client:
        for _ in range(10):
            # 缺验证码会由路由拒绝，但每次仍消耗登录提交额度。
            assert (await client.post("/login", data=login_data())).status_code == 400
        response = await client.post("/login", data=login_data())
        assert response.status_code == 429
        assert response.json()["retry_after"] == 60
        assert response.headers["Retry-After"] == "60"
        # 提交被限流后仍然可以打开登录页。
        assert (await client.get("/login")).status_code == 200


async def test_browser_limit_response_is_login_page_not_raw_json(limiter):
    async with make_client() as client:
        for _ in range(10):
            await client.post("/login", data=login_data())
        response = await client.post("/login", data=login_data(), headers={"Accept": "text/html,application/xhtml+xml"})
        assert response.status_code == 429
        assert response.headers["content-type"].startswith("text/html")
        assert 'id="loginForm"' in response.text
        assert 'id="captchaDialog"' in response.text
        assert "登录提交过于频繁" in response.text
        assert '"detail"' not in response.text
        assert response.headers["Cache-Control"] == "no-store"
        assert "fixture-only" not in response.text


async def test_submission_budget_recovers_after_window(limiter, monkeypatch):
    current = [10000.0]
    monkeypatch.setattr(security.time, "time", lambda: current[0])
    for _ in range(10):
        assert not await limiter.is_rate_limited("test:login-submit", 10, 60)
    assert await limiter.is_rate_limited("test:login-submit", 10, 60)
    current[0] += 61
    assert not await limiter.is_rate_limited("test:login-submit", 10, 60)


async def test_account_failure_lock_remains_enforced(limiter):
    for _ in range(5):
        await limiter.record_login_failure("127.0.0.1")
    async with make_client() as client:
        assert (await client.get("/login")).status_code == 200
        response = await client.post("/login", data=login_data(), headers={"Accept": "text/html"})
        assert response.status_code == 429
        assert "登录尝试次数过多" in response.text
        assert int(response.headers["Retry-After"]) > 60


def test_login_methods_and_trailing_slash_have_separate_buckets():
    middleware = security.SecurityMiddleware(app)
    assert middleware._get_rate_limit("/login", "POST") == (10, 60, "login-submit")
    assert middleware._get_rate_limit("/login/", "POST") == (10, 60, "login-submit")
    assert middleware._get_rate_limit("/login", "GET") == (100, 60, "login-page")
    assert middleware._get_rate_limit("/api/nodes", "GET") == (200, 60, "/api/")


def test_api_response_stays_json_even_with_html_accept():
    middleware = security.SecurityMiddleware(app)
    request = Request({"type": "http", "path": "/api/nodes", "method": "GET", "headers": [(b"accept", b"text/html")]})
    response = middleware._rate_limit_response(request, "请求过于频繁", 60)
    assert response.headers["content-type"].startswith("application/json")
    assert response.status_code == 429


@pytest.mark.skipif(os.getenv("RUN_REDIS_INTEGRATION") != "1", reason="显式启用Redis隔离键测试")
async def test_real_redis_separates_page_and_submit_counts(monkeypatch):
    from app.repositories.redis_repository import RedisRepository
    repo = RedisRepository()
    store = security.RateLimitStore()
    store._prefix = "test-login-" + uuid.uuid4().hex + ":"
    monkeypatch.setattr(security, "get_redis_repository", lambda: repo)
    client = await repo._get_client()
    page_key, submit_key = "127.0.0.1:login-page", "127.0.0.1:login-submit"
    keys = [f"{repo._prefix}node:{store._prefix}req:{key}:errors" for key in (page_key, submit_key)]
    try:
        for _ in range(20):
            assert not await store.is_rate_limited(page_key, 100, 60)
        for _ in range(10):
            assert not await store.is_rate_limited(submit_key, 10, 60)
        assert await store.is_rate_limited(submit_key, 10, 60)
        assert not await store.is_rate_limited(page_key, 100, 60)
        for key in keys:
            assert 0 < await client.ttl(key) <= 60
    finally:
        await client.delete(*keys)  # 只清理本测试随机前缀的两个键。
        await client.aclose()
