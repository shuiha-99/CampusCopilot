from mcp import Client
from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError

from app.db.session import async_session_factory
from app.services.action_service import ConfirmationError, create_task_idempotently


action_mcp_server = MCPServer(
    "campus-copilot-actions",
    description="只创建项目内部校园办事准备任务，不连接真实校务系统。",
)


@action_mcp_server.tool(
    name="create_preparation_task",
    description="在显式确认后创建一个可恢复、幂等的内部准备清单任务。",
)
async def create_preparation_task(
    draft_id: str,
    run_id: str | None,
    user_id: str,
    conversation_id: int | None,
    action_type: str,
    payload_json: str,
    idempotency_key: str,
) -> dict[str, str]:
    async with async_session_factory() as session:
        try:
            task = await create_task_idempotently(
                session,
                draft_id=draft_id,
                run_id=run_id,
                user_id=user_id,
                conversation_id=conversation_id,
                action_type=action_type,
                payload_json=payload_json,
                idempotency_key=idempotency_key,
            )
        except ConfirmationError as exc:
            raise ToolError(str(exc)) from exc
    return {"task_id": task.id, "status": task.status}


async def invoke_create_preparation_task(**arguments) -> dict[str, str]:
    """真实经过 MCP Client/Server 工具协议，而不是直接调用普通函数。"""
    async with Client(action_mcp_server, raise_exceptions=True) as client:
        result = await client.call_tool("create_preparation_task", arguments)
    if result.is_error:
        raise RuntimeError("MCP 工具创建任务失败")
    if result.structured_content:
        return dict(result.structured_content)
    raise RuntimeError("MCP 工具没有返回结构化任务结果")
