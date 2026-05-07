from __future__ import annotations

import json
import os
from contextlib import contextmanager
from dataclasses import asdict, is_dataclass
from datetime import date, datetime
from typing import Any, Callable, Iterator

from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session, sessionmaker

from app.jobs.runtime import logger
from app.repositories.stats_repository import StatsRepository
from app.services.lineup_service import LineupService
from app.services.metric_service import MetricService


# ---------------------------------------------------------
# DB session helpers
# ---------------------------------------------------------


def _build_session_factory() -> sessionmaker:
    try:
        from app.core.database import SessionLocal  # type: ignore

        return SessionLocal
    except (ImportError, AttributeError):
        pass

    try:
        from app.core.database import engine  # type: ignore

        return sessionmaker(bind=engine, autoflush=False, autocommit=False, expire_on_commit=False)
    except (ImportError, AttributeError):
        pass

    try:
        from app.core.config import settings  # type: ignore

        database_url = settings.database_url
    except (ImportError, AttributeError) as exc:
        raise RuntimeError(
            "DB接続設定が見つかりません。app.core.database または app.core.config.settings を確認してください。"
        ) from exc

    engine = create_engine(database_url, pool_pre_ping=True, future=True)
    return sessionmaker(bind=engine, autoflush=False, autocommit=False, expire_on_commit=False)


SessionLocal = _build_session_factory()


@contextmanager
def session_scope() -> Iterator[Session]:
    db = SessionLocal()
    try:
        yield db
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


# ---------------------------------------------------------
# small utils
# ---------------------------------------------------------


def _today_or(value: date | None) -> date:
    return value or date.today()



def _utcnow() -> datetime:
    return datetime.utcnow()



def _normalize_result(value: Any) -> dict[str, Any]:
    if value is None:
        return {"status": "ok"}
    if isinstance(value, dict):
        return value
    if is_dataclass(value):
        return asdict(value)
    if hasattr(value, "model_dump"):
        return value.model_dump()
    if hasattr(value, "dict"):
        return value.dict()
    return {"value": value}



def _model_dump_any(value: Any) -> dict[str, Any]:
    if hasattr(value, "model_dump"):
        return value.model_dump()
    if hasattr(value, "dict"):
        return value.dict()
    if isinstance(value, dict):
        return value
    raise TypeError(f"model_dumpできない型です: {type(value)}")



def _safe_get(obj: Any, *names: str, default: Any = None) -> Any:
    for name in names:
        if isinstance(obj, dict) and name in obj:
            return obj[name]
        if hasattr(obj, name):
            return getattr(obj, name)
    return default



def _json_dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))



def _round6(value: float | int | None) -> float:
    return round(float(value or 0.0), 6)



def _invoke_with_supported_kwargs(func: Callable[..., Any], **kwargs: Any) -> Any:
    try:
        return func(**kwargs)
    except TypeError:
        return func()


# ---------------------------------------------------------
# fetch job
# ---------------------------------------------------------


def fetch_daily_data(target_date: date | None = None) -> dict[str, Any]:
    target_date = _today_or(target_date)
    logger.info("fetch_daily_data: started target_date=%s", target_date)

    with session_scope() as db:
        repository = StatsRepository(db=db, use_mock_if_empty=False)

        execution_plan: list[tuple[str, tuple[str, ...]]] = [
            (
                "daily_bundle",
                (
                    "sync_daily_data",
                    "fetch_and_store_daily_data",
                    "ingest_daily_source_pages",
                ),
            ),
            ("games", ("sync_games_for_date", "fetch_games_for_date")),
            ("lineups", ("sync_game_lineups_for_date", "fetch_game_lineups_for_date")),
            ("batting", ("sync_batting_stats_for_date", "fetch_batting_stats_for_date")),
            ("fielding", ("sync_fielding_stats_for_date", "fetch_fielding_stats_for_date")),
        ]

        summary: dict[str, Any] = {
            "target_date": target_date.isoformat(),
            "steps": {},
            "status": "ok",
        }

        executed = 0

        for step_name, method_names in execution_plan:
            method = None
            for method_name in method_names:
                if hasattr(repository, method_name):
                    method = getattr(repository, method_name)
                    break

            if method is None:
                continue

            result = _invoke_with_supported_kwargs(method, target_date=target_date)
            summary["steps"][step_name] = _normalize_result(result)
            executed += 1
            logger.info("fetch_daily_data: executed step=%s", step_name)

        if executed == 0:
            raise RuntimeError(
                "StatsRepository に日次取得メソッドが未接続です。少なくとも sync_daily_data か sync_games_for_date 系を実装してください。"
            )

        logger.info("fetch_daily_data: finished target_date=%s", target_date)
        return summary


