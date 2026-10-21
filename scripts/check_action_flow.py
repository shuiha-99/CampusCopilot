import asyncio
import json
import sys
from datetime import datetime

from sqlalchemy import delete, update

from app.db.session import async_session_factory
from app.mcp_server import invoke_create_preparation_task
from app.models import ActionDraft, ActionTask
from app.services.action_service import (
    authorize_draft_confirmation,
    build_preparation_result,
    create_action_draft,
    finish_task,
    get_task,
    lease_one_task,
)


if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")


async def main() -> None:
    user_id = "integration-action-user"
    async with async_session_factory() as cleanup_session:
        async with cleanup_session.begin():
            await cleanup_session.execute(delete(ActionTask).where(ActionTask.user_id == user_id))
            await cleanup_session.execute(delete(ActionDraft).where(ActionDraft.user_id == user_id))
    async with async_session_factory() as session:
        draft, token = await create_action_draft(
            session,
            user_id=user_id,
            conversation_id=None,
            run_id=None,
            action_type="scholarship_preparation",
            details={
                "grade": "大二",
                "college": "计算机工程学院",
                "scholarship_type": "国家励志奖学金",
            },
        )
    unconfirmed_arguments = {
        "draft_id": draft.id,
        "run_id": draft.run_id,
        "user_id": draft.user_id,
        "conversation_id": draft.conversation_id,
        "action_type": draft.action_type,
        "payload_json": draft.payload_json,
        "idempotency_key": f"confirmed:{draft.operation_hash}",
    }
    try:
        await invoke_create_preparation_task(**unconfirmed_arguments)
    except Exception:
        pass
    else:
        raise AssertionError("未确认动作被 MCP 工具接受")
    async with async_session_factory() as session:
        confirmed = await authorize_draft_confirmation(
            session,
            draft_id=draft.id,
            user_id=user_id,
            confirmation_token=token,
        )

    arguments = {
        "draft_id": confirmed.id,
        "run_id": confirmed.run_id,
        "user_id": confirmed.user_id,
        "conversation_id": confirmed.conversation_id,
        "action_type": confirmed.action_type,
        "payload_json": confirmed.payload_json,
        "idempotency_key": f"confirmed:{confirmed.operation_hash}",
    }
    first = await invoke_create_preparation_task(**arguments)
    second = await invoke_create_preparation_task(**arguments)
    if first["task_id"] != second["task_id"]:
        raise AssertionError("重复 MCP 调用创建了不同任务")

    stale_worker_id = "integration-stale-worker"
    async with async_session_factory() as session:
        stale_lease = await lease_one_task(
            session, worker_id=stale_worker_id, lease_seconds=-1
        )
    if stale_lease is None:
        raise AssertionError("没有领取到刚创建的任务")
    stale_task, stale_generation = stale_lease

    worker_id = "integration-recovery-worker"
    async with async_session_factory() as session:
        leased = await lease_one_task(session, worker_id=worker_id)
    if leased is None:
        raise AssertionError("超时任务没有被新 worker 接管")
    task, generation = leased
    result = build_preparation_result(task)
    async with async_session_factory() as session:
        stale_committed = await finish_task(
            session,
            task_id=stale_task.id,
            worker_id=stale_worker_id,
            generation=stale_generation,
            result={"invalid": "stale worker result"},
        )
    if stale_committed:
        raise AssertionError("过期 worker 覆盖了新租约结果")
    async with async_session_factory() as session:
        if not await finish_task(
            session,
            task_id=task.id,
            worker_id=worker_id,
            generation=generation,
            result=result,
        ):
            raise AssertionError("任务租约提交失败")
    async with async_session_factory() as session:
        completed = await get_task(session, task_id=task.id, user_id=user_id)
    if completed is None or completed.status != "SUCCEEDED":
        raise AssertionError("任务没有成功完成")

    async with async_session_factory() as session:
        retry_draft, retry_token = await create_action_draft(
            session,
            user_id=user_id,
            conversation_id=None,
            run_id=None,
            action_type="major_transfer_preparation",
            details={"grade": "大一", "target_major": "计算机科学与技术"},
        )
    async with async_session_factory() as session:
        retry_confirmed = await authorize_draft_confirmation(
            session,
            draft_id=retry_draft.id,
            user_id=user_id,
            confirmation_token=retry_token,
        )
    retry_arguments = {
        "draft_id": retry_confirmed.id,
        "run_id": retry_confirmed.run_id,
        "user_id": retry_confirmed.user_id,
        "conversation_id": retry_confirmed.conversation_id,
        "action_type": retry_confirmed.action_type,
        "payload_json": retry_confirmed.payload_json,
        "idempotency_key": f"confirmed:{retry_confirmed.operation_hash}",
    }
    retry_created = await invoke_create_preparation_task(**retry_arguments)
    retry_states = []
    for attempt in range(1, 4):
        retry_worker_id = f"integration-retry-worker-{attempt}"
        async with async_session_factory() as session:
            retry_lease = await lease_one_task(session, worker_id=retry_worker_id)
        if retry_lease is None:
            raise AssertionError(f"第 {attempt} 次失败演示没有领取到任务")
        retry_task, retry_generation = retry_lease
        async with async_session_factory() as session:
            if not await finish_task(
                session,
                task_id=retry_task.id,
                worker_id=retry_worker_id,
                generation=retry_generation,
                result=None,
                error=f"integration failure {attempt}",
            ):
                raise AssertionError(f"第 {attempt} 次失败状态提交被拒绝")
        async with async_session_factory() as session:
            current = await get_task(
                session,
                task_id=retry_created["task_id"],
                user_id=user_id,
            )
        if current is None:
            raise AssertionError("重试任务丢失")
        retry_states.append(
            {
                "attempts": current.attempts,
                "status": current.status,
                "has_next_attempt_at": current.next_attempt_at is not None,
            }
        )
        if attempt < 3:
            if current.status != "RETRY" or current.next_attempt_at is None:
                raise AssertionError("失败任务未正确进入延迟重试")
            async with async_session_factory() as session:
                async with session.begin():
                    await session.execute(
                        update(ActionTask)
                        .where(ActionTask.id == current.id)
                        .values(next_attempt_at=datetime(2000, 1, 1))
                    )
        elif current.status != "MANUAL_REVIEW" or current.next_attempt_at is not None:
            raise AssertionError("第三次失败后未进入人工处理")
    print(
        json.dumps(
            {
                "mcp_idempotent": True,
                "unconfirmed_action_rejected": True,
                "lease_takeover_safe": True,
                "task_id": completed.id,
                "status": completed.status,
                "attempts": completed.attempts,
                "result": json.loads(completed.result_json or "{}"),
                "retry_states": retry_states,
            },
            ensure_ascii=False,
        )
    )
    async with async_session_factory() as cleanup_session:
        async with cleanup_session.begin():
            await cleanup_session.execute(delete(ActionTask).where(ActionTask.user_id == user_id))
            await cleanup_session.execute(delete(ActionDraft).where(ActionDraft.user_id == user_id))


if __name__ == "__main__":
    asyncio.run(main())
