# 服务器监控面板 - 生产镜像
#
# 构建：
#   docker build -t server-monitor:1.0.1 .
#
# 运行（需先确保 MySQL / Redis 可达）：
#   docker run -d --name server-monitor \
#     -p 8080:8080 \
#     --env-file .env \
#     -v server-monitor-logs:/app/logs \
#     server-monitor:1.0.1
#
# 首次启动前需建库建表（在能访问 MySQL 的环境下执行一次）：
#   docker run --rm --env-file .env server-monitor:1.0.1 \
#     python scripts/bootstrap_database.py

# ---------- 构建阶段 ----------
FROM python:3.12-slim AS builder

WORKDIR /build

# 生产镜像用锁文件（精确版本）保证可复现。
# 若要跟进上游新版本，改 requirements.txt 后重新生成 lock。
COPY requirements.lock .

RUN pip wheel --no-cache-dir --wheel-dir /wheels -r requirements.lock


# ---------- 运行阶段 ----------
FROM python:3.12-slim

# 时区：告警按本地时刻展示（静默时段、发送时段均为本地时间）
# UTC+8 对应 Asia/Shanghai
ENV TZ=Asia/Shanghai \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

# curl 供 HEALTHCHECK 使用
RUN apt-get update \
    && apt-get install -y --no-install-recommends curl tzdata \
    && rm -rf /var/lib/apt/lists/*

# 安装编译依赖（部分包需要），装完即删
COPY --from=builder /wheels /wheels
COPY requirements.lock /tmp/requirements.txt
RUN pip install --no-cache-dir --no-index --find-links=/wheels -r /tmp/requirements.txt \
    && rm -rf /wheels /tmp/requirements.txt

# 非 root 运行
RUN groupadd -r monitor && useradd -r -g monitor monitor

WORKDIR /app

# 先复制依赖声明再复制源码，最大化利用层缓存
COPY --chown=monitor:monitor app/ ./app/
COPY --chown=monitor:monitor configs/ ./configs/
COPY --chown=monitor:monitor scripts/ ./scripts/
COPY --chown=monitor:monitor pytest.ini ./
# 装的是 requirements.lock；requirements.txt 一并带上便于排查依赖来源
COPY --chown=monitor:monitor requirements.lock requirements.txt ./

# 日志目录需可写
RUN mkdir -p /app/logs && chown -R monitor:monitor /app

USER monitor

EXPOSE 8080

# 健康检查：/healthz/live 是公开路径，不需认证
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD curl -fsS http://127.0.0.1:8080/healthz/live || exit 1

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8080"]
