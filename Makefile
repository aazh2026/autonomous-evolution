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

# ---------- 本地环境 ----------

.PHONY: up
up: ## 启动本地全栈（PG + MinIO + Langfuse + API）
	$(COMPOSE) up -d --wait

.PHONY: down
down: ## 停止本地全栈
	$(COMPOSE) down

.PHONY: logs
logs: ## 查看本地全栈日志
	$(COMPOSE) logs -f --tail=100

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
