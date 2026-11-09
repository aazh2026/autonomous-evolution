"""角色化 Prompt 的公共骨架 —— 六套 Prompt 共用的那一份。

设计依据：docs/design.md §3.2（四个逻辑角色 + Director）、§5（"每一阶段都应该
有结构化输出，并且允许下游独立验证"）；docs/tasks.md TASK-P1-057（六套 Prompt；
输出绑定对应 Pydantic schema；证据引用要求；模型与温度可外部配置）。

**这一层只放六套 Prompt 共用的那一份。** 角色各自的正文与输出形状在各自的
模块里；这里放三件事：输出长什么样、结论有没有依据、怎么把模型的原始输出
变成结构化对象。

**为什么"有没有依据"是算出来的，不是模型填的。**
一条结论有没有依据，看的是它**带了几条证据引用** —— 这是能数出来的事实。
如果让模型自己填 ``status``，那么"这条结论一条证据都没有、模型却说它有依据"
就成了一种可能，而那正是这套系统要消灭的东西：一份**看起来有依据**的结论比
一份明说"没依据"的结论危险得多 —— 后者会被下游排除，前者会被当成事实用。

所以 :attr:`Conclusion.status` 是**属性**不是字段：它由 ``evidence`` 算出来，
模型填不了，也填不掉。

**为什么模型多说的字段要被丢掉，而不是让整份输出作废。**
``status`` 那一栏在契约里是**系统说了算**的（见 :func:`parse_output`）。模型
要是多说一句 ``"status": "supported"``，那是它没读输出契约 —— 但那份输出里
其余部分可能全是对的。为一句话毙掉一次调用（一次调用的钱、一次跑偏的分析）
太贵，所以这一句**收下但不采用**。

真正毙掉输出的是**契约本身**：``extra="forbid"`` 意味着 schema 之外的字段
会让解析失败。字段名对不上是"模型没读契约"的硬证据 —— 那就该当场停下。
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace
from enum import StrEnum
from typing import Final, Protocol

from pydantic import BaseModel, ConfigDict, Field, field_validator

from core.evolution.evidence import Evidence


class Role(StrEnum):
    """六个逻辑角色。

    design.md §3.2 把 Director 拆成四个逻辑角色（Director 本身、Analyst、
    Strategist、Architect、Quality），再加上 §5 建议的独立 Critic —— 一共六个。

    **它们是六种"看法"，不是六个进程。** design.md 明说"四个角色不一定是四个
    常驻 Agent……建议初期作为四种独立的决策任务，由同一个 Director 调用不同
    模型或 Prompt 完成"。所以这里有的是**六套 Prompt 与六种输出形状**，
    不是六个长期驻留的角色对象。
    """

    #: 分析师 —— 理解项目现状，识别差距、缺陷与机会。
    ANALYST = "analyst"
    #: 产品策略师 —— 判断产品价值、需求优先级与功能演进方向。
    STRATEGIST = "strategist"
    #: 首席架构师 —— 判断架构质量、技术债、技术选型与重构必要性。
    ARCHITECT = "architect"
    #: 质量策略师 —— 评估质量、可靠性、安全性与测试充分性。
    QUALITY = "quality"
    #: 项目总监 —— 最终演进决策，综合目标、风险、成本与收益。
    DIRECTOR = "director"
    #: 独立 Critic —— 九项检查，不给分数，给一份可复核的报告。
    CRITIC = "critic"


class ConclusionStatus(StrEnum):
    """一条结论有没有依据。

    两态而不是三态：**"没依据"与"有依据"就够用了。**
    "还没查"与"查了没有"是两件事 —— 那件事由 Critic 的 :class:`Verdict`
    三态（通过 / 不通过 / 未核实）管，不该在这里再开一格。
    """

    #: 带了至少一条证据引用。
    SUPPORTED = "supported"
    #: 一条证据引用都没有。
    UNSUPPORTED = "unsupported"


def conclusion_status(evidence: Sequence[object]) -> ConclusionStatus:
    """有依据 → ``supported``；没有 → ``unsupported``。

    **判据只有一条：有没有证据引用。** 不看引用指向什么、不看有几条 ——
    "至少一条"是那条引用能被独立核验的最低要求（能不能核验是取证据那一端的
    事，不是这一端的判断）。
    """
    return ConclusionStatus.SUPPORTED if evidence else ConclusionStatus.UNSUPPORTED


class HasStatus(Protocol):
    """带 ``status`` 的东西 —— 结论与它的同类。

    **为什么用协议而不是基类。** Critic 的结论用的证据词表与其它角色不同
    （它要引用非目标、约束、停止条件 —— 那些在 ``core`` 的证据词表里
    压根没有），所以它不继承 :class:`Conclusion`。但"有没有依据"这件事对
    两者是一样的 —— 有协议就够了，不必为了复用两行判断去造一个基类。
    """

    @property
    def status(self) -> ConclusionStatus: ...


def supported[StatusT: HasStatus](conclusions: Sequence[StatusT]) -> tuple[StatusT, ...]:
    """只留有依据的那些。"""
    return tuple(item for item in conclusions if item.status is ConclusionStatus.SUPPORTED)


def unsupported[StatusT: HasStatus](conclusions: Sequence[StatusT]) -> tuple[StatusT, ...]:
    """只留没依据的那些。

    **它们不是被丢掉了，是被标出来了。** 下游排除的是**它们的作用**
    （不派生任务、不否决候选），不是它们的存在 —— "这里有一条拿不出证据的
    结论"本身就是下游要知道的事。
    """
    return tuple(item for item in conclusions if item.status is ConclusionStatus.UNSUPPORTED)


class Conclusion(BaseModel):
    """一条结论 —— 角色说的一句话，加上凭什么这么说。

    **它有两半，缺一不可**：``statement`` 是主张，``evidence`` 是依据。
    依据**可以**为空 —— 但那时这条结论会被标成 ``unsupported``，在下游被
    排除。允许它为空，是因为"这里缺证据"是一条值得下游知道的结论；不让它
    为空，是因为下游不该拿到一条既没依据又看不出没依据的结论。
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    #: 这条结论的标识。下游靠它引用（机会回应哪些差距、决策针对哪个候选）。
    id: str = Field(min_length=1)
    #: 主张本身。一句话。
    statement: str = Field(min_length=1)
    #: 支撑这条结论的证据引用。**空了就意味着"没有依据"。**
    evidence: tuple[Evidence, ...] = ()

    @field_validator("id", "statement")
    @classmethod
    def _not_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("结论的 id 与主张都不能是空白")
        return value.strip()

    @property
    def status(self) -> ConclusionStatus:
        """这条结论有没有依据。**算出来的，模型填不了。**"""
        return conclusion_status(self.evidence)


