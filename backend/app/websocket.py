from fastapi import WebSocket, WebSocketDisconnect

from app.bot_manager import bot_manager


async def websocket_endpoint(websocket: WebSocket) -> None:
    await websocket.accept()
    queue = bot_manager.subscribe()
    try:
        while True:
            payload = await queue.get()
            await websocket.send_json(payload)
    except WebSocketDisconnect:
        pass
    finally:
        bot_manager.unsubscribe(queue)
