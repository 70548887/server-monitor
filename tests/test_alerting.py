"""
告警评估与故障事件测试

覆盖本次改造的三条主线：
    1. 事件化提醒：一次故障 = 一个事件，节奏由 next_notify_at 闸门控制
    2. 跨午夜行为：不再因为自然日边界重新爆发
    3. 静默时段：提醒被顺延而不是丢弃，且不会在静默结束时连发
"""

from datetime import datetime, timedelta

import pytest

from app.services.alerting import AlertingService, NodeAlertState
from app.services.config import AlertingConfig
from app.services.incident import compute_next_notify_at

LADDER = (0, 60, 240, 480, 720)
LOOP = 720


def make_config(**kwargs):
    """构造测试用告警配置：默认不写 Redis、不启用静默"""
    params = dict(
        enabled=True,
        consecutive_failures=3,
        latency_p95_ms=2000,
        suppress_minutes=240,
        suppress_persist=False,
        reminder_intervals=LADDER,
        intervals_loop_minutes=LOOP,
        max_notifications_per_incident=0,
        quiet_hours_enabled=False,
    )
    params.update(kwargs)
    return AlertingConfig(**params)


# ===================== 阶梯节奏计算 =====================


class TestNodeAlertState:
    """节点告警状态数据类"""

    def test_initial_state(self):
        state = NodeAlertState(node_name="n1")
        assert state.node_name == "n1"
        assert state.consecutive_failures == 0
        assert state.last_success is True
        assert state.suppressed_alerts == {}
        assert len(state.latencies) == 0

    def test_record_failure(self):
        """失败计数累加，状态标记为失败"""
        state = NodeAlertState(node_name="n1")
        state.consecutive_failures += 1
        state.last_success = False

        assert state.consecutive_failures == 1
        assert state.last_success is False

    def test_reset_on_success(self):
        """成功后计数归零"""
        state = NodeAlertState(node_name="n1")
        state.consecutive_failures = 5
        state.last_success = False

        state.consecutive_failures = 0
        state.last_success = True

        assert state.consecutive_failures == 0
        assert state.last_success is True

    def test_latency_deque_is_bounded(self):
        """延迟队列有上限，避免长期运行后内存增长"""
        state = NodeAlertState(node_name="n1")
        for i in range(500):
            state.latencies.append(float(i))
        assert len(state.latencies) <= 100


class TestStateQueries:
    """状态查询接口"""

    async def test_get_all_states(self, frozen_datetime, make_heartbeat_result,
                                  incident_store):
        svc = make_service(frozen_datetime)

        await svc.evaluate(make_heartbeat_result(node_name="n1", success=False))
        await svc.evaluate(make_heartbeat_result(node_name="n2", success=True))

        states = svc.get_all_states()
        assert set(states) == {"n1", "n2"}
        assert states["n1"]["consecutive_failures"] == 1
        assert states["n2"]["consecutive_failures"] == 0

    async def test_get_node_state_unknown(self, frozen_datetime):
        svc = make_service(frozen_datetime)
        assert svc.get_node_state("never-seen") is None

    async def test_state_reports_suppressed_alerts(self, frozen_datetime,
                                                   make_heartbeat_result,
                                                   incident_store):
        """
        状态查询能反映抑制情况

        注意 node_down 的节奏已改由事件闸门（next_notify_at）控制，
        不再写入内存抑制表；仍走抑制路径的是 node_recovery 这类
        容易抖动的告警。
        """
        svc = make_service(frozen_datetime)

        # 先制造一次故障并恢复，触发恢复通知及其抑制记录
        for _ in range(3):
            await svc.evaluate(make_heartbeat_result(success=False))
        await svc.evaluate(make_heartbeat_result(success=True))

        state = svc.get_node_state("test-node")
        assert "node_recovery" in state["suppressed_alerts"], (
            "恢复通知的去重记录应在状态里可见"
        )

    async def test_node_down_not_in_memory_suppression(self, frozen_datetime,
                                                       make_heartbeat_result,
                                                       incident_store):
        """
        node_down 不再占用内存抑制表

        避免将来有人误把抑制窗口当成 node_down 的节奏来源。
        """
        svc = make_service(frozen_datetime)
        for _ in range(3):
            await svc.evaluate(make_heartbeat_result(success=False))

        state = svc.get_node_state("test-node")
        assert "node_down" not in state["suppressed_alerts"]


