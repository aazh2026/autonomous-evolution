.DEFAULT_GOAL := help
SHELL := /bin/bash
PY ?= uv run
COMPOSE ?= docker compose

.PHONY: help
help: ## 显示可用目标
	@grep -hE '^[a-zA-Z0-9_-]+:.*?## .*$$' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-16s\033[0m %s\n", $$1, $$2}'

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

# M1 单轮闭环演示（TASK-P1-081 的 T3 验收）：真的走一遍
# 候选 → 决策 → 任务 → 执行 → 独立验证 → 记录。
#
# **要一个真的 PostgreSQL** —— 那些记录得真的在库里（T1 的验收口径就是
# "每一步的 DB 记录可查"）。先 `make up`。
#
# 跑完不清库是故意的：留下这一轮的行，才好对着 docs/demo/m1-walkthrough.md
# 里的查询语句自己查。
.PHONY: demo-m1
demo-m1: ## M1 单轮闭环演示（候选 → 决策 → 任务 → 执行 → 独立验证 → 记录）
	uv run python -m scripts.demo_m1_cycle

# M2 自主闭环演示（TASK-P1-082 的 T3 验收）：
# 只给 project.yaml，系统自主发掘候选、决策不行动（defer）、写代码修复缺陷、
# 独立验证、计算目标达成度增量并更新差距。
#
# 同样需要真实 PostgreSQL（先 make up）。
.PHONY: demo-m2
demo-m2: ## M2 自主闭环演示（自主发掘 → 不行动验证 → 修复 → 目标增量驱动）
	uv run python -m scripts.demo_m2_cycle

# ---------- 本地环境 ----------

# 启动等待时间（秒）。
#
# `--wait` 会等所有带健康检查的服务变成 healthy。给一个明确的上限，
# 而不是让它无限等 —— "卡住"和"就绪"必须能区分开。
COMPOSE_WAIT_TIMEOUT ?= 120

# 重建应用镜像。
#
# **为什么单独有一个 build：`up` 不会重建。**
# `docker compose up -d` 用的是**上一次构建出来的镜像** —— 源码改了、
# 镜像没重建，容器里跑的就是旧代码，而且看起来一切正常（容器是 healthy 的）。
#
# 这个坑踩过：改完 S3 客户端复用的代码、提交了、推了，容器里跑的还是
# 旧实现 —— 修复等于没上。`make build` 之后 `make up` 才算数。
#
# 依赖层是缓的（Dockerfile 先拷依赖清单再拷源码），所以改一行业务代码
# 的重建只要十几秒。
.PHONY: build
build: ## 重建应用镜像（**改过源码之后必须先跑这个**）
	$(COMPOSE) build

.PHONY: up
up: ## 启动本地全栈（PG + MinIO + API + worker）—— 不重建镜像，源码改过先 make build
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
