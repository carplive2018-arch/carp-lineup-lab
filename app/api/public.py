from __future__ import annotations

from functools import lru_cache
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import json
import re
import time

from html import escape, unescape
from html.parser import HTMLParser
from urllib.parse import urljoin
from urllib.request import Request as URLRequest, urlopen

from fastapi import APIRouter, HTTPException, Request
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
CURRENT_RESULTS_URL = "https://npb.jp/bis/teams/results_c_index.html"

CURRENT_SEASON_YEAR = 2026
PRORAN_TEAM_BATTERS_URL = "https://proran.jp/team_detail_b.php?t=_c"
PRORAN_PLAYER_DETAIL_MORE_URL = "https://proran.jp/player_detail_more.php?id={player_id}&y={year}"

NPBBASEMENT_FIELDING_URL = "https://npbbasement.com/fielding"
NPBBASEMENT_BASE_URL = "https://npbbasement.com"

FIRST_TEAM_CONFIRM_HOUR = 17
FIRST_TEAM_CONFIRM_MINUTE = 30

FARM_MIN_PA = 50
FARM_DISCOUNT = 0.90
PROMOTION_GRACE_DAYS = 7

CACHE_TTL_PLAYER_DEFENSE = 60 * 60 * 12
CACHE_TTL_SEASON_POSITION_BATTING = 60 * 60 * 6
CACHE_TTL_RECENT_BATTING = 60 * 5
CACHE_TTL_PREDICTED_LINEUP = 60 * 3

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

POSITION_LABEL_TO_CODE = {
    "捕手": "C",
    "捕": "C",
    "C": "C",
    "一塁": "1B",
    "一塁手": "1B",
    "1B": "1B",
    "二塁": "2B",
    "二塁手": "2B",
    "2B": "2B",
    "三塁": "3B",
    "三塁手": "3B",
    "3B": "3B",
    "遊撃": "SS",
    "遊撃手": "SS",
    "SS": "SS",
    "左翼": "LF",
    "左翼手": "LF",
    "LF": "LF",
    "中堅": "CF",
    "中堅手": "CF",
    "CF": "CF",
    "右翼": "RF",
    "右翼手": "RF",
    "RF": "RF",
    "指名打者": "DH",
    "DH": "DH",
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
    "小園": "小園 海斗",
    "勝田成": "勝田 成",
    "勝田": "勝田 成",
    "二俣翔一": "二俣 翔一",
    "二俣": "二俣 翔一",
    "大盛穂": "大盛 穂",
    "大盛": "大盛 穂",
    "田村俊介": "田村 俊介",
    "田村": "田村 俊介",
    "矢野雅哉": "矢野 雅哉",
    "矢野": "矢野 雅哉",
    "坂倉将吾": "坂倉 将吾",
    "坂倉": "坂倉 将吾",
    "持丸泰輝": "持丸 泰輝",
    "持丸輝泰": "持丸 泰輝",
    "持丸": "持丸 泰輝",
    "菊池": "菊池 涼介",
    "菊池涼介": "菊池 涼介",
    "野間": "野間 峻祥",
    "野間峻祥": "野間 峻祥",
    "秋山": "秋山 翔吾",
    "秋山翔吾": "秋山 翔吾",
    "石原": "石原 貴規",
    "石原貴規": "石原 貴規",
    "末包": "末包 昇大",
    "末包昇大": "末包 昇大",
    "堂林": "堂林 翔太",
    "堂林翔太": "堂林 翔太",
    "モンテロ": "モンテロ",
    "ファビアン": "ファビアン",
}

CACHE = {
    "player_defense": {"value": None, "expires_at": 0},
    "season_position_batting": {"value": None, "expires_at": 0},
    "recent_batting": {},
    "predicted_lineup": {},
}

SEASON_POSITION_BATTING: dict[str, dict] = {}

PLAYER_DEFENSE_FALLBACK = {
    "坂倉 将吾": {"C": 0.30, "1B": 0.20, "3B": -0.20, "DH": 0.00},
    "小園 海斗": {"SS": 0.80, "3B": 0.40},
    "菊池 涼介": {"2B": 1.50},
    "石原 貴規": {"C": 0.45},
    "持丸 泰輝": {"C": 0.10},
}

PLAYER_DEFENSE = dict(PLAYER_DEFENSE_FALLBACK)

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


def _cache_get_bucket(bucket: str) -> dict:
    return CACHE.setdefault(bucket, {})


def _clean_text(value: str) -> str:
    value = unescape(value or "")
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


