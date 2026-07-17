from fastapi import FastAPI, WebSocket
from fastapi.middleware.cors import CORSMiddleware

from app.api.routes import router
from app.db import init_db
from app.websocket import websocket_endpoint

app = FastAPI(title="Forex AI Trading Bot")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173", "http://127.0.0.1:5173"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(router)


@app.on_event("startup")
def on_startup() -> None:
    init_db()


@app.websocket("/ws")
async def ws_route(websocket: WebSocket) -> None:
    await websocket_endpoint(websocket)
