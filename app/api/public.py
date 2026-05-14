from __future__ import annotations

from functools import lru_cache
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import json
import re
import time

from html import unescape
from html.parser import HTMLParser
from urllib.parse import urljoin
from urllib.request import Request, urlopen

from fastapi import APIRouter, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse


router = APIRouter(tags=["public"])


TEAM_CODE_TO_NAME = {
    "c": "広島",
    "t": "阪神",
    "g": "巨人",
    "db": "DeNA",
    "d": "中日",
    "s": "ヤクルト",
    "e": "楽天",
    "l": "西武",
    "b": "オリックス",
    "h": "ソフトバンク",
    "m": "ロッテ",
    "f": "日本ハム",
}

TEAM_NAME_TO_CODE = {
    "広島": "c",
    "阪神": "t",
    "巨人": "g",
    "DeNA": "db",
    "ＤｅＮＡ": "db",
    "中日": "d",
    "ヤクルト": "s",
    "楽天": "e",
    "西武": "l",
    "オリックス": "b",
    "ソフトバンク": "h",
    "ロッテ": "m",
    "日本ハム": "f",
}

HOME_VENUE_KEYWORDS = ["マツダ"]
POSITION_BATTING_PRIOR_PA = 60
POSITION_BATTING_PRIOR_AB = 80
RECENT_OBP_PRIOR_PA = 12
RECENT_ISO_PRIOR_AB = 20
RECENT_FULL_TRUST_PA = 8
MIN_CATCHER_RECENT_PA = 4
WEAK_CATCHER_PENALTY = 1.6
TOP_CATCHER_TO_DH_PENALTY = 1.0
TOP_CATCHER_AT_C_BONUS = 0.8
JST = ZoneInfo("Asia/Tokyo")

FIRST_TEAM_MEMBERS_URL = "https://www.carp.co.jp/team/members"
FARM_BATTING_STATS_URL = "https://npb.jp/bis/2026/stats/idb2_c.html"

FIRST_TEAM_CONFIRM_HOUR = 17
FIRST_TEAM_CONFIRM_MINUTE = 30

FARM_MIN_PA = 50
FARM_DISCOUNT = 0.90
PROMOTION_GRACE_DAYS = 7

LAST_FIXED_FIRST_TEAM_POSITION_PLAYERS = {
    "坂倉 将吾",
    "石原 貴規",
    "持丸 泰輝",
    "勝田 成",
    "矢野 雅哉",
    "小園 海斗",
    "菊池 涼介",
    "林 晃汰",
    "辰見 鴻之介",
    "前川 誠太",
    "モンテロ",
    "二俣 翔一",
    "野間 峻祥",
    "平川 蓮",
    "大盛 穂",
}

PROMOTED_FROM_FARM = {
    # 例:
    # "田村 俊介": "2026-05-12",
    # "末包 昇大": "2026-05-12",
}

POS_C = "C"
POS_1B = "1B"
POS_2B = "2B"
POS_3B = "3B"
POS_SS = "SS"
POS_LF = "LF"
POS_CF = "CF"
POS_RF = "RF"
POS_DH = "DH"
POS_P = "P"

POSITION_LABELS = {
    POS_C: "捕手",
    POS_1B: "一塁",
    POS_2B: "二塁",
    POS_3B: "三塁",
    POS_SS: "遊撃",
    POS_LF: "左翼",
    POS_CF: "中堅",
    POS_RF: "右翼",
    POS_DH: "DH",
    POS_P: "投手",
}

PLAYER_PROFILE = {
    "坂倉 将吾": {"eligible_positions": [POS_C, POS_1B, POS_3B, POS_DH]},
    "小園 海斗": {"eligible_positions": [POS_SS, POS_3B]},
    "菊池 涼介": {"eligible_positions": [POS_2B]},
    "モンテロ": {"eligible_positions": [POS_1B, POS_DH]},
    "持丸 泰輝": {"eligible_positions": [POS_C, POS_DH]},
    "石原 貴規": {"eligible_positions": [POS_C]},
    "矢野 雅哉": {"eligible_positions": [POS_SS]},
    "二俣 翔一": {"eligible_positions": [POS_1B, POS_3B, POS_SS, POS_2B, POS_RF, POS_CF, POS_LF]},
    "秋山 翔吾": {"eligible_positions": [POS_LF, POS_CF, POS_RF]},
    "大盛 穂": {"eligible_positions": [POS_LF, POS_CF, POS_RF]},
    "野間 峻祥": {"eligible_positions": [POS_LF, POS_CF, POS_RF]},
    "平川 蓮": {"eligible_positions": [POS_LF, POS_CF, POS_RF]},
    "ファビアン": {"eligible_positions": [POS_LF, POS_RF, POS_DH]},
    "佐々木 泰": {"eligible_positions": [POS_1B, POS_3B, POS_DH]},
    "勝田 成": {"eligible_positions": [POS_2B, POS_SS]},
}

FARM_PROMOTION_CANDIDATES = {
    "堂林 翔太": {"eligible_positions": [POS_1B, POS_3B]},
    "末包 昇大": {"eligible_positions": [POS_LF, POS_RF, POS_DH]},
    "田村 俊介": {"eligible_positions": [POS_LF, POS_CF, POS_RF]},
    "中村 貴浩": {"eligible_positions": [POS_LF, POS_RF]},
    "名原 典彦": {"eligible_positions": [POS_LF, POS_CF, POS_RF]},
    "岸本 大希": {"eligible_positions": [POS_2B, POS_SS]},
    "内田 湘大": {"eligible_positions": [POS_1B, POS_3B]},
}

PLAYER_PROFILE.update(FARM_PROMOTION_CANDIDATES)

PLAYER_NAME_ALIASES = {
    "小園海斗": "小園 海斗",
    "勝田成": "勝田 成",
    "二俣翔一": "二俣 翔一",
    "大盛穂": "大盛 穂",
    "田村俊介": "田村 俊介",
    "矢野雅哉": "矢野 雅哉",
    "坂倉将吾": "坂倉 将吾",
    "持丸泰輝": "持丸 泰輝",
    "持丸輝泰": "持丸 泰輝",
}

CURRENT_SEASON_YEAR = 2026
PRORAN_TEAM_BATTERS_URL = "https://proran.jp/team_detail_b.php?t=_c"
PRORAN_PLAYER_DETAIL_MORE_URL = "https://proran.jp/player_detail_more.php?id={player_id}&y={year}"
NPBBASEMENT_FIELDING_URL = "https://npbbasement.com/fielding"
NPBBASEMENT_BASE_URL = "https://npbbasement.com"

CACHE_TTL_PLAYER_DEFENSE = 60 * 60 * 12
CACHE_TTL_SEASON_POSITION_BATTING = 60 * 60 * 6
CACHE_TTL_RECENT_BATTING = 60 * 5
CACHE_TTL_PREDICTED_LINEUP = 60 * 3

POSITION_LABEL_TO_CODE = {
    "捕手": "C",
    "一塁": "1B",
    "一塁手": "1B",
    "二塁": "2B",
    "二塁手": "2B",
    "三塁": "3B",
    "三塁手": "3B",
    "遊撃": "SS",
    "遊撃手": "SS",
    "左翼": "LF",
    "左翼手": "LF",
    "中堅": "CF",
    "中堅手": "CF",
    "右翼": "RF",
    "右翼手": "RF",
    "指名打者": "DH",
    "DH": "DH",
}