def _canonical_player_name(name: str) -> str:
    normalized = _normalize_player_name(name)

    if not normalized:
        return name

    if normalized in PLAYER_NAME_ALIASES:
        return PLAYER_NAME_ALIASES[normalized]

    for full_name in PLAYER_PROFILE.keys():
        if _normalize_player_name(full_name) == normalized:
            return full_name

    surname_matches = []
    for full_name in PLAYER_PROFILE.keys():
        parts = [p for p in full_name.replace("　", " ").split(" ") if p]
        if not parts:
            continue

        surname_normalized = _normalize_player_name(parts[0])
        if surname_normalized == normalized:
            surname_matches.append(full_name)

    if len(surname_matches) == 1:
        return surname_matches[0]

    return name


def _normalize_name(value: str) -> str:
    value = _clean_text(value)
    value = value.replace(" ", "").replace("　", "")
    return value


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


def _fetch_text(url: str) -> str:
    req = URLRequest(
        url,
        headers={
            "User-Agent": "Mozilla/5.0",
            "Accept-Language": "ja,en-US;q=0.9,en;q=0.8",
        },
    )
    with urlopen(req, timeout=20) as res:
        return res.read().decode("utf-8", errors="ignore")

def _is_pitcher_for_display(name: str) -> bool:
    canonical = _canonical_player_name(name)

    profile = PLAYER_PROFILE.get(canonical, {}) if "PLAYER_PROFILE" in globals() else {}

    for key in ("position", "primary_position", "main_position", "pos"):
        value = str(profile.get(key, "")).strip().upper()
        if value in {"P", "SP", "RP", "投手", "PITCHER"}:
            return True

    pitcher_names = {
        "栗林", "栗林 良吏",
        "床田", "床田 寛樹",
        "玉村", "玉村 昇悟",
        "岡本", "岡本 駿",
        "ハーン",
        "中﨑", "中崎", "中﨑 翔太", "中崎 翔太",
        "塹江", "塹江 敦哉",
        "常廣", "常廣 羽也斗",
        "森浦", "森浦 大輔",
        "益田", "益田 武尚",
        "赤木", "赤木 翔太",
        "遠藤", "遠藤 淳志",
        "髙", "高", "髙 太一", "高 太一",
    }

    return canonical in pitcher_names or name in pitcher_names


def _fetch_html(url: str) -> str:
    return _fetch_text(url)


def _extract_proran_position_table(html_text: str) -> dict[str, dict[str, float]]:
    result: dict[str, dict[str, float]] = {}

    if not html_text:
        print("DEBUG_PRORAN_POSITION_EMPTY_HTML")
        return result

    def _clean_label(text: str) -> str:
        text = _clean_text(text or "")
        text = re.sub(r"[（(].*?[）)]", "", text).strip()
        return text

    def _to_float(text: str) -> float:
        text = _clean_text(text or "")
        if not text:
            return 0.0
        m = re.search(r"([0-9]*\.[0-9]+|[0-9]+)", text)
        return float(m.group(1)) if m else 0.0

    th_matches = re.findall(
        r'<div class="player_detail_more_th">(.*?)</div>',
        html_text,
        flags=re.S | re.I,
    )
    ba_matches = re.findall(
        r'<div class="player_detail_more_ba">(.*?)</div>',
        html_text,
        flags=re.S | re.I,
    )

    raw_labels = [_clean_label(x) for x in th_matches if _clean_label(x)]
    raw_values = [_clean_label(x) for x in ba_matches]

    row_size = 5

    for i, raw_label in enumerate(raw_labels):
        position = POSITION_LABEL_TO_CODE.get(raw_label, "")
        if not position:
            continue

        base = i * row_size
        row = raw_values[base:base + row_size]
        if len(row) < 5:
            continue

        ab = _to_float(row[1])
        avg = _to_float(row[2])
        obp = _to_float(row[3])
        ops = _to_float(row[4])

        if ab <= 0 and obp <= 0 and ops <= 0:
            continue

        slg = max(0.0, ops - obp)
        iso = max(0.0, slg - avg)

        result[position] = {
            "pa": ab,
            "ab": ab,
            "avg": round(avg, 3),
            "obp": round(obp, 3),
            "ops": round(ops, 3),
            "iso": round(iso, 3),
        }

    print("DEBUG_PRORAN_POSITION_RESULT", result)
    return result


