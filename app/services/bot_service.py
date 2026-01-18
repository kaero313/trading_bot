from datetime import datetime, timezone

from app.core.state import state


def start_bot():
    state.running = True
    state.last_heartbeat = datetime.now(timezone.utc)
    state.last_error = None
    return state.status()


def stop_bot():
    state.running = False
    state.last_heartbeat = datetime.now(timezone.utc)
    return state.status()
