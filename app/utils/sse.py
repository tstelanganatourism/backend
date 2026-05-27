import asyncio
import json
import time
from typing import Dict, Set

class SSEManager:
    def __init__(self):
        # Maps package_id to a set of queues
        self.package_clients: Dict[str, Set[asyncio.Queue]] = {}
        # Maps room_id to a set of queues
        self.room_clients: Dict[str, Set[asyncio.Queue]] = {}

    def _get_clients(self, entity_type: str, entity_id: str) -> Set[asyncio.Queue]:
        if entity_type == "package":
            if entity_id not in self.package_clients:
                self.package_clients[entity_id] = set()
            return self.package_clients[entity_id]
        elif entity_type == "room":
            if entity_id not in self.room_clients:
                self.room_clients[entity_id] = set()
            return self.room_clients[entity_id]
        return set()

    def add_client(self, entity_type: str, entity_id: str, queue: asyncio.Queue):
        clients = self._get_clients(entity_type, entity_id)
        clients.add(queue)

    def remove_client(self, entity_type: str, entity_id: str, queue: asyncio.Queue):
        clients = self._get_clients(entity_type, entity_id)
        if queue in clients:
            clients.remove(queue)
            if not clients:
                if entity_type == "package":
                    del self.package_clients[entity_id]
                elif entity_type == "room":
                    del self.room_clients[entity_id]

    async def broadcast_event(self, entity_type: str, entity_id: str, event_name: str, payload: dict):
        """
        Emits a Server-Sent Event to all scoped connected clients.
        Example Payload MUST include version, timestamp, etc.
        """
        clients = self._get_clients(entity_type, str(entity_id))
        if not clients:
            return

        data_str = json.dumps(payload)
        sse_message = f"event: {event_name}\ndata: {data_str}\n\n"
        
        # Snapshot clients to avoid runtime modification errors
        for queue in list(clients):
            try:
                # Non-blocking put
                queue.put_nowait(sse_message)
            except asyncio.QueueFull:
                pass

sse_manager = SSEManager()
