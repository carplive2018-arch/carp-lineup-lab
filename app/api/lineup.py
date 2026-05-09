import os

from fastapi import APIRouter
from sqlalchemy import create_engine, text

router = APIRouter(tags=["lineup"])


def normalize_database_url(db_url: str) -> str:
    if db_url.startswith("postgres://"):
        return db_url.replace("postgres://", "postgresql+psycopg://", 1)
    if db_url.startswith("postgresql://"):
        return db_url.replace("postgresql://", "postgresql+psycopg://", 1)
    return db_url


def get_engine():
    db_url = os.getenv("DATABASE_URL")
    if not db_url:
        return None
    return create_engine(normalize_database_url(db_url), pool_pre_ping=True)


@router.get("/api/lineups/today")
def today_lineup():
    engine = get_engine()
    if engine is None:
        return {
            "status": "ok",
            "source": "fallback",
            "message": "DATABASE_URL not set",
            "lineup": []
        }

    with engine.connect() as conn:
        rows = conn.execute(
            text("""
                SELECT id, player_name
                FROM players
                ORDER BY id
                LIMIT 9
            """)
        ).fetchall()

    positions = ["CF", "2B", "SS", "C", "LF", "1B", "3B", "RF", "P"]

    lineup = []
    for i, row in enumerate(rows, start=1):
        lineup.append({
            "batting_order": i,
            "player_id": row.id,
            "player_name": row.player_name,
            "position": positions[i - 1] if i - 1 < len(positions) else None
        })

    return {
        "status": "ok",
        "source": "database",
        "count": len(lineup),
        "lineup": lineup
    }