@lru_cache(maxsize=1)
def _discover_proran_player_ids() -> dict[str, str]:
    html = _fetch_text(PRORAN_TEAM_BATTERS_URL)
    result: dict[str, str] = {}

    patterns = [
        r'href=["\']\./player_detail\.php\?id=(\d+)(?:&[^"\']*)?["\'][^>]*>(.*?)</a>',
        r'href=["\']/player_detail\.php\?id=(\d+)(?:&[^"\']*)?["\'][^>]*>(.*?)</a>',
        r'href=["\']player_detail\.php\?id=(\d+)(?:&[^"\']*)?["\'][^>]*>(.*?)</a>',
        r'href=["\']\./player_detail_more\.php\?id=(\d+)(?:&[^"\']*)?["\'][^>]*>(.*?)</a>',
        r'href=["\']/player_detail_more\.php\?id=(\d+)(?:&[^"\']*)?["\'][^>]*>(.*?)</a>',
        r'href=["\']player_detail_more\.php\?id=(\d+)(?:&[^"\']*)?["\'][^>]*>(.*?)</a>',
    ]

    for pattern in patterns:
        for player_id, raw_name in re.findall(pattern, html, flags=re.S | re.I):
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

    print("DEBUG_PRORAN_PLAYER_IDS_COUNT", len(result))
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

    fallback = SEASON_POSITION_BATTING if isinstance(SEASON_POSITION_BATTING, dict) else {}
    CACHE["season_position_batting"] = {
        "expires_at": _cache_now() + 60,
        "value": fallback,
    }
    return fallback


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
def _get_adjusted_position_batting(player_name: str, position: str) -> dict:
    global SEASON_POSITION_BATTING

    if not isinstance(SEASON_POSITION_BATTING, dict) or not SEASON_POSITION_BATTING:
        SEASON_POSITION_BATTING = _get_season_position_batting() or {}

    canonical_name = _canonical_player_name(player_name)
    normalized_name = _normalize_player_name(canonical_name)

    player_stats = (
        SEASON_POSITION_BATTING.get(canonical_name)
        or SEASON_POSITION_BATTING.get(normalized_name)
        or {}
    )

    overall = (
        SEASON_OVERALL_BATTING.get(canonical_name)
        or SEASON_OVERALL_BATTING.get(normalized_name)
        or {"obp": 0.0, "iso": 0.0}
    )
    overall_obp = float(overall.get("obp", 0.0) or 0.0)
    overall_iso = float(overall.get("iso", 0.0) or 0.0)

    if player_stats.get("__empty__"):
        return {
            "pa": 0.0,
            "ab": 0.0,
            "obp": _round3(overall_obp),
            "iso": _round3(overall_iso),
        }

    if not player_stats:
        player_ids = _get_proran_player_ids()
        player_id = player_ids.get(normalized_name)

        if player_id:
            try:
                fetched = _fetch_proran_position_batting(canonical_name, player_id)

                if fetched:
                    SEASON_POSITION_BATTING[canonical_name] = fetched
                    SEASON_POSITION_BATTING[normalized_name] = fetched
                    player_stats = fetched
                else:
                    empty_marker = {"__empty__": True}
                    SEASON_POSITION_BATTING[canonical_name] = empty_marker
                    SEASON_POSITION_BATTING[normalized_name] = empty_marker
                    player_stats = empty_marker

                CACHE["season_position_batting"] = {
                    "value": dict(SEASON_POSITION_BATTING),
                    "expires_at": _cache_now() + 60,
                }

            except Exception as e:
                print("DEBUG_PRORAN_FETCH_ERROR", canonical_name, str(e))
                player_stats = {}

    if player_stats.get("__empty__"):
        return {
            "pa": 0.0,
            "ab": 0.0,
            "obp": _round3(overall_obp),
            "iso": _round3(overall_iso),
        }

    pos_stats = (player_stats or {}).get(position, {})
    if not pos_stats:
        return {
            "pa": 0.0,
            "ab": 0.0,
            "obp": _round3(overall_obp),
            "iso": _round3(overall_iso),
        }

    pa = float(pos_stats.get("pa", pos_stats.get("ab", 0.0)) or 0.0)
    ab = float(pos_stats.get("ab", 0.0) or 0.0)
    raw_obp = float(pos_stats.get("obp", 0.0) or 0.0)
    raw_iso = float(pos_stats.get("iso", 0.0) or 0.0)

    adj_obp_den = pa + POSITION_BATTING_PRIOR_PA
    adj_iso_den = ab + POSITION_BATTING_PRIOR_AB

    adjusted_obp = (
        ((pa * raw_obp) + (POSITION_BATTING_PRIOR_PA * overall_obp)) / adj_obp_den
        if adj_obp_den > 0
        else overall_obp
    )
    adjusted_iso = (
        ((ab * raw_iso) + (POSITION_BATTING_PRIOR_AB * overall_iso)) / adj_iso_den
        if adj_iso_den > 0
        else overall_iso
    )

    return {
        "pa": pa,
        "ab": ab,
        "obp": _round3(adjusted_obp),
        "iso": _round3(adjusted_iso),
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
    except Exception as e:
        print("DEBUG_FIRST_TEAM_MEMBER_ERROR", str(e))

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

    except Exception as e:
        print("DEBUG_FARM_SCORE_ERROR", str(e))
        return {
            "farm_score": {},
            "farm_pa": {},
        }
def _normalize_opponent_name(name: str) -> str:
    name = _clean_text(name)
    name = name.replace("　", "").replace(" ", "")
    if name in ("ＤｅＮＡ", "DeNA", "横浜DeNA", "横浜ＤｅＮＡ"):
        return "DeNA"
    return name


def _is_home_game(venue: str) -> bool:
    venue = _clean_text(venue)
    return any(keyword in venue for keyword in HOME_VENUE_KEYWORDS)


def _extract_year_from_results_page(html: str) -> str:
    m = re.search(r"(\d{4})年度", html)
    return m.group(1) if m else str(CURRENT_SEASON_YEAR)


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
    current_html = _fetch_html(CURRENT_RESULTS_URL)
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
        except Exception as e:
            print("DEBUG_PREVIOUS_RESULTS_ERROR", str(e))

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
            "strikeouts": extra["strikeouts"],
            "sacrifice_bunts": extra["sacrifice_bunts"],
            "sacrifice_flies": extra["sacrifice_flies"],
        })

    return rows


