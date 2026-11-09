# 试点仓库的已知问题清单

这是 **tinylib 的"历史问题/缺陷"台账** —— 试点仓库选它当演化对象之前，
先把它身上真实存在的毛病一条条记下来，再由人工逐条判定"**这是不是有效
发现**"。

**为什么要有这份清单。** 误报率需要一个分母，而分母不能由被评价的一方
（自主演进系统）自己决定。所以流程是反过来的：

1. 先由**人**把真实存在的问题找出来，写成本清单；
2. 再由**人**逐条判定每一条"是不是有效发现"；
3. 判定结果落成 `evaluation/labels/pilot_labels.jsonl` —— **误报率的分母
   从那儿来**；
4. 系统在试点仓库上提出的候选，拿去和这份清单对 —— 对上了却**没有效**，
   就是误报。

**"有效发现"与"无效发现"的分界线。**

- **有效发现** —— 指出的确实是这个仓库里的真问题，或者是这个仓库真实缺
  的东西（缺测试、缺校验）。修掉它，仓库确实变好。
- **无效发现** —— 看起来像问题，但要么是**文档/代码里已经明说的设计取舍**
  （那不是缺陷，是意图），要么是**既有用例已经明确覆盖的行为**。报它等于
  要求系统去改一件本来就对的事 —— 那不是发现，是噪声。

**八条无效发现不是"凑数"。** 它们是这份清单里最值钱的部分：误报率的分母
之所以可信，靠的就是**有人认真看过并认定它们不是问题**。全是真问题的清单
会让误报率永远是 0，那是个没有意义的数。

**证据类型有两种，不许混。**

- `repro` —— 有一条**会失败的复现用例**（`known_issues/test_known_issues.py`
  里，标着 `xfail`）。缺陷是真的，而且**可复现**。
- `review` —— **人工评审**结论：要么是代码/文档审查（行为与文档写明的意图
  冲突），要么是覆盖测量（某处根本没有用例）。这类结论**没有**会失败的
  用例可写 —— 它们复现的是"缺一样东西"，不是"行为不对"。

每一条的判定、类别与证据类型都同步记在
`evaluation/labels/pilot_labels.jsonl` 里，**两边必须一致** ——
`tests/system/test_pilot_labels.py` 会核对，改了一边忘了另一边会被测出来。

---

### PILOT-001 —— `cli.py` 的 `stats` 子命令收到空输入时抛未捕获的异常

- **位置**：`src/tinylib/cli.py`
- **类别**：`runtime_error`
- **判定**：有效发现
- **证据类型**：`repro`
- **证据**：见 `known_issues/test_known_issues.py::test_the_cli_reports_an_empty_stats_input_instead_of_crashing`
- **现象**：`python -m tinylib.cli stats ""` 直接抛
  `ValueError: 空序列没有平均值`，用户看到的是一整段 traceback。
- **理由**：命令行是**面向人的**入口。领域层抛异常是对的，但入口要把它翻成
  "用法错误 + 退出码"。现在的样子让一个笔误变成了堆栈回溯。

### PILOT-002 —— `cli.py` 的 `--percentile` 越界时抛未捕获的异常

- **位置**：`src/tinylib/cli.py`
- **类别**：`runtime_error`
- **判定**：有效发现
- **证据类型**：`repro`
- **证据**：见 `known_issues/test_known_issues.py::test_the_cli_reports_an_out_of_range_percentile_instead_of_crashing`
- **现象**：`python -m tinylib.cli stats "1,2" --percentile 101` 抛
  `ValueError: 百分位必须在 0 到 100 之间：101.0`。
- **理由**：同上。而且百分位是**用户会随手写错的参数** —— 101 越界是极常见
  的手误，入口必须把它变成一句人话。

### PILOT-003 —— `--slug` 与 `--limit` 同时给出时，`--limit` 被静默忽略

- **位置**：`src/tinylib/cli.py`
- **类别**：`cli_semantics`
- **判定**：有效发现
- **证据类型**：`repro`
- **证据**：见 `known_issues/test_known_issues.py::test_the_cli_does_not_silently_drop_the_limit_option`
- **现象**：`python -m tinylib.cli text "Hello World" --slug --limit 5` 输出
  `hello-world` —— `--limit` 一个字都没生效，**也不报任何错**。
