"""失败分类与定向修复 —— "哪儿坏了、改哪一行"。

设计依据：docs/design.md §6（失败诊断与有界修复）；docs/tasks.md
TASK-P2-015（"阶段一已有的有界修复扩展到多失败类型"；"自动定位失败原因并
生成 Targeted 修复指令，修复次数与预算严格受全局配额限制"）、R-098、I-04。

**阶段一的归类停在"能不能自己修"这一级。**
:class:`core.task.repair.FailureKind` 有七类：必检项没过、执行器出错、
验证引擎出错、取消、超时、前提不成立、判不出。**其中"必检项没过"这一类
装着语法错、依赖缺失、断言失败、类型不匹配这四种完全不同的东西** ——
而"改哪儿"这层信息在阶段一被压掉了。

**这一层把那一类拆开。** :class:`FailureType` 说"是哪种错"，
:class:`Classification` 说"改哪儿"，:meth:`Classification.to_diagnosis` 把
结论翻译回阶段一那份 :class:`core.task.execution_loop.FailureDiagnosis` ——
于是有界修复的三重上限、每任务闸门与全局账本**原样接着用**，不另立一套。

**归类靠结构化的东西，不靠读人话。** 阶段一在这件事上的理由要再说一遍：
措辞是工具定的，改一次措辞就改一次分类，而分类决定"要不要自己修"。
所以这里的输入是**工具的机器可读输出** —— ruff 的 JSON、mypy 的 JSON 行、
pytest 的 JUnit XML、Python 自己抛的 SyntaxError。
:data:`_CODE_TO_TYPE` 是"一张表，改一处"：新工具只要把自己的诊断码归一化到
这张表里，就白拿一份分类。

**判不出就判不出。** 认不出来的输出归 :attr:`FailureType.UNKNOWN`，
而"判不出"归"需人工" —— 猜出来的类别会决定"要不要自己修"，猜错一次就是
一次不该发生的返工。
"""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Final, cast
from xml.etree import ElementTree

from core.task.execution_loop import FailureCategory, FailureDiagnosis, RepairTask
from core.task.repair import FailureKind, build_repair_task


class FailureType(StrEnum):
    """失败的细类 —— 阶段一只有"必检项没过"这一类，这里把它拆开。

    **每一类的后续动作不同。** 语法错自己改；依赖版本对不上得有人拍板
    （选哪个版本是口径问题）；架构违规是设计决定，改之前要有人认。
    归错类就是**自动修一件不该自动修的事**。
    """

    #: 语法错 —— 代码根本解析不了。改完之前别的都白说。
    SYNTAX_ERROR = "syntax_error"
    #: 依赖缺失 —— 找不到模块 / 找不到名字。
    IMPORT_ERROR = "import_error"
    #: 依赖版本冲突 —— 装的版本和要的不一致。**该有人拍板**：选哪边是口径。
    DEPENDENCY_CONFLICT = "dependency_conflict"
    #: 编译阶段失败 —— 不是语法错，是编译这一步本身过不去。
    COMPILE_ERROR = "compile_error"
    #: 类型不匹配 —— 静态类型检查报出来的。
    TYPE_ERROR = "type_error"
    #: 断言失败 —— 测试跑起来了，但结果不对。
    TEST_ASSERTION = "test_assertion"
    #: 测试自己炸了 —— 收集阶段或夹具出错，**测试压根没跑起来**。
    TEST_SETUP_ERROR = "test_setup_error"
    #: 规范违规 —— lint 报的。
    LINT_VIOLATION = "lint_violation"
    #: 架构违规 —— 依赖方向错了。**改之前要有人认**。
    ARCHITECTURE_VIOLATION = "architecture_violation"
    #: 环境不可用 —— 依赖装不上、执行器起不来。
    ENVIRONMENT_ERROR = "environment_error"
    #: 超时。
    TIMEOUT = "timeout"
    #: 有人取消。
    CANCELLED = "cancelled"
    #: 前提不成立 —— 验收标准与现状冲突。**修不了**。
    SPEC_CONFLICT = "spec_conflict"
    #: 验证引擎自己出错 —— 没得出结论。
    VERIFIER_ERROR = "verifier_error"
    #: 判不出是什么。
    UNKNOWN = "unknown"