def _aggregate_recent_batting_stats(window_games: int) -> dict:
    cache_bucket = _cache_get_bucket("recent_batting")
    cache_key = f"aggregate:{window_games}"
    cache_entry = cache_bucket.get(cache_key)

    if _cache_alive(cache_entry):
        cached_value = cache_entry.get("value")
        if isinstance(cached_value, dict):
            return cached_value

    games = _fetch_recent_carp_games(window_games)

    player_totals: dict[str, dict] = {}
    team_totals = {
        "at_bats": 0,
        "runs": 0,
        "hits": 0,
        "rbi": 0,
        "steals": 0,
        "doubles": 0,
        "triples": 0,
        "homeruns": 0,
        "walks": 0,
        "hit_by_pitch": 0,
        "strikeouts": 0,
        "sacrifice_bunts": 0,
        "sacrifice_flies": 0,
    }

    def _empty_stat_line(player_name: str) -> dict:
        return {
            "player_name": player_name,
            "games": 0,
            "at_bats": 0,
            "runs": 0,
            "hits": 0,
            "rbi": 0,
            "steals": 0,
            "doubles": 0,
            "triples": 0,
            "homeruns": 0,
            "walks": 0,
            "hit_by_pitch": 0,
            "strikeouts": 0,
            "sacrifice_bunts": 0,
            "sacrifice_flies": 0,
        }

    for game in games:
        try:
            rows = _parse_carp_batting_rows(game["box_url"])
        except Exception as e:
            print("DEBUG_RECENT_GAME_PARSE_ERROR", game.get("box_url"), str(e))
            continue

        seen_in_game: set[str] = set()

        for row in rows:
            canonical_name = _canonical_player_name(row.get("player_name", ""))
            if not canonical_name:
                continue

            stat_line = player_totals.setdefault(
                canonical_name,
                _empty_stat_line(canonical_name),
            )

            if canonical_name not in seen_in_game:
                stat_line["games"] += 1
                seen_in_game.add(canonical_name)

            for key in [
                "at_bats",
                "runs",
                "hits",
                "rbi",
                "steals",
                "doubles",
                "triples",
                "homeruns",
                "walks",
                "hit_by_pitch",
                "strikeouts",
                "sacrifice_bunts",
                "sacrifice_flies",
            ]:
                value = int(row.get(key, 0) or 0)
                stat_line[key] += value
                team_totals[key] += value

    result = {
        "games": games,
        "player_totals": player_totals,
        "team_totals": team_totals,
    }

    cache_bucket[cache_key] = {
        "value": result,
        "expires_at": _cache_now() + CACHE_TTL_RECENT_BATTING,
    }
    return result


def _calc_recent_pa(stats: dict) -> int:
    return (
        int(stats.get("at_bats", 0) or 0)
        + int(stats.get("walks", 0) or 0)
        + int(stats.get("hit_by_pitch", 0) or 0)
        + int(stats.get("sacrifice_flies", 0) or 0)
    )


def _calc_recent_obp(stats: dict) -> float:
    hits = int(stats.get("hits", 0) or 0)
    ab = int(stats.get("at_bats", 0) or 0)
    walks = int(stats.get("walks", 0) or 0)
    hit_by_pitch = int(stats.get("hit_by_pitch", 0) or 0)
    sacrifice_flies = int(stats.get("sacrifice_flies", 0) or 0)

    denominator = ab + walks + hit_by_pitch + sacrifice_flies
    if denominator <= 0:
        return 0.0

    return _round3((hits + walks + hit_by_pitch) / denominator)


