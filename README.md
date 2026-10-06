# 服务器监控面板

多服务器心跳监控：检测接口可用性、延迟与状态，带告警通知与趋势分析。

## 快速开始

```bash
# 1. 安装依赖
pip install -r requirements.txt

# 2. 配置环境变量
cp .env.example .env
#    编辑 .env，至少填写 ADMIN_PASSWORD 与 MySQL 密码
#    注意：email-validator 是 notifications 路由的硬依赖，务必装齐

# 3. 初始化数据库（创建表 + 事件表迁移）
python scripts/migrate_phase2_incidents.py

# 4. 启动
uvicorn app.main:app --host 0.0.0.0 --port 8080
```

访问 `http://localhost:8080`，用 `.env` 中的管理员账号登录。

> 数据库不可用时应用会降级为内存模式运行（数据不持久化），
> 便于在没有 MySQL 的环境下先跑起来看界面。

## 架构

```
app/
├── main.py            FastAPI 入口、生命周期、中间件装配
├── routes/            HTTP 接口与页面
├── services/          业务逻辑（心跳、可用性、告警、通知、配置、调度）
├── repositories/      存储访问（MySQL / Redis）
├── models/            SQLAlchemy ORM
├── schemas/           Pydantic 请求/响应模型
├── middleware/        认证、安全（限流、响应头）
├── templates/         Jinja2 页面
└── static/            前端资源（原生 JS + 本地 Chart.js）

configs/app.yaml       应用配置（可被环境变量覆盖）
configs/nodes.yaml     节点配置（仅首次为空库时导入）
tests/                 251 个测试用例
```

**技术栈**：FastAPI · SQLAlchemy(async) · MySQL · Redis · APScheduler · httpx · structlog

## 告警设计

这是本项目的核心部分，经历了从"按自然日去重"到"事件化阶梯提醒"的演进。

### 问题

早期实现有两个缺陷，表现为**节点离线后过了凌晨连续触发邮件**：

1. 抑制窗口只有 15 分钟 → 24 小时内理论可触发 96 次
2. 每日上限按自然日计数 → 午夜归零后立刻重新爆发一轮

根因是去重的锚点绑在**日历**上，而故障与日历无关。

### 现状：一次故障 = 一个事件

```
节点离线
  └─ 事件开启（AlertIncident）
       ├─ 第 1 次提醒    立即
       ├─ 第 2 次提醒    +1h
       ├─ 第 3 次提醒    +5h
       ├─ 第 4 次提醒    +13h
       ├─ 第 5 次提醒    +25h
       └─ 之后每 12h 一次
  └─ 节点恢复
       └─ 立即发送恢复通知（附故障时长与期间提醒次数），事件关闭
```

关键机制：

| 机制 | 作用 |
|---|---|
| `next_notify_at` 闸门 | 唯一放行条件。锚定事件开始时刻，跨午夜/进程重启都不重置节奏 |
| 静默时段 | 23:00–07:00 内的提醒**顺延**到结束时刻，不丢弃、不连发 |
| 升级提醒 | 持续超过阈值后加 `[升级]` 标记并放开每日上限 |
| 恢复通知 | 不受任何限制，立即送达并关闭事件 |

**闸门未到时**：不产生告警、不发通知、不写发送记录、不消耗配额，
只静默累计失败次数。

### 配置

面板「通知 → 告警设置」可视化配置，也可直接改 `configs/app.yaml`：

```yaml
alerting:
  consecutive_failures: 3              # 连续失败 N 次触发
  reminder_intervals_minutes: [0, 60, 240, 480, 720]   # 阶梯时刻
  intervals_loop_minutes: 720          # 走完阶梯后的循环间隔
  escalate_after_minutes: 0            # 升级阈值，0 = 不启用
  suppress_minutes: 240                # 抖动抑制（管恢复/延迟告警）
  quiet_hours:
    enabled: false
    start: "23:00"
    end: "07:00"
```

设置会写回 `app.yaml`，且**保留文件中的所有注释**。

### 效果对比

节点从 20:00 起持续离线 24 小时：

| | 改造前 | 改造后 |
|---|---|---|
| 邮件总数 | ~20 | 4（含静默时段则更少） |
| 跨午夜 | **重新爆发** | 无影响 |
| 进程重启 | 抑制丢失，刷屏 | 节奏延续 |
| 提醒内容 | 逐封相同 | 递增（显示已持续时长） |

## 数据留存

样本表按"每分钟每节点一行"增长，`retention` 配置由调度器强制执行：

| 任务 | 频率 | 作用 |
|---|---|---|
| `heartbeat_task` | 60s | 心跳检测 |
| `aggregate_rollup` | 每小时 | 样本汇总到 `aggregates` 表 |
| `retention_cleanup` | 每日 3:00 | 清理过期样本、聚合、通知记录 |

> **7 天/30 天可用性来自聚合表。** 内存只保留 48 小时记录，
> 因此这两个指标在聚合数据就绪前返回 `null` 并附带说明，
> 而不是用 48 小时的数据冒充长周期值。

## 测试

```bash
python -m pytest              # 287 passed, 30 skipped
python -m pytest -v           # 详细输出
```

- 单元测试不依赖 MySQL / Redis，可直接运行
- 时间相关逻辑用可推进的假时钟（`FrozenClock`）驱动，
  因此"跨午夜不爆发""静默不连发"这类场景才能被确定性验证。
  假时钟区分 UTC 与本地两种语义，避免测试通过而生产出错
- 时区相关逻辑有专门测试（`tests/test_timezone.py`），
  本机非 UTC 时区时验证静默窗口对齐本地时刻
- 路由 API 测试需要 `email-validator`；缺失时优雅跳过并提示安装命令、
  而不是让整个文件收集失败

## 关键文件

| 文件 | 说明 |
|---|---|
| `app/services/alerting.py` | 告警评估、闸门、静默时段、升级判定 |
| `app/services/incident.py` | 故障事件生命周期与阶梯节奏计算 |
| `app/services/notification.py` | 邮件与 Webhook 发送、发送限制 |
| `app/services/scheduler.py` | 定时任务注册 |
| `app/services/availability.py` | 内存可用性统计（48 小时窗口） |
| `app/repositories/mysql_repository.py` | 持久化与聚合读写 |
| `app/utils/timeutil.py` | 时间工具（UTC 取值、时区换算） |
| `scripts/migrate_phase2_incidents.py` | 事件表迁移（幂等） |

## 时间约定

- **存储层**：数据库列为朴素 `DateTime`（未启用 `timezone=True`），统一写入 **UTC 朴素时间**
- **展示层**：静默时段等配置由用户按**本地时刻**填写，判定前必须先换算

`app/utils/timeutil.py` 提供两者之间的转换。不要直接用
`datetime.now(timezone.utc)` 写库——带时区的对象与已有朴素数据
混用会产生 8 小时偏差。

## 已知限制

- **p50/p95/p99 延迟未落库**，聚合表只存窗口内的可用性比例。
  给出未经样本级计算的分位数会显得精确却无依据，故未实现
- **单实例假设**：内存可用性统计与内存 Session 降级不支持多进程部署