#: 细类 → 能不能自己修。
#:
#: **细化了可能比粗类更保守** —— 粗类"必检项没过"是可自动修的，而细化出来的
#: "架构违规""依赖版本冲突"归"需人工"。这是有意的：知道得更具体时，
#: **收窄自动修复的范围比扩大它安全**。
_TYPE_CATEGORY: Final[Mapping[FailureType, FailureCategory]] = {
    FailureType.SYNTAX_ERROR: FailureCategory.AUTO_REPAIRABLE,
    FailureType.IMPORT_ERROR: FailureCategory.AUTO_REPAIRABLE,
    FailureType.DEPENDENCY_CONFLICT: FailureCategory.NEEDS_HUMAN,
    FailureType.COMPILE_ERROR: FailureCategory.AUTO_REPAIRABLE,
    FailureType.TYPE_ERROR: FailureCategory.AUTO_REPAIRABLE,
    FailureType.TEST_ASSERTION: FailureCategory.AUTO_REPAIRABLE,
    FailureType.TEST_SETUP_ERROR: FailureCategory.AUTO_REPAIRABLE,
    FailureType.LINT_VIOLATION: FailureCategory.AUTO_REPAIRABLE,
    FailureType.ARCHITECTURE_VIOLATION: FailureCategory.NEEDS_HUMAN,
    FailureType.ENVIRONMENT_ERROR: FailureCategory.NEEDS_HUMAN,
    FailureType.TIMEOUT: FailureCategory.NEEDS_HUMAN,
    FailureType.CANCELLED: FailureCategory.NEEDS_HUMAN,
    FailureType.SPEC_CONFLICT: FailureCategory.UNREPAIRABLE,
    FailureType.VERIFIER_ERROR: FailureCategory.NEEDS_HUMAN,
}

#: 粗类 → （细类，能不能自己修）。**工具输出没能细化时用这一份。**
#:
#: **"必检项没过"落到"可自动修"**，与阶段一的口径一致：改代码就是了。
#: 但那时的施工说明只能说到"哪几项必检项没过"（见
#: :attr:`ToolFailure.failed_checks`）—— 那是结构化的，也是有用的。
_COARSE: Final[Mapping[FailureKind, tuple[FailureType, FailureCategory]]] = {
    FailureKind.CHECK_FAILED: (FailureType.UNKNOWN, FailureCategory.AUTO_REPAIRABLE),
    FailureKind.EXECUTOR_ERROR: (FailureType.ENVIRONMENT_ERROR, FailureCategory.NEEDS_HUMAN),
    FailureKind.VERIFIER_ERROR: (FailureType.VERIFIER_ERROR, FailureCategory.NEEDS_HUMAN),
    FailureKind.CANCELLED: (FailureType.CANCELLED, FailureCategory.NEEDS_HUMAN),
    FailureKind.TIMED_OUT: (FailureType.TIMEOUT, FailureCategory.NEEDS_HUMAN),
    FailureKind.SPEC_CONFLICT: (FailureType.SPEC_CONFLICT, FailureCategory.UNREPAIRABLE),
    FailureKind.UNKNOWN: (FailureType.UNKNOWN, FailureCategory.NEEDS_HUMAN),
}