def _build_recent_batting_response(window_games: int) -> dict:
    cache_bucket = _cache_get_bucket("recent_batting")
    cache_key = f"response:{window_games}"
    cache_entry = cache_bucket.get(cache_key)

    if _cache_alive(cache_entry):
        cached_value = cache_entry.get("value")
        if isinstance(cached_value, dict):
            return cached_value

    aggregated = _aggregate_recent_batting_stats(window_games)

    rows = []
    for player_name, stats in aggregated.get("player_totals", {}).items():
        pa = _calc_recent_pa(stats)
        if _is_pitcher_for_display(player_name):
            continue
        if pa <= 0:
            continue

        ab = int(stats.get("at_bats", 0) or 0)
        hits = int(stats.get("hits", 0) or 0)

        avg = _round3(hits / ab) if ab > 0 else 0.0
        obp = _calc_recent_obp(stats)
        iso = _calc_iso_from_stats(stats)

        rows.append({
            "player_name": player_name,
            "games": int(stats.get("games", 0) or 0),
            "pa": pa,
            "ab": ab,
            "hits": hits,
            "runs": int(stats.get("runs", 0) or 0),
            "rbi": int(stats.get("rbi", 0) or 0),
            "steals": int(stats.get("steals", 0) or 0),
            "walks": int(stats.get("walks", 0) or 0),
            "hit_by_pitch": int(stats.get("hit_by_pitch", 0) or 0),
            "strikeouts": int(stats.get("strikeouts", 0) or 0),
            "homeruns": int(stats.get("homeruns", 0) or 0),
            "doubles": int(stats.get("doubles", 0) or 0),
            "triples": int(stats.get("triples", 0) or 0),
            "avg": avg,
            "obp": obp,
            "iso": iso,
            "defense_bonus": round(float(_defense_value_for(player_name)), 3),

        })

    rows.sort(
        key=lambda x: (
            -x["pa"],
            -x["obp"],
            -x["iso"],
            x["player_name"],
        )
    )

    result = {
        "window_games": window_games,
        "games": aggregated.get("games", []),
        "players": rows,
        "team_totals": aggregated.get("team_totals", {}),
    }

    cache_bucket[cache_key] = {
        "value": result,
        "expires_at": _cache_now() + CACHE_TTL_RECENT_BATTING,
    }
    return result


def _recent_snapshot_map(window_games: int) -> dict[str, dict]:
    aggregated = _aggregate_recent_batting_stats(window_games)
    result: dict[str, dict] = {}

    for player_name, stats in aggregated.get("player_totals", {}).items():
        canonical_name = _canonical_player_name(player_name)
        result[canonical_name] = {
            "games": int(stats.get("games", 0) or 0),
            "pa": _calc_recent_pa(stats),
            "ab": int(stats.get("at_bats", 0) or 0),
            "obp": _calc_recent_obp(stats),
            "iso": _calc_iso_from_stats(stats),
            "raw": stats,
        }

    return result


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

    for name in PROMOTED_FROM_FARM.keys():
        if _is_recently_promoted(name, now) and name not in candidates:
            candidates.append(name)

    return candidates


def _defense_value_for(name: str, position: str = "", defense_map: dict | None = None) -> float:
    canonical_name = _canonical_name(name)

    player_def = (
        defense_map.get(canonical_name)
        or defense_map.get(_normalize_name(canonical_name))
        or {}
    )
    if position in player_def:
        return float(player_def.get(position, 0.0) or 0.0)

    fallback = PLAYER_DEFENSE_FALLBACK.get(canonical_name, {})
    return float(fallback.get(position, 0.0) or 0.0)


def _slot_score(
    player_name: str,
    position: str,
    slot_def: dict,
    recent_map: dict[str, dict],
    defense_map: dict,
) -> tuple[float, dict, dict, float]:
    canonical_name = _canonical_player_name(player_name)

    recent = recent_map.get(canonical_name, {
        "games": 0,
        "pa": 0,
        "ab": 0,
        "obp": 0.0,
        "iso": 0.0,
        "raw": {},
    })
    season_pos = _get_adjusted_position_batting(canonical_name, position)
    defense = _defense_value_for(canonical_name, position, defense_map)

    recent_value = recent["obp"] * 100 + recent["iso"] * 100
    season_value = (
        float(season_pos.get("obp", 0.0) or 0.0) * 100
        + float(season_pos.get("iso", 0.0) or 0.0) * 100
    )
    defense_value = defense * 10

    weights = slot_def.get("weights", {})
    score = (
        float(weights.get("recent", 0.0) or 0.0) * recent_value
        + float(weights.get("season_pos", 0.0) or 0.0) * season_value
        + float(weights.get("defense", 0.0) or 0.0) * defense_value
    )

    min_defense = float(slot_def.get("min_defense", -999.0) or -999.0)
    penalty = float(slot_def.get("low_defense_penalty", 0.0) or 0.0)
    if defense < min_defense:
        score -= penalty

    return score, recent, season_pos, defense