class TestAlertingDisabled:
    """全局开关"""

    async def test_disabled_returns_no_events(self, frozen_datetime,
                                              make_heartbeat_result,
                                              incident_store):
        svc = make_service(frozen_datetime, enabled=False)
        for _ in range(5):
            events = await svc.evaluate(make_heartbeat_result(success=False))
            assert events == []


class TestReminderLadder:
    """next_notify_at 的计算"""

    def test_first_reminder_is_immediate(self):
        """事件建立时闸门敞开，首次提醒立即放行"""
        start = datetime(2026, 10, 4, 20, 0)
        nxt = compute_next_notify_at(start, notify_count=0, intervals=LADDER,
                                     loop_minutes=LOOP)
        assert nxt == start

    def test_ladder_offsets(self):
        """阶梯各档位对应距事件开始的固定时刻"""
        start = datetime(2026, 10, 4, 20, 0)
        expected = {1: 60, 2: 240, 3: 480, 4: 720}  # 分钟

        for count, minutes in expected.items():
            nxt = compute_next_notify_at(start, notify_count=count,
                                         intervals=LADDER, loop_minutes=LOOP)
            assert nxt == start + timedelta(minutes=minutes), (
                f"第 {count} 次提醒后应指向 +{minutes} 分钟"
            )

    def test_loop_after_ladder_exhausted(self):
        """走完阶梯后按循环间隔继续"""
        start = datetime(2026, 10, 4, 20, 0)
        # 第 5 次后：阶梯最后一档 720 + 循环 720
        nxt = compute_next_notify_at(start, notify_count=5,
                                     intervals=LADDER, loop_minutes=LOOP)
        assert nxt == start + timedelta(minutes=1440)

        nxt2 = compute_next_notify_at(start, notify_count=6,
                                      intervals=LADDER, loop_minutes=LOOP)
        assert nxt2 == start + timedelta(minutes=2160)

    def test_anchor_prevents_drift(self):
        """
        锚点在事件开始时刻，不受"上次发送时间"影响

        这样即使某次发送因限流被跳过，后续节奏也不累积漂移。
        """
        start = datetime(2026, 10, 4, 20, 0)
        # 本次发送早于锚点时刻（例如被限流后补发），第三档仍对齐到 +240 分钟
        nxt = compute_next_notify_at(start, notify_count=2, intervals=LADDER,
                                     loop_minutes=LOOP,
                                     now=start + timedelta(minutes=60))
        assert nxt == start + timedelta(minutes=240)

    def test_spacing_prevents_burst(self):
        """
        长时间未发送后不应连发（静默顺延、进程停摆等场景）

        锚点算出的时刻若落在过去，闸门会一直敞开，
        因此必须同时受"本次发送 + 当前档位间隔"约束。
        """
        start = datetime(2026, 10, 4, 20, 0)
        late = start + timedelta(hours=12)  # 12 小时后才发出第二次
        nxt = compute_next_notify_at(start, notify_count=2, intervals=LADDER,
                                     loop_minutes=LOOP, now=late)
        # 锚点算出的是 +240 分钟（已过去），因此取发送时刻 + 档位间隔（180 分钟）
        assert nxt == late + timedelta(minutes=180)
        assert nxt > late

    def test_empty_intervals_does_not_crash(self):
        """阶梯为空时退化为立即，不抛异常"""
        start = datetime(2026, 10, 4, 20, 0)
        nxt = compute_next_notify_at(start, notify_count=1, intervals=(),
                                     loop_minutes=LOOP)
        assert nxt >= start


# ===================== 告警触发 =====================


def make_service(clock, **config_kwargs):
    """构造告警服务，并让服务内的 datetime 跟随假时钟"""
    import app.services.alerting as alerting_mod

    svc = AlertingService(make_config(**config_kwargs))
    return svc