#: 归一化诊断码 → 失败类型。**一张表，改一处。**
#:
#: 解析器把工具自己的码归一化到这张表里；表里没有的码走"工具默认类别"
#: （见 :data:`_TOOL_DEFAULTS`）。**新工具接进来只有两条路**：把码归一化
#: 进这张表（更准），或者只接解析器、用工具的默认类别（粗一点）。
_CODE_TO_TYPE: Final[Mapping[str, FailureType]] = {
    "invalid-syntax": FailureType.SYNTAX_ERROR,
    "import-not-found": FailureType.IMPORT_ERROR,
    "import-untyped": FailureType.IMPORT_ERROR,
    "dependency-conflict": FailureType.DEPENDENCY_CONFLICT,
    "compile-error": FailureType.COMPILE_ERROR,
    "type-mismatch": FailureType.TYPE_ERROR,
    "test-failed": FailureType.TEST_ASSERTION,
    "test-error": FailureType.TEST_SETUP_ERROR,
    "lint-violation": FailureType.LINT_VIOLATION,
    "architecture-violation": FailureType.ARCHITECTURE_VIOLATION,
    "environment-error": FailureType.ENVIRONMENT_ERROR,
}

#: 细类的人话标签 —— 理由与施工说明里都要有它。
_TYPE_LABEL: Final[Mapping[FailureType, str]] = {
    FailureType.SYNTAX_ERROR: "语法错",
    FailureType.IMPORT_ERROR: "依赖缺失",
    FailureType.DEPENDENCY_CONFLICT: "依赖版本冲突",
    FailureType.COMPILE_ERROR: "编译错",
    FailureType.TYPE_ERROR: "类型不匹配",
    FailureType.TEST_ASSERTION: "断言失败",
    FailureType.TEST_SETUP_ERROR: "测试自身出错",
    FailureType.LINT_VIOLATION: "规范违规",
    FailureType.ARCHITECTURE_VIOLATION: "架构违规",
    FailureType.ENVIRONMENT_ERROR: "环境不可用",
    FailureType.TIMEOUT: "超时",
    FailureType.CANCELLED: "已取消",
    FailureType.SPEC_CONFLICT: "前提不成立",
    FailureType.VERIFIER_ERROR: "验证引擎出错",
    FailureType.UNKNOWN: "原因不明",
}

#: 多类同时出现时选哪一类当主因。**顺序固定。**
#:
#: **语法错排在最前**：模块根本解析不了的时候，"类型不匹配""规范违规"
#: 全是解析器的连锁反应，报上去只会让人去改本来没错的地方。
_PRIORITY: Final[tuple[FailureType, ...]] = (
    FailureType.SYNTAX_ERROR,
    FailureType.COMPILE_ERROR,
    FailureType.IMPORT_ERROR,
    FailureType.DEPENDENCY_CONFLICT,
    FailureType.TEST_SETUP_ERROR,
    FailureType.TEST_ASSERTION,
    FailureType.TYPE_ERROR,
    FailureType.LINT_VIOLATION,
    FailureType.ARCHITECTURE_VIOLATION,
    FailureType.ENVIRONMENT_ERROR,
    FailureType.TIMEOUT,
    FailureType.CANCELLED,
    FailureType.SPEC_CONFLICT,
    FailureType.VERIFIER_ERROR,
    FailureType.UNKNOWN,
)

#: 施工说明里最多列几处。**超出的要说明超了多少** —— 截断而不说，
#: 读的人会以为就这么多。
_MAX_TARGETS: Final[int] = 5


@dataclass(frozen=True, slots=True)
class ToolFinding:
    """一条结构化发现 —— 位置、诊断码、说明。

    :attr:`code` 是**归一化后**的诊断码（见 :data:`_CODE_TO_TYPE`），
    :attr:`raw_code` 是工具自己的码。**两个都留着**：归一化码给分类用，
    工具自己的码是修的时候要对着查的东西（"F401"比"规范违规"有用）。
    """

    file: str = ""
    line: int | None = None
    column: int | None = None
    #: 归一化诊断码。
    code: str = ""
    #: 工具自己的诊断码。
    raw_code: str = ""
    message: str = ""
    #: 具体的名字：模块名、测试的 nodeid、规则名。
    symbol: str = ""