# ---------------------------------------------------------
# metric snapshot build
# ---------------------------------------------------------


def build_metric_snapshots(target_date: date) -> dict[str, Any]:
    logger.info("build_metric_snapshots: started target_date=%s", target_date)

    metric_service = MetricService()

    with session_scope() as db:
        repository = StatsRepository(db=db, use_mock_if_empty=False)
        rows = repository.get_player_inputs_for_lineup(opponent_pitcher_hand=None)

        if not rows:
            raise RuntimeError("build_metric_snapshots: 選手入力が0件です。")

        snapshot_count = 0

        for row in rows:
            snapshot = metric_service.build_player_snapshot(
                player_id=row["player_id"],
                player_name=row["player_name"],
                last_5=row["last_5"],
                last_10=row["last_10"],
                last_15=row["last_15"],
                season=row["season"],
                defense_score=row.get("defense_score", 0.0),
                position_scarcity=row.get("position_scarcity", 0.0),
                recent_playing_time=row.get("recent_playing_time", 0.0),
                opponent_handedness_bonus=0.0,
            )

            eligible_positions = row.get("eligible_positions", [])
            defense_by_position = row.get("defense_by_position", {})

            for model_type in ("best_5", "best_10", "best_15", "best_mix"):
                metric_pack = _extract_metric_pack(snapshot, model_type)

                batting_score = metric_service.build_batting_score(
                    woba=metric_pack["woba"],
                    obp=metric_pack["obp"],
                    iso=metric_pack["iso"],
                    bb_rate=metric_pack["bb_rate"],
                    k_rate=metric_pack["k_rate"],
                    speed_score=metric_pack["speed_score"],
                    gidp_avoidance=metric_pack["gidp_avoidance"],
                )
                starter_score = metric_service.build_starter_score(
                    batting_score=batting_score,
                    defense_score=_safe_get(snapshot, "defense_score", default=0.0),
                    position_scarcity=_safe_get(snapshot, "position_scarcity", default=0.0),
                    recent_playing_time=_safe_get(snapshot, "recent_playing_time", default=0.0),
                    opponent_handedness_bonus=0.0,
                )

                _upsert_metric_snapshot(
                    db=db,
                    target_date=target_date,
                    player_id=row["player_id"],
                    player_name=row["player_name"],
                    model_type=model_type,
                    metric_pack=metric_pack,
                    batting_score=batting_score,
                    starter_score=starter_score,
                    defense_score=row.get("defense_score", 0.0),
                    position_scarcity=row.get("position_scarcity", 0.0),
                    recent_playing_time=row.get("recent_playing_time", 0.0),
                    opponent_handedness_bonus=0.0,
                    eligible_positions=eligible_positions,
                    defense_by_position=defense_by_position,
                )
                snapshot_count += 1

        logger.info("build_metric_snapshots: finished target_date=%s rows=%s", target_date, snapshot_count)
        return {
            "status": "ok",
            "target_date": target_date.isoformat(),
            "players": len(rows),
            "snapshot_rows": snapshot_count,
        }