CACHE = {
    "player_defense": {"value": None, "expires_at": 0},
    "season_position_batting": {"value": None, "expires_at": 0},
    "recent_batting": {},
    "predicted_lineup": {},
}


PLAYER_DEFENSE_FALLBACK = {
    "坂倉 将吾": {"C": 0.30, "1B": 0.20, "3B": -0.20, "DH": 0.00},
    "小園 海斗": {"SS": 0.80, "3B": 0.40},
    "菊池 涼介": {"2B": 1.50},
}

SEASON_OVERALL_BATTING = {
    "坂倉 将吾": {"obp": 0.330, "iso": 0.130},
    "小園 海斗": {"obp": 0.310, "iso": 0.110},
    "菊池 涼介": {"obp": 0.290, "iso": 0.080},
    "モンテロ": {"obp": 0.320, "iso": 0.180},
    "持丸 泰輝": {"obp": 0.310, "iso": 0.150},
    "石原 貴規": {"obp": 0.280, "iso": 0.070},
    "矢野 雅哉": {"obp": 0.290, "iso": 0.050},
    "二俣 翔一": {"obp": 0.300, "iso": 0.090},
    "秋山 翔吾": {"obp": 0.330, "iso": 0.100},
    "大盛 穂": {"obp": 0.300, "iso": 0.080},
    "野間 峻祥": {"obp": 0.310, "iso": 0.070},
    "平川 蓮": {"obp": 0.290, "iso": 0.080},
    "ファビアン": {"obp": 0.320, "iso": 0.180},
    "佐々木 泰": {"obp": 0.300, "iso": 0.110},
    "勝田 成": {"obp": 0.290, "iso": 0.070},
    "堂林 翔太": {"obp": 0.310, "iso": 0.150},
    "末包 昇大": {"obp": 0.310, "iso": 0.180},
    "田村 俊介": {"obp": 0.320, "iso": 0.120},
    "中村 貴浩": {"obp": 0.300, "iso": 0.140},
    "名原 典彦": {"obp": 0.290, "iso": 0.090},
    "岸本 大希": {"obp": 0.290, "iso": 0.080},
    "内田 湘大": {"obp": 0.290, "iso": 0.110},
}


DH_LINEUP_SLOTS = [
    {
        "order": 1,
        "allowed_positions": [POS_CF, POS_2B],
        "role": "lead_obp_glove",
        "weights": {"recent": 0.45, "defense": 0.35, "season_pos": 0.20},
        "min_defense": 0.00,
        "low_defense_penalty": 1.20,
    },
    {
        "order": 2,
        "allowed_positions": [POS_DH, POS_1B],
        "role": "two_hole_bat",
        "weights": {"recent": 0.50, "defense": 0.10, "season_pos": 0.40},
        "min_defense": -9.99,
        "low_defense_penalty": 0.00,
    },
    {
        "order": 3,
        "allowed_positions": [POS_3B, POS_RF],
        "role": "three_hole_iso_glove",
        "weights": {"recent": 0.45, "defense": 0.20, "season_pos": 0.35},
        "min_defense": -0.30,
        "low_defense_penalty": 0.80,
    },
    {
        "order": 4,
        "allowed_positions": [POS_1B, POS_DH],
        "role": "cleanup_bat",
        "weights": {"recent": 0.50, "defense": 0.05, "season_pos": 0.45},
        "min_defense": -9.99,
        "low_defense_penalty": 0.00,
    },
    {
        "order": 5,
        "allowed_positions": [POS_LF],
        "role": "five_hole_power",
        "weights": {"recent": 0.45, "defense": 0.15, "season_pos": 0.40},
        "min_defense": -0.60,
        "low_defense_penalty": 0.90,
    },
    {
        "order": 6,
        "allowed_positions": [POS_2B, POS_CF],
        "role": "six_hole_balance",
        "weights": {"recent": 0.35, "defense": 0.40, "season_pos": 0.25},
        "min_defense": 0.00,
        "low_defense_penalty": 1.20,
    },
    {
        "order": 7,
        "allowed_positions": [POS_C],
        "role": "glove_bottom",
        "weights": {"recent": 0.15, "defense": 0.65, "season_pos": 0.20},
        "min_defense": 0.30,
        "low_defense_penalty": 1.50,
    },
    {
        "order": 8,
        "allowed_positions": [POS_SS],
        "role": "glove_bottom",
        "weights": {"recent": 0.10, "defense": 0.70, "season_pos": 0.20},
        "min_defense": 0.30,
        "low_defense_penalty": 1.50,
    },
    {
        "order": 9,
        "allowed_positions": [POS_RF, POS_3B],
        "role": "turnover_obp",
        "weights": {"recent": 0.35, "defense": 0.25, "season_pos": 0.40},
        "min_defense": -0.30,
        "low_defense_penalty": 0.80,
    },
]

NO_DH_LINEUP_SLOTS = [
    {
        "order": 1,
        "allowed_positions": [POS_CF, POS_2B],
        "role": "lead_obp_glove",
        "weights": {"recent": 0.40, "defense": 0.40, "season_pos": 0.20},
        "min_defense": 0.00,
        "low_defense_penalty": 1.20,
    },
    {
        "order": 2,
        "allowed_positions": [POS_1B, POS_3B],
        "role": "two_hole_bat",
        "weights": {"recent": 0.40, "defense": 0.20, "season_pos": 0.40},
        "min_defense": -0.40,
        "low_defense_penalty": 0.90,
    },
    {
        "order": 3,
        "allowed_positions": [POS_RF, POS_3B],
        "role": "three_hole_iso_glove",
        "weights": {"recent": 0.45, "defense": 0.20, "season_pos": 0.35},
        "min_defense": -0.30,
        "low_defense_penalty": 0.80,
    },
    {
        "order": 4,
        "allowed_positions": [POS_LF, POS_1B],
        "role": "cleanup_bat",
        "weights": {"recent": 0.45, "defense": 0.15, "season_pos": 0.40},
        "min_defense": -0.50,
        "low_defense_penalty": 0.90,
    },
    {
        "order": 5,
        "allowed_positions": [POS_3B, POS_RF],
        "role": "five_hole_power",
        "weights": {"recent": 0.40, "defense": 0.20, "season_pos": 0.40},
        "min_defense": -0.30,
        "low_defense_penalty": 0.80,
    },
    {
        "order": 6,
        "allowed_positions": [POS_2B, POS_CF],
        "role": "six_hole_balance",
        "weights": {"recent": 0.35, "defense": 0.40, "season_pos": 0.25},
        "min_defense": 0.00,
        "low_defense_penalty": 1.20,
    },
    {
        "order": 7,
        "allowed_positions": [POS_C, POS_SS],
        "role": "glove_bottom",
        "weights": {"recent": 0.10, "defense": 0.70, "season_pos": 0.20},
        "min_defense": 0.30,
        "low_defense_penalty": 1.50,
    },
    {
        "order": 8,
        "allowed_positions": [POS_SS, POS_C],
        "role": "glove_bottom",
        "weights": {"recent": 0.10, "defense": 0.70, "season_pos": 0.20},
        "min_defense": 0.30,
        "low_defense_penalty": 1.50,
    },
]


def _cache_now() -> float:
    return time.time()


def _cache_alive(entry: dict | None) -> bool:
    if not entry:
        return False
    return float(entry.get("expires_at", 0) or 0) > _cache_now()


def _cache_get_bucket(bucket: str):
    return CACHE.setdefault(bucket, {})


