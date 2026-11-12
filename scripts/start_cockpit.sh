#!/usr/bin/env bash
# ==============================================================================
# ASES 自主软件演进控制舱 (Cockpit) 一键启动脚本
#
# 用法:
#   ./scripts/start_cockpit.sh              # 默认：本地启动 API 与控制舱前端
#   ./scripts/start_cockpit.sh --full       # 全栈：Docker 启动 PG + MinIO + Migrate + API + Worker + 控制舱
#   ./scripts/start_cockpit.sh --port 8080  # 指定端口启动
#   ./scripts/start_cockpit.sh --no-open    # 启动后不自动打开浏览器
#   ./scripts/start_cockpit.sh --no-build   # 不构建前端（没有构建产物就用旧版内嵌页面）
#
# **前后端分离之后有两种形态，脚本各管一种。**
#
#   本地模式  vite 构建出来的 SPA 由**后端自己托管** ——
#             apps/api/static_frontend.py 发现 apps/dashboard/dist 就把它挂到 /，
#             否则退回旧版内嵌单页（apps/api/control.html）。
#             —— 一次命令、一个端口，操作员不用记第二个地址。
#
#   全栈模式  前端是**自己的容器**（docker-compose.yml 里的 dashboard 服务）：
#             nginx 发静态文件，并把 /api 反代给 api 容器。
#             反代之后前后端同源，于是**完全不需要 CORS**。
#             —— 前端有独立的发版节奏，不必拖着 Python 镜像一起重建。
# ==============================================================================

set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

HOST="${HOST:-0.0.0.0}"
PORT="${PORT:-8000}"
# 全栈模式下控制舱容器映射到宿主机的端口。
# 刻意不是 3000 —— 那是 vite 开发服务器的默认端口，
# 而 observability profile 里的 langfuse 也占着 3000。
DASHBOARD_PORT="${DASHBOARD_PORT:-8080}"
MODE="local"
AUTO_OPEN="true"
BUILD_FRONTEND="true"

DASHBOARD_DIR="apps/dashboard"
DIST_INDEX="${DASHBOARD_DIR}/dist/index.html"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --full|--docker)
      MODE="docker"
      shift
      ;;
    --port|-p)
      PORT="$2"
      shift 2
      ;;
    --host)
      HOST="$2"
      shift 2
      ;;
    --dashboard-port)
      DASHBOARD_PORT="$2"
      shift 2
      ;;
    --no-open)
      AUTO_OPEN="false"
      shift
      ;;
    --no-build)
      BUILD_FRONTEND="false"
      shift
      ;;
    -h|--help)
      echo "用法: $0 [--full] [--port PORT] [--host HOST] [--dashboard-port PORT] [--no-open] [--no-build]"
      echo ""
      echo "选项:"
      echo "  --full, --docker     使用 Docker Compose 启动完整栈 (PG + MinIO + Migrate + API + Worker + 控制舱)"
      echo "  --port, -p PORT      指定 API 监听端口 (默认: 8000)"
      echo "  --host HOST          指定监听地址 (默认: 0.0.0.0)"
      echo "  --dashboard-port N   指定控制舱前端端口（仅全栈模式，默认: 8080）"
      echo "  --no-open            启动后不自动打开系统浏览器"
      echo "  --no-build           不构建前端（没有构建产物就退回旧版内嵌单页）"
      exit 0
      ;;
    *)
      echo "未知参数: $1 (使用 --help 查看帮助)" >&2
      exit 1
      ;;
  esac
done

# 等服务就绪再开浏览器。
#
# **等的是"能访问"，不是"进程起来了"。** 进程起来了但端口还没 listen、
# 或者起来了却在初始化数据库 —— 这两种情况下打开浏览器都是白屏，
# 而操作员会以为"这玩意儿坏了"。所以这里轮询的是 HTTP 状态。
open_browser_when_ready() {
  local ready_url="$1"
  local url="$2"
  if [[ "$AUTO_OPEN" != "true" ]]; then
    return 0
  fi
  (
    for _ in {1..60}; do
      if curl -sSf "$ready_url" >/dev/null 2>&1; then
        echo -e "\033[32m✔ 控制舱已就绪，正在打开浏览器: ${url}\033[0m"
        if command -v open >/dev/null 2>&1; then
          open "$url" || true
        elif command -v xdg-open >/dev/null 2>&1; then
          xdg-open "$url" || true
        fi
        return 0
      fi
      sleep 0.5
    done
  ) &
}

# 前端构建产物是不是比源码旧了。
#
# **为什么要在意这个。** 本地模式下 SPA 是**静态文件** —— uvicorn 的 --reload
# 只管 Python，前端改完不会自动重新构建。于是"我明明改了，怎么界面还是旧的"
# 这个问题会反复出现，而原因藏在两个地方之间（改了 .tsx，但 dist 是三天前的）。
#
# 所以启动时对一下时间：源码比产物新，就重建一次。
frontend_is_stale() {
  [[ -f "$DIST_INDEX" ]] || return 0
  local newer
  newer="$(find "$DASHBOARD_DIR/src" "$DASHBOARD_DIR/index.html" "$DASHBOARD_DIR/package.json" \
    -newer "$DIST_INDEX" -print -quit 2>/dev/null || true)"
  [[ -n "$newer" ]]
}

