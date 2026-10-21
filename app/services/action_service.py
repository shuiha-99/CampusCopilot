from __future__ import annotations

import hashlib
import json
import secrets
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import or_, select
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import ActionDraft, ActionTask, ActionTaskOperation, Conversation
from app.services.persistence_errors import ConversationNotFoundError, PersistenceError


class ActionValidationError(ValueError):
    pass


class ConfirmationError(ValueError):
    pass


class ManualTaskNotFound(ValueError):
    pass


class ManualTaskConflict(ValueError):
    pass


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _canonical_json(value: dict[str, Any]) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _validate_details(action_type: str, details: dict[str, Any]) -> None:
    required = {
        "scholarship_preparation": {"grade", "college", "scholarship_type"},
        "major_transfer_preparation": {"grade", "target_major"},
    }[action_type]
    missing = sorted(key for key in required if not str(details.get(key, "")).strip())
    if missing:
        raise ActionValidationError("缺少创建准备任务所需字段：" + "、".join(missing))


async def create_action_draft(
    session: AsyncSession,
    *,
    user_id: str,
    conversation_id: int | None,
    run_id: str | None,
    action_type: str,
    details: dict[str, Any],
) -> tuple[ActionDraft, str]:
    _validate_details(action_type, details)
    payload = {
        "action_type": action_type,
        "user_id": user_id,
        "conversation_id": conversation_id,
        "details": details,
    }
    payload_json = _canonical_json(payload)
    token = secrets.token_urlsafe(32)
    draft = ActionDraft(
        id=str(uuid.uuid4()),
        run_id=run_id,
        user_id=user_id,
        conversation_id=conversation_id,
        action_type=action_type,
        payload_json=payload_json,
        operation_hash=_digest(payload_json),
        confirmation_token_hash=_digest(token),
        status="DRAFT",
        expires_at=_now() + timedelta(minutes=15),
    )
    try:
        async with session.begin():
            if conversation_id is not None:
                owner = await session.scalar(
                    select(Conversation.id).where(
                        Conversation.id == conversation_id, Conversation.user_id == user_id
                    )
                )
                if owner is None:
                    raise ConversationNotFoundError("会话不存在或不属于当前用户")
            session.add(draft)
            await session.flush()
    except (ConversationNotFoundError, ActionValidationError):
        raise
    except SQLAlchemyError as exc:
        raise PersistenceError("保存动作草稿失败") from exc
    return draft, token


async def authorize_draft_confirmation(
    session: AsyncSession,
    *,
    draft_id: str,
    user_id: str,
    confirmation_token: str,
) -> ActionDraft:
    try:
        async with session.begin():
            draft = await session.scalar(
                select(ActionDraft)
                .where(ActionDraft.id == draft_id, ActionDraft.user_id == user_id)
                .with_for_update()
            )
            if draft is None:
                raise ConfirmationError("动作草稿不存在或不属于当前用户")
            if not secrets.compare_digest(
                draft.confirmation_token_hash, _digest(confirmation_token)
            ):
                raise ConfirmationError("确认令牌无效")
            if _digest(draft.payload_json) != draft.operation_hash:
                raise ConfirmationError("待确认的操作内容已经变化")
            if draft.expires_at < _now():
                draft.status = "EXPIRED"
                raise ConfirmationError("确认已过期，请重新生成草稿")
            if draft.status not in {"DRAFT", "CONFIRMED"}:
                raise ConfirmationError("当前草稿状态不可确认")
            draft.status = "CONFIRMED"
            draft.confirmed_at = draft.confirmed_at or _now()
            await session.flush()
    except ConfirmationError:
        raise
    except SQLAlchemyError as exc:
        raise PersistenceError("确认动作草稿失败") from exc
    return draft