def _canonical_player_name(name: str) -> str:
    normalized = _normalize_player_name(name)

    if normalized in PLAYER_NAME_ALIASES:
        return PLAYER_NAME_ALIASES[normalized]

    for full_name in PLAYER_PROFILE.keys():
        if _normalize_player_name(full_name) == normalized:
            return full_name

    return name


def _layout(title: str, body: str) -> HTMLResponse:
    return HTMLResponse(
        f"""
        <!doctype html>
        <html lang="ja">
        <head>
          <meta charset="utf-8" />
          <meta name="viewport" content="width=device-width, initial-scale=1" />
          <title>{title}</title>
          <style>
            body {{ font-family: -apple-system, BlinkMacSystemFont, 'Hiragino Sans', sans-serif; margin: 0; background: #0b1020; color: #f5f7fb; }}
            .wrap {{ max-width: 960px; margin: 0 auto; padding: 40px 20px 72px; }}
            .card {{ background: #121a31; border: 1px solid #26304d; border-radius: 18px; padding: 24px; margin-top: 18px; }}
            .game-card {{ background: #0f1730; border: 1px solid #26304d; border-radius: 16px; padding: 18px; }}
            .grid {{ display: grid; gap: 14px; }}
            a {{ color: #9fc2ff; }}
            h1, h2, h3 {{ line-height: 1.3; margin-top: 0; }}
            ul, ol {{ line-height: 1.8; }}
            .muted {{ color: #a9b5d1; }}
            .pill {{ display: inline-block; padding: 6px 10px; border-radius: 999px; background: #243154; color: #cfe0ff; font-size: 12px; }}
            .date {{ font-size: 18px; font-weight: 700; margin-bottom: 8px; }}
            .small {{ font-size: 12px; color: #a9b5d1; }}
            code {{ background: #0f1730; padding: 2px 6px; border-radius: 6px; }}
            .segmented {{ display: flex; gap: 8px; flex-wrap: wrap; margin: 10px 0 14px; }}
            .segmented button {{ border: 1px solid #39507d; background: #16203d; color: #eaf1ff; border-radius: 999px; padding: 10px 14px; cursor: pointer; font-weight: 700; }}
            .segmented button.active {{ background: #ffd54a; color: #182033; border-color: #ffd54a; }}
            .stats-grid {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(140px, 1fr)); gap: 10px; margin-top: 14px; }}
            .stat-box {{ background: #0f1730; border: 1px solid #26304d; border-radius: 12px; padding: 12px; }}
            .stat-label {{ font-size: 12px; color: #a9b5d1; margin-bottom: 6px; }}
            .stat-value {{ font-size: 20px; font-weight: 700; }}
            .table-wrap {{ margin-top: 16px; overflow-x: auto; }}
            .stats-table {{ width: 100%; border-collapse: collapse; min-width: 880px; }}
            .stats-table th, .stats-table td {{ border-bottom: 1px solid #26304d; padding: 10px 8px; text-align: right; }}
            .stats-table th:first-child, .stats-table td:first-child {{ text-align: left; position: sticky; left: 0; background: #121a31; }}
            .stats-table th {{ font-size: 12px; color: #a9b5d1; }}
          </style>
        </head>
        <body>
          <div class="wrap">{body}</div>
        </body>
        </html>
        """
    )


def _clean_text(value: str) -> str:
    value = unescape(value)
    value = re.sub(r"<br\s*/?>", " ", value, flags=re.IGNORECASE)
    value = re.sub(r"<[^>]+>", "", value)
    value = value.replace("&nbsp;", " ")
    value = value.replace("\u3000", " ")
    value = re.sub(r"\s+", " ", value).strip()
    return value


def _normalize_player_name(name: str) -> str:
    if not name:
        return ""
    text = unescape(str(name)).strip()
    text = text.replace("　", "").replace(" ", "")
    text = re.sub(r"\s+", "", text)
    return text


def _to_float_or_none(value: str | None) -> float | None:
    if value is None:
        return None
    s = str(value).strip()
    if s in {"", "-", "---", "—"}:
        return None
    if s.startswith("."):
        s = "0" + s
    try:
        return float(s)
    except ValueError:
        return None


def _safe_float(value):
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).strip().replace(",", "")
    if text in {"", "-", "—", "–", "None", "null"}:
        return None
    try:
        return float(text)
    except Exception:
        return None


def _extract_proran_position_table(html_text: str) -> dict[str, dict[str, float]]:
    result: dict[str, dict[str, float]] = {}

    start = html_text.find("守備ポジション別成績")
    if start == -1:
        return result

    end = html_text.find("対球団別成績", start)
    if end == -1:
        end = html_text.find("球場別成績", start)
    if end == -1:
        end = start + 40000

    section = html_text[start:end]

    def _clean(text: str) -> str:
        text = re.sub(r"<br\s*/?>", "", text, flags=re.I)
        text = re.sub(r"<[^>]+>", "", text)
        text = unescape(text)
        text = text.replace("　", " ")
        text = re.sub(r"\s+", "", text)
        text = text.replace("Ｏ", "O")
        return text.strip()

    def _to_float(text: str) -> float:
        text = _clean(text)
        if text in {"", "-", "--", "---", "----"}:
            return 0.0
        text = text.replace("−", "-").replace("%", "")
        if text.startswith("."):
            text = "0" + text
        try:
            return float(text)
        except Exception:
            return 0.0

    fixed_match = re.search(
        r'flex_box\s+fixed_l.*?border_r_only.*?<div>(.*?)</div>\s*</div>',
        section,
        flags=re.S | re.I,
    )
    if not fixed_match:
        return result

    fixed_html = fixed_match.group(1)

    position_labels = [
        _clean(x)
        for x in re.findall(
            r'<div class="player_detail_more_ba[^"]*bg_c_th[^"]*">(.*?)</div>',
            fixed_html,
            flags=re.S | re.I,
        )
    ]
    position_labels = [x for x in position_labels if x not in {"", "-", "－"}]
    if not position_labels:
        return result

    metrics: dict[str, list[float]] = {}

    for metric_name in ["打数", "打率", "出塁率", "OPS"]:
        metric_match = re.search(
            rf'player_detail_more_th[^>]*>\s*{metric_name}\s*</div>(.*?)(?=player_detail_more_th[^>]*>|$)',
            section,
            flags=re.S | re.I,
        )
        if not metric_match:
            metrics[metric_name] = []
            continue

        metric_block = metric_match.group(1)
        values = re.findall(
            r'player_detail_more_ba[^"]*right[^"]*">(.*?)</div>',
            metric_block,
            flags=re.S | re.I,
        )
        metrics[metric_name] = [_to_float(v) for v in values[: len(position_labels)]]

    for i, label in enumerate(position_labels):
        pos_code = POSITION_LABEL_TO_CODE.get(label)
        if not pos_code:
            continue

        ab = metrics.get("打数", [])[i] if i < len(metrics.get("打数", [])) else 0.0
        avg = metrics.get("打率", [])[i] if i < len(metrics.get("打率", [])) else 0.0
        obp = metrics.get("出塁率", [])[i] if i < len(metrics.get("出塁率", [])) else 0.0
        ops = metrics.get("OPS", [])[i] if i < len(metrics.get("OPS", [])) else 0.0

        if ab <= 0 and obp == 0.0 and ops == 0.0:
            continue

        iso = max(0.0, ops - obp - avg)

        result[pos_code] = {
            "pa": ab,
            "ab": ab,
            "obp": round(obp, 3),
            "iso": round(iso, 3),
        }

    return result


