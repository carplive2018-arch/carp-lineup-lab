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

SCORE_MAP = {
    "秋山 翔吾": 95,
    "小園 海斗": 94,
    "坂倉 将吾": 93,
    "末包 昇大": 91,
    "菊池 涼介": 89,
    "野間 峻祥": 88,
    "矢野 雅哉": 87,
    "ファビアン": 86,
    "堂林 翔太": 84,
    "田村 俊介": 83,
    "林 晃汰": 82,
    "モンテロ": 82,
    "中村 奨成": 80,
    "上本 崇司": 78,
    "羽月 隆太郎": 77,
    "會澤 翼": 76,
    "石原 貴規": 75,
    "大盛 穂": 74,
    "中村 貴浩": 73,
    "二俣 翔一": 72,
    "佐々木 泰": 71,
}


@router.get("/api/lineups/today")
def today_lineup():
    engine = get_engine()
    if engine is None:
        return {"status": "ok", "source": "fallback", "lineup": []}

    with engine.connect() as conn:
        rows = conn.execute(
            text("""
                SELECT id, player_name
                FROM players
            """)
        ).fetchall()

    candidates = []
    for row in rows:
        candidates.append({
            "player_id": row.id,
            "player_name": row.player_name,
            "position": POSITION_MAP.get(row.player_name),
            "score": SCORE_MAP.get(row.player_name, 50),
        })

    candidates.sort(key=lambda x: x["score"], reverse=True)

    lineup = []
    used_positions = set()

    for candidate in candidates:
        pos = candidate["position"]
        if pos is None:
            continue
        if pos in used_positions and pos != "C":
            continue
        lineup.append(candidate)
        used_positions.add(pos)
        if len(lineup) == 9:
            break

    for i, player in enumerate(lineup, start=1):
        player["batting_order"] = i

    return {
        "status": "ok",
        "source": "database_scored",
        "count": len(lineup),
        "lineup": lineup
    }