class TestAlertTriggering:
    """触发与抑制"""

    async def test_no_alert_on_first_failure(self, frozen_datetime,
                                             make_heartbeat_result):
        """单次失败不告警，必须连续达到阈值"""
        svc = make_service(frozen_datetime)

        events = await svc.evaluate(make_heartbeat_result(success=False))
        assert events == []

        state = svc.get_node_state("test-node")
        assert state["consecutive_failures"] == 1

    async def test_alert_after_threshold(self, frozen_datetime,
                                         make_heartbeat_result, incident_store):
        """连续失败达到阈值时触发一次告警"""
        svc = make_service(frozen_datetime)

        events = []
        for _ in range(3):
            events.extend(await svc.evaluate(make_heartbeat_result(success=False)))

        assert len(events) == 1
        assert events[0].alert_type == "node_down"
        assert events[0].incident_id is not None
        assert events[0].notify_seq == 1

    async def test_different_nodes_independent(self, frozen_datetime,
                                               make_heartbeat_result,
                                               incident_store):
        """节点之间的计数与事件互不影响"""
        svc = make_service(frozen_datetime)

        for _ in range(3):
            await svc.evaluate(make_heartbeat_result(node_name="node-a", success=False))

        # node-b 刚失败一次，不应受影响
        events_b = await svc.evaluate(
            make_heartbeat_result(node_name="node-b", success=False)
        )
        assert events_b == []

        states = svc.get_all_states()
        assert states["node-a"]["consecutive_failures"] == 3
        assert states["node-b"]["consecutive_failures"] == 1

    async def test_recovery_alert(self, frozen_datetime, make_heartbeat_result,
                                  incident_store):
        """
        故障恢复产生恢复告警

        同时验证不会重复发：连续成功只应产生一次恢复通知。
        """
        svc = make_service(frozen_datetime)

        for _ in range(3):
            await svc.evaluate(make_heartbeat_result(success=False))

        first = await svc.evaluate(make_heartbeat_result(success=True))
        assert any(e.alert_type == "node_recovery" for e in first)

        # 继续成功不应再发恢复通知
        second = await svc.evaluate(make_heartbeat_result(success=True))
        assert [e for e in second if e.alert_type == "node_recovery"] == []

    async def test_recovery_alert_closes_incident(self, frozen_datetime,
                                                  make_heartbeat_result,
                                                  incident_store):
        """恢复时发出恢复通知并关闭事件"""
        svc = make_service(frozen_datetime)

        for _ in range(3):
            await svc.evaluate(make_heartbeat_result(success=False))

        incident = await incident_store.get_open_incident("test-node", "node_down")
        assert incident is not None

        events = await svc.evaluate(make_heartbeat_result(success=True))
        recovery = [e for e in events if e.alert_type == "node_recovery"]
        assert len(recovery) == 1

        # 事件应被关闭
        assert await incident_store.get_open_incident("test-node", "node_down") is None
        closed = incident_store.rows[incident["id"]]
        assert closed["status"] == "resolved"

    async def test_recovery_carries_incident_summary(self, frozen_datetime,
                                                     make_heartbeat_result,
                                                     incident_store, clock):
        """恢复通知携带故障时长与期间提醒次数"""
        svc = make_service(frozen_datetime)

        for _ in range(3):
            await svc.evaluate(make_heartbeat_result(success=False))

        # 推进 24 小时后再恢复
        clock.advance(hours=24)
        events = await svc.evaluate(make_heartbeat_result(success=True))
        recovery = [e for e in events if e.alert_type == "node_recovery"][0]

        assert recovery.incident_duration_s is not None
        assert recovery.incident_duration_s > 0
        assert recovery.incident_notify_count >= 1
        assert "故障持续" in recovery.message

    async def test_reset_on_success(self, frozen_datetime, make_heartbeat_result):
        """成功一次即重置失败计数"""
        svc = make_service(frozen_datetime)

        await svc.evaluate(make_heartbeat_result(success=False))
        await svc.evaluate(make_heartbeat_result(success=False))
        await svc.evaluate(make_heartbeat_result(success=True))

        assert svc.get_node_state("test-node")["consecutive_failures"] == 0