def _extract_metric_pack(snapshot: Any, model_type: str) -> dict[str, float]:
    if model_type == "best_mix":
        return {
            "pa": _round6(_safe_get(snapshot, "mixed_pa", default=0.0)),
            "avg": _round6(_safe_get(snapshot, "mixed_avg", default=0.0)),
            "obp": _round6(_safe_get(snapshot, "mixed_obp", default=0.0)),
            "slg": _round6(_safe_get(snapshot, "mixed_slg", default=0.0)),
            "iso": _round6(_safe_get(snapshot, "mixed_iso", default=0.0)),
            "bb_rate": _round6(_safe_get(snapshot, "mixed_bb_rate", default=0.0)),
            "k_rate": _round6(_safe_get(snapshot, "mixed_k_rate", default=0.0)),
            "woba": _round6(_safe_get(snapshot, "mixed_woba", "mixed_woba_lite", default=0.0)),
            "speed_score": _round6(_safe_get(snapshot, "mixed_speed_score", default=0.0)),
            "gidp_avoidance": _round6(_safe_get(snapshot, "mixed_gidp_avoidance", default=1.0)),
        }

    if model_type == "best_5":
        window = _safe_get(snapshot, "adjusted_5", "window_5_adjusted", "last_5_adjusted", "adjusted_window_5")
    elif model_type == "best_10":
        window = _safe_get(snapshot, "adjusted_10", "window_10_adjusted", "last_10_adjusted", "adjusted_window_10")
    elif model_type == "best_15":
        window = _safe_get(snapshot, "adjusted_15", "window_15_adjusted", "last_15_adjusted", "adjusted_window_15")
    else:
        raise ValueError(f"未知の model_type: {model_type}")

    if window is None:
        raise RuntimeError(
            f"{model_type} の adjusted window が snapshot にありません。MetricService.build_player_snapshot の返り値属性名を確認してください。"
        )

    return {
        "pa": _round6(_safe_get(window, "pa", default=0.0)),
        "avg": _round6(_safe_get(window, "avg", default=0.0)),
        "obp": _round6(_safe_get(window, "obp", default=0.0)),
        "slg": _round6(_safe_get(window, "slg", default=0.0)),
        "iso": _round6(_safe_get(window, "iso", default=0.0)),
        "bb_rate": _round6(_safe_get(window, "bb_rate", default=0.0)),
        "k_rate": _round6(_safe_get(window, "k_rate", default=0.0)),
        "woba": _round6(_safe_get(window, "woba", "woba_lite", default=0.0)),
        "speed_score": _round6(_safe_get(window, "speed_score", default=0.0)),
        "gidp_avoidance": _round6(_safe_get(window, "gidp_avoidance", default=1.0)),
    }



def _upsert_metric_snapshot(
    db: Session,
    target_date: date,
    player_id: int,
    player_name: str,
    model_type: str,
    metric_pack: dict[str, float],
    batting_score: float,
    starter_score: float,
    defense_score: float,
    position_scarcity: float,
    recent_playing_time: float,
    opponent_handedness_bonus: float,
    eligible_positions: list[str],
    defense_by_position: dict[str, float],
) -> None:
    sql = text(
        """
        INSERT INTO player_metric_snapshots (
            target_date,
            player_id,
            player_name,
            model_type,
            pa,
            avg,
            obp,
            slg,
            iso,
            bb_rate,
            k_rate,
            woba_lite,
            speed_score,
            gidp_avoidance,
            batting_score,
            starter_score,
            defense_score,
            position_scarcity,
            recent_playing_time,
            opponent_handedness_bonus,
            eligible_positions_json,
            defense_by_position_json,
            created_at,
            updated_at
        ) VALUES (
            :target_date,
            :player_id,
            :player_name,
            :model_type,
            :pa,
            :avg,
            :obp,
            :slg,
            :iso,
            :bb_rate,
            :k_rate,
            :woba_lite,
            :speed_score,
            :gidp_avoidance,
            :batting_score,
            :starter_score,
            :defense_score,
            :position_scarcity,
            :recent_playing_time,
            :opponent_handedness_bonus,
            CAST(:eligible_positions_json AS JSONB),
            CAST(:defense_by_position_json AS JSONB),
            :created_at,
            :updated_at
        )
        ON CONFLICT (target_date, player_id, model_type)
        DO UPDATE SET
            player_name = EXCLUDED.player_name,
            pa = EXCLUDED.pa,
            avg = EXCLUDED.avg,
            obp = EXCLUDED.obp,
            slg = EXCLUDED.slg,
            iso = EXCLUDED.iso,
            bb_rate = EXCLUDED.bb_rate,
            k_rate = EXCLUDED.k_rate,
            woba_lite = EXCLUDED.woba_lite,
            speed_score = EXCLUDED.speed_score,
            gidp_avoidance = EXCLUDED.gidp_avoidance,
            batting_score = EXCLUDED.batting_score,
            starter_score = EXCLUDED.starter_score,
            defense_score = EXCLUDED.defense_score,
            position_scarcity = EXCLUDED.position_scarcity,
            recent_playing_time = EXCLUDED.recent_playing_time,
            opponent_handedness_bonus = EXCLUDED.opponent_handedness_bonus,
            eligible_positions_json = EXCLUDED.eligible_positions_json,
            defense_by_position_json = EXCLUDED.defense_by_position_json,
            updated_at = EXCLUDED.updated_at
        """
    )

    now = _utcnow()

    db.execute(
        sql,
        {
            "target_date": target_date,
            "player_id": player_id,
            "player_name": player_name,
            "model_type": model_type,
            "pa": metric_pack["pa"],
            "avg": metric_pack["avg"],
            "obp": metric_pack["obp"],
            "slg": metric_pack["slg"],
            "iso": metric_pack["iso"],
            "bb_rate": metric_pack["bb_rate"],
            "k_rate": metric_pack["k_rate"],
            "woba_lite": metric_pack["woba"],
            "speed_score": metric_pack["speed_score"],
            "gidp_avoidance": metric_pack["gidp_avoidance"],
            "batting_score": batting_score,
            "starter_score": starter_score,
            "defense_score": _round6(defense_score),
            "position_scarcity": _round6(position_scarcity),
            "recent_playing_time": _round6(recent_playing_time),
            "opponent_handedness_bonus": _round6(opponent_handedness_bonus),
            "eligible_positions_json": _json_dumps(eligible_positions),
            "defense_by_position_json": _json_dumps(defense_by_position),
            "created_at": now,
            "updated_at": now,
        },
    )


