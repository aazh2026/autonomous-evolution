# 项目知识库与架构认知 —— 索引、架构模型、ADR、项目记忆

设计依据：`docs/tasks.md` TASK-P2-016（交付物：架构知识图谱、ADR 存储、模块
依赖索引；验收条件：系统能回答"某模块被哪些模块依赖"且答案来自已构建的索引
而非每次重读仓库）、R-064（不每轮重读仓库重建认知，T3）、R-067（不全塞向量库，
不用对话历史当事实，T1）、R-068（十二个核心实体）；`docs/design.md` §6（四类
存储的职责分离）。

> **这一层是阶段二的第一个交付物。** 它回答的是一个很具体的问题：
> **下一轮演进开始的时候，系统怎么在不重读整个仓库的前提下，知道"这个模块
> 被谁依赖着""这个模块当初为什么这么设计""上一轮做过什么"。**

## 0. 一句话

**把"读仓库"这件事变成"读索引"。** 索引是构建一次、反复查询的；仓库是每轮
都要重读的。R-064 要的是前者。

## 1. 四块东西

| 东西 | 落点 | 回答什么问题 |
| --- | --- | --- |
| 仓库索引 | `knowledge/repository_index/` | 谁依赖谁？改这里会影响谁？ |
| 架构模型 | `knowledge/architecture/model.py` | 分层对不对？有没有环？哪些公开接口没人碰？ |
| ADR 存储 | `core/architecture/decisions.py`（端口）+ `knowledge/architecture/decisions.py`（文件）+ `infrastructure/database/repositories.py`（数据库） | 这个模块当初为什么这么设计？ |
| 项目记忆 | `knowledge/project_memory/` | 上一轮做过什么？哪些缺陷已经修好了？ |

**它们之间的关系是一条链**：索引给出**事实**（谁依赖谁），架构模型在事实
上给出**判断**（这个依赖方向有问题），ADR 给出**理由**（为什么当初这么定），
记忆给出**跨轮的状态**（哪些事已经做过）。

## 2. 仓库索引 —— 一次构建，多次查询

### 2.1 它怎么被度量

**验收条件那句话是可测的**：把仓库删掉，再问同一个问题，答案还在，就说明
答案来自索引。

```python
index = build_index(repo)
store = RepositoryIndexStore(work / "index.json")
store.save(index)
shutil.rmtree(repo)  # 仓库整个删掉
loaded = store.load()
loaded.dependents("tinylib.stats")  # → ('tests.test_stats', 'tinylib.cli')
```

这条断言在 `tests/unit/knowledge/test_repository_index.py::Test依赖查询::
test_答案来自索引而不是仓库` 里，它**先删仓库再问** —— 不是"顺便也落了个盘"。

**刷新不是重建。** 索引里每个文件记着指纹 `(大小, 修改时间, 内容哈希)`；
刷新时指纹没变的文件**一个字节都不读**：

```python
index = build_index(repo)
monkeypatch.setattr(pathlib.Path, "read_bytes", spy)
refreshed = refresh_index(repo, index)
assert reads == []  # 仓库没变 → 读了 0 个文件
```

改一个文件之后重读**恰好一个**。这条也用"数读了多少次"来验，不是靠感觉。

**摘要能看出源码变了。** 索引摘要把每个文件的路径、层、符号、导入与**内容
哈希**都算进去 —— 所以"索引一样但源码不一样"这种事一定会被摘要看出来。
两份索引看起来一模一样，用起来却不是同一件事，这种情况不能发生。

### 2.2 它怎么算

- **模块名**：去掉 `src/` 根的虚线路径（`src/tinylib/cli.py` → `tinylib.cli`）。
  `src/` 之下才是真正的包，模块名不该带 `src`。
- **依赖边**：AST 解析 `import` / `from ... import ...`。`from a.b import c`
  解析成 `a.b.c`（存在）或 `a.b`（不存在）—— 这正是 `from tinylib import cli`
  能落到 `tinylib.cli` 的原因。
- **相对导入**：按包上下文展开（`from . import leaf` → `pkg.leaf`）。
- **外部依赖不进图**：`import os` 记下来，但不产生内部依赖边。
- **坏文件留在索引里**：语法错误的文件带着 `parse_error` 继续待在索引中。
  **坏掉的文件是事实** —— 索引里要看得见它坏了，而不是当它不存在。

### 2.3 分层

按顶层目录判层：`src/` → 源，`tests/` → 测试，`known_issues/` → 缺陷复现，
`scripts/` → 脚本，其余 → 其它。分层的用处是**方向**：产品代码依赖测试代码
是架构问题，测试依赖产品不是。

