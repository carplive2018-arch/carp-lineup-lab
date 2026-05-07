from __future__ import annotations

from app.jobs.runtime import logger, resolve_target_date



def main() -> int:
    target_date = resolve_target_date()
    logger.info("build_metric_snapshots started for %s", target_date)
    try:
        from app.jobs.tasks import build_metric_snapshots  # type: ignore
    except ImportError as exc:
        logger.error(
            "app.jobs.tasks.build_metric_snapshots が未実装です。集計処理を追加してください: %s",
            exc,
        )
        return 1

    build_metric_snapshots(target_date=target_date)
    logger.info("build_metric_snapshots finished for %s", target_date)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