def _fetch_text(url: str) -> str:
    req = Request(
        url,
        headers={
            "User-Agent": "Mozilla/5.0",
            "Accept-Language": "ja,en-US;q=0.9,en;q=0.8",
        },
    )
    with urlopen(req, timeout=20) as res:
        return res.read().decode("utf-8", errors="ignore")


@lru_cache(maxsize=1)
def _discover_proran_player_ids() -> dict[str, str]:
    html = _fetch_text(PRORAN_TEAM_BATTERS_URL)

    result: dict[str, str] = {}

    patterns = [
        r'href=["\'](?:\./)?player_detail(?:_more)?\.php\?id=(\d+)(?:&[^"\']*)?["\'][^>]*>(.*?)</a>',
        r'href=["\'][^"\']*player_detail(?:_more)?\.php\?id=(\d+)(?:&[^"\']*)?["\'][^>]*>(.*?)</a>',
    ]

    for pattern in patterns:
        pairs = re.findall(pattern, html, flags=re.S | re.I)
        for player_id, raw_name in pairs:
            player_name = _clean_text(raw_name)
            if not player_name:
                continue

            normalized = _normalize_player_name(player_name)
            if not normalized:
                continue

            result[normalized] = player_id

            canonical = PLAYER_NAME_ALIASES.get(normalized)
            if canonical:
                result[_normalize_player_name(canonical)] = player_id

    return result


def _get_proran_player_ids() -> dict[str, str]:
    try:
        return _discover_proran_player_ids()
    except Exception as e:
        print("DEBUG_PRORAN_ID_ERROR", str(e))
        return {}


def _fetch_proran_position_batting(player_name: str, player_id: str) -> dict:
    url = PRORAN_PLAYER_DETAIL_MORE_URL.format(
        player_id=player_id,
        year=CURRENT_SEASON_YEAR,
    )
    html_text = _fetch_text(url)
    data = _extract_proran_position_table(html_text)
    print("DEBUG_PRORAN_PARSED", player_name, data)
    return data


def _build_season_position_batting_from_proran() -> dict:
    result = {}
    player_ids = _get_proran_player_ids()

    for player_name in PLAYER_PROFILE.keys():
        normalized_name = _normalize_player_name(player_name)
        player_id = player_ids.get(normalized_name)
        if not player_id:
            continue

        try:
            position_stats = _fetch_proran_position_batting(player_name, player_id)
            if position_stats:
                result[normalized_name] = position_stats
                result[player_name] = position_stats
        except Exception as e:
            print("DEBUG_PRORAN_BUILD_ERROR", player_name, str(e))
            continue

    return result


def _get_season_position_batting() -> dict:
    global SEASON_POSITION_BATTING

    cache_entry = CACHE.get("season_position_batting", {})
    if _cache_alive(cache_entry):
        cached_value = cache_entry.get("value")
        if isinstance(cached_value, dict):
            return cached_value

    try:
        data = _build_season_position_batting_from_proran()
        if not isinstance(data, dict):
            data = {}
    except Exception as e:
        print("DEBUG_SEASON_POSITION_BATTING_ERROR", str(e))
        data = {}

    if data:
        SEASON_POSITION_BATTING = data
        CACHE["season_position_batting"] = {
            "expires_at": _cache_now() + CACHE_TTL_SEASON_POSITION_BATTING,
            "value": data,
        }
        return data

    fallback_value = SEASON_POSITION_BATTING if isinstance(SEASON_POSITION_BATTING, dict) else {}

    CACHE["season_position_batting"] = {
        "expires_at": _cache_now() + 60,
        "value": fallback_value,
    }

    return fallback_value


def _calc_def_from_components(fld: dict) -> float:
    value = 0.0

    for key in ["RngR", "DPR", "ARM", "ErrR", "Positional"]:
        value += _safe_float(fld.get(key)) or 0.0

    if (fld.get("POS") or "").upper() == "C":
        value += _safe_float(fld.get("Framing")) or 0.0
        value += _safe_float(fld.get("Blocking")) or 0.0

    return round(value, 3)


def _discover_npbbasement_main_bundle_url() -> str | None:
    html = _fetch_text(NPBBASEMENT_FIELDING_URL)
    match = re.search(
        r'<script type="module" crossorigin src="([^"]+index-[^"]+\.js)">',
        html,
    )
    if not match:
        return None
    return urljoin(NPBBASEMENT_BASE_URL, match.group(1))


def _discover_npbbasement_2026_chunk_url() -> str | None:
    main_bundle_url = _discover_npbbasement_main_bundle_url()
    if not main_bundle_url:
        return None

    js = _fetch_text(main_bundle_url)
    match = re.search(r'\./2026_1g-[A-Za-z0-9_-]+\.js', js)
    if not match:
        return None

    chunk_name = match.group(0).replace("./", "")
    return urljoin(NPBBASEMENT_BASE_URL + "/assets/", chunk_name)


def _load_npbbasement_players() -> list[dict]:
    chunk_url = _discover_npbbasement_2026_chunk_url()
    if not chunk_url:
        return []

    js = _fetch_text(chunk_url)
    match = re.search(r"JSON\.parse\(`(.*)`\)", js, flags=re.S)
    if not match:
        return []

    raw_json = match.group(1)
    try:
        raw_json = raw_json.encode("utf-8").decode("unicode_escape")
    except Exception:
        pass

    try:
        return json.loads(raw_json)
    except Exception:
        return []


def _build_player_defense_from_npbbasement() -> dict:
    result = {}
    players = _load_npbbasement_players()

    normalized_profile_names = {
        _normalize_player_name(name): name
        for name in PLAYER_PROFILE.keys()
    }

    for player in players:
        name = _normalize_player_name(
            player.get("nameJ") or player.get("nameSponavi") or ""
        )
        real_name = normalized_profile_names.get(name)
        if not real_name:
            continue

        fld_list = ((player.get("Stats") or {}).get("fld") or [])
        for fld in fld_list:
            pos = (fld.get("POS") or "").upper().strip()
            if not pos:
                continue

            defense_value = _calc_def_from_components(fld)
            result.setdefault(real_name, {})[pos] = defense_value

    return result


def _get_player_defense() -> dict[str, dict[str, float]]:
    global PLAYER_DEFENSE

    cache_entry = CACHE.get("player_defense", {})
    if _cache_alive(cache_entry) and cache_entry.get("value"):
        return cache_entry["value"]

    try:
        data = _build_player_defense_from_npbbasement()
        if not data:
            data = dict(PLAYER_DEFENSE_FALLBACK)
    except Exception:
        data = dict(PLAYER_DEFENSE_FALLBACK)

    PLAYER_DEFENSE = data
    CACHE["player_defense"] = {
        "expires_at": _cache_now() + CACHE_TTL_PLAYER_DEFENSE,
        "value": data,
    }
    return data


def _normalize_name(value: str) -> str:
    value = _clean_text(value)
    value = value.replace(" ", "").replace("　", "")
    return value


def _safe_int(value: str) -> int:
    value = _clean_text(value).replace(",", "")
    m = re.search(r"-?\d+", value)
    if not m:
        return 0
    return int(m.group(0))


def _round3(value: float) -> float:
    return round(value, 3)