## 3. 架构模型 —— 在索引上做判断

### 3.1 它能回答什么

| 问题 | 方法 |
| --- | --- |
| 这个模块在哪一层 | `layer_of` |
| 谁依赖它 | `dependents` |
| 改它会影响谁（传递） | `impact_of` / `transitive_dependents` |
| 有没有依赖环 | `cycles` |
| 有没有分层违规 | `violations` |
| 哪些公开接口没有任何用例引用过 | `unreferenced_public_symbols` |
| 哪些源模块没有任何用例引用过 | `untested_modules` |
| 哪些模块是入口 | `entry_points` |

### 3.2 最有用的一条：结构性覆盖率缺口

`unreferenced_public_symbols()` 回答的是：**这个对外接口，没有任何一条自有用例
引用过**。

这是覆盖率缺口的**静态版本**。试点仓库 KR-002 用覆盖率量（`cli` 模块 0%），
这一层用引用关系量（没有任何用例 import 过 `tinylib.cli`）—— 两种量法指向同一
件事，而这一种**不用跑测试**。

**粒度到符号，不到模块。** `from api import used` 只说明 `used` 被用过，
`api` 里的其它公开符号仍然可能没人碰。只看模块的话，"这个模块有人 import"
会把整个模块的缺口都盖住。而**类的方法跟着类走** —— `from api import Money`
拿到的类，它的方法不能算"没人碰"（那条用例完全可能正在调 `Money.total`）。

### 3.3 依赖环怎么找

Kosaraju（迭代版，不递归）求强连通分量。**迭代是刻意的** —— 递归在环上不会
停，而环恰恰是这里要找的东西。

## 4. ADR 存储 —— 决策不可变，只能被取代

### 4.1 两套存储，一个端口

架构决策要存两处：Git 里的 Markdown（**给人读**）与 PostgreSQL 里的行
（**给系统读**）。

**端口在 `core/architecture/decisions.py`，不在 `knowledge/` 里** —— 因为它有
两头：两个实现分处两层（`knowledge/` 与 `infrastructure/`），端口跟着其中
一个走，另一个就得反过来依赖它，而 `infrastructure` 依赖 `knowledge` 是反的。
所以端口与它的数据类型留在最底下的 `core/`，两头都往上指它。

端口是**异步的**：数据库那一份的会话是异步的，而端口由**存系统的**那一头
定义 —— 两边都照它实现，文件那一边用 `asyncio.to_thread` 把同步的文件读写挪
出事件循环。

**两套存储的语义必须一致** —— 否则"Git 里写的决策"与"库里那条决策"会各说
各话，而**各说各话的事实比没有事实更坏**。集成测试
`tests/integration/test_knowledge_decisions.py::Test两套存储一致` 拿同一组
决策分别写两遍，然后比"哪条还生效"与"取代链怎么串"这两个答案。

### 4.2 决策不可变

新决策用 `supersedes` 指向旧的，旧的**不删不改**。"已被取代"这件事是**推导**
出来的（谁被指向了谁就是被取代了），不需要回头改旧文件 —— 而"回头改旧文件"
正是那种会在并发或中断下留下半截状态的操作。

**编号只进不退。** 编号一旦发出去就作废，哪怕那个文件后来被人手工删掉。
**重号会毁掉取代链**：有一条决策写着"取代 adr-0002"，而 adr-0002 后来被删了、
又发了一个同号的，"谁取代了谁"就会指向一条完全不相干的决策，而且**看不出来**。
所以宁可跳号（留个空洞）也不重号 —— 空洞看得见，重号看不见。

写文件用**独占创建**（`Path.open("x")`）：并发的两个进程可能算出同一个编号，
撞上了就换下一个，而不是把对方写的内容覆盖掉。

### 4.3 文件长什么样

```markdown
---
created_at: '2026-11-09T16:28:52.698408+00:00'
id: adr-0002
project_id: pilot-tinylib
status: accepted
supersedes: adr-0001
---

# adr-0002

## 背景

索引落盘之后，改一个文件就得重扫全仓库才能确认没漏

## 决策

刷新时按 (大小, 修改时间) 判断哪些文件需要重读

## 代价

文件被改过又改回原样时会多读一次 —— 少见，且无害
```

**前言放结构化字段，正文放人读的内容。** 这样既能在 Git 里读（`git diff` 看
得出改了哪条决策），又能被程序读（解析前言拿到结构化字段）。

## 5. 项目记忆 —— 跨轮对照

