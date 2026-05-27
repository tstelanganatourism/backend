import asyncio
from fastapi import APIRouter, Request
from fastapi.responses import StreamingResponse
from app.utils.sse import sse_manager

router = APIRouter(
    prefix="/stream",
    tags=["Realtime - SSE"]
)

@router.get("/packages/{package_id}")
async def stream_package(package_id: str, request: Request):
    """
    Server-Sent Events endpoint for a specific package.
    Clients connect here to receive live inventory and price updates.
    """
    queue = asyncio.Queue(maxsize=100)
    sse_manager.add_client("package", package_id, queue)

    async def event_generator():
        try:
            # Send initial connection success
            yield "event: connected\ndata: {\"status\": \"ok\"}\n\n"
            
            while True:
                # Wait for next event or ping every 25 seconds
                try:
                    # Timeout to send heartbeat
                    message = await asyncio.wait_for(queue.get(), timeout=25.0)
                    yield message
                except asyncio.TimeoutError:
                    # Send heartbeat ping
                    yield "event: ping\ndata: alive\n\n"
                    
                # If client disconnected, request is disconnected
                if await request.is_disconnected():
                    break
        except asyncio.CancelledError:
            pass
        finally:
            sse_manager.remove_client("package", package_id, queue)

    return StreamingResponse(event_generator(), media_type="text/event-stream")

@router.get("/rooms/{room_id}")
async def stream_room(room_id: str, request: Request):
    """
    Server-Sent Events endpoint for a specific room.
    """
    queue = asyncio.Queue(maxsize=100)
    sse_manager.add_client("room", room_id, queue)

    async def event_generator():
        try:
            yield "event: connected\ndata: {\"status\": \"ok\"}\n\n"
            
            while True:
                try:
                    message = await asyncio.wait_for(queue.get(), timeout=25.0)
                    yield message
                except asyncio.TimeoutError:
                    yield "event: ping\ndata: alive\n\n"
                    
                if await request.is_disconnected():
                    break
        except asyncio.CancelledError:
            pass
        finally:
            sse_manager.remove_client("room", room_id, queue)

    return StreamingResponse(event_generator(), media_type="text/event-stream")