async def create_task_idempotently(
    session: AsyncSession,
    *,
    draft_id: str,
    run_id: str | None,
    user_id: str,
    conversation_id: int | None,
    action_type: str,
    payload_json: str,
    idempotency_key: str,
) -> ActionTask:
    try:
        async with session.begin():
            draft = await session.scalar(
                select(ActionDraft)
                .where(ActionDraft.id == draft_id, ActionDraft.user_id == user_id)
                .with_for_update()
            )
            if draft is None or draft.status != "CONFIRMED":
                raise ConfirmationError("MCP 工具拒绝未确认的动作")
            if (
                draft.run_id != run_id
                or draft.conversation_id != conversation_id
                or draft.action_type != action_type
                or draft.payload_json != payload_json
                or idempotency_key != f"confirmed:{draft.operation_hash}"
            ):
                raise ConfirmationError("MCP 工具参数与已确认动作不一致")
            existing = await session.scalar(
                select(ActionTask).where(ActionTask.idempotency_key == idempotency_key)
            )
            if existing is not None:
                return existing
            task = ActionTask(
                id=str(uuid.uuid4()),
                draft_id=draft_id,
                run_id=run_id,
                user_id=user_id,
                conversation_id=conversation_id,
                action_type=action_type,
                payload_json=payload_json,
                idempotency_key=idempotency_key,
                status="PENDING",
            )
            session.add(task)
            await session.flush()
            return task
    except ConfirmationError:
        raise
    except IntegrityError:
        await session.rollback()
        existing = await session.scalar(
            select(ActionTask).where(ActionTask.idempotency_key == idempotency_key)
        )
        if existing is not None:
            return existing
        raise PersistenceError("幂等任务创建冲突")
    except SQLAlchemyError as exc:
        raise PersistenceError("创建动作任务失败") from exc


async def get_task(session: AsyncSession, *, task_id: str, user_id: str) -> ActionTask | None:
    try:
        async with session.begin():
            return await session.scalar(
                select(ActionTask).where(ActionTask.id == task_id, ActionTask.user_id == user_id)
            )
    except SQLAlchemyError as exc:
        raise PersistenceError("读取任务失败") from exc


async def lease_one_task(
    session: AsyncSession, *, worker_id: str, lease_seconds: int = 30
) -> tuple[ActionTask, int] | None:
    now = _now()
    try:
        async with session.begin():
            task = await session.scalar(
                select(ActionTask)
                .where(
                    or_(
                        (
                            ActionTask.status.in_(["PENDING", "RETRY"])
                            & or_(
                                ActionTask.next_attempt_at.is_(None),
                                ActionTask.next_attempt_at <= now,
                            )
                        ),
                        (
                            (ActionTask.status == "RUNNING")
                            & (ActionTask.lease_until < now)
                        ),
                    ),
                )
                .order_by(ActionTask.created_at)
                .with_for_update(skip_locked=True)
                .limit(1)
            )
            if task is None:
                return None
            if task.status == "RUNNING" and task.retry_attempts >= 3:
                task.status = "MANUAL_REVIEW"
                task.error_message = task.error_message or "执行租约过期，达到本轮尝试上限"
                task.next_attempt_at = None
                task.lease_owner = None
                task.lease_until = None
                task.generation += 1
                await session.flush()
                return None
            task.status = "RUNNING"
            task.attempts += 1
            task.retry_attempts += 1
            task.generation += 1
            task.lease_owner = worker_id
            task.lease_until = now + timedelta(seconds=lease_seconds)
            generation = task.generation
            await session.flush()
            return task, generation
    except SQLAlchemyError as exc:
        raise PersistenceError("领取任务失败") from exc


def build_preparation_result(task: ActionTask) -> dict[str, Any]:
    payload = json.loads(task.payload_json)
    details = payload["details"]
    if task.action_type == "scholarship_preparation":
        items = [
            "核对当年奖助学金评审通知和截止时间",
            f"向{details['college']}确认学院评审安排",
            "准备成绩、综合表现和家庭经济困难认定相关材料",
            "提交前再次核对申请类别是否可以与其他奖助项目同时获得",
            "保存提交回执及学院、学校公示信息",
        ]
    else:
        items = [
            "查询当前年级最新转专业通知和接收计划",
            f"核对拟转入专业“{details['target_major']}”的条件、人数与考核方式",
            "确认本人学籍、处分、招生类型和身体条件是否满足要求",
            "在规定时间通过教务系统提交申请并保存回执",
            "关注学院考核、学校复审、公示及后续课程补修安排",
        ]
    return {
        "title": "奖学金申请准备清单" if task.action_type == "scholarship_preparation" else "转专业准备清单",
        "items": items,
        "disclaimer": "这是项目内部准备任务，不代表已经向学校提交申请或获得审核。",
    }


