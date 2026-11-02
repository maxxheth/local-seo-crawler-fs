"""Thread-safe FlareSolverr SessionSlot manager pinned to proxy ports."""

from __future__ import annotations

from dataclasses import dataclass
import threading


@dataclass
class SessionSlot:
    """Represents an isolated proxy slot pinned to a FlareSolverr session."""

    session_id: str
    proxy_url: str
    proxy_port: int
    in_use: bool = False
    active_requests: int = 0
    error_count: int = 0


class SessionPool:
    """Thread-safe pool of SessionSlot instances managed under lock synchronization."""

    def __init__(self, flaresolverr_url: str) -> None:
        self.slots: list[SessionSlot] = []
        self._lock = threading.Lock()
        self.flaresolverr_url = flaresolverr_url

    def add_slot(
        self,
        session_id: str,
        proxy_url: str,
        proxy_port: int,
        worker_id: int = 1,
    ) -> None:
        """Register a new slot in the pool."""
        with self._lock:
            self.slots.append(SessionSlot(session_id, proxy_url, proxy_port))

    def acquire_slot(self, worker_id: int = 1) -> int:
        """Acquire an available session slot. Returns index or -1 if all are busy."""
        with self._lock:
            for i, slot in enumerate(self.slots):
                if not slot.in_use:
                    slot.in_use = True
                    slot.active_requests += 1
                    return i
            return -1

    def release_slot(self, worker_id: int, slot_idx: int) -> None:
        """Release a session slot back to the pool."""
        with self._lock:
            if 0 <= slot_idx < len(self.slots):
                self.slots[slot_idx].in_use = False

    def record_error(self, worker_id: int, slot_idx: int) -> None:
        """Increment error count for the specified slot."""
        with self._lock:
            if 0 <= slot_idx < len(self.slots):
                self.slots[slot_idx].error_count += 1

    def update_session(self, worker_id: int, slot_idx: int, new_session_id: str) -> None:
        """Update session_id after recycling."""
        with self._lock:
            if 0 <= slot_idx < len(self.slots):
                self.slots[slot_idx].session_id = new_session_id

    def get_session_id(self, slot_idx: int, worker_id: int = 1) -> str:
        with self._lock:
            if 0 <= slot_idx < len(self.slots):
                return self.slots[slot_idx].session_id
            return ""

    def get_proxy_url(self, slot_idx: int, worker_id: int = 1) -> str:
        with self._lock:
            if 0 <= slot_idx < len(self.slots):
                return self.slots[slot_idx].proxy_url
            return ""

    def get_proxy_port(self, slot_idx: int, worker_id: int = 1) -> int:
        with self._lock:
            if 0 <= slot_idx < len(self.slots):
                return self.slots[slot_idx].proxy_port
            return 0

    def get_error_count(self, slot_idx: int, worker_id: int = 1) -> int:
        with self._lock:
            if 0 <= slot_idx < len(self.slots):
                return self.slots[slot_idx].error_count
            return 0

    def size(self, worker_id: int = 1) -> int:
        with self._lock:
            return len(self.slots)
