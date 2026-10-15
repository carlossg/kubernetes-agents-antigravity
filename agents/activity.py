"""In-memory activity feed exposed at GET /a2a/events.

The quarkus-demo dashboard polls this endpoint and renders each event in its
"agent activity" panel. The event shape matches the dashboard's ActivityEvent
record: {id, timestamp, type, message, details}.
"""
import threading
from datetime import datetime, timezone

MAX_EVENTS = 200

# Types understood by the dashboard (see formatEventType in its app.js).
ANALYSIS_START = "ANALYSIS_START"
AGENT_START = "AGENT_START"
AGENT_COMPLETE = "AGENT_COMPLETE"
TOOL_CALL = "TOOL_CALL"
TOOL_RESULT = "TOOL_RESULT"
DECISION = "DECISION"
CONFIDENCE_SCORE = "CONFIDENCE_SCORE"
ANALYSIS_SUMMARY = "ANALYSIS_SUMMARY"
REMEDIATION = "REMEDIATION"
ERROR = "ERROR"


class ActivityFeed:
    def __init__(self, max_events: int = MAX_EVENTS):
        self._max_events = max_events
        self._events: list[dict] = []
        self._next_id = 1
        self._lock = threading.Lock()

    def emit(self, type: str, message: str, details: str = "") -> dict:
        with self._lock:
            event = {
                "id": self._next_id,
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "type": type,
                "message": message,
                "details": details,
            }
            self._next_id += 1
            self._events.append(event)
            del self._events[: -self._max_events]
            return event

    def events(self, since: int | None = None) -> list[dict]:
        with self._lock:
            if since:
                return [e for e in self._events if e["id"] > since]
            return list(self._events)


feed = ActivityFeed()