def _calc_iso_from_stats(stats: dict) -> float:
    at_bats = int(stats.get("at_bats", stats.get("ab", 0)) or 0)
    if at_bats <= 0:
        return 0.0

    doubles = int(stats.get("doubles", 0) or 0)
    triples = int(stats.get("triples", 0) or 0)
    homeruns = int(stats.get("homeruns", 0) or 0)

    iso = (doubles + 2 * triples + 3 * homeruns) / at_bats
    return _round3(iso)


def _zscore_map(values: dict[str, float]) -> dict[str, float]:
    if not values:
        return {}

    nums = list(values.values())
    mean = sum(nums) / len(nums)
    variance = sum((x - mean) ** 2 for x in nums) / len(nums)
    std = variance ** 0.5

    if std == 0:
        return {k: 0.0 for k in values}

    return {k: (v - mean) / std for k, v in values.items()}


def _position_universe(slot_defs: list[dict]) -> list[str]:
    result: list[str] = []
    for slot in slot_defs:
        for pos in slot["allowed_positions"]:
            if pos not in result:
                result.append(pos)
    return result


def _get_adjusted_position_batting(player_name: str, position: str) -> dict:
    global SEASON_POSITION_BATTING

    canonical_name = _canonical_player_name(player_name)
    normalized_name = _normalize_player_name(canonical_name)

    if not SEASON_POSITION_BATTING:
        try:
            SEASON_POSITION_BATTING.update(_get_season_position_batting())
        except Exception:
            pass

    player_stats = (
        SEASON_POSITION_BATTING.get(canonical_name)
        or SEASON_POSITION_BATTING.get(normalized_name)
        or {}
    )

    if not player_stats:
        player_ids = _get_proran_player_ids()
        player_id = player_ids.get(normalized_name)
        if player_id:
            try:
                fetched = _fetch_proran_position_batting(canonical_name, player_id)
                print("DEBUG_PRORAN_FETCH", canonical_name, fetched)
                if fetched:
                    SEASON_POSITION_BATTING[canonical_name] = fetched
                    SEASON_POSITION_BATTING[normalized_name] = fetched
                    player_stats = fetched
                    CACHE["season_position_batting"] = {
                        "value": dict(SEASON_POSITION_BATTING),
                        "expires_at": _cache_now() + CACHE_TTL_SEASON_POSITION_BATTING,
                    }
            except Exception as e:
                print("DEBUG_PRORAN_FETCH_ERROR", canonical_name, str(e))
                player_stats = {}

    pos_stats = (player_stats or {}).get(position, {})

    return {
        "pa": float(pos_stats.get("pa", 0.0) or 0.0),
        "ab": float(pos_stats.get("ab", 0.0) or 0.0),
        "obp": float(pos_stats.get("obp", 0.0) or 0.0),
        "iso": float(pos_stats.get("iso", 0.0) or 0.0),
    }


def _now_jst() -> datetime:
    return datetime.now(JST)


def _is_after_first_team_confirm_time(now: datetime | None = None) -> bool:
    now = now or _now_jst()
    check_time = now.replace(
        hour=FIRST_TEAM_CONFIRM_HOUR,
        minute=FIRST_TEAM_CONFIRM_MINUTE,
        second=0,
        microsecond=0,
    )
    return now >= check_time


def _name_aliases(name: str) -> set[str]:
    cleaned = _clean_text(name)
    nospace = cleaned.replace(" ", "").replace("　", "")
    aliases = {cleaned, nospace}

    if " " in cleaned:
        parts = [p for p in cleaned.split(" ") if p]
        if parts:
            aliases.add(parts[0])

    if "." in cleaned:
        tail = cleaned.split(".")[-1].strip()
        if tail:
            aliases.add(tail)
            aliases.add(_normalize_name(tail))

    return {a for a in aliases if a}


def _find_farm_batting_table(tables: list[list[list[str]]]) -> list[list[str]]:
    for table in tables:
        if not table:
            continue

        header = [_clean_text(cell) for cell in table[0]]
        required = {"選手", "打席", "打数", "打率", "長打率", "出塁率"}

        if required.issubset(set(header)):
            return table

    return []


@lru_cache(maxsize=1)
def _fetch_current_first_team_position_players() -> set[str]:
    try:
        html = _fetch_html(FIRST_TEAM_MEMBERS_URL)
        text = _clean_text(html)
        normalized_text = text.replace(" ", "").replace("　", "")

        m = re.search(r"一軍メンバー(.*?)二軍メンバー", normalized_text)
        block = m.group(1) if m else normalized_text

        result = set()
        for name in PLAYER_PROFILE.keys():
            for alias in _name_aliases(name):
                if _normalize_name(alias) in block:
                    result.add(name)
                    break

        if result:
            return result
    except Exception:
        pass

    return set(LAST_FIXED_FIRST_TEAM_POSITION_PLAYERS)


def _get_active_first_team_position_players(now: datetime | None = None) -> set[str]:
    now = now or _now_jst()

    if _is_after_first_team_confirm_time(now):
        current = _fetch_current_first_team_position_players()
        if current:
            return set(current)

    return set(LAST_FIXED_FIRST_TEAM_POSITION_PLAYERS)


def _is_recently_promoted(player_name: str, now: datetime | None = None) -> bool:
    now = now or _now_jst()
    date_str = PROMOTED_FROM_FARM.get(player_name)
    if not date_str:
        return False

    try:
        promoted_at = datetime.strptime(date_str, "%Y-%m-%d").replace(tzinfo=JST)
    except Exception:
        return False

    return now < promoted_at + timedelta(days=PROMOTION_GRACE_DAYS)


def _build_farm_score_maps(candidate_names: list[str]) -> dict:
    try:
        html = _fetch_html(FARM_BATTING_STATS_URL)
        tables = _extract_tables(html)
        table = _find_farm_batting_table(tables)

        if not table:
            return {
                "farm_score": {},
                "farm_pa": {},
            }

        header = [_clean_text(cell) for cell in table[0]]
        idx = {name: i for i, name in enumerate(header)}

        def cell(row: list[str], key: str) -> str:
            i = idx.get(key)
            if i is None or i >= len(row):
                return ""
            return row[i]

        alias_to_full = {}
        for name in candidate_names:
            for alias in _name_aliases(name):
                alias_to_full[_normalize_name(alias)] = name

        obp_map: dict[str, float] = {}
        iso_map: dict[str, float] = {}
        pa_map: dict[str, int] = {}

        for row in table[1:]:
            raw_name = _clean_text(cell(row, "選手"))
            raw_name = re.sub(r"^[*+]+", "", raw_name).strip()

            mapped_name = None
            for alias in _name_aliases(raw_name):
                mapped_name = alias_to_full.get(_normalize_name(alias))
                if mapped_name:
                    break

            if not mapped_name:
                continue

            pa = _safe_int(cell(row, "打席"))
            if pa < FARM_MIN_PA:
                continue

            ba = _safe_float(cell(row, "打率")) or 0.0
            obp = _safe_float(cell(row, "出塁率")) or 0.0
            slg = _safe_float(cell(row, "長打率")) or 0.0
            iso = max(0.0, slg - ba)

            pa_map[mapped_name] = pa
            obp_map[mapped_name] = obp
            iso_map[mapped_name] = iso

        obp_z = _zscore_map(obp_map)
        iso_z = _zscore_map(iso_map)

        farm_score = {
            name: FARM_DISCOUNT * (
                0.55 * obp_z.get(name, 0.0) + 0.45 * iso_z.get(name, 0.0)
            )
            for name in obp_map.keys()
        }

        return {
            "farm_score": farm_score,
            "farm_pa": pa_map,
        }

    except Exception:
        return {
            "farm_score": {},
            "farm_pa": {},
        }