def _build_simple_predicted_lineup(window_games: int, use_dh: bool) -> dict:
    cache_bucket = _cache_get_bucket("predicted_lineup")
    cache_key = f"w{window_games}:dh{int(use_dh)}"
    cache_entry = cache_bucket.get(cache_key)

    if _cache_alive(cache_entry):
        cached_value = cache_entry.get("value")
        if isinstance(cached_value, dict):
            return cached_value

    slot_defs = DH_LINEUP_SLOTS if use_dh else NO_DH_LINEUP_SLOTS
    recent_map = _recent_snapshot_map(window_games)
    defense_map = _get_player_defense()
    candidate_names = _get_prediction_candidate_names()

    used_players: set[str] = set()
    used_positions: set[str] = set()
    lineup: list[dict] = []

    for slot_def in slot_defs:
        best_pick = None
        allowed_positions = slot_def.get("allowed_positions", [])

        for player_name in candidate_names:
            canonical_name = _canonical_player_name(player_name)
            if canonical_name in used_players:
                continue

            eligible_positions = (PLAYER_PROFILE.get(canonical_name) or {}).get("eligible_positions", [])

            for position in allowed_positions:
                if position in used_positions:
                    continue
                if position not in eligible_positions:
                    continue

                score, recent, season_pos, defense = _slot_score(
                    canonical_name,
                    position,
                    slot_def,
                    recent_map,
                    defense_map,
                )

                if (best_pick is None) or (score > best_pick["score"]):
                    best_pick = {
                        "order": int(slot_def.get("order", 0) or 0),
                        "position": position,
                        "player_name": canonical_name,
                        "score": round(score, 3),
                        "recent": recent,
                        "season_pos": season_pos,
                        "defense": round(defense, 3),
                        "role": slot_def.get("role", ""),
                    }

        if best_pick is None:
            continue

        used_players.add(best_pick["player_name"])
        used_positions.add(best_pick["position"])

        recent = best_pick["recent"]
        season_pos = best_pick["season_pos"]
        position = best_pick["position"]

        reason = (
            f"直近OBP {recent['obp']:.3f} / ISO {recent['iso']:.3f}、"
            f"{position}補正OBP {float(season_pos.get('obp', 0.0) or 0.0):.3f} / "
            f"ISO {float(season_pos.get('iso', 0.0) or 0.0):.3f}、"
            f"守備補正 {best_pick['defense']:+.3f}"
        )

        lineup.append({
            "order": best_pick["order"],
            "position": position,
            "player_name": best_pick["player_name"],
            "score": best_pick["score"],
            "reason": reason,
            "recent": {
                "games": recent["games"],
                "pa": recent["pa"],
                "ab": recent["ab"],
                "obp": recent["obp"],
                "iso": recent["iso"],
            },
            "season_position": {
                "pa": float(season_pos.get("pa", 0.0) or 0.0),
                "ab": float(season_pos.get("ab", 0.0) or 0.0),
                "obp": float(season_pos.get("obp", 0.0) or 0.0),
                "iso": float(season_pos.get("iso", 0.0) or 0.0),
            },
            "defense": best_pick["defense"],
            "role": best_pick["role"],
        })

    lineup.sort(key=lambda x: x["order"])

    result = {
        "use_dh": use_dh,
        "window_games": window_games,
        "generated_at": _now_jst().isoformat(),
        "lineup": lineup,
    }

    cache_bucket[cache_key] = {
        "value": result,
        "expires_at": _cache_now() + CACHE_TTL_PREDICTED_LINEUP,
    }
    return result

def _wants_html(request: Request, view: str | None) -> bool:
    if view == "json":
        return False
    if view == "html":
        return True

    accept = (request.headers.get("accept") or "").lower()
    return "text/html" in accept


