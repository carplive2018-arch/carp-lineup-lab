from __future__ import annotations

import json

from app.jobs.runtime import logger, resolve_target_date
from app.services.lineup_service import LineupService



def main() -> int:
    target_date = resolve_target_date()
    logger.info("generate_today_lineup started for %s", target_date)

    service = LineupService()
    payload = service.get_today_lineup(target_date)
    print(json.dumps(payload.model_dump(), ensure_ascii=False, indent=2))

    logger.info("generate_today_lineup finished for %s", target_date)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
