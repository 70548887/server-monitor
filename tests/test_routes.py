"""
路由层测试

用 httpx.ASGITransport 直接调用 ASGI 应用，不启动服务器、不连数据库。
只覆盖无需认证的公开端点，以及认证边界本身的行为。

注意：app.main 会导入通知路由，后者在类定义时校验 EmailStr，
因此需要 requirements.txt 中的 email-validator。缺失时本文件的
应用级测试会被跳过（而不是整体收集失败），并给出明确的安装提示。
"""

import importlib.util

import pytest
import httpx

_EMAIL_VALIDATOR_MISSING = importlib.util.find_spec("email_validator") is None

if _EMAIL_VALIDATOR_MISSING:
    app = None
else:
    from app.main import app


requires_app = pytest.mark.skipif(
    _EMAIL_VALIDATOR_MISSING,
    reason="需要 email-validator（requirements.txt 中的运行依赖）："
           "pip install 'pydantic[email]'",
)


def collect_paths(routes, out):
    """
    递归收集路由路径，兼容 Starlette 的嵌套路由容器

    include_router 的实现随版本变化：旧版把路由展平追加到 app.routes，
    新版放入 _IncludedRouter（原始 router 在 original_router 上）。
    两种形态都要支持，否则路由注册断言会在升级后集体失效。
    """
    for r in routes:
        path = getattr(r, "path", None)
        if isinstance(path, str):
            out.add(path)
            continue

        # Starlette >= 1.x 的嵌套容器
        original = getattr(r, "original_router", None)
        if original is not None and hasattr(original, "routes"):
            collect_paths(original.routes, out)
            continue

        # 其他可能的嵌套形态
        inner = getattr(r, "router", None)
        if inner is not None and hasattr(inner, "routes"):
            collect_paths(inner.routes, out)
            continue

        sub = getattr(r, "routes", None)
        if sub:
            collect_paths(sub, out)


def make_client():
    """构造直连 ASGI 应用的测试客户端"""
    transport = httpx.ASGITransport(app=app)
    return httpx.AsyncClient(transport=transport, base_url="http://test")


def make_authed_client(monkeypatch):
    """
    构造"已登录"的测试客户端

    直接替换会话校验，跳过 Redis / MySQL —— 这里要测的是路由与
    中间件行为，不是认证本身（认证逻辑由 test_auth.py 覆盖）。
    """
    from app.services.auth import AuthService

    async def fake_get_session(self, session_id):
        if session_id == "valid-test-session":
            return {"admin_id": 1, "username": "test-admin"}
        return None

    monkeypatch.setattr(AuthService, "get_session", fake_get_session)
    transport = httpx.ASGITransport(app=app)
    return httpx.AsyncClient(
        transport=transport,
        base_url="http://test",
        cookies={"session_id": "valid-test-session"},
    )


@requires_app
class TestHealthRoutes:
    """健康探针"""

    async def test_liveness_probe(self):
        async with make_client() as client:
            res = await client.get("/healthz/live")

        assert res.status_code == 200
        body = res.json()
        assert body["status"] == "alive"
        assert "timestamp" in body

    async def test_healthz_returns_status(self):
        """综合健康检查返回可识别的状态字段"""
        async with make_client() as client:
            res = await client.get("/healthz")

        # 无数据库时可能 degraded / unhealthy，但必须是合法响应
        assert res.status_code in (200, 503)
        body = res.json()
        assert "status" in body

    async def test_readiness_probe_ready(self):
        """
        就绪探针返回 200（就绪）或 503（未就绪）

        响应体含 ready / checks，不是 status —— /healthz 才有 status 字段。
        """
        async with make_client() as client:
            res = await client.get("/healthz/ready")

        assert res.status_code in (200, 503)
        body = res.json()
        assert "ready" in body
        assert "checks" in body

    async def test_scheduler_health(self):
        """调度器健康端点可访问（无需认证）"""
        async with make_client() as client:
            res = await client.get("/healthz/scheduler")

        assert res.status_code == 200
        assert isinstance(res.json(), dict)