def _html_page(title: str, body: str) -> HTMLResponse:
    return HTMLResponse(
        f"""<!doctype html>
<html lang="ja">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{escape(title)}</title>
  <style>
    body {{
      margin: 0;
      background: #0b1020;
      color: #f5f7fb;
      font-family: -apple-system, BlinkMacSystemFont, "Hiragino Sans", sans-serif;
    }}
    .wrap {{
      max-width: 1100px;
      margin: 0 auto;
      padding: 24px 16px 64px;
    }}
    .hero {{
      background: linear-gradient(135deg, #121a31 0%, #172449 100%);
      border: 1px solid #26304d;
      border-radius: 20px;
      padding: 20px;
      margin-bottom: 18px;
    }}
    .hero h1 {{
      margin: 0 0 8px;
      font-size: 28px;
      line-height: 1.3;
    }}
    .muted {{
      color: #a9b5d1;
      font-size: 13px;
    }}
    .links {{
      margin-top: 12px;
      display: flex;
      gap: 8px;
      flex-wrap: wrap;
    }}
    .links a {{
      display: inline-block;
      text-decoration: none;
      color: #0b1020;
      background: #ffd54a;
      border-radius: 999px;
      padding: 8px 12px;
      font-weight: 700;
      font-size: 13px;
    }}
    .card {{
      background: #121a31;
      border: 1px solid #26304d;
      border-radius: 18px;
      padding: 18px;
      margin-top: 14px;
    }}
    .lineup-grid {{
      display: grid;
      gap: 14px;
    }}
    .slot-head {{
      display: flex;
      justify-content: space-between;
      gap: 12px;
      flex-wrap: wrap;
      align-items: baseline;
      margin-bottom: 10px;
    }}
    .order {{
      font-size: 24px;
      font-weight: 800;
      color: #ffd54a;
    }}
    .name {{
      font-size: 22px;
      font-weight: 800;
    }}
    .pos {{
      display: inline-block;
      margin-left: 8px;
      padding: 4px 8px;
      border-radius: 999px;
      background: #243154;
      color: #d8e5ff;
      font-size: 12px;
      font-weight: 700;
    }}
    .reason {{
      margin-top: 10px;
      font-size: 15px;
      line-height: 1.8;
      color: #f5f7fb;
    }}
    .stats {{
      margin-top: 14px;
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(150px, 1fr));
      gap: 10px;
    }}
    .stat {{
      background: #0f1730;
      border: 1px solid #26304d;
      border-radius: 14px;
      padding: 12px;
    }}
    .stat .label {{
      font-size: 12px;
      color: #a9b5d1;
      margin-bottom: 6px;
    }}
    .stat .value {{
      font-size: 20px;
      font-weight: 800;
    }}
    table {{
      width: 100%;
      border-collapse: collapse;
      margin-top: 12px;
      min-width: 880px;
    }}
    .table-wrap {{
      overflow-x: auto;
    }}
    th, td {{
      border-bottom: 1px solid #26304d;
      padding: 10px 8px;
      text-align: right;
      white-space: nowrap;
    }}
    th:first-child, td:first-child {{
      text-align: left;
      position: sticky;
      left: 0;
      background: #121a31;
    }}
    th {{
      color: #a9b5d1;
      font-size: 12px;
    }}
    .empty {{
      color: #a9b5d1;
      padding: 18px;
    }}
  </style>
</head>
<body>
  <div class="wrap">
    {body}
  </div>
</body>
</html>"""
    )


def _render_recent_batting_html(data: dict) -> HTMLResponse:
    rows_html = []

    for row in data.get("players", []):
        rows_html.append(
            f"""
            <tr>
              <td>{escape(str(row.get("player_name", "")))}</td>
              <td>{int(row.get("games", 0) or 0)}</td>
              <td>{int(row.get("pa", 0) or 0)}</td>
              <td>{int(row.get("ab", 0) or 0)}</td>
              <td>{int(row.get("hits", 0) or 0)}</td>
              <td>{float(row.get("avg", 0.0) or 0.0):.3f}</td>
              <td>{float(row.get("obp", 0.0) or 0.0):.3f}</td>
              <td>{float(row.get("iso", 0.0) or 0.0):.3f}</td>
              <td>{int(row.get("homeruns", 0) or 0)}</td>
              <td>{int(row.get("walks", 0) or 0)}</td>
              <td>{int(row.get("strikeouts", 0) or 0)}</td>
              <td>{float(row.get("defense_bonus", 0.0) or 0.0):+.3f}</td>
            </tr>
            """
        )

    games_html = []
    for game in data.get("games", []):
        games_html.append(
            f"""
            <div class="card">
              <div><strong>{escape(str(game.get("date", "")))}</strong> / {escape(str(game.get("opponent", "")))}</div>
              <div class="muted">{escape(str(game.get("venue", "")))} ・ {escape(str(game.get("score", "")))} {escape(str(game.get("result", "")))}</div>
            </div>
            """
        )

    body = f"""
    <div class="hero">
      <h1>直近打撃成績</h1>
      <div class="muted">直近 {int(data.get("window_games", 0) or 0)} 試合の打撃成績と守備指標です。</div>
      <div class="links">
      
       
      </div>
    </div>

    <div class="card">
      <h2>最近の試合</h2>
      {''.join(games_html) if games_html else '<div class="empty">試合データがありません</div>'}
    </div>

    <div class="card">
      <h2>選手別の直近打撃</h2>
      <div class="table-wrap">
        <table>
          <thead>
            <tr>
              <th>選手</th>
              <th>試合</th>
              <th>PA</th>
              <th>AB</th>
              <th>H</th>
              <th>AVG</th>
              <th>OBP</th>
              <th>ISO</th>
              <th>HR</th>
              <th>BB</th>
              <th>K</th>
            　<th>守備補正</th>
            </tr>
          </thead>
          <tbody>
            {''.join(rows_html) if rows_html else '<tr><td colspan="11" class="empty">データがありません</td></tr>'}
          </tbody>
        </table>
      </div>
    </div>
    """
    return _html_page("直近打撃成績", body)


