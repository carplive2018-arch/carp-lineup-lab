import os

from fastapi import APIRouter, HTTPException, Query
from sqlalchemy import create_engine, text

router = APIRouter()


def normalize_database_url(db_url: str) -> str:
    if db_url.startswith("postgres://"):
        return db_url.replace("postgres://", "postgresql+psycopg://", 1)
    if db_url.startswith("postgresql://"):
        return db_url.replace("postgresql://", "postgresql+psycopg://", 1)
    return db_url


def get_engine():
    db_url = os.getenv("DATABASE_URL")
    if not db_url:
        raise HTTPException(status_code=500, detail="DATABASE_URL not set")
    return create_engine(normalize_database_url(db_url), pool_pre_ping=True)


def check_token(token: str):
    expected = os.getenv("ADMIN_TOKEN")
    if not expected:
        raise HTTPException(status_code=500, detail="ADMIN_TOKEN not set")
    if token != expected:
        raise HTTPException(status_code=401, detail="unauthorized")


@router.get("/api/admin/init_db")
def init_db(token: str = Query(...)):
    check_token(token)
    engine = get_engine()

    statements = [
        """
        CREATE TABLE IF NOT EXISTS players (
            id SERIAL PRIMARY KEY,
            team_code TEXT NOT NULL DEFAULT 'C',
            player_name TEXT NOT NULL,
            bats TEXT,
            throws_hand TEXT,
            eligible_positions TEXT,
            created_at TIMESTAMP DEFAULT NOW(),
            UNIQUE(team_code, player_name)
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS games (
            id SERIAL PRIMARY KEY,
            game_date DATE NOT NULL,
            home_team TEXT NOT NULL,
            away_team TEXT NOT NULL,
            created_at TIMESTAMP DEFAULT NOW(),
            UNIQUE(game_date, home_team, away_team)
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS player_game_batting_stats (
            id SERIAL PRIMARY KEY,
            game_id INTEGER REFERENCES games(id) ON DELETE CASCADE,
            player_id INTEGER REFERENCES players(id) ON DELETE CASCADE,
            pa INTEGER DEFAULT 0,
            ab INTEGER DEFAULT 0,
            hits INTEGER DEFAULT 0,
            doubles INTEGER DEFAULT 0,
            triples INTEGER DEFAULT 0,
            homeruns INTEGER DEFAULT 0,
            walks INTEGER DEFAULT 0,
            strikeouts INTEGER DEFAULT 0,
            rbi INTEGER DEFAULT 0,
            created_at TIMESTAMP DEFAULT NOW(),
            UNIQUE(game_id, player_id)
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS lineup_predictions (
            id SERIAL PRIMARY KEY,
            target_date DATE NOT NULL,
            model_type TEXT NOT NULL,
            expected_runs NUMERIC(5,2),
            summary TEXT,
            created_at TIMESTAMP DEFAULT NOW(),
            UNIQUE(target_date, model_type)
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS lineup_prediction_players (
            id SERIAL PRIMARY KEY,
            prediction_id INTEGER REFERENCES lineup_predictions(id) ON DELETE CASCADE,
            batting_order INTEGER NOT NULL,
            player_id INTEGER REFERENCES players(id) ON DELETE SET NULL,
            player_name TEXT NOT NULL,
            position TEXT,
            reason TEXT,
            created_at TIMESTAMP DEFAULT NOW(),
            UNIQUE(prediction_id, batting_order)
        )
        """,
    ]

    with engine.begin() as conn:
        for stmt in statements:
            conn.execute(text(stmt))

    return {"status": "ok", "message": "tables created"}


@router.get("/api/admin/seed_players")
def seed_players(token: str = Query(...)):
    check_token(token)
    engine = get_engine()

    sample_players = [
        "秋山 翔吾",
        "菊池 涼介",
        "小園 海斗",
        "坂倉 将吾",
        "末包 昇大",
        "野間 峻祥",
        "矢野 雅哉",
        "堂林 翔太",
        "田村 俊介",
        "會澤 翼",
        "上本 崇司",
        "大盛 穂",
    ]

    with engine.begin() as conn:
        for name in sample_players:
            conn.execute(
                text("""
                    INSERT INTO players (team_code, player_name)
                    VALUES ('C', :name)
                    ON CONFLICT (team_code, player_name) DO NOTHING
                """),
                {"name": name},
            )

    return {"status": "ok", "count": len(sample_players)}


@router.post("/api/admin/players")
def list_players(token: str = Query(...)):
    check_token(token)
    engine = get_engine()

    with engine.connect() as conn:
        rows = conn.execute(
            text("""
                SELECT id, player_name
                FROM players
                ORDER BY id
            """)
        ).fetchall()

    return {
        "status": "ok",
        "count": len(rows),
        "players": [{"id": r.id, "player_name": r.player_name} for r in rows],
    }