@requires_app
class TestAuthBoundary:
    """认证边界"""

    async def test_login_page_is_public(self):
        async with make_client() as client:
            res = await client.get("/login")

        assert res.status_code == 200
        assert "登录" in res.text

    async def test_protected_ui_redirects_to_login(self):
        """未登录访问受保护页面应重定向到登录页"""
        async with make_client() as client:
            res = await client.get("/ui/dashboard")

        assert res.status_code == 302
        assert "/login" in res.headers["location"]

    async def test_protected_api_returns_401(self):
        """未登录访问 API 返回 401 而不是重定向（前端需要 JSON）"""
        async with make_client() as client:
            res = await client.get("/api/nodes")

        assert res.status_code == 401
        assert "未登录" in res.json()["detail"] or "会话" in res.json()["detail"]

    async def test_nodes_api_requires_auth(self):
        async with make_client() as client:
            res = await client.get("/api/nodes")

        assert res.status_code == 401

    async def test_metrics_requires_auth(self):
        """Prometheus 抓取端点默认受保护"""
        async with make_client() as client:
            res = await client.get("/metrics")

        assert res.status_code == 401

    async def test_captcha_is_public(self):
        """验证码接口必须在登录前可访问，否则无法登录"""
        async with make_client() as client:
            res = await client.get("/api/auth/captcha")

        assert res.status_code == 200
        body = res.json()
        assert "captcha_id" in body or "id" in body

    async def test_invalid_session_rejected(self):
        """伪造的 session cookie 应被拒绝"""
        async with make_client() as client:
            res = await client.get(
                "/api/nodes",
                cookies={"session_id": "forged-session-value"},
            )

        assert res.status_code == 401

    async def test_bearer_token_accepted_channel(self):
        """Authorization 头是受支持的会话传递方式（无效值同样 401）"""
        async with make_client() as client:
            res = await client.get(
                "/api/nodes",
                headers={"Authorization": "Bearer invalid-token"},
            )

        assert res.status_code == 401


class TestModuleImports:
    """模块可导入性 —— 防止路由注册被破坏"""

    def test_alerting_service_import(self):
        from app.services.alerting import AlertingService, get_alerting_service

        assert callable(get_alerting_service)
        assert AlertingService is not None

    def test_availability_service_import(self):
        from app.services.availability import get_availability_service

        assert callable(get_availability_service)

    def test_incident_service_import(self):
        """Phase 2 新增的事件服务"""
        from app.services.incident import IncidentService, compute_next_notify_at

        assert callable(compute_next_notify_at)
        assert hasattr(IncidentService, "get_open_incident")
        assert hasattr(IncidentService, "defer_notification")


@requires_app
class TestNodeRoutes:
    """节点 API（已登录）"""

    async def test_list_nodes(self, monkeypatch):
        """节点列表返回数组（mock 数据库，避免连接超时）"""
        import app.routes.nodes as nodes_mod

        class FakeRepo:
            async def get_all_nodes(self, include_deleted=False):
                return []

        monkeypatch.setattr(nodes_mod, "_get_db_repository", lambda: FakeRepo())

        async with make_authed_client(monkeypatch) as client:
            res = await client.get("/api/nodes")

        assert res.status_code == 200
        assert isinstance(res.json(), list)

    async def test_get_node(self, monkeypatch):
        """查询不存在的节点返回 404 而不是 500"""
        import app.routes.nodes as nodes_mod

        class FakeRepo:
            async def get_node_by_name(self, name):
                return None

        monkeypatch.setattr(nodes_mod, "_get_db_repository", lambda: FakeRepo())

        async with make_authed_client(monkeypatch) as client:
            res = await client.get("/api/nodes/definitely-not-a-real-node")

        assert res.status_code == 404

    async def test_get_node_not_found(self, monkeypatch):
        import app.routes.nodes as nodes_mod

        class FakeRepo:
            async def get_node_by_name(self, name):
                return None

        monkeypatch.setattr(nodes_mod, "_get_db_repository", lambda: FakeRepo())

        async with make_authed_client(monkeypatch) as client:
            res = await client.get("/api/nodes/no-such-node-xyz")

        assert res.status_code == 404
        detail = res.json().get("detail")
        # 业务错误码 E003 = 节点不存在
        if isinstance(detail, dict):
            assert detail.get("code") == "E003"


@requires_app
class TestAlertRoutes:
    """告警 API（已登录）"""

    async def test_list_alerts(self, monkeypatch):
        """
        告警列表返回分页结构

        mock 掉数据库：本文件测的是路由与中间件行为，不是持久化。
        若真连 MySQL，无库环境下会因连接超时把整个套件拖慢数分钟。
        """
        import app.services.alerting as alerting_mod

        async def fake_get_logs(*args, **kwargs):
            return ([], 0)

        monkeypatch.setattr(
            alerting_mod.AlertLogService, "get_logs", fake_get_logs
        )

        async with make_authed_client(monkeypatch) as client:
            res = await client.get("/api/notifications/alerts/logs")

        assert res.status_code == 200
        body = res.json()
        assert body["alerts"] == []
        assert body["total"] == 0


@requires_app
class TestNotificationRoutes:
    """通知 API（已登录）"""

    async def test_list_channels(self, monkeypatch):
        """渠道列表返回数组（mock 数据库，避免连接超时）"""
        import app.services.notification as notif_mod

        async def fake_get_all(cls=None):
            return []

        monkeypatch.setattr(
            notif_mod.NotificationService, "get_all_channels", fake_get_all
        )

        async with make_authed_client(monkeypatch) as client:
            res = await client.get("/api/notifications/channels")

        assert res.status_code == 200
        assert isinstance(res.json(), list)


@requires_app
class TestAvailabilityRoutes:
    """可用性 API（已登录）"""

    async def test_get_availability_stats(self, monkeypatch):
        """统计接口不应因缺少数据库而变成 401/404"""
        async with make_authed_client(monkeypatch) as client:
            res = await client.get("/api/nodes/stats/availability")

        assert res.status_code != 401


