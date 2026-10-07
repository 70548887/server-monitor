## 变更内容

<!-- 简要说明这次改了什么，以及为什么 -->

## 变更类型

- [ ] 功能（feat）— 新增能力
- [ ] 修复（fix）— 修正缺陷
- [ ] 重构（refactor）— 不改变外部行为
- [ ] 文档（docs）
- [ ] 构建/运维（build/chore）
- [ ] 性能（perf）

## 影响范围

<!-- 勾选受影响的模块，未涉及的不必勾 -->

- [ ] 告警与通知（services/alerting.py, services/incident.py, services/notification.py）
- [ ] 心跳与调度（services/heartbeat.py, services/scheduler.py）
- [ ] 数据存储（repositories/, models/, 迁移脚本）
- [ ] 认证与安全（middleware/, routes/auth.py）
- [ ] 前端页面（templates/, static/）
- [ ] 配置（configs/app.yaml, .env.example）
- [ ] 部署配置（Dockerfile, nginx, systemd）

## 数据库变更

- [ ] 无
- [ ] 需执行迁移（`python scripts/bootstrap_database.py`）
- [ ] 需手工 DDL：请在下方说明

## 验证方式

- [ ] 新增/修改了测试
- [ ] 手动验证步骤：<!-- 列出验证路径，如"登录 → 告警页 → 展开事件" -->
- [ ] 生产数据验证：<!-- 如涉及告警行为，说明观察到什么 -->

## 检查清单

- [ ] `python -m pytest` 全部通过
- [ ] 提交信息符合 Conventional Commits（`feat:` / `fix:` / `docs:` …）
- [ ] 若改了配置项，已同步 `.env.example` 与 `configs/app.yaml` 注释
- [ ] 若改了告警节奏，评估过对通知量的影响
- [ ] 敏感信息未进入提交（`.env`、密码、SMTP 授权码）

## 风险与回滚

<!-- 这次变更可能带来的影响，以及出问题时如何回退 -->

---

<!--
维护者注：
- `master` 只接收来自 develop 的稳定版本，已打 tag。
- 功能分支从 develop 切出，命名建议 feat/xxx、fix/xxx。
- 涉及告警节奏、通知限流、数据库结构的改动，请重点说明。
-->
