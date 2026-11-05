# ASES 应用镜像（API / worker 共用）
#
# 设计依据：docs/tasks.md TASK-P1-007（``make up`` 一键启动全栈）。
#
# 这个镜像装的是**应用**，不是沙箱 —— 沙箱是给被演进的目标项目用的
# （见 docker/sandbox/Dockerfile）。两者的隔离要求不同：
#
#   沙箱镜像：**不能**有 root、不能有凭据、出站网络默认关闭。
#   应用镜像：不需要 root，但**需要**能连数据库和对象存储。
#
# 用 uv 而不是 pip：锁文件（uv.lock）已经在版本库里，
# 镜像里装出来的依赖版本与本地开发、CI 完全一致。
# 依赖层先于源码层 —— 改一行业务代码不会让依赖重新下载。

# 基础镜像做成参数，是为了能指向内部镜像仓库（``docker compose build
# --build-arg BASE_IMAGE=registry.internal/python:3.12-slim-bookworm``）。
# 公共仓库不可达时，至少还有一条路。
ARG BASE_IMAGE=python:3.12-slim-bookworm
FROM ${BASE_IMAGE}

# uv —— 一个极快的 Python 包管理器
COPY --from=ghcr.io/astral-sh/uv:0.9.22 /uv /uvx /bin/

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never \
    PATH="/app/.venv/bin:$PATH"

WORKDIR /app

# 先只拷贝依赖清单，让依赖层可以被缓存。
COPY pyproject.toml uv.lock README.md ./

# --frozen：严格按 uv.lock 装，不重新解析、不改锁文件。
#   镜像里的依赖版本因此是可复现的 —— 与 uv.lock 一一对应。
# --no-dev：不装 pytest / mypy / ruff。运行镜像不需要它们，
#   少了它们镜像更小、攻击面更小。
# --extra s3：MinIO 后端需要 aioboto3。
RUN uv sync --frozen --no-dev --extra s3 --no-install-project

# 再拷贝源码并把项目本身装进去。
COPY apps ./apps
COPY core ./core
COPY agents ./agents
COPY orchestration ./orchestration
COPY executors ./executors
COPY harness ./harness
COPY knowledge ./knowledge
COPY evaluation ./evaluation
COPY infrastructure ./infrastructure
COPY migrations ./migrations
COPY alembic.ini ./

RUN uv sync --frozen --no-dev --extra s3

# 同样非 root 运行。应用镜像没有沙箱那么严格的理由，但也没有理由给 root ——
# 容器逃逸的第一道防线就是容器内不是 root。
RUN addgroup --gid 1000 ases \
 && adduser --uid 1000 --gid 1000 --home /home/ases --no-create-home \
            --gecos "" --disabled-password --shell /bin/bash ases \
 && mkdir -p /home/ases /app \
 && chown -R ases:ases /app /home/ases

USER 1000:1000

# 容器内的健康检查由 compose 定义（见 docker-compose.yml）。
# 这里不写 HEALTHCHECK —— compose 的 healthcheck 会覆盖它，
# 而且 compose 版本里还能写 depends_on 的 condition，Dockerfile 里写不了。

CMD ["uvicorn", "apps.api.main:app", "--host", "0.0.0.0", "--port", "8000"]