- **理由**：**静默丢弃用户给的选项是最坏的一种错法** —— 用户以为自己拿到
 的是被截断过的结果，实际上没有，而且从输出里看不出来。两个选项语义冲突
  时应当报用法错误（退出码 2），或者两个都生效；**不能装作没看见**。

### PILOT-004 —— `main` 遇到非法参数时抛 `SystemExit`，与"返回退出码"的契约不符

- **位置**：`src/tinylib/cli.py`
- **类别**：`api_contract`
- **判定**：有效发现
- **证据类型**：`repro`
- **证据**：见 `known_issues/test_known_issues.py::test_main_returns_an_exit_code_instead_of_raising`
- **现象**：`main` 的文档字符串写的是"跑一次。**返回进程退出码**"，但
  `main(["nosuchcommand"])` 抛的是 `SystemExit(2)` —— 调用方拿到的是异常，
  不是退出码。`main` 末尾那句 `parser.error("未知命令：…")` 因此**永远
  执行不到**（argparse 的必填子命令已经先报错了）。
- **理由**：**契约与实现不一致，调用方只能靠读实现才知道该怎么用。** 而且
  那句死代码会让人误以为"未知命令"这条路径被处理过。

### PILOT-005 —— `Money` 的文档字符串说"不可变"，`amount` 却可以直接改

- **位置**：`src/tinylib/money.py`
- **类别**：`immutability`
- **判定**：有效发现
- **证据类型**：`repro`
- **证据**：见 `known_issues/test_known_issues.py::test_money_amount_cannot_be_reassigned`
- **现象**：`money.amount = Decimal("999")` **不报错**。`__slots__` 只挡住
  新增属性，挡不住赋值。
- **理由**：模块文档明写"构造出来的金额**不可变**，币种改不了" —— 而实现
  拦不住任何一种修改。**金额被改掉是账本上的事**：一个被就地改过的金额
  会让持有它的那笔账在无人察觉的情况下变了。

### PILOT-006 —— `load_config` 接受空键

- **位置**：`src/tinylib/config.py`
- **类别**：`input_validation`
- **判定**：有效发现
- **证据类型**：`repro`
- **证据**：见 `known_issues/test_known_issues.py::test_load_config_rejects_an_empty_key`
- **现象**：配置文件里写 `=value`，`load_config` 返回 `{'': 'value'}` —— 不报
  错。空键在后续取值时和"没有这个键"混为一谈。
- **理由**：**配置写错了却读不出错**，是配置类缺陷里最难查的一种 —— 它不会
  在写入时报，只会在某天"这个设置怎么没生效"时才被发现。

### PILOT-007 —— `load_config` 遇到重复键时静默取最后一个

- **位置**：`src/tinylib/config.py`
- **类别**：`input_validation`
- **判定**：有效发现
- **证据类型**：`repro`
- **证据**：见 `known_issues/test_known_issues.py::test_load_config_reports_a_duplicated_key`
- **现象**：配置文件里写两行 `a=1` / `a=2`，`load_config` 返回
  `{'a': '2'}` —— 不报错，也不警告。
- **理由**：**"后者覆盖前者"是一种选择，而这里没做过这个选择。** 多数配置
  格式（INI、dotenv、systemd）在重复键上要么报错要么警告。静默取最后一个
  的话，配置文件里多写的一行就成了**看不见的改动**。

### PILOT-008 —— `word_wrap` 遇到比宽度还长的单词时不硬折行

- **位置**：`src/tinylib/strings.py`
- **类别**：`algorithm`
- **判定**：有效发现
- **证据类型**：`repro`
- **证据**：见 `known_issues/test_known_issues.py::test_word_wrap_breaks_a_word_longer_than_the_width`
- **现象**：`word_wrap("averyverylongword", 4)` 返回
  `['averyverylongword']` —— 13 个字符，而宽度是 4。
- **理由**：**"按宽度折行"这个承诺没有兑现。** 折行函数的用处就是把长行变短，
  而一个超长单词直接原样吐出来，调用方（比如要按宽度排版的渲染器）拿到
  的还是超宽的行。硬折行（切断单词）是标准做法。

### PILOT-009 —— 非正间隔在 `next_run` 里报错、在 `is_due` 里却静默返回