# ===================== 闸门（核心） =====================


class TestIncidentGate:
    """next_notify_at 闸门 - 取代旧的抑制窗口"""

    async def test_no_repeat_before_gate(self, frozen_datetime,
                                         make_heartbeat_result, incident_store,
                                         clock):
        """闸门未到时不再重复告警"""
        svc = make_service(frozen_datetime)

        # 达到阈值，发出首次告警
        events = []
        for _ in range(3):
            events.extend(await svc.evaluate(make_heartbeat_result(success=False)))
        assert len(events) == 1

        # 未满 1 小时，后续失败不应再产生告警
        for _ in range(30):
            clock.advance(minutes=1)  # 推进 30 分钟
            more = await svc.evaluate(make_heartbeat_result(success=False))
            assert more == [], "闸门未到却产生了告警"

    async def test_second_alert_after_gate_opens(self, frozen_datetime,
                                                 make_heartbeat_result,
                                                 incident_store, clock):
        """跨过闸门后产生第二次提醒，序号递增"""
        svc = make_service(frozen_datetime)

        for _ in range(3):
            await svc.evaluate(make_heartbeat_result(success=False))

        # 推进 61 分钟，越过第一档（1 小时）
        clock.advance(minutes=61)
        events = await svc.evaluate(make_heartbeat_result(success=False))

        assert len(events) == 1
        assert events[0].notify_seq == 2
        assert "第 2 次提醒" in events[0].message
        assert "已持续" in events[0].message

    async def test_failures_still_counted_while_gated(self, frozen_datetime,
                                                      make_heartbeat_result,
                                                      incident_store, clock):
        """被闸门挡下的检测仍累计失败次数，只是不产生通知"""
        svc = make_service(frozen_datetime)

        for _ in range(3):
            await svc.evaluate(make_heartbeat_result(success=False))

        incident_id = list(incident_store.rows)[0]
        after_first = incident_store.rows[incident_id]["total_failures"]

        for _ in range(20):
            clock.advance(minutes=1)
            await svc.evaluate(make_heartbeat_result(success=False))

        after_gated = incident_store.rows[incident_id]["total_failures"]
        assert after_gated == after_first + 20
        # 但提醒次数没变
        assert incident_store.rows[incident_id]["notify_count"] == 1

    async def test_notification_cap(self, frozen_datetime, make_heartbeat_result,
                                    incident_store, clock):
        """单事件提醒上限生效后不再提醒"""
        svc = make_service(frozen_datetime, max_notifications_per_incident=2)

        for _ in range(3):
            await svc.evaluate(make_heartbeat_result(success=False))

        clock.advance(minutes=61)
        second = await svc.evaluate(make_heartbeat_result(success=False))
        assert len(second) == 1
        assert second[0].notify_seq == 2

        # 再跨过闸门也不应产生第三次
        clock.advance(hours=5)
        third = await svc.evaluate(make_heartbeat_result(success=False))
        assert third == []


# ===================== 跨午夜（原始故障场景） =====================