def _get_prediction_candidate_names(now: datetime | None = None) -> list[str]:
    now = now or _now_jst()

    active_first_team = _get_active_first_team_position_players(now)
    farm_maps = _build_farm_score_maps(list(FARM_PROMOTION_CANDIDATES.keys()))

    candidates: list[str] = []

    for name in PLAYER_PROFILE.keys():
        if name in active_first_team and name not in candidates:
            candidates.append(name)

    for name in FARM_PROMOTION_CANDIDATES.keys():
        if name in farm_maps["farm_score"] and name not in candidates:
            candidates.append(name)

    return candidates


def _build_recent_score_maps(window_games: int, candidate_names: list[str]) -> dict:
    cache_key = f"recent:{window_games}"
    bucket = _cache_get_bucket("recent_batting")
    entry = bucket.get(cache_key)

    if _cache_alive(entry):
        return entry["value"]

    recent_data = _aggregate_recent_batting_stats(window_games)

    alias_to_full = {}
    for name in candidate_names:
        for alias in _name_aliases(name):
            alias_to_full[_normalize_name(alias)] = name

    recent_players = {}
    for p in recent_data.get("players", []):
        raw_name = _clean_text(p.get("player_name", ""))
        mapped_name = None

        for alias in _name_aliases(raw_name):
            mapped_name = alias_to_full.get(_normalize_name(alias))
            if mapped_name:
                break

        if mapped_name:
            recent_players[mapped_name] = p

    team_totals = recent_data.get("team_totals", {})
    team_obp = _safe_float(team_totals.get("on_base_percentage", 0.0)) or 0.0
    team_iso = _calc_iso_from_stats(team_totals)

    raw_obp_map: dict[str, float] = {}
    raw_iso_map: dict[str, float] = {}
    adj_obp_map: dict[str, float] = {}
    adj_iso_map: dict[str, float] = {}
    pa_map: dict[str, int] = {}
    ab_map: dict[str, int] = {}
    sample_weight_map: dict[str, float] = {}

    for name in candidate_names:
        player = recent_players.get(name, {})

        raw_obp = _safe_float(player.get("on_base_percentage", 0.0)) or 0.0
        raw_iso = _calc_iso_from_stats(player)
        pa = int(player.get("plate_appearances", 0) or 0)
        ab = int(player.get("at_bats", 0) or 0)

        adj_obp_den = pa + RECENT_OBP_PRIOR_PA
        adj_iso_den = ab + RECENT_ISO_PRIOR_AB

        adj_obp = (
            ((pa * raw_obp) + (RECENT_OBP_PRIOR_PA * team_obp)) / adj_obp_den
            if adj_obp_den > 0
            else team_obp
        )
        adj_iso = (
            ((ab * raw_iso) + (RECENT_ISO_PRIOR_AB * team_iso)) / adj_iso_den
            if adj_iso_den > 0
            else team_iso
        )

        sample_weight = min(pa / RECENT_FULL_TRUST_PA, 1.0)

        raw_obp_map[name] = raw_obp
        raw_iso_map[name] = raw_iso
        adj_obp_map[name] = _round3(adj_obp)
        adj_iso_map[name] = _round3(adj_iso)
        pa_map[name] = pa
        ab_map[name] = ab
        sample_weight_map[name] = sample_weight

    obp_z = _zscore_map(adj_obp_map)
    iso_z = _zscore_map(adj_iso_map)

    recent_form_score = {
        name: sample_weight_map.get(name, 0.0)
        * (0.55 * obp_z.get(name, 0.0) + 0.45 * iso_z.get(name, 0.0))
        for name in candidate_names
    }

    recent_bat_value = {
        name: sample_weight_map.get(name, 0.0)
        * (0.60 * obp_z.get(name, 0.0) + 0.40 * iso_z.get(name, 0.0))
        for name in candidate_names
    }

    active_first_team = _get_active_first_team_position_players()
    farm_maps = _build_farm_score_maps(candidate_names)
    farm_score_map = farm_maps.get("farm_score", {})
    farm_pa_map = farm_maps.get("farm_pa", {})

    for name in candidate_names:
        if _is_recently_promoted(name) and name in farm_score_map:
            recent_form_score[name] = farm_score_map[name]
            recent_bat_value[name] = farm_score_map[name]
        elif name not in active_first_team and name in farm_score_map:
            recent_form_score[name] = farm_score_map[name]
            recent_bat_value[name] = farm_score_map[name]

    catcher_candidates = [
        name
        for name in candidate_names
        if POS_C in PLAYER_PROFILE.get(name, {}).get("eligible_positions", [])
    ]
    catcher_candidates.sort(
        key=lambda name: recent_bat_value.get(name, -9999.0),
        reverse=True,
    )
    top_catcher_bats = set(catcher_candidates[:2])

    result = {
        "raw_players": recent_players,
        "raw_obp_map": raw_obp_map,
        "raw_iso_map": raw_iso_map,
        "adj_obp_map": adj_obp_map,
        "adj_iso_map": adj_iso_map,
        "pa_map": pa_map,
        "ab_map": ab_map,
        "sample_weight": sample_weight_map,
        "obp_z": obp_z,
        "iso_z": iso_z,
        "recent_form_score": recent_form_score,
        "recent_bat_value": recent_bat_value,
        "top_catcher_bats": list(top_catcher_bats),
        "farm_score_map": farm_score_map,
        "farm_pa_map": farm_pa_map,
        "active_first_team": list(active_first_team),
    }

    bucket[cache_key] = {
        "value": result,
        "expires_at": _cache_now() + CACHE_TTL_RECENT_BATTING,
    }

    return result


def _build_season_position_score_map(candidate_names: list[str], position: str) -> dict[str, float]:
    adj_obp_map: dict[str, float] = {}
    adj_iso_map: dict[str, float] = {}

    for name in candidate_names:
        adj = _get_adjusted_position_batting(name, position)
        adj_obp_map[name] = adj["obp"]
        adj_iso_map[name] = adj["iso"]

    obp_z = _zscore_map(adj_obp_map)
    iso_z = _zscore_map(adj_iso_map)

    return {
        name: 0.60 * obp_z.get(name, 0.0) + 0.40 * iso_z.get(name, 0.0)
        for name in candidate_names
    }


