"""A fake Plow chat API — the ticket, messages and socket endpoints the
plow-chat server uses, served over real HTTP and WebSocket by uvicorn."""
import asyncio
import itertools
import socket
from collections import defaultdict

import uvicorn
from starlette.applications import Starlette
from starlette.responses import JSONResponse
from starlette.routing import Route, WebSocketRoute
from starlette.websockets import WebSocketDisconnect

TOKEN = "agt_fake-token-xyz"


class FakePlow:
    def __init__(self):
        self.history = defaultdict(list)  # chat -> messages, oldest first
        self.sent = []  # (chat, body) the agent posted
        self.sockets = []
        self._uids = itertools.count(1)
        self.app = Starlette(routes=[
            Route("/v1/ws/ticket", self._ticket, methods=["POST"]),
            Route("/v1/chats/{chat}/messages", self._messages, methods=["GET", "POST"]),
            WebSocketRoute("/v1/ws", self._ws),
        ])

    def _message(self, chat, direction, body, handle=None):
        sender = {"type": "member", "provider_key": handle} if handle else {"type": "agent"}
        message = {"uid": f"msg_{next(self._uids)}", "chat_uid": chat, "direction": direction,
                   "body": body, "sender": sender}
        self.history[chat].append(message)
        return message

    async def inbound(self, chat, handle, body, live=True):
        """A member texts the line; `live=False` lands it while no socket hears it."""
        message = self._message(chat, "inbound", body, handle)
        if live:
            await self._broadcast(message)
        return message

    async def _broadcast(self, message):
        for ws in list(self.sockets):
            await ws.send_json({"event_type": "message_received", "chat_id": message["chat_uid"],
                                "data": {"type": "message_received", "message": message}})

    async def drop_sockets(self):
        for ws in list(self.sockets):
            await ws.close()
        self.sockets.clear()

    def _authorized(self, request):
        return request.headers.get("authorization") == f"Bearer {TOKEN}"

    async def _ticket(self, request):
        if not self._authorized(request):
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        return JSONResponse({"ticket": f"wst_{next(self._uids)}"}, status_code=201)

    async def _messages(self, request):
        if not self._authorized(request):
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        chat = request.path_params["chat"]
        if request.method == "POST":
            body = (await request.json())["body"]
            self.sent.append((chat, body))
            message = self._message(chat, "outbound", body)
            await self._broadcast(message)  # Plow echoes the agent's own sends
            return JSONResponse(message, status_code=201)
        limit = int(request.query_params.get("limit", 20))
        return JSONResponse({"data": self.history[chat][::-1][:limit], "has_more": False})

    async def _ws(self, websocket):
        await websocket.accept()
        await websocket.send_json({"type": "connected"})
        self.sockets.append(websocket)
        try:
            while True:
                await websocket.receive_text()
        except WebSocketDisconnect:
            pass

    async def serve(self):
        """Start on a free localhost port; returns (base_url, uvicorn server)."""
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            port = s.getsockname()[1]
        server = uvicorn.Server(uvicorn.Config(self.app, host="127.0.0.1", port=port, log_level="warning", ws="websockets-sansio"))
        asyncio.create_task(server.serve())
        while not server.started:
            await asyncio.sleep(0.01)
        return f"http://127.0.0.1:{port}", server
