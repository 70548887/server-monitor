"""
滑块验证码测试

覆盖此前实测确认的缺陷：
- 存储无界增长（200 次请求 → 200 条永久滞留，可被匿名请求打爆内存）
- 无过期时间
- 多进程部署失效（进程内字典）
- 背景图路径依赖 CWD（从其他目录启动会静默降级为纯色背景）
- 同一验证码可重放
- 位置容差过窄（5px）
"""

import asyncio
import sys
from datetime import datetime
from pathlib import Path

import pytest

from app.utils import captcha as C
from app.utils.captcha import (
    CAPTCHA_TTL_SECONDS,
    MEMORY_MAX_ENTRIES,
    POSITION_TOLERANCE,
    CaptchaStore,
    SliderCaptcha,
    _MemoryStore,
    check_captcha,
    create_captcha,
    validate_trail,
)

ROOT = Path(__file__).parent.parent


def _fresh_store() -> CaptchaStore:
    """构造一个确定走内存路径的 store（避免测试依赖 Redis）"""
    store = CaptchaStore()
    store._redis_available = False
    store._memory = _MemoryStore()
    return store


class TestBackgroundPath:
    """背景图路径"""

    def test_uses_absolute_path(self):
        """
        必须是绝对路径

        原实现用相对路径 os.path.join("app", "static", ...)，
        工作目录不对时静默降级为纯色随机背景 —— 那种背景极易被
        机器识别，且降级是静默的，没人察觉。
        """
        assert C.BG_DIR.is_absolute(), "BG_DIR 必须是绝对路径"

    def test_directory_resolves_from_any_cwd(self):
        """切换工作目录后仍能找到背景图"""
        import os

        original = os.getcwd()
        try:
            os.chdir("C:/" if Path("C:/").exists() else ROOT.parent)
            bg = SliderCaptcha()._load_local_bg()
            assert bg is not None, "从其他目录启动时找不到背景图"
        finally:
            os.chdir(original)

    def test_loads_real_background(self):
        bg = SliderCaptcha()._load_local_bg()
        assert bg is not None
        assert bg.size == (310, 155)


class TestMemoryStoreLimits:
    """内存降级存储的容量与过期"""

    def test_evicts_when_over_capacity(self):
        """
        超容量时淘汰旧条目

        这是防止 Redis 不可用时被匿名请求打爆内存的关键。
        原实现是无界 dict，200 次请求就是 200 条永久驻留。
        """
        store = _MemoryStore(max_entries=10)
        for i in range(50):
            store.save(f"k{i}", {"x": i}, ttl=120)
        assert len(store) <= 10, f"超容量未淘汰: {len(store)} 条"

    def test_default_capacity_is_bounded(self):
        store = _MemoryStore()
        assert store._max == MEMORY_MAX_ENTRIES
        assert MEMORY_MAX_ENTRIES > 0

    def test_expires_entries(self):
        store = _MemoryStore()
        store.save("a", {"x": 1}, ttl=120)
        assert store.get("a") is not None
        store.save("a", {"x": 1}, ttl=-1)  # 立即过期
        assert store.get("a") is None

    def test_missing_key_returns_none(self):
        assert _MemoryStore().get("never-existed") is None


class TestCaptchaStore:
    """存储层"""

    async def test_save_and_get(self):
        store = _fresh_store()
        await store.save("id1", {"x": 42, "y": 7}, ttl=120)
        assert await store.get("id1") == {"x": 42, "y": 7}

    async def test_delete(self):
        store = _fresh_store()
        await store.save("id1", {"x": 1}, ttl=120)
        assert await store.delete("id1") is True
        assert await store.get("id1") is None

    async def test_missing_returns_none(self):
        assert await _fresh_store().get("nope") is None

    async def test_key_is_namespaced(self):
        """Redis 键需加前缀，避免与业务键冲突"""
        assert CaptchaStore._key("abc") == "captcha:abc"


