import os
from datetime import date, timedelta

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
    "野間 峻祥",
    "末包 昇大",
    "田村 俊介",
    "大盛 穂",
    "中村 奨成",
    "中村 貴浩",
    "ファビアン",
    "菊池 涼介",
    "小園 海斗",
    "矢野 雅哉",
    "堂林 翔太",
    "上本 崇司",
    "羽月 隆太郎",
    "林 晃汰",
    "モンテロ",
    "二俣 翔一",
    "佐々木 泰",
    "坂倉 将吾",
    "會澤 翼",
    "石原 貴規"
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


@router.get("/api/admin/players")
def split_total(total: int, games_count: int = 5):
    base = total // games_count
    rem = total % games_count
    return [base + (1 if i < rem else 0) for i in range(games_count)]


@router.get("/api/admin/seed_recent_stats")
def seed_recent_stats(token: str = Query(...)):
    check_token(token)
    engine = get_engine()

    recent_totals = {
        "秋山 翔吾": {"pa": 23, "ab": 20, "hits": 8, "doubles": 2, "triples": 0, "homeruns": 0, "walks": 3, "strikeouts": 1, "rbi": 2},
        "菊池 涼介": {"pa": 21, "ab": 19, "hits": 6, "doubles": 1, "triples": 0, "homeruns": 0, "walks": 1, "strikeouts": 2, "rbi": 2},
        "小園 海斗": {"pa": 22, "ab": 20, "hits": 7, "doubles": 2, "triples": 1, "homeruns": 0, "walks": 1, "strikeouts": 3, "rbi": 4},
        "坂倉 将吾": {"pa": 20, "ab": 17, "hits": 7, "doubles": 1, "triples": 0, "homeruns": 1, "walks": 3, "strikeouts": 2, "rbi": 5},
        "末包 昇大": {"pa": 19, "ab": 17, "hits": 6, "doubles": 1, "triples": 0, "homeruns": 2, "walks": 2, "strikeouts": 4, "rbi": 7},
        "野間 峻祥": {"pa": 18, "ab": 16, "hits": 5, "doubles": 1, "triples": 0, "homeruns": 0, "walks": 2, "strikeouts": 2, "rbi": 1},
        "矢野 雅哉": {"pa": 17, "ab": 15, "hits": 4, "doubles": 0, "triples": 0, "homeruns": 0, "walks": 2, "strikeouts": 1, "rbi": 1},
        "堂林 翔太": {"pa": 18, "ab": 16, "hits": 5, "doubles": 2, "triples": 0, "homeruns": 1, "walks": 1, "strikeouts": 3, "rbi": 4},
        "田村 俊介": {"pa": 16, "ab": 14, "hits": 4, "doubles": 1, "triples": 0, "homeruns": 0, "walks": 2, "strikeouts": 4, "rbi": 1},
        "會澤 翼": {"pa": 12, "ab": 11, "hits": 3, "doubles": 0, "triples": 0, "homeruns": 1, "walks": 1, "strikeouts": 3, "rbi": 3},
    }

    game_days = [5, 4, 3, 2, 1]

    with engine.begin() as conn:
        player_rows = conn.execute(
            text("SELECT id, player_name FROM players")
        ).fetchall()
        player_map = {r.player_name: r.id for r in player_rows}

        game_ids = []
        for days_ago in game_days:
            game_date = date.today() - timedelta(days=days_ago)
            away_team = f"recent-opponent-{days_ago}"

            game_id = conn.execute(
                text("""
                    INSERT INTO games (game_date, home_team, away_team)
                    VALUES (:game_date, '広島', :away_team)
                    ON CONFLICT (game_date, home_team, away_team)
                    DO UPDATE SET away_team = EXCLUDED.away_team
                    RETURNING id
                """),
                {"game_date": game_date, "away_team": away_team},
            ).scalar_one()

            game_ids.append(game_id)

        inserted = 0

        for player_name, totals in recent_totals.items():
            player_id = player_map.get(player_name)
            if not player_id:
                continue

            pa_list = split_total(totals["pa"])
            ab_list = split_total(totals["ab"])
            hits_list = split_total(totals["hits"])
            doubles_list = split_total(totals["doubles"])
            triples_list = split_total(totals["triples"])
            homeruns_list = split_total(totals["homeruns"])
            walks_list = split_total(totals["walks"])
            strikeouts_list = split_total(totals["strikeouts"])
            rbi_list = split_total(totals["rbi"])

            for i, game_id in enumerate(game_ids):
                conn.execute(
                    text("""
                        INSERT INTO player_game_batting_stats (
                            game_id, player_id, pa, ab, hits, doubles, triples,
                            homeruns, walks, strikeouts, rbi
                        )
                        VALUES (
                            :game_id, :player_id, :pa, :ab, :hits, :doubles, :triples,
                            :homeruns, :walks, :strikeouts, :rbi
                        )
                        ON CONFLICT (game_id, player_id)
                        DO UPDATE SET
                            pa = EXCLUDED.pa,
                            ab = EXCLUDED.ab,
                            hits = EXCLUDED.hits,
                            doubles = EXCLUDED.doubles,
                            triples = EXCLUDED.triples,
                            homeruns = EXCLUDED.homeruns,
                            walks = EXCLUDED.walks,
                            strikeouts = EXCLUDED.strikeouts,
                            rbi = EXCLUDED.rbi
                    """),
                    {
                        "game_id": game_id,
                        "player_id": player_id,
                        "pa": pa_list[i],
                        "ab": ab_list[i],
                        "hits": hits_list[i],
                        "doubles": doubles_list[i],
                        "triples": triples_list[i],
                        "homeruns": homeruns_list[i],
                        "walks": walks_list[i],
                        "strikeouts": strikeouts_list[i],
                        "rbi": rbi_list[i],
                    },
                )
                inserted += 1

    return {
        "status": "ok",
        "message": "recent stats seeded",
        "games": len(game_ids),
        "rows": inserted
    }

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
