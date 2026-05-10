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


POSITION_MAP = {
    "秋山 翔吾": "CF",
    "野間 峻祥": "RF",
    "末包 昇大": "LF",
    "田村 俊介": "RF",
    "大盛 穂": "CF",
    "中村 奨成": "LF",
    "中村 貴浩": "LF",
    "ファビアン": "LF",
    "菊池 涼介": "2B",
    "小園 海斗": "SS",
    "矢野 雅哉": "3B",
    "堂林 翔太": "1B",
    "上本 崇司": "2B",
    "羽月 隆太郎": "2B",
    "林 晃汰": "1B",
    "モンテロ": "1B",
    "二俣 翔一": "3B",
    "佐々木 泰": "3B",
    "坂倉 将吾": "C",
    "會澤 翼": "C",
    "石原 貴規": "C",
}

LINEUP_POSITIONS = ["CF", "2B", "SS", "LF", "C", "1B", "3B", "RF"]


def calc_recent_score(row):
    return round(
        row["hits"] * 4
        + row["doubles"] * 2
        + row["triples"] * 3
        + row["homeruns"] * 6
        + row["walks"] * 2
        + row["pa"] * 0.3
        - row["strikeouts"] * 1,
        1,
    )


def make_reason(row):
    parts = []
    if row["hits"] > 0:
        parts.append(f'直近5試合で{row["hits"]}安打')
    if row["homeruns"] > 0:
        parts.append(f'{row["homeruns"]}本塁打')
    if row["walks"] > 0:
        parts.append(f'{row["walks"]}四球')
    if row["strikeouts"] == 0 and row["pa"] > 0:
        parts.append("三振なし")
    if not parts:
        return "直近5試合の出場データが少なめ"
    return "、".join(parts)


@router.get("/api/lineups/today")
def today_lineup():
    engine = get_engine()
    if engine is None:
        return {
            "status": "ok",
            "source": "fallback",
            "lineup": []
        }

    with engine.connect() as conn:
        rows = conn.execute(
            text("""
                WITH recent_games AS (
                    SELECT id
                    FROM games
                    ORDER BY game_date DESC
                    LIMIT 5
                )
                SELECT
                    p.id AS player_id,
                    p.player_name,
                    COALESCE(SUM(s.pa), 0) AS pa,
                    COALESCE(SUM(s.ab), 0) AS ab,
                    COALESCE(SUM(s.hits), 0) AS hits,
                    COALESCE(SUM(s.doubles), 0) AS doubles,
                    COALESCE(SUM(s.triples), 0) AS triples,
                    COALESCE(SUM(s.homeruns), 0) AS homeruns,
                    COALESCE(SUM(s.walks), 0) AS walks,
                    COALESCE(SUM(s.strikeouts), 0) AS strikeouts,
                    COALESCE(SUM(s.rbi), 0) AS rbi
                FROM players p
                LEFT JOIN player_game_batting_stats s
                  ON p.id = s.player_id
                 AND s.game_id IN (SELECT id FROM recent_games)
                GROUP BY p.id, p.player_name
                ORDER BY p.id
            """)
        ).mappings().all()

    candidates = []
    for row in rows:
        position = POSITION_MAP.get(row["player_name"])
        if not position:
            continue

        recent_score = calc_recent_score(row)
        candidates.append({
            "player_id": row["player_id"],
            "player_name": row["player_name"],
            "position": position,
            "recent_score": recent_score,
            "reason": make_reason(row),
            "pa": row["pa"],
            "hits": row["hits"],
            "homeruns": row["homeruns"],
            "walks": row["walks"],
            "strikeouts": row["strikeouts"],
        })

    lineup = []
    used_ids = set()

    for position in LINEUP_POSITIONS:
        pool = [
            c for c in candidates
            if c["position"] == position and c["player_id"] not in used_ids
        ]
        pool.sort(key=lambda x: x["recent_score"], reverse=True)

        if pool:
            picked = pool[0]
            used_ids.add(picked["player_id"])
            lineup.append(picked)

    lineup.append({
        "player_id": None,
        "player_name": "投手",
        "position": "P",
        "recent_score": 0,
        "reason": "投手枠",
        "pa": 0,
        "hits": 0,
        "homeruns": 0,
        "walks": 0,
        "strikeouts": 0,
    })

    for i, player in enumerate(lineup, start=1):
        player["batting_order"] = i

    return {
        "status": "ok",
        "source": "recent_stats_scored",
        "count": len(lineup),
        "lineup": lineup
    }
