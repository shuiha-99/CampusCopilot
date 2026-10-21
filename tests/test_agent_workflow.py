import asyncio

from app.retrieval.hybrid import Evidence
from app.runtime.workflow import (
    MAX_AGENT_STEPS,
    WorkflowInput,
    _policy_check,
    run_scholarship_workflow,
)


def make_input(*, evidence=True, unknowns=(), security=()):
    items = (Evidence(
        chunk_id="school:award", source_key="school", title="学校通知",
        url="https://example.test", text="奖学金申请资料", scope="学校",
        collection_status="ACTIVE", validity="UNKNOWN", version_label="v1",
        published_at="2026-01-01", score=1.0, vector_score=1.0, bm25_score=1.0,
    ),) if evidence else ()
    return WorkflowInput(
        evidence=items, unknowns=unknowns, security_findings=security,
        conflict_detected=False, action_requested=True,
    )


def test_agents_claim_in_order_and_publish_distinct_artifacts():
    persisted = []

    async def save(event):
        persisted.append(event)

    result = asyncio.run(run_scholarship_workflow(make_input(), event_sink=save))
    assert result.status == "SUCCEEDED"
    assert result.steps_used == MAX_AGENT_STEPS == 3
    assert list(result.tasks.values()) == ["SUCCEEDED"] * 3
    assert [(event["agent"], event["event_type"]) for event in persisted] == [
        (name, kind) for name in ("policy_check", "preparation_plan", "safety_review")
        for kind in ("TASK_CLAIMED", "ARTIFACT_CREATED", "TASK_COMPLETED")
    ]
    assert [event["sequence"] for event in persisted] == list(range(1, 10))
    assert result.artifacts["preparation_plan"]["source_chunk_ids"] == ["school:award"]
    assert result.artifacts["policy_check"]["supported_topics"]["application_process"] == [
        "school:award"
    ]
    assert result.artifacts["policy_check"]["supported_topics"]["materials"] == []
    assert "向学校核对申请材料" in result.artifacts["preparation_plan"]["steps"]
    assert result.can_create_task is True
    assert result.artifacts["safety_review"]["confirmation_required"] is True


def test_missing_evidence_and_profile_close_action_gate():
    async def save(event):
        pass

    result = asyncio.run(run_scholarship_workflow(
        make_input(evidence=False, unknowns=("年级",)), event_sink=save
    ))
    assert result.status == "SUCCEEDED"
    assert result.can_create_task is False
    assert result.artifacts["safety_review"]["reasons"] == [
        "NO_EVIDENCE", "MISSING_PROFILE"
    ]


def test_agent_failure_stops_following_claims_and_keeps_failure_event():
    events = []

    async def save(event):
        events.append(event)

    def broken(data, artifacts):
        raise RuntimeError("failed")

    def must_not_run(data, artifacts):
        raise AssertionError("a later agent was claimed")

    result = asyncio.run(run_scholarship_workflow(
        make_input(), event_sink=save,
        agents=(("policy_check", broken), ("preparation_plan", must_not_run)),
    ))
    assert result.status == "FAILED"
    assert result.tasks == {"policy_check": "FAILED", "preparation_plan": "PENDING"}
    assert result.steps_used == 1
    assert result.can_create_task is False
    assert [event["event_type"] for event in events] == ["TASK_CLAIMED", "TASK_FAILED"]
    assert events[-1]["payload"] == {"error_code": "RuntimeError"}


def test_planning_failure_preserves_policy_artifact_and_skips_review():
    events = []

    async def save(event):
        events.append(event)

    def broken_plan(data, artifacts):
        assert artifacts["policy_check"]["evidence_ids"] == ["school:award"]
        raise RuntimeError("planning failed")

    def review_must_not_run(data, artifacts):
        raise AssertionError("safety review was claimed")

    result = asyncio.run(run_scholarship_workflow(
        make_input(), event_sink=save,
        agents=(
            ("policy_check", _policy_check),
            ("preparation_plan", broken_plan),
            ("safety_review", review_must_not_run),
        ),
    ))
    assert result.status == "FAILED"
    assert result.steps_used == 2
    assert result.tasks == {
        "policy_check": "SUCCEEDED",
        "preparation_plan": "FAILED",
        "safety_review": "PENDING",
    }
    assert result.can_create_task is False
    assert list(result.artifacts) == ["policy_check"]
    assert [event["event_type"] for event in events] == [
        "TASK_CLAIMED", "ARTIFACT_CREATED", "TASK_COMPLETED",
        "TASK_CLAIMED", "TASK_FAILED",
    ]
    assert events[1]["payload"]["evidence_ids"] == ["school:award"]


def test_registry_cannot_exceed_step_limit():
    async def save(event):
        raise AssertionError("invalid workflow must not write events")

    try:
        asyncio.run(run_scholarship_workflow(
            make_input(), event_sink=save,
            agents=tuple((str(index), lambda data, artifacts: {}) for index in range(4)),
        ))
    except ValueError as exc:
        assert str(exc) == "invalid agent registry"
    else:
        raise AssertionError("unbounded registry accepted")
