# 服务器监控面板

多服务器心跳监控：检测接口可用性、延迟与状态，带告警通知与趋势分析。

当前版本：**v1.0.0**

## 快速开始

### Docker Compose（推荐）

```bash
# 1. 配置环境变量
cp .env.example .env
#    至少填写 ADMIN_PASSWORD 与 MYSQL_PASSWORD

# 2. 启动（应用 + MySQL + Redis）
docker compose up -d

# 3. 首次建库建表（一次性）
docker compose exec app python scripts/bootstrap_database.py

# 4. 访问
open http://localhost:8080
```

### 本地开发

```bash
# 1. 安装依赖
pip install -r requirements.txt
#    注意：email-validator 是 notifications 路由的硬依赖，缺它应用无法导入

# 2. 配置环境变量
cp .env.example .env
#    至少填写 ADMIN_PASSWORD 与 MySQL 凭据

# 3. 建库建表（含 Phase 2 事件表与补列，幂等可重复执行）
python scripts/bootstrap_database.py

# 4. 启动
uvicorn app.main:app --host 0.0.0.0 --port 8080
```

访问 `http://localhost:8080`，用 `.env` 中的管理员账号登录。

启动后会自动创建默认管理员与通知渠道（凭据取自 `.env`）。

> 数据库不可用时应用会降级为内存模式运行（数据不持久化），
> 便于在没有 MySQL 的环境下先跑起来看界面。但此时**无法登录**
> ——管理员账户存储在数据库中。

### 从备份恢复

```bash
# 导入 SQL dump（流式读取，GB 级文件不爆内存；--to-innodb 转为事务引擎）
python scripts/import_sql.py backup.sql --to-innodb

# 备份通常来自旧版本，可能缺少新表与新列，需再跑一次引导
python scripts/bootstrap_database.py
```

导出的 `.sql` 文件已被 `.gitignore` 排除（体积大且含真实业务数据）。

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

### 生产数据佐证

从旧库导出的 9101 条通知记录可以量化这个问题：

| 日期 | 发送成功率 |
|---|---|
| 2026-02 | 97.2% |
| 2026-04 | 25.7% |
| 2026-08 | 37.1% |
| 2026-10 | **23.3%** |

失败原因 100% 是 `发送被限制: 今日发送已达上限（20次）`，而非 SMTP 故障。

最典型的一例：`Bilibili API B` 与 `汉堡WEB` 两个节点自 2026-09-21/22 起完全离线
（连续 16 天成功率 0%），旧系统在 2026-10-07 单日各产生 **60 条** `node_down`
告警——内容逐字相同，合计 120 条中仅 40 条送达，80 条被限流丢弃。

按本设计的阶梯节奏，这两天本应各约 4~5 条，**浪费约 110 条**。
真正的告警被限流挤掉，是比刷屏更隐蔽的损害。

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
| 恢复通知 | 立即送达并关闭事件，附故障时长与期间提醒次数 |

**闸门未到时**：不产生告警、不发通知、不写发送记录、不消耗配额，
只静默累计失败次数。

**恢复通知的前提**：只有此前**确实送达**过离线通知时才发恢复通知。
若一条都没送达（提醒被静默时段顺延、渠道故障、限流丢弃），
恢复通知会让人莫名其妙——"我从没收到过离线告警，为什么收到恢复提醒"？
此时只关闭事件，不发通知。

判定依据是 `notification_logs` 中 `success=True` 的记录，而非
`incident.notify_count`——后者只表示"尝试发过"，发送失败时照样递增。
数据库不可用导致判定不出时保守放行，避免故障"没有结束"。

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
| `scripts/bootstrap_database.py` | 建库建表 + Phase 2 迁移（幂等，凭据取自配置） |
| `scripts/import_sql.py` | SQL dump 流式导入，支持 `--to-innodb` |

## 时间约定

- **存储层**：数据库列为朴素 `DateTime`（未启用 `timezone=True`），统一写入 **UTC 朴素时间**
- **展示层**：静默时段等配置由用户按**本地时刻**填写，判定前必须先换算

`app/utils/timeutil.py` 提供两者之间的转换。不要直接用
`datetime.now(timezone.utc)` 写库——带时区的对象与已有朴素数据
混用会产生 8 小时偏差。

## 已知限制

- **未接线的 API**：9 个后端端点前端未调用，其中
  `/api/auth/change-password`、`/api/nodes/reload` 有实际价值待接入
- **时区约定并存**：`notification_logs.sent_at` 由 `func.now()`
  （MySQL 本地时）写入，与代码中显式使用的 UTC 并存。两者在各自
  场景内自洽，但跨表按时间关联查询会有 8 小时偏差
- **p50/p95/p99 延迟未落库**，聚合表只存窗口内的可用性比例。
  给出未经样本级计算的分位数会显得精确却无依据，故未实现
- **单实例假设**：内存可用性统计与内存 Session 降级不支持多进程部署

## 版本管理

### 分支模型

```
master      稳定发布分支，只接收已打标签的版本
develop     集成分支，日常变更的目标
feat/xxx    功能分支，从 develop 切出
fix/xxx     修复分支，从 develop 切出
```

流程：功能分支 → PR → develop → 合并到 master 时打标签。

### 提交信息

遵循 [Conventional Commits](https://www.conventionalcommits.org/)：

```
feat: 新增能力
fix: 缺陷修复
docs: 文档
refactor: 不改变外部行为的重构
chore: 构建/运维
```

标题用中文正文，类型前缀用英文。

### 版本号

遵循语义化版本。改动版本时**三处同步**：

1. `app/__init__.py` 的 `__version__`
2. `configs/app.yaml` 的 `app.version`
3. Git 标签：`git tag -a vX.Y.Z -m "说明"`

打标签前确认该提交是可运行状态。历史上
`v0.1.0-pre` 标记的是首次纳管的快照（页面全 500），
`v1.0.0` 才是首个可用版本。

### 发布检查清单

- [ ] `python -m pytest` 全部通过
- [ ] 三处版本号一致
- [ ] `CHANGELOG.md` 已更新
- [ ] 若改了数据库结构，`bootstrap_database.py` 已验证
- [ ] 若改了告警节奏，评估过对通知量的影响
- [ ] `.env` 未进入版本库

## 安全配置

三项安全开关通过环境变量控制，默认均为保守值：

| 变量 | 默认 | 生产环境建议 |
|---|---|---|
| `COOKIE_SECURE` | `false` | `true`（需配合 HTTPS） |
| `ALLOWED_HOSTS` | 空（不校验） | 填实际域名 |
| `CORS_ALLOWED_ORIGINS` | 空（不启用） | 仅跨域时填明确来源 |

不要用 `CORS_ALLOWED_ORIGINS=*` —— 与 Cookie 凭证组合会被浏览器拒绝。