def _render_predicted_lineup_html(data: dict) -> HTMLResponse:
    lineup_html = []

    for item in data.get("lineup", []):
        recent = item.get("recent", {}) or {}
        season = item.get("season_position", {}) or {}

        lineup_html.append(
            f"""
            <div class="card">
              <div class="slot-head">
                <div>
                  <span class="order">{int(item.get("order", 0) or 0)}番</span>
                  <span class="name">{escape(str(item.get("player_name", "")))}</span>
                  <span class="pos">{escape(str(item.get("position", "")))}</span>
                </div>
                <div class="muted">score {float(item.get("score", 0.0) or 0.0):.3f}</div>
              </div>

              <div class="reason">{escape(str(item.get("reason", "")))}</div>

              <div class="stats">
                <div class="stat">
                  <div class="label">直近 OBP</div>
                  <div class="value">{float(recent.get("obp", 0.0) or 0.0):.3f}</div>
                </div>
                <div class="stat">
                  <div class="label">直近 ISO</div>
                  <div class="value">{float(recent.get("iso", 0.0) or 0.0):.3f}</div>
                </div>
                <div class="stat">
                  <div class="label">ポジション補正 OBP</div>
                  <div class="value">{float(season.get("obp", 0.0) or 0.0):.3f}</div>
                </div>
                <div class="stat">
                  <div class="label">ポジション補正 ISO</div>
                  <div class="value">{float(season.get("iso", 0.0) or 0.0):.3f}</div>
                </div>
                <div class="stat">
                  <div class="label">守備補正</div>
                  <div class="value">{float(item.get("defense", 0.0) or 0.0):+.3f}</div>
                </div>
              </div>
            </div>
            """
        )

    body = f"""
    <div class="hero">
      <h1>予想打順</h1>
      <div class="muted">
        DH {('あり' if bool(data.get('use_dh', True)) else 'なし')} /
        直近 {int(data.get("window_games", 0) or 0)} 試合ベース /
        生成時刻 {escape(str(data.get("generated_at", "")))}
      </div>
      <div class="links">

        <a href="/public/recent-batting?window_games={int(data.get("window_games", 5) or 5)}">直近打撃を見る</a>
      </div>
    </div>

    <div class="lineup-grid">
      {''.join(lineup_html) if lineup_html else '<div class="card empty">打順データがありません</div>'}
    </div>
    """
    return _html_page("予想打順", body)

def _no_cache_json(data: dict) -> JSONResponse:
    return JSONResponse(
        content=data,
        headers={
            "Cache-Control": "no-store, no-cache, must-revalidate, max-age=0",
            "Pragma": "no-cache",
            "Expires": "0",
        },
    )


@router.get("/public/recent-batting")
def public_recent_batting(request: Request, window_games: int = 5, view: str | None = None):
    try:
        window_games = max(1, min(window_games, 10))
        data = _build_recent_batting_response(window_games)

        if _wants_html(request, view):
            return _render_recent_batting_html(data)

        return _no_cache_json(data)
    except Exception as e:
        return JSONResponse(
            status_code=500,
            content={
                "error": "recent-batting failed",
                "type": type(e).__name__,
                "message": str(e),
            },
        )

@router.get("/public/predicted-lineup")
def public_predicted_lineup(
    request: Request,
    window_games: int = 5,
    use_dh: bool = True,
    view: str | None = None,
):
    try:
        window_games = max(1, min(window_games, 10))
        data = _build_simple_predicted_lineup(window_games=window_games, use_dh=use_dh)

        if _wants_html(request, view):
            return _render_predicted_lineup_html(data)

        return _no_cache_json(data)
    except Exception as e:
        return JSONResponse(
            status_code=500,
            content={
                "error": "predicted-lineup failed",
                "type": type(e).__name__,
                "message": str(e),
            },
        )