class TestCaptchaGeneration:
    """验证码生成"""

    async def test_answer_not_exposed(self):
        """返回给前端的字段不能包含答案 x"""
        cap = await create_captcha()
        assert "x" not in cap, "答案 x 被泄露给前端"
        assert set(cap) == {"id", "bg", "slider", "y", "tolerance"}

    async def test_tolerance_is_published(self):
        cap = await create_captcha()
        assert cap["tolerance"] == POSITION_TOLERANCE

    async def test_tolerance_wider_than_before(self):
        """容差从 5px 放宽到 8px，对触摸屏更友好"""
        assert POSITION_TOLERANCE >= 8

    async def test_images_are_data_urls(self):
        cap = await create_captcha()
        assert cap["bg"].startswith("data:image/png;base64,")
        assert cap["slider"].startswith("data:image/png;base64,")

    def test_puzzle_has_transparent_contour_inside_image(self):
        import base64
        import io
        from PIL import Image
        data = SliderCaptcha().generate()
        piece = Image.open(io.BytesIO(base64.b64decode(data["slider"])))
        background = Image.open(io.BytesIO(base64.b64decode(data["bg"])))
        assert piece.mode == "RGBA"
        assert piece.getchannel("A").getextrema() == (0, 255)
        assert data["x"] + piece.width < background.width
        assert data["y"] + piece.height < background.height

    async def test_fallback_background_has_texture(self):
        """
        降级背景也要有足够纹理

        原降级实现是浅色底 + 几条线，结构过于简单容易识别。
        """
        sc = SliderCaptcha()
        bg = sc._create_random_bg()
        assert bg.size == (310, 155)
        # 颜色种类应足够多样
        colors = bg.getcolors(maxcolors=100000)
        assert colors is not None and len(colors) > 100, "降级背景纹理过于简单"


class TestPositionValidation:
    """位置校验与容差"""

    async def _make(self, pre_verify=False):
        """创建验证码；pre_verify=True 时先做一次前端校验以取得 passed 标记"""
        cap = await create_captcha()
        rec = await C.store.get(cap["id"])
        if pre_verify:
            await check_captcha(cap["id"], rec["x"], TestTrailValidation.human_trail(rec["x"]), consume=False)
        return cap["id"], rec["x"]

    async def test_exact_position_passes(self):
        cid, x = await self._make(pre_verify=True)
        ok, _ = await check_captcha(cid, x, TestTrailValidation.human_trail(x), consume=True)
        assert ok is True

    async def test_within_tolerance_passes(self):
        cid, x = await self._make(pre_verify=True)
        ok, _ = await check_captcha(
            cid, x + POSITION_TOLERANCE, TestTrailValidation.human_trail(x), consume=True
        )
        assert ok is True

    async def test_outside_tolerance_fails(self):
        cid, x = await self._make(pre_verify=True)
        ok, reason = await check_captcha(
            cid, x + POSITION_TOLERANCE + 12, None, consume=True
        )
        assert ok is False
        assert reason

    async def test_invalid_coordinates(self):
        cid, _ = await self._make(pre_verify=True)
        ok, _ = await check_captcha(cid, "abc", None, consume=True)
        assert ok is False

    async def test_missing_params(self):
        ok, _ = await check_captcha(None, None)
        assert ok is False
        ok, _ = await check_captcha("id", None)
        assert ok is False

    async def test_expired_id_fails(self):
        ok, reason = await check_captcha("never-existed", 100, None, consume=True)
        assert ok is False
        assert "过期" in reason


class TestReplayProtection:
    """用完即焚"""

    async def test_consume_prevents_replay(self):
        """
        同一验证码不可重复用于登录

        原实现 verify 端点不 pop 记录，可被反复试探；
        且登录失败后验证码仍有效，等于给了攻击者免费的 oracle。
        """
        cap = await create_captcha()
        rec = await C.store.get(cap["id"])
        x = rec["x"]

        # 前端校验取得标记
        first, _ = await check_captcha(cap["id"], x, TestTrailValidation.human_trail(x), consume=False)
        assert first is True

        # 登录校验
        login_ok, _ = await check_captcha(cap["id"], x, TestTrailValidation.human_trail(x), consume=True)
        assert login_ok is True

        # 重放
        second, reason = await check_captcha(cap["id"], x, TestTrailValidation.human_trail(x), consume=True)
        assert second is False
        assert "过期" in reason

    async def test_failed_attempt_also_consumes(self):
        """
        失败也消耗

        否则用户可以无限次在同一个验证码上试不同坐标。
        """
        cap = await create_captcha()
        rec = await C.store.get(cap["id"])

        ok, _ = await check_captcha(
            cap["id"], rec["x"] + 200, None, consume=True
        )
        assert ok is False

        # 该验证码已被销毁
        assert await C.store.get(cap["id"]) is None

    async def test_non_consuming_check_keeps_record(self):
        """实时校验阶段不销毁（登录时还要再验一次）"""
        cap = await create_captcha()
        rec = await C.store.get(cap["id"])

        ok, _ = await check_captcha(cap["id"], rec["x"], TestTrailValidation.human_trail(rec["x"]), consume=False)
        assert ok is True
        assert await C.store.get(cap["id"]) is not None


