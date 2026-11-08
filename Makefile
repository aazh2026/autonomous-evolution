.DEFAULT_GOAL := help
SHELL := /bin/bash
PY ?= uv run
COMPOSE ?= docker compose

.PHONY: help
help: ## 显示可用目标
	@grep -hE '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-16s\033[0m %s\n", $$1, $$2}'

# ---------- 工具链 ----------

.PHONY: install
install: ## 安装依赖（含 dev）
	uv sync --extra dev

.PHONY: lint
lint: ## Ruff 静态检查
	uv run ruff check .

.PHONY: format
format: ## Ruff 格式化
	uv run ruff format .

.PHONY: typecheck
typecheck: ## mypy 严格类型检查
	uv run mypy .

.PHONY: check
check: lint typecheck test ## 本地全量检查（lint + typecheck + test）

# ---------- 测试 ----------

.PHONY: test
test: ## 单元测试（不含 integration）
	uv run pytest -m "not integration"

.PHONY: test-all
test-all: ## 全部测试（含 integration，需先 make up）
	uv run pytest

.PHONY: test-cov
test-cov: ## 覆盖率报告
	uv run pytest --cov --cov-report=term-missing --cov-report=html

# ---------- 演示 ----------

# 审批门演示（TASK-P1-046 的 T3 验收）：真的建 git 仓库、真的跑测试、真的合并。
# 不需要数据库与 Docker —— 它在临时目录里造一个自己的仓库。
.PHONY: demo-approval
demo-approval: ## 人工审批门演示（验证通过 → 审批 → 合并到 main）
	uv run python -m scripts.demo_approval_gate

# ---------- 本地环境 ----------

# 启动等待时间（秒）。
#
# `--wait` 会等所有带健康检查的服务变成 healthy。给一个明确的上限，
# 而不是让它无限等 —— "卡住"和"就绪"必须能区分开。
COMPOSE_WAIT_TIMEOUT ?= 120

.PHONY: up
up: ## 启动本地全栈（PG + MinIO + API + worker）
	$(COMPOSE) up -d --wait --wait-timeout $(COMPOSE_WAIT_TIMEOUT)

.PHONY: up-observability
up-observability: ## 启动本地全栈并带上 Langfuse（较慢，见 docs/tasks.md TASK-P1-007）
	$(COMPOSE) --profile observability up -d --wait --wait-timeout $(COMPOSE_WAIT_TIMEOUT)

.PHONY: down
down: ## 停止本地全栈（**保留数据卷**）
	$(COMPOSE) down

.PHONY: logs
logs: ## 查看本地全栈日志
	$(COMPOSE) logs -f --tail=100

.PHONY: ps
ps: ## 查看本地全栈状态
	$(COMPOSE) ps

.PHONY: health
health: ## 查看 API 健康检查结果
	@curl -sS http://localhost:$${API_PORT:-8000}/healthz | $(PY) -m json.tool

.PHONY: reset
reset: ## 停止并清空本地数据卷
	$(COMPOSE) down -v

# ---------- 数据库 ----------

.PHONY: migrate
migrate: ## 应用数据库迁移
	uv run alembic upgrade head

.PHONY: migrate-down
migrate-down: ## 回滚一级数据库迁移
	uv run alembic downgrade -1

.PHONY: revision
revision: ## 生成新迁移：make revision m="说明"
	uv run alembic revision --autogenerate -m "$(m)"

.PHONY: psql
psql: ## 打开数据库 CLI
	$(COMPOSE) exec postgres psql -U ases -d ases

# ---------- 杂项 ----------

.PHONY: seed
seed: ## 载入示例项目配置
	uv run python -m scripts.seed

.PHONY: precommit
precommit: ## 运行 pre-commit 全量钩子
	uv run pre-commit run --all-files
