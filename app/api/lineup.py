from fastapi import APIRouter

router = APIRouter(tags=["lineup"])

@router.get("/api/lineups/today")
def today_lineup():
    return {
        "status": "ok",
        "source": "placeholder",
        "lineup": [
            {"batting_order": 1, "player_name": "秋山 翔吾", "position": "CF"},
            {"batting_order": 2, "player_name": "菊池 涼介", "position": "2B"},
            {"batting_order": 3, "player_name": "小園 海斗", "position": "SS"},
            {"batting_order": 4, "player_name": "坂倉 将吾", "position": "C"},
            {"batting_order": 5, "player_name": "末包 昇大", "position": "LF"},
            {"batting_order": 6, "player_name": "堂林 翔太", "position": "1B"},
            {"batting_order": 7, "player_name": "矢野 雅哉", "position": "3B"},
            {"batting_order": 8, "player_name": "野間 峻祥", "position": "RF"},
            {"batting_order": 9, "player_name": "投手", "position": "P"}
        ]
    }