class TestTrailValidation:
    """反机器人轨迹校验"""

    @staticmethod
    def human_trail(target=200, seed=42):
        """构造一段像真人的轨迹：慢起步、加速、减速、轻微抖动"""
        import random

        random.seed(seed)
        pts = [[0, 0]]
        t, x = 0, 0
        while x < target:
            t += random.randint(12, 40)
            x = min(target, x + random.choice([6, 8, 10, 12, 14, 10, 8, 6]))
            pts.append([x, t])
        return pts

    def test_human_trail_accepted(self):
        assert validate_trail(self.human_trail(), 200) is True

    def test_perfect_linear_rejected(self):
        """
        等速直线必被拒绝

        脚本按时间插值设置 left 属性时，轨迹是完美的等差数列。
        """
        linear = [[i * 20, i * 15] for i in range(10)]
        assert validate_trail(linear, 200) is False

    def test_instant_jump_rejected(self):
        """瞬间跳到终点"""
        assert validate_trail([[0, 0], [200, 30]], 200) is False

    def test_backward_drag_rejected(self):
        """往回拖是明显异常"""
        trail = [[0, 0], [100, 50], [50, 100], [200, 150]]
        assert validate_trail(trail, 200) is False

    def test_too_few_points_rejected(self):
        assert validate_trail([[0, 0], [100, 100]], 200) is False

    def test_non_monotonic_time_rejected(self):
        trail = [[0, 100], [50, 50], [100, 120], [150, 130], [200, 140]]
        assert validate_trail(trail, 200) is False

    def test_malformed_points_rejected(self):
        assert validate_trail([[0], [1, 2]], 200) is False
        assert validate_trail("not-a-trail", 200) is False

    def test_absent_trail_rejected(self):
        """轨迹是必填验证数据。"""
        assert validate_trail(None, 200) is False
        assert validate_trail([], 200) is False

    async def test_fake_trail_blocks_login(self):
        """端到端：脚本用等速轨迹应被拦截"""
        cap = await create_captcha()
        rec = await C.store.get(cap["id"])
        linear = [[i * 21, i * 15] for i in range(10)]

        ok, reason = await check_captcha(
            cap["id"], rec["x"], linear, consume=False
        )
        assert ok is False
        assert "轨迹" in reason


class TestConstants:
    """常量配置"""

    def test_ttl_is_bounded(self):
        """验证码不该永久有效"""
        assert 30 <= CAPTCHA_TTL_SECONDS <= 600

    def test_memory_cap_sane(self):
        assert 100 <= MEMORY_MAX_ENTRIES <= 100_000


class TestAuditCaptchaRegressions:
    async def test_failed_preview_consumes_challenge(self):
        cap = await create_captcha()
        record = await C.store.get(cap["id"])
        trail = TestTrailValidation.human_trail(record["x"])
        assert (await check_captcha(cap["id"], 0, trail))[0] is False
        assert (await check_captcha(cap["id"], record["x"], trail))[0] is False

    async def test_empty_trail_cannot_verify(self):
        cap = await create_captcha()
        record = await C.store.get(cap["id"])
        assert (await check_captcha(cap["id"], record["x"], None))[0] is False

    async def test_concurrent_consumers_only_one_succeeds(self):
        import asyncio
        cap = await create_captcha()
        record = await C.store.get(cap["id"])
        trail = TestTrailValidation.human_trail(record["x"])
        assert (await check_captcha(cap["id"], record["x"], trail))[0]
        outcomes = await asyncio.gather(*(check_captcha(cap["id"], record["x"], trail, consume=True) for _ in range(2)))
        assert sum(ok for ok, _ in outcomes) == 1

    def test_middle_of_trail_is_signed(self):
        original = [[0, 0], [50, 100], [200, 400]]
        altered = [[0, 0], [99, 100], [200, 400]]
        assert C._trail_signature(original) != C._trail_signature(altered)

    def test_nonfinite_and_wrong_endpoint_rejected(self):
        trail = TestTrailValidation.human_trail(200)
        assert not validate_trail(trail, 100)
        trail[5][0] = float("nan")
        assert not validate_trail(trail, 200)


