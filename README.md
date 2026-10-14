# 服务器监控面板

多服务器心跳监控：检测接口可用性、延迟与状态，带告警通知与趋势分析。

当前版本：**v1.3.0**

代码仓库：[70548887/server-monitor](https://github.com/70548887/server-monitor)

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
# 1. 安装已验证的依赖组合（与生产、CI 保持一致）
python -m pip install -r requirements.lock
python -m pip check
#    requirements.txt 仅用于主动测试其他依赖版本
#    注意：email-validator 是 notifications 路由的硬依赖，缺它应用无法导入

# 2. 配置环境变量
cp .env.example .env
#    至少填写 ADMIN_PASSWORD 与 MySQL 凭据

# 3. 建库建表（含事件表与补列，幂等可重复执行）
python scripts/bootstrap_database.py

# 4. 启动
uvicorn app.main:app --host 0.0.0.0 --port 8080
```

访问 `http://localhost:8080`，用 `.env` 中的管理员账号登录。

启动时会先自动检查数据库表结构，补齐已发布版本所需的缺失字段和索引，再创建默认管理员与通知渠道（凭据取自 `.env`）。旧库升级无需另跑补列命令；此检查每次启动都会执行，已完成的变更不会重复执行。数据库应已存在，应用账号需具备建表、改表及创建索引的权限；结构升级失败会记录具体原因并停止启动，修复后重新启动即可继续。

### 传统部署（systemd + nginx）

不使用 Docker 时的部署方式，配置文件在 `deploy/`：

```bash
# systemd 托管（安装步骤见 deploy/server-monitor.service 末尾注释）
sudo cp deploy/server-monitor.service /etc/systemd/system/
sudo systemctl enable --now server-monitor

# nginx 反代：TLS 终止 + 静态资源直出
sudo cp deploy/nginx.conf /etc/nginx/conf.d/server-monitor.conf
sudo nginx -t && sudo systemctl reload nginx
```

`systemd` 与 nginx 配置里已标注关键项：`WorkingDirectory` 必须正确
（应用用相对路径定位配置与日志）、生产需 `COOKIE_SECURE=true`、
`/metrics` 应限制为内网访问。

> 数据库不可用时应用会降级为内存模式运行（数据不持久化），
> 若配置了 ADMIN_PASSWORD，数据库故障时可使用环境变量账号降级登录。
> 此时用于应急查看，节点等数据库写入操作会返回失败。

### 宝塔部署后登录页返回 500

若日志已显示 `Application startup complete`，随后访问 `/login` 报
`ValueError: context must include a "request" key`，说明运行环境仍在使用旧版
Starlette 模板接口。项目使用 `TemplateResponse(request, name, context)`，
要求 FastAPI ≥ 0.108.0、Starlette ≥ 0.29.0；旧环境把模板名误当成上下文，
即使代码的字典中已有 `request` 也会报错。

在宝塔项目实际使用的 Python 环境中执行（`python` 应替换为项目解释器的完整路径）：

```bash
cd /www/wwwroot/server_monitor
python -m pip install -r requirements.lock
python -m pip check
python -c "import sys, fastapi, starlette; print(sys.executable); print('FastAPI', fastapi.__version__, 'Starlette', starlette.__version__)"
```

安装及依赖检查成功后，在宝塔中重启该 Python 项目，再验证登录页：

```bash
curl -sS -o /dev/null -w '%{http_code}\n' http://127.0.0.1:8080/login
```

预期返回 `200`。若配置了 `ALLOWED_HOSTS`，需通过配置的域名访问或为请求添加对应的 `Host` 头。
只替换代码不会更新 Python 依赖；单独升级 Starlette 可能与旧 FastAPI 冲突，
因此应整体安装锁定文件。开头 APScheduler 的 `pkg_resources is deprecated`
是另一项弃用警告，不是本次登录页 500 的原因；锁定文件同时提供了较新的 APScheduler。

### 告警事件展开失败：通知记录缺少字段

旧版展开故障事件时报 `Unexpected token ... Internal Server Error`，且后台出现
`Unknown column 'notification_logs.incident_id' in 'field list'`，是旧表缺列导致。
当前版本在启动时自动检查并补齐 `notification_logs`、`alert_logs` 的事件关联字段、
相关索引，以及事件表的 InnoDB 引擎。安装当前代码后重启即可执行；完成后日志显示
“数据库表结构检查完成，本次补齐 N 项”。多个启动进程通过数据库命名锁避免同时改表。
下列手工脚本仍可用于旧版修复或独立建库，与启动过程共用升级逻辑。

在服务器项目目录中，用宝塔项目实际使用的 Python 解释器运行：

```bash
cd /www/wwwroot/server_monitor
python scripts/bootstrap_database.py
```

脚本会检查并补齐 `notification_logs`、`alert_logs` 的 `incident_id`、`notify_seq`
以及关联索引，保留已有数据，可重复运行。成功后重启项目并重新展开事件。
恢复旧版 SQL 备份之后，重启当前版本会重新补齐结构；也可直接运行此脚本。
此前因缺列而写入失败的通知记录不会自动补回；补列后若显示“尚无通知发送记录”，
表示该事件还没有已保存的关联记录。

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
tests/                 单元、SQL集成与浏览器测试
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
       ├─ 第 3 次提醒    +4h
       ├─ 第 4 次提醒    +8h
       ├─ 第 5 次提醒    +12h
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
| 邮件总数 | ~20 | 5（24小时边界前；静默时段可进一步减少） |
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

每个 5 分钟窗口除计数与可用性外，还写入延迟的
`min / avg / max / p50 / p95 / p99`。分位数采用线性插值
（与 NumPy `percentile` 默认口径一致）。MySQL 5.7/8 均使用这套计算，
不依赖不存在的 `PERCENT_RANK_CONT` 内置函数。节点详情展示最近完整窗口，
`GET /api/nodes/{name}/latency/aggregates?hours=24` 返回逐窗口分位数。

### 历史聚合补算

停用其他聚合写入进程后执行；脚本先备份事件与聚合表，再按6小时批次重建现存样本覆盖的完整窗口：

```bash
python scripts/repair_aggregates.py          # 预览范围
python scripts/repair_aggregates.py --apply  # 备份后执行，可幂等重跑
```

不会按年龄删除未覆盖的历史统计。旧样本已清理的范围无法补算；备份和执行日志保存在忽略版本管理的 `logs/`。

旧事件若由本地时间错误写入恢复时间，可执行 `python scripts/repair_incident_times.py --legacy-offset-hours 8` 预览。仅当首个成功样本与修正时间吻合、原时长一致时才列为候选；加 `--apply` 后先备份、再按原值条件更新，重复执行不二次偏移。

## 测试

```bash
python -m pytest              # 默认不连接真实数据库或启动浏览器
python -m pytest -v           # 详细输出
# PowerShell：启用真实MySQL集成（只写临时测试库）
$env:RUN_MYSQL_INTEGRATION="1"
python -m pytest tests/test_mysql_integration.py
# 浏览器验收另需安装 playwright 及 Chromium
$env:RUN_BROWSER_TESTS="1"
python -m pytest tests/test_browser.py
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

⚠️ **当前各表约定不统一**，下表是实况（由测试锁定，见
`tests/test_timezone_convention.py`）。改动任一字段前先看这里。

| 字段 | 写入方式 | 存储时区 | 序列化 |
|---|---|---|---|
| `samples.timestamp` | `utcnow()` | UTC | 趋势图显式加 `Z` |
| `alert_incidents.started_at/last_notified_at/next_notify_at/resolved_at` | 心跳UTC时间 / `utcnow()` | UTC | 加 `Z`（`format_iso_utc`） |
| `alert_logs.triggered_at` | `datetime.now()` | **本地** | 不加 `Z` |
| `notification_logs.sent_at` | MySQL `func.now()` | **本地** | 不加 `Z` |

**为什么序列化是否带 `Z` 至关重要**：前端 `formatTime()` 用
`new Date(isoStr)` 解析——带 `Z` 会识别为 UTC 并换算成本地时间显示，
不带 `Z` 则按本地时间原样显示。因此存储时区与 `Z` 后缀必须匹配，
否则显示偏差一个时区（UTC+8 环境下即 8 小时）。

新增字段时请先确认落在哪一类，并补对应测试。

**尚未统一的原因与影响**：统一到 UTC 需要迁移
`alert_logs`（3.3 万行）与 `notification_logs`（9.1 千行），
且每日发送上限的"当天 0 点"边界需同步改为 UTC 零点
（否则配额会在本地 08:00 而非 00:00 重置）。迁移前不做，
因为两表内部自洽，当前显示也是正确的。

`app/utils/timeutil.py` 提供转换工具。不要直接用
`datetime.now(timezone.utc)` 写库——带时区的对象与已有朴素数据
混用会产生 8 小时偏差。

## 设置页

设置分为「账户资料」「账户安全」「系统更新」三个标签。每次只展示当前标签的内容，
支持左右方向键与 Home / End 切换；刷新、浏览器前进后退、表单提交后保留对应标签。
可通过 `/settings?tab=account`、`/settings?tab=security`、`/settings?tab=updates` 直接访问。

## 在线升级

“设置 → 系统更新”提供版本检查、更新说明、校验下载、程序备份、应用更新、回滚和重启。
首次安装本功能后，使用独立启动管理器启动（单进程）：

```bash
python scripts/run_server.py --host 127.0.0.1 --port 8080
```

仍可用原 `uvicorn` 命令启动，但界面仅提供检查、下载与备份，应用和自动重启需要上述管理器。
systemd 示例已改用管理器；服务用户须拥有项目目录写权限。Docker 部署在界面检查版本后应重建镜像，不能在线覆盖容器程序。

更新包只来自本仓库正式Release的 `server-monitor-vX.Y.Z.zip`，必须有同名文件的 `SHA256SUMS.txt` 校验值。
下载和校验不改变正在运行的代码；点击应用后先准备独立依赖环境和程序备份，再停止应用、前向检查数据库结构、替换程序、重启并检查健康状态。
独立管理器在应用退出后继续工作，失败自动恢复旧程序和Python运行环境；系统断电后重新启动管理器会恢复未完成的文件事务。
若中断后新Python环境丢失，会先检查旧备份的完整性与解释器可用性，再在端口检查和互斥锁内恢复；缺少有效备份时会停止启动并保留现场。
启动入口在导入应用前检查事务，使用校验过的备份恢复代码和旧解释器执行恢复，避免部分替换后的应用包阻断恢复。尚未恢复的事务会阻止提交新升级。
界面重启会重新读取项目 `.env`，修改和删除的参数均在新子进程生效；启动管理器时显式传入的环境变量仍优先。systemd 请使用当前示例，不再通过 `EnvironmentFile` 重复加载同一 `.env`。

`.env`、`configs/`、数据库、日志和Git元数据不会被程序包覆盖。备份**不包含数据库**，也不会逆向执行数据库DDL，发布中的迁移必须保持向后兼容；升级重大版本前仍需自行备份数据库。
程序备份、操作状态和本地诊断日志位于 `.updates/`，不会公开下载或提交Git。已发布版本缺少此功能时，首次需手动安装包含升级模块的版本。

### 更新网络代理

服务器无法直接访问 GitHub 时，可在「设置 → 系统更新 → 更新网络代理」开启代理，
填写 `http://用户名:密码@主机:端口`（也支持 HTTPS 代理），点击「保存代理」后再检查更新。
用户名或密码中的保留字符需使用 URL 编码。此配置同时用于版本检查、校验清单、更新包
及应用更新时的依赖安装；不会注入节点探测、邮件或普通应用请求。

保存后输入框会清空，页面仅显示主机和端口；留空保存保留已有地址，关闭保留配置，
「清除代理」同时关闭并删除保存的地址。配置保存在项目私有目录 `.updates/proxy.json`，
不进入程序备份、发布包、状态返回或审计日志，重启和后续程序升级会保留。
需重启独立启动管理器以加载本次新增的 pip 代理支持；关闭本页代理不修改用户已有的 pip 配置文件。

### 节点配置重载

`POST /api/nodes/reload` 需要登录，从数据库刷新运行时节点配置和状态缓存，返回
`count`、`enabled_count` 和 `source: "database"`。它不导入 `nodes.yaml`，不触发额外检测或通知；
调度器在下一轮读取最新配置。同一URL的最近检测结果保留，更换URL后重置，数据库已删除的节点从缓存移除。
空库会清空配置缓存；数据库读取或配置校验失败时返回503，原配置和缓存保持不变。
检测结果携带配置版本；更换URL、检测条件、启停状态或删除节点后，旧请求的迟到结果不会覆盖缓存或进入后续告警评估。只修改地区、标签等展示信息时仍保留有效结果。

## 已知限制

- **界面入口**：`/api/auth/change-password` 是设置页表单的重复实现（表单已可用）；
  `/api/nodes/reload` 是已登录管理员使用的配置重载接口，尚无单独按钮。
- **时区约定未统一**：4 个时间字段分属 UTC 与本地两种约定
  （详见[时间约定](#时间约定)）。当前各表内部自洽、显示正确，
  但跨表按时间关联查询需先换算。统一需迁移 4.2 万行数据
- **分位数在 Python 侧计算**：按有限时间批次读取延迟样本。节点数增大时需监控聚合内存与耗时。
- **历史覆盖范围**：7天/30天值是该时间范围内已采样数据的比例；接口同时返回实际覆盖起止和窗口数，缺测不当作成功。
- **历史通知关联**：旧版本未写事件ID的发送记录保持原样，无法仅凭时间可靠推断归属；新通知完整写入事件与提醒序号。
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

### 本地发布归档

每个正式版本统一保存在项目根目录的 `release/<版本标签>/`，例如：

```text
release/
└── v1.1.0/
    ├── server-monitor-v1.1.0.zip
    ├── SHA256SUMS.txt
    └── RELEASE_NOTES.md
```

目录名与 GitHub Release 标签一致，安装包和校验文件保留原文件名及字节。新版本新增独立目录，保留旧版本；`release/` 不提交 Git，正式附件上传到 GitHub Release。开发期间的解包、下载和回归验证目录仍放在 `dist/` 或 `logs/`。

已发布版本可直接用 GitHub CLI 归档（PowerShell）：

```powershell
$releaseTag = 'v1.1.0'
gh release download $releaseTag --repo 70548887/server-monitor --dir "release/$releaseTag" --pattern "server-monitor-$releaseTag.zip" --pattern 'SHA256SUMS.txt'
if ($LASTEXITCODE -ne 0) { throw '下载发布附件失败' }
$releaseNotes = gh release view $releaseTag --repo 70548887/server-monitor --json body --jq .body
if ($LASTEXITCODE -ne 0) { throw '读取发布说明失败' }
$releaseNotes | Set-Content -Encoding utf8 "release/$releaseTag/RELEASE_NOTES.md"
Get-FileHash -Algorithm SHA256 "release/$releaseTag/server-monitor-$releaseTag.zip"
```

将输出的 SHA256 与该版本的 `SHA256SUMS.txt` 核对一致，再用于部署。

### 发布检查清单

- [ ] `python -m pytest` 全部通过
- [ ] 三处版本号一致
- [ ] `CHANGELOG.md` 已更新
- [ ] 正式安装包、SHA256 清单与发布说明已归档至 `release/<版本标签>/`
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