def _slot_score(
    player_name: str,
    slot: dict,
    chosen_position: str,
    recent_maps: dict,
    season_pos_score_maps: dict[str, dict[str, float]],
) -> float:
    eligible = PLAYER_PROFILE.get(player_name, {}).get("eligible_positions", [])
    if chosen_position not in eligible:
        return -1000000.0

    sample_weight = recent_maps["sample_weight"].get(player_name, 0.0)
    recent_form = recent_maps["recent_form_score"].get(player_name, 0.0)
    recent_obp_z = recent_maps["obp_z"].get(player_name, 0.0) * sample_weight
    recent_iso_z = recent_maps["iso_z"].get(player_name, 0.0) * sample_weight
    defense_map = _get_player_defense()
    defense_score = _safe_float(
        defense_map.get(player_name, {}).get(chosen_position, 0.0)
    ) or 0.0
    season_pos_score = season_pos_score_maps.get(chosen_position, {}).get(player_name, 0.0)
    recent_pa = int(recent_maps["pa_map"].get(player_name, 0) or 0)
    top_catcher_bats = set(recent_maps.get("top_catcher_bats", []))

    weights = slot["weights"]

    score = (
        recent_form * weights.get("recent", 0.0)
        + defense_score * weights.get("defense", 0.0)
        + season_pos_score * weights.get("season_pos", 0.0)
    )

    role = slot["role"]

    if role == "lead_obp_glove":
        score += 0.30 * recent_obp_z + 0.15 * defense_score
    elif role == "two_hole_bat":
        score += 0.30 * recent_obp_z + 0.30 * recent_iso_z
    elif role == "three_hole_iso_glove":
        score += 0.15 * recent_obp_z + 0.30 * recent_iso_z + 0.10 * defense_score
    elif role == "cleanup_bat":
        score += 0.15 * recent_obp_z + 0.40 * recent_iso_z
    elif role == "five_hole_power":
        score += 0.05 * recent_obp_z + 0.35 * recent_iso_z
    elif role == "six_hole_balance":
        score += 0.20 * recent_obp_z + 0.20 * defense_score
    elif role == "glove_bottom":
        score += 0.25 * defense_score - 0.05 * recent_obp_z - 0.05 * recent_iso_z
    elif role == "turnover_obp":
        score += 0.25 * recent_obp_z + 0.10 * defense_score

    if chosen_position == POS_C and recent_pa < MIN_CATCHER_RECENT_PA:
        score -= WEAK_CATCHER_PENALTY

    if chosen_position == POS_DH and player_name in top_catcher_bats:
        score -= TOP_CATCHER_TO_DH_PENALTY

    if chosen_position == POS_C and player_name in top_catcher_bats:
        score += TOP_CATCHER_AT_C_BONUS

    if sample_weight < 0.5:
        score -= (0.5 - sample_weight) * 0.8

    if defense_score < slot.get("min_defense", -999):
        score -= slot.get("low_defense_penalty", 0.0)

    return round(score, 3)


def _build_slot_reason(
    player_name: str,
    chosen_position: str,
    slot: dict,
    recent_maps: dict,
) -> str:
    canonical_name = _canonical_player_name(player_name)

    adj = _get_adjusted_position_batting(canonical_name, chosen_position)
    adjusted_obp = _safe_float(adj.get("obp")) or 0.0
    adjusted_iso = _safe_float(adj.get("iso")) or 0.0
    defense_map = _get_player_defense()
    defense_score = _safe_float(
        defense_map.get(canonical_name, {}).get(chosen_position, 0.0)
    ) or 0.0
    recent_form = recent_maps.get("recent_form_score", {}).get(canonical_name, 0.0)

    parts = [
        f"直近スコア {recent_form:.3f}",
        f"今期補正OBP {adjusted_obp:.3f}",
        f"今期補正ISO {adjusted_iso:.3f}",
        f"守備スコア {defense_score:.3f}",
    ]
    return " / ".join(parts)


def _fetch_html(url: str) -> str:
    req = Request(url, headers={"User-Agent": "Mozilla/5.0"})
    return urlopen(req, timeout=20).read().decode("utf-8", errors="ignore")


class _TopLevelTableParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.tables: list[list[list[str]]] = []
        self._table_depth = 0
        self._current_table: list[list[str]] | None = None
        self._current_row: list[str] | None = None
        self._current_cell: list[str] | None = None
        self._in_cell = False

    def handle_starttag(self, tag: str, attrs) -> None:
        if tag == "table":
            self._table_depth += 1
            if self._table_depth == 1:
                self._current_table = []
            return

        if self._table_depth != 1:
            return

        if tag == "tr":
            self._current_row = []
        elif tag in ("td", "th"):
            self._in_cell = True
            self._current_cell = []
        elif tag == "br" and self._in_cell and self._current_cell is not None:
            self._current_cell.append(" ")

    def handle_endtag(self, tag: str) -> None:
        if tag == "table":
            if self._table_depth == 1 and self._current_table is not None:
                self.tables.append(self._current_table)
                self._current_table = None
            self._table_depth -= 1
            return

        if self._table_depth != 1:
            return

        if tag in ("td", "th"):
            if self._current_row is not None and self._current_cell is not None:
                cell_text = _clean_text("".join(self._current_cell))
                self._current_row.append(cell_text)
            self._in_cell = False
            self._current_cell = None

        elif tag == "tr":
            if self._current_table is not None and self._current_row is not None:
                if any(cell != "" for cell in self._current_row):
                    self._current_table.append(self._current_row)
            self._current_row = None

    def handle_data(self, data: str) -> None:
        if self._table_depth == 1 and self._in_cell and self._current_cell is not None:
            self._current_cell.append(data)


def _extract_tables(html: str) -> list[list[list[str]]]:
    parser = _TopLevelTableParser()
    parser.feed(html)
    parser.close()
    return parser.tables


def _normalize_opponent_name(name: str) -> str:
    name = _clean_text(name)
    name = name.replace("　", "").replace(" ", "")
    if name in ("ＤｅＮＡ", "DeNA", "横浜DeNA", "横浜ＤｅＮＡ"):
        return "DeNA"
    return name


def _is_home_game(venue: str) -> bool:
    venue = _clean_text(venue)
    return any(keyword in venue for keyword in HOME_VENUE_KEYWORDS)


def _parse_date_cell(date_cell: str, current_month: int | None) -> tuple[int, int] | None:
    value = _clean_text(date_cell).replace(" ", "")
    if not value:
        return None

    if "/" in value:
        parts = value.split("/")
        if len(parts) != 2:
            return None
        month = _safe_int(parts[0])
        day = _safe_int(parts[1])
        if month <= 0 or day <= 0:
            return None
        return month, day

    if current_month is None:
        return None

    day = _safe_int(value)
    if day <= 0:
        return None

    return current_month, day


def _find_results_table(tables: list[list[list[str]]]) -> list[list[str]]:
    for table in tables:
        if not table:
            continue
        header = [_clean_text(cell) for cell in table[0]]
        if "月日" in header and "対戦球団" in header and "回戦" in header and "球場" in header:
            return table
    return []


def _extract_current_scoreboard_game_urls(html: str) -> list[str]:
    links = re.findall(r'href="(/scores/\d{4}/\d{4}/[a-z]{1,2}-[a-z]{1,2}-\d{2}/)"', html)
    result: list[str] = []
    for link in links:
        if "/c-" in link or "-c-" in link:
            absolute = f"https://npb.jp{link}box.html"
            if absolute not in result:
                result.append(absolute)
    return result


def _build_game_meta_from_box_url(box_url: str) -> dict:
    m = re.search(r"/scores/(\d{4})/(\d{2})(\d{2})/([a-z]{1,2})-([a-z]{1,2})-(\d{2})/box\.html", box_url)
    if not m:
        return {
            "date": "",
            "date_sort": "",
            "opponent": "",
            "venue": "",
            "round": 0,
            "box_url": box_url,
        }

    year, mm, dd, home_code, away_code, round_no = m.groups()
    if home_code == "c":
        opponent_code = away_code
        venue = "マツダ"
    else:
        opponent_code = home_code
        venue = "ビジター"

    return {
        "date": f"{int(mm)}月{int(dd)}日",
        "date_sort": f"{year}-{mm}-{dd}",
        "opponent": TEAM_CODE_TO_NAME.get(opponent_code, opponent_code),
        "venue": venue,
        "round": int(round_no),
        "box_url": box_url,
    }


