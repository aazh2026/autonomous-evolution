"""把运行配置接到角色化 Prompt 上。

设计依据：docs/tasks.md TASK-P1-057（"模型与温度可外部配置"）。

**这个桥为什么在编排层。** 两头都不肯放：

- 放 ``agents/`` —— ``agents/`` 就要 import ``infrastructure/``，而
  ``agents/`` 是角色层，它不关心配置是从环境变量来的还是从文件来的；
- 放 ``infrastructure/`` —— ``infrastructure/`` 就要 import ``agents/``，
  那是反过来的依赖：基础设施层不该知道"角色化 Prompt"这种上层的东西。

编排层是**把东西接起来**的那一层，桥就在这儿。

**为什么配置只落在"三个数"上。** ``Settings`` 里那三栏是纯基本类型
（模型名、温度、token 上限），而 :class:`ModelSettings` 也是 —— 两边都不
import 对方，桥就只是一次赋值。哪天配置那边多出一栏（比如按角色分别配
温度），改的也只是这个文件。

**换模型不动 Prompt。** :meth:`with_configured_model` 返回**新的一套**
Prompt，正文一个字都不改 —— 于是"同一套 Prompt 在两个模型上跑出的结果
不一样"这件事是可比的，而不是在两次调用之间被别处的改动悄悄改掉了。
"""

from __future__ import annotations

from agents.prompts.base import ModelSettings, PromptSet, RoleOutput
from infrastructure.config.settings import Settings


def model_settings_from(settings: Settings) -> ModelSettings:
    """从运行配置里取出模型设置。"""
    return ModelSettings(
        model=settings.model_name,
        temperature=settings.model_temperature,
        max_tokens=settings.model_max_tokens,
    )


def with_configured_model[OutputT: RoleOutput](
    prompt_set: PromptSet[OutputT], settings: Settings
) -> PromptSet[OutputT]:
    """把运行配置里的模型设置套到这套 Prompt 上，返回**新**的一套。"""
    return prompt_set.with_model(model_settings_from(settings))


__all__ = [
    "model_settings_from",
    "with_configured_model",
]
