"""Docker 相关的小工具。

**为什么需要区分"构建失败"和"拉不到基础镜像"**：

镜像构建失败有两种完全不同的原因 ——
    1. Dockerfile 写错了（代码缺陷，必须报出来）
    2. 公共镜像仓库不可达（环境限制，只能跳过）

把两者混为一谈会有两个后果：断网的机器上跑测试会报一堆红，
看着像代码坏了；而真正写错 Dockerfile 时，如果分类逻辑写松了，
就会静悄悄地"跳过"。

所以这里用**特征串**来分类，而不是"构建失败就跳过"。
特征串全部来自 Docker 在网络不可达时的实际报错 ——
换句话说，它们描述的是"拉不下来"，不是"构建步骤失败"。
"""

from __future__ import annotations

#: 命中任意一条就判定为"镜像仓库不可达"。
#:
#: 这些串都出自 Docker 解析/拉取基础镜像 manifest 时的报错，
#: 而不是出自 Dockerfile 的任何一条指令。
#:
#: 刻意保持**窄**：像 "EOF"、"dial tcp" 这种泛泛的串也会出现在
#: 真实的构建失败里（比如某个 RUN 脚本崩了），把它们算进来就会
#: 把代码缺陷悄悄降级成"跳过"。
REGISTRY_UNREACHABLE_MARKERS = (
    "failed to resolve source metadata",
    "failed to fetch anonymous token",
    "failed to authorize",
    "failed to do request",
    "connection reset by peer",
    "TLS handshake timeout",
    "no such host",
    "network is unreachable",
)


def is_registry_unreachable(output: str) -> bool:
    """构建输出是否表明"镜像仓库不可达"。

    判定为不可达时，调用方应该**跳过**测试而不是报错 ——
    环境没网不是代码的问题。但反过来，一张写错的 Dockerfile
    必须在同一个环境里**报错**，否则离线就永远发现不了。
    """
    return any(marker in output for marker in REGISTRY_UNREACHABLE_MARKERS)


__all__ = ["REGISTRY_UNREACHABLE_MARKERS", "is_registry_unreachable"]