@dataclass(frozen=True, slots=True)
class ToolOutput:
    """一次工具运行的机器可读输出。

    :attr:`payload` 是工具的原始输出 —— ruff 的 JSON 数组、mypy 的 JSON 行、
    pytest 的 JUnit XML。:attr:`exit_code` 是工具自己的退出码。

    **这两个成对出现。** 退出码说"它觉得自己失败了"，输出说"为什么"——
    分开放，容易只拿一半：只看出退出码，就得去猜原因；只拿输出，就不知道
    那份输出是"检查没过"还是"工具自己崩了"。
    """

    payload: str = ""
    exit_code: int = 0


@dataclass(frozen=True, slots=True)
class ToolFailure:
    """一次失败的机器可读记录。

    :attr:`kind` 是**粗类**（阶段一那份，由判定结果与执行结果推出）；
    :attr:`findings` 是**细料**（工具的机器可读输出）。两者合起来才是
    "哪儿坏了、坏成什么样"。

    :attr:`failed_checks` 是没过的必检项 —— **结构化的**，六层验证给的。
    工具输出认不出来时，至少还知道该修哪一项。

    :attr:`root` 是**工作区根目录** —— 工具报的是绝对路径，而施工说明里
    要的是相对路径：``/tmp/xyz/src/pkg/thing.py:1`` 说"改那个临时目录里的
    东西"，而 ``src/pkg/thing.py:1`` 才说"改哪儿"。
    """

    kind: FailureKind
    tool: str = ""
    exit_code: int = 0
    findings: tuple[ToolFinding, ...] = ()
    failed_checks: tuple[str, ...] = ()
    root: str = ""


@dataclass(frozen=True, slots=True)
class RepairTarget:
    """一处要改的地方 —— 改到哪、改什么。

    :attr:`symbol` 是**具体的那个东西**：模块名、测试的 nodeid、规则名。
    只给文件名等于说"改那边" —— 那不叫定向。
    """

    file: str = ""
    line: int | None = None
    column: int | None = None
    symbol: str = ""
    action: str = ""

    def location(self) -> str:
        """人在话里指得出来的位置。"""
        if self.file and self.line is not None and self.column is not None:
            return f"{self.file}:{self.line}:{self.column}"
        if self.file and self.line is not None:
            return f"{self.file}:{self.line}"
        if self.file:
            return self.file
        return self.symbol or "(位置未知)"


@dataclass(frozen=True, slots=True)
class Classification:
    """一次失败的归类结论。

    **三样东西缺一不可**：是什么错（:attr:`failure_type`）、能不能自己修
    （:attr:`category`）、改哪儿（:attr:`targets`）。
    """

    failure_type: FailureType
    category: FailureCategory
    reason: str
    detail: str = ""
    targets: tuple[RepairTarget, ...] = ()
    evidence: tuple[str, ...] = ()

    def targeted_instruction(self) -> str:
        """给下一轮执行的施工说明 —— **说到行、说到改什么**。

        **不能是空的，也不能是"随便改改"。** 空的指令会让 Agent 拿到一份
        没有着力点的施工说明，而"随便改改"正是无限修复循环的起点。
        """
        if not self.targets:
            return f"修掉{self.reason}"
        label = _TYPE_LABEL[self.failure_type]
        shown = self.targets[:_MAX_TARGETS]
        body = "；".join(f"{target.location()} —— {target.action}" for target in shown)
        hidden = len(self.targets) - len(shown)
        if hidden:
            body += f"；另有 {hidden} 处同类问题"
        return f"修掉 {len(self.targets)} 处{label}：{body}"

    def to_diagnosis(self) -> FailureDiagnosis:
        """翻译成阶段一那份诊断 —— **有界修复认的那一种**。

        翻译过去之后，三重上限、每任务闸门与全局账本**原样接着用**：
        这一层只负责把"哪儿坏了"说清楚，不另立一套修复机制。
        """
        return FailureDiagnosis(
            reason=self.reason,
            category=self.category,
            detail=self.detail,
            repair_hint=self.targeted_instruction(),
            evidence=self.evidence,
        )


