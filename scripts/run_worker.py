import argparse
import asyncio
import uuid

from app.db.session import async_session_factory
from app.services.action_service import (
    build_preparation_result,
    finish_task,
    lease_one_task,
)


async def run_once(worker_id: str) -> bool:
    async with async_session_factory() as session:
        leased = await lease_one_task(session, worker_id=worker_id)
    if leased is None:
        return False
    task, generation = leased
    try:
        result = build_preparation_result(task)
        error = None
    except Exception as exc:
        result = None
        error = f"{type(exc).__name__}: {exc}"
    async with async_session_factory() as session:
        return await finish_task(
            session,
            task_id=task.id,
            worker_id=worker_id,
            generation=generation,
            result=result,
            error=error,
        )


async def main(continuous: bool) -> None:
    worker_id = "worker-" + uuid.uuid4().hex[:8]
    while True:
        processed = await run_once(worker_id)
        if not continuous:
            print("processed" if processed else "idle")
            return
        if not processed:
            await asyncio.sleep(2)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--continuous", action="store_true")
    args = parser.parse_args()
    asyncio.run(main(args.continuous))
