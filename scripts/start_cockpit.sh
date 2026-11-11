#!/usr/bin/env bash
# ==============================================================================
# ASES 自主软件演进控制舱 (Cockpit) 一键启动脚本
#
# 用法:
#   ./scripts/start_cockpit.sh              # 默认：本地秒级启动 API 与前端控制舱 (/ui)
#   ./scripts/start_cockpit.sh --full       # 全栈：Docker 启动 PG + MinIO + Migrate + API + Worker
#   ./scripts/start_cockpit.sh --port 8080  # 指定端口启动
#   ./scripts/start_cockpit.sh --no-open    # 启动后不自动打开浏览器
# ==============================================================================

set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

HOST="${HOST:-0.0.0.0}"
PORT="${PORT:-8000}"
MODE="local"
AUTO_OPEN="true"

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
    --no-open)
      AUTO_OPEN="false"
      shift
      ;;
    -h|--help)
      echo "用法: $0 [--full] [--port PORT] [--host HOST] [--no-open]"
      echo ""
      echo "选项:"
      echo "  --full, --docker   使用 Docker Compose 启动完整后端栈 (PG + MinIO + Migrate + API + Worker)"
      echo "  --port, -p PORT    指定服务监听端口 (默认: 8000)"
      echo "  --host HOST        指定监听地址 (默认: 0.0.0.0)"
      echo "  --no-open          启动后不自动打开系统浏览器"
      exit 0
      ;;
    *)
      echo "未知参数: $1 (使用 --help 查看帮助)" >&2
      exit 1
      ;;
  esac
done

UI_URL="http://localhost:${PORT}/ui"

open_browser_when_ready() {
  local url="$1"
  if [[ "$AUTO_OPEN" != "true" ]]; then
    return 0
  fi
  (
    for _ in {1..30}; do
      if curl -sSf "http://localhost:${PORT}/ui" >/dev/null 2>&1; then
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
  echo -e "\033[34m➜ [全栈模式] 正在构建并启动 Docker 容器集群 (PostgreSQL + MinIO + API + Worker)...\033[0m"
  API_PORT="$PORT" docker compose build
  API_PORT="$PORT" docker compose up -d --wait --wait-timeout 120
  echo -e "\033[32m✔ 全栈服务已启动！\033[0m"
  echo -e "  • 前端控制舱 UI : \033[1;32m${UI_URL}\033[0m"
  echo -e "  • 健康检查端点  : http://localhost:${PORT}/healthz"
  echo -e "  • OpenAPI 文档  : http://localhost:${PORT}/docs"
  open_browser_when_ready "$UI_URL"
  exit 0
fi

# 3. 本地快速启动模式（直接启动 FastAPI + 前端控制舱）
if [[ -x ".venv/bin/uvicorn" ]]; then
  UVICORN_CMD=(".venv/bin/uvicorn")
elif command -v uv >/dev/null 2>&1; then
  UVICORN_CMD=("uv" "run" "uvicorn")
else
  echo -e "\033[31m✖ 未找到 .venv/bin/uvicorn 或 uv，请先执行: make install\033[0m" >&2
  exit 1
fi

echo -e "\033[34m➜ [本地模式] 正在启动 ASES 控制面 API 与前端控制舱...\033[0m"
echo -e "  • 控制舱地址 : \033[1;32m${UI_URL}\033[0m"
echo -e "  • API 文档   : http://localhost:${PORT}/docs"
echo -e "  • 按 Ctrl+C 停止服务\033[0m"
echo ""

open_browser_when_ready "$UI_URL"

exec "${UVICORN_CMD[@]}" apps.api.main:app --host "$HOST" --port "$PORT" --reload
