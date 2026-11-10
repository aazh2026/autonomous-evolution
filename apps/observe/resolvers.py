"""证据核验 —— "这个引用指的东西，真的在这儿吗"。

设计依据：docs/tasks.md TASK-P3-013（验收条件"候选的 evidence 指向真实可解
析的证据"；R-029 证据必须可独立核验）；docs/design.md §3.3。

**这一层是候选的关口。** 发现层拿着每一条证据引用来问：指向的文件在不在
仓库里、指向的问题单在不在台账里。**问完才决定这条候选算不算数。**

**三态，不是两态。** 找到了、找过了不在、查不了 —— **"查不了"与"不在"是
两件事**：

- 一个指向仓库里文件的引用找不到 → **证据指错了** → 候选丢掉；
- 一个指向对象存储里产物的引用查不了 → **本机没有产物存储** → **不是候选
  有问题，是这儿看不见**。

**这一条流水线只放"当场核验过"的候选过去。** 所以"查不了"也丢 —— 但丢的
理由写得清清楚楚：**证明不了它真实，就不能说它真实。** 要接产物存储，就在
这里加一个能查产物的核验器（P1-048 的归档器知道产物在哪）。

**路径不许跑出仓库。** 一个写着 ``../../etc/passwd`` 的证据引用不该被当成
"真的在这儿" —— 它在别处，而"在别处"跟"在这儿"不是一回事。
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from apps.readers import read_ledger
from core.evolution.evidence import Evidence, EvidenceKind
from core.evolution.opportunity import Resolution, ResolutionStatus


@dataclass
class RepositoryEvidenceResolver:
    """仓库里的证据核验器。

    :param root: 仓库根目录。**文件引用按它来解析**（仓库相对路径）。
    :param issue_ledger: 问题单台账在仓库里的相对路径。**不给就没法核验问
        题单** —— 那时候问题单引用一律"查不了"，不会说"不在"。

    **它只读，不写。**
    """

    root: Path
    issue_ledger: Path | None = None

    def resolve(self, evidence: Evidence) -> Resolution:
        """核验一条证据引用。"""
        match evidence.kind:
            case EvidenceKind.FILE:
                return self._resolve_file(evidence.ref)
            case EvidenceKind.ISSUE:
                return self._resolve_issue(evidence.identifier)
            case EvidenceKind.ARTIFACT | EvidenceKind.TEST_REPORT:
                return Resolution(
                    status=ResolutionStatus.UNAVAILABLE,
                    detail="本机没有产物存储，没法当场核验",
                )

    def _resolve_file(self, ref: str) -> Resolution:
        """核验一个文件引用。"""
        candidate = Path(ref)
        if candidate.is_absolute():
            path = candidate
        else:
            path = self.root / candidate
            if not _inside(self.root, path):
                return Resolution(
                    status=ResolutionStatus.MISSING,
                    detail=f"{ref} 指着仓库外面的地方",
                )
        if path.exists():
            return Resolution(status=ResolutionStatus.RESOLVED, detail=str(path))
        return Resolution(status=ResolutionStatus.MISSING, detail=f"{ref} 不在仓库里")

    def _resolve_issue(self, identifier: str) -> Resolution:
        """核验一个问题单引用。"""
        if self.issue_ledger is None:
            return Resolution(
                status=ResolutionStatus.UNAVAILABLE,
                detail="本机没有接问题单台账，没法核验",
            )
        ledger = self.root / self.issue_ledger
        if not ledger.exists():
            return Resolution(
                status=ResolutionStatus.UNAVAILABLE,
                detail=f"问题单台账 {self.issue_ledger.as_posix()} 不在本机",
            )
        for entry in read_ledger(ledger):
            if entry.id == identifier:
                return Resolution(
                    status=ResolutionStatus.RESOLVED,
                    detail=f"{identifier} 在 {self.issue_ledger.as_posix()} 里",
                )
        return Resolution(
            status=ResolutionStatus.MISSING,
            detail=f"{identifier} 不在 {self.issue_ledger.as_posix()} 里",
        )


def _inside(root: Path, path: Path) -> bool:
    """这个路径在根目录底下吗。

    **用解析后的绝对路径比。** 不解析的话，``root/../..`` 这种写法能绕过去
    —— 而那正是要挡的。
    """
    try:
        resolved = path.resolve()
        base = root.resolve()
    except OSError:
        return False
    return resolved == base or resolved.is_relative_to(base)


__all__ = ["RepositoryEvidenceResolver"]