class RoleOutput(BaseModel):
    """角色输出的公共形状。

    **每个子类都必须能翻译成领域层的对象。** 输出格式本身不是目的 ——
    目的是让下游拿到一份**能独立核验**的结构化结论。翻译方法（``to_*``）
    就是"能独立核验"的那一半：它把角色的话变成领域模型认的东西，并且在转换
    时**丢掉没有依据的那些**。
    """

    model_config = ConfigDict(frozen=True, extra="forbid")


class ModelSettings(BaseModel):
    """用哪个模型、什么温度。

    **这一份是可以被外部覆盖的。** 模型名与温度不该写死在 Prompt 里 ——
    换模型是运维的事，不是改代码的事（见
    :mod:`infrastructure.config.settings` 里的 ``model_name`` /
    ``model_temperature`` / ``model_max_tokens``）。

    **默认温度低。** 这套系统的输出要能被下游独立核验，而"发散"正是核验的
    对立面 —— 一份每次跑出来不一样的结论，没法被拿去跟别的结论比。
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    #: 模型名。
    model: str = Field(default="claude-sonnet-4-6", min_length=1)
    #: 采样温度，0~2。
    temperature: float = Field(default=0.2, ge=0.0, le=2.0)
    #: 单次输出的 token 上限。
    max_tokens: int = Field(default=4096, ge=1)


class PromptRequest(BaseModel):
    """一次模型调用 —— 系统提示词、用户提示词与输出格式。

    **输出格式是请求的一部分，不是调用方的额外负担。** 角色与它的 schema 是
    一套东西的两面：换了角色就换了输出形状，而"换了角色忘了换 schema"这种
    事在这份请求里根本构造不出来。
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    #: 这次调用属于哪个角色。
    role: Role
    #: 系统提示词正文。
    system_prompt: str
    #: 这一次的用户提示词（角色要读的那份材料）。
    user_prompt: str
    #: 用哪个模型。
    model: str
    #: 采样温度。
    temperature: float
    #: 输出 token 上限。
    max_tokens: int
    #: 输出必须符合的 JSON Schema。
    output_schema: dict[str, object]


class PromptClient(Protocol):
    """调用模型的那一头。

    **为什么是协议。** 具体调哪家（Anthropic SDK、自建的推理网关，还是测试
    里的假客户端）是**接线**的事，不是 Prompt 集的事 —— Prompt 集只管
    "要什么、拿到什么形状的答案"。

    **返回原始文本，不返回结构化对象。** 解析由 :func:`parse_output` 做 ——
    解析失败要能报出"这份输出不合格"而不是"调用失败"，这两件事的补救动作
    完全不同（前者是改 Prompt，后者是查网络）。
    """

    async def complete(self, request: PromptRequest) -> str:
        """按请求去问模型，返回模型的原始文本。"""
        ...


