from __future__ import annotations

import sys

from app.jobs.runtime import logger



def main() -> int:
    logger.info("fetch_daily_data started")
    try:
        from app.jobs.tasks import fetch_daily_data  # type: ignore
    except ImportError as exc:
        logger.error(
            "app.jobs.tasks.fetch_daily_data が未実装です。実データ取得処理を追加してください: %s",
            exc,
        )
        return 1

    fetch_daily_data()
    logger.info("fetch_daily_data finished")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