# ---------------------------------------------------------
# lineup generation + save
# ---------------------------------------------------------


def generate_today_lineup(target_date: date | None = None) -> dict[str, Any]:
    target_date = _today_or(target_date)
    logger.info("generate_today_lineup: started target_date=%s", target_date)

    use_mock_if_empty = os.getenv("USE_MOCK_IF_EMPTY", "false").lower() == "true"

    with session_scope() as db:
        repository = StatsRepository(db=db, use_mock_if_empty=use_mock_if_empty)
        service = LineupService(repository=repository)
        response = service.get_today_lineup(target_date)
        payload = _normalize_today_lineup_payload(response)

        saved_models = 0

        for model_row in payload["models"]:
            prediction_id = _upsert_lineup_prediction(
                db=db,
                target_date=target_date,
                model_type=model_row["model_type"],
                opponent_team_code=payload["opponent"]["team_code"],
                opponent_pitcher_hand=payload["opponent"]["pitcher_hand"],
                expected_runs=model_row["expected_runs"],
                confidence=payload["confidence"],
                summary=payload["summary"] if model_row["model_type"] == payload["best_model"] else f"{model_row['model_type']} の候補打線",
                lineup_json=model_row,
            )

            _replace_lineup_prediction_players(db=db, prediction_id=prediction_id, lineup=model_row["lineup"])
            saved_models += 1

        logger.info(
            "generate_today_lineup: finished target_date=%s saved_models=%s best_model=%s",
            target_date,
            saved_models,
            payload["best_model"],
        )

        return {
            "status": "ok",
            "target_date": target_date.isoformat(),
            "best_model": payload["best_model"],
            "saved_models": saved_models,
            "expected_runs": payload["expected_runs"],
        }



def _normalize_today_lineup_payload(response: Any) -> dict[str, Any]:
    raw = _model_dump_any(response)
    target_date = raw.get("targetDate") or raw.get("target_date")
    best_model = raw.get("bestModel") or raw.get("best_model")
    expected_runs = raw.get("expectedRuns") or raw.get("expected_runs") or 0.0
    confidence = raw.get("confidence") or 0.0
    summary = raw.get("summary") or ""

    raw_opponent = raw.get("opponent") or {}
    opponent = {
        "team_code": raw_opponent.get("teamCode") or raw_opponent.get("team_code") or "UNK",
        "pitcher_hand": raw_opponent.get("pitcherHand") or raw_opponent.get("pitcher_hand") or "R",
    }

    raw_models = raw.get("models") or []
    models: list[dict[str, Any]] = []

    for model in raw_models:
        model_dict = _model_dump_any(model)
        lineup_rows = []
        for player in model_dict.get("lineup", []):
            player_dict = _model_dump_any(player)
            lineup_rows.append(
                {
                    "order": player_dict.get("order"),
                    "player_id": player_dict.get("playerId") or player_dict.get("player_id"),
                    "name": player_dict.get("name"),
                    "position": player_dict.get("position"),
                    "starter_score": player_dict.get("starterScore") or player_dict.get("starter_score") or 0.0,
                    "slot_fit_score": player_dict.get("slotFitScore") or player_dict.get("slot_fit_score") or 0.0,
                    "reason": player_dict.get("reason") or "",
                }
            )

        models.append(
            {
                "model_type": model_dict.get("modelType") or model_dict.get("model_type"),
                "expected_runs": model_dict.get("expectedRuns") or model_dict.get("expected_runs") or 0.0,
                "lineup": lineup_rows,
            }
        )

    return {
        "target_date": target_date,
        "best_model": best_model,
        "expected_runs": expected_runs,
        "confidence": confidence,
        "summary": summary,
        "opponent": opponent,
        "models": models,
    }



