"""Bounded, sequential agents for scholarship preparation.

Each agent claims one task and reads only the artifacts from earlier tasks.
The event sink is called for every transition so a failed step remains visible.
"""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

from app.retrieval import Evidence


MAX_AGENT_STEPS = 3
EventSink = Callable[[dict[str, Any]], Awaitable[None]]


@dataclass(frozen=True)
class WorkflowInput:
    evidence: tuple[Evidence, ...]
    unknowns: tuple[str, ...]
    security_findings: tuple[str, ...]
    conflict_detected: bool
    action_requested: bool


@dataclass
class WorkflowResult:
    status: str = "PENDING"
    steps_used: int = 0
    tasks: dict[str, str] = field(default_factory=dict)
    artifacts: dict[str, dict[str, Any]] = field(default_factory=dict)
    events: list[dict[str, Any]] = field(default_factory=list)
    error_code: str | None = None

    @property
    def can_create_task(self) -> bool:
        return self.status == "SUCCEEDED" and bool(
            self.artifacts.get("safety_review", {}).get("allow_action")
        )

    def snapshot(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "step_limit": MAX_AGENT_STEPS,
            "steps_used": self.steps_used,
            "tasks": dict(self.tasks),
            "artifacts": deepcopy(self.artifacts),
            "error_code": self.error_code,
        }


def _policy_check(data: WorkflowInput, artifacts: dict[str, dict]) -> dict[str, Any]:
    topics = {
        "eligibility": ("条件", "资格"),
        "application_process": ("申请", "评审", "公示"),
        "materials": ("材料", "证明"),
        "deadline": ("截止", "时间"),
    }
    supported_topics = {
        topic: [item.chunk_id for item in data.evidence
                if any(word in item.text for word in words)]
        for topic, words in topics.items()
    }
    return {
        "evidence_ids": [item.chunk_id for item in data.evidence],
        "evidence_count": len(data.evidence),
        "conflict_detected": data.conflict_detected,
        "supported_topics": supported_topics,
        "non_active_ids": [item.chunk_id for item in data.evidence
                           if item.collection_status != "ACTIVE"],
    }


def _preparation_plan(data: WorkflowInput, artifacts: dict[str, dict]) -> dict[str, Any]:
    policy = artifacts["policy_check"]
    topics = policy["supported_topics"]
    steps = []
    if policy["evidence_count"]:
        steps.append("核对引用资料的适用年份和申请条件")
    else:
        steps.append("先查找适用年份的学校正式通知")
    steps.append(
        "按引用资料核对申请及评审流程" if topics["application_process"]
        else "向学校核对正式申请及评审流程"
    )
    steps.append(
        "按引用资料核对申请材料" if topics["materials"]
        else "向学校核对申请材料"
    )
    if data.unknowns:
        steps.append("补充适用信息：" + "、".join(data.unknowns))
    steps.append("核对所问年份的截止时间" if topics["deadline"] else "向学校核对当年截止时间")
    return {
        "steps": steps,
        "source_chunk_ids": policy["evidence_ids"],
        "topic_sources": topics,
    }


def _safety_review(data: WorkflowInput, artifacts: dict[str, dict]) -> dict[str, Any]:
    policy = artifacts["policy_check"]
    plan = artifacts["preparation_plan"]
    reasons = []
    if not policy["evidence_count"]:
        reasons.append("NO_EVIDENCE")
    if data.unknowns:
        reasons.append("MISSING_PROFILE")
    if data.security_findings:
        reasons.append("SECURITY_FINDING")
    if not plan["steps"]:
        reasons.append("NO_PLAN")
    if not set(plan["source_chunk_ids"]) <= set(policy["evidence_ids"]):
        reasons.append("UNVERIFIED_PLAN_SOURCE")
    return {
        "allow_action": not reasons,
        "reasons": reasons,
        "confirmation_required": True,
        "action_requested": data.action_requested,
    }


AGENTS = (
    ("policy_check", _policy_check),
    ("preparation_plan", _preparation_plan),
    ("safety_review", _safety_review),
)


async def run_scholarship_workflow(
    data: WorkflowInput,
    *,
    event_sink: EventSink,
    agents=AGENTS,
) -> WorkflowResult:
    """Claim at most three fixed tasks; stop on first agent failure."""
    result = WorkflowResult(tasks={name: "PENDING" for name, _ in agents})
    if len(agents) > MAX_AGENT_STEPS or len({name for name, _ in agents}) != len(agents):
        raise ValueError("invalid agent registry")

    async def append(agent: str, kind: str, payload: dict[str, Any]) -> None:
        event = {
            "sequence": len(result.events) + 1,
            "agent": agent,
            "event_type": kind,
            "payload": deepcopy(payload),
        }
        await event_sink(event)
        result.events.append(event)

    result.status = "RUNNING"
    for name, agent in agents:
        result.steps_used += 1
        result.tasks[name] = "RUNNING"
        await append(name, "TASK_CLAIMED", {"step": result.steps_used})
        try:
            artifact = agent(data, deepcopy(result.artifacts))
            if not isinstance(artifact, dict):
                raise TypeError("agent artifact must be an object")
        except Exception as exc:
            result.tasks[name] = "FAILED"
            result.status = "FAILED"
            result.error_code = type(exc).__name__
            await append(name, "TASK_FAILED", {"error_code": result.error_code})
            break
        result.artifacts[name] = deepcopy(artifact)
        await append(name, "ARTIFACT_CREATED", artifact)
        result.tasks[name] = "SUCCEEDED"
        await append(name, "TASK_COMPLETED", {})
    else:
        result.status = "SUCCEEDED"
    return result