# 构建前端。
#
# **失败不算错误。** 控制舱是一个"值班用的界面" —— 操作员半夜被叫醒时
# 敲一条命令就得看到东西，不该先记得跑一遍 npm run build，更不该因为
# 前端构建失败就什么都看不到。后端还留着旧版内嵌单页，它连的是同一个 API。
# 降级比报错强。
build_frontend() {
  if [[ "$BUILD_FRONTEND" != "true" ]]; then
    return 1
  fi
  if ! command -v npm >/dev/null 2>&1; then
    echo -e "\033[33m➜ 未找到 npm，跳过前端构建\033[0m"
    return 1
  fi
  if [[ ! -d "${DASHBOARD_DIR}/node_modules" ]]; then
    echo -e "\033[34m➜ 正在安装前端依赖 (npm ci) ...\033[0m"
    if ! (cd "$DASHBOARD_DIR" && npm ci >/dev/null 2>&1); then
      echo -e "\033[33m➜ 前端依赖安装失败，退回旧版内嵌单页\033[0m"
      return 1
    fi
  fi
  echo -e "\033[34m➜ 正在构建控制舱前端 (npm run build) ...\033[0m"
  if (cd "$DASHBOARD_DIR" && npm run build >/dev/null 2>&1); then
    return 0
  fi
  echo -e "\033[33m➜ 前端构建失败，退回旧版内嵌单页\033[0m"
  return 1
}

echo -e "\033[36m==============================================================\033[0m"
echo -e "\033[1;36m  ASES · Autonomous Software Evolution System 控制舱启动器\033[0m"
echo -e "\033[36m==============================================================\033[0m"

# 1. 确保 .env 配置文件存在
if [[ ! -f ".env" && -f ".env.example" ]]; then
  cp .env.example .env
  echo -e "\033[33m➜ 已从 .env.example 自动初始化 .env 配置文件\033[0m"
fi

# 2. 全栈 Docker 模式
if [[ "$MODE" == "docker" ]]; then
  DASHBOARD_UI_URL="http://localhost:${DASHBOARD_PORT}/"
  API_UI_URL="http://localhost:${PORT}/ui"
  echo -e "\033[34m➜ [全栈模式] 正在构建并启动 Docker 容器集群 (PostgreSQL + MinIO + API + Worker + 控制舱)...\033[0m"
  API_PORT="$PORT" DASHBOARD_PORT="$DASHBOARD_PORT" docker compose build
  API_PORT="$PORT" DASHBOARD_PORT="$DASHBOARD_PORT" docker compose up -d --wait --wait-timeout 120
  echo -e "\033[32m✔ 全栈服务已启动！\033[0m"
  echo -e "  • 控制舱 (新前端) : \033[1;32m${DASHBOARD_UI_URL}\033[0m"
  echo -e "  • 旧版内嵌单页    : ${API_UI_URL}"
  echo -e "  • 健康检查端点    : http://localhost:${PORT}/healthz"
  echo -e "  • OpenAPI 文档    : http://localhost:${PORT}/docs"
  # 等的是**控制舱容器**那个地址 —— 打开的是它，就等它。
  open_browser_when_ready "$DASHBOARD_UI_URL" "$DASHBOARD_UI_URL"
  exit 0
fi

# 3. 本地快速启动模式（FastAPI + 前端控制舱）
if [[ -x ".venv/bin/uvicorn" ]]; then
  UVICORN_CMD=(".venv/bin/uvicorn")
elif command -v uv >/dev/null 2>&1; then
  UVICORN_CMD=("uv" "run" "uvicorn")
else
  echo -e "\033[31m✖ 未找到 .venv/bin/uvicorn 或 uv，请先执行: make install\033[0m" >&2
  exit 1
fi

# 本地模式下前端由**后端自己托管**：有构建产物就是新 SPA，没有就退回旧版内嵌单页。
#
# 两种情况的 URL 不同（"/" 与 "/ui"），所以这里要把 UI_URL 定下来，
# 后面打印和开浏览器都用它。
UI_URL="http://localhost:${PORT}/ui"
READY_URL="http://localhost:${PORT}/ui"
FRONTEND_MODE="旧版内嵌单页"

if [[ -f "$DIST_INDEX" ]] && ! frontend_is_stale; then
  FRONTEND_MODE="独立前端 (SPA)"
  UI_URL="http://localhost:${PORT}/"
  READY_URL="http://localhost:${PORT}/"
elif build_frontend && [[ -f "$DIST_INDEX" ]]; then
  FRONTEND_MODE="独立前端 (SPA)"
  UI_URL="http://localhost:${PORT}/"
  READY_URL="http://localhost:${PORT}/"
fi

echo -e "\033[34m➜ [本地模式] 正在启动 ASES 控制面 API 与控制舱前端...\033[0m"
echo -e "  • 控制舱地址 : \033[1;32m${UI_URL}\033[0m  (${FRONTEND_MODE})"
echo -e "  • API 文档   : http://localhost:${PORT}/docs"
if [[ "$FRONTEND_MODE" != "独立前端 (SPA)" ]]; then
  echo -e "  • \033[33m提示: 跑一次 make dashboard-build 即可切到独立前端\033[0m"
fi
echo -e "  • 按 Ctrl+C 停止服务\033[0m"
echo ""

open_browser_when_ready "$READY_URL" "$UI_URL"

exec "${UVICORN_CMD[@]}" apps.api.main:app --host "$HOST" --port "$PORT" --reload