class TestVerifiedMarker:
    """
    通过标记

    两阶段设计带来的新问题：非 consume 模式若既不销毁也不标记，
    同一个 captcha_id 就能被反复调用 verify 试探位置
    （实测中攻击者可借此二分搜索 x）。因此通过后必须留下凭证。
    """

    async def test_login_requires_prior_verify(self):
        """未做前端校验直接提交登录应被拒"""
        cap = await create_captcha()
        rec = await C.store.get(cap["id"])

        ok, reason = await check_captcha(
            cap["id"], rec["x"], TestTrailValidation.human_trail(rec["x"]), consume=True
        )
        assert ok is False
        assert "人机验证" in reason

    async def test_verify_marks_passed(self):
        """verify 通过后应写入标记"""
        cap = await create_captcha()
        rec = await C.store.get(cap["id"])

        ok, _ = await check_captcha(cap["id"], rec["x"], TestTrailValidation.human_trail(rec["x"]), consume=False)
        assert ok is True

        marked = await C.store.get(cap["id"])
        assert marked is not None
        assert marked.get("passed") is True

    async def test_login_after_verify_succeeds(self):
        """标记存在时登录校验可通过"""
        cap = await create_captcha()
        rec = await C.store.get(cap["id"])

        await check_captcha(cap["id"], rec["x"], TestTrailValidation.human_trail(rec["x"]), consume=False)
        ok, _ = await check_captcha(
            cap["id"], rec["x"], TestTrailValidation.human_trail(rec["x"]), consume=True
        )
        assert ok is True

    async def test_login_consumes_marked_record(self):
        """登录校验后记录被销毁"""
        cap = await create_captcha()
        rec = await C.store.get(cap["id"])

        await check_captcha(cap["id"], rec["x"], TestTrailValidation.human_trail(rec["x"]), consume=False)
        await check_captcha(cap["id"], rec["x"], TestTrailValidation.human_trail(rec["x"]), consume=True)

        assert await C.store.get(cap["id"]) is None

    async def test_trail_tampering_rejected(self):
        """
        轨迹替换攻击应被拦截

        用真人轨迹通过 verify，登录时换成另一条 ——
        指纹不同即判定数据被篡改。
        """
        from test_captcha import TestTrailValidation

        cap = await create_captcha()
        rec = await C.store.get(cap["id"])
        x = rec["x"]

        good = TestTrailValidation.human_trail(x, seed=1)
        await check_captcha(cap["id"], x, good, consume=False)

        tampered = TestTrailValidation.human_trail(x, seed=2)
        ok, reason = await check_captcha(
            cap["id"], x, tampered, consume=True
        )
        assert ok is False
        assert "不一致" in reason

    def test_trail_signature_detects_difference(self):
        a = [[0, 0], [50, 100], [200, 400]]
        b = [[0, 0], [50, 100], [200, 999]]
        assert C._trail_signature(a) != C._trail_signature(b)
        assert C._trail_signature(a) == C._trail_signature(
            [[0, 0], [50, 100], [200, 400]]
        )

    def test_trail_signature_handles_garbage(self):
        """畸形输入不应抛异常"""
        assert C._trail_signature(None) == "empty"
        assert C._trail_signature([]) == "empty"
        assert C._trail_signature("x") == "empty"   # 非 list
        assert C._trail_signature([[1]]) == "invalid"  # 元素缺字段


class TestRouteContract:
    """路由层契约"""

    def test_auth_route_uses_new_api(self):
        """登录路由应使用 check_captcha 而非已移除的 verify_captcha"""
        import inspect

        from app.routes import auth

        src = inspect.getsource(auth)
        assert "check_captcha" in src
        # 注意不能用裸子串匹配：路由函数名 api_verify_captcha 本身
        # 就包含 verify_captcha。这里匹配的是"被调用"的形态。
        assert "await verify_captcha(" not in src, "仍在调用已移除的 verify_captcha"

    def test_login_consumes_captcha(self):
        """登录必须 consume=True，否则可重放"""
        import inspect

        from app.routes import auth

        src = inspect.getsource(auth.login)
        assert "consume=True" in src, "登录时未用掉验证码"

    def test_template_has_trail_field(self):
        """表单需提交轨迹字段"""
        html = (ROOT / "app" / "templates" / "login.html").read_text(
            encoding="utf-8"
        )
        assert 'name="captcha_trail"' in html