def classify(failure: ToolFailure) -> Classification:
    """把一次失败归类。

    **规则只有一条：细化了就按细类的口径，没细化就按粗类的口径。**

    粗类已经能定性的（取消、超时、前提不成立、验证引擎出错）不再看工具
    输出 —— 那些不是工具能说清的事。而"必检项没过"要靠工具输出细化：
    语法错与断言失败改法完全不同，给出同一句"修掉没过的必检项"就是没定位。
    """
    coarse_type, coarse_category = _COARSE[failure.kind]
    if coarse_type is not FailureType.UNKNOWN:
        return _build(coarse_type, coarse_category, failure, targets=())

    refined = _refine(failure)
    if refined is not None:
        failure_type, primary = refined
        return _build(
            failure_type,
            _TYPE_CATEGORY[failure_type],
            failure,
            targets=tuple(_target_of(item, failure_type, failure.root) for item in primary),
        )

    # 细化不出来 —— 至少知道**哪几项**没过（结构化的）。
    if failure.failed_checks:
        targets = tuple(
            RepairTarget(symbol=check, action="让这项必检项通过") for check in failure.failed_checks
        )
        return _build(FailureType.UNKNOWN, coarse_category, failure, targets=targets)
    return _build(FailureType.UNKNOWN, coarse_category, failure, targets=())


def _refine(failure: ToolFailure) -> tuple[FailureType, tuple[ToolFinding, ...]] | None:
    """用工具输出把粗类细化。**细化不出来就返回 None** —— 不猜。"""
    default = _TOOL_DEFAULTS.get(failure.tool)
    normalizer = _TOOL_NORMALIZERS.get(failure.tool)
    grouped: dict[FailureType, list[ToolFinding]] = {}
    for finding in failure.findings:
        code = finding.code
        if not code and normalizer is not None:
            # 手工造的发现可能只填了工具自己的码 —— 按这个工具的规矩归一化。
            code = normalizer(finding.raw_code)
        failure_type = _CODE_TO_TYPE.get(code, default)
        if failure_type is None:
            continue
        grouped.setdefault(failure_type, []).append(finding)
    if not grouped:
        return None
    chosen = min(grouped, key=_PRIORITY.index)
    return chosen, tuple(grouped[chosen])


def _build(
    failure_type: FailureType,
    category: FailureCategory,
    failure: ToolFailure,
    *,
    targets: tuple[RepairTarget, ...],
) -> Classification:
    """把归类结果拼成一份结论。"""
    label = _TYPE_LABEL[failure_type]
    if targets:
        reason = f"{label}：{targets[0].location()}"
        if len(targets) > 1:
            reason = f"{label}（{len(targets)} 处）"
    elif failure.failed_checks:
        reason = f"{label}：{'、'.join(failure.failed_checks)}"
    else:
        reason = label
    return Classification(
        failure_type=failure_type,
        category=category,
        reason=reason,
        detail=failure.tool,
        targets=targets,
        evidence=_evidence_of(failure),
    )


def _target_of(finding: ToolFinding, failure_type: FailureType, root: str) -> RepairTarget:
    """一条发现 → 一处要改的地方。"""
    return RepairTarget(
        file=_relative(finding.file, root),
        line=finding.line,
        column=finding.column,
        symbol=finding.symbol,
        action=_action_of(finding, failure_type),
    )


def _action_of(finding: ToolFinding, failure_type: FailureType) -> str:
    """这一处要做什么 —— **说的是"改什么"，不是"有问题"。**

    **工具自己的诊断码要带上**（``F401`` 比"规范违规"有用 —— 前者能直接
    查到规则，后者只能重跑一遍才知道）。归一化码与工具的码相同时就不重复
    写一遍了。
    """
    label = _TYPE_LABEL[failure_type]
    head = label
    if finding.raw_code and finding.raw_code != finding.code:
        head = f"{label}（{finding.raw_code}）"
    tail = " ".join(part for part in (finding.symbol, finding.message) if part)
    return f"{head}：{tail}" if tail else head


