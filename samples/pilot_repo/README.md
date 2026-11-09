# tinylib —— 试点仓库

这是一个**小而真的**项目：它是阶段一自主演进闭环的**试点仓库**
（TASK-P1-082：系统在它上面自主完成一轮）。

**为什么要有它。** 基线（成本、时延、覆盖率）必须在一个**具体的仓库**上采，
才有意义 —— "一个典型任务花多少钱"这句话，离开"在哪个仓库上做的"就没有
答案。所以这里放一个足够小（跑一遍测试只要几秒）、又足够真（有真实逻辑、
有真实缺陷）的项目。

**它不是玩具。** 每个模块都有可以出错的地方，`tests/` 里是真的断言。基线
采的就是"在这种仓库上干活的代价"。

## 用法

```python
from tinylib.stats import mean, median, percentile
from tinylib.strings import slugify, truncate

mean([1, 2, 3])            # 2.0
median([1, 3, 2])          # 2
percentile([1, 2, 3, 4], 50)  # 2.5
slugify("Hello, World!")   # "hello-world"
truncate("a very long sentence", 10)  # "a very l…"
```

## 跑测试

```bash
python -m pytest
```

覆盖率：

```bash
python -m pytest --cov=src --cov-report=term-missing
```

## 目录

| 路径 | 是什么 |
| --- | --- |
| `src/tinylib/` | 库本体 |
| `tests/` | 单元测试 |
| `CHANGELOG.md` | 变更记录 |

## 换基线

基线表（`docs/evaluation/baselines.yaml`）是这个仓库的。**换仓库就要重采**：

```bash
python -m scripts.collect_baselines --pilot <另一个仓库>
```