async def finish_task(
    session: AsyncSession,
    *,
    task_id: str,
    worker_id: str,
    generation: int,
    result: dict[str, Any] | None,
    error: str | None = None,
) -> bool:
    try:
        async with session.begin():
            task = await session.scalar(
                select(ActionTask).where(ActionTask.id == task_id).with_for_update()
            )
            if (
                task is None
                or task.lease_owner != worker_id
                or task.generation != generation
                or task.status != "RUNNING"
            ):
                return False
            task.lease_owner = None
            task.lease_until = None
            if error is None:
                task.status = "SUCCEEDED"
                task.result_json = _canonical_json(result or {})
                task.error_message = None
                task.next_attempt_at = None
            else:
                task.status = "MANUAL_REVIEW" if task.retry_attempts >= 3 else "RETRY"
                task.error_message = error[:500]
                task.next_attempt_at = (
                    None
                    if task.status == "MANUAL_REVIEW"
                    else _now() + timedelta(seconds=10 * (2 ** (task.attempts - 1)))
                )
            await session.flush()
            return True
    except SQLAlchemyError as exc:
        raise PersistenceError("完成任务失败") from exc


async def list_manual_tasks(session: AsyncSession, *, limit: int = 50) -> list[ActionTask]:
    try:
        async with session.begin():
            return list(
                (await session.scalars(
                    select(ActionTask)
                    .where(ActionTask.status == "MANUAL_REVIEW")
                    .order_by(ActionTask.created_at, ActionTask.id)
                    .limit(limit)
                )).all()
            )
    except SQLAlchemyError as exc:
        raise PersistenceError("读取待人工处理任务失败") from exc


async def inspect_manual_task(
    session: AsyncSession, *, task_id: str
) -> tuple[ActionTask, list[ActionTaskOperation]]:
    try:
        async with session.begin():
            task = await session.get(ActionTask, task_id)
            if task is None:
                raise ManualTaskNotFound("任务不存在")
            operations = list(
                (await session.scalars(
                    select(ActionTaskOperation)
                    .where(ActionTaskOperation.task_id == task_id)
                    .order_by(ActionTaskOperation.id)
                )).all()
            )
            return task, operations
    except SQLAlchemyError as exc:
        raise PersistenceError("读取人工处理任务失败") from exc


async def decide_manual_task(
    session: AsyncSession,
    *,
    task_id: str,
    operator_id: str,
    operation: str,
    reason: str,
) -> ActionTask:
    if operation not in {"RESUME", "CANCEL"}:
        raise ValueError("不支持的人工操作")
    reason = reason.strip()
    if not reason:
        raise ValueError("必须填写操作原因")
    try:
        async with session.begin():
            task = await session.scalar(
                select(ActionTask).where(ActionTask.id == task_id).with_for_update()
            )
            if task is None:
                raise ManualTaskNotFound("任务不存在")
            before = task.status
            if operation == "RESUME":
                if before != "MANUAL_REVIEW":
                    raise ManualTaskConflict("只有待人工处理任务可以恢复")
                task.status = "PENDING"
                task.retry_attempts = 0
                task.next_attempt_at = None
            else:
                if before not in {"PENDING", "RETRY", "RUNNING", "MANUAL_REVIEW"}:
                    raise ManualTaskConflict("当前任务状态不可终止")
                task.status = "CANCELLED"
                task.next_attempt_at = None
            attempts_before = task.attempts
            generation_before = task.generation
            task.generation += 1
            task.lease_owner = None
            task.lease_until = None
            session.add(
                ActionTaskOperation(
                    task_id=task.id,
                    operator_id=operator_id,
                    operation=operation,
                    before_status=before,
                    after_status=task.status,
                    reason=reason[:500],
                    attempts_before=attempts_before,
                    generation_before=generation_before,
                )
            )
            await session.flush()
            await session.refresh(task)
            return task
    except (ManualTaskNotFound, ManualTaskConflict):
        raise
    except SQLAlchemyError as exc:
        raise PersistenceError("保存人工处理决定失败") from exc
