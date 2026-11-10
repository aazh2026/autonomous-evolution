"""M2 自主闭环的预算与耗时核验。

设计依据：docs/tasks.md TASK-P1-082。
"""

from __future__ import annotations

from apps.director.m1_budget import (
    BASELINE_RELATIVE_PATH,
    COST_UNIT,
    DEFAULT_FACTOR,
    DURATION_UNIT,
    M1BudgetCheck,
    M1BudgetError,
    check_m1_budget,
    load_baseline,
)

#: M2 预算检查复用相同的基线口径。
M2BudgetCheck = M1BudgetCheck
M2BudgetError = M1BudgetError
check_m2_budget = check_m1_budget

__all__ = [
    "BASELINE_RELATIVE_PATH",
    "COST_UNIT",
    "DEFAULT_FACTOR",
    "DURATION_UNIT",
    "M2BudgetCheck",
    "M2BudgetError",
    "check_m2_budget",
    "load_baseline",
]