def _evidence_of(failure: ToolFailure) -> tuple[str, ...]:
    """证据 —— **机器可读的标识**，不是一段话。"""
    items: list[str] = []
    for finding in failure.findings:
        location = _location_of(finding, failure.root)
        parts = [part for part in (finding.symbol, location, finding.raw_code) if part]
        items.append(" ".join(parts))
    return tuple(items)


def _location_of(finding: ToolFinding, root: str) -> str:
    path = _relative(finding.file, root)
    if path and finding.line is not None:
        return f"{path}:{finding.line}"
    return path


def _relative(path: str, root: str) -> str:
    """路径要相对于工作区。

    **绝对路径塞进施工说明等于噪声** —— 那一长串临时目录前缀不告诉人
    该改哪儿，只让人以为路径变了。
    """
    if not root or not path.startswith(root):
        return path
    trimmed = path[len(root) :].lstrip("/\\")
    return trimmed or path


# --------------------------------------------------------------------------
# 工具输出 → 结构化发现
# --------------------------------------------------------------------------


#: 工具 → 认不出来的诊断码算哪一类。
#:
#: **这不是猜。** ruff 报的都是规范、mypy 报的都是类型、pytest 报的都是
#: 断言 —— 工具的职责就是这个。**工具不在表里就不给默认类别**，
#: 那时候认不出来的发现直接丢掉，而不是被塞进"类型不匹配"。
_TOOL_DEFAULTS: Final[Mapping[str, FailureType]] = {
    "ruff": FailureType.LINT_VIOLATION,
    "mypy": FailureType.TYPE_ERROR,
    "pytest": FailureType.TEST_ASSERTION,
}

#: mypy 的诊断码 → 归一化码。
_MYPY_CODES: Final[Mapping[str, str]] = {
    "import": "import-not-found",
    "import-not-found": "import-not-found",
    "import-untyped": "import-untyped",
    "assignment": "type-mismatch",
    "arg-type": "type-mismatch",
    "attr-defined": "type-mismatch",
    "call-arg": "type-mismatch",
    "dict-item": "type-mismatch",
    "has-type": "type-mismatch",
    "index": "type-mismatch",
    "list-item": "type-mismatch",
    "operator": "type-mismatch",
    "override": "type-mismatch",
    "return-value": "type-mismatch",
    "typeddict-item": "type-mismatch",
    "union-attr": "type-mismatch",
    "valid-type": "type-mismatch",
}

#: ruff 的诊断码 → 归一化码。
#:
#: **ruff 报的基本都是规范违规**，只有一个例外：语法错。
#: （ruff 早期用 ``E999``，后来改成 ``invalid-syntax`` —— 两个都认。）
_RUFF_SYNTAX_CODES: Final[frozenset[str]] = frozenset({"E999", "invalid-syntax"})


def parse_ruff_json(payload: str) -> tuple[ToolFinding, ...]:
    """ruff ``--output-format=json`` 的输出。"""
    return _parse_diagnostic_list(_load_json(payload), _normalize_ruff)


def parse_mypy_json(payload: str) -> tuple[ToolFinding, ...]:
    """mypy ``--output=json`` 的输出 —— **一行一个 JSON 对象**。"""
    findings: list[ToolFinding] = []
    for raw_line in payload.splitlines():
        text = raw_line.strip()
        if not text:
            continue
        findings.extend(_parse_diagnostic_list(_load_json(text), _normalize_mypy))
    return tuple(findings)


def parse_junit_xml(payload: str) -> tuple[ToolFinding, ...]:
    """pytest ``--junitxml`` 的输出。

    **断言失败与测试自身出错是两回事。** JUnit XML 里前者是 ``<failure>``、
    后者是 ``<error>``：前者是"跑起来了但结果不对"，后者是"压根没跑起来"
    —— 后者去改断言是改错地方。
    """
    try:
        root = ElementTree.fromstring(payload)
    except ElementTree.ParseError:
        return ()
    findings: list[ToolFinding] = []
    for case in root.iter("testcase"):
        failure = case.find("failure")
        error = case.find("error")
        node = failure if failure is not None else error
        if node is None:
            continue
        code = "test-failed" if failure is not None else "test-error"
        classname = _as_text(case.get("classname"))
        name = _as_text(case.get("name"))
        findings.append(
            ToolFinding(
                file=_as_text(case.get("file")) or _module_path(classname),
                line=_as_int(case.get("line")),
                code=code,
                raw_code=code,
                message=_first_line(_as_text(node.get("message"))),
                symbol=f"{classname}::{name}" if classname else name,
            )
        )
    return tuple(findings)


