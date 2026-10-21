"""Real-MySQL and in-process HTTP checks for authorized manual task decisions."""

import asyncio
import json
import secrets
import sys
import uuid
from datetime import datetime, timedelta

import httpx
from pydantic import SecretStr
from sqlalchemy import delete, or_, select, update

from app.core.config import settings
from app.db.session import async_session_factory
from app.models import ActionDraft, ActionTask, ActionTaskOperation
from app.services.action_service import (
    ManualTaskConflict,
    decide_manual_task,
    finish_task,
    lease_one_task,
)
from main import app


if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")


USER_ID = "integration-manual-recovery"


async def create_task(*, status: str, attempts: int, worker_id: str | None = None) -> str:
    task_id = str(uuid.uuid4())
    draft_id = str(uuid.uuid4())
    payload = json.dumps(
        {
            "action_type": "major_transfer_preparation",
            "user_id": USER_ID,
            "conversation_id": None,
            "details": {"grade": "大一", "target_major": "计算机科学与技术"},
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    async with async_session_factory() as session:
        async with session.begin():
            session.add(
                ActionDraft(
                    id=draft_id,
                    user_id=USER_ID,
                    run_id=None,
                    conversation_id=None,
                    action_type="major_transfer_preparation",
                    payload_json=payload,
                    operation_hash="integration-hash-" + draft_id,
                    confirmation_token_hash="integration-token-" + draft_id,
                    status="CONFIRMED",
                    expires_at=datetime.utcnow() + timedelta(minutes=15),
                    confirmed_at=datetime.utcnow(),
                )
            )
            session.add(
                ActionTask(
                    id=task_id,
                    draft_id=draft_id,
                    user_id=USER_ID,
                    run_id=None,
                    conversation_id=None,
                    action_type="major_transfer_preparation",
                    payload_json=payload,
                    idempotency_key="integration-manual:" + draft_id,
                    status=status,
                    attempts=attempts,
                    retry_attempts=attempts,
                    generation=attempts,
                    lease_owner=worker_id,
                    lease_until=(datetime.utcnow() + timedelta(minutes=5)) if worker_id else None,
                    error_message="integration failure" if status == "MANUAL_REVIEW" else None,
                )
            )
    return task_id


async def cleanup() -> None:
    async with async_session_factory() as session:
        async with session.begin():
            task_ids = select(ActionTask.id).where(ActionTask.user_id == USER_ID)
            await session.execute(delete(ActionTaskOperation).where(ActionTaskOperation.task_id.in_(task_ids)))
            await session.execute(delete(ActionTask).where(ActionTask.user_id == USER_ID))
            await session.execute(delete(ActionDraft).where(ActionDraft.user_id == USER_ID))


async def concurrent_resume(task_id: str) -> str:
    async with async_session_factory() as session:
        try:
            await decide_manual_task(
                session,
                task_id=task_id,
                operator_id="integration-operator",
                operation="RESUME",
                reason="并发恢复验证",
            )
            return "accepted"
        except ManualTaskConflict:
            return "conflict"


async def check() -> dict:
    await cleanup()
    async with async_session_factory() as session:
        other_eligible = await session.scalar(
            select(ActionTask.id).where(
                ActionTask.user_id != USER_ID,
                or_(
                    ActionTask.status.in_(["PENDING", "RETRY"]),
                    (ActionTask.status == "RUNNING")
                    & (ActionTask.lease_until < datetime.utcnow()),
                ),
            ).limit(1)
        )
    if other_eligible is not None:
        raise RuntimeError("有其他可领取任务，不能安全运行人工恢复专项验收")
    token = secrets.token_urlsafe(48)
    settings.manual_operator_token = SecretStr(token)
    settings.manual_operator_id = "integration-operator"
    task_id = await create_task(status="MANUAL_REVIEW", attempts=3)
    running_id = await create_task(status="RUNNING", attempts=1, worker_id="stale-worker")
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://testserver"
        ) as client:
            path = f"/actions/manual/tasks/{task_id}"
            assert (await client.get("/actions/manual/tasks")).status_code == 401
            assert (await client.post(path + "/resume", json={"reason": "test"},
                                      headers={"Authorization": "Bearer wrong"})).status_code == 401
            headers = {"Authorization": "Bearer " + token}
            listed = await client.get("/actions/manual/tasks", headers=headers)
            assert listed.status_code == 200, listed.text
            assert task_id in {row["task_id"] for row in listed.json()}
            initial = await client.get(path, headers=headers)
            assert initial.status_code == 200 and initial.json()["retry_attempts"] == 3
            assert initial.json()["operations"] == []
            assert (await client.post(
                path + "/resume",
                json={"reason": "试图改内容", "payload": {"target_major": "其他专业"}},
                headers=headers,
            )).status_code == 422
            resumed = await client.post(path + "/resume", json={"reason": "原内容重新执行"}, headers=headers)
            assert resumed.status_code == 200, resumed.text
            assert resumed.json()["status"] == "PENDING"
            assert resumed.json()["payload"] == initial.json()["payload"]
            assert resumed.json()["attempts"] == 3 and resumed.json()["retry_attempts"] == 0
            assert (await client.post(path + "/resume", json={"reason": "重复"}, headers=headers)).status_code == 409

            for number in range(1, 4):
                async with async_session_factory() as session:
                    leased = await lease_one_task(session, worker_id=f"round2-worker-{number}")
                assert leased is not None and leased[0].id == task_id
                async with async_session_factory() as session:
                    assert await finish_task(
                        session, task_id=task_id, worker_id=f"round2-worker-{number}",
                        generation=leased[1], result=None, error="integration failure",
                    )
                if number < 3:
                    async with async_session_factory() as session:
                        async with session.begin():
                            await session.execute(
                                update(ActionTask).where(ActionTask.id == task_id)
                                .values(next_attempt_at=datetime(2000, 1, 1))
                            )
            after_round = (await client.get(path, headers=headers)).json()
            assert after_round["status"] == "MANUAL_REVIEW"
            assert after_round["attempts"] == 6 and after_round["retry_attempts"] == 3
            assert len(after_round["operations"]) == 1

            outcomes = await asyncio.gather(concurrent_resume(task_id), concurrent_resume(task_id))
            assert sorted(outcomes) == ["accepted", "conflict"], outcomes
            assert (await client.get(path, headers=headers)).json()["status"] == "PENDING"
            cancelled = await client.post(path + "/cancel", json={"reason": "人工终止"}, headers=headers)
            assert cancelled.status_code == 200 and cancelled.json()["status"] == "CANCELLED"
            assert (await client.post(path + "/resume", json={"reason": "终止后恢复"}, headers=headers)).status_code == 409

            running_path = f"/actions/manual/tasks/{running_id}"
            stopped = await client.post(running_path + "/cancel", json={"reason": "执行中终止"}, headers=headers)
            assert stopped.status_code == 200 and stopped.json()["status"] == "CANCELLED"
            async with async_session_factory() as session:
                stale_write = await finish_task(
                    session, task_id=running_id, worker_id="stale-worker", generation=1,
                    result={"wrong": True},
                )
            assert stale_write is False

            crash_id = await create_task(status="RUNNING", attempts=3, worker_id="crashed-worker")
            async with async_session_factory() as session:
                async with session.begin():
                    await session.execute(
                        update(ActionTask).where(ActionTask.id == crash_id)
                        .values(lease_until=datetime(2000, 1, 1))
                    )
            async with async_session_factory() as session:
                assert await lease_one_task(session, worker_id="after-crash-worker") is None
            crash_state = (await client.get(
                f"/actions/manual/tasks/{crash_id}", headers=headers
            )).json()
            assert crash_state["status"] == "MANUAL_REVIEW"
            assert crash_state["attempts"] == 3 and crash_state["retry_attempts"] == 3
            async with async_session_factory() as session:
                assert not await finish_task(
                    session, task_id=crash_id, worker_id="crashed-worker",
                    generation=3, result={"wrong": True},
                )

            final = (await client.get(path, headers=headers)).json()
            assert [item["operation"] for item in final["operations"]] == [
                "RESUME", "RESUME", "CANCEL"
            ]
            assert all(item["operator_id"] == "integration-operator"
                       for item in final["operations"])
            async with async_session_factory() as session:
                eligible_other = await session.scalar(
                    select(ActionTask.id).where(
                        ActionTask.user_id != USER_ID,
                        or_(
                            ActionTask.status.in_(["PENDING", "RETRY"]),
                            (ActionTask.status == "RUNNING")
                            & (ActionTask.lease_until < datetime.utcnow()),
                        ),
                    ).limit(1)
                )
            if eligible_other is None:
                async with async_session_factory() as session:
                    assert await lease_one_task(session, worker_id="after-cancel-worker") is None
            return {
                "unauthorized_rejected": True,
                "duplicate_resume_rejected": True,
                "concurrent_resume": sorted(outcomes),
                "total_attempts_after_second_round": after_round["attempts"],
                "retry_attempts_after_second_round": after_round["retry_attempts"],
                "cancelled_not_leased": eligible_other is None,
                "cancelled_task_status": stopped.json()["status"],
                "old_worker_finish_return": stale_write,
                "stale_worker_rejected": not stale_write,
                "crash_limit_manual_review": crash_state["status"] == "MANUAL_REVIEW",
                "audit_operations": [item["operation"] for item in final["operations"]],
            }
    finally:
        await cleanup()
        settings.manual_operator_token = None


if __name__ == "__main__":
    print(json.dumps(asyncio.run(check()), ensure_ascii=False))
