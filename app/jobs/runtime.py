from __future__ import annotations

import logging
import os
from datetime import date


logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)

logger = logging.getLogger("app.jobs")


def resolve_target_date() -> date:
    raw = os.getenv("TARGET_DATE")
    if raw:
        return date.fromisoformat(raw)
    return date.today()