def _first_line(text: str) -> str:
    """只留第一行。

    **pytest 的 ``message`` 属性装的是整份报告** —— 一个夹具错误的消息里
    连"可用夹具列表"都在里面。整段塞进施工说明，那条说明就没法读了：
    真正有用的是"坏在哪"那一行。
    """
    stripped = text.strip()
    if not stripped:
        return ""
    return stripped.splitlines()[0].strip()


def _module_path(classname: str) -> str:
    """pytest 的 ``classname`` 是**模块路径**（``tests.test_thing``）——
    转成文件路径（``tests/test_thing.py``）。

    **pytest 9 的 JUnit XML 不带 ``file`` 属性** —— 位置得自己推出来。
    """
    if not classname:
        return ""
    return f"{classname.replace('.', '/')}.py"


def parse_structured_diagnostics(payload: str) -> tuple[ToolFinding, ...]:
    """通用形态：``[{"file":…, "line":…, "column":…, "code":…, "message":…}]``。

    **给不写专有解析器的工具用** —— 很多检查器都长这个样。诊断码按
    :data:`_CODE_TO_TYPE` 认；认不出来的走工具的默认类别（表里没有就没有）。
    """
    return _parse_diagnostic_list(_load_json(payload), lambda code: code)


def parse_python_syntax_error(source: str, *, filename: str = "<unknown>") -> ToolFinding | None:
    """Python 自己说的语法错 —— **结构化的**：行、列、原文都在。

    **编译阶段失败与语法错不是一回事。** 源码里有 null 字节这类问题时，
    :func:`compile` 抛的是 :class:`ValueError` 而不是 :class:`SyntaxError`
    —— 那是"编译这一步过不去"，归 :attr:`FailureType.COMPILE_ERROR`。
    """
    try:
        compile(source, filename, "exec")
    except SyntaxError as error:
        return ToolFinding(
            file=filename,
            line=error.lineno,
            column=error.offset,
            code="invalid-syntax",
            raw_code="SyntaxError",
            message=error.msg or "",
            symbol=(error.text or "").strip(),
        )
    except ValueError as error:
        return ToolFinding(
            file=filename,
            code="compile-error",
            raw_code="ValueError",
            message=str(error),
        )
    return None


def parse_tool_output(tool: str, payload: str) -> tuple[ToolFinding, ...]:
    """按工具名选解析器。**工具不认识就什么也不给** —— 不硬套。"""
    parser = _TOOL_PARSERS.get(tool)
    if parser is None:
        return ()
    return parser(payload)


def tool_failure(
    *,
    kind: FailureKind,
    tool: str,
    output: ToolOutput | None = None,
    failed_checks: tuple[str, ...] = (),
    root: str = "",
) -> ToolFailure:
    """把工具的机器可读输出装成一条失败记录。

    **这是编排层拿到失败时的入口。** 工具名选解析器，粗类由判定结果定，
    两样都是结构化的 —— 没有一处需要读执行器写的措辞。

    :param output: 工具的输出与退出码。没给就当"没有输出、退出码 0"。
    :param root: 工作区根目录 —— 工具报的绝对路径会被换成相对路径。
    """
    result = output or ToolOutput()
    return ToolFailure(
        kind=kind,
        tool=tool,
        exit_code=result.exit_code,
        findings=parse_tool_output(tool, result.payload),
        failed_checks=failed_checks,
        root=root,
    )


