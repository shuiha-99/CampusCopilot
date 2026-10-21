import asyncio
import json
import sys

from fastapi.testclient import TestClient
from redis.exceptions import RedisError
from sqlalchemy import delete, select

from app.cache.conversation_cache import build_history_key
from app.cache.redis_client import redis_client
from app.db.session import async_session_factory, engine
from app.models import ActionDraft, ActionTask, Conversation, ConversationRun
from main import app


if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")


USER_ID = "integration-http-user"


async def cleanup() -> None:
    async with async_session_factory() as session:
        async with session.begin():
            conversation_ids = list(
                (
                    await session.scalars(
                        select(Conversation.id).where(Conversation.user_id == USER_ID)
                    )
                ).all()
            )
            await session.execute(delete(ActionTask).where(ActionTask.user_id == USER_ID))
            await session.execute(delete(ActionDraft).where(ActionDraft.user_id == USER_ID))
            await session.execute(
                delete(ConversationRun).where(ConversationRun.user_id == USER_ID)
            )
            await session.execute(delete(Conversation).where(Conversation.user_id == USER_ID))
    for conversation_id in conversation_ids:
        try:
            await redis_client.delete(build_history_key(USER_ID, conversation_id))
        except RedisError:
            pass


async def cleanup_and_dispose() -> None:
    await cleanup()
    await engine.dispose()


def require_ok(response, label: str) -> dict:
    if response.status_code >= 300:
        raise AssertionError(f"{label} failed: {response.status_code} {response.text}")
    return response.json()


def main() -> None:
    asyncio.run(cleanup_and_dispose())
    try:
        with TestClient(app) as client:
            query = require_ok(
                client.post(
                    "/agent/query",
                    json={
                        "user_id": USER_ID,
                        "message": "请根据学校资料说明国家励志奖学金申请条件，并提醒我需要复核什么。",
                        "grade": "大二",
                        "college": "计算机工程学院",
                        "major": "软件工程",
                    },
                ),
                "agent query",
            )
            trace = require_ok(
                client.get(
                    f"/runs/{query['run_id']}", params={"user_id": USER_ID}
                ),
                "run trace",
            )
            draft = require_ok(
                client.post(
                    "/actions/drafts",
                    json={
                        "user_id": USER_ID,
                        "conversation_id": query["conversation_id"],
                        "run_id": query["run_id"],
                        "action_type": "scholarship_preparation",
                        "details": {
                            "grade": "大二",
                            "college": "计算机工程学院",
                            "scholarship_type": "国家励志奖学金",
                        },
                    },
                ),
                "action draft",
            )
            before_confirmation = require_ok(
                client.get(
                    f"/runs/{query['run_id']}", params={"user_id": USER_ID}
                ),
                "trace before confirmation",
            )
            if before_confirmation["tasks"]:
                raise AssertionError("草稿创建后、显式确认前已经存在动作任务")
            task = require_ok(
                client.post(
                    "/actions/confirm",
                    json={
                        "user_id": USER_ID,
                        "draft_id": draft["draft_id"],
                        "confirmation_token": draft["confirmation_token"],
                    },
                ),
                "MCP confirmation",
            )
            worker = require_ok(
                client.post("/actions/worker/run-once"), "worker run"
            )
            completed = require_ok(
                client.get(
                    f"/actions/tasks/{task['task_id']}",
                    params={"user_id": USER_ID},
                ),
                "task query",
            )
            final_trace = require_ok(
                client.get(
                    f"/runs/{query['run_id']}", params={"user_id": USER_ID}
                ),
                "completed run trace",
            )
            if (
                trace["status"] != "SUCCEEDED"
                or not trace["retrievals"]
                or not trace["model_attempts"]
            ):
                raise AssertionError("Trace 缺少成功状态、检索记录或模型调用记录")
            operations = [item["operation"] for item in trace["model_attempts"]]
            if "classification" not in operations or "answer" not in operations:
                raise AssertionError("Trace 缺少分类或回答的实际模型尝试")
            workflow = trace["blackboard"].get("agent_workflow", {})
            events = trace["blackboard_events"]
            if (
                workflow.get("status") != "SUCCEEDED"
                or workflow.get("steps_used") != 3
                or workflow.get("step_limit") != 3
                or list(workflow.get("tasks", {}).values()) != ["SUCCEEDED"] * 3
                or len(events) != 9
                or [item["sequence"] for item in events] != list(range(1, 10))
            ):
                raise AssertionError("三步Agent成果或Blackboard事件不完整")
            if not worker["processed"] or completed["status"] != "SUCCEEDED":
                raise AssertionError("异步任务没有完成")
            linked_tasks = [
                item for item in final_trace["tasks"] if item["task_id"] == task["task_id"]
            ]
            if (
                final_trace["run_id"] != query["run_id"]
                or len(linked_tasks) != 1
                or linked_tasks[0]["status"] != completed["status"]
                or linked_tasks[0]["attempts"] != completed["attempts"]
            ):
                raise AssertionError("Trace 未关联到已完成的动作任务")
            print(
                json.dumps(
                    {
                        "http_e2e": True,
                        "real_model_attempt": trace["model_attempts"][0]["provider"],
                        "citations": len(query["citations"]),
                        "conflict_detected": query["conflict_detected"],
                        "model_operations": operations,
                        "agent_steps": workflow["steps_used"],
                        "blackboard_events": len(events),
                        "task_before_confirmation": False,
                        "mcp_task_status": completed["status"],
                        "trace_task_linked": True,
                    },
                    ensure_ascii=False,
                )
            )
    finally:
        asyncio.run(cleanup_and_dispose())


if __name__ == "__main__":
    main()