### 5.1 它解决什么

试点仓库里有一条名为 `PILOT-001` 的已知缺陷；一轮演进把它修好之后，下一轮
的发现器如果还把它当成"新发现"提出来，那就是系统在**重复劳动** —— 而更糟的是
它会再修一次、再验证一次、再花一次钱。

**记住"这条已经修好了"是自主演进能连续跑的前提。**

### 5.2 事实的四种

| 种类 | 键 | 用途 |
| --- | --- | --- |
| `issue_fixed` | 缺陷编号（`PILOT-001`） | 跨轮对照要用的那份清单 |
| `decision` | 决策编号 | 做过什么决定 |
| `constraint` | 约束名 | 非目标、边界条件 |
| `observation` | 观察编号 | 测量结果、架构发现 |

**键是跨轮对照时唯一能用的东西** —— 摘要会改，键不会。

### 5.3 记住的事只能追加

同一条事实再记一次，是**新的修订**（`<key>.md` → `<key>.r2.md`），不是覆盖旧的。
"当时记的是什么"是历史的一部分，而覆盖等于让历史只剩最后一次。当前生效的那
一版由**修订号最大的那个文件**决定 —— 这条规则是推导出来的，不是存出来的。

## 6. 为什么不是向量库

R-067 写得很直接：**不要把所有项目知识都存进向量数据库，也不要把 Agent 的
对话历史当作项目事实。** `docs/design.md` §6 的四类存储里，"语义检索"那一类
是**按需**的（"PostgreSQL + pgvector（按需）"），它的职责是**知识发现** ——
给 Agent 找相关材料。而这一层要回答的问题全是**精确**的：

| 问题 | 向量检索给得出吗 |
| --- | --- |
| 谁依赖 `tinylib.stats`？ | 给不出 —— "最像的若干条"不是依赖关系 |
| `PILOT-001` 修好了吗？ | 给不出 —— 要的是精确命中 |
| 这个模块的取代链是什么？ | 给不出 —— 要的是图上的可达性 |

**依赖关系、取代关系、键命中 —— 这三样都是结构化查询，不是相似度查询。**
把它们塞进向量库，得到的不是"更聪明的检索"，而是**丢了结构的检索**。

而"用对话历史当事实"更糟：对话历史是**执行轨迹**，不是知识来源。同一件事在
不同轮次里被说过 differently 时，你没法知道哪句是当时的决定、哪句是后来的
复述。**事实必须是写下来的、可以被 Git 追踪的。**

## 7. 落在哪儿

```
core/architecture/decisions.py             端口 + 领域模型（DecisionRecord）
knowledge/repository_index/index.py        索引：模块、符号、导入、依赖边
knowledge/repository_index/store.py        索引的落盘与刷新
knowledge/architecture/model.py            架构模型：分层、环、违规、缺口
knowledge/architecture/decisions.py        ADR 的文件存储（Git + Markdown/YAML）
knowledge/project_memory/memory.py         项目记忆（Markdown + YAML 前言）
infrastructure/database/repositories.py    ArchitectureDecisionRepository（数据库那一半）
```

**依赖方向**：`core` ← `knowledge` ← `infrastructure` ← `apps`。端口在 `core`，
所以 `infrastructure` 不需要认识 `knowledge`。

## 8. 怎么跑

```bash
# 单元测试
uv run pytest tests/unit/knowledge -q

# 集成测试（需要真实 PostgreSQL）
uv run pytest tests/integration/test_knowledge_decisions.py -q

# 演示
uv run python -m scripts.demo_knowledge
```

演示会：给试点仓库建索引 → 问"谁依赖 `tinylib.stats`" → 刷新（没变的文件
读了 0 个）→ **把仓库整个删掉再问一次** → 打印架构认知 → 记一条 ADR 并演示
取代链 → 记一条项目记忆并演示"第二轮不再提已修好的缺陷" → （给了
`--database-url` 的话）两套存储的取代链当场比一遍。

## 9. 边界

**这一层不做的事**（留给后面的任务）：

- **不替 Agent 做判断。** 这里给的是事实与结构化的缺口，决策仍然归 Director。
- **不做语义检索。** "找相关的代码片段"是阶段三的事（`docs/tasks.md` 的语义
  检索工作流），而那一类问题才需要向量库。
- **不跨仓库。** 一个索引对应一个仓库根目录。
- **索引可能与仓库不同步。** 摘要里记着每个文件的内容哈希，不同步是**看得见**
  的 —— 这就是为什么指纹里要带内容哈希而不只是修改时间。