- **位置**：`src/tinylib/schedule.py`
- **类别**：`consistency`
- **判定**：有效发现
- **证据类型**：`repro`
- **证据**：见 `known_issues/test_known_issues.py::test_is_due_rejects_a_non_positive_interval_the_same_way_next_run_does`
- **现象**：`next_run(0)` 抛 `ValueError("间隔必须是正数")`；
  `is_due(NOW, 0)` 却**静默返回 `False`**。而且 `is_due(None, 0)` 返回
  `True` —— **同一个非法输入，两个结果**，取决于有没有上次运行时刻。
- **理由**：**同一个错误在同一个模块里有两种处理方式，调用方无从预期。**
  间隔非正本身就是配置错误，两处都该报错。

### PILOT-010 —— `cli.py` 完全没有测试，覆盖率 0%

- **位置**：`src/tinylib/cli.py`
- **类别**：`test_gap`
- **判定**：有效发现
- **证据类型**：`review`
- **证据**：覆盖测量 —— 试点仓库跑 `pytest --cov` 时
  `src/tinylib/cli.py` 的覆盖率是 **0%**（见 `docs/evaluation/pilot-repo.md`
  的基线表）。命令行是这个仓库**唯一的对外接口**，一条用例都没有。
- **理由**：**一个对外接口没有用例，等于这个接口的行为没有被定义过。**
  PILOT-001 ~ PILOT-004 四条缺陷全都落在这个文件里，而它们一条都没被
  现有用例挡住 —— 不是巧合。

### PILOT-011 —— `Money` 的 `is_zero` / `__hash__` / `__eq__`（与非 `Money` 比较）没有用例

- **位置**：`src/tinylib/money.py`
- **类别**：`test_gap`
- **判定**：有效发现
- **证据类型**：`review`
- **证据**：源码审查 + 覆盖测量 —— `tests/test_money.py` 只覆盖了构造、量化、
  币种归一、加法、乘法与 `total`；`is_zero`、`__hash__`、
  `__eq__` 与非 `Money` 比较这三条路径**没有任何用例**。
- **理由**：**`__hash__` 与 `__eq__` 是"这个类型能不能放进集合/当字典键"的
  全部依据** —— 它们错了，症状会出现在很远的地方（一个金额被当成两个）。
  `is_zero` 同理：金额是不是零，正是账本里最常问的问题。

### PILOT-012 —— `moving_average` 在"窗口长度恰好等于序列长度"的边界上没有用例

- **位置**：`src/tinylib/stats.py`
- **类别**：`test_gap`
- **判定**：有效发现
- **证据类型**：`review`
- **证据**：源码审查 —— `tests/test_stats.py` 只测了窗口 2（序列 4）与
  窗口 3（序列 2，**超长**）。**窗口恰好等于序列长度**这一档
  （`window == len(values)`，应当得到**恰好一个**窗口）没有用例。
- **理由**：**边界值恰恰是 bug 最爱藏的地方** —— 现有两条用例测的是
  "窗口比序列短"与"窗口比序列长"，中间那档被跳过了。少了它，
  `range(len(values) - window + 1)` 这类式子的 off-by-one 没人能发现。

### PILOT-013 —— 「`slugify` 会把中文与重音字符丢掉」

- **位置**：`src/tinylib/strings.py`
- **类别**：`documented_behavior`
- **判定**：无效发现
- **证据类型**：`review`
- **证据**：源码审查 —— `slugify` 的文档字符串明写"**非 ASCII 字母会被丢掉**
  —— 短横线形式的标识符本来就只该有 ASCII"，实现与文档一致。
- **理由**：**这是设计取舍，不是缺陷。** 短横线形式的标识符（URL slug、
  容器标签名）本来就只用 ASCII；真要支持中文，那是**加功能**，不是"修
  bug"。把它当缺陷报，等于要求系统去改一件本来就对的事。

### PILOT-014 —— 「`median` 取中间两个的平均，应改成取下中位数」

- **位置**：`src/tinylib/stats.py`
- **类别**：`documented_behavior`
- **判定**：无效发现
- **证据类型**：`review`
- **证据**：源码审查 —— `median` 的文档字符串明写"偶数个样本取中间两个的
  平均 —— 取下中位数会让一半样本落在'中位数'之上，那样'一半的样本比它小'
  这句话就不成立了"，且 `tests/test_stats.py` 里
  `test_median_of_even_count_averages_the_middle_two` 已明确覆盖。
