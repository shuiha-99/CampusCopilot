"""Exercise the bounded scholarship workflow through HTTP and real MySQL."""

import asyncio
import json
import uuid

from fastapi.testclient import TestClient
from sqlalchemy import delete

from app.db.session import async_session_factory
from app.models import Conversation, ConversationRun
from main import app


async def cleanup(user_id: str) -> None:
    async with async_session_factory() as session:
        async with session.begin():
            await session.execute(delete(ConversationRun).where(ConversationRun.user_id == user_id))
            await session.execute(delete(Conversation).where(Conversation.user_id == user_id))


def main() -> None:
    user_id = "workflow-check-" + uuid.uuid4().hex[:12]
    try:
        with TestClient(app) as client:
            response = client.post("/agent/query", json={
                "user_id": user_id,
                "message": "我是大二学生，想申请国家励志奖学金，需要准备什么？",
                "grade": "大二",
                "college": "计算机工程学院",
                "major": "软件工程",
            })
            response.raise_for_status()
            answer = response.json()
            trace_response = client.get(
                "/runs/" + answer["run_id"], params={"user_id": user_id}
            )
            trace_response.raise_for_status()
            trace = trace_response.json()
            foreign_trace = client.get(
                "/runs/" + answer["run_id"], params={"user_id": "another-user"}
            )
            assert foreign_trace.status_code == 404
        workflow = trace["blackboard"]["agent_workflow"]
        events = trace["blackboard_events"]
        assert workflow["status"] == "SUCCEEDED"
        assert workflow["steps_used"] == workflow["step_limit"] == 3
        assert len(events) == 9
        assert [event["sequence"] for event in events] == list(range(1, 10))
        assert list(workflow["tasks"].values()) == ["SUCCEEDED"] * 3
        assert answer["can_create_task"] == (
            workflow["artifacts"]["safety_review"]["allow_action"]
            and bool(answer["citations"])
        )
        print(json.dumps({
            "run_id": answer["run_id"],
            "status": trace["status"],
            "workflow_status": workflow["status"],
            "steps_used": workflow["steps_used"],
            "event_count": len(events),
            "agents": list(workflow["tasks"]),
            "citation_count": len(answer["citations"]),
            "can_create_task": answer["can_create_task"],
            "action_task_count": len(trace["tasks"]),
            "foreign_trace_status": foreign_trace.status_code,
        }, ensure_ascii=False))
    finally:
        asyncio.run(cleanup(user_id))


if __name__ == "__main__":
    main()