# --------------------------------------------------------------------------
# 内部小工具
# --------------------------------------------------------------------------


#: 工具名 → 解析器。
_TOOL_PARSERS: Final[Mapping[str, Callable[[str], tuple[ToolFinding, ...]]]] = {
    "ruff": parse_ruff_json,
    "mypy": parse_mypy_json,
    "pytest": parse_junit_xml,
}


def _load_json(payload: str) -> object:
    """读一段 JSON。**读不出来就是读不出来** —— 返回 ``None``，不报错。"""
    try:
        return cast(object, json.loads(payload))
    except ValueError:
        return None


def _parse_diagnostic_list(
    raw: object,
    normalize: Callable[[str], str],
) -> tuple[ToolFinding, ...]:
    """读"诊断对象"这种形态 —— **一个对象或一串对象都认**。

    mypy 的 JSON 输出是**一行一个对象**，ruff 的是一个数组 —— 两种都得认。
    """
    items: list[object] = []
    if isinstance(raw, list):
        items = list(raw)
    elif isinstance(raw, dict):
        items = [raw]
    else:
        return ()
    findings: list[ToolFinding] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        location = item.get("location")
        row: int | None = None
        column: int | None = None
        if isinstance(location, dict):
            row = _as_int(location.get("row"))
            column = _as_int(location.get("column"))
        else:
            row = _as_int(item.get("line"))
            column = _as_int(item.get("column"))
        raw_code = _as_text(item.get("code"))
        findings.append(
            ToolFinding(
                file=_as_text(item.get("filename")) or _as_text(item.get("file")),
                line=row,
                column=column,
                code=normalize(raw_code),
                raw_code=raw_code,
                message=_as_text(item.get("message")),
                symbol=_as_text(item.get("symbol")),
            )
        )
    return tuple(findings)


def _normalize_ruff(code: str) -> str:
    """ruff 的诊断码 → 归一化码。"""
    if code in _RUFF_SYNTAX_CODES:
        return "invalid-syntax"
    return "lint-violation"


def _normalize_mypy(code: str) -> str:
    """mypy 的诊断码 → 归一化码。**认不出来的码原样留着**，
    分类时走工具的默认类别（类型不匹配）。"""
    return _MYPY_CODES.get(code, code)


#: 工具 → 诊断码归一化。**没归一化过的码按原样认**。
_TOOL_NORMALIZERS: Final[Mapping[str, Callable[[str], str]]] = {
    "ruff": _normalize_ruff,
    "mypy": _normalize_mypy,
    "pytest": lambda code: code,
}


def _as_text(value: object) -> str:
    """取出字符串。**不是字符串就是没有** —— 不转。"""
    return value if isinstance(value, str) else ""


def _as_int(value: object) -> int | None:
    """取出整数。JSON 里的数字与 XML 属性里的字符串都认。"""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        try:
            return int(value)
        except ValueError:
            return None
    return None


def build_targeted_repair_task(
    *,
    task_id: str,
    attempt: int,
    classification: Classification,
) -> RepairTask:
    """把归类结果变成一条**定向**修复任务。

    **不能自动修的不生成任务。** 生成一条"随便改改"的施工说明等于把无限
    修复循环的起点交出去 —— 那一类失败该挂起等人，不该排任务。
    """
    if classification.category is not FailureCategory.AUTO_REPAIRABLE:
        label = _TYPE_LABEL[classification.failure_type]
        raise ValueError(f"{label} 不能自动修 —— 不生成修复任务")
    return build_repair_task(
        task_id=task_id,
        attempt=attempt,
        diagnosis=classification.to_diagnosis(),
    )


__all__ = [
    "Classification",
    "FailureType",
    "RepairTarget",
    "ToolFailure",
    "ToolFinding",
    "build_targeted_repair_task",
    "classify",
    "parse_junit_xml",
    "parse_mypy_json",
    "parse_python_syntax_error",
    "parse_ruff_json",
    "parse_structured_diagnostics",
    "parse_tool_output",
    "tool_failure",
]