def _upsert_lineup_prediction(
    db: Session,
    target_date: date,
    model_type: str,
    opponent_team_code: str,
    opponent_pitcher_hand: str,
    expected_runs: float,
    confidence: float,
    summary: str,
    lineup_json: dict[str, Any],
) -> int:
    now = _utcnow()

    upsert_sql = text(
        """
        INSERT INTO lineup_predictions (
            target_date,
            model_type,
            opponent_team_code,
            opponent_pitcher_hand,
            expected_runs,
            confidence,
            summary,
            lineup_json,
            created_at,
            updated_at
        ) VALUES (
            :target_date,
            :model_type,
            :opponent_team_code,
            :opponent_pitcher_hand,
            :expected_runs,
            :confidence,
            :summary,
            CAST(:lineup_json AS JSONB),
            :created_at,
            :updated_at
        )
        ON CONFLICT (target_date, model_type)
        DO UPDATE SET
            opponent_team_code = EXCLUDED.opponent_team_code,
            opponent_pitcher_hand = EXCLUDED.opponent_pitcher_hand,
            expected_runs = EXCLUDED.expected_runs,
            confidence = EXCLUDED.confidence,
            summary = EXCLUDED.summary,
            lineup_json = EXCLUDED.lineup_json,
            updated_at = EXCLUDED.updated_at
        """
    )

    db.execute(
        upsert_sql,
        {
            "target_date": target_date,
            "model_type": model_type,
            "opponent_team_code": opponent_team_code,
            "opponent_pitcher_hand": opponent_pitcher_hand,
            "expected_runs": _round6(expected_runs),
            "confidence": _round6(confidence),
            "summary": summary,
            "lineup_json": _json_dumps(lineup_json),
            "created_at": now,
            "updated_at": now,
        },
    )

    select_sql = text(
        """
        SELECT id
        FROM lineup_predictions
        WHERE target_date = :target_date
          AND model_type = :model_type
        LIMIT 1
        """
    )

    prediction_id = db.execute(select_sql, {"target_date": target_date, "model_type": model_type}).scalar_one()
    return int(prediction_id)



def _replace_lineup_prediction_players(db: Session, prediction_id: int, lineup: list[dict[str, Any]]) -> None:
    db.execute(text("DELETE FROM lineup_prediction_players WHERE prediction_id = :prediction_id"), {"prediction_id": prediction_id})

    insert_sql = text(
        """
        INSERT INTO lineup_prediction_players (
            prediction_id,
            batting_order,
            player_id,
            player_name,
            position,
            starter_score,
            slot_fit_score,
            reason,
            created_at,
            updated_at
        ) VALUES (
            :prediction_id,
            :batting_order,
            :player_id,
            :player_name,
            :position,
            :starter_score,
            :slot_fit_score,
            :reason,
            :created_at,
            :updated_at
        )
        """
    )

    now = _utcnow()

    for row in lineup:
        db.execute(
            insert_sql,
            {
                "prediction_id": prediction_id,
                "batting_order": row["order"],
                "player_id": row["player_id"],
                "player_name": row["name"],
                "position": row["position"],
                "starter_score": _round6(row["starter_score"]),
                "slot_fit_score": _round6(row["slot_fit_score"]),
                "reason": row["reason"],
                "created_at": now,
                "updated_at": now,
            },
        )
