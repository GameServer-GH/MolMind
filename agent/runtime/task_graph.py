"""Bounded task-graph primitives used by the Agent runtime.

The graph is an execution/audit model only.  It may order tools and
conversation workers, but it never owns scientific ranking state.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterable


TERMINAL_TASK_STATUSES = {
    "succeeded",
    "failed",
    "denied",
    "cancelled",
    "skipped",
    "not_executed",
}
SUCCESS_TASK_STATUSES = {"succeeded", "skipped"}


@dataclass
class TaskSpec:
    task_id: str
    kind: str
    label: str = ""
    tool_id: str = ""
    args: dict[str, Any] = field(default_factory=dict)
    depends_on: tuple[str, ...] = ()
    status: str = "pending"
    observation: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "kind": self.kind,
            "label": self.label,
            "tool": self.tool_id,
            "args": dict(self.args),
            "depends_on": list(self.depends_on),
            "status": self.status,
            "observation": dict(self.observation),
        }


@dataclass
class TaskGraph:
    goal: str
    tasks: list[TaskSpec] = field(default_factory=list)

    def __post_init__(self) -> None:
        self.validate()

    @classmethod
    def from_steps(
        cls,
        *,
        goal: str,
        steps: Iterable[dict[str, Any]],
        sequential_default: bool = True,
    ) -> "TaskGraph":
        tasks: list[TaskSpec] = []
        previous_id = ""
        used_ids: set[str] = set()
        for index, raw in enumerate(steps, start=1):
            if not isinstance(raw, dict):
                continue
            tool_id = str(raw.get("tool") or raw.get("tool_id") or "")
            kind = str(raw.get("kind") or ("tool" if tool_id else "task"))
            base_id = str(raw.get("task_id") or f"task-{index}")
            task_id = base_id
            suffix = 2
            while task_id in used_ids:
                task_id = f"{base_id}-{suffix}"
                suffix += 1
            used_ids.add(task_id)
            explicit_dependencies = raw.get("depends_on")
            if explicit_dependencies is None:
                dependencies = (previous_id,) if sequential_default and previous_id else ()
            else:
                dependencies = tuple(
                    str(value)
                    for value in explicit_dependencies
                    if str(value).strip()
                )
            tasks.append(
                TaskSpec(
                    task_id=task_id,
                    kind=kind,
                    label=str(raw.get("label") or raw.get("title") or tool_id or task_id),
                    tool_id=tool_id,
                    args=dict(raw.get("args") or {}),
                    depends_on=dependencies,
                )
            )
            previous_id = task_id
        return cls(goal=goal, tasks=tasks)

    def validate(self) -> None:
        ids = [task.task_id for task in self.tasks]
        if any(not task_id for task_id in ids):
            raise ValueError("task_id 不能为空")
        if len(ids) != len(set(ids)):
            raise ValueError("TaskGraph 中存在重复 task_id")
        known = set(ids)
        for task in self.tasks:
            missing = [dep for dep in task.depends_on if dep not in known]
            if missing:
                raise ValueError(
                    f"任务 {task.task_id} 引用了不存在的依赖: {','.join(missing)}"
                )
            if task.task_id in task.depends_on:
                raise ValueError(f"任务 {task.task_id} 不能依赖自身")

        pending = {task.task_id: set(task.depends_on) for task in self.tasks}
        resolved: set[str] = set()
        while pending:
            ready = [task_id for task_id, deps in pending.items() if deps <= resolved]
            if not ready:
                raise ValueError("TaskGraph 中存在循环依赖")
            for task_id in ready:
                resolved.add(task_id)
                pending.pop(task_id)

    def task(self, task_id: str) -> TaskSpec | None:
        return next((task for task in self.tasks if task.task_id == task_id), None)

    def ready_tasks(self) -> list[TaskSpec]:
        statuses = {task.task_id: task.status for task in self.tasks}
        return [
            task
            for task in self.tasks
            if task.status == "pending"
            and all(statuses.get(dep) in SUCCESS_TASK_STATUSES for dep in task.depends_on)
        ]

    def blocked_tasks(self) -> list[TaskSpec]:
        statuses = {task.task_id: task.status for task in self.tasks}
        return [
            task
            for task in self.tasks
            if task.status == "pending"
            and any(
                statuses.get(dep) in TERMINAL_TASK_STATUSES - SUCCESS_TASK_STATUSES
                for dep in task.depends_on
            )
        ]

    def mark_running(self, task_id: str, *, observation: dict[str, Any] | None = None) -> None:
        task = self.task(task_id)
        if task is None:
            raise KeyError(task_id)
        if task.status != "pending":
            raise ValueError(f"任务 {task_id} 当前状态不能启动: {task.status}")
        if task not in self.ready_tasks():
            raise ValueError(f"任务 {task_id} 的依赖尚未满足")
        task.status = "running"
        task.observation = dict(observation or {})

    def mark_terminal(
        self,
        task_id: str,
        *,
        status: str,
        observation: dict[str, Any] | None = None,
    ) -> None:
        if status not in TERMINAL_TASK_STATUSES:
            raise ValueError(f"非法终态: {status}")
        task = self.task(task_id)
        if task is None:
            raise KeyError(task_id)
        task.status = status
        task.observation = dict(observation or {})

    @property
    def status(self) -> str:
        statuses = {task.status for task in self.tasks}
        if not self.tasks:
            return "completed"
        if "running" in statuses:
            return "running"
        if "failed" in statuses or "denied" in statuses:
            return "failed"
        if "cancelled" in statuses:
            return "cancelled"
        if statuses <= SUCCESS_TASK_STATUSES:
            return "completed"
        if statuses & {"pending", "not_executed"}:
            return "incomplete"
        return "partial"

    def to_dict(self) -> dict[str, Any]:
        return {
            "goal": self.goal,
            "status": self.status,
            "tasks": [task.to_dict() for task in self.tasks],
        }