- **理由**：**文档把"为什么这么算"写清楚了，用例把它钉住了。** 这不是缺陷，
  是对中位数定义的一次正确取舍。

### PILOT-015 —— 「`percentile` 应该取最近的样本，而不是插值」

- **位置**：`src/tinylib/stats.py`
- **类别**：`documented_behavior`
- **判定**：无效发现
- **证据类型**：`review`
- **证据**：源码审查 —— `percentile` 的文档字符串明写"用**线性插值**（与
  numpy 的默认口径一致）：位置 `p/100 * (n-1)`，两端之间的样本按小数部分
  插值"。
- **理由**：**插值口径是明写的，而且是与 numpy 对齐的** —— 换口径会让同一
  组数据在两个工具里给出不同的数，那是**更糟**的结果。

### PILOT-016 —— 「`Money.add` 遇到币种不同应该按汇率换算，而不是报错」

- **位置**：`src/tinylib/money.py`
- **类别**：`design_intent`
- **判定**：无效发现
- **证据类型**：`review`
- **证据**：源码审查 —— `add` 的文档字符串明写"**币种不一样就报错** ——
  换个币种记一笔账比记错数字更坏"，且 `tests/test_money.py` 里
  `test_add_rejects_a_different_currency` 已明确覆盖。
- **理由**：**这个模块里根本没有汇率表** —— 换算要引入汇率来源、汇率时点、
  舍入规则，那是另一个子系统。拿一个不存在的能力当缺陷报，等于凭空造出
  一个需求。

### PILOT-017 —— 「`merge` 应该做深合并」

- **位置**：`src/tinylib/config.py`
- **类别**：`documented_behavior`
- **判定**：无效发现
- **证据类型**：`review`
- **证据**：源码审查 —— `merge` 的文档字符串明写"**只有顶层是覆盖，不是深
  合并。** 深合并要问'两个都是字典时怎么办'，那个问题有多个合理答案，而
  配置文件通常不需要它"。
- **理由**：**文档把"为什么不做深合并"的理由都写下来了** —— 这是有意收窄的
  作用域，不是漏做。

### PILOT-018 —— 「`moving_average` 在窗口比序列长时应该返回部分窗口的平均，而不是空序列」

- **位置**：`src/tinylib/stats.py`
- **类别**：`documented_behavior`
- **判定**：无效发现
- **证据类型**：`review`
- **证据**：源码审查 —— `moving_average` 的文档字符串明写"窗口超出序列长度
  时返回空序列 —— **没有完整的窗口就没有平均值**"，且
  `tests/test_stats.py` 里 `test_moving_average_window_longer_than_the_series`
  已明确覆盖。
- **理由**：**"没有完整的窗口就没有平均值"是对的** —— 返回一个"半截窗口的
  平均"会让调用方误以为那是完整窗口的结果。

### PILOT-019 —— 「`total([])` 应该返回 0，而不是抛 `ValueError`」

- **位置**：`src/tinylib/money.py`
- **类别**：`documented_behavior`
- **判定**：无效发现
- **证据类型**：`review`
- **证据**：源码审查 —— `total` 的实现写明"空列表没有合计"并抛
  `ValueError`，且 `tests/test_money.py` 里
  `test_total_of_nothing_raises` 已明确覆盖。
- **理由**：**"空列表的合计是 0"在这类账本语义里是个陷阱** —— 忘了加一笔
  与"合计确实是 0"长得一模一样。抛错是刻意的：**逼调用方面对"我以为有
  东西"这件事**。

### PILOT-020 —— 「`parse_bool` 遇到拼错的布尔值应当报错，而不是回落到默认值」

- **位置**：`src/tinylib/config.py`
- **类别**：`documented_behavior`
- **判定**：无效发现
- **证据类型**：`review`
- **证据**：源码审查 —— `parse_bool` 的签名带 `default` 参数，
  `tests/test_config.py` 里 `test_parse_bool_falls_back_to_the_default`
  已明确覆盖"无法识别的值回落到 `default`"这一行为。
- **理由**：**"未知值回落到默认值"就是那个参数的用途** —— 有默认值的解析器
  与没有默认值的解析器是两种东西，这里选的是前者，而且有用例钉住。把它
  当缺陷报，就等于要求删掉一个**被明确设计过的参数**。