class TestCrossMidnight:
    """
    节点持续离线跨过午夜

    这是最初报告的故障：旧实现按自然日重置配额，零点后重新爆发。
    """

    async def test_no_midnight_burst(self, frozen_datetime, make_heartbeat_result,
                                     incident_store, clock):
        """午夜不产生额外提醒"""
        svc = make_service(frozen_datetime)

        # 从 20:00 开始持续离线 24 小时
        clock.set(2026, 10, 4, 20, 0)
        fired = []

        for i in range(24 * 60):
            events = await svc.evaluate(make_heartbeat_result(success=False))
            for e in events:
                fired.append((clock.now(), e.notify_seq))
            clock.advance(minutes=1)

        # 午夜 00 点时段（00:00-00:59）最多一小时一次
        midnight = [t for t, _ in fired if t.strftime("%m-%d %H") == "10-05 00"]
        assert len(midnight) <= 1, f"午夜出现了 {len(midnight)} 次提醒"

    async def test_alert_count_matches_ladder(self, frozen_datetime,
                                              make_heartbeat_result,
                                              incident_store, clock):
        """
        24 小时内提醒次数符合阶梯

        从 20:00 起算，阶梯 0/60/240/480/720 分钟对应
        20:02 / 21:02 / 01:02 / 09:02 / 次日 21:02 —— 共 5 次。
        对比改造前：抑制 15 分钟 + 午夜重置配额，约 20 次。
        """
        svc = make_service(frozen_datetime)
        clock.set(2026, 10, 4, 20, 0)

        fired = []
        for i in range(24 * 60):
            events = await svc.evaluate(make_heartbeat_result(success=False))
            fired.extend(events)
            clock.advance(minutes=1)

        assert len(fired) == 5, (
            f"24 小时应提醒 5 次（阶梯 0/1h/5h/13h/25h），实际 {len(fired)} 次"
        )
        # 序号连续，无跳号
        assert [e.notify_seq for e in fired] == [1, 2, 3, 4, 5]

    async def test_single_incident_spanning_midnight(self, frozen_datetime,
                                                     make_heartbeat_result,
                                                     incident_store, clock):
        """跨午夜的 24 小时离线只产生一个事件"""
        svc = make_service(frozen_datetime)
        clock.set(2026, 10, 4, 20, 0)

        for i in range(24 * 60):
            await svc.evaluate(make_heartbeat_result(success=False))
            clock.advance(minutes=1)

        assert len(incident_store.rows) == 1, "一次故障应只有一个事件"


# ===================== 静默时段 =====================


