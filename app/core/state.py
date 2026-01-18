from dataclasses import dataclass, field
from datetime import datetime, timezone

from app.models.schemas import BotConfig, BotStatus


@dataclass
class RuntimeState:
    running: bool = False
    last_heartbeat: datetime | None = None
    last_error: str | None = None
    config: BotConfig = field(default_factory=BotConfig)

    def status(self) -> BotStatus:
        return BotStatus(
            running=self.running,
            last_heartbeat=self.last_heartbeat.isoformat() if self.last_heartbeat else None,
            last_error=self.last_error,
        )


state = RuntimeState()
