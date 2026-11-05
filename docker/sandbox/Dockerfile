# ASES 沙箱基础镜像
#
# 设计依据：docs/design.md §3.5A（R-046 / R-047：Docker 为 POC 首选执行环境）、
# docs/tasks.md TASK-P1-006。
#
# 这个镜像存在的意义是：**Agent 在里面碰不到不该碰的东西**。三条硬要求：
#
# 1. **非 root** —— 容器里是普通用户，不是 root。
# 2. **不带任何凭据** —— 镜像里没有 ~/.ssh、~/.aws、~/.docker/config.json。
#    宿主机的凭据也**绝不**挂载进来（design.md §3.5D：禁止 Coding Agent
#    直接访问宿主机的 SSH 凭据、云平台管理员密钥和生产数据库）。
# 3. **出站网络默认关闭** —— 运行时的默认网络模式是 ``none``
#    （``Settings.sandbox_network``）。镜像本身不监听任何端口。
#
# 装 openssh-client 是**故意的反面**：沙箱里不装 SSH 客户端。
# 一个连 ``ssh`` 都没有的镜像，"没有 SSH 凭据"就不只是"没挂载"，
# 而是"想用也没有"。

FROM python:3.12-slim-bookworm

ARG SANDBOX_UID=1000
ARG SANDBOX_GID=1000
ARG SANDBOX_USER=ases

ENV DEBIAN_FRONTEND=noninteractive \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    HOME=/home/ases

# git / nodejs 是执行器的实际依赖：Coding Agent 要能 clone、要看仓库历史、
# 要跑前端的构建脚本。python3 由基础镜像提供。
RUN apt-get update \
 && apt-get install --no-install-recommends -y \
      ca-certificates \
      git \
      nodejs \
      npm \
 && rm -rf /var/lib/apt/lists/*

# 非 root 用户与工作目录。
RUN addgroup --gid "${SANDBOX_GID}" "${SANDBOX_USER}" \
 && adduser --uid "${SANDBOX_UID}" --gid "${SANDBOX_GID}" \
            --home "/home/${SANDBOX_USER}" --no-create-home \
            --gecos "" --disabled-password --shell /bin/bash "${SANDBOX_USER}" \
 && mkdir -p /workspace "/home/${SANDBOX_USER}" \
 && chown -R "${SANDBOX_UID}:${SANDBOX_GID}" /workspace "/home/${SANDBOX_USER}"

USER ${SANDBOX_UID}:${SANDBOX_GID}
WORKDIR /workspace

# 构建期的兜底断言。凭据目录一旦存在就在构建期失败 ——
# 构建期失败是"这次别发布"，运行期失败是"任务跑挂了"，前者便宜得多。
RUN set -eu; \
    for path in "${HOME}/.ssh" "${HOME}/.aws" "${HOME}/.docker" "/root/.ssh" "/root/.aws"; do \
      if [ -e "${path}" ]; then \
        echo "沙箱镜像里不允许存在 ${path}" >&2; exit 1; \
      fi; \
    done; \
    if [ "$(id -u)" = "0" ]; then \
      echo "沙箱镜像不允许以 root 运行" >&2; exit 1; \
    fi

CMD ["sleep", "infinity"]