class TestQuietHours:
    """夜间静默"""

    @pytest.mark.parametrize("hhmm,expected", [
        ("22:59", False),
        ("23:00", True),
        ("23:30", True),
        ("02:00", True),
        ("06:59", True),
        ("07:00", False),
        ("12:00", False),
    ])
    def test_quiet_window_detection(self, frozen_datetime, clock, hhmm, expected):
        """跨夜区间 23:00-07:00 的边界判定"""
        svc = make_service(frozen_datetime, quiet_hours_enabled=True,
                           quiet_hours_start="23:00", quiet_hours_end="07:00")
        h, m = map(int, hhmm.split(":"))
        clock.set_local(2026, 10, 4, h, m)
        assert svc._in_quiet_hours() is expected

    def test_disabled_never_quiet(self, frozen_datetime, clock):
        """未启用时任何时刻都不静默"""
        svc = make_service(frozen_datetime, quiet_hours_enabled=False)
        clock.set_local(2026, 10, 4, 2, 0)
        assert svc._in_quiet_hours() is False

    def test_invalid_config_does_not_silence(self, frozen_datetime, clock):
        """配置非法时不静默 —— 宁可打扰也不要漏掉整晚告警"""
        svc = make_service(frozen_datetime, quiet_hours_enabled=True,
                           quiet_hours_start="banana", quiet_hours_end="alsobad")
        clock.set_local(2026, 10, 4, 2, 0)
        assert svc._in_quiet_hours() is False

    def test_end_datetime_outside_quiet_is_none(self, frozen_datetime, clock):
        """非静默时段没有"结束时刻"，否则会把白天的提醒推到明天"""
        svc = make_service(frozen_datetime, quiet_hours_enabled=True,
                           quiet_hours_start="23:00", quiet_hours_end="07:00")
        clock.set_local(2026, 10, 4, 12, 0)
        assert svc._quiet_hours_end_datetime() is None

    def test_end_datetime_across_midnight(self, frozen_datetime, clock):
        """
        深夜顺延到次日，凌晨则顺延到当天

        注意：返回值是 UTC 朴素时间（与心跳时间戳同基准），因此断言
        必须先转换成本地时刻再比对 —— 直接 strftime 会得到 UTC 的
        读数，看起来像"没顺延到 07:00"。
        """
        svc = make_service(frozen_datetime, quiet_hours_enabled=True,
                           quiet_hours_start="23:00", quiet_hours_end="07:00")

        clock.set_local(2026, 10, 4, 23, 30)
        end = svc._quiet_hours_end_datetime()
        assert end is not None
        assert clock.as_local(end).strftime("%m-%d %H:%M") == "10-05 07:00"

        clock.set_local(2026, 10, 5, 2, 0)
        end = svc._quiet_hours_end_datetime()
        assert end is not None
        assert clock.as_local(end).strftime("%m-%d %H:%M") == "10-05 07:00"

    def test_end_datetime_is_in_utc_basis(self, frozen_datetime, clock):
        """
        顺延时刻必须与传入的 now 同为 UTC 基准

        否则闸门比较会拿 UTC 与本地时间戳相比，永远判为"还没到时间"，
        提醒会被无限推迟。
        """
        svc = make_service(frozen_datetime, quiet_hours_enabled=True,
                           quiet_hours_start="23:00", quiet_hours_end="07:00")

        clock.set_local(2026, 10, 4, 23, 30)
        end = svc._quiet_hours_end_datetime()
        assert end is not None
        # UTC 朴素时间：无 tzinfo，且不早于当前时刻
        assert end.tzinfo is None
        assert end >= clock.now()

    async def test_quiet_hours_defer_not_discard(self, frozen_datetime,
                                                 make_heartbeat_result,
                                                 incident_store, clock):
        """
        静默期内的提醒被顺延而不是丢弃

        事件在静默期间被推迟，notify_count 不增加 ——
        静默结束后仍按原阶梯节奏继续，不会少一次提醒。
        """
        svc = make_service(frozen_datetime, quiet_hours_enabled=True,
                           quiet_hours_start="23:00", quiet_hours_end="07:00")
        clock.set_local(2026, 10, 4, 22, 0)

        # 22:02 首次告警（阈值 3 次）
        fired = []
        for _ in range(3):
            fired.extend(await svc.evaluate(make_heartbeat_result(success=False)))
        assert len(fired) == 1

        incident_id = list(incident_store.rows)[0]
        count_before = incident_store.rows[incident_id]["notify_count"]

        # 推进到 23:30，此时闸门已开但处于静默期
        clock.advance(minutes=90)
        events = await svc.evaluate(make_heartbeat_result(success=False))

        assert events == [], "静默期内不应发出提醒"
        # 提醒未被消耗
        assert incident_store.rows[incident_id]["notify_count"] == count_before
        assert len(incident_store.deferrals) >= 1

    async def test_no_burst_after_quiet_hours(self, frozen_datetime,
                                              make_heartbeat_result,
                                              incident_store, clock):
        """
        静默结束后不得连发

        这是实现过程中真实出现过的缺陷：闸门只推到静默结束时刻，
        而下次计算仍以事件开始为锚点，算出的时刻已在过去，
        导致 07:00 / 07:01 / 07:02 连发三封。
        """
        svc = make_service(frozen_datetime, quiet_hours_enabled=True,
                           quiet_hours_start="23:00", quiet_hours_end="07:00")
        clock.set_local(2026, 10, 4, 22, 0)

        fired = []
        # 22:00 离线，持续到次日 09:00
        for i in range(11 * 60):
            events = await svc.evaluate(make_heartbeat_result(success=False))
            for e in events:
                fired.append(clock.now())
            clock.advance(minutes=1)

        # 时钟基准是 UTC（与心跳时间戳一致），静默时段按本地配置，
        # 因此判断"是否落在静默期"前必须先换算成本地时刻。
        local_fired = [clock.as_local(t) for t in fired]

        # 静默期内（本地 23:00-07:00）不应有任何提醒
        in_quiet = [t for t in local_fired if t.hour >= 23 or t.hour < 7]
        assert in_quiet == [], f"静默期内发出了 {len(in_quiet)} 次提醒"

        # 静默结束后一小时内不应出现连发
        after = [t for t in local_fired if t.strftime("%m-%d %H") == "10-05 07"]
        assert len(after) <= 1, f"静默结束后一小时内连发了 {len(after)} 次"