@requires_app
class TestModuleImportsWithApp:
    """需要完整应用（含通知路由）的导入检查"""

    def test_nodes_module_import(self):
        from app.routes import nodes

        assert hasattr(nodes, "router")

    def test_notifications_module_import(self):
        from app.routes import notifications

        assert hasattr(notifications, "router")


@requires_app
class TestRouteRegistration:
    """路由注册完整性"""

    def _paths(self):
        """
        收集全部已注册路径

        Starlette >= 1.x 的 include_router 不再把路由展平到 app.routes，
        而是放入 _IncludedRouter 容器（通过 original_router 访问原始
        router）。因此不能直接 {r.path for r in app.routes} —— 那会漏掉
        全部业务路由并抛 AttributeError。
        """
        out = set()
        collect_paths(app.routes, out)
        return out

    def test_health_routes_registered(self):
        paths = self._paths()
        for p in ("/healthz", "/healthz/live", "/healthz/ready"):
            assert p in paths

    def test_ui_routes_registered(self):
        paths = self._paths()
        for p in ("/ui/dashboard", "/ui/nodes", "/ui/alerts",
                  "/ui/notifications", "/ui/trends", "/ui/"):
            assert p in paths, f"缺少路由 {p}"

    def test_node_api_registered(self):
        paths = self._paths()
        for p in ("/api/nodes", "/api/nodes/{name}", "/api/nodes/{name}/ping",
                  "/api/nodes/stats/availability", "/api/nodes/stats/latency-trend"):
            assert p in paths, f"缺少路由 {p}"

    def test_notification_api_registered(self):
        paths = self._paths()
        for p in ("/api/notifications/channels", "/api/notifications/logs",
                  "/api/notifications/recipients", "/api/notifications/alerts/config",
                  "/api/notifications/alerts/logs"):
            assert p in paths, f"缺少路由 {p}"

    def test_incident_api_registered(self):
        """Phase 2/3 新增的事件端点"""
        paths = self._paths()
        for p in ("/api/notifications/incidents",
                  "/api/notifications/incidents/stats",
                  "/api/notifications/incidents/{incident_id}",
                  "/api/notifications/incidents/{incident_id}/notifications"):
            assert p in paths, f"缺少路由 {p}"

    def test_incident_stats_before_dynamic_route(self):
        """
        /incidents/stats 必须注册在 /incidents/{id} 之前

        否则 Starlette 会把 "stats" 当成 incident_id 匹配，
        导致统计接口返回 422。
        """
        out = set()
        collect_paths(app.routes, out)
        paths = self._ordered_paths()
        stats_idx = paths.index("/api/notifications/incidents/stats")
        dynamic_idx = paths.index("/api/notifications/incidents/{incident_id}")
        assert stats_idx < dynamic_idx, "动态路由抢先匹配，统计接口会失效"

    def _ordered_paths(self):
        """按注册顺序展开路由路径（需递归，Starlette 1.x 为嵌套结构）"""
        ordered = []

        def walk(routes):
            for r in routes:
                path = getattr(r, "path", None)
                if isinstance(path, str):
                    ordered.append(path)
                    continue
                original = getattr(r, "original_router", None)
                if original is not None and hasattr(original, "routes"):
                    walk(original.routes)
                    continue
                inner = getattr(r, "router", None)
                if inner is not None and hasattr(inner, "routes"):
                    walk(inner.routes)
                    continue
                sub = getattr(r, "routes", None)
                if sub:
                    walk(sub)

        walk(app.routes)
        return ordered

    def test_robots_and_anti_crawling(self):
        paths = self._paths()
        assert "/robots.txt" in paths
        assert "/anti-crawling.txt" in paths


@requires_app
class TestAlertConfigSchema:
    """告警配置接口的数据契约"""

    def test_update_schema_accepts_cadence_fields(self):
        """提醒节奏字段必须能被请求体解析"""
        from app.routes.notifications import AlertConfigUpdate

        payload = AlertConfigUpdate(
            reminder_intervals_minutes=[0, 60, 240],
            intervals_loop_minutes=720,
            quiet_hours_enabled=True,
            quiet_hours_start="23:00",
            quiet_hours_end="07:00",
        )
        assert payload.reminder_intervals_minutes == [0, 60, 240]
        assert payload.quiet_hours_enabled is True

    def test_update_schema_all_fields_optional(self):
        """部分更新必须允许省略字段，否则面板无法只保存一个开关"""
        from app.routes.notifications import AlertConfigUpdate

        payload = AlertConfigUpdate()
        assert payload.enabled is None
        assert payload.reminder_intervals_minutes is None

    def test_alert_log_response_has_incident_fields(self):
        """告警记录响应需带事件字段，前端才能显示「第 N 次提醒」"""
        from app.routes.notifications import AlertLogResponse

        assert "incident_id" in AlertLogResponse.model_fields
        assert "notify_seq" in AlertLogResponse.model_fields