def _extract_year_from_results_page(html: str) -> str:
    m = re.search(r"(\d{4})年度", html)
    return m.group(1) if m else "2026"


def _extract_previous_results_page_url(html: str) -> str | None:
    links = re.findall(r'href="([^"]*results_c[^"]*\.html)"', html)
    for link in links:
        if "results_c_index.html" in link:
            continue
        if link.startswith("http://") or link.startswith("https://"):
            return link
        return f"https://npb.jp/bis/teams/{link.lstrip('/')}"
    return None


def _extract_result_rows_from_html(html: str) -> list[list[str]]:
    rows: list[list[str]] = []

    row_matches = re.findall(
        r"<tr[^>]*>(.*?)</tr>",
        html,
        flags=re.DOTALL | re.IGNORECASE,
    )

    for row_html in row_matches:
        cells = re.findall(
            r"<t[dh][^>]*>(.*?)</t[dh]>",
            row_html,
            flags=re.DOTALL | re.IGNORECASE,
        )
        cleaned = [_clean_text(cell) for cell in cells]
        cleaned = [cell for cell in cleaned if cell != ""]

        if len(cleaned) < 8:
            continue

        first_cell = cleaned[0].replace(" ", "")
        opponent_cell = cleaned[1].replace(" ", "").replace("　", "")

        if first_cell == "月日":
            continue

        if not re.fullmatch(r"\d{1,2}/\d{1,2}|\d{1,2}", first_cell):
            continue

        if opponent_cell not in TEAM_NAME_TO_CODE:
            continue

        rows.append(cleaned)

    return rows


def _parse_result_rows_to_games(rows: list[list[str]], year: str) -> list[dict]:
    games: list[dict] = []
    current_month: int | None = None

    for row in rows:
        if len(row) < 8:
            continue

        date_cell = _clean_text(row[0])
        if not date_cell:
            continue

        if "/" in date_cell:
            parts = date_cell.split("/")
            if len(parts) != 2:
                continue
            month = _safe_int(parts[0])
            day = _safe_int(parts[1])
            current_month = month
        else:
            if current_month is None:
                continue
            month = current_month
            day = _safe_int(date_cell)

        if month <= 0 or day <= 0:
            continue

        opponent_name = _normalize_opponent_name(row[1])
        opponent_code = TEAM_NAME_TO_CODE.get(opponent_name)
        if not opponent_code:
            continue

        round_no = _safe_int(row[2])
        if round_no <= 0:
            continue

        venue = _clean_text(row[3])
        score = _clean_text(row[6]) if len(row) > 6 else ""
        result_mark = _clean_text(row[7]) if len(row) > 7 else ""

        if not score:
            continue

        mmdd = f"{month:02d}{day:02d}"
        matchup = f"c-{opponent_code}" if _is_home_game(venue) else f"{opponent_code}-c"
        box_url = f"https://npb.jp/scores/{year}/{mmdd}/{matchup}-{round_no:02d}/box.html"

        games.append({
            "date": f"{month}月{day}日",
            "date_sort": f"{year}-{month:02d}-{day:02d}",
            "opponent": opponent_name,
            "venue": venue,
            "round": round_no,
            "score": score,
            "result": result_mark,
            "box_url": box_url,
        })

    return games


@lru_cache(maxsize=4)
def _fetch_recent_carp_games(limit: int) -> list[dict]:
    current_results_url = "https://npb.jp/bis/teams/results_c_index.html"
    current_html = _fetch_html(current_results_url)
    year = _extract_year_from_results_page(current_html)

    all_games: list[dict] = []

    current_rows = _extract_result_rows_from_html(current_html)
    all_games.extend(_parse_result_rows_to_games(current_rows, year))

    previous_url = _extract_previous_results_page_url(current_html)
    if previous_url:
        try:
            previous_html = _fetch_html(previous_url)
            previous_rows = _extract_result_rows_from_html(previous_html)
            all_games.extend(_parse_result_rows_to_games(previous_rows, year))
        except Exception:
            pass

    dedup: dict[str, dict] = {}
    for game in all_games:
        dedup[game["box_url"]] = game

    sorted_games = sorted(dedup.values(), key=lambda x: x["date_sort"])
    recent_games = sorted_games[-limit:]
    recent_games.reverse()
    return recent_games


def _is_batting_table(table: list[list[str]]) -> bool:
    if not table:
        return False
    header = table[0]
    required = {"守備", "選手", "打数", "得点", "安打", "打点", "盗塁"}
    return required.issubset(set(header))


def _analyze_plate_results(result_cells: list[str]) -> dict:
    stats = {
        "doubles": 0,
        "triples": 0,
        "homeruns": 0,
        "walks": 0,
        "hit_by_pitch": 0,
        "strikeouts": 0,
        "sacrifice_bunts": 0,
        "sacrifice_flies": 0,
    }

    for raw in result_cells:
        text = _clean_text(raw).replace(" ", "").replace("　", "")
        if text in ("", "-", "－"):
            continue

        if "四球" in text:
            stats["walks"] += 1
        if "死球" in text:
            stats["hit_by_pitch"] += 1
        if "三振" in text:
            stats["strikeouts"] += 1
        if "犠飛" in text:
            stats["sacrifice_flies"] += 1
        if "犠打" in text:
            stats["sacrifice_bunts"] += 1

        if "本" in text:
            stats["homeruns"] += 1
        elif "３" in text or "三塁打" in text:
            stats["triples"] += 1
        elif "２" in text or "二塁打" in text:
            stats["doubles"] += 1

    return stats


@lru_cache(maxsize=32)
def _parse_carp_batting_rows(box_url: str) -> list[dict]:
    html = _fetch_html(box_url)
    tables = _extract_tables(html)

    batting_tables = [table for table in tables if _is_batting_table(table)]
    if len(batting_tables) < 2:
        raise ValueError(f"打撃表を見つけられませんでした: {box_url}")

    carp_is_home = bool(re.search(r"/scores/\d{4}/\d{4}/c-[a-z]{1,2}-\d{2}/box\.html", box_url))
    carp_table = batting_tables[1] if carp_is_home else batting_tables[0]

    header = carp_table[0]
    index_map = {name: idx for idx, name in enumerate(header)}

    def cell(row: list[str], name: str) -> str:
        idx = index_map.get(name)
        if idx is None or idx >= len(row):
            return ""
        return row[idx]

    result_start_idx = index_map.get("盗塁", 7) + 1

    rows: list[dict] = []

    for row in carp_table[1:]:
        if len(row) < 3:
            continue

        player_name = _normalize_name(cell(row, "選手"))
        if not player_name or player_name == "チーム計":
            continue

        ab = _safe_int(cell(row, "打数"))
        runs = _safe_int(cell(row, "得点"))
        hits = _safe_int(cell(row, "安打"))
        rbi = _safe_int(cell(row, "打点"))
        steals = _safe_int(cell(row, "盗塁"))

        plate_results = row[result_start_idx:] if len(row) > result_start_idx else []
        extra = _analyze_plate_results(plate_results)

        rows.append({
            "player_name": player_name,
            "position": _clean_text(cell(row, "守備")),
            "at_bats": ab,
            "runs": runs,
            "hits": hits,
            "rbi": rbi,
            "steals": steals,
            "doubles": extra["doubles"],
            "triples": extra["triples"],
            "homeruns": extra["homeruns"],
            "walks": extra["walks"],
            "hit_by_pitch": extra["hit_by_pitch"],
            "strikeouts": extra
