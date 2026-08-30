from collections import defaultdict
from uuid import UUID

from fastapi import WebSocket


class ConnectionManager:
    def __init__(self) -> None:
        self._connections: dict[UUID, set[WebSocket]] = defaultdict(set)

    async def connect(self, session_id: UUID, websocket: WebSocket) -> None:
        await websocket.accept()
        self._connections[session_id].add(websocket)

    def disconnect(self, session_id: UUID, websocket: WebSocket) -> None:
        connections = self._connections.get(session_id)
        if connections is None:
            return
        connections.discard(websocket)
        if not connections:
            self._connections.pop(session_id, None)

    async def broadcast(self, session_id: UUID, message: dict) -> None:
        dead: list[WebSocket] = []
        for connection in self._connections.get(session_id, set()).copy():
            try:
                await connection.send_json(message)
            except RuntimeError:
                dead.append(connection)
        for connection in dead:
            self.disconnect(session_id, connection)


connections = ConnectionManager()