@dataclass(frozen=True)
class PromptSet[OutputT: RoleOutput]:
    """一套 Prompt —— 一个角色的职责、正文与输出格式。

    **职责边界与正文是同一份东西的两面。** ``purpose`` 与 ``boundary`` 说
    "这个角色负责什么、不负责什么"，``system_prompt`` 把那两句话**写进**
    模型看得见的地方。只写在文档里不行 —— 没人看文档，但模型每次都看正文。

    **换模型不改 Prompt。** :meth:`with_model` 返回**新的一套**，原封不动
    —— 于是"同一套 Prompt 在两个模型上跑出的结果不一样"这件事是可比的，
    而不是在两次调用之间被别处的改动悄悄改掉了。

    **``OutputT`` 是协变的。** 于是 ``PromptSet[AnalystOutput]`` 可以当
    ``PromptSet[RoleOutput]`` 用 —— 六套 Prompt 要能放进同一份清单
    （``PROMPT_SETS``），而它们的输出类型各不相同。协变由类型检查器按
    ``OutputT`` 在类体里的出现位置**推断**（PEP 695）：它只出现在
    ``type[OutputT]`` 与返回值 ``PromptSet[OutputT]`` 里，两处都是协变位置。
    """

    #: 这个 Prompt 属于哪个角色。
    role: Role
    #: 这个角色负责什么。一句话。
    purpose: str
    #: 这个角色**不**负责什么。一句话，职责边界。
    boundary: str
    #: 系统提示词正文。
    system_prompt: str
    #: 输出必须符合的模式。
    output_schema: type[OutputT]
    #: 用哪个模型、什么温度。
    model: ModelSettings = field(default_factory=ModelSettings)

    def with_model(self, model: ModelSettings) -> PromptSet[OutputT]:
        """换一套模型配置，返回**新**的一套。"""
        return replace(self, model=model)

    def request(self, user_prompt: str) -> PromptRequest:
        """把这次调用拼成请求。"""
        return PromptRequest(
            role=self.role,
            system_prompt=self.system_prompt,
            user_prompt=user_prompt,
            model=self.model.model,
            temperature=self.model.temperature,
            max_tokens=self.model.max_tokens,
            output_schema=self.output_schema.model_json_schema(),
        )


class PromptParseError(RuntimeError):
    """模型的原始输出没法解析成角色输出。

    **这不是"调用失败"。** 调用成功了，模型也答了 —— 只是答得不合格。
    两者的补救动作完全不同：调用失败要查网络与额度，解析失败要改 Prompt
    或换模型。混成一种错误，读日志的人就会去查错的方向。
    """


#: **由系统说了算**的字段名。
#:
#: ``status`` 那一栏在契约里是系统算出来的（见 :attr:`Conclusion.status`），
#: 模型说了不算。收下它是为了不因为模型多说一句就把整份输出毙掉。
_SYSTEM_OWNED_FIELDS: Final[frozenset[str]] = frozenset({"status"})

#: 一个完整的围栏最少几行 —— 开围栏那行、内容、闭围栏那行。
_FENCE_MIN_LINES: Final[int] = 3


def _strip_system_fields(payload: object) -> object:
    """把系统说了算的字段从原始 JSON 里去掉（递归）。"""
    if isinstance(payload, Mapping):
        return {
            key: _strip_system_fields(value)
            for key, value in payload.items()
            if key not in _SYSTEM_OWNED_FIELDS
        }
    if isinstance(payload, list):
        return [_strip_system_fields(item) for item in payload]
    return payload


def _strip_fence(raw: str) -> str:
    """去掉 ```` ```json ```` 围栏。

    **模型爱加围栏，加围栏不是错误。** 报错的话，一次格式偏好就毁掉一次
    调用 —— 而围栏里装的还是 JSON，那就按 JSON 读。

    只脱**围栏**，不脱围栏里的散文：围栏里不是 JSON 时照样报错 ——
    "从散文里抠 JSON"那条路只会把一份不合格的输出变成一份**看起来合格**的
    输出。
    """
    text = raw.strip()
    if not text.startswith("```"):
        return text
    lines = text.splitlines()
    if len(lines) >= _FENCE_MIN_LINES and lines[-1].strip() == "```":
        return "\n".join(lines[1:-1]).strip()
    return text


def parse_output[OutputT: RoleOutput](prompt_set: PromptSet[OutputT], raw: str) -> OutputT:
    """把模型的原始输出解析成角色输出。

    **三步，缺一不可。**

    1. 去掉围栏；
    2. 解析 JSON —— 不是 JSON 就报错；
    3. 按**这个角色自己的** schema 校验 —— 六套 Prompt 的输出形状不同，
       互相套是套不上的（ Analyst 的 ``gaps`` 塞进 Director 的 schema 会
       直接被 ``extra="forbid"`` 挡下来）。
    """
    text = _strip_fence(raw)
    try:
        payload = json.loads(text)
    except json.JSONDecodeError as error:
        raise PromptParseError(f"{prompt_set.role.value} 的输出不是 JSON：{error}") from error
    return prompt_set.output_schema.model_validate(_strip_system_fields(payload))


__all__ = [
    "Conclusion",
    "ConclusionStatus",
    "HasStatus",
    "ModelSettings",
    "PromptClient",
    "PromptParseError",
    "PromptRequest",
    "PromptSet",
    "Role",
    "RoleOutput",
    "conclusion_status",
    "parse_output",
    "supported",
    "unsupported",
]
