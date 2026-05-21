from __future__ import annotations

from functools import lru_cache
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from concurrent.futures import ThreadPoolExecutor, as_completed

import json
import re
import time
import math
import threading

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
# 直近成績のベイズ収縮: PA/AB が少ないほどシーズン実績値に引き寄せる
# OBP は四球・死球・犠飛を含む per-PA 指標 → PA ベース
# ISO は長打のみ per-AB 指標 → AB ベース
# PRIOR_PA/AB が大きいほど「信頼できるとみなすのに必要な打席数が多い」→ 補正が強い
RECENT_OBP_PRIOR_PA  = 18   # 旧12 → 18: 5試合程度（~20PA）でも半分以上引き戻す
RECENT_ISO_PRIOR_AB  = 25   # 旧20 → 25: ISO は長打率なので標本分散が大きい → より強い収縮
RECENT_FULL_TRUST_PA = 8
# NPBリーグ平均（prior が個人シーズン成績にない場合のフォールバック）
NPB_LEAGUE_AVG_OBP   = 0.310
NPB_LEAGUE_AVG_ISO   = 0.095
MIN_CATCHER_RECENT_PA = 4
WEAK_CATCHER_PENALTY = 1.6
TOP_CATCHER_TO_DH_PENALTY = 1.0
TOP_CATCHER_AT_C_BONUS = 0.8

JST = ZoneInfo("Asia/Tokyo")

FIRST_TEAM_MEMBERS_URL = "https://npb.jp/announcement/roster/"
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

CACHE_TTL_PLAYER_DEFENSE = 60 * 60 * 24      # 守備指標: 24時間（試合後も翌日まで有効）
CACHE_TTL_SEASON_POSITION_BATTING = 60 * 60 * 12  # シーズン打撃成績: 12時間
CACHE_TTL_RECENT_BATTING = 60 * 30           # 直近打撃成績: 30分（5分→30分）
CACHE_TTL_PREDICTED_LINEUP = 60 * 20         # 予想打順: 20分（3分→20分）
CACHE_TTL_RISP = 60 * 30                     # 得点圏打率キャッシュ: 30分（10分→30分）

YAHOO_SCHEDULE_URL = "https://baseball.yahoo.co.jp/npb/schedule/first/all"
YAHOO_GAME_TEXT_URL = "https://baseball.yahoo.co.jp/npb/game/{game_id}/text"
CARP_TEAM_ID = 6   # Yahoo baseball 広島東洋カープのチームID

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
    # 坂倉: 2026年シーズンよりサード転向がメインだが、捕手に入る可能性もあり。
    "坂倉 将吾": {"eligible_positions": [POS_3B, POS_C, POS_1B, POS_DH]},
    "小園 海斗": {"eligible_positions": [POS_SS, POS_3B]},
    "菊池 涼介": {"eligible_positions": [POS_2B]},
    "モンテロ": {"eligible_positions": [POS_1B, POS_DH]},
    "持丸 泰輝": {"eligible_positions": [POS_C, POS_DH]},
    "石原 貴規": {"eligible_positions": [POS_C]},
    "矢野 雅哉": {"eligible_positions": [POS_SS]},
    "二俣 翔一": {"eligible_positions": [POS_1B, POS_3B, POS_SS, POS_2B, POS_RF, POS_CF, POS_LF]},
    "秋山 翔吾": {"eligible_positions": [POS_LF, POS_RF]},        # 主にLF/RF。CFは大盛に譲る
    "大盛 穂":   {"eligible_positions": [POS_CF]},                 # CF専任（守備力最高）
    "野間 峻祥": {"eligible_positions": [POS_LF, POS_RF]},        # 主にLF/RF。CF大盛不在時は平川・田村
    "平川 蓮":   {"eligible_positions": [POS_LF, POS_CF, POS_RF]}, # 大盛不在時のCF候補
    "ファビアン": {"eligible_positions": [POS_LF, POS_RF, POS_DH]},
    "佐々木 泰": {"eligible_positions": [POS_1B, POS_3B, POS_DH]},
    "勝田 成": {"eligible_positions": [POS_2B, POS_SS]},
    "辰見 鴻之介": {"eligible_positions": [POS_SS, POS_2B, POS_CF]},
    "前川 誠太": {"eligible_positions": [POS_1B, POS_3B, POS_DH]},
    "林 晃汰": {"eligible_positions": [POS_LF, POS_RF, POS_DH]},
}

FARM_PROMOTION_CANDIDATES = {
    "堂林 翔太": {"eligible_positions": [POS_1B, POS_3B]},
    "末包 昇大": {"eligible_positions": [POS_LF, POS_RF, POS_DH]},
    "田村 俊介": {"eligible_positions": [POS_LF, POS_CF, POS_RF]}, # 大盛不在時のCF候補
    "中村 貴浩": {"eligible_positions": [POS_LF, POS_RF]},
    "名原 典彦": {"eligible_positions": [POS_LF, POS_CF, POS_RF]}, # 大盛不在時のCF候補
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
    "risp": {},  # 得点圏打率キャッシュ
}

SEASON_POSITION_BATTING: dict[str, dict] = {}

PLAYER_DEFENSE_FALLBACK = {
    # 坂倉: サードがメイン(+0.05)。捕手は継続してこなせるが専任捕手より劣るため+0.10に調整。
    "坂倉 将吾": {"3B": 0.05, "C": 0.10, "1B": 0.20, "DH": 0.00},
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
    "田村 俊介": {"obp": 0.310, "iso": 0.085},
    "中村 貴浩": {"obp": 0.300, "iso": 0.140},
    "名原 典彦": {"obp": 0.290, "iso": 0.090},
    "岸本 大希": {"obp": 0.290, "iso": 0.080},
    "内田 湘大": {"obp": 0.290, "iso": 0.110},
}

DH_LINEUP_SLOTS = [
    {
        # 1番：出塁最重視 → adj_obp 最高の選手を補正なしで選出
        "order": 1,
        "role": "lead_obp_glove",
        "leadoff": True,  # このフラグがある場合は adj_obp 単独で選出
        "weights": {"recent_obp": 0.35, "recent_iso": 0.07, "season_obp": 0.30, "season_iso": 0.03, "defense": 0.25},
    },
    {
        # 2番：打撃バランス（OBP＋長打）
        "order": 2,
        "role": "two_hole_bat",
        "weights": {"recent_obp": 0.25, "recent_iso": 0.25, "season_obp": 0.25, "season_iso": 0.15, "defense": 0.10},
    },
    {
        # 3番：総合打撃最強（OBP＋ISO重視）
        "order": 3,
        "role": "three_hole_contact",
        "weights": {"recent_obp": 0.30, "recent_iso": 0.25, "season_obp": 0.25, "season_iso": 0.15, "defense": 0.05},
    },
    {
        # 4番：長打力最大（直近ISO最重視）
        "order": 4,
        "role": "cleanup_power",
        "weights": {"recent_obp": 0.10, "recent_iso": 0.42, "season_obp": 0.10, "season_iso": 0.33, "defense": 0.05},
        "min_adj_iso": 0.100,  # adj_isoがこれ未満の選手は4番候補から除外
    },
    {
        # 5番：長打＋出塁（4番に次ぐ長打、直近ISO優先）
        "order": 5,
        "role": "five_hole_power",
        "weights": {"recent_obp": 0.15, "recent_iso": 0.35, "season_obp": 0.15, "season_iso": 0.25, "defense": 0.10},
        "min_adj_iso": 0.085,  # adj_isoがこれ未満の選手は5番候補から除外
    },
    {
        # 6番：総合打撃（直近ISO優先）
        "order": 6,
        "role": "six_hole_balance",
        "weights": {"recent_obp": 0.25, "recent_iso": 0.25, "season_obp": 0.25, "season_iso": 0.15, "defense": 0.10},
    },
    {
        # 7番：シーズン成績重視＋守備（直近ISO優先）
        "order": 7,
        "role": "seven_hole_season",
        "weights": {"recent_obp": 0.15, "recent_iso": 0.13, "season_obp": 0.30, "season_iso": 0.12, "defense": 0.30},
    },
    {
        # 8番：守備最重視（直近ISO優先）
        "order": 8,
        "role": "glove_bottom",
        "weights": {"recent_obp": 0.10, "recent_iso": 0.08, "season_obp": 0.20, "season_iso": 0.07, "defense": 0.55},
    },
    {
        # 9番（DH有）：繋ぎ出塁
        "order": 9,
        "role": "turnover_obp",
        "weights": {"recent_obp": 0.30, "recent_iso": 0.13, "season_obp": 0.35, "season_iso": 0.07, "defense": 0.15},
    },
]

NO_DH_LINEUP_SLOTS = [
    {
        # 1番：出塁最重視 → adj_obp 最高の選手を補正なしで選出
        "order": 1,
        "role": "lead_obp_glove",
        "leadoff": True,  # このフラグがある場合は adj_obp 単独で選出
        "weights": {"recent_obp": 0.35, "recent_iso": 0.07, "season_obp": 0.30, "season_iso": 0.03, "defense": 0.25},
    },
    {
        # 2番：打撃バランス（直近ISO優先）
        "order": 2,
        "role": "two_hole_bat",
        "weights": {"recent_obp": 0.25, "recent_iso": 0.25, "season_obp": 0.25, "season_iso": 0.15, "defense": 0.10},
    },
    {
        # 3番：総合打撃最強（直近ISO優先）
        "order": 3,
        "role": "three_hole_contact",
        "weights": {"recent_obp": 0.30, "recent_iso": 0.25, "season_obp": 0.25, "season_iso": 0.15, "defense": 0.05},
    },
    {
        # 4番：長打力最大（直近ISO最重視）
        "order": 4,
        "role": "cleanup_power",
        "weights": {"recent_obp": 0.10, "recent_iso": 0.42, "season_obp": 0.10, "season_iso": 0.33, "defense": 0.05},
        "min_adj_iso": 0.100,  # adj_isoがこれ未満の選手は4番候補から除外
    },
    {
        # 5番：長打＋出塁（直近ISO優先）
        "order": 5,
        "role": "five_hole_power",
        "weights": {"recent_obp": 0.15, "recent_iso": 0.35, "season_obp": 0.15, "season_iso": 0.25, "defense": 0.10},
        "min_adj_iso": 0.085,  # adj_isoがこれ未満の選手は5番候補から除外
    },
    {
        # 6番：総合打撃（直近ISO優先）
        "order": 6,
        "role": "six_hole_balance",
        "weights": {"recent_obp": 0.25, "recent_iso": 0.25, "season_obp": 0.25, "season_iso": 0.15, "defense": 0.10},
    },
    {
        # 7番：シーズン成績重視＋守備（直近ISO優先）
        "order": 7,
        "role": "seven_hole_season",
        "weights": {"recent_obp": 0.15, "recent_iso": 0.13, "season_obp": 0.30, "season_iso": 0.12, "defense": 0.30},
    },
    {
        # 8番（DH無）：守備最重視（直近ISO優先）
        "order": 8,
        "role": "glove_bottom",
        "weights": {"recent_obp": 0.10, "recent_iso": 0.08, "season_obp": 0.20, "season_iso": 0.07, "defense": 0.55},
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
    with urlopen(req, timeout=10) as res:
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

    def _clean(t: str) -> str:
        t = re.sub(r"<[^>]+>", "", t or "")
        t = t.replace("\u3000", "").replace("　", "").strip()
        return t

    def _to_float(text: str) -> float:
        text = _clean(text)
        if not text or text == "---":
            return 0.0
        m = re.search(r"([0-9]*\.[0-9]+|[0-9]+)", text)
        return float(m.group(1)) if m else 0.0

    # ── 「守備ポジション別成績」セクションを切り出す ──
    # 次の <h1 で終わる（対球団別成績などの直前まで）
    start_m = re.search(r"守備ポジション別成績", html_text)
    if start_m is None:
        print("DEBUG_PRORAN_POSITION_SECTION_NOT_FOUND")
        return result
    next_h1 = re.search(r"<h1", html_text[start_m.start() + 1:])
    start = start_m.start()
    end   = start + next_h1.start() + 1 if next_h1 else start + 6000
    section = html_text[start:end]

    # ── ポジション名リスト（左固定列：bg_c_th クラスのba） ──
    # 例: <div class="player_detail_more_ba border_t bg_c_th">捕手</div>
    pos_names_raw = re.findall(
        r'<div class="player_detail_more_ba[^"]*bg_c_th[^"]*">(.*?)</div>',
        section, re.S | re.I
    )
    pos_names = [_clean(p) for p in pos_names_raw]
    n_pos = len(pos_names)  # 通常9（捕手〜指名打者）
    if n_pos == 0:
        print("DEBUG_PRORAN_POSITION_NO_POSITIONS")
        return result

    # ── 列ヘッダー（player_detail_more_th） ──
    th_raw = re.findall(
        r'<div class="player_detail_more_th[^"]*">(.*?)</div>',
        section, re.S | re.I
    )
    col_headers = [_clean(t) for t in th_raw]

    # ── 値セル（bg_c_th でない ba） ──
    ba_all = re.findall(
        r'<div class="player_detail_more_ba[^"]*?(?:right|center)[^"]*?">(.*?)</div>',
        section, re.S | re.I
    )
    val_cells = [_clean(v) for v in ba_all]

    # col_headers: ['', '打数', '打率', '出塁率', 'ＯＰＳ', '安打', '本塁打', '三振率']
    # 最初の列（空）はポジション名列なのでスキップ、実データは2列目以降
    # val_cells は各列 n_pos 個ずつ並ぶ
    # 全列数 = len(col_headers)、先頭1列（空/ポジション名）はスキップ
    n_stat_cols = len(col_headers) - 1  # 空列を除いた統計列数
    # val_cells の前 n_pos 個は最初の空列のダミー（ないケースも）、残りが統計値
    # 実際は val_cells = 打数列9 + 打率列9 + 出塁率列9 + OPS列9 + ...
    expected = n_stat_cols * n_pos
    if len(val_cells) < expected:
        # フォールバック: val_cells がそのまま並んでいると仮定
        pass

    # ヘッダー名 → インデックスのマップを作る（全角文字対応）
    def _norm_header(s: str) -> str:
        return s.replace("　", "").replace("\u3000", "").replace("\n", "").replace("<br>", "")

    header_map: dict[str, int] = {}
    for idx, h in enumerate(col_headers[1:]):  # 空列スキップ
        key = _norm_header(h)
        header_map[key] = idx  # 0始まり（val_cells内のオフセット用）

    # 各ポジションの打数・打率・出塁率・OPS を取り出す
    # val_cells[col_idx * n_pos + pos_idx]
    def _get_val(col_name_candidates: list[str], pos_idx: int) -> float:
        for cand in col_name_candidates:
            for key, col_idx in header_map.items():
                if cand in key or key in cand:
                    cell_idx = col_idx * n_pos + pos_idx
                    if cell_idx < len(val_cells):
                        return _to_float(val_cells[cell_idx])
        return 0.0

    for pos_idx, pos_label in enumerate(pos_names):
        pos_code = POSITION_LABEL_TO_CODE.get(pos_label, "")
        if not pos_code:
            continue

        ab  = _get_val(["打数"],       pos_idx)
        avg = _get_val(["打率"],       pos_idx)
        obp = _get_val(["出塁率"],     pos_idx)
        ops = _get_val(["ＯＰＳ", "OPS"], pos_idx)
        hr  = _get_val(["本塁打"],     pos_idx)
        hits = _get_val(["安打"],      pos_idx)

        if ab <= 0 and obp <= 0 and ops <= 0:
            continue

        slg = max(0.0, ops - obp)
        iso = max(0.0, slg - avg)

        result[pos_code] = {
            "pa":  ab,
            "ab":  ab,
            "avg": round(avg, 3),
            "obp": round(obp, 3),
            "slg": round(slg, 3),
            "ops": round(ops, 3),
            "iso": round(iso, 3),
            "hr":  int(hr),
            "hits": int(hits),
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

    # 対象プレイヤーと player_id のペアを収集
    targets: list[tuple[str, str]] = []
    for player_name in PLAYER_PROFILE.keys():
        normalized_name = _normalize_player_name(player_name)
        player_id = player_ids.get(normalized_name)
        if player_id:
            targets.append((player_name, player_id))

    # ThreadPoolExecutor で並列 fetch（最大 8 スレッド）
    def _fetch_one(args: tuple[str, str]) -> tuple[str, dict]:
        player_name, player_id = args
        try:
            position_stats = _fetch_proran_position_batting(player_name, player_id)
            return (player_name, position_stats or {})
        except Exception as e:
            print("DEBUG_PRORAN_BUILD_ERROR", player_name, str(e))
            return (player_name, {})

    with ThreadPoolExecutor(max_workers=8) as executor:
        futures = {executor.submit(_fetch_one, t): t for t in targets}
        for future in as_completed(futures):
            player_name, position_stats = future.result()
            if position_stats:
                normalized_name = _normalize_player_name(player_name)
                result[normalized_name] = position_stats
                result[player_name] = position_stats

    return result


def _get_season_position_batting() -> dict:
    global SEASON_POSITION_BATTING

    cache_entry = CACHE.get("season_position_batting", {})
    if _cache_alive(cache_entry):
        cached_value = cache_entry.get("value")
        if isinstance(cached_value, dict):
            return cached_value

    # キャッシュ期限切れでも古いデータがあればすぐ返し、バックグラウンドで更新
    stale = cache_entry.get("value") if cache_entry else None
    if stale and isinstance(stale, dict) and stale:
        def _bg_refresh():
            try:
                data = _build_season_position_batting_from_proran()
                if data and isinstance(data, dict):
                    global SEASON_POSITION_BATTING
                    SEASON_POSITION_BATTING = data
                    CACHE["season_position_batting"] = {
                        "expires_at": _cache_now() + CACHE_TTL_SEASON_POSITION_BATTING,
                        "value": data,
                    }
            except Exception as e:
                print("DEBUG_SEASON_POSITION_BATTING_BG_ERROR", str(e))
        # 一時的に60秒延長して2重更新を防ぐ
        CACHE["season_position_batting"]["expires_at"] = _cache_now() + 60
        threading.Thread(target=_bg_refresh, daemon=True, name="bg-season-batting").start()
        return stale

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


def _decode_nb(s: str) -> str:
    """npbbasement の文字化け名前を UTF-8 に戻す（latin-1→utf-8 二重エンコード修正）"""
    try:
        return s.encode("latin-1").decode("utf-8")
    except Exception:
        return s


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
        raw_name = player.get("nameJ") or player.get("nameSponavi") or ""
        name = _normalize_player_name(_decode_nb(raw_name))
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

    # キャッシュ期限切れでも古いデータがあればすぐ返し、バックグラウンドで更新
    stale = cache_entry.get("value") if cache_entry else None
    if stale and isinstance(stale, dict) and stale:
        def _bg_refresh_defense():
            try:
                data = _build_player_defense_from_npbbasement()
                if data:
                    global PLAYER_DEFENSE
                    PLAYER_DEFENSE = data
                    CACHE["player_defense"] = {
                        "expires_at": _cache_now() + CACHE_TTL_PLAYER_DEFENSE,
                        "value": data,
                    }
            except Exception as e:
                print("DEBUG_PLAYER_DEFENSE_BG_ERROR", str(e))
        CACHE["player_defense"]["expires_at"] = _cache_now() + 60
        threading.Thread(target=_bg_refresh_defense, daemon=True, name="bg-player-defense").start()
        return stale

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
    """NPB公示「出場選手登録名簿」ページから広島の現在の一軍登録選手を取得する。

    ページ構造:
      - ページ上部: 当日の登録/抹消情報（広島枠は当日変更があった選手のみ）
      - ページ下部: 「出場選手一覧」セクション（全球団の全登録選手を掲載）
    → 「出場選手一覧」セクション内の広島ブロックを優先的に取得する。

    他球団の同姓選手との誤マッチを防ぐため姓のみ(2文字以下)のエイリアスはスキップ。
    PLAYER_PROFILE 登録済み選手名のみを返す（eligible_positions が保証されるため）。
    """
    try:
        html = _fetch_html(FIRST_TEAM_MEMBERS_URL)
        text = _clean_text(html)
        normalized = text.replace(" ", "").replace("　", "")

        # ── ① 「出場選手一覧」セクション内の広島ブロックを優先取得 ──
        # ページ下部の全登録選手一覧から広島東洋カープブロックを切り出す
        block: str | None = None
        idx_list = normalized.find("出場選手一覧")
        if idx_list >= 0:
            after_list = normalized[idx_list:]
            carp_match = re.search(
                r"広島東洋カープ(.*?)以上\d+名",
                after_list,
                re.DOTALL,
            )
            if carp_match:
                block = carp_match.group(1)
                print(f"DEBUG_FIRST_TEAM: 出場選手一覧セクションから広島ブロック取得 ({len(block)}文字)")

        # ── ② フォールバック: ページ先頭からの広島ブロック（当日変更のみ掲載の場合もある） ──
        if not block:
            carp_match = re.search(
                r"広島東洋カープ(.*?)以上\d*名",
                normalized,
                re.DOTALL,
            )
            if carp_match:
                block = carp_match.group(1)
                print(f"DEBUG_FIRST_TEAM: フォールバック(先頭ブロック)から広島ブロック取得 ({len(block)}文字)")

        # ── ③ フォールバック: 次球団名まで ──
        if not block:
            carp_match = re.search(
                r"広島東洋カープ(.*?)"
                r"(?:東京ヤクルト|中日ドラゴンズ|読売ジャイアンツ"
                r"|横浜DeNA|阪神タイガース|福岡ソフトバンク|埼玉西武"
                r"|千葉ロッテ|オリックス|東北楽天|北海道日本ハム)",
                normalized,
                re.DOTALL,
            )
            if carp_match:
                block = carp_match.group(1)
                print(f"DEBUG_FIRST_TEAM: フォールバック(次球団名区切り)から広島ブロック取得 ({len(block)}文字)")

        if not block:
            print("DEBUG_FIRST_TEAM_BLOCK_NOT_FOUND")
            return set(LAST_FIXED_FIRST_TEAM_POSITION_PLAYERS)

        # PLAYER_PROFILE 登録済み選手と広島ブロック内でマッチング
        # 姓のみ（2文字以下）のエイリアスは他球団選手との誤マッチ防止のためスキップ
        result: set[str] = set()
        for name in PLAYER_PROFILE.keys():
            for alias in _name_aliases(name):
                n = _normalize_name(alias)
                if len(n) <= 2:
                    continue
                if n in block:
                    result.add(name)
                    break

        print(f"DEBUG_FIRST_TEAM_FOUND {len(result)}名を公示ページから取得")
        if result:
            return result
    except Exception as e:
        print("DEBUG_FIRST_TEAM_MEMBER_ERROR", str(e))

    return set(LAST_FIXED_FIRST_TEAM_POSITION_PLAYERS)
def _get_active_first_team_position_players(now: datetime | None = None) -> set[str]:
    """npb.jp の一軍打撃成績ページを常時参照して一軍登録選手を返す。
    時刻制限なし（npb.jp の一軍成績ページは常に最新の登録選手を反映）。
    取得失敗時は LAST_FIXED_FIRST_TEAM_POSITION_PLAYERS にフォールバック。
    """
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


def _build_farm_score_maps() -> dict:
    """二軍打撃成績ページの全選手（50PA以上）をスキャンしてスコアを算出する。
    candidate_names 縛りなし。選手名はページ記載の正規化名をそのまま使用。
    返り値の farm_score キーは正規化済み選手名 → スコア(float)。
    """
    try:
        html = _fetch_html(FARM_BATTING_STATS_URL)
        tables = _extract_tables(html)
        table = _find_farm_batting_table(tables)

        if not table:
            return {
                "farm_score": {},
                "farm_pa": {},
                "farm_raw": {},
            }

        header = [_clean_text(cell) for cell in table[0]]
        idx = {name: i for i, name in enumerate(header)}

        def cell(row: list[str], key: str) -> str:
            i = idx.get(key)
            if i is None or i >= len(row):
                return ""
            return row[i]

        obp_map: dict[str, float] = {}
        iso_map: dict[str, float] = {}
        pa_map: dict[str, int] = {}
        raw_name_map: dict[str, str] = {}  # canonical -> 元の表記

        for row in table[1:]:
            raw_name = _clean_text(cell(row, "選手"))
            raw_name = re.sub(r"^[*+]+", "", raw_name).strip()
            if not raw_name:
                continue

            # 正規化名をキーとして使用
            canonical = _canonical_player_name(raw_name)
            if not canonical:
                canonical = raw_name

            pa = _safe_int(cell(row, "打席"))
            if pa < FARM_MIN_PA:
                continue

            ba = _safe_float(cell(row, "打率")) or 0.0
            obp = _safe_float(cell(row, "出塁率")) or 0.0
            slg = _safe_float(cell(row, "長打率")) or 0.0
            iso = max(0.0, slg - ba)

            pa_map[canonical] = pa
            obp_map[canonical] = obp
            iso_map[canonical] = iso
            raw_name_map[canonical] = raw_name

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
            "farm_raw": raw_name_map,
        }

    except Exception as e:
        print("DEBUG_FARM_SCORE_ERROR", str(e))
        return {
            "farm_score": {},
            "farm_pa": {},
            "farm_raw": {},
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


# NPB 平均 wOBA (2024〜2025 セ・リーグ参考値)
_LEAGUE_WOBA = 0.310
_WOBA_SCALE  = 1.15   # wRAA に換算するスケールファクタ
# wOBA 係数 (FanGraphs 2023 scaling を参考に NPB 向け微調整)
_WOBA_BB  = 0.69
_WOBA_HBP = 0.72
_WOBA_1B  = 0.89
_WOBA_2B  = 1.27
_WOBA_3B  = 1.62
_WOBA_HR  = 2.10
# 打撃 WAR 換算: 10 wRAA ≒ 1 WAR (簡易)
_WRAA_PER_WAR = 10.0


def _calc_woba(stats: dict, pa: int) -> float:
    """直近集計 stats から wOBA を計算して返す。"""
    if pa <= 0:
        return 0.0
    hits     = int(stats.get("hits", 0) or 0)
    doubles  = int(stats.get("doubles", 0) or 0)
    triples  = int(stats.get("triples", 0) or 0)
    hr       = int(stats.get("homeruns", 0) or 0)
    bb       = int(stats.get("walks", 0) or 0)
    hbp      = int(stats.get("hit_by_pitch", 0) or 0)
    singles  = hits - doubles - triples - hr
    woba = (
        _WOBA_BB  * bb
      + _WOBA_HBP * hbp
      + _WOBA_1B  * singles
      + _WOBA_2B  * doubles
      + _WOBA_3B  * triples
      + _WOBA_HR  * hr
    ) / pa
    return _round3(woba)


def _calc_war_batting(stats: dict, pa: int, defense_bonus: float) -> float:
    """wOBA ベースの簡易打撃 WAR を計算して返す。
    wRAA = (wOBA - league_wOBA) / wOBA_scale × PA
    守備 run = defense_bonus × PA / 9  (1 試合 9 打席換算)
    WAR  = (wRAA + defense_run) / _WRAA_PER_WAR
    """
    if pa <= 0:
        return 0.0
    woba = _calc_woba(stats, pa)
    wraa = (woba - _LEAGUE_WOBA) / _WOBA_SCALE * pa
    defense_run = defense_bonus * pa / 9.0
    war = (wraa + defense_run) / _WRAA_PER_WAR
    return round(war, 2)


def _build_recent_batting_response(window_games: int) -> dict:
    cache_bucket = _cache_get_bucket("recent_batting")
    cache_key = f"response:{window_games}"
    cache_entry = cache_bucket.get(cache_key)

    if _cache_alive(cache_entry):
        cached_value = cache_entry.get("value")
        if isinstance(cached_value, dict):
            return cached_value

    # stale-while-revalidate
    stale = cache_entry.get("value") if cache_entry else None
    if stale and isinstance(stale, dict):
        def _bg_rebuild_recent():
            try:
                # aggregateキャッシュをクリアして新規取得
                agg_bucket = _cache_get_bucket("recent_batting")
                agg_bucket.pop(f"aggregate:{window_games}", None)
                aggregated = _aggregate_recent_batting_stats(window_games)
                _do_build_recent_batting(window_games, aggregated, cache_bucket, cache_key)
            except Exception as e:
                print("DEBUG_RECENT_BATTING_BG_ERROR", str(e))
        cache_bucket[cache_key] = {**cache_entry, "expires_at": _cache_now() + 60}
        threading.Thread(target=_bg_rebuild_recent, daemon=True, name=f"bg-recent-{window_games}").start()
        return stale

    aggregated = _aggregate_recent_batting_stats(window_games)
    return _do_build_recent_batting(window_games, aggregated, cache_bucket, cache_key)


def _do_build_recent_batting(window_games: int, aggregated: dict, cache_bucket: dict, cache_key: str) -> dict:

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
        # SLG = AVG + ISO;  OPS = OBP + SLG
        slg = _round3(avg + iso)
        ops = _round3(obp + slg)

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
            "slg": slg,
            "ops": ops,
            "iso": iso,
            "defense_bonus": round(float(_defense_value_for(player_name)), 3),
            "woba": _calc_woba(stats, pa),
            "war": _calc_war_batting(stats, pa, round(float(_defense_value_for(player_name)), 3)),
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
    """直近 window_games 試合の打撃スナップショットを選手名→dict で返す。

    各エントリに生の観測値 (obp/iso) に加え、ベイズ収縮済み値 (adj_obp/adj_iso) を格納する。
    打席数が少ないほどシーズン期待値に引き戻されるため、5試合・2打席の選手が
    偶然OBP=1.000を叩き出しても過大評価されなくなる。

    ベイズ収縮式:
        adj = (pa × raw + PRIOR_PA × prior_val) / (pa + PRIOR_PA)
    prior_val は SEASON_OVERALL_BATTING の個人値、なければ NPB リーグ平均。
    """
    aggregated = _aggregate_recent_batting_stats(window_games)
    result: dict[str, dict] = {}

    for player_name, stats in aggregated.get("player_totals", {}).items():
        canonical_name = _canonical_player_name(player_name)
        pa  = _calc_recent_pa(stats)
        ab  = int(stats.get("at_bats", 0) or 0)
        raw_obp = _calc_recent_obp(stats)
        raw_iso = _calc_iso_from_stats(stats)

        # ── ベイズ収縮: prior = 個人シーズン期待値 or リーグ平均 ──
        overall = (
            SEASON_OVERALL_BATTING.get(canonical_name)
            or SEASON_OVERALL_BATTING.get(player_name)
            or {}
        )
        prior_obp = float(overall.get("obp", NPB_LEAGUE_AVG_OBP) or NPB_LEAGUE_AVG_OBP)
        prior_iso = float(overall.get("iso", NPB_LEAGUE_AVG_ISO) or NPB_LEAGUE_AVG_ISO)

        # pa=0 でも prior が返るため 0 打席の選手も prior 値を持つ
        adj_obp = (pa * raw_obp + RECENT_OBP_PRIOR_PA * prior_obp) / (pa + RECENT_OBP_PRIOR_PA)
        adj_iso = (ab * raw_iso + RECENT_ISO_PRIOR_AB * prior_iso) / (ab + RECENT_ISO_PRIOR_AB)

        # 信頼度: 0.0（0打席）〜 1.0（PRIOR_PA打席以上で≒1）
        reliability = pa / (pa + RECENT_OBP_PRIOR_PA) if pa > 0 else 0.0

        result[canonical_name] = {
            "games":       int(stats.get("games", 0) or 0),
            "pa":          pa,
            "ab":          ab,
            "obp":         raw_obp,         # 表示用（生の観測値）
            "iso":         raw_iso,         # 表示用（生の観測値）
            "adj_obp":     _round3(adj_obp),  # スコア計算用（収縮済み）
            "adj_iso":     _round3(adj_iso),  # スコア計算用（収縮済み）
            "prior_obp":   _round3(prior_obp),
            "prior_iso":   _round3(prior_iso),
            "reliability": _round3(reliability),
            "raw": stats,
        }

    return result


def _get_prediction_candidate_names(now: datetime | None = None) -> list[str]:
    """打順予測の候補選手リストを返す。

    一軍候補:
        一軍メンバー登録ページに掲載されている選手のみ。
        PLAYER_PROFILE への登録有無は問わない（ページ掲載名をそのまま採用）。

    二軍候補:
        二軍打撃成績ページで50PA以上の全選手をスキャンし、
        打撃スコア（OBP/ISO z-score 合成）が最上位の1名のみを選出。
        そのスコアに FARM_DISCOUNT(0.9) を乗じて一軍選手と同一軸で比較される。
    """
    now = now or _now_jst()

    # ① 一軍候補: メンバーページのみ参照
    active_first_team = _get_active_first_team_position_players(now)
    candidates: list[str] = list(active_first_team)

    # ② 二軍候補: 50PA以上の全選手からスコア最上位1名を選出
    #    ただし一軍登録済み選手はスキップし、純粋な二軍選手のみを対象とする
    farm_maps = _build_farm_score_maps()
    farm_score = farm_maps.get("farm_score", {})

    if farm_score:
        # 一軍登録済み選手を除外した上でスコア最大の選手を1名選出
        first_team_canonical = {_canonical_player_name(n) for n in active_first_team}
        farm_score_filtered = {
            n: s for n, s in farm_score.items()
            if _canonical_player_name(n) not in first_team_canonical
        }
        if farm_score_filtered:
            best_farm_name = max(farm_score_filtered, key=lambda n: farm_score_filtered[n])
            best_farm_score = farm_score_filtered[best_farm_name]
            print(
                f"DEBUG_FARM_BEST name={best_farm_name}"
                f" score={best_farm_score:.4f}"
                f" pa={farm_maps.get('farm_pa', {}).get(best_farm_name, 0)}"
            )
            if best_farm_name not in candidates:
                candidates.append(best_farm_name)
        else:
            print("DEBUG_FARM_BEST: 二軍スキャン結果が全員一軍登録済みのため追加なし")

    return candidates

def _defense_value_for(name: str, position: str = "", defense_map: dict | None = None) -> float:
    canonical_name = _canonical_player_name(name)
    defense_map = defense_map or _get_player_defense()

    player_def = (
        defense_map.get(canonical_name)
        or defense_map.get(_normalize_player_name(canonical_name))
        or PLAYER_DEFENSE_FALLBACK.get(canonical_name)
        or {}
    )

    if not isinstance(player_def, dict) or not player_def:
        return 0.0

    if position:
        return float(player_def.get(position, 0.0) or 0.0)

    return float(max((float(v or 0.0) for v in player_def.values()), default=0.0))




def _slot_score(
    player_name: str,
    position: str,
    slot_def: dict,
    recent_map: dict[str, dict],
    defense_map: dict,
) -> tuple[float, dict, dict, float]:
    """打順スロット専用スコア計算。
    weights キー: recent_obp / recent_iso / season_obp / season_iso / defense
    各指標を 0〜100 スケールに正規化してウエイト合計で算出。
    """
    canonical_name = _canonical_player_name(player_name)

    recent = recent_map.get(canonical_name, {
        "games": 0, "pa": 0, "ab": 0,
        "obp": 0.0, "iso": 0.0,
        "adj_obp": NPB_LEAGUE_AVG_OBP, "adj_iso": NPB_LEAGUE_AVG_ISO,
        "reliability": 0.0, "raw": {},
    })
    season_pos = _get_adjusted_position_batting(canonical_name, position)
    defense    = _defense_value_for(canonical_name, position, defense_map)

    # ── スコア計算はベイズ収縮済み値を使用 ──
    # adj_obp/adj_iso: 打席数が少ない場合はシーズン期待値に引き寄せられた補正値
    # これにより「5試合2打席でOBP=1.000」のような過大評価を防ぐ
    adj_obp_val = float(recent.get("adj_obp", recent.get("obp", 0.0)) or 0.0)
    adj_iso_val = float(recent.get("adj_iso", recent.get("iso", 0.0)) or 0.0)
    raw_obp_val = float(recent.get("obp", 0.0) or 0.0)

    r_obp = adj_obp_val * 100
    r_iso = adj_iso_val * 100
    s_obp = float(season_pos.get("obp", 0.0) or 0.0) * 100
    s_iso = float(season_pos.get("iso", 0.0) or 0.0) * 100
    defv  = defense * 10   # 守備補正を同スケールに

    # ── leadoff スロット専用：adj_obp をそのままスコアとして返す ──
    # 補正なし・純粋に出塁率最高の選手を1番に起用する
    if slot_def.get("leadoff"):
        return adj_obp_val * 100, recent, season_pos, defense

    # ── OBP=0.000 ペナルティ ──
    # 直近の生OBPが 0.000（ヒット・四球・死球いずれもなし）の場合は
    # 「出塁ゼロ」として最低評価のペナルティを付与する
    # ベイズ収縮で adj_obp が prior に引き上げられても実態は0なので補正する
    if raw_obp_val == 0.0 and int(recent.get("pa", 0) or 0) > 0:
        # adj_obp を強制的に 0 に戻す（prior による下駄を剥ぐ）
        r_obp = 0.0

    # ── min_adj_iso ハードカット ──
    # 4番・5番など長打力必須スロットで、adj_isoが基準を下回る選手を除外する
    # adj_iso は直近成績をベイズ収縮した値のため「priorに引き上げられた下駄」込み
    # それでも基準未満 = 実質的に長打力がない選手
    min_adj_iso = slot_def.get("min_adj_iso")
    if min_adj_iso is not None:
        actual_adj_iso = float(recent.get("adj_iso", recent.get("iso", 0.0)) or 0.0)
        if actual_adj_iso < min_adj_iso:
            return float("-inf"), recent, season_pos, defense

    weights = slot_def.get("weights", {})
    score = (
        float(weights.get("recent_obp",  0.0) or 0.0) * r_obp
      + float(weights.get("recent_iso",  0.0) or 0.0) * r_iso
      + float(weights.get("season_obp",  0.0) or 0.0) * s_obp
      + float(weights.get("season_iso",  0.0) or 0.0) * s_iso
      + float(weights.get("defense",     0.0) or 0.0) * defv
    )

    return score, recent, season_pos, defense



def _ordinal_ja(n: int) -> str:
    """1→'1位', 2→'2位' ... """
    return f"{n}位"


def _build_ranks(all_stats: list[dict]) -> dict[str, dict[str, int]]:
    """全候補選手の各指標ランキングを事前計算。
    返り値: {player_name: {metric_key: rank}}
    """
    metrics = ["recent_obp", "recent_iso", "season_obp", "season_iso", "defense"]
    ranks: dict[str, dict[str, int]] = {s["name"]: {} for s in all_stats}

    for metric in metrics:
        # 値が 0.0 の選手はランキング対象外（欠損扱い）にする
        scored = [(s["name"], s[metric]) for s in all_stats if s[metric] != 0.0]
        scored.sort(key=lambda x: -x[1])
        for rank, (name, _) in enumerate(scored, start=1):
            ranks[name][metric] = rank

    return ranks


def _build_reason(
    player_name: str,
    position: str,
    role: str,
    recent: dict,
    season_pos: dict,
    defense: float,
    ranks: dict[str, dict[str, int]],
    window_games: int,
) -> str:
    """指標の順位を交えた日本語の根拠文を生成する。"""
    r_obp  = recent.get("obp", 0.0)
    r_iso  = recent.get("iso", 0.0)
    s_obp  = float(season_pos.get("obp", 0.0) or 0.0)
    s_iso  = float(season_pos.get("iso", 0.0) or 0.0)

    player_ranks = ranks.get(player_name, {})

    def rank_tag(metric: str) -> str:
        r = player_ranks.get(metric)
        if r and r <= 3:
            return f"（候補中{_ordinal_ja(r)}）"
        return ""

    r_obp_tag  = rank_tag("recent_obp")
    r_iso_tag  = rank_tag("recent_iso")
    s_obp_tag  = rank_tag("season_obp")
    s_iso_tag  = rank_tag("season_iso")
    def_tag    = rank_tag("defense")

    # 打順役割ごとに強調する指標を変える
    if role in ("lead_obp_glove",):
        # 1番：出塁＋守備重視
        parts = [
            f"直近{window_games}試合出塁率 {r_obp:.3f}{r_obp_tag}",
            f"シーズン{position}補正出塁率 {s_obp:.3f}{s_obp_tag}",
            f"守備補正 {defense:+.3f}{def_tag}",
        ]
    elif role in ("two_hole_bat",):
        # 2番：打撃バランス
        parts = [
            f"直近{window_games}試合出塁率 {r_obp:.3f}{r_obp_tag}",
            f"シーズン補正出塁率 {s_obp:.3f}{s_obp_tag}",
            f"直近長打率 {r_iso:.3f}{r_iso_tag}",
        ]
    elif role in ("three_hole_iso_glove",):
        # 3番：長打＋守備
        parts = [
            f"直近{window_games}試合長打率 {r_iso:.3f}{r_iso_tag}",
            f"シーズン{position}補正長打率 {s_iso:.3f}{s_iso_tag}",
            f"守備補正 {defense:+.3f}{def_tag}",
        ]
    elif role in ("cleanup_bat",):
        # 4番：長打力最重視
        parts = [
            f"直近{window_games}試合長打率 {r_iso:.3f}{r_iso_tag}",
            f"直近出塁率 {r_obp:.3f}{r_obp_tag}",
            f"シーズン補正長打率 {s_iso:.3f}{s_iso_tag}",
        ]
    elif role in ("five_hole_power",):
        # 5番：長打＋出塁
        parts = [
            f"直近{window_games}試合長打率 {r_iso:.3f}{r_iso_tag}",
            f"シーズン補正出塁率 {s_obp:.3f}{s_obp_tag}",
            f"シーズン補正長打率 {s_iso:.3f}{s_iso_tag}",
        ]
    elif role in ("six_hole_balance",):
        # 6番：総合打撃バランス
        parts = [
            f"直近{window_games}試合出塁率 {r_obp:.3f}{r_obp_tag}",
            f"直近長打率 {r_iso:.3f}{r_iso_tag}",
            f"シーズン補正出塁率 {s_obp:.3f}{s_obp_tag}",
            f"シーズン補正長打率 {s_iso:.3f}{s_iso_tag}",
        ]
    elif role in ("seven_hole_season",):
        # 7番：シーズン成績重視＋守備
        parts = [
            f"シーズン補正出塁率 {s_obp:.3f}{s_obp_tag}",
            f"シーズン補正長打率 {s_iso:.3f}{s_iso_tag}",
            f"守備補正 {defense:+.3f}{def_tag}",
            f"直近{window_games}試合出塁率 {r_obp:.3f}{r_obp_tag}",
        ]
    elif role in ("glove_bottom",):
        # 8番：守備最重視
        parts = [
            f"守備補正 {defense:+.3f}{def_tag}",
            f"シーズン補正出塁率 {s_obp:.3f}{s_obp_tag}",
            f"直近{window_games}試合出塁率 {r_obp:.3f}{r_obp_tag}",
        ]
    elif role in ("turnover_obp",):
        # 9番：繋ぎ出塁
        parts = [
            f"シーズン補正出塁率 {s_obp:.3f}{s_obp_tag}",
            f"直近{window_games}試合出塁率 {r_obp:.3f}{r_obp_tag}",
            f"守備補正 {defense:+.3f}{def_tag}",
        ]
    else:
        parts = [
            f"直近{window_games}試合出塁率 {r_obp:.3f}{r_obp_tag}",
            f"直近長打率 {r_iso:.3f}{r_iso_tag}",
            f"シーズン補正出塁率 {s_obp:.3f}{s_obp_tag}",
            f"シーズン補正長打率 {s_iso:.3f}{s_iso_tag}",
        ]

    return "、".join(parts)


# ── 打順役割ラベル（日本語） ──────────────────────────────────────────────
_ROLE_LABEL_JA: dict[str, str] = {
    "lead_obp_glove":      "1番（出塁＋守備型）",
    "two_hole_bat":        "2番（バランス型）",
    "three_hole_contact":  "3番（巧打型）",
    "cleanup_power":       "4番（長打型）",
    "five_hole_power":     "5番（長打補完型）",
    "six_hole_balance":    "6番（総合バランス型）",
    "seven_hole_season":   "7番（シーズン実績型）",
    "glove_bottom":        "8番（守備型）",
    "turnover_obp":        "9番（繋ぎ出塁型）",
}


def _build_commentary(
    player_name: str,
    position: str,
    role: str,
    recent: dict,
    season_pos: dict,
    defense: float,
    ranks: dict[str, dict[str, int]],
    window_games: int,
    score: float,
) -> str:
    """打順決定の論理的な解説文（2〜3文）を生成する。"""
    # 生の観測値（表示用）
    r_obp = recent.get("obp", 0.0)
    r_iso = recent.get("iso", 0.0)
    # ベイズ補正済み値（スコア計算に使った値）
    adj_obp = recent.get("adj_obp", r_obp)
    adj_iso = recent.get("adj_iso", r_iso)
    s_obp = float(season_pos.get("obp", 0.0) or 0.0)
    s_iso = float(season_pos.get("iso", 0.0) or 0.0)

    pa           = int(recent.get("pa", 0) or 0)
    reliability  = float(recent.get("reliability", 1.0) or 1.0)
    prior_obp    = float(recent.get("prior_obp", s_obp or NPB_LEAGUE_AVG_OBP) or NPB_LEAGUE_AVG_OBP)

    # ── 打席数に応じた信頼度注記 ──
    def _reliability_note() -> str:
        if pa == 0:
            return (
                f"（注: 直近{window_games}試合の打席データなし。"
                f"シーズン期待値 OBP={prior_obp:.3f} を基準として評価している）"
            )
        if reliability < 0.40:
            return (
                f"（注: 直近{window_games}試合は{pa}打席と少ないため、"
                f"直近OBP {r_obp:.3f} をシーズン期待値 {prior_obp:.3f} 方向へ補正し"
                f" {adj_obp:.3f} として評価している）"
            )
        if reliability < 0.65:
            return (
                f"（直近{pa}打席のデータにシーズン期待値を一部混合して評価）"
            )
        return ""  # 打席数十分 → 注記なし

    player_ranks = ranks.get(player_name, {})

    def rank_str(metric: str) -> str:
        r = player_ranks.get(metric)
        if r is None:
            return "データ参照不能"
        if r == 1:
            return "候補中トップ"
        if r == 2:
            return "候補中2位"
        if r == 3:
            return "候補中3位"
        if r <= 5:
            return f"候補中{r}位"
        return "候補中下位"

    def rank_adj(metric: str, high_word: str = "高く", low_word: str = "低め") -> str:
        r = player_ranks.get(metric)
        if r is None:
            return low_word
        return high_word if r <= 3 else low_word

    # ── role別に解説文テンプレートを分岐 ──
    if role == "lead_obp_glove":
        # 1番 = ベイズ補正済み出塁率（adj_obp）が候補中最高の選手を補正なしで選出
        sent1 = (
            f"1番打者はウェイト計算を使わず、"
            f"直近{window_games}試合のベイズ補正済み出塁率（adj_obp）が候補中最高の選手を選出する。"
        )
        sent2 = (
            f"この選手の adj_obp は {adj_obp:.3f}（{rank_str('recent_obp')}）で、"
            f"候補の中で最も出塁能力が高く、打線の起点として最適と判断した。"
        )
        sent3 = (
            f"シーズン通算の{position}補正出塁率は {s_obp:.3f} で、"
            f"直近の数値と合わせて安定した出塁が期待できる。"
        )
        return sent1 + sent2 + sent3 + _reliability_note()

    elif role == "two_hole_bat":
        # 2番スコア = recent_obp×25 + recent_iso×25 + season_obp×25 + season_iso×15 + defense×10
        sent1 = (
            f"直近{window_games}試合の出塁率 {r_obp:.3f}（{rank_str('recent_obp')}）と"
            f"長打指数 {r_iso:.3f}（{rank_str('recent_iso')}）を兼備しており、"
            f"1番走者を進める「つなぎ」と自身の長打による得点機創出を両立できる。"
        )
        sent2 = (
            f"2番スコアはOBP系（直近25%＋シーズン補正25%）とISO系を評価する設計で、"
            f"直近長打指数（25%）をシーズン補正長打率（15%）より重視している。"
            f"この選手の総合スコア {score:.1f} が候補中最高と判定された。"
        )
        return sent1 + sent2 + _reliability_note()

    elif role == "three_hole_contact":
        # 3番スコア = recent_obp×30 + recent_iso×25 + season_obp×25 + season_iso×15 + defense×5
        sent1 = (
            f"直近{window_games}試合の出塁率 {r_obp:.3f}（{rank_str('recent_obp')}）と"
            f"長打指数 {r_iso:.3f}（{rank_str('recent_iso')}）を持ち、"
            f"クリーンアップ前の3番として出塁と長打の両方を要求するスロットに適合している。"
        )
        sent2 = (
            f"3番スコアは直近OBP（30%）を最重視しつつ、"
            f"直近長打指数（25%）をシーズン補正長打率（15%）より重く評価する設計で、"
            f"現在の長打力を特に重視している。"
            f"シーズン補正出塁率 {s_obp:.3f}・補正長打率 {s_iso:.3f} も含めた"
            f"総合スコア {score:.1f} が候補中最高となり、選出した。"
        )
        return sent1 + sent2 + _reliability_note()

    elif role == "cleanup_power":
        # 4番スコア = recent_obp×10 + recent_iso×42 + season_obp×10 + season_iso×33 + defense×5
        # → ISO合計75%（直近42%がシーズン補正33%を上回る）。OBPは20%のみ
        sent1 = (
            f"4番スコアは長打指数（ISO）に重点を置いた設計で、"
            f"直近ISO（ウェイト42%）とシーズン補正ISO（33%）の合計75%が評価の中心だ。"
            f"特に直近の長打力を最重視しており、OBP系は残り20%に過ぎない。"
        )
        # 直近ISOが低い場合とそうでない場合を分ける
        if r_iso < 0.100:
            sent2 = (
                f"この選手の直近長打指数は {r_iso:.3f}（{rank_str('recent_iso')}）と振るわないが、"
                f"シーズン補正長打率 {s_iso:.3f}（{rank_str('season_iso')}）が補完し、"
                f"長打力を評価する2指標のウェイト加算後のスコア {score:.1f} が、"
                f"守備位置制約を外した候補全員の中で4番スロットへの適合度が最も高かった。"
            )
        else:
            sent2 = (
                f"直近長打指数 {r_iso:.3f}（{rank_str('recent_iso')}）が最大ウェイト42%で評価され、"
                f"シーズン補正長打率 {s_iso:.3f}（{rank_str('season_iso')}）の33%が加算された"
                f"スコア {score:.1f} が候補中最高となった。"
            )
        if r_obp == 0.0 and int(recent.get("pa", 0) or 0) > 0:
            sent3 = (
                f"ただし直近出塁率は {r_obp:.3f}（ヒット・四球・死球なし）と最低評価であり、"
                f"残り20%のOBP評価がスコアの足を引っ張っている点は留意が必要だ。"
            )
        elif r_obp < 0.200:
            sent3 = (
                f"直近出塁率 {r_obp:.3f} はやや低調で、"
                f"残り20%のOBP評価はスコアを押し下げているが、長打力の優位性が上回った。"
            )
        else:
            sent3 = (
                f"出塁率 {r_obp:.3f} も一定の水準を保っており、"
                f"残り20%のOBP評価も大きく足を引っ張らなかった点も選出の後押しとなっている。"
            )
        return sent1 + sent2 + sent3 + _reliability_note()

    elif role == "five_hole_power":
        # 5番スコア = recent_obp×15 + recent_iso×35 + season_obp×15 + season_iso×25 + defense×10
        # → ISO系60%（直近35%がシーズン補正25%を上回る）、OBP系30%
        if r_iso < 0.050:
            # 直近ISOがほぼゼロの場合：正直に説明
            sent1 = (
                f"直近{window_games}試合の長打指数は {r_iso:.3f} と低調で、"
                f"本来5番に求める直近の長打力という観点では候補中で恵まれた数値ではない。"
            )
            sent2 = (
                f"ただし5番スコアはISO系（直近35%＋シーズン補正25%）が合計60%を占め、"
                f"シーズン補正長打率 {s_iso:.3f}（{rank_str('season_iso')}）が直近不振を一定補完する。"
                f"さらに出塁率とシーズン補正OBPを合わせた30%分も加算した結果、"
                f"スコア {score:.1f} が残り候補の中で相対的に最高となり、繰り上がり選出となった。"
            )
        else:
            sent1 = (
                f"直近{window_games}試合の長打指数 {r_iso:.3f}（{rank_str('recent_iso')}）が示すように、"
                f"現在の長打力が4番に次ぐ水準にある。"
            )
            sent2 = (
                f"5番スコアはISO系（直近35%＋シーズン補正25%）が合計60%を占める設計で、"
                f"特に直近の長打力を重視している。"
                f"シーズン補正長打率 {s_iso:.3f}（{rank_str('season_iso')}）も加算した"
                f"スコア {score:.1f} が候補中最高となり、中軸5番として選出した。"
            )
        return sent1 + sent2 + _reliability_note()

    elif role == "six_hole_balance":
        # 6番スコア = recent_obp×25 + recent_iso×25 + season_obp×25 + season_iso×15 + defense×10
        sent1 = (
            f"直近{window_games}試合の出塁率 {r_obp:.3f}・長打指数 {r_iso:.3f} に加え、"
            f"シーズン補正出塁率 {s_obp:.3f}・補正長打率 {s_iso:.3f} の4指標で評価した。"
        )
        sent2 = (
            f"6番スコアはOBP系（直近25%＋補正25%）とISO系（直近25%＋補正15%）で構成され、"
            f"直近長打指数をシーズン補正長打率より重視する設計だ。"
            f"この選手のスコア {score:.1f} が残り候補の中で最高となり、6番に配置した。"
        )
        return sent1 + sent2 + _reliability_note()

    elif role == "seven_hole_season":
        # 7番スコア = recent_obp×15 + recent_iso×13 + season_obp×30 + season_iso×12 + defense×30
        sent1 = (
            f"シーズン通算の補正出塁率 {s_obp:.3f}（{rank_str('season_obp')}）と"
            f"補正長打率 {s_iso:.3f}（{rank_str('season_iso')}）が7番評価の中心となる。"
        )
        sent2 = (
            f"7番スコアはシーズン補正OBP（30%）と守備補正（30%）を重視しつつ、"
            f"ISO系では直近長打指数（13%）をシーズン補正長打率（12%）より若干重く評価する設計だ。"
            f"直近出塁率 {r_obp:.3f} に加え守備補正 {defense:+.3f} も含めたスコア {score:.1f} が"
            f"候補中最高となり、下位打線の安定役として選出した。"
        )
        return sent1 + sent2 + _reliability_note()

    elif role == "glove_bottom":
        # 8番スコア = recent_obp×10 + recent_iso×8 + season_obp×20 + season_iso×7 + defense×55
        sent1 = (
            f"8番スコアは守備補正が全ウェイトの55%を占め、守備力が選出の最大要因となる設計だ。"
            f"ISO系では直近長打指数（8%）をシーズン補正長打率（7%）より重く評価している。"
        )
        if defense > 0:
            sent2 = (
                f"この選手の守備補正 {defense:+.3f}（{rank_str('defense')}）が55%のウェイトで効き、"
                f"打撃系指標（シーズン補正OBP {s_obp:.3f}・直近OBP {r_obp:.3f}）が残り45%を補完した"
                f"結果、スコア {score:.1f} が候補中最高となった。"
            )
        elif defense == 0:
            sent2 = (
                f"この選手の守備補正は {defense:+.3f} と中立値だが、"
                f"残り候補の守備補正もほぼ同水準であるため差がつかず、"
                f"打撃系指標（シーズン補正OBP {s_obp:.3f}・直近OBP {r_obp:.3f}）の45%分で"
                f"スコア {score:.1f} が相対的に最高となり、8番に繰り上がり選出となった。"
            )
        else:
            sent2 = (
                f"守備補正 {defense:+.3f} はマイナスだが、残り候補の中では相対的に高く、"
                f"打撃系指標の45%分も加算したスコア {score:.1f} が候補中最高となった。"
            )
        return sent1 + sent2 + _reliability_note()

    elif role == "turnover_obp":
        # 9番スコア = recent_obp×30 + recent_iso×13 + season_obp×35 + season_iso×7 + defense×15
        sent1 = (
            f"9番スコアはシーズン補正OBP（35%）と直近OBP（30%）が合計65%を占め、"
            f"打線をつなぐ『出塁』が評価の最重要軸となる設計だ。"
            f"ISO系では直近長打指数（13%）をシーズン補正長打率（7%）より重視している。"
        )
        sent2 = (
            f"シーズン補正出塁率 {s_obp:.3f}（{rank_str('season_obp')}）と"
            f"直近{window_games}試合出塁率 {r_obp:.3f}（{rank_str('recent_obp')}）の"
            f"合算が主導して、スコア {score:.1f} が候補中最高となり、"
            f"イニング先頭で出塁して上位打線に繋げる9番として選出した。"
        )
        return sent1 + sent2 + _reliability_note()

    else:
        # fallback
        return (
            f"直近{window_games}試合の出塁率 {r_obp:.3f}・長打指数 {r_iso:.3f}、"
            f"シーズン補正出塁率 {s_obp:.3f}・補正長打率 {s_iso:.3f} の総合評価により、"
            f"このスロットへの割り当てスコア {score:.1f} が候補中最高となったため選出した。"
        ) + _reliability_note()


def _build_simple_predicted_lineup(window_games: int, use_dh: bool) -> dict:
    cache_bucket = _cache_get_bucket("predicted_lineup")
    cache_key = f"w{window_games}:dh{int(use_dh)}"
    cache_entry = cache_bucket.get(cache_key)

    if _cache_alive(cache_entry):
        cached_value = cache_entry.get("value")
        if isinstance(cached_value, dict):
            return cached_value

    # stale-while-revalidate: 古いキャッシュがあればすぐ返してバックグラウンドで更新
    stale = cache_entry.get("value") if cache_entry else None
    if stale and isinstance(stale, dict):
        def _bg_rebuild():
            try:
                _do_build_predicted_lineup(window_games, use_dh, cache_bucket, cache_key)
            except Exception as e:
                print("DEBUG_PREDICTED_LINEUP_BG_ERROR", str(e))
        cache_bucket[cache_key] = {**cache_entry, "expires_at": _cache_now() + 60}
        threading.Thread(target=_bg_rebuild, daemon=True, name=f"bg-lineup-{cache_key}").start()
        return stale

    return _do_build_predicted_lineup(window_games, use_dh, cache_bucket, cache_key)


def _do_build_predicted_lineup(window_games: int, use_dh: bool, cache_bucket: dict, cache_key: str) -> dict:
    slot_defs = DH_LINEUP_SLOTS if use_dh else NO_DH_LINEUP_SLOTS
    recent_map    = _recent_snapshot_map(window_games)
    defense_map   = _get_player_defense()
    candidate_names = _get_prediction_candidate_names()

    # ── 全候補の指標を事前集計してランキングを作る ──
    all_stats_for_rank: list[dict] = []
    for player_name in candidate_names:
        cname    = _canonical_player_name(player_name)
        recent_r = recent_map.get(cname, {
            "obp": 0.0, "iso": 0.0,
            "adj_obp": NPB_LEAGUE_AVG_OBP, "adj_iso": NPB_LEAGUE_AVG_ISO,
        })
        def_val  = _defense_value_for(cname, "", defense_map)
        eligible = (PLAYER_PROFILE.get(cname) or {}).get("eligible_positions", [])
        best_s_obp, best_s_iso = 0.0, 0.0
        for pos in eligible:
            sp = _get_adjusted_position_batting(cname, pos)
            best_s_obp = max(best_s_obp, float(sp.get("obp", 0.0) or 0.0))
            best_s_iso = max(best_s_iso, float(sp.get("iso", 0.0) or 0.0))
        all_stats_for_rank.append({
            "name":       cname,
            # ランキングもベイズ補正済み値で評価（打席数の少なさを反映）
            "recent_obp": float(recent_r.get("adj_obp", recent_r.get("obp", 0.0)) or 0.0),
            "recent_iso": float(recent_r.get("adj_iso", recent_r.get("iso", 0.0)) or 0.0),
            "season_obp": best_s_obp,
            "season_iso": best_s_iso,
            "defense":    def_val,
        })
    ranks = _build_ranks(all_stats_for_rank)

    # ── 全スロット × 全候補 × 全ポジションでスコアを計算し
    #    1番から順にグリーディに最高スコアの選手を割り当て ──
    used_players: set[str]   = set()
    used_positions: set[str] = set()   # 同一守備位置の重複を防ぐ
    lineup: list[dict]       = []

    for slot_def in sorted(slot_defs, key=lambda s: s["order"]):
        best_pick = None

        for player_name in candidate_names:
            canonical_name = _canonical_player_name(player_name)
            if canonical_name in used_players:
                continue

            # 守備位置はそのプレーヤーの最も高いスコアになるポジションを採用
            # ただし used_positions に含まれないポジションのみ候補とする
            # PLAYER_PROFILE 未登録の場合は DH のみ（守備位置不明なため）
            eligible_positions = (
                (PLAYER_PROFILE.get(canonical_name) or {}).get("eligible_positions", [])
                or [POS_DH]
            )
            # まだ使われていないポジションに絞る
            # NO_DH のときは DH を候補から除外する
            available_positions = [
                p for p in eligible_positions
                if p not in used_positions
                and (use_dh or p != POS_DH)
            ]
            if not available_positions:
                continue  # この選手が出場できるポジションがすべて埋まっている

            best_pos_score   = None
            best_pos         = available_positions[0]
            best_recent      = {}
            best_season_pos  = {}
            best_defense     = 0.0

            for position in available_positions:
                score, recent, season_pos, defense = _slot_score(
                    canonical_name, position, slot_def, recent_map, defense_map,
                )
                # -inf はハードカット（min_adj_iso 未達）→ このポジション/スロットは不適格
                if math.isinf(score) and score < 0:
                    continue
                if best_pos_score is None or score > best_pos_score:
                    best_pos_score  = score
                    best_pos        = position
                    best_recent     = recent
                    best_season_pos = season_pos
                    best_defense    = defense

            # 全ポジションがハードカットされた場合はこの選手をスキップ
            if best_pos_score is None:
                continue

            if best_pick is None or best_pos_score > best_pick["score"]:
                best_pick = {
                    "order":      int(slot_def.get("order", 0) or 0),
                    "position":   best_pos,
                    "player_name": canonical_name,
                    "score":      round(best_pos_score, 3),
                    "recent":     best_recent,
                    "season_pos": best_season_pos,
                    "defense":    round(best_defense, 3),
                    "role":       slot_def.get("role", ""),
                }

        # ── フォールバック：min_adj_iso ハードカットで全候補が弾かれた場合 ──
        # min_adj_iso 制約を外して「最もISOが高い残り選手」を割り当てる
        if best_pick is None and slot_def.get("min_adj_iso") is not None:
            fallback_slot = {k: v for k, v in slot_def.items() if k != "min_adj_iso"}
            for player_name in candidate_names:
                canonical_name = _canonical_player_name(player_name)
                if canonical_name in used_players:
                    continue
                eligible_positions = (
                    (PLAYER_PROFILE.get(canonical_name) or {}).get("eligible_positions", [])
                    or [POS_DH]
                )
                available_positions = [
                    p for p in eligible_positions
                    if p not in used_positions
                    and (use_dh or p != POS_DH)
                ]
                if not available_positions:
                    continue
                best_pos_score  = None
                best_pos        = available_positions[0]
                best_recent     = {}
                best_season_pos = {}
                best_defense    = 0.0
                for position in available_positions:
                    score, recent, season_pos, defense = _slot_score(
                        canonical_name, position, fallback_slot, recent_map, defense_map,
                    )
                    if math.isinf(score) and score < 0:
                        continue
                    if best_pos_score is None or score > best_pos_score:
                        best_pos_score  = score
                        best_pos        = position
                        best_recent     = recent
                        best_season_pos = season_pos
                        best_defense    = defense
                if best_pos_score is None:
                    continue
                if best_pick is None or best_pos_score > best_pick["score"]:
                    best_pick = {
                        "order":       int(slot_def.get("order", 0) or 0),
                        "position":    best_pos,
                        "player_name": canonical_name,
                        "score":       round(best_pos_score, 3),
                        "recent":      best_recent,
                        "season_pos":  best_season_pos,
                        "defense":     round(best_defense, 3),
                        "role":        slot_def.get("role", ""),
                    }

        if best_pick is None:
            continue

        used_players.add(best_pick["player_name"])
        used_positions.add(best_pick["position"])  # 使用済みポジションに追加

        recent     = best_pick["recent"]
        season_pos = best_pick["season_pos"]
        position   = best_pick["position"]
        reason = _build_reason(
            best_pick["player_name"], position, best_pick["role"],
            recent, season_pos, best_pick["defense"], ranks, window_games,
        )
        commentary = _build_commentary(
            best_pick["player_name"], position, best_pick["role"],
            recent, season_pos, best_pick["defense"], ranks, window_games,
            best_pick["score"],
        )
        lineup.append({
            "order":    best_pick["order"],
            "position": position,
            "player_name": best_pick["player_name"],
            "score":    best_pick["score"],
            "reason":   reason,
            "commentary": commentary,
            "recent": {
                "games": recent["games"], "pa": recent["pa"],
                "ab":    recent["ab"],    "obp": recent["obp"], "iso": recent["iso"],
            },
            "season_position": {
                "pa":  float(season_pos.get("pa",  0.0) or 0.0),
                "ab":  float(season_pos.get("ab",  0.0) or 0.0),
                "obp": float(season_pos.get("obp", 0.0) or 0.0),
                "iso": float(season_pos.get("iso", 0.0) or 0.0),
            },
            "defense": best_pick["defense"],
            "role":    best_pick["role"],
        })

    lineup.sort(key=lambda x: x["order"])

    result = {
        "use_dh":       use_dh,
        "window_games": window_games,
        "generated_at": _now_jst().isoformat(),
        "lineup":       lineup,
    }

    cache_bucket[cache_key] = {
        "value":      result,
        "expires_at": _cache_now() + CACHE_TTL_PREDICTED_LINEUP,
    }
    return result


def _wants_html(request: Request, view: str | None) -> bool:
    if view == "json":
        return False
    if view in ("html", "season"):
        return True

    accept = (request.headers.get("accept") or "").lower()
    return "text/html" in accept


def _html_page(title: str, body: str, description: str = "") -> HTMLResponse:
    _desc = description or "広島東洋カープの打撃成績・予想打順・得点圏打率・WAR・走塁守備指標をリアルタイムで分析するファンサイトです。"
    return HTMLResponse(
        f"""<!doctype html>
<html lang="ja">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{escape(title)} | 鯉男の打席分析室</title>
  <meta name="description" content="{escape(_desc)}">
  <meta name="robots" content="index, follow">
  <meta property="og:title" content="{escape(title)} | 鯉男の打席分析室">
  <meta property="og:description" content="{escape(_desc)}">
  <meta property="og:type" content="website">
  <meta property="og:locale" content="ja_JP">
  <style>
    /* ── リセット & ベース ── */
    *, *::before, *::after {{ box-sizing: border-box; }}
    body {{
      margin: 0;
      background: #070d1a;
      color: #e8edf8;
      font-family: -apple-system, BlinkMacSystemFont, "Hiragino Sans", "Noto Sans JP", sans-serif;
      font-size: 14px;
      line-height: 1.5;
    }}
    a {{ color: inherit; text-decoration: none; }}

    /* ── サイトヘッダー ── */
    .site-header {{
      background: #050b17;
      border-bottom: 1px solid #1a2540;
      padding: 10px 16px;
      display: flex;
      align-items: center;
      justify-content: space-between;
      position: sticky;
      top: 0;
      z-index: 100;
    }}
    .site-logo {{
      font-size: 16px;
      font-weight: 900;
      color: #ffd54a;
      letter-spacing: -0.01em;
      text-decoration: none;
    }}
    .site-logo span {{
      color: #c8d8f4;
      font-weight: 400;
      font-size: 12px;
      margin-left: 8px;
    }}
    .site-header-nav {{
      display: flex;
      gap: 16px;
      font-size: 12px;
      color: #5a6e94;
    }}
    .site-header-nav a:hover {{ color: #c8d8f4; }}

    /* ── 3カラム広告レイアウト ── */
    .page-layout {{
      display: grid;
      grid-template-columns: 160px 1fr 160px;
      gap: 0;
      max-width: 1480px;
      margin: 0 auto;
      align-items: start;
    }}
    /* 広告カラム */
    .ad-col {{
      padding: 16px 8px;
      display: flex;
      flex-direction: column;
      gap: 16px;
      position: sticky;
      top: 57px;  /* site-header の高さ */
      align-self: start;
    }}
    .ad-unit {{
      width: 160px;
      max-width: 100%;
      min-height: 600px;
      background: #0c1424;
      border: 1px dashed #1e2d50;
      border-radius: 8px;
      display: flex;
      flex-direction: column;
      align-items: center;
      justify-content: center;
      gap: 6px;
      color: #2a3a5a;
      font-size: 10px;
      text-align: center;
    }}
    .ad-unit-label {{
      font-size: 9px;
      color: #2a3a5a;
      letter-spacing: 0.1em;
      text-transform: uppercase;
    }}
    /* コンテンツカラム */
    .content-col {{
      min-width: 0;
      /* overflow:clip は sticky を壊さずに子要素の横溢れだけをクリップする */
      overflow-x: clip;
      border-left: 1px solid #0f1829;
      border-right: 1px solid #0f1829;
    }}
    /* ── 既存 .wrap をコンテンツ内に ── */
    .wrap {{
      max-width: 100%;
      padding: 20px 20px 40px;
    }}

    /* タブレット以下: 広告カラム非表示・1カラムレイアウト */
    @media (max-width: 1100px) {{
      .page-layout {{
        display: block;  /* gridを解除して単純なblock */
        max-width: 100%;
      }}
      .ad-col {{ display: none !important; }}
      .content-col {{
        border-left: none;
        border-right: none;
        width: 100%;
      }}
    }}

    /* ── ページヘッダー ── */
    .hero {{
      background: linear-gradient(135deg, #0e1628 0%, #142040 60%, #0e1a35 100%);
      border: 1px solid #1e2d50;
      border-radius: 16px;
      padding: 20px 24px 16px;
      margin-bottom: 16px;
      position: relative;
      overflow: hidden;
    }}
    .hero::before {{
      content: "";
      position: absolute;
      top: 0; left: 0; right: 0;
      height: 3px;
      background: linear-gradient(90deg, #ffd54a 0%, #ff9800 50%, #e91e63 100%);
      border-radius: 16px 16px 0 0;
    }}
    .hero h1 {{
      margin: 0 0 4px;
      font-size: 26px;
      font-weight: 800;
      letter-spacing: -0.02em;
      line-height: 1.2;
    }}
    .muted {{
      color: #8494b8;
      font-size: 12px;
      margin-top: 2px;
    }}

    /* ── スティッキーナビバー（ページナビ固定用） ── */
    .sticky-nav {{
      position: sticky;
      top: 57px;   /* site-header の高さ */
      z-index: 90;
      background: #070d1a;
      border-bottom: 1px solid #1a2540;
      padding: 6px 16px 8px;
      margin: 0 -16px;  /* wrap の padding を打ち消して端まで広げる */
    }}
    .sticky-nav .nav-bar {{
      margin-top: 0;
      border-top: none;
      padding-top: 0;
    }}

    /* ── ナビゲーション ── */
    .nav-bar {{
      margin-top: 12px;
      border-top: 1px solid #1e2d50;
      padding-top: 10px;
      /* 横スクロール対応（スマホでもはみ出さない） */
      display: flex;
      flex-wrap: nowrap;
      align-items: center;
      gap: 0;
      overflow-x: auto;
      -webkit-overflow-scrolling: touch;
      /* スクロールバー非表示 */
      scrollbar-width: none;
      -ms-overflow-style: none;
    }}
    .nav-bar::-webkit-scrollbar {{ display: none; }}
    .nav-section {{
      display: flex;
      align-items: center;
      gap: 3px;
      flex-shrink: 0;
      padding-right: 8px;
    }}
    .nav-section:not(:last-child)::after {{
      content: '';
      display: inline-block;
      width: 1px;
      height: 16px;
      background: #1e2d50;
      margin-left: 8px;
      flex-shrink: 0;
    }}
    .nav-label {{
      font-size: 9px;
      font-weight: 700;
      color: #3d4e6e;
      letter-spacing: 0.08em;
      text-transform: uppercase;
      white-space: nowrap;
      padding: 0 4px 0 2px;
      flex-shrink: 0;
    }}
    .nav-group {{
      display: flex;
      gap: 3px;
      flex-shrink: 0;
    }}
    .nav-btn {{
      display: inline-flex;
      align-items: center;
      text-decoration: none;
      color: #8aa0c8;
      background: #0d1628;
      border: 1px solid #1a2846;
      border-radius: 5px;
      padding: 4px 9px;
      font-weight: 600;
      font-size: 11.5px;
      transition: all 0.12s;
      white-space: nowrap;
      cursor: pointer;
      flex-shrink: 0;
    }}
    .nav-btn:hover {{
      background: #172038;
      border-color: #2e4070;
      color: #d8e4f8;
    }}
    .nav-btn.active {{
      background: #ffd54a;
      color: #06100a;
      border-color: #ffd54a;
      font-weight: 800;
      pointer-events: none;
    }}
    .nav-divider {{
      display: none;
    }}

    /* ── ツールチップ（指標説明） ── */
    .tip-wrap {{
      position: relative;
      display: inline-block;
    }}
    .tip-wrap .tip-icon {{
      display: inline-block;
      width: 12px;
      height: 12px;
      background: #2a3a5a;
      color: #7090c0;
      font-size: 8px;
      font-weight: 800;
      line-height: 12px;
      text-align: center;
      border-radius: 50%;
      margin-left: 3px;
      cursor: pointer;
      vertical-align: middle;
      user-select: none;
      flex-shrink: 0;
    }}
    /* tip-box は JS で body 直下に移動・position:fixed で描画するため
       ここでは共通スタイルのみ定義（display は JS 側で制御） */
    #tip-floating {{
      display: none;
      position: fixed;
      background: #0e1a30;
      border: 1px solid #2a3e60;
      border-radius: 8px;
      padding: 8px 10px;
      font-size: 11px;
      font-weight: 400;
      color: #b0c4e4;
      white-space: normal;
      min-width: 180px;
      max-width: 260px;
      z-index: 99999;
      box-shadow: 0 4px 16px rgba(0,0,0,0.6);
      pointer-events: none;
      line-height: 1.6;
      text-align: left;
      letter-spacing: 0;
      text-transform: none;
    }}

    /* ── カード ── */
    .card {{
      background: #0c1424;
      border: 1px solid #1a2540;
      border-radius: 14px;
      /* 左右 padding は 0 にして、内部コンテンツ側で padding を持つ */
      /* これにより .table-wrap が card 幅いっぱいに広がり left:0 の sticky が正確に機能する */
      padding: 20px 0;
      margin-top: 14px;
      /* テーブルの横溢れを card 内部に閉じ込めることでページ全体の横スクロールを防ぐ */
      overflow: hidden;
      /* box-sizing で border 辺を含めた幅 100% に収める */
      box-sizing: border-box;
      width: 100%;
    }}
    /* card 内の直接子要素（table-wrap 以外）に左右 padding を付ける */
    .card > *:not(.table-wrap) {{
      padding-left: 20px;
      padding-right: 20px;
    }}
    .card-title {{
      font-size: 16px;
      font-weight: 700;
      color: #c8d8f4;
      margin: 0 0 4px;
      display: flex;
      align-items: center;
      gap: 8px;
    }}
    .card-title::before {{
      content: "";
      display: inline-block;
      width: 4px;
      height: 16px;
      background: #ffd54a;
      border-radius: 2px;
    }}
    .legend {{
      font-size: 11px;
      color: #4a5878;
      margin: 8px 0 14px;
      line-height: 1.8;
    }}
    .legend b {{ color: #7a90b8; }}

    /* ── テーブル共通 ── */
    /* .table-wrap は card の padding-left/right:0 の恩恵でそのまま幅いっぱいに広がる */
    .table-wrap {{
      overflow-x: auto;
      -webkit-overflow-scrolling: touch;
      margin-top: 4px;
      border-top: 1px solid #1a2540;
    }}
    table {{
      width: 100%;
      border-collapse: collapse;
      min-width: 640px;
    }}
    thead {{ background: #0a1020; }}
    th {{
      color: #5a6e94;
      font-size: 11px;
      font-weight: 700;
      letter-spacing: 0.05em;
      padding: 10px 10px;
      text-align: right;
      border-bottom: 1px solid #1a2540;
      white-space: nowrap;
      user-select: none;
    }}
    th:first-child {{
      text-align: left;
      position: sticky;
      left: 0;
      background: #0a1020;
      z-index: 2;
    }}
    td {{
      padding: 9px 10px;
      text-align: right;
      border-bottom: 1px solid #111e35;
      white-space: nowrap;
      font-size: 13px;
    }}
    td:first-child {{
      text-align: left;
      font-weight: 600;
      color: #c8d8f4;
      position: sticky;
      left: 0;
      background: #0c1424;
      z-index: 1;
    }}
    tbody tr:nth-child(even) td {{ background: #0a1120; }}
    tbody tr:nth-child(even) td:first-child {{ background: #0a1120; }}
    tbody tr:hover td {{ background: #132040 !important; transition: background 0.1s; }}
    tbody tr:last-child td {{ border-bottom: none; }}
    .empty {{ color: #4a5878; padding: 24px; text-align: center; }}

    /* ── ソート可能ヘッダ ── */
    th.sortable {{
      cursor: pointer;
      position: relative;
      padding-right: 20px;
      transition: color 0.15s;
    }}
    th.sortable:first-child {{
      position: sticky;  /* relative の上書きを防ぐ */
    }}
    th.sortable::after {{ content: "⇅"; position: absolute; right: 5px; opacity: 0.3; font-size: 9px; }}
    th.sortable.asc::after  {{ content: "▲"; opacity: 0.9; color: #ffd54a; }}
    th.sortable.desc::after {{ content: "▼"; opacity: 0.9; color: #ffd54a; }}
    th.sortable:hover {{ color: #ffd54a; }}

    /* ── 強調カラム ── */
    .col-gold  {{ color: #ffd54a; font-weight: 700; }}
    .col-cyan  {{ color: #56cff8; font-weight: 700; }}
    .col-green {{ color: #5ce65c; font-weight: 700; }}
    .col-red   {{ color: #f06060; }}
    th.col-gold {{ color: #ffd54a; }}
    th.col-cyan {{ color: #56cff8; }}

    /* ── 2カラムレイアウト（一部ページ用） ── */
    .two-col-layout {{
      display: grid;
      grid-template-columns: 1fr 210px;
      gap: 14px;
      align-items: start;
    }}
    /* grid の 1fr アイテムが子要素の幅に引きずられてオーバーフローしないよう min-width:0 を設定 */
    .main-col {{
      min-width: 0;
      /* overflow:clip はスクロールコンテキストを作らず子要素の横スクロールを妨げない */
      overflow-x: clip;
    }}
    @media (max-width: 860px) {{
      .two-col-layout {{ grid-template-columns: 1fr; }}
      .sidebar-col {{ order: -1; }}
    }}
    .sidebar-card {{ position: sticky; top: 16px; }}
    .sidebar-title {{ font-size: 14px; font-weight: 700; margin: 0 0 12px; color: #ffd54a; }}

    /* ── 試合カード ── */
    .game-card {{ padding: 10px 0; border-bottom: 1px solid #1a2540; }}
    .game-card:last-child {{ border-bottom: none; }}
    .game-date-row {{ display: flex; align-items: center; justify-content: space-between; margin-bottom: 3px; }}
    .game-date {{ font-size: 12px; font-weight: 700; color: #8494b8; }}
    .game-result {{ font-size: 11px; font-weight: 800; padding: 2px 8px; border-radius: 999px; background: #1a2540; color: #8494b8; }}
    .result-win  {{ background: #0e2a0e; color: #5ce65c; }}
    .result-lose {{ background: #2a0e0e; color: #f06060; }}
    .result-draw {{ background: #1e2010; color: #d4c84a; }}
    .game-opponent {{ font-size: 14px; font-weight: 700; margin-bottom: 1px; }}
    .game-score    {{ font-size: 18px; font-weight: 800; }}
    .game-meta     {{ font-size: 11px; color: #4a5878; }}

    /* ── 打順ページ専用 ── */
    .lineup-grid {{ display: grid; gap: 12px; }}
    .slot-head {{ display: flex; justify-content: space-between; gap: 12px; flex-wrap: wrap; align-items: baseline; margin-bottom: 8px; }}
    .order {{ font-size: 22px; font-weight: 800; color: #ffd54a; }}
    .name  {{ font-size: 20px; font-weight: 800; }}
    .pos {{ display: inline-block; margin-left: 8px; padding: 3px 8px; border-radius: 999px; background: #1a2540; color: #8494b8; font-size: 11px; font-weight: 700; }}
    .reason {{ margin-top: 8px; font-size: 14px; line-height: 1.8; color: #c8d8f4; }}
    .stats {{ margin-top: 12px; display: grid; grid-template-columns: repeat(auto-fit, minmax(140px, 1fr)); gap: 8px; }}
    .stat {{ background: #0a1120; border: 1px solid #1a2540; border-radius: 10px; padding: 10px 12px; }}
    .stat .label {{ font-size: 11px; color: #4a5878; margin-bottom: 4px; }}
    .stat .value {{ font-size: 18px; font-weight: 800; }}
    .hero-sm {{ padding: 14px 20px 10px; margin-bottom: 10px; }}
    .hero-title-sm {{ margin: 0 0 4px; font-size: 18px; }}

    /* ── フッター ── */
    .site-footer {{
      background: #050b17;
      border-top: 1px solid #1a2540;
      padding: 24px 20px;
      margin-top: 40px;
      text-align: center;
      font-size: 11px;
      color: #3a4a6a;
      line-height: 2;
    }}
    .site-footer a {{ color: #4a6a9a; }}
    .site-footer a:hover {{ color: #9db0d4; }}
    .footer-links {{ display: flex; justify-content: center; gap: 20px; flex-wrap: wrap; margin-bottom: 8px; }}

    /* ── タブレット ── */
    @media (max-width: 860px) {{
      .site-header-nav {{ gap: 10px; font-size: 11px; }}
      .hero h1 {{ font-size: 20px; }}
      .nav-label {{ font-size: 9px; }}
    }}

    /* ── スマホ ── */
    @media (max-width: 600px) {{
      /* ヘッダー */
      .site-header {{ padding: 8px 12px; }}
      .site-logo {{ font-size: 14px; }}
      .site-logo span {{ display: none; }}
      .site-header-nav {{ display: none; }}

      /* ページ全体 */
      .wrap {{ padding: 8px 10px 40px; }}
      .page-layout {{ display: block; width: 100%; }}
      /* overflow-x:clip のまま維持する（visible にすると sticky left:0 の基準がずれる） */
      .content-col {{ width: 100%; min-width: 0; border: none; }}

      /* hero */
      .hero {{ border-radius: 10px; padding: 12px 12px 10px; margin-bottom: 12px; }}
      .hero h1 {{ font-size: 18px; }}
      .muted {{ font-size: 11px; }}

      /* ナビ */
      .nav-bar {{ margin-top: 8px; padding-top: 8px; gap: 0; }}
      .nav-section {{ gap: 2px; padding-right: 6px; }}
      .nav-label {{ font-size: 8px; padding: 0 3px 0 1px; }}
      .nav-group {{ gap: 2px; }}
      .nav-btn {{ font-size: 10.5px; padding: 3px 7px; border-radius: 4px; }}
      .nav-divider {{ display: none; }}

      /* カード：左右 padding は 0 のまま維持（table-wrap の sticky left:0 を壊さないため） */
      .card {{ padding: 14px 0; border-radius: 10px; margin-top: 10px; }}
      .card > *:not(.table-wrap) {{ padding-left: 12px; padding-right: 12px; }}
      .card-title {{ font-size: 14px; }}

      /* テーブル */
      .table-wrap {{ margin-top: 2px; }}
      th, td {{ padding: 6px 6px; }}
      table {{ font-size: 11px; min-width: 480px; }}

      /* 打順ページ */
      .order {{ font-size: 18px; }}
      .name  {{ font-size: 17px; }}
      .reason {{ font-size: 13px; }}
      .stats {{ grid-template-columns: repeat(auto-fit, minmax(110px, 1fr)); gap: 6px; }}
      .stat .value {{ font-size: 15px; }}

      /* フッター */
      .site-footer {{ padding: 16px 12px; font-size: 10px; }}
      .footer-links {{ gap: 12px; }}
    }}
  </style>
  <!-- Google AdSense -->
  <script async src="https://pagead2.googlesyndication.com/pagead/js/adsbygoogle.js?client=ca-pub-9923885942831563"
       crossorigin="anonymous"></script>
</head>
<body>
  <!-- サイトヘッダー -->
  <header class="site-header">
    <a class="site-logo" href="/public/predicted-lineup?window_games=5&use_dh=true&view=html">
      鯉男の打席分析室<span>広島カープ データ分析</span>
    </a>
    <nav class="site-header-nav">
      <a href="/public/game-recap?view=html">試合一覧</a>
      <a href="/public/risp?view=html">得点圏</a>
      <a href="/public/privacy">プライバシーポリシー</a>
    </nav>
  </header>

  <!-- 3カラムレイアウト -->
  <div class="page-layout">

    <!-- 左広告 -->
    <aside class="ad-col">
      <div class="ad-unit" id="ad-left-1">
        <ins class="adsbygoogle"
             style="display:inline-block;width:160px;height:600px"
             data-ad-client="ca-pub-9923885942831563"
             data-ad-slot="auto"
             data-ad-format="auto"></ins>
        <script>(adsbygoogle = window.adsbygoogle || []).push({{}});</script>
      </div>
    </aside>

    <!-- メインコンテンツ -->
    <main class="content-col">
      <div class="wrap">
        {body}
      </div>
      <!-- フッター -->
      <footer class="site-footer">
        <div class="footer-links">
          <a href="/public/predicted-lineup?window_games=5&use_dh=true&view=html">予想打順</a>
          <a href="/public/recent-batting?view=html">直近打撃</a>
          <a href="/public/risp?view=html">得点圏</a>
          <a href="/public/game-recap?view=html">試合一覧</a>
          <a href="/public/fielding-baserunning?view=html">走塁・守備</a>
          <a href="/public/war-ranking?view=html">WAR</a>
          <a href="/public/privacy">プライバシーポリシー</a>
          <a href="/public/terms">利用規約</a>
        </div>
        <div>© 2025 鯉男の打席分析室 — 非公式ファンサイト。掲載データはYahoo!スポーツ・NPB Basementより取得。</div>
        <div style="margin-top:4px">本サイトは広島東洋カープ及びNPBとは無関係の個人ファンサイトです。</div>
      </footer>
    </main>

    <!-- 右広告 -->
    <aside class="ad-col">
      <div class="ad-unit" id="ad-right-1">
        <ins class="adsbygoogle"
             style="display:inline-block;width:160px;height:600px"
             data-ad-client="ca-pub-9923885942831563"
             data-ad-slot="auto"
             data-ad-format="auto"></ins>
        <script>(adsbygoogle = window.adsbygoogle || []).push({{}});</script>
      </div>
    </aside>

  </div>

  <!-- ツールチップ: タップで開閉 -->
  <script>
  (function() {{
    // ── フローティング tip-box を body に1つ作成 ──
    var floatBox = document.getElementById('tip-floating');
    if (!floatBox) {{
      floatBox = document.createElement('div');
      floatBox.id = 'tip-floating';
      document.body.appendChild(floatBox);
    }}

    var hideTimer = null;

    function showTip(text, anchorEl) {{
      clearTimeout(hideTimer);
      floatBox.textContent = text;
      floatBox.style.display = 'block';
      positionTip(anchorEl);
    }}

    function hideTip() {{
      hideTimer = setTimeout(function() {{
        floatBox.style.display = 'none';
      }}, 80);
    }}

    function positionTip(anchorEl) {{
      var rect = anchorEl.getBoundingClientRect();
      var boxW = floatBox.offsetWidth  || 220;
      var boxH = floatBox.offsetHeight || 60;
      var margin = 8;

      // アイコンの上に出す（デフォルト）
      var top  = rect.top - boxH - margin;
      var left = rect.left + rect.width / 2 - boxW / 2;

      // 上に収まらない場合は下に出す
      if (top < margin) top = rect.bottom + margin;

      // 左右はみ出し防止
      var vw = window.innerWidth;
      if (left + boxW > vw - margin) left = vw - boxW - margin;
      if (left < margin) left = margin;

      floatBox.style.top  = top  + 'px';
      floatBox.style.left = left + 'px';
    }}

    document.addEventListener('DOMContentLoaded', function() {{
      // ── PC: hover で表示 ──
      document.addEventListener('mouseover', function(e) {{
        var icon = e.target.closest('.tip-icon');
        if (!icon) return;
        var wrap = icon.closest('.tip-wrap');
        if (!wrap) return;
        var box = wrap.querySelector('.tip-box');
        var text = box ? box.textContent : (wrap.dataset.tip || '');
        if (text) showTip(text, icon);
      }});
      document.addEventListener('mouseout', function(e) {{
        var icon = e.target.closest('.tip-icon');
        if (!icon) return;
        hideTip();
      }});

      // ── スマホ: タップでトグル ──
      document.addEventListener('click', function(e) {{
        var icon = e.target.closest('.tip-icon');
        if (icon) {{
          e.stopPropagation();
          if (floatBox.style.display === 'block') {{
            var wrap = icon.closest('.tip-wrap');
            var box = wrap ? wrap.querySelector('.tip-box') : null;
            var text = box ? box.textContent : (wrap ? wrap.dataset.tip || '' : '');
            // 同じアイコンならトグル閉じ
            if (floatBox.textContent === text) {{
              floatBox.style.display = 'none';
              return;
            }}
            if (text) showTip(text, icon);
          }} else {{
            var wrap = icon.closest('.tip-wrap');
            var box = wrap ? wrap.querySelector('.tip-box') : null;
            var text = box ? box.textContent : (wrap ? wrap.dataset.tip || '' : '');
            if (text) showTip(text, icon);
          }}
          return;
        }}
        // アイコン以外タップで閉じる
        floatBox.style.display = 'none';
      }});
    }});
  }})();
  </script>
  <script>
  // ── ホバー/タッチ時プリフェッチ（ナビ遷移を体感高速化）──
  (function() {{
    var prefetched = {{}};
    function tryPrefetch(el) {{
      if (!el) return;
      var url = el.dataset && el.dataset.prefetch;
      if (!url || prefetched[url]) return;
      prefetched[url] = true;
      var link = document.createElement('link');
      link.rel = 'prefetch'; link.href = url;
      document.head.appendChild(link);
    }}
    document.addEventListener('mouseover', function(e) {{
      tryPrefetch(e.target.closest('[data-prefetch]'));
    }}, {{passive: true}});
    document.addEventListener('touchstart', function(e) {{
      tryPrefetch(e.target.closest('[data-prefetch]'));
    }}, {{passive: true}});
  }})();
  </script>
</body>
</html>"""
    )


# ─────────────────────────────────────────────
# 指標ツールチップ ヘルパー
# ─────────────────────────────────────────────

# 指標名 → 説明文 マスター辞書
_TIP_DICT: dict[str, str] = {
    # 打撃基本
    "打率":    "安打÷打数。シンプルな打撃結果指標。四球・死球は含まない。",
    "出塁率":  "（安打＋四球＋死球）÷（打数＋四球＋死球＋犠飛）。どれだけ出塁できるかを示す最重要指標。",
    "長打率":  "塁打数÷打数。ヒットの「質」を示す。単打=1、二塁打=2、三塁打=3、本塁打=4。",
    "OPS":     "出塁率＋長打率。打者の総合打力を示す一番シンプルな合成指標。0.800以上は優秀。",
    "長打指数": "（二塁打×1＋三塁打×2＋本塁打×3）÷打数。四球を除いた長打力のみを測る（ISO）。",
    "ISO":     "（二塁打×1＋三塁打×2＋本塁打×3）÷打数。四球を除いた長打力のみを測る（Isolated Power）。",
    "wOBA":   "打席の結果に重みをつけた出塁指標。四球・単打・本塁打の価値を正確に反映。リーグ平均は通常.320前後。",
    # WAR系
    "WAR":     "Wins Above Replacement。その選手がいることで平均以下の選手と比べて何勝分の価値があるかを示す総合評価指標。",
    "打撃WAR": "wOBAをベースにしたwRAAから算出した打撃の貢献度。",
    "走塁WAR": "UBR+wSBをWARに換算した走塁貢献度。",
    "守備WAR": "TZRをベースにした守備貢献度のWAR換算値。",
    "総合WAR": "打撃WAR＋走塁WAR＋守備WAR。選手全体の価値を一つの数値で表す。",
    "wRAA":    "wOBAを使って算出した「平均打者と比べた得点貢献」。＋が打力が高い選手。",
    # 走塁
    "UBR":     "Ultimate Base Running。盗塁以外の走塁（進塁の積極性・判断力など）を評価する指標。＋が走塁巧者。",
    "wSB":     "Weighted Stolen Base。盗塁と盗塁死をリーグ平均と比べて評価した指標。",
    # 守備
    "TZR":     "Total Zone Rating。守備全体（レンジ・エラー・送球・捕手指標）を合計したランナーセーブ数。＋が守備貢献。",
    "RngR":    "Range Runs。守備範囲の広さを表す。ゴロ・フライへの到達能力を反映。",
    "DPR":     "Double Play Runs。ダブルプレーへの貢献度。",
    "ARM":     "Arm Runs。外野手の送球精度・内野手の牽制力などを評価。",
    "ErrR":    "Error Runs。エラーの少なさを評価。失策が少ない選手が＋になる。",
    "Framing": "捕手のフレーミング（ボールをストライクと判定させる技術）の価値。捕手のみ。",
    "Blocking": "捕手のブロッキング（ワイルドピッチ防止）の価値。捕手のみ。",
    "守備WAR":  "TZRをベースにした守備貢献度のWAR換算値。",
    "守備回":   "その選手がそのポジションで守った回数（イニング数）。",
    "守備回(計)": "全ポジション合計の守備イニング数。",
    # 得点圏
    "得点圏打率": "2塁または3塁にランナーがいる場面での打率。チャンスでの強さを示す。",
    "チャンス打席": "得点圏（2・3塁）にランナーがいる場面の打席数。",
}


def _th_tip(label: str, col_class: str = "") -> str:
    """ツールチップ付き <th> の中身（labelとiconのみ）を返す。
    テーブルの <th> 内で使う。例: f'<th class="sortable" data-col="0">{_th_tip("打率","col-avg")}</th>'
    """
    desc = _TIP_DICT.get(label, "")
    if not desc:
        return label
    safe_desc = escape(desc)
    return (
        f'<span class="tip-wrap">'
        f'{label}'
        f'<span class="tip-icon" role="button" aria-label="{escape(label)}の説明">?</span>'
        f'<span class="tip-box" style="display:none">{safe_desc}</span>'
        f'</span>'
    )


def _render_season_stats_html(active_page: str = "", window_games: int = 5) -> str:
    """今シーズン通算成績テーブルHTML（全ページ共通カード）。

    active_page に応じて表示するテーブルを切り替える：
      'recent-batting' / 'risp' → 打撃通算テーブル（proran）
      'fielding'                 → 走塁・守備通算テーブル
      'war'                      → WAR通算テーブル
    """

    def _fv(v, fmt=".3f", plus=False) -> str:
        """値を整形。Noneなら —"""
        if v is None:
            return '<span style="color:#555">—</span>'
        s = format(float(v), fmt)
        if plus and float(v) > 0:
            s = "+" + s
        return s

    # ── 打撃通算（recent-batting / risp 用）──
    if active_page in ("recent-batting", "risp"):
        season_data = _get_season_position_batting()
        adv_rows    = _get_advanced_stats_rows()

        # 選手ごとに「全ポジション中で最もPAが多い打撃成績」を集約
        seen: dict[str, dict] = {}
        for player_name in PLAYER_PROFILE.keys():
            cname = _canonical_player_name(player_name)
            if cname in seen:
                continue
            pdata = (
                season_data.get(cname)
                or season_data.get(_normalize_player_name(cname))
                or {}
            )
            if pdata.get("__empty__") or not pdata:
                continue

            # ポジション別データから最もPAが多いものを選ぶ（全ポジション合算ABも計算）
            best: dict = {}
            best_pa = -1
            total_ab = 0
            total_hits = 0
            total_hr = 0
            for pos_key, pos_val in pdata.items():
                if not isinstance(pos_val, dict):
                    continue
                pa = int(pos_val.get("pa", 0) or 0)
                if pa > best_pa:
                    best_pa = pa
                    best = pos_val
                total_ab   += int(pos_val.get("ab", 0) or 0)
                total_hits += int(pos_val.get("hits", 0) or 0)
                total_hr   += int(pos_val.get("hr", 0) or 0)

            if best_pa <= 0:
                continue

            # advanced_stats から PA・wOBA・BB%・K% を補完
            adv = next((r for r in adv_rows if r["player_name"] == cname), {})
            pa_adv   = int(adv.get("bat_pa", 0) or 0)
            bb_pct   = adv.get("bb_pct")   # 0〜1 の小数
            k_pct    = adv.get("k_pct")
            pa_use   = pa_adv if pa_adv > 0 else best_pa

            # 四球・三振の実数（PA × 率）
            bb_count = round(pa_use * bb_pct) if bb_pct is not None else None
            k_count  = round(pa_use * k_pct)  if k_pct  is not None else None

            # 打率・出塁率・長打率・OPS・長打指数（best = 最多PA守備位置）
            avg = float(best.get("avg", 0.0) or 0.0)
            obp = float(best.get("obp", 0.0) or 0.0)
            slg = float(best.get("slg", 0.0) or 0.0)
            ops = float(best.get("ops", 0.0) or 0.0)
            iso = float(best.get("iso", 0.0) or 0.0)

            seen[cname] = {
                "player_name": cname,
                "pa":    pa_use,
                "ab":    total_ab,
                "hits":  total_hits,
                "hr":    total_hr,
                "avg":   avg,
                "obp":   obp,
                "slg":   slg,
                "ops":   ops,
                "iso":   iso,
                "bb":    bb_count,
                "k":     k_count,
                "woba":  adv.get("bat_woba"),
            }

        rows = sorted(seen.values(), key=lambda r: -r["pa"])

        rows_html = []
        for r in rows:
            avg_v = r["avg"]; obp_v = r["obp"]; slg_v = r["slg"]
            ops_v = r["ops"]; iso_v = r["iso"]; woba_v = r["woba"]
            bb_v  = r["bb"];  k_v   = r["k"]
            rows_html.append(
                f'<tr>'
                f'<td data-val="{escape(r["player_name"])}">{escape(r["player_name"])}</td>'
                f'<td data-val="{r["pa"]}">{r["pa"]}</td>'
                f'<td data-val="{r["ab"]}">{r["ab"]}</td>'
                f'<td data-val="{r["hits"]}">{r["hits"]}</td>'
                f'<td data-val="{avg_v:.3f}" class="s-avg">{avg_v:.3f}</td>'
                f'<td data-val="{obp_v:.3f}" class="s-obp">{obp_v:.3f}</td>'
                f'<td data-val="{slg_v:.3f}">{slg_v:.3f}</td>'
                f'<td data-val="{ops_v:.3f}" class="s-ops"><strong>{ops_v:.3f}</strong></td>'
                f'<td data-val="{iso_v:.3f}">{iso_v:.3f}</td>'
                f'<td data-val="{r["hr"]}">{r["hr"]}</td>'
                f'<td data-val="{bb_v if bb_v is not None else -1}">{bb_v if bb_v is not None else _fv(None)}</td>'
                f'<td data-val="{k_v  if k_v  is not None else -1}">{k_v  if k_v  is not None else _fv(None)}</td>'
                f'<td data-val="{woba_v if woba_v is not None else -1}" class="s-woba">{_fv(woba_v)}</td>'
                f'</tr>'
            )
        tbody = "".join(rows_html) or '<tr><td colspan="13" class="empty">データがありません</td></tr>'

        return f"""
        <style>
          #season-bat-table td.s-avg  {{ color: #ffd54a; font-weight: 700; }}
          #season-bat-table td.s-obp  {{ color: #56cff8; }}
          #season-bat-table td.s-ops  {{ color: #ffd54a; font-weight: 700; }}
          #season-bat-table td.s-woba {{ color: #56cff8; font-weight: 700; }}
          #season-bat-table th.col-avg  {{ color: #ffd54a !important; }}
          #season-bat-table th.col-obp  {{ color: #56cff8 !important; }}
          #season-bat-table th.col-ops  {{ color: #ffd54a !important; }}
          #season-bat-table th.col-woba {{ color: #56cff8 !important; }}
          #season-bat-table tbody tr:nth-child(even) td:first-child {{ background: #0a1120; }}
          #season-bat-table tbody tr:hover td:first-child {{ background: #132040 !important; }}
          #season-bat-table thead th:first-child {{ background: #0a1020; }}
        </style>
        <div class="card" style="margin-top:14px">
          <div class="card-title">今シーズン通算 打撃成績</div>
          <div class="legend">打率・出塁率・長打率は守備ポジション別最多打席時の値。四球・三振は打席数×率から算出。</div>
          <div class="table-wrap">
            <table id="season-bat-table" class="sortable-table">
              <thead><tr>
                <th class="sortable" data-col="0">選手</th>
                <th class="sortable" data-col="1">打席</th>
                <th class="sortable" data-col="2">打数</th>
                <th class="sortable" data-col="3">安打</th>
                <th class="sortable col-avg" data-col="4">{_th_tip("打率")}</th>
                <th class="sortable col-obp" data-col="5">{_th_tip("出塁率")}</th>
                <th class="sortable" data-col="6">{_th_tip("長打率")}</th>
                <th class="sortable col-ops" data-col="7">{_th_tip("OPS")}</th>
                <th class="sortable" data-col="8">{_th_tip("長打指数")}</th>
                <th class="sortable" data-col="9">本塁打</th>
                <th class="sortable" data-col="10">四球</th>
                <th class="sortable" data-col="11">三振</th>
                <th class="sortable col-woba" data-col="12">{_th_tip("wOBA")}</th>
              </tr></thead>
              <tbody>{tbody}</tbody>
            </table>
          </div>
        </div>
        {_make_sort_script(["season-bat-table"])}
        """

    # ── 走塁・守備通算（fielding 用）──
    if active_page == "fielding":
        adv_rows = _get_advanced_stats_rows()
        rows_html = []
        for r in adv_rows:
            ubr = r.get("ubr"); wsb = r.get("wsb"); rw = r.get("runn_war")
            tzr = r.get("tzr_total"); fw = r.get("fld_war"); inn = r.get("def_inn")
            rows_html.append(
                f'<tr>'
                f'<td>{escape(r["player_name"])}</td>'
                f'<td data-val="{r["pa"]}">{r["pa"]}</td>'
                f'<td data-val="{ubr if ubr is not None else -99}" style="color:#56cff8">{_fv(ubr, ".2f", plus=True)}</td>'
                f'<td data-val="{wsb if wsb is not None else -99}">{_fv(wsb, ".2f", plus=True)}</td>'
                f'<td data-val="{rw  if rw  is not None else -99}">{_fv(rw,  ".2f", plus=True)}</td>'
                f'<td data-val="{inn if inn is not None else -99}">{_fv(inn, ".1f")}</td>'
                f'<td data-val="{tzr if tzr is not None else -99}" style="color:#56cff8;font-weight:700">{_fv(tzr, ".2f", plus=True)}</td>'
                f'<td data-val="{fw  if fw  is not None else -99}">{_fv(fw,  ".2f", plus=True)}</td>'
                f'</tr>'
            )
        tbody = "".join(rows_html) or '<tr><td colspan="8" class="empty">データがありません</td></tr>'
        return f"""
        <div class="card" style="margin-top:14px">
          <div class="card-title">今シーズン通算 走塁・守備指標</div>
          <div class="table-wrap">
            <table id="season-fld-table" class="sortable-table">
              <thead><tr>
                <th class="sortable" data-col="0">選手</th>
                <th class="sortable" data-col="1">打席</th>
                <th class="sortable" data-col="2">{_th_tip("UBR")}</th>
                <th class="sortable" data-col="3">{_th_tip("wSB")}</th>
                <th class="sortable" data-col="4">{_th_tip("走塁WAR")}</th>
                <th class="sortable" data-col="5">守備回</th>
                <th class="sortable" data-col="6">{_th_tip("TZR")}</th>
                <th class="sortable" data-col="7">{_th_tip("守備WAR")}</th>
              </tr></thead>
              <tbody>{tbody}</tbody>
            </table>
          </div>
        </div>
        {_make_sort_script(["season-fld-table"])}
        """

    # ── ホットバッター通算（hot-batters 用）──
    if active_page == "hot-batters":
        season_data = _get_season_position_batting()
        adv_rows    = _get_advanced_stats_rows()

        seen: dict[str, dict] = {}
        for player_name in PLAYER_PROFILE.keys():
            cname = _canonical_player_name(player_name)
            if cname in seen:
                continue
            pdata = (
                season_data.get(cname)
                or season_data.get(_normalize_player_name(cname))
                or {}
            )
            if pdata.get("__empty__") or not pdata:
                continue

            best: dict = {}
            best_pa = -1
            for pos_key, pos_val in pdata.items():
                if not isinstance(pos_val, dict):
                    continue
                pa = int(pos_val.get("pa", 0) or 0)
                if pa > best_pa:
                    best_pa = pa
                    best = pos_val

            if best_pa <= 0:
                continue

            adv = next((r for r in adv_rows if r["player_name"] == cname), {})

            seen[cname] = {
                "player_name": cname,
                "pa":      best_pa,
                "avg":     float(best.get("avg", 0.0) or 0.0),
                "obp":     float(best.get("obp", 0.0) or 0.0),
                "slg":     float(best.get("slg", 0.0) or 0.0),
                "ops":     float(best.get("ops", 0.0) or 0.0),
                "hr":      int(best.get("homeruns", 0) or 0),
                "woba":    adv.get("bat_woba"),
                "wraa":    adv.get("bat_wraa"),
                "bat_war": adv.get("bat_war"),
            }

        rows = sorted(seen.values(), key=lambda r: -(r["obp"] or 0))

        rows_html = []
        for r in rows:
            rows_html.append(
                f'<tr>'
                f'<td data-val="{escape(r["player_name"])}">{escape(r["player_name"])}</td>'
                f'<td data-val="{r["pa"]}">{r["pa"]}</td>'
                f'<td data-val="{r["avg"]:.3f}" style="color:#ffd54a;font-weight:700">{r["avg"]:.3f}</td>'
                f'<td data-val="{r["obp"]:.3f}" style="color:#56cff8">{r["obp"]:.3f}</td>'
                f'<td data-val="{r["slg"]:.3f}">{r["slg"]:.3f}</td>'
                f'<td data-val="{r["ops"]:.3f}">{r["ops"]:.3f}</td>'
                f'<td data-val="{r["hr"]}">{r["hr"]}</td>'
                f'<td data-val="{r["woba"] if r["woba"] is not None else -1}" style="color:#56cff8;font-weight:700">{_fv(r["woba"])}</td>'
                f'<td data-val="{r["wraa"] if r["wraa"] is not None else -99}">{_fv(r["wraa"], ".2f", plus=True)}</td>'
                f'<td data-val="{r["bat_war"] if r["bat_war"] is not None else -99}">{_fv(r["bat_war"], ".2f", plus=True)}</td>'
                f'</tr>'
            )
        tbody = "".join(rows_html) or '<tr><td colspan="10" class="empty">データがありません</td></tr>'

        return f"""
        <div class="card" style="margin-top:14px">
          <div class="card-title">今シーズン通算 打撃成績</div>
          <div class="table-wrap">
            <table id="season-hb-table" class="sortable-table">
              <thead><tr>
                <th class="sortable" data-col="0">選手</th>
                <th class="sortable" data-col="1">打席</th>
                <th class="sortable" data-col="2">{_th_tip("打率")}</th>
                <th class="sortable" data-col="3">{_th_tip("出塁率")}</th>
                <th class="sortable" data-col="4">{_th_tip("長打率")}</th>
                <th class="sortable" data-col="5">{_th_tip("OPS")}</th>
                <th class="sortable" data-col="6">HR</th>
                <th class="sortable" data-col="7">{_th_tip("wOBA")}</th>
                <th class="sortable" data-col="8">{_th_tip("wRAA")}</th>
                <th class="sortable" data-col="9">{_th_tip("打撃WAR")}</th>
              </tr></thead>
              <tbody>{tbody}</tbody>
            </table>
          </div>
        </div>
        {_make_sort_script(["season-hb-table"])}
        """

    # ── WAR通算（war 用）──
    if active_page == "war":
        adv_rows = _get_advanced_stats_rows()
        rows_html = []
        for r in adv_rows:
            tw = r.get("total_war"); bw = r.get("bat_war")
            rw = r.get("runn_war"); fw = r.get("fld_war")
            woba = r.get("bat_woba"); wraa = r.get("bat_wraa")
            rows_html.append(
                f'<tr>'
                f'<td>{escape(r["player_name"])}</td>'
                f'<td data-val="{r["pa"]}">{r["pa"]}</td>'
                f'<td data-val="{woba if woba is not None else -1}">{_fv(woba, ".3f")}</td>'
                f'<td data-val="{wraa if wraa is not None else -99}">{_fv(wraa, ".2f", plus=True)}</td>'
                f'<td data-val="{bw   if bw   is not None else -99}">{_fv(bw,   ".2f", plus=True)}</td>'
                f'<td data-val="{rw   if rw   is not None else -99}">{_fv(rw,   ".2f", plus=True)}</td>'
                f'<td data-val="{fw   if fw   is not None else -99}">{_fv(fw,   ".2f", plus=True)}</td>'
                f'<td data-val="{tw   if tw   is not None else -99}" style="color:#ffd54a;font-weight:700">{_fv(tw, ".2f", plus=True)}</td>'
                f'</tr>'
            )
        tbody = "".join(rows_html) or '<tr><td colspan="8" class="empty">データがありません</td></tr>'
        return f"""
        <div class="card" style="margin-top:14px">
          <div class="card-title">今シーズン通算 WAR</div>
          <div class="table-wrap">
            <table id="season-war-table" class="sortable-table">
              <thead><tr>
                <th class="sortable" data-col="0">選手</th>
                <th class="sortable" data-col="1">打席</th>
                <th class="sortable" data-col="2">{_th_tip("wOBA")}</th>
                <th class="sortable" data-col="3">{_th_tip("wRAA")}</th>
                <th class="sortable" data-col="4">{_th_tip("打撃WAR")}</th>
                <th class="sortable" data-col="5">{_th_tip("走塁WAR")}</th>
                <th class="sortable" data-col="6">{_th_tip("守備WAR")}</th>
                <th class="sortable" data-col="7">{_th_tip("総合WAR")}</th>
              </tr></thead>
              <tbody>{tbody}</tbody>
            </table>
          </div>
        </div>
        {_make_sort_script(["season-war-table"])}
        """

    return ""


def _common_nav(active_page: str = "", window_games: int = 5) -> str:
    """全ページ共通ナビゲーションバー HTML を返す。
    active_page: 'recent-batting' / 'risp' / 'fielding' / 'war' /
                 'predicted-lineup-5t' / 'predicted-lineup-5f' /
                 'predicted-lineup-10t' / 'predicted-lineup-10f' / 'game-recap'
    """
    def _a(label: str, href: str, page_key: str) -> str:
        cls = " active" if active_page == page_key else ""
        return f'<a class="nav-btn{cls}" href="{href}" data-prefetch="{href}">{label}</a>'

    wg = window_games
    nav_html = f"""
    <nav class="nav-bar">
      <div class="nav-section">
        <span class="nav-label">予想打順</span>
        <div class="nav-group">
          {_a("5試合 DH有",  f"/public/predicted-lineup?window_games=5&use_dh=true",   "predicted-lineup-5t")}
          {_a("5試合 DH無",  f"/public/predicted-lineup?window_games=5&use_dh=false",  "predicted-lineup-5f")}
          {_a("10試合 DH有", f"/public/predicted-lineup?window_games=10&use_dh=true",  "predicted-lineup-10t")}
          {_a("10試合 DH無", f"/public/predicted-lineup?window_games=10&use_dh=false", "predicted-lineup-10f")}
        </div>
      </div>
      <div class="nav-section">
        <span class="nav-label">打撃</span>
        <div class="nav-group">
          {_a("直近打撃",  f"/public/recent-batting?window_games={wg}", "recent-batting")}
          {_a("得点圏",    f"/public/risp?window_games={wg}&view=html", "risp")}
        </div>
      </div>
      <div class="nav-section">
        <span class="nav-label">指標</span>
        <div class="nav-group">
          {_a("走塁・守備", "/public/fielding-baserunning", "fielding")}
          {_a("WAR",       "/public/war-ranking",          "war")}
        </div>
      </div>
      <div class="nav-section">
        <span class="nav-label">試合</span>
        <div class="nav-group">
          {_a("試合一覧", "/public/game-recap", "game-recap")}
        </div>
      </div>
    </nav>"""
    return nav_html


def _render_recent_batting_html(data: dict, show_season: bool = False) -> HTMLResponse:
    rows_html = []

    for row in data.get("players", []):
        # data-* 属性にソート用の生数値を埋め込む
        rows_html.append(
            f"""
            <tr>
              <td data-val="{escape(str(row.get("player_name", "")))}">{escape(str(row.get("player_name", "")))}</td>
              <td data-val="{int(row.get("games", 0) or 0)}">{int(row.get("games", 0) or 0)}</td>
              <td data-val="{int(row.get("pa", 0) or 0)}">{int(row.get("pa", 0) or 0)}</td>
              <td data-val="{float(row.get("avg", 0.0) or 0.0):.3f}">{float(row.get("avg", 0.0) or 0.0):.3f}</td>
              <td data-val="{float(row.get("obp", 0.0) or 0.0):.3f}">{float(row.get("obp", 0.0) or 0.0):.3f}</td>
              <td data-val="{float(row.get("ops", 0.0) or 0.0):.3f}"><strong>{float(row.get("ops", 0.0) or 0.0):.3f}</strong></td>
              <td data-val="{int(row.get("homeruns", 0) or 0)}">{int(row.get("homeruns", 0) or 0)}</td>
              <td data-val="{float(row.get("woba", 0.0) or 0.0):.3f}">{float(row.get("woba", 0.0) or 0.0):.3f}</td>
            </tr>
            """
        )

    games_html = []
    for game in data.get("games", []):
        result_val = str(game.get("result", "") or "")
        result_class = ""
        if result_val == "勝":
            result_class = "result-win"
        elif result_val == "負":
            result_class = "result-lose"
        elif result_val == "分":
            result_class = "result-draw"
        games_html.append(
            f"""
            <div class="game-card">
              <div class="game-date-row">
                <span class="game-date">{escape(str(game.get("date", "")))}</span>
                <span class="game-result {result_class}">{escape(result_val)}</span>
              </div>
              <div class="game-opponent">vs {escape(str(game.get("opponent", "")))}</div>
              <div class="game-meta">{escape(str(game.get("venue", "")))} &nbsp;{escape(str(game.get("score", "")))}</div>
            </div>
            """
        )

    wg = int(data.get("window_games", 5) or 5)

    def _rb_cls(w):
        return " active" if w == wg else ""

    body = f"""
    <style>
      /* 列: 選手(1) 試合(2) 打席(3) 打率(4) 出塁率(5) OPS(6) HR(7) wOBA(8) */
      #batting-table td:nth-child(4)  {{ color: #ffd54a; font-weight:700; }}
      #batting-table td:nth-child(5)  {{ color: #56cff8; }}
      #batting-table td:nth-child(6)  {{ color: #ffd54a; font-weight:700; }}
      #batting-table td:nth-child(8)  {{ color: #56cff8; font-weight:700; }}
      th.col-ops  {{ color: #ffd54a !important; }}
      th.col-woba {{ color: #56cff8 !important; }}
      th.col-avg  {{ color: #ffd54a !important; }}
      /* 選手名列（1列目）のsticky固定 ── 偶数行・hover 背景もつぶす */
      #batting-table tbody tr:nth-child(even) td:first-child {{ background: #0a1120; }}
      #batting-table tbody tr:hover td:first-child {{ background: #132040 !important; }}
      #batting-table thead th:first-child {{ background: #0a1020; }}
    </style>

    <div class="hero">
      <h1>直近打撃成績</h1>
      <div class="muted">直近 {wg} 試合の打撃成績 ／ 列ヘッダをクリックでソート</div>
    </div>
    <div class="sticky-nav">
      <div class="nav-bar">
        <div class="nav-section">
          <span class="nav-label">期間</span>
          <div class="nav-group">
            <a class="nav-btn{_rb_cls(5)}"  href="/public/recent-batting?window_games=5">直近 5試合</a>
            <a class="nav-btn{_rb_cls(10)}" href="/public/recent-batting?window_games=10">直近 10試合</a>
          </div>
        </div>
        <div class="nav-section">
          <span class="nav-label">表示</span>
          <div class="nav-group">
            <a class="nav-btn{'' if show_season else ' active'}" href="/public/recent-batting?window_games={wg}">直近</a>
            <a class="nav-btn{' active' if show_season else ''}" href="/public/recent-batting?window_games={wg}&view=season">通算</a>
          </div>
        </div>
      </div>
      {_common_nav("recent-batting", wg)}
    </div>

    <div id="recent-content"{' style="display:none"' if show_season else ''}>
    <div class="two-col-layout">
      <div class="main-col">
        <div class="card">
          <div class="card-title">選手別 直近{wg}試合成績</div>
          <div class="table-wrap">
            <table id="batting-table">
              <thead>
                <tr>
                  <th class="sortable" data-col="0">選手</th>
                  <th class="sortable" data-col="1">試合</th>
                  <th class="sortable" data-col="2">打席</th>
                  <th class="sortable col-avg" data-col="3">{_th_tip("打率")}</th>
                  <th class="sortable col-woba" data-col="4">{_th_tip("出塁率")}</th>
                  <th class="sortable col-ops" data-col="5">{_th_tip("OPS")}</th>
                  <th class="sortable" data-col="6">HR</th>
                  <th class="sortable col-woba" data-col="7">{_th_tip("wOBA")}</th>
                </tr>
              </thead>
              <tbody>
                {''.join(rows_html) if rows_html else '<tr><td colspan="8" class="empty">データがありません</td></tr>'}
              </tbody>
            </table>
          </div>
        </div>
      </div>

    </div>
    </div><!-- /#recent-content -->

    {_make_sort_script(["batting-table"])}
    """
    if show_season:
        body += _render_season_stats_html("recent-batting", wg)
    return _html_page("直近打撃成績", body)


def _render_predicted_lineup_html(data: dict) -> HTMLResponse:
    lineup = data.get("lineup", [])

    # ── 打順行（1行レイアウト）を生成 ──
    rows_html = []
    for item in lineup:
        recent   = item.get("recent", {}) or {}
        season   = item.get("season_position", {}) or {}
        order    = int(item.get("order", 0) or 0)
        pos_code = str(item.get("position", "") or "")
        pos_ja   = POSITION_LABELS.get(pos_code, pos_code)
        r_obp    = float(recent.get("obp", 0.0) or 0.0)
        r_iso    = float(recent.get("iso", 0.0) or 0.0)
        s_obp    = float(season.get("obp", 0.0) or 0.0)
        s_iso    = float(season.get("iso", 0.0) or 0.0)
        r_avg    = float(recent.get("avg", 0.0) or 0.0)
        r_slg    = round(r_avg + r_iso, 3)
        r_ops    = round(r_obp + r_slg, 3)
        defv     = float(item.get("defense", 0.0) or 0.0)
        score    = float(item.get("score",   0.0) or 0.0)
        def_cls  = "def-pos" if defv > 0 else ("def-neg" if defv < 0 else "")
        reason      = escape(str(item.get("reason", "")))
        commentary  = escape(str(item.get("commentary", "")))

        rows_html.append(f"""
        <div class="lu-row" data-id="{order}">
          <!-- ── ヘッダー：常時表示 ── -->
          <div class="lu-header">
            <span class="lu-order">{order}</span>
            <span class="lu-pos">{escape(pos_ja)}</span>
            <span class="lu-name">{escape(str(item.get("player_name", "")))}</span>
            <div class="lu-score-wrap">
              <div class="lu-slabel">スコア</div>
              <div class="lu-score">{score:.1f}</div>
            </div>
            <!-- タブボタン群 -->
            <div class="lu-tabs">
              <button class="lu-tab-btn" data-tab="stats" aria-expanded="false">
                指標<span class="lu-tab-arrow">›</span>
              </button>
              <button class="lu-tab-btn" data-tab="commentary" aria-expanded="false">
                解説<span class="lu-tab-arrow">›</span>
              </button>
            </div>
          </div>

          <!-- ── 指標パネル ── -->
          <div class="lu-panel" data-panel="stats">
            <div class="lu-stats-grid">
              <div class="lu-stat">
                <div class="lu-slabel">出塁率</div>
                <div class="lu-sval">{r_obp:.3f}</div>
              </div>
              <div class="lu-stat">
                <div class="lu-slabel">長打指数</div>
                <div class="lu-sval">{r_iso:.3f}</div>
              </div>
              <div class="lu-stat">
                <div class="lu-slabel">長打率</div>
                <div class="lu-sval">{r_slg:.3f}</div>
              </div>
              <div class="lu-stat lu-stat-ops">
                <div class="lu-slabel">OPS</div>
                <div class="lu-sval">{r_ops:.3f}</div>
              </div>
              <div class="lu-stat">
                <div class="lu-slabel">補正出塁</div>
                <div class="lu-sval">{s_obp:.3f}</div>
              </div>
              <div class="lu-stat">
                <div class="lu-slabel">補正長打</div>
                <div class="lu-sval">{s_iso:.3f}</div>
              </div>
              <div class="lu-stat">
                <div class="lu-slabel">守備補正</div>
                <div class="lu-sval {def_cls}">{defv:+.3f}</div>
              </div>
            </div>
            <div class="lu-reason-inner">
              <span class="lu-reason-text">{reason}</span>
            </div>
          </div>

          <!-- ── 解説パネル ── -->
          <div class="lu-panel" data-panel="commentary">
            <div class="lu-commentary-inner">
              <span class="lu-commentary-text">{commentary}</span>
            </div>
          </div>
        </div>""")

    wg     = int(data.get("window_games", 5) or 5)
    dh     = bool(data.get("use_dh", True))
    dh_str = 'あり' if dh else 'なし'

    def _lu_cls(w, d):
        return " active" if (w == wg and d == dh) else ""

    def _rb_cls2(w):
        return " active" if w == wg else ""

    rows_body = (
        ''.join(rows_html)
        if rows_html
        else '<div class="empty">打順データがありません</div>'
    )

    body = f"""
    <style>
      /* ══════════════════════════════════════
         予想打順 専用スタイル
      ══════════════════════════════════════ */

      .lu-grid {{
        display: flex;
        flex-direction: column;
        gap: 5px;
        width: 100%;
      }}

      /* ── カード ── */
      .lu-row {{
        display: block;
        width: 100%;
        box-sizing: border-box;
        background: #121a31;
        border: 1px solid #26304d;
        border-radius: 12px;
        overflow: hidden;
        transition: border-color .15s;
      }}
      .lu-row:hover {{ border-color: #3a4d7a; }}

      /* ── ヘッダー（常時表示） ── */
      .lu-header {{
        display: flex;
        align-items: center;
        gap: 8px;
        padding: 9px 12px;
        flex-wrap: nowrap;
        width: 100%;
        box-sizing: border-box;
      }}

      /* 打順番号 */
      .lu-order {{
        font-size: 28px;
        font-weight: 900;
        color: #ffd54a;
        min-width: 30px;
        text-align: center;
        flex-shrink: 0;
        line-height: 1;
      }}

      /* 守備位置バッジ */
      .lu-pos {{
        display: inline-block;
        padding: 3px 9px;
        border-radius: 6px;
        background: #243154;
        color: #d8e5ff;
        font-size: 12px;
        font-weight: 700;
        flex-shrink: 0;
        white-space: nowrap;
        min-width: 40px;
        text-align: center;
      }}

      /* 選手名 */
      .lu-name {{
        font-size: 18px;
        font-weight: 800;
        white-space: nowrap;
        flex: 1 1 auto;
        color: #ffffff;
        min-width: 0;
        overflow: hidden;
        text-overflow: ellipsis;
      }}

      /* スコアボックス */
      .lu-score-wrap {{
        background: #0f1730;
        border: 1px solid #3a4570;
        border-radius: 8px;
        padding: 3px 9px;
        text-align: center;
        flex-shrink: 0;
        min-width: 50px;
      }}
      .lu-slabel {{
        font-size: 9px;
        color: #7080a0;
        white-space: nowrap;
        line-height: 1.3;
      }}
      .lu-score {{
        font-size: 16px;
        font-weight: 800;
        color: #ffd54a;
        line-height: 1.3;
        white-space: nowrap;
      }}

      /* ── タブボタン群 ── */
      .lu-tabs {{
        display: flex;
        gap: 5px;
        flex-shrink: 0;
        margin-left: 4px;
      }}
      .lu-tab-btn {{
        display: flex;
        align-items: center;
        gap: 2px;
        padding: 4px 9px;
        border-radius: 7px;
        border: 1px solid #2e3d60;
        background: #0f1730;
        color: #8090b8;
        font-size: 11px;
        font-weight: 600;
        cursor: pointer;
        white-space: nowrap;
        transition: background .15s, color .15s, border-color .15s;
        line-height: 1;
      }}
      .lu-tab-btn:hover {{
        background: #1a2848;
        color: #c0d0f0;
      }}
      .lu-tab-btn.active {{
        background: #1e3a6e;
        color: #ffd54a;
        border-color: #3a6abf;
      }}
      .lu-tab-btn.active[data-tab="commentary"] {{
        background: #0d2040;
        color: #88bbff;
        border-color: #2a4a90;
      }}
      .lu-tab-arrow {{
        font-size: 13px;
        line-height: 1;
        display: inline-block;
        transition: transform .2s;
      }}
      .lu-tab-btn.active .lu-tab-arrow {{
        transform: rotate(90deg);
      }}

      /* ── パネル共通（アコーディオン） ── */
      .lu-panel {{
        display: block;
        width: 100%;
        box-sizing: border-box;
        max-height: 0;
        overflow: hidden;
        transition: max-height .25s ease, padding .2s ease;
        padding: 0 12px;
        border-top: 0px solid #26304d;
      }}
      .lu-panel.open {{
        max-height: 600px;
        padding: 8px 12px;
        border-top-width: 1px;
      }}

      /* ── 指標パネル内容 ── */
      .lu-stats-grid {{
        display: flex;
        flex-wrap: wrap;
        gap: 5px;
        margin-bottom: 8px;
      }}
      .lu-stat {{
        background: #0f1730;
        border: 1px solid #26304d;
        border-radius: 8px;
        padding: 4px 10px;
        text-align: center;
        min-width: 54px;
      }}
      .lu-stat-ops {{
        border-color: #4a5c8a;
        background: #14203a;
      }}
      .lu-sval {{
        font-size: 15px;
        font-weight: 700;
        line-height: 1.3;
        white-space: nowrap;
        color: #d8e5ff;
      }}
      .lu-stat-ops .lu-sval {{
        color: #ffd54a;
        font-size: 16px;
      }}
      .def-pos {{ color: #6ee86e; }}
      .def-neg {{ color: #e86e6e; }}

      /* 根拠テキスト（指標パネル下部） */
      .lu-reason-inner {{
        padding: 5px 8px;
        background: #0a1020;
        border-radius: 6px;
      }}
      .lu-reason-text {{
        font-size: 11px;
        color: #8090b0;
        line-height: 1.6;
        white-space: normal;
      }}

      /* ── 解説パネル内容 ── */
      .lu-commentary-inner {{
        padding: 6px 10px;
        background: #0d1628;
        border-left: 3px solid #3a5faa;
        border-radius: 0 6px 6px 0;
      }}
      .lu-commentary-text {{
        font-size: 12.5px;
        color: #c8d8f8;
        line-height: 1.75;
        white-space: normal;
      }}

      /* ══ スマホ（≤600px） ══ */
      @media (max-width: 600px) {{
        .lu-grid {{ gap: 4px; }}

        /* ヘッダー: 1行に収まるよう nowrap 維持、サイズ縮小 */
        .lu-header {{
          padding: 7px 9px;
          gap: 5px;
          flex-wrap: nowrap;
          min-height: 0;
        }}
        .lu-order  {{ font-size: 20px; min-width: 20px; }}
        .lu-pos    {{ font-size: 10px; padding: 2px 5px; min-width: 30px; }}
        .lu-name   {{ font-size: 14px; min-width: 0; }}
        .lu-score-wrap {{ padding: 2px 6px; min-width: 38px; }}
        .lu-slabel {{ font-size: 8px; }}
        .lu-score  {{ font-size: 13px; }}

        /* タブボタン: 縮めて1行に */
        .lu-tabs   {{ gap: 3px; margin-left: 2px; flex-shrink: 0; }}
        .lu-tab-btn {{ font-size: 10px; padding: 3px 7px; gap: 1px; }}
        .lu-tab-arrow {{ font-size: 11px; }}

        /* パネル */
        .lu-panel.open {{ padding: 6px 10px; }}

        /* 指標グリッド: 折り返しで縦並び + スクロール不要 */
        .lu-stats-grid {{
          gap: 4px;
          flex-wrap: wrap;
          overflow-x: visible;
        }}
        .lu-stat   {{ min-width: 70px; flex: 1 1 70px; padding: 4px 8px; }}
        .lu-sval   {{ font-size: 14px; }}
        .lu-stat-ops .lu-sval {{ font-size: 15px; }}

        .lu-commentary-text {{ font-size: 11.5px; line-height: 1.65; }}
        .lu-commentary-inner {{ border-left-width: 2px; }}
      }}
    </style>

    <script>
    (function() {{
      document.addEventListener('DOMContentLoaded', function() {{
        document.querySelectorAll('.lu-row').forEach(function(row) {{
          row.querySelectorAll('.lu-tab-btn').forEach(function(btn) {{
            btn.addEventListener('click', function() {{
              var tab = btn.dataset.tab;
              var panel = row.querySelector('.lu-panel[data-panel="' + tab + '"]');
              var isOpen = btn.classList.contains('active');

              // 全パネル・全ボタンを閉じる
              row.querySelectorAll('.lu-panel').forEach(function(p) {{
                p.classList.remove('open');
              }});
              row.querySelectorAll('.lu-tab-btn').forEach(function(b) {{
                b.classList.remove('active');
                b.setAttribute('aria-expanded', 'false');
              }});

              // 同じタブを押した場合はトグルで閉じる
              if (!isOpen) {{
                panel.classList.add('open');
                btn.classList.add('active');
                btn.setAttribute('aria-expanded', 'true');
              }}
            }});
          }});
        }});
      }});
    }})();
    </script>

    <div class="hero">
      <h1>予想打順</h1>
      <div class="muted">DH {dh_str} / 直近 {wg} 試合ベース / 生成時刻 {escape(str(data.get("generated_at", "")))}</div>
      {_common_nav("predicted-lineup-" + str(wg) + ("t" if dh else "f"), wg)}
    </div>

    <div class="lu-grid">
      {rows_body}
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
            show_season = (view == "season")
            return _render_recent_batting_html(data, show_season=show_season)

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


# ─────────────────────────────────────────────
# 走塁・守備指標 / WAR一覧  共通データ取得
# ─────────────────────────────────────────────

def _build_advanced_stats_rows() -> list[dict]:
    """
    npbbasement の今シーズン通算データから
    走塁指標・守備指標・WAR を選手ごとに集約して返す。
    PLAYER_PROFILE に登録済みの野手のみ対象。
    """
    players = _load_npbbasement_players()
    profile_names = set(PLAYER_PROFILE.keys())

    # normalized_name -> canonical_profile_name のマップ
    norm_to_profile: dict[str, str] = {
        _normalize_player_name(n): n for n in profile_names
    }

    rows: list[dict] = []

    for p in players:
        if not isinstance(p, dict):
            continue
        nameJ = _decode_nb(p.get("nameJ") or "")
        norm  = _normalize_player_name(nameJ)
        profile_name = norm_to_profile.get(norm)
        if not profile_name:
            continue

        stats = p.get("Stats") or {}
        if not isinstance(stats, dict):
            continue

        war_obj = stats.get("war") or {}
        bat_obj = stats.get("bat") or {}
        fld_list = stats.get("fld") or []

        def _f(obj: dict, key: str) -> float | None:
            v = obj.get(key)
            return round(float(v), 2) if v is not None else None

        # 走塁
        ubr  = _f(war_obj, "UBR")
        wsb  = _f(war_obj, "wSB")
        runn_war = _f(war_obj, "runnWAR")

        # 守備 – ポジション別明細
        fld_rows = []
        for fld in fld_list:
            if not isinstance(fld, dict):
                continue
            pos = (fld.get("POS") or "").upper()
            inn = fld.get("Inn")
            tzr = _f(fld, "TZR")
            rng = _f(fld, "RngR")
            dpr = _f(fld, "DPR")
            arm = _f(fld, "ARM")
            err = _f(fld, "ErrR")
            frm = _f(fld, "Framing")
            blk = _f(fld, "Blocking")
            if pos:
                fld_rows.append({
                    "pos": pos,
                    "inn": round(float(inn), 1) if inn is not None else None,
                    "tzr": tzr,
                    "rng": rng,
                    "dpr": dpr,
                    "arm": arm,
                    "err": err,
                    "framing": frm,
                    "blocking": blk,
                })

        # 守備通算 (war_obj 内の _total フィールド)
        def_inn   = _f(war_obj, "DefInn_total")
        tzr_total = _f(war_obj, "TZR_total")
        rng_total = _f(war_obj, "RngR_total")
        dpr_total = _f(war_obj, "DPR_total")
        arm_total = _f(war_obj, "ARM_total")
        err_total = _f(war_obj, "ErrR_total")
        fld_war   = _f(war_obj, "fldWAR")

        # バット
        bat_pa   = bat_obj.get("PA") if isinstance(bat_obj, dict) else None
        bat_woba = _f(bat_obj, "wOBA") if isinstance(bat_obj, dict) else None
        bat_wraa = _f(bat_obj, "wRAA") if isinstance(bat_obj, dict) else None
        bat_war  = _f(war_obj, "batWAR")
        # 四球率・三振率（npbbasement は 0〜100 のパーセント値で格納）
        bb_pct_raw = bat_obj.get("BB%") if isinstance(bat_obj, dict) else None
        k_pct_raw  = bat_obj.get("K%")  if isinstance(bat_obj, dict) else None
        # 0〜1 に正規化して保存（小数値で四球数・三振数の計算に使う）
        bb_pct = round(float(bb_pct_raw) / 100, 4) if bb_pct_raw is not None else None
        k_pct  = round(float(k_pct_raw)  / 100, 4) if k_pct_raw  is not None else None

        # 総合 WAR
        total_war = _f(war_obj, "WAR")

        rows.append({
            "player_name": profile_name,
            "pa": int(bat_pa) if bat_pa is not None else 0,
            # 走塁
            "ubr":      ubr,
            "wsb":      wsb,
            "runn_war": runn_war,
            # 守備通算
            "def_inn":   def_inn,
            "tzr_total": tzr_total,
            "rng_total": rng_total,
            "dpr_total": dpr_total,
            "arm_total": arm_total,
            "err_total": err_total,
            "fld_war":   fld_war,
            # ポジション別守備
            "fld_rows":  fld_rows,
            # 打撃
            "bat_pa":   int(bat_pa) if bat_pa is not None else 0,
            "bat_woba": bat_woba,
            "bat_wraa": bat_wraa,
            "bat_war":  bat_war,
            "bb_pct":   bb_pct,
            "k_pct":    k_pct,
            # 総合
            "total_war": total_war,
        })

    # PA 降順でソート
    rows.sort(key=lambda x: (-x["pa"], x["player_name"]))
    return rows


def _get_advanced_stats_rows() -> list[dict]:
    """キャッシュ付き advanced stats 取得（12時間）"""
    cache_entry = CACHE.get("advanced_stats", {})
    if _cache_alive(cache_entry) and cache_entry.get("value"):
        return cache_entry["value"]
    rows = _build_advanced_stats_rows()
    CACHE["advanced_stats"] = {
        "value": rows,
        "expires_at": _cache_now() + CACHE_TTL_PLAYER_DEFENSE,  # 12h
    }
    return rows


# ─────────────────────────────────────────────
# 走塁・守備指標ページ  /public/fielding-baserunning
# ─────────────────────────────────────────────

def _fmt(v: float | None, fmt: str = ".2f", plus: bool = False) -> str:
    if v is None:
        return '<span style="color:#555">—</span>'
    s = format(v, fmt)
    if plus and v > 0:
        s = "+" + s
    color = ""
    if plus:
        color = "#7fff9e" if v > 0 else ("#ff7e7e" if v < 0 else "#aaa")
    return f'<span style="color:{color}">{s}</span>' if color else s


def _render_fielding_baserunning_html(rows: list[dict], show_season: bool = False) -> HTMLResponse:

    # ── 走塁テーブル行 ──
    run_rows_html = []
    for r in rows:
        ubr  = r.get("ubr")
        wsb  = r.get("wsb")
        rw   = r.get("runn_war")
        run_rows_html.append(f"""
        <tr>
          <td>{r["player_name"]}</td>
          <td>{r["pa"]}</td>
          <td data-val="{ubr if ubr is not None else -99}">{_fmt(ubr, ".2f", plus=True)}</td>
          <td data-val="{wsb if wsb is not None else -99}">{_fmt(wsb, ".2f", plus=True)}</td>
          <td data-val="{rw  if rw  is not None else -99}">{_fmt(rw,  ".2f", plus=True)}</td>
        </tr>""")

    # ── 守備テーブル行（ポジション別） ──
    fld_rows_html = []
    for r in rows:
        name = r["player_name"]
        for fld in r.get("fld_rows", []):
            tzr = fld.get("tzr")
            rng = fld.get("rng")
            dpr = fld.get("dpr")
            arm = fld.get("arm")
            err = fld.get("err")
            frm = fld.get("framing")
            blk = fld.get("blocking")
            inn = fld.get("inn")
            fld_rows_html.append(f"""
        <tr>
          <td>{name}</td>
          <td>{fld["pos"]}</td>
          <td>{inn if inn is not None else "—"}</td>
          <td data-val="{tzr if tzr is not None else -99}">{_fmt(tzr, ".2f", plus=True)}</td>
          <td data-val="{rng if rng is not None else -99}">{_fmt(rng, ".2f", plus=True)}</td>
          <td data-val="{dpr if dpr is not None else -99}">{_fmt(dpr, ".2f", plus=True)}</td>
          <td data-val="{arm if arm is not None else -99}">{_fmt(arm, ".2f", plus=True)}</td>
          <td data-val="{err if err is not None else -99}">{_fmt(err, ".2f", plus=True)}</td>
          <td data-val="{frm if frm is not None else -99}">{_fmt(frm, ".2f", plus=True)}</td>
          <td data-val="{blk if blk is not None else -99}">{_fmt(blk, ".2f", plus=True)}</td>
        </tr>""")

    # ── 守備通算テーブル行 ──
    fld_total_rows_html = []
    for r in rows:
        if not r.get("fld_rows"):
            continue  # 守備イニング 0 は除外
        tzr = r.get("tzr_total")
        rng = r.get("rng_total")
        dpr = r.get("dpr_total")
        arm = r.get("arm_total")
        err = r.get("err_total")
        fw  = r.get("fld_war")
        inn = r.get("def_inn")
        fld_total_rows_html.append(f"""
        <tr>
          <td>{r["player_name"]}</td>
          <td>{inn if inn is not None else "—"}</td>
          <td data-val="{tzr if tzr is not None else -99}">{_fmt(tzr, ".2f", plus=True)}</td>
          <td data-val="{rng if rng is not None else -99}">{_fmt(rng, ".2f", plus=True)}</td>
          <td data-val="{dpr if dpr is not None else -99}">{_fmt(dpr, ".2f", plus=True)}</td>
          <td data-val="{arm if arm is not None else -99}">{_fmt(arm, ".2f", plus=True)}</td>
          <td data-val="{err if err is not None else -99}">{_fmt(err, ".2f", plus=True)}</td>
          <td data-val="{fw  if fw  is not None else -99}" style="color:#7fff9e;font-weight:bold">{_fmt(fw, ".2f", plus=True)}</td>
        </tr>""")

    def sortable_table(table_id: str, headers: list[tuple[str, int]], body_html: str, empty_msg: str = "データがありません") -> str:
        ths = "".join(
            f'<th class="sortable" data-col="{i}">{_th_tip(h) if h in _TIP_DICT else h}</th>'
            for i, (h, _) in enumerate(headers)
        )
        return f"""
        <table id="{table_id}">
          <thead><tr>{ths}</tr></thead>
          <tbody>
            {body_html if body_html.strip() else f'<tr><td colspan="{len(headers)}" class="empty">{empty_msg}</td></tr>'}
          </tbody>
        </table>"""

    run_table = sortable_table(
        "run-table",
        [("選手", 0), ("打席", 1), ("UBR", 2), ("wSB", 3), ("走塁WAR", 4)],
        "".join(run_rows_html),
    )
    fld_pos_table = sortable_table(
        "fld-pos-table",
        [("選手", 0), ("POS", 1), ("守備回", 2),
         ("TZR", 3), ("RngR", 4), ("DPR", 5), ("ARM", 6), ("ErrR", 7),
         ("Framing", 8), ("Blocking", 9)],
        "".join(fld_rows_html),
    )
    fld_total_table = sortable_table(
        "fld-total-table",
        [("選手", 0), ("守備回(計)", 1),
         ("TZR", 2), ("RngR", 3), ("DPR", 4), ("ARM", 5), ("ErrR", 6),
         ("守備WAR", 7)],
        "".join(fld_total_rows_html),
    )

    body = f"""
    <style>
      /* タブ UI */
      .tab-bar {{
        display: flex;
        gap: 4px;
        margin-bottom: 0;
        border-bottom: 2px solid #1a2540;
        padding-bottom: 0;
        /* スマホで横スクロール */
        overflow-x: auto;
        -webkit-overflow-scrolling: touch;
        scrollbar-width: none;
        -ms-overflow-style: none;
        flex-wrap: nowrap;
      }}
      .tab-bar::-webkit-scrollbar {{ display: none; }}
      .tab-btn {{
        padding: 9px 18px;
        font-size: 13px;
        font-weight: 600;
        color: #5a6e94;
        background: transparent;
        border: none;
        border-bottom: 2px solid transparent;
        margin-bottom: -2px;
        cursor: pointer;
        transition: all 0.15s;
        white-space: nowrap;
        flex-shrink: 0;
      }}
      .tab-btn:hover {{ color: #c8d8f4; }}
      .tab-btn.active {{
        color: #ffd54a;
        border-bottom-color: #ffd54a;
        font-weight: 800;
      }}
      .tab-panel {{ display: none; margin-top: 16px; }}
      .tab-panel.active {{ display: block; }}
    </style>

    <div class="hero">
      <h1>走塁・守備指標 <span style="font-size:13px;color:#4a5878;font-weight:400">今シーズン通算</span></h1>
      <div class="muted">出所：NPB Basement（TZR ベース） ／ 列ヘッダをクリックでソート</div>
    </div>
    <div class="sticky-nav">
      <div class="nav-bar">
        <div class="nav-section">
          <span class="nav-label">表示</span>
          <div class="nav-group">
            <a class="nav-btn{'' if show_season else ' active'}" href="/public/fielding-baserunning">直近</a>
            <a class="nav-btn{' active' if show_season else ''}" href="/public/fielding-baserunning?view=season">通算</a>
          </div>
        </div>
      </div>
      {_common_nav("fielding")}
    </div>

    <div id="recent-content"{' style="display:none"' if show_season else ''}>
    <div class="card">
      <div class="tab-bar">
        <button class="tab-btn active" data-tab="run">🏃 走塁指標</button>
        <button class="tab-btn" data-tab="fld-pos">🧤 守備（ポジション別）</button>
        <button class="tab-btn" data-tab="fld-total">📋 守備（通算合計）</button>
      </div>

      <!-- 走塁 -->
      <div class="tab-panel active" id="tab-run">
        <div class="legend">
          <b>UBR</b> 非盗塁走塁価値（進塁・突入） ／
          <b>wSB</b> 盗塁価値（盗塁数・刺殺から算出） ／
          <b>走塁WAR</b> UBR+wSB をWAR換算
        </div>
        <div class="table-wrap">{run_table}</div>
      </div>

      <!-- 守備ポジション別 -->
      <div class="tab-panel" id="tab-fld-pos">
        <div class="legend">
          <b>TZR</b> Total Zone Rating ／ <b>RngR</b> レンジ ／ <b>DPR</b> 併殺 ／
          <b>ARM</b> 送球 ／ <b>ErrR</b> エラー ／ <b>Framing</b>・<b>Blocking</b> 捕手のみ
        </div>
        <div class="table-wrap">{fld_pos_table}</div>
      </div>

      <!-- 守備通算合計 -->
      <div class="tab-panel" id="tab-fld-total">
        <div class="legend">複数ポジションを守った選手は全ポジションの合算値</div>
        <div class="table-wrap">{fld_total_table}</div>
      </div>
    </div>
    </div><!-- /#recent-content -->

    {_make_sort_script(["run-table","fld-pos-table","fld-total-table"])}
    <script>
    (function(){{
      var btns = document.querySelectorAll('.tab-btn');
      btns.forEach(function(btn){{
        btn.addEventListener('click', function(){{
          btns.forEach(function(b){{ b.classList.remove('active'); }});
          document.querySelectorAll('.tab-panel').forEach(function(p){{ p.classList.remove('active'); }});
          btn.classList.add('active');
          document.getElementById('tab-' + btn.dataset.tab).classList.add('active');
        }});
      }});
    }})();
    </script>
    """
    if show_season:
        body += _render_season_stats_html("fielding")
    return _html_page("走塁・守備指標", body)


# ─────────────────────────────────────────────
# WAR一覧ページ  /public/war-ranking
# ─────────────────────────────────────────────

def _war_chart_html(rows: list[dict]) -> str:
    """総合WAR の横棒グラフ HTML を生成（WAR降順ソート）"""
    sorted_rows = sorted(rows, key=lambda r: r.get("total_war") or 0.0, reverse=True)
    max_abs = max((abs(r.get("total_war") or 0.0) for r in sorted_rows), default=1.0)
    max_abs = max(max_abs, 0.1)
    lines = []
    for r in sorted_rows:
        tw = r.get("total_war") or 0.0
        name = r["player_name"]
        pct = min(abs(tw) / max_abs * 48, 48)  # 最大48%（センターから両端）
        if tw >= 0:
            bar = f'<div class="war-bar-pos" style="width:{pct:.1f}%"></div>'
        else:
            bar = f'<div class="war-bar-neg" style="width:{pct:.1f}%"></div>'
        val_cls = "pos" if tw > 0.005 else ("neg" if tw < -0.005 else "zero")
        sign = "+" if tw > 0.005 else ""
        lines.append(
            f'<div class="war-row">'
            f'<span class="war-name">{escape(name)}</span>'
            f'<div class="war-bar-track">{bar}</div>'
            f'<span class="war-val {val_cls}">{sign}{tw:.2f}</span>'
            f'</div>'
        )
    return "\n".join(lines)


def _render_war_ranking_html(rows: list[dict], show_season: bool = False) -> HTMLResponse:

    war_rows_html = []
    for r in rows:
        tw  = r.get("total_war")
        bw  = r.get("bat_war")
        rw  = r.get("runn_war")
        fw  = r.get("fld_war")
        wraa = r.get("bat_wraa")
        woba = r.get("bat_woba")

        war_rows_html.append(f"""
        <tr>
          <td>{r["player_name"]}</td>
          <td>{r["pa"]}</td>
          <td data-val="{woba  if woba  is not None else -99}">{_fmt(woba,  ".3f")}</td>
          <td data-val="{wraa  if wraa  is not None else -99}">{_fmt(wraa,  ".2f", plus=True)}</td>
          <td data-val="{bw    if bw    is not None else -99}">{_fmt(bw,    ".2f", plus=True)}</td>
          <td data-val="{rw    if rw    is not None else -99}">{_fmt(rw,    ".2f", plus=True)}</td>
          <td data-val="{fw    if fw    is not None else -99}">{_fmt(fw,    ".2f", plus=True)}</td>
          <td data-val="{tw    if tw    is not None else -99}" style="color:#ffd54a;font-weight:bold">{_fmt(tw, ".2f", plus=True)}</td>
        </tr>""")

    war_table = f"""
    <table id="war-table">
      <thead>
        <tr>
          <th class="sortable" data-col="0">選手</th>
          <th class="sortable" data-col="1">打席</th>
          <th class="sortable" data-col="2">{_th_tip("wOBA")}</th>
          <th class="sortable" data-col="3">{_th_tip("wRAA")}</th>
          <th class="sortable" data-col="4">{_th_tip("打撃WAR")}</th>
          <th class="sortable" data-col="5">{_th_tip("走塁WAR")}</th>
          <th class="sortable" data-col="6">{_th_tip("守備WAR")}</th>
          <th class="sortable" data-col="7" style="color:#ffd54a">{_th_tip("総合WAR")}</th>
        </tr>
      </thead>
      <tbody>
        {"".join(war_rows_html) if war_rows_html else '<tr><td colspan="8" class="empty">データがありません</td></tr>'}
      </tbody>
    </table>"""

    body = f"""
    <style>
      /* WARバーチャート */
      .war-chart {{ margin: 20px 0 6px; display: flex; flex-direction: column; gap: 6px; }}
      .war-row {{
        display: grid;
        grid-template-columns: 90px 1fr 52px;
        align-items: center;
        gap: 8px;
        font-size: 12px;
      }}
      .war-name {{ font-weight:600; color:#c8d8f4; text-align:right; white-space:nowrap; overflow:hidden; text-overflow:ellipsis; }}
      .war-bar-track {{
        background: #0a1120;
        border-radius: 4px;
        height: 14px;
        position: relative;
        overflow: hidden;
      }}
      .war-bar-fill {{
        height: 100%;
        border-radius: 4px;
        transition: width 0.4s ease;
      }}
      .war-bar-neg {{
        position: absolute;
        right: 50%;
        height: 100%;
        border-radius: 4px 0 0 4px;
        background: linear-gradient(90deg, #f06060, #c03030);
      }}
      .war-bar-pos {{
        position: absolute;
        left: 50%;
        height: 100%;
        border-radius: 0 4px 4px 0;
        background: linear-gradient(90deg, #3cb878, #5ce65c);
      }}
      .war-val {{ font-weight:700; font-size:13px; text-align:right; }}
      .war-val.pos {{ color: #5ce65c; }}
      .war-val.neg {{ color: #f06060; }}
      .war-val.zero {{ color: #4a5878; }}
    </style>

    <div class="hero">
      <h1>WAR一覧 <span style="font-size:13px;color:#4a5878;font-weight:400">今シーズン通算</span></h1>
      <div class="muted">出所：NPB Basement ／ 列ヘッダをクリックでソート</div>
    </div>
    <div class="sticky-nav">
      <div class="nav-bar">
        <div class="nav-section">
          <span class="nav-label">表示</span>
          <div class="nav-group">
            <a class="nav-btn{'' if show_season else ' active'}" href="/public/war-ranking">直近</a>
            <a class="nav-btn{' active' if show_season else ''}" href="/public/war-ranking?view=season">通算</a>
          </div>
        </div>
      </div>
      {_common_nav("war")}
    </div>

    <div id="recent-content"{' style="display:none"' if show_season else ''}>
    <!-- WAR バーチャート -->
    <div class="card">
      <div class="card-title">総合WAR ランキング</div>
      <div class="legend"><b>WAR</b>: 0より大きければ平均以上の貢献。打撃＋走塁＋守備の合計。</div>
      <div class="war-chart" id="war-chart">
        {_war_chart_html(rows)}
      </div>
    </div>

    <!-- WAR 詳細テーブル -->
    <div class="card">
      <div class="card-title">選手別 WAR内訳</div>
      <div class="legend">
        <b>wOBA</b> 加重出塁率 ／ <b>wRAA</b> 平均打者比較の得点貢献 ／
        <b>打撃WAR</b> wRAA ベース ／ <b>走塁WAR</b> UBR+wSB ／
        <b>守備WAR</b> TZR ベース ／ <b>総合WAR</b> 打撃+走塁+守備
      </div>
      <div class="table-wrap">{war_table}</div>
    </div>
    </div><!-- /#recent-content -->

    {_make_sort_script(["war-table"])}
    """
    if show_season:
        body += _render_season_stats_html("war")
    return _html_page("WAR一覧", body)


# ─────────────────────────────────────────────
# 共通ソートスクリプト生成
# ─────────────────────────────────────────────

def _make_sort_script(table_ids: list[str]) -> str:
    """複数テーブルに同じソートロジックを適用する JS スニペットを返す"""
    ids_js = "[" + ",".join(f"'{tid}'" for tid in table_ids) + "]"
    return f"""
    <script>
    (function(){{
      var tableIds = {ids_js};
      tableIds.forEach(function(tid){{
        var table = document.getElementById(tid);
        if (!table) return;
        var sortCol = -1, sortAsc = true;
        table.querySelectorAll('th.sortable').forEach(function(th){{
          th.addEventListener('click', function(){{
            var col = parseInt(th.dataset.col, 10);
            if (sortCol === col) {{ sortAsc = !sortAsc; }}
            else {{ sortCol = col; sortAsc = false; }}
            table.querySelectorAll('th.sortable').forEach(function(h){{
              h.classList.remove('asc','desc');
            }});
            th.classList.add(sortAsc ? 'asc' : 'desc');
            var tbody = table.tBodies[0];
            var rows = Array.from(tbody.rows);
            rows.sort(function(a,b){{
              var av = a.cells[col].dataset.val || '';
              var bv = b.cells[col].dataset.val || '';
              var an = parseFloat(av), bn = parseFloat(bv);
              var cmp = (!isNaN(an)&&!isNaN(bn)) ? an-bn : av.localeCompare(bv,'ja');
              return sortAsc ? cmp : -cmp;
            }});
            rows.forEach(function(r){{ tbody.appendChild(r); }});
          }});
        }});
      }});
    }})();
    </script>"""


# ─────────────────────────────────────────────
# ルート定義
# ─────────────────────────────────────────────

@router.get("/public/fielding-baserunning")
def public_fielding_baserunning(request: Request, view: str | None = None):
    try:
        rows = _get_advanced_stats_rows()
        if _wants_html(request, view):
            show_season = (view == "season")
            return _render_fielding_baserunning_html(rows, show_season=show_season)
        return _no_cache_json({"players": rows})
    except Exception as e:
        return JSONResponse(
            status_code=500,
            content={
                "error": "fielding-baserunning failed",
                "type": type(e).__name__,
                "message": str(e),
            },
        )


@router.get("/public/war-ranking")
def public_war_ranking(request: Request, view: str | None = None):
    try:
        rows = _get_advanced_stats_rows()
        if _wants_html(request, view):
            show_season = (view == "season")
            return _render_war_ranking_html(rows, show_season=show_season)
        return _no_cache_json({"players": rows})
    except Exception as e:
        return JSONResponse(
            status_code=500,
            content={
                "error": "war-ranking failed",
                "type": type(e).__name__,
                "message": str(e),
            },
        )


# ─────────────────────────────────────────────
# ホットバッター（直近5試合ランキング）ページ
# /public/hot-batters
# ─────────────────────────────────────────────

@lru_cache(maxsize=16)
def _parse_carp_batting_risp(box_url: str) -> dict[str, dict]:
    """
    ボックスコアの生HTML を直接パースしてカープ選手の
    ・得点圏安打数 (rbi_hits)  ← class="hit Red rbi"
    ・全安打数     (hits)       ← class="hit Red" or "hit Red rbi"
    ・打点付き打席(chance_pa)  ← 打点列 > 0 の行を得点圏打席として近似
    ・全打席数     (pa)
    を返す。
    """
    try:
        html = _fetch_html(box_url)
    except Exception:
        return {}

    # 打撃テーブルブロックを raw HTML で抽出
    # <table> 内を保持したまま処理
    raw_tables = re.findall(r"<table[^>]*>(.*?)</table>", html, re.DOTALL)

    # 守備・選手・打点 ヘッダーを持つテーブルのみ抜き出す
    batting_raw = []
    for rt in raw_tables:
        rows_raw = re.findall(r"<tr[^>]*>(.*?)</tr>", rt, re.DOTALL)
        if not rows_raw:
            continue
        h_cells = re.findall(r"<t[hd][^>]*>(.*?)</t[hd]>", rows_raw[0], re.DOTALL)
        hc = [re.sub(r"<[^>]+>", "", c).strip() for c in h_cells]
        if {"守備", "選手", "打点"}.issubset(set(hc)):
            batting_raw.append((rows_raw, hc))

    if len(batting_raw) < 2:
        return {}

    carp_is_home = bool(re.search(r"/scores/\d{4}/\d{4}/c-[a-z]{1,2}-\d{2}/box\.html", box_url))
    carp_rows_raw, header = batting_raw[1] if carp_is_home else batting_raw[0]

    idx_map = {name: i for i, name in enumerate(header)}
    rbi_col = idx_map.get("打点", -1)

    result: dict[str, dict] = {}

    for row_raw in carp_rows_raw[1:]:
        tds = re.findall(r"<td([^>]*)>(.*?)</td>", row_raw, re.DOTALL)
        if not tds:
            continue

        player_name = ""
        rbi_hits   = 0
        hits       = 0
        plate_abs  = []  # (class_val, text) の打席リスト

        for attrs, content in tds:
            text = re.sub(r"<[^>]+>", "", content).strip()
            cls_m = re.search(r'class=["\']([^"\']*)["\']', attrs)
            cls_val = (cls_m.group(1) if cls_m else "").strip()

            if "player" in cls_val:
                player_name = text
            else:
                plate_abs.append((cls_val, text))

        if not player_name or player_name == "チーム計":
            continue

        # 打席結果セルを走査（守備・打順・各種カウント列を除く打席結果列）
        # 打席結果列の識別: class が hit/walk/Green/'' かつ
        #   テキストが打席結果らしい（ゴロ/飛/安/本/振/球/邪 などを含む）
        def _is_plate_result(cls: str, text: str) -> bool:
            result_keywords = ["ゴロ", "飛", "安", "本", "振", "球", "邪", "犠", "ゴ失", "内野安"]
            if not text or text in ("-", "－", "&nbsp;"):
                return False
            if any(kw in text for kw in result_keywords):
                return True
            if cls in ("hit Red", "hit Red rbi", "walk Blue", "Green"):
                return True
            return False

        pa_count   = 0
        chance_pa  = 0

        for cls_val, text in plate_abs:
            if not _is_plate_result(cls_val, text):
                continue
            pa_count += 1
            if "hit Red rbi" in cls_val:
                hits += 1
                rbi_hits += 1
                chance_pa += 1
            elif "hit Red" in cls_val:
                hits += 1
            # 打点付き非安打（犠飛等）も得点圏打席扱い
            elif "①" in text or "②" in text or "③" in text:
                chance_pa += 1

        result[player_name] = {
            "hits":      hits,
            "rbi_hits":  rbi_hits,
            "chance_pa": chance_pa,
            "pa":        pa_count,
        }

    return result


def _build_hot_batters_data(window_games: int = 5) -> dict:
    """
    直近 window_games 試合の 打率・出塁率・チャンス打率 TOP選手を算出。
    """
    cache_key = f"hot_batters:{window_games}"
    cache_entry = CACHE.get(cache_key, {})
    if _cache_alive(cache_entry) and cache_entry.get("value"):
        return cache_entry["value"]

    # ── 打率・出塁率は既存の集計を再利用 ──
    recent_data  = _build_recent_batting_response(window_games)
    players      = recent_data.get("players", [])
    games_list   = recent_data.get("games", [])

    # MIN_PA フィルタ（打席数が少なすぎる選手を除外）
    MIN_PA = 5
    eligible = [p for p in players if p.get("pa", 0) >= MIN_PA]

    # ── 打率TOP ──
    avg_top  = max(eligible, key=lambda p: p.get("avg",  0.0), default=None) if eligible else None

    # ── 出塁率TOP ──
    obp_top  = max(eligible, key=lambda p: p.get("obp",  0.0), default=None) if eligible else None

    # ── チャンス打率（得点圏近似）集計 ──
    chance_totals: dict[str, dict] = {}
    for game in games_list:
        box_url = game.get("box_url", "")
        if not box_url:
            continue
        try:
            risp = _parse_carp_batting_risp(box_url)
        except Exception:
            continue
        for raw_name, s in risp.items():
            cname = _canonical_player_name(raw_name)
            if not cname:
                continue
            ct = chance_totals.setdefault(cname, {"rbi_hits": 0, "chance_pa": 0})
            ct["rbi_hits"]  += s["rbi_hits"]
            ct["chance_pa"] += s["chance_pa"]

    # 最低 2 チャンス打席以上
    chance_eligible = {
        name: d for name, d in chance_totals.items()
        if d["chance_pa"] >= 2
    }

    def _chance_avg(d: dict) -> float:
        cp = d.get("chance_pa", 0)
        return d["rbi_hits"] / cp if cp > 0 else 0.0

    chance_top_name = (
        max(chance_eligible, key=lambda n: _chance_avg(chance_eligible[n]))
        if chance_eligible else None
    )

    # フルネームに変換（canonical → PLAYER_PROFILE キー）
    def _profile_name(cname: str) -> str:
        for pname in PLAYER_PROFILE:
            if _canonical_player_name(pname) == cname:
                return pname
        return cname

    chance_top = None
    if chance_top_name:
        d = chance_eligible[chance_top_name]
        chance_top = {
            "player_name":  _profile_name(chance_top_name),
            "chance_avg":   round(_chance_avg(d), 3),
            "rbi_hits":     d["rbi_hits"],
            "chance_pa":    d["chance_pa"],
        }

    # ── 全選手チャンス成績リスト（補足用）──
    chance_ranking = []
    for cname, d in sorted(chance_eligible.items(),
                            key=lambda x: -_chance_avg(x[1])):
        chance_ranking.append({
            "player_name": _profile_name(cname),
            "chance_avg":  round(_chance_avg(d), 3),
            "rbi_hits":    d["rbi_hits"],
            "chance_pa":   d["chance_pa"],
        })

    result = {
        "window_games": window_games,
        "games":        games_list,
        "avg_top":      avg_top,
        "obp_top":      obp_top,
        "chance_top":   chance_top,
        "chance_ranking": chance_ranking,
        "all_players":  players,
    }

    CACHE[cache_key] = {
        "value":      result,
        "expires_at": _cache_now() + CACHE_TTL_RECENT_BATTING,
    }
    return result


def _render_hot_batters_html(data: dict, show_season: bool = False) -> HTMLResponse:
    wg          = int(data.get("window_games", 5))
    avg_top     = data.get("avg_top")  or {}
    obp_top     = data.get("obp_top")  or {}
    chance_top  = data.get("chance_top") or {}
    all_players = data.get("all_players", [])
    games_list  = data.get("games", [])
    chance_rank = data.get("chance_ranking", [])

    def _wg_cls(w: int) -> str:
        return " active" if w == wg else ""

    def _hero_card(
        rank_label: str,
        stat_label: str,
        stat_key: str,
        player: dict,
        val_fmt: str,
        accent: str,
        extra_html: str = "",
    ) -> str:
        if not player:
            return f"""
            <div class="hero-card" style="--accent:{accent}">
              <div class="rank-badge">{rank_label}</div>
              <div class="stat-label">{stat_label}</div>
              <div class="no-data">データなし</div>
            </div>"""

        name     = player.get("player_name", "")
        val      = player.get(stat_key, 0.0) or 0.0
        games_n  = int(player.get("games", 0) or 0)
        pa       = int(player.get("pa",    0) or 0)
        hits     = int(player.get("hits",  0) or 0)
        hr       = int(player.get("homeruns", 0) or 0)
        rbi      = int(player.get("rbi",   0) or 0)

        val_str = format(val, val_fmt)

        sub_stats = f"""
          <div class="sub-stats">
            <span>{games_n}試合</span>
            <span>{pa}打席</span>
            <span>{hits}安打</span>
            {"<span>"+str(hr)+"本塁打</span>" if hr else ""}
            {"<span>"+str(rbi)+"打点</span>" if rbi else ""}
          </div>"""

        return f"""
        <div class="hero-card" style="--accent:{accent}">
          <div class="rank-badge">{rank_label}</div>
          <div class="stat-label">{stat_label}</div>
          <div class="player-name">{name}</div>
          <div class="big-val" style="color:var(--accent)">{val_str}</div>
          {sub_stats}
          {extra_html}
        </div>"""

    # チャンス打率カードの extra_html
    chance_extra = ""
    if chance_top:
        rbi_h  = int(chance_top.get("rbi_hits", 0) or 0)
        ch_pa  = int(chance_top.get("chance_pa", 0) or 0)
        chance_extra = f'<div class="sub-note">打点安打 {rbi_h}/{ch_pa} チャンス打席</div>'

    # チャンス用カードを独立生成
    def _chance_card() -> str:
        if not chance_top:
            return """
            <div class="hero-card" style="--accent:#ff9f43">
              <div class="rank-badge">チャンス強打者</div>
              <div class="stat-label">チャンス打率</div>
              <div class="no-data">データなし<br><small>(最低2チャンス打席)</small></div>
            </div>"""
        name = chance_top.get("player_name", "")
        val  = chance_top.get("chance_avg", 0.0)
        rbi_h = int(chance_top.get("rbi_hits", 0))
        ch_pa = int(chance_top.get("chance_pa", 0))
        return f"""
        <div class="hero-card" style="--accent:#ff9f43">
          <div class="rank-badge">チャンス強打者</div>
          <div class="stat-label">チャンス打率 <span style="font-size:11px;opacity:.7">※打点付打席</span></div>
          <div class="player-name">{name}</div>
          <div class="big-val" style="color:#ff9f43">{val:.3f}</div>
          <div class="sub-stats">
            <span>打点安打 {rbi_h}本</span>
            <span>チャンス打席 {ch_pa}</span>
          </div>
        </div>"""

    card_avg    = _hero_card("首位打者", f"直近{wg}試合 打率", "avg", avg_top, ".3f", "#ffd54a")
    card_obp    = _hero_card("出塁王", f"直近{wg}試合 出塁率", "obp", obp_top, ".3f", "#7ecfff")
    card_chance = _chance_card()

    # ── サポートテーブル：全選手成績 ──
    # PA 降順・打率TOP から並べ替え
    sorted_players = sorted(all_players, key=lambda p: -p.get("avg", 0))
    rows_html = []
    for p in sorted_players:
        pname  = p.get("player_name", "")
        avg    = float(p.get("avg",  0) or 0)
        obp    = float(p.get("obp",  0) or 0)
        slg    = float(p.get("slg",  0) or 0)
        ops    = float(p.get("ops",  0) or 0)
        pa     = int(p.get("pa",    0) or 0)
        hits   = int(p.get("hits",  0) or 0)
        hr     = int(p.get("homeruns", 0) or 0)
        woba   = float(p.get("woba", 0) or 0)

        # チャンス成績を取得
        cname  = _canonical_player_name(pname)
        chance_d = next((r for r in chance_rank if _canonical_player_name(r["player_name"]) == cname), None)
        if chance_d:
            ca_str = f"{chance_d['chance_avg']:.3f} ({chance_d['rbi_hits']}/{chance_d['chance_pa']})"
        else:
            ca_str = "—"

        # アクティブ選手ハイライト
        is_avg_top  = avg_top  and _canonical_player_name(avg_top.get("player_name",""))  == cname
        is_obp_top  = obp_top  and _canonical_player_name(obp_top.get("player_name",""))  == cname
        is_ch_top   = chance_top and _canonical_player_name(chance_top.get("player_name","")) == cname

        badges = ""
        if is_avg_top:  badges += '<span class="badge avg-badge">打</span>'
        if is_obp_top:  badges += '<span class="badge obp-badge">出</span>'
        if is_ch_top:   badges += '<span class="badge ch-badge">機</span>'

        rows_html.append(f"""
        <tr{"" if pa >= 5 else ' style="opacity:.55"'}>
          <td>{pname} {badges}</td>
          <td>{pa}</td>
          <td>{hits}</td>
          <td style="color:#ffd54a;font-weight:bold">{avg:.3f}</td>
          <td style="color:#7ecfff">{obp:.3f}</td>
          <td>{slg:.3f}</td>
          <td>{ops:.3f}</td>
          <td>{hr}</td>
          <td style="color:#ff9f43">{ca_str}</td>
          <td>{woba:.3f}</td>
        </tr>""")

    # 試合リスト
    games_html_parts = []
    for g in games_list:
        rv = str(g.get("result","") or "")
        rc = "result-win" if rv=="勝" else ("result-lose" if rv=="負" else "result-draw" if rv=="分" else "")
        games_html_parts.append(f"""
        <div class="mini-game">
          <span class="gdate">{g.get("date","")}</span>
          <span class="gopp">vs {g.get("opponent","")}</span>
          <span class="gres {rc}">{rv}</span>
          <span class="gscore">{g.get("score","")}</span>
        </div>""")

    body = f"""
    <style>
      /* ── ホットバッターページ専用スタイル ── */
      .hb-wrap {{ max-width: 1000px; margin: 0 auto; }}

      /* ヒーローカードグリッド */
      .hero-cards {{
        display: grid;
        grid-template-columns: repeat(3, 1fr);
        gap: 18px;
        margin-bottom: 20px;
      }}
      .hero-card {{
        background: linear-gradient(150deg, #131e38 0%, #0c1424 100%);
        border: 1.5px solid var(--accent, #ffd54a);
        border-radius: 18px;
        padding: 28px 22px 22px;
        text-align: center;
        box-shadow: 0 0 28px color-mix(in srgb, var(--accent,#ffd54a) 15%, transparent),
                    inset 0 1px 0 rgba(255,255,255,.05);
        transition: transform .2s, box-shadow .2s;
        position: relative;
        overflow: hidden;
      }}
      .hero-card::before {{
        content: "";
        position: absolute;
        inset: 0;
        background: radial-gradient(ellipse at 50% -10%, color-mix(in srgb, var(--accent,#ffd54a) 12%, transparent) 0%, transparent 65%);
        pointer-events: none;
      }}
      .hero-card:hover {{
        transform: translateY(-5px);
        box-shadow: 0 8px 40px color-mix(in srgb, var(--accent,#ffd54a) 22%, transparent),
                    inset 0 1px 0 rgba(255,255,255,.08);
      }}
      .rank-badge {{
        display: inline-block;
        font-size: 11px;
        font-weight: 700;
        letter-spacing: .08em;
        text-transform: uppercase;
        color: var(--accent, #ffd54a);
        background: color-mix(in srgb, var(--accent,#ffd54a) 10%, transparent);
        border: 1px solid color-mix(in srgb, var(--accent,#ffd54a) 30%, transparent);
        border-radius: 20px;
        padding: 3px 14px;
        margin-bottom: 10px;
      }}
      .stat-label {{
        font-size: 12px;
        color: #5a6e94;
        margin-bottom: 14px;
        min-height: 16px;
      }}
      .player-name {{
        font-size: 26px;
        font-weight: 800;
        color: #e8f0ff;
        margin-bottom: 10px;
        letter-spacing: .02em;
        line-height: 1.2;
      }}
      .big-val {{
        font-size: 64px;
        font-weight: 900;
        line-height: 1;
        margin-bottom: 16px;
        font-variant-numeric: tabular-nums;
        text-shadow: 0 0 22px currentColor;
      }}
      .sub-stats {{
        display: flex;
        flex-wrap: wrap;
        justify-content: center;
        gap: 6px;
        font-size: 11px;
        color: #5a6e94;
      }}
      .sub-stats span {{
        background: rgba(255,255,255,.04);
        border: 1px solid #1e2d50;
        border-radius: 6px;
        padding: 3px 9px;
      }}
      .sub-note {{
        font-size: 11px;
        color: #4a5878;
        margin-top: 8px;
      }}
      .no-data {{
        font-size: 16px;
        color: #2a3550;
        margin: 24px 0;
      }}

      /* ── サポートグリッド ── */
      .support-grid {{
        display: grid;
        grid-template-columns: 1fr 200px;
        gap: 14px;
        align-items: start;
      }}
      @media (max-width: 720px) {{
        .hero-cards {{ grid-template-columns: 1fr; gap: 12px; }}
        .big-val {{ font-size: 52px; }}
        .player-name {{ font-size: 22px; }}
        .support-grid {{ grid-template-columns: 1fr; }}
      }}

      /* バッジ */
      .badge {{
        display: inline-block;
        font-size: 10px;
        font-weight: 700;
        border-radius: 4px;
        padding: 1px 5px;
        margin-left: 4px;
        vertical-align: middle;
      }}
      .avg-badge {{ background: #ffd54a15; color: #ffd54a; border: 1px solid #ffd54a40; }}
      .obp-badge {{ background: #56cff815; color: #56cff8; border: 1px solid #56cff840; }}
      .ch-badge  {{ background: #ff9f4315; color: #ff9f43; border: 1px solid #ff9f4340; }}

      /* 試合リスト */
      .mini-game {{
        display: flex;
        align-items: center;
        gap: 8px;
        padding: 7px 0;
        border-bottom: 1px solid #1a2540;
        font-size: 12px;
      }}
      .mini-game:last-child {{ border-bottom: none; }}
      .gdate {{ color: #4a5878; min-width: 44px; }}
      .gopp  {{ flex: 1; color: #c8d4f0; font-weight: 600; }}
      .gres  {{ font-weight: 800; min-width: 14px; text-align: center; }}
      .result-win  {{ color: #5ce65c; }}
      .result-lose {{ color: #f06060; }}
      .result-draw {{ color: #d4c84a; }}
      .gscore {{ color: #4a5878; font-size: 11px; }}

      /* テーブル強調 */
      #all-table td:nth-child(4) {{ color: #ffd54a; font-weight:700; }}
      #all-table td:nth-child(5) {{ color: #56cff8; }}
      #all-table td:nth-child(9) {{ color: #ff9f43; font-weight:700; }}
    </style>

    <div class="hb-wrap">
      <div class="hero">
        <h1>ホットバッター</h1>
        <div class="muted">直近 {wg} 試合で最も輝いた打者</div>
        <div class="nav-bar">
          <div class="nav-section">
            <span class="nav-label">期間</span>
            <div class="nav-group">
              <a class="nav-btn{_wg_cls(5)}"  href="/public/hot-batters?window_games=5">直近 5試合</a>
              <a class="nav-btn{_wg_cls(10)}" href="/public/hot-batters?window_games=10">直近 10試合</a>
            </div>
          </div>
          <div class="nav-section">
            <span class="nav-label">表示</span>
            <div class="nav-group">
              <a class="nav-btn{'' if show_season else ' active'}" href="/public/hot-batters?window_games={wg}">直近</a>
              <a class="nav-btn{' active' if show_season else ''}" href="/public/hot-batters?window_games={wg}&view=season">通算</a>
            </div>
          </div>
        </div>
        {_common_nav("", wg)}
      </div>

      <div id="recent-content"{' style="display:none"' if show_season else ''}>
      <!-- ── 3枚ヒーローカード ── -->
      <div class="hero-cards">
        {card_avg}
        {card_obp}
        {card_chance}
      </div>

      <!-- ── サポートエリア ── -->
      <div class="support-grid">
        <div class="card">
          <div class="card-title">直近{wg}試合 全選手成績</div>
          <div class="legend">
            <span class="badge avg-badge">打</span> 打率TOP ／
            <span class="badge obp-badge">出</span> 出塁率TOP ／
            <span class="badge ch-badge">機</span> チャンスTOP ／
            薄字 = 5打席未満
          </div>
          <div class="table-wrap">
            <table id="all-table">
              <thead>
                <tr>
                  <th class="sortable" data-col="0">選手</th>
                  <th class="sortable" data-col="1">打席</th>
                  <th class="sortable" data-col="2">安打</th>
                  <th class="sortable col-gold" data-col="3">{_th_tip("打率")}</th>
                  <th class="sortable col-cyan"  data-col="4">{_th_tip("出塁率")}</th>
                  <th class="sortable" data-col="5">{_th_tip("長打率")}</th>
                  <th class="sortable" data-col="6">{_th_tip("OPS")}</th>
                  <th class="sortable" data-col="7">HR</th>
                  <th class="sortable" data-col="8" style="color:#ff9f43">{_th_tip("得点圏打率")}</th>
                  <th class="sortable col-cyan" data-col="9">{_th_tip("wOBA")}</th>
                </tr>
              </thead>
              <tbody>
                {"".join(rows_html) if rows_html else '<tr><td colspan="10" class="empty">データなし</td></tr>'}
              </tbody>
            </table>
          </div>
        </div>
      </div>
      </div><!-- /#recent-content -->
    </div>

    {_make_sort_script(["all-table"])}
    """

    if show_season:
        body += _render_season_stats_html("hot-batters", wg)

    return _html_page("ホットバッター", body)


@router.get("/public/hot-batters")
def public_hot_batters(request: Request, window_games: int = 5, view: str | None = None):
    try:
        window_games = max(1, min(window_games, 10))
        data = _build_hot_batters_data(window_games)
        if _wants_html(request, view):
            show_season = (view == "season")
            return _render_hot_batters_html(data, show_season=show_season)
        return _no_cache_json(data)
    except Exception as e:
        return JSONResponse(
            status_code=500,
            content={
                "error": "hot-batters failed",
                "type": type(e).__name__,
                "message": str(e),
            },
        )


# ═══════════════════════════════════════════════════════
#  得点圏打率（RISP）機能
#  Yahoo Baseball テキスト速報からスクレイピング
# ═══════════════════════════════════════════════════════

# 得点圏（Runners In Scoring Position）: 二塁・三塁にランナーがいる状態
# テキスト速報の打席情報フォーマット:
#   「○死 走者状況」 例: 「無死走者なし」「一死一塁」「二死二塁」「無死満塁」 等
# ヒット判定: 「ヒット」「二塁打」「三塁打」「本塁打」「タイムリー」を含む打席
# アウト判定: 「三振」「ゴロ」「フライ」「ライナー」「ダブルプレー」「バントアウト」等
# ※四球・死球・バント成功・エラー出塁は打数に含めない

# ─── 走者状況 → 得点圏かどうかの判定 ───
_RISP_RUNNER_PATTERNS = [
    # 二塁にランナーがいるパターン
    r"二塁",       # 「一死二塁」「無死二塁」等
    r"二三塁",     # 「一死二三塁」等（三塁も含む）
    r"満塁",       # 「無死満塁」「一死満塁」等
    r"一二三塁",   # 満塁の別表現
    r"一三塁",     # 「一三塁」（三塁のみ得点圏）
    r"三塁",       # 「無死三塁」（二塁なし）
]

# ─── 打席結果の分類パターン ───
# ヒット（打数あり・安打あり）
_HIT_PATTERNS = [
    r"ヒット",
    r"二塁打",
    r"ツーベース",
    r"三塁打",
    r"スリーベース",
    r"本塁打",
    r"ホームラン",
    r"タイムリー",
]
# アウト（打数あり・安打なし）
_OUT_PATTERNS = [
    r"三振",
    r"ゴロ",
    r"フライ",
    r"ライナー",
    r"ファウルフライ",
    r"バントアウト",
    r"スリーバント",
    r"犠牲フライ",     # 犠飛は打数なし→別処理
    r"ゲッツー",
    r"ダブルプレー",
]
# 打数に含まない（四球・死球・バント成功・エラー出塁・敬遠 等）
_NO_AB_PATTERNS = [
    r"フォアボール",
    r"四球",
    r"死球",
    r"デッドボール",
    r"送りバント.*成功",
    r"犠牲バント",
    r"敬遠",
    r"インテンショナル",
    r"妨害",
    r"エラー.*出塁",
    r"フィルダースチョイス",  # 野選も打数なし
]


def _is_risp(runner_text: str) -> bool:
    """走者状況テキストが得点圏（二塁または三塁にランナーあり）かどうか判定"""
    # 「走者なし」は明示的に除外
    if "走者なし" in runner_text or "ランナーなし" in runner_text:
        return False
    # 一塁のみ（「一塁」単独で「二三塁」「三塁」を含まない）
    if re.fullmatch(r"[無一二三]死一塁", runner_text.strip()):
        return False
    # 得点圏パターンをチェック
    for pat in _RISP_RUNNER_PATTERNS:
        if re.search(pat, runner_text):
            return True
    return False


def _classify_at_bat(result_text: str) -> str:
    """打席結果テキストを分類: 'hit' / 'out' / 'no_ab' / 'unknown'"""
    # 四球・死球・バント・敬遠 → 打数なし
    for pat in _NO_AB_PATTERNS:
        if re.search(pat, result_text):
            return "no_ab"
    # ヒット系
    for pat in _HIT_PATTERNS:
        if re.search(pat, result_text):
            return "hit"
    # アウト系
    for pat in _OUT_PATTERNS:
        if re.search(pat, result_text):
            return "out"
    # 犠飛は明示チェック
    if re.search(r"犠牲フライ|サクリファイスフライ", result_text):
        return "no_ab"
    return "unknown"


def _parse_text_report(html: str, carp_team_name: str = "広島") -> list[dict]:
    """テキスト速報HTMLを解析し、広島打者の全打席を返す。

    Yahoo Baseball テキスト速報の HTML 構造:
      <header class="bb-liveText__head bb-liveText__head--npbTeam6"> → 広島の攻撃イニング
      <li class="bb-liveText__item"> → 各打席
        bb-liveText__order: 「N番」「代打」
        bb-liveText__player: 選手名
        bb-liveText__state: 走者状況 / 結果テキスト

    Returns:
        list of {
            "player": str,       # 選手名
            "runner": str,       # 走者状況テキスト
            "result": str,       # 結果テキスト
            "is_risp": bool,     # 得点圏か
            "ab_type": str,      # 'hit'/'out'/'no_ab'/'unknown'
            "half": str,         # '表'/'裏'
            "inning": int,       # 回
        }
    """
    def _strip(s: str) -> str:
        s = re.sub(r"<[^>]+>", " ", s)
        return re.sub(r"\s+", " ", s).strip()

    at_bats: list[dict] = []

    # HTML を「イニングブロック」ごとに分割
    # 各ブロックは <header class="bb-liveText__head ..."> から始まる
    sections = re.split(r"(?=<header\s[^>]*bb-liveText__head)", html)

    for sec in sections:
        # 広島のイニングのみ対象（bb-liveText__head--npbTeam6）
        if "bb-liveText__head--npbTeam6" not in sec:
            continue

        # イニング番号・表裏
        m_inn = re.search(r"<h1[^>]*>(\d+)回([表裏])</h1>", sec)
        inning = int(m_inn.group(1)) if m_inn else 0
        half   = m_inn.group(2)     if m_inn else ""

        # 各打席 <li class="bb-liveText__item"> を抽出
        items = re.findall(
            r'<li class="bb-liveText__item">(.*?)</li>', sec, re.DOTALL
        )

        for item in items:
            item_text = _strip(item)

            # 打者ヘッダーパターン: 「N番 選手名 走者状況」または「代打 選手名 走者状況」
            # 走者状況: [無一二三]死 + 走者テキスト
            m = re.search(
                r"(?:\d+番|代打)\s+(.+?)\s+([無一二三]死(?:走者なし|[一二三満]?塁|一二塁|一三塁|二三塁|一二三塁|満塁))",
                item_text,
            )
            if not m:
                continue

            player = m.group(1).strip()
            runner = m.group(2).strip()
            result = item_text[m.end():].strip()

            ab_type = _classify_at_bat(result)
            is_risp = _is_risp(runner)

            at_bats.append({
                "player":  player,
                "runner":  runner,
                "result":  result[:120],
                "is_risp": is_risp,
                "ab_type": ab_type,
                "half":    half,
                "inning":  inning,
            })

    return at_bats


def _fetch_risp_for_game(game_id: str) -> list[dict]:
    """1試合分のテキスト速報から広島打者の打席データを取得（キャッシュ付き）"""
    cache_bucket = _cache_get_bucket("risp")
    cache_key = f"game:{game_id}"
    cache_entry = cache_bucket.get(cache_key)
    if _cache_alive(cache_entry):
        return cache_entry.get("value", [])

    url = YAHOO_GAME_TEXT_URL.format(game_id=game_id)
    try:
        html = _fetch_html(url)
        at_bats = _parse_text_report(html)
        cache_bucket[cache_key] = {"value": at_bats, "expires_at": _cache_now() + CACHE_TTL_RISP}
        print(f"DEBUG_RISP_GAME {game_id}: {len(at_bats)} at-bats parsed")
        return at_bats
    except Exception as e:
        print(f"DEBUG_RISP_GAME_ERROR {game_id}: {e}")
        return []


def _fetch_carp_finished_game_ids_from_team_schedule() -> list[str]:
    """広島チーム専用スケジュールページから「試合終了」のゲームIDを古い順で返す。

    URL: https://baseball.yahoo.co.jp/npb/teams/6/schedule
    このページには広島の試合のみが含まれるため teams/6 フィルタ不要。
    「試合終了」テキストを持つリンクのゲームIDを順に抽出する。
    重複除去・順序維持（古い順）で返す。
    """
    url = f"https://baseball.yahoo.co.jp/npb/teams/{CARP_TEAM_ID}/schedule"
    try:
        html = _fetch_html(url)
    except Exception as e:
        print(f"DEBUG_RISP_TEAM_SCHEDULE_ERROR: {e}")
        return []

    # /npb/game/(ID)/index">試合終了 パターンで抽出（広島試合のみ含まれる）
    raw_ids = re.findall(r'/npb/game/(\d+)/index[^"]*">\s*試合終了', html)

    # 重複除去（順序維持）
    seen: set[str] = set()
    unique_ids: list[str] = []
    for gid in raw_ids:
        if gid not in seen:
            seen.add(gid)
            unique_ids.append(gid)

    print(f"DEBUG_RISP_TEAM_SCHEDULE: {len(unique_ids)} finished games found")
    return unique_ids  # 古い順


def _get_game_date_from_text_page(game_id: str, html: str) -> str:
    """テキスト速報HTMLから試合日付を 'YYYY-MM-DD' 形式で返す。

    ページ本文に含まれる「YYYY年M月D日」パターンを探す。
    見つからない場合は空文字を返す。
    """
    m = re.search(r'(\d{4})年(\d{1,2})月(\d{1,2})日', html)
    if m:
        return f"{m.group(1)}-{int(m.group(2)):02d}-{int(m.group(3)):02d}"
    return ""


def _get_recent_carp_game_ids(num_games: int = 5) -> list[tuple[str, str]]:
    """直近 num_games 試合の (game_id, date_str) リストを返す（新しい順）。

    広島チームスケジュールページから「試合終了」ゲームIDを取得し、
    テキスト速報ページに広島攻撃イニング (bb-liveText__head--npbTeam6) が
    存在するものだけを広島試合として採用する。
    各試合の日付はテキスト速報ページ本文内「YYYY年M月D日」から取得する。

    Returns:
        [(game_id, 'YYYY-MM-DD'), ...] 新しい順
    """
    cache_bucket = _cache_get_bucket("risp")
    cache_key = f"game_ids:{num_games}"
    cache_entry = cache_bucket.get(cache_key)
    if _cache_alive(cache_entry):
        return cache_entry.get("value", [])

    # チームスケジュールから完了試合IDを取得（古い順）
    all_finished = _fetch_carp_finished_game_ids_from_team_schedule()

    if not all_finished:
        return []

    # 新しい順（末尾から）に走査し、広島出場確認済みの num_games 件を収集
    # スケジュールページに他球団試合が混入する場合があるため npbTeam6 でフィルタ
    found: list[tuple[str, str]] = []
    for gid in reversed(all_finished):
        if len(found) >= num_games:
            break
        url = YAHOO_GAME_TEXT_URL.format(game_id=gid)
        try:
            html = _fetch_html(url)
            # 広島の攻撃イニングが存在する試合のみ採用
            if "bb-liveText__head--npbTeam6" not in html:
                print(f"DEBUG_RISP_SKIP {gid}: no Carp inning found, skipping")
                continue
            date_str = _get_game_date_from_text_page(gid, html)
            found.append((gid, date_str))
            print(f"DEBUG_RISP_GAME_ID {gid}: date={date_str}")
        except Exception as e:
            print(f"DEBUG_RISP_GAME_ID_ERROR {gid}: {e}")

    cache_bucket[cache_key] = {"value": found, "expires_at": _cache_now() + CACHE_TTL_RISP}
    return found

# ────────────────────────────────────────────────────────────
#  game-recap: 試合要約
# ────────────────────────────────────────────────────────────

# チーム略称 → 表示名マップ
_TEAM_SHORT: dict[str, str] = {
    "阪神タイガース":     "阪神",
    "読売ジャイアンツ":   "巨人",
    "横浜DeNAベイスターズ": "DeNA",
    "中日ドラゴンズ":     "中日",
    "東京ヤクルトスワローズ": "ヤクルト",
    "広島東洋カープ":     "広島",
    "福岡ソフトバンクホークス": "ソフトバンク",
    "埼玉西武ライオンズ": "西武",
    "東北楽天ゴールデンイーグルス": "楽天",
    "千葉ロッテマリーンズ": "ロッテ",
    "北海道日本ハムファイターズ": "日本ハム",
    "オリックス・バファローズ": "オリックス",
}

def _shorten_team(name: str) -> str:
    return _TEAM_SHORT.get(name.strip(), name.strip())


def _build_game_recap(game_id: str, date_str: str, html: str) -> dict:
    """テキスト速報HTMLから試合要約を生成"""
    # タイトルから対戦チーム
    title_m = re.search(r"(\d{4})年(\d{1,2})月(\d{1,2})日\s+(.+?)vs\.(.+?)\s+試合", html)
    if title_m:
        team1 = _shorten_team(title_m.group(4))
        team2 = _shorten_team(title_m.group(5))
    else:
        team1, team2 = "?", "広島"

    # 広島が先攻か後攻かを判定
    is_home = bool(re.search(r"後攻:広島|ホーム:広島", html))
    away_team = team2 if is_home else team1
    home_team = team1 if is_home else team2
    carp_team = "広島"
    opp_team  = away_team if carp_team == home_team else home_team

    # 最終スコア（最後のスコア表記）
    score_all = re.findall(r"([^\s]{1,6})\s+(\d+)\s*-\s*(\d+)\s+([^\s]{1,6})", html)
    carp_score: int | None = None
    opp_score: int | None = None
    if score_all:
        last = score_all[-1]
        # どちらが広島？
        s1, n1, n2, s2 = last
        if "広" in s1 or "広島" in s1:
            carp_score, opp_score = int(n1), int(n2)
        elif "広" in s2 or "広島" in s2:
            carp_score, opp_score = int(n2), int(n1)
        else:
            # 先攻/後攻で判定
            carp_score = int(n1) if not is_home else int(n2)
            opp_score  = int(n2) if not is_home else int(n1)

    # 勝敗
    if carp_score is not None and opp_score is not None:
        result = "勝" if carp_score > opp_score else ("負" if carp_score < opp_score else "分")
    else:
        result = "?"

    # 打席データ
    at_bats = _parse_text_report(html)

    # ハイライト: タイムリー・本塁打・犠牲フライ
    hr_players: list[str] = []
    timely_players: list[str] = []
    sf_players: list[str] = []
    for ab in at_bats:
        res = ab.get("result", "")
        player = ab["player"]
        if re.search(r"本塁打|ホームラン", res):
            hr_players.append(player)
        elif re.search(r"タイムリー", res):
            timely_players.append(player)
        elif re.search(r"犠牲フライ|犠飛", res):
            sf_players.append(player)

    # 選手別安打集計
    hit_counts: dict[str, int] = {}
    for ab in at_bats:
        if ab.get("ab_type") == "hit":
            p = ab["player"]
            hit_counts[p] = hit_counts.get(p, 0) + 1
    multi_hit = [p for p, c in sorted(hit_counts.items(), key=lambda x: -x[1]) if c >= 2]

    # 先発投手
    starter_m = re.search(r"広島が([^\s、」<]{2,8})(?:が|は|の)?(?:先発|登板|マウンド)", html)
    if not starter_m:
        starter_m = re.search(r"先発ピッチャーは.*?広島が([^\s、」<]{2,8})", html)
    starter = starter_m.group(1) if starter_m else None

    # 要約文を生成
    summary_parts: list[str] = []

    # スコア行
    if carp_score is not None:
        score_str = f"{carp_score}－{opp_score}"
        if result == "勝":
            summary_parts.append(f"{opp_team}に{score_str}で勝利。")
        elif result == "負":
            summary_parts.append(f"{opp_team}に{score_str}で敗戦。")
        else:
            summary_parts.append(f"{opp_team}と{score_str}で引き分け。")
    else:
        summary_parts.append(f"{opp_team}戦。")

    # 得点シーン
    if hr_players:
        # 重複を除いて数を表示
        from collections import Counter
        hr_cnt = Counter(hr_players)
        hr_parts = [f"{p}の{c}本塁打" if c > 1 else f"{p}の本塁打" for p, c in hr_cnt.items()]
        summary_parts.append("、".join(hr_parts) + "が飛び出した。")
    if timely_players:
        uniq = list(dict.fromkeys(timely_players))
        summary_parts.append("、".join(uniq[:3]) + "がタイムリーを放った。")
    if sf_players and not (hr_players or timely_players):
        summary_parts.append("、".join(sf_players[:2]) + "が犠牲フライで得点。")

    # マルチヒット
    if multi_hit:
        summary_parts.append("、".join(multi_hit[:3]) + "がマルチ安打。")

    # 安打ゼロ・僅少
    total_hits = sum(hit_counts.values())
    if total_hits == 0:
        summary_parts.append("広島打線はノーヒット。")
    elif total_hits <= 3:
        summary_parts.append(f"広島の安打は{total_hits}本に終わった。")

    if not summary_parts:
        summary_parts.append("試合データを取得しました。")

    return {
        "game_id":    game_id,
        "date":       date_str,
        "opp_team":   opp_team,
        "carp_score": carp_score,
        "opp_score":  opp_score,
        "result":     result,
        "total_hits": total_hits,
        "hr_players": list(dict.fromkeys(hr_players)),
        "timely_players": list(dict.fromkeys(timely_players)),
        "multi_hit":  multi_hit[:5],
        "starter":    starter,
        "summary":    "".join(summary_parts),
    }


def _build_game_recap_data(num_games: int = 10) -> dict:
    """直近 num_games 試合の要約データを構築（キャッシュ10分）"""
    cache_bucket = _cache_get_bucket("risp")
    cache_key    = f"game_recap:{num_games}"
    cache_entry  = cache_bucket.get(cache_key)
    if _cache_alive(cache_entry):
        cached = cache_entry.get("value")
        if isinstance(cached, dict):
            return cached

    all_finished = _fetch_carp_finished_game_ids_from_team_schedule()
    games: list[dict] = []
    for gid in reversed(all_finished):
        if len(games) >= num_games:
            break
        try:
            html = _fetch_html(YAHOO_GAME_TEXT_URL.format(game_id=gid))
            if "bb-liveText__head--npbTeam6" not in html:
                continue
            date_str = _get_game_date_from_text_page(gid, html)
            recap = _build_game_recap(gid, date_str, html)
            games.append(recap)
        except Exception as e:
            print(f"DEBUG_RECAP_ERROR {gid}: {e}")

    result = {
        "games":        games,
        "num_games":    len(games),
        "generated_at": _now_jst().isoformat(),
    }
    cache_bucket[cache_key] = {"value": result, "expires_at": _cache_now() + CACHE_TTL_RISP}
    return result


def _render_game_recap_html(data: dict) -> HTMLResponse:
    games       = data.get("games", [])
    generated_at = data.get("generated_at", "")

    cards_html = ""
    for g in games:
        result   = g.get("result", "?")
        cs       = g.get("carp_score")
        os_      = g.get("opp_score")
        opp      = g.get("opp_team", "?")
        date     = g.get("date", "")
        summary  = g.get("summary", "")
        hits     = g.get("total_hits", 0)
        hrs      = g.get("hr_players", [])
        timely   = g.get("timely_players", [])
        multi    = g.get("multi_hit", [])
        starter  = g.get("starter")

        score_str = f"{cs}－{os_}" if cs is not None else "-"

        if result == "勝":
            result_color  = "#4ade80"
            result_bg     = "rgba(74,222,128,.12)"
            result_border = "#4ade80"
        elif result == "負":
            result_color  = "#f87171"
            result_bg     = "rgba(248,113,113,.10)"
            result_border = "#f87171"
        else:
            result_color  = "#ffd54a"
            result_bg     = "rgba(255,213,74,.10)"
            result_border = "#ffd54a"

        # バッジ行
        badges = ""
        for hr in hrs:
            badges += f'<span class="gr-badge gr-hr">{escape(hr)} HR</span>'
        for t in timely[:3]:
            badges += f'<span class="gr-badge gr-timely">{escape(t)} タイムリー</span>'
        for m in multi[:3]:
            badges += f'<span class="gr-badge gr-multi">{escape(m)} マルチ</span>'

        starter_html = f'<span class="gr-starter">先発: {escape(starter)}</span>' if starter else ""

        cards_html += f"""
        <div class="gr-card" style="border-color:{result_border};background:linear-gradient(135deg,{result_bg},rgba(11,20,36,.9))">
          <div class="gr-card-top">
            <div class="gr-date-opp">
              <span class="gr-date">{escape(date)}</span>
              <span class="gr-opp">vs {escape(opp)}</span>
              {starter_html}
            </div>
            <div class="gr-score-wrap">
              <span class="gr-result" style="color:{result_color}">{result}</span>
              <span class="gr-score" style="color:{result_color}">{score_str}</span>
            </div>
          </div>
          <p class="gr-summary">{escape(summary)}</p>
          {f'<div class="gr-badges">{badges}</div>' if badges else ''}
        </div>"""

    if not cards_html:
        cards_html = '<div style="text-align:center;color:#5a6e94;padding:40px 0">試合データなし</div>'

    body = f"""
    <style>
      .gr-card {{
        border: 1px solid #1e2d50;
        border-radius: 14px;
        padding: 18px 20px;
        margin-bottom: 12px;
      }}
      .gr-card:last-child {{ margin-bottom: 0; }}
      .gr-card-top {{
        display: flex;
        align-items: flex-start;
        justify-content: space-between;
        margin-bottom: 10px;
        gap: 12px;
      }}
      .gr-date-opp {{
        display: flex;
        flex-direction: column;
        gap: 3px;
      }}
      .gr-date {{
        font-size: 12px;
        color: #8494b8;
        font-weight: 700;
      }}
      .gr-opp {{
        font-size: 20px;
        font-weight: 900;
        color: #c8d8f4;
      }}
      .gr-starter {{
        font-size: 11px;
        color: #5a6e94;
        margin-top: 2px;
      }}
      .gr-score-wrap {{
        display: flex;
        flex-direction: column;
        align-items: flex-end;
        gap: 2px;
        flex-shrink: 0;
      }}
      .gr-result {{
        font-size: 13px;
        font-weight: 900;
        letter-spacing: .08em;
      }}
      .gr-score {{
        font-size: 28px;
        font-weight: 900;
        letter-spacing: .04em;
        line-height: 1;
      }}
      .gr-summary {{
        font-size: 14px;
        color: #c8d8f4;
        line-height: 1.7;
        margin: 0 0 8px;
      }}
      .gr-badges {{
        display: flex;
        flex-wrap: wrap;
        gap: 6px;
        margin-top: 6px;
      }}
      .gr-badge {{
        font-size: 11px;
        font-weight: 700;
        padding: 2px 8px;
        border-radius: 999px;
      }}
      .gr-hr     {{ background: rgba(255,99,99,.18); color: #ff7070; border: 1px solid #ff7070; }}
      .gr-timely {{ background: rgba(74,222,128,.14); color: #4ade80; border: 1px solid #4ade80; }}
      .gr-multi  {{ background: rgba(96,165,250,.14); color: #60a5fa; border: 1px solid #60a5fa; }}
      @media (max-width: 480px) {{
        .gr-opp   {{ font-size: 17px; }}
        .gr-score {{ font-size: 22px; }}
      }}
    </style>

    <div class="hero">
      <h1>試合一覧</h1>
      <div class="muted">広島東洋カープ 直近試合 / 生成 {generated_at}</div>
      {_common_nav("game-recap")}
    </div>

    <div class="card">
      <div class="card-title">直近試合 結果・要約</div>
      {cards_html}
    </div>
    """
    return _html_page("試合一覧", body)


@router.get("/public/game-recap")
def public_game_recap(request: Request, view: str | None = None):
    """広島の直近試合一覧と要約"""
    try:
        data = _build_game_recap_data(num_games=10)
        if _wants_html(request, view):
            return _render_game_recap_html(data)
        return _no_cache_json(data)
    except Exception as e:
        return JSONResponse(
            status_code=500,
            content={
                "error": "game-recap failed",
                "type": type(e).__name__,
                "message": str(e),
            },
        )


def _build_season_risp_data() -> dict:
    """今シーズン全試合の得点圏打率データを構築（通算ランキング用）。

    直近版と同じ `_fetch_risp_for_game` を使い、チームスケジュールの
    全完了試合を集計する。計算コストが高いため6時間キャッシュ。
    """
    cache_bucket = _cache_get_bucket("risp")
    cache_key = "season_risp"
    cache_entry = cache_bucket.get(cache_key)
    if _cache_alive(cache_entry):
        cached = cache_entry.get("value")
        if isinstance(cached, dict):
            return cached

    all_finished = _fetch_carp_finished_game_ids_from_team_schedule()
    if not all_finished:
        result = {"games_found": 0, "players": [], "generated_at": _now_jst().isoformat()}
        cache_bucket[cache_key] = {"value": result, "expires_at": _cache_now() + 60 * 30}
        return result

    player_stats: dict[str, dict] = {}
    games_found = 0

    for gid in all_finished:
        try:
            at_bats = _fetch_risp_for_game(gid)
        except Exception:
            continue

        if not at_bats:
            continue

        games_found += 1
        for ab in at_bats:
            pname = ab["player"]
            if pname not in player_stats:
                player_stats[pname] = {
                    "risp_ab": 0, "risp_hit": 0,
                    "total_ab": 0, "total_hit": 0,
                    "bb": 0, "hbp": 0, "sf": 0, "rbi": 0,
                }
            ps = player_stats[pname]
            ab_type = ab["ab_type"]
            is_risp = ab["is_risp"]
            result_text = ab.get("result", "")

            if ab_type in ("hit", "out"):
                ps["total_ab"] += 1
                if ab_type == "hit":
                    ps["total_hit"] += 1

            if is_risp and ab_type in ("hit", "out"):
                ps["risp_ab"] += 1
                if ab_type == "hit":
                    ps["risp_hit"] += 1

            if ab_type == "no_ab":
                if re.search(r"四球|フォアボール", result_text):
                    ps["bb"] += 1
                elif re.search(r"死球|デッドボール", result_text):
                    ps["hbp"] += 1

            if re.search(r"犠牲フライ|サクリファイスフライ", result_text):
                ps["sf"] += 1

            if re.search(r"タイムリー|本塁打|ホームラン|犠牲フライ|サクリファイスフライ|犠飛", result_text):
                m = re.search(r"(\d+)[点本].*(?:タイムリー|打点)", result_text)
                m2 = re.search(r"(\d+)ラン", result_text)
                if m:
                    ps["rbi"] += int(m.group(1))
                elif m2:
                    ps["rbi"] += int(m2.group(1))
                else:
                    ps["rbi"] += 1

    rows = []
    for pname, ps in player_stats.items():
        risp_avg  = (ps["risp_hit"] / ps["risp_ab"]) if ps["risp_ab"] > 0 else None
        obp_denom = ps["total_ab"] + ps["bb"] + ps["hbp"] + ps["sf"]
        obp       = ((ps["total_hit"] + ps["bb"] + ps["hbp"]) / obp_denom) if obp_denom > 0 else None
        rows.append({
            "player":    pname,
            "risp_ab":   ps["risp_ab"],
            "risp_hit":  ps["risp_hit"],
            "risp_avg":  round(risp_avg, 3) if risp_avg is not None else None,
            "total_ab":  ps["total_ab"],
            "total_hit": ps["total_hit"],
            "bb": ps["bb"], "hbp": ps["hbp"], "sf": ps["sf"], "rbi": ps["rbi"],
            "obp": round(obp, 3) if obp is not None else None,
        })

    rows.sort(key=lambda r: (-r["risp_ab"], r["player"]))
    result = {"games_found": games_found, "players": rows, "generated_at": _now_jst().isoformat()}
    # 通算は6時間キャッシュ（試合ごとに大きく変わらない）
    cache_bucket[cache_key] = {"value": result, "expires_at": _cache_now() + 60 * 60 * 6}
    return result


def _render_season_risp_html(window_games: int) -> str:
    """通算得点圏ランキング HTML（得点圏打率・出塁率・打点の3カラム）。

    直近版 `_render_risp_html` と同じUIで、今シーズン全試合の通算データを表示。
    最低出場要件: 得点圏打数 >= 5 / OBP は打席数 >= 15
    """
    data = _build_season_risp_data()
    players    = data.get("players", [])
    games_found = data.get("games_found", 0)
    generated_at = data.get("generated_at", "")

    MIN_RISP_AB = 5   # 得点圏打数の最低ライン
    MIN_PA      = 15  # 出塁率・打点の最低打席数

    def _enough_pa(r: dict) -> bool:
        pa = r.get("total_ab", 0) + r.get("bb", 0) + r.get("hbp", 0) + r.get("sf", 0)
        return pa >= MIN_PA

    # ─── 列1: 得点圏打率（risp_ab >= MIN_RISP_AB）───
    risp_ranked = sorted(
        [r for r in players if r.get("risp_ab", 0) >= MIN_RISP_AB and r.get("risp_avg") is not None],
        key=lambda r: (-(r.get("risp_avg") or 0.0), -r.get("risp_hit", 0)),
    )[:7]

    # ─── 列2: 出塁率（pa >= MIN_PA）───
    obp_ranked = sorted(
        [r for r in players if _enough_pa(r) and r.get("obp") is not None],
        key=lambda r: (-(r.get("obp") or 0.0), -r.get("total_ab", 0)),
    )[:7]

    # ─── 列3: 打点（rbi >= 1 & pa >= MIN_PA）───
    rbi_ranked = sorted(
        [r for r in players if _enough_pa(r) and r.get("rbi", 0) >= 1],
        key=lambda r: (-r.get("rbi", 0), -(r.get("obp") or 0.0)),
    )[:7]

    RANK_COLORS = {1: "#ffd54a", 2: "#b0c4de", 3: "#cd8f5a", 4: "#7a8fb8", 5: "#7a8fb8", 6: "#7a8fb8", 7: "#7a8fb8"}

    def _rank_badge(rank: int) -> str:
        color = RANK_COLORS.get(rank, "#7a8fb8")
        return f'<span class="rc-rank" style="color:{color}">{rank}</span>'

    def _avg_color(val: float) -> str:
        if val >= 0.500: return "#ffd54a"
        if val >= 0.400: return "#ff9e4a"
        if val >= 0.333: return "#4ade80"
        if val >= 0.250: return "#60a5fa"
        return "#c8d8f4"

    def _col_risp(ranked: list) -> str:
        rows_html = ""
        for rank, r in enumerate(ranked, 1):
            avg = r.get("risp_avg") or 0.0
            color = _avg_color(avg)
            ab = r.get("risp_ab", 0); hit = r.get("risp_hit", 0)
            rows_html += f"""
            <div class="rc-row">
              {_rank_badge(rank)}
              <span class="rc-name">{escape(r['player'])}</span>
              <span class="rc-val" style="color:{color}">{_fmt_avg(avg)}</span>
              <span class="rc-sub">{hit}/{ab}</span>
            </div>"""
        return rows_html or '<div class="rc-empty">データなし</div>'

    def _col_obp(ranked: list) -> str:
        rows_html = ""
        for rank, r in enumerate(ranked, 1):
            obp = r.get("obp") or 0.0
            color = _avg_color(obp)
            pa = r.get("total_ab", 0) + r.get("bb", 0) + r.get("hbp", 0) + r.get("sf", 0)
            rows_html += f"""
            <div class="rc-row">
              {_rank_badge(rank)}
              <span class="rc-name">{escape(r['player'])}</span>
              <span class="rc-val" style="color:{color}">{_fmt_avg(obp)}</span>
              <span class="rc-sub">{pa}打席</span>
            </div>"""
        return rows_html or '<div class="rc-empty">データなし</div>'

    def _col_rbi(ranked: list) -> str:
        rows_html = ""
        for rank, r in enumerate(ranked, 1):
            rbi = r.get("rbi", 0)
            pa  = r.get("total_ab", 0) + r.get("bb", 0) + r.get("hbp", 0) + r.get("sf", 0)
            rows_html += f"""
            <div class="rc-row">
              {_rank_badge(rank)}
              <span class="rc-name">{escape(r['player'])}</span>
              <span class="rc-val rc-rbi-val">{rbi}</span>
              <span class="rc-sub">{pa}打席</span>
            </div>"""
        return rows_html or '<div class="rc-empty">データなし</div>'

    return f"""
    <div id="season-content">
    <div class="card" style="margin-top:14px">
      <div class="card-title">今シーズン通算 得点圏・出塁・打点ランキング</div>
      <p style="font-size:11px;color:#5a6e94;margin:4px 0 14px">
        対象: 得点圏打数{MIN_RISP_AB}以上 / 打席数{MIN_PA}以上 ／ {games_found}試合集計 ／ 生成 {generated_at[:16]}
      </p>
      <div class="rc-grid">
        <div class="rc-col">
          <div class="rc-col-header"><div class="rc-col-title">得点圏打率</div></div>
          <div class="rc-col-body">{_col_risp(risp_ranked)}</div>
        </div>
        <div class="rc-col">
          <div class="rc-col-header"><div class="rc-col-title">出塁率</div></div>
          <div class="rc-col-body">{_col_obp(obp_ranked)}</div>
        </div>
        <div class="rc-col">
          <div class="rc-col-header"><div class="rc-col-title">打点</div></div>
          <div class="rc-col-body">{_col_rbi(rbi_ranked)}</div>
        </div>
      </div>
    </div>
    </div><!-- /#season-content -->
    """


def _build_risp_data(window_games: int = 5) -> dict:
    """直近 window_games 試合の得点圏打率データを構築"""
    cache_bucket = _cache_get_bucket("risp")
    cache_key = f"risp:{window_games}"
    cache_entry = cache_bucket.get(cache_key)
    if _cache_alive(cache_entry):
        cached = cache_entry.get("value")
        if isinstance(cached, dict):
            return cached

    game_list = _get_recent_carp_game_ids(window_games)

    # 選手別集計
    # player_name → {risp_ab, risp_hit, total_ab, total_hit, bb, hbp, sf, rbi}
    player_stats: dict[str, dict] = {}
    game_details: list[dict] = []

    for game_id, date_str in game_list:
        at_bats = _fetch_risp_for_game(game_id)
        game_risp_count = 0
        game_at_bats = []

        for ab in at_bats:
            pname = ab["player"]
            if pname not in player_stats:
                player_stats[pname] = {
                    "risp_ab": 0, "risp_hit": 0,
                    "total_ab": 0, "total_hit": 0,
                    "bb": 0, "hbp": 0, "sf": 0, "rbi": 0,
                }
            ps = player_stats[pname]

            ab_type = ab["ab_type"]
            is_risp = ab["is_risp"]
            result_text = ab.get("result", "")

            # 打数カウント（四球・死球・バント・敬遠は除く）
            if ab_type in ("hit", "out"):
                ps["total_ab"] += 1
                if ab_type == "hit":
                    ps["total_hit"] += 1

            # 得点圏での打数
            if is_risp and ab_type in ("hit", "out"):
                ps["risp_ab"] += 1
                game_risp_count += 1
                if ab_type == "hit":
                    ps["risp_hit"] += 1

            # 四球・死球（OBP分子・分母両方に加算）
            if ab_type == "no_ab":
                if re.search(r"四球|フォアボール", result_text):
                    ps["bb"] += 1
                elif re.search(r"死球|デッドボール", result_text):
                    ps["hbp"] += 1

            # 犠牲フライ（OBP分母のみ加算）
            if re.search(r"犠牲フライ|サクリファイスフライ", result_text):
                ps["sf"] += 1

            # 打点（タイムリー・本塁打・犠牲フライ等）
            if re.search(r"タイムリー|本塁打|ホームラン|犠牲フライ|サクリファイスフライ|犠飛", result_text):
                # 「2点タイムリー」「3ランホームラン」等の点数を抽出、取れなければ1
                m = re.search(r"(\d+)[点本].*(?:タイムリー|打点)", result_text)
                m2 = re.search(r"(\d+)ラン", result_text)  # 2ラン、3ラン
                if m:
                    ps["rbi"] += int(m.group(1))
                elif m2:
                    ps["rbi"] += int(m2.group(1))
                else:
                    ps["rbi"] += 1

            game_at_bats.append(ab)

        game_details.append({
            "game_id":    game_id,
            "date":       date_str,
            "at_bats":    game_at_bats,
            "risp_count": game_risp_count,
        })

    # 選手別サマリーを作成（得点圏打席ゼロの選手は除外）
    rows = []
    for pname, ps in player_stats.items():
        risp_avg = (ps["risp_hit"] / ps["risp_ab"]) if ps["risp_ab"] > 0 else None
        total_avg = (ps["total_hit"] / ps["total_ab"]) if ps["total_ab"] > 0 else None
        # OBP: (安打 + 四球 + 死球) / (打数 + 四球 + 死球 + 犠飛)
        obp_denom = ps["total_ab"] + ps["bb"] + ps["hbp"] + ps["sf"]
        obp = ((ps["total_hit"] + ps["bb"] + ps["hbp"]) / obp_denom) if obp_denom > 0 else None
        rows.append({
            "player":     pname,
            "risp_ab":    ps["risp_ab"],
            "risp_hit":   ps["risp_hit"],
            "risp_avg":   round(risp_avg, 3) if risp_avg is not None else None,
            "total_ab":   ps["total_ab"],
            "total_hit":  ps["total_hit"],
            "total_avg":  round(total_avg, 3) if total_avg is not None else None,
            "bb":         ps["bb"],
            "hbp":        ps["hbp"],
            "sf":         ps["sf"],
            "rbi":        ps["rbi"],
            "obp":        round(obp, 3) if obp is not None else None,
        })

    # 得点圏打席数降順でソート
    rows.sort(key=lambda r: (-r["risp_ab"], r["player"]))

    result = {
        "window_games":  window_games,
        "games_found":   len(game_list),
        "game_list":     [{"game_id": g, "date": d} for g, d in game_list],
        "players":       rows,
        "generated_at":  _now_jst().isoformat(),
    }
    cache_bucket[cache_key] = {"value": result, "expires_at": _cache_now() + CACHE_TTL_RISP}
    return result


def _fmt_avg(val) -> str:
    """打率を .XXX 形式でフォーマット（Noneは '---'）"""
    if val is None:
        return "---"
    if isinstance(val, float):
        return f".{int(round(val * 1000)):03d}"
    return str(val)


def _render_risp_html(data: dict, window_games: int, show_season: bool = False) -> HTMLResponse:
    """得点圏打率ページのHTML生成 — 3カラムランキング（得点圏打率・出塁率・打点）"""
    players    = data.get("players", [])
    games_found = data.get("games_found", 0)
    generated_at = data.get("generated_at", "")
    game_list  = data.get("game_list", [])

    # ─── 一軍登録選手セットを取得（正規化済み = スペース除去）───
    try:
        active_set = _fetch_current_first_team_position_players()
        active_normalized = {_normalize_name(n) for n in active_set}
    except Exception:
        active_normalized = set()  # 取得失敗時はフィルタなし（全員表示）

    def _is_active(player_name: str) -> bool:
        if not active_normalized:
            return True
        return _normalize_name(player_name) in active_normalized

    # 一軍登録中の選手のみ対象
    active_players = [r for r in players if _is_active(r["player"])]

    # ─── 列1: 得点圏打率ランキング（得点圏安打≧1）───
    risp_ranked = sorted(
        [r for r in active_players if r.get("risp_hit", 0) >= 1],
        key=lambda r: (
            -(r.get("risp_avg") or 0.0),
            -r.get("risp_hit", 0),
            -r.get("risp_ab", 0),
        ),
    )[:5]

    # ─── 列2: 出塁率ランキング（打席≧1）───
    obp_ranked = sorted(
        [r for r in active_players if (r.get("total_ab", 0) + r.get("bb", 0) + r.get("hbp", 0)) >= 1 and r.get("obp") is not None],
        key=lambda r: (
            -(r.get("obp") or 0.0),
            -r.get("total_ab", 0),
        ),
    )[:5]

    # ─── 列3: 打点ランキング（打点≧1）───
    rbi_ranked = sorted(
        [r for r in active_players if r.get("rbi", 0) >= 1],
        key=lambda r: (
            -r.get("rbi", 0),
            -(r.get("obp") or 0.0),
        ),
    )[:5]

    # ─── 行番号ラベル ───
    RANK_COLORS = {1: "#ffd54a", 2: "#b0c4de", 3: "#cd8f5a", 4: "#7a8fb8", 5: "#7a8fb8"}

    def _rank_badge(rank: int) -> str:
        color = RANK_COLORS.get(rank, "#7a8fb8")
        return f'<span class="rc-rank" style="color:{color}">{rank}</span>'

    def _avg_color(val: float) -> str:
        if val >= 0.500: return "#ffd54a"
        if val >= 0.400: return "#ff9e4a"
        if val >= 0.333: return "#4ade80"
        if val >= 0.250: return "#60a5fa"
        return "#c8d8f4"

    # ─── 列HTML生成ヘルパー ───
    def _col_risp(ranked: list) -> str:
        rows_html = ""
        for rank, r in enumerate(ranked, 1):
            avg = r.get("risp_avg") or 0.0
            avg_str = _fmt_avg(avg)
            ab = r.get("risp_ab", 0)
            hit = r.get("risp_hit", 0)
            color = _avg_color(avg)
            rows_html += f"""
            <div class="rc-row">
              {_rank_badge(rank)}
              <span class="rc-name">{escape(r['player'])}</span>
              <span class="rc-val" style="color:{color}">{avg_str}</span>
              <span class="rc-sub">{hit}/{ab}</span>
            </div>"""
        if not rows_html:
            rows_html = '<div class="rc-empty">データなし</div>'
        return rows_html

    def _col_obp(ranked: list) -> str:
        rows_html = ""
        for rank, r in enumerate(ranked, 1):
            obp = r.get("obp") or 0.0
            obp_str = _fmt_avg(obp)
            color = _avg_color(obp)
            ab = r.get("total_ab", 0)
            bb = r.get("bb", 0)
            hbp = r.get("hbp", 0)
            pa = ab + bb + hbp + r.get("sf", 0)
            rows_html += f"""
            <div class="rc-row">
              {_rank_badge(rank)}
              <span class="rc-name">{escape(r['player'])}</span>
              <span class="rc-val" style="color:{color}">{obp_str}</span>
              <span class="rc-sub">{pa}打席</span>
            </div>"""
        if not rows_html:
            rows_html = '<div class="rc-empty">データなし</div>'
        return rows_html

    def _col_rbi(ranked: list) -> str:
        rows_html = ""
        for rank, r in enumerate(ranked, 1):
            rbi = r.get("rbi", 0)
            rows_html += f"""
            <div class="rc-row">
              {_rank_badge(rank)}
              <span class="rc-name">{escape(r['player'])}</span>
              <span class="rc-val rc-rbi-val">{rbi}</span>
              <span class="rc-sub">打点</span>
            </div>"""
        if not rows_html:
            rows_html = '<div class="rc-empty">データなし</div>'
        return rows_html

    risp_col_html = _col_risp(risp_ranked)
    obp_col_html  = _col_obp(obp_ranked)
    rbi_col_html  = _col_rbi(rbi_ranked)

    # ─── 集計対象試合バッジ ───
    game_badges = ""
    for g in game_list:
        game_badges += f'<span class="game-badge">{g["date"]}</span>'

    body = f"""
    <style>
      /* ─── 3カラムランキング ─── */
      .rc-grid {{
        display: grid;
        grid-template-columns: repeat(3, 1fr);
        gap: 12px;
        margin-top: 4px;
      }}
      .rc-col {{
        background: #0b1424;
        border: 1px solid #1e2d50;
        border-radius: 12px;
        overflow: hidden;
      }}
      .rc-col-header {{
        background: linear-gradient(135deg, #0f1e3a, #0a1628);
        border-bottom: 1px solid #1e2d50;
        padding: 12px 14px 10px;
        text-align: center;
      }}
      .rc-col-title {{
        font-size: 14px;
        font-weight: 800;
        color: #c8d8f4;
        letter-spacing: 0.05em;
      }}
      .rc-col-body {{
        padding: 8px 6px;
      }}
      .rc-row {{
        display: flex;
        align-items: center;
        gap: 6px;
        padding: 9px 8px;
        border-radius: 8px;
        margin-bottom: 4px;
        background: #0f1829;
        border: 1px solid #1a2740;
        min-width: 0;
      }}
      .rc-row:last-child {{ margin-bottom: 0; }}
      .rc-rank {{
        font-size: 15px;
        font-weight: 900;
        min-width: 18px;
        text-align: center;
        flex-shrink: 0;
      }}
      .rc-name {{
        font-size: 16px;
        font-weight: 800;
        color: #ffffff;
        flex: 1;
        min-width: 0;
        white-space: nowrap;
        overflow: hidden;
        text-overflow: ellipsis;
      }}
      .rc-val {{
        font-size: 18px;
        font-weight: 900;
        letter-spacing: 0.02em;
        flex-shrink: 0;
      }}
      .rc-rbi-val {{
        color: #ff9e4a;
      }}
      .rc-sub {{
        font-size: 11px;
        color: #4a5e84;
        flex-shrink: 0;
        white-space: nowrap;
      }}
      .rc-empty {{
        text-align: center;
        color: #5a6e94;
        padding: 24px 0;
        font-size: 13px;
      }}
      .game-badge {{
        display: inline-block;
        background: #0f1829;
        border: 1px solid #1e2d50;
        border-radius: 5px;
        padding: 2px 8px;
        font-size: 11px;
        color: #9db0d4;
      }}
      @media (max-width: 560px) {{
        .rc-grid {{ grid-template-columns: 1fr; }}
        .rc-name {{ font-size: 18px; }}
        .rc-val  {{ font-size: 20px; }}
      }}
    </style>

    <div class="hero">
      <h1>得点圏・出塁・打点</h1>
      <div class="muted">直近 {games_found} 試合 / 生成 {generated_at}</div>
      <div class="nav-bar">
        <div class="nav-section">
          <span class="nav-label">試合数</span>
          <div class="nav-group">
            <a class="nav-btn{'active' if window_games==3 else ''}" href="/public/risp?window_games=3&view=html">直近3試合</a>
            <a class="nav-btn {'active' if window_games==5 else ''}" href="/public/risp?window_games=5&view=html">直近5試合</a>
            <a class="nav-btn {'active' if window_games==10 else ''}" href="/public/risp?window_games=10&view=html">直近10試合</a>
          </div>
        </div>
        <div class="nav-section">
          <span class="nav-label">表示</span>
          <div class="nav-group">
            <a class="nav-btn{'' if show_season else ' active'}" href="/public/risp?window_games={window_games}&view=html">直近</a>
            <a class="nav-btn{' active' if show_season else ''}" href="/public/risp?window_games={window_games}&view=season">通算</a>
          </div>
        </div>
      </div>
      {_common_nav("risp", window_games)}
    </div>

    <div id="recent-content"{' style="display:none"' if show_season else ''}>
    <div class="card">
      <div class="card-title">直近 {games_found} 試合 打撃ランキング（一軍登録中）</div>
      <p style="font-size:11px;color:#5a6e94;margin:4px 0 14px">得点圏打率 = 二塁・三塁にランナーがいる打席 ／ 出塁率 = (安打+四球+死球)÷(打数+四球+死球+犠飛) ／ 打点はタイムリー・HR・犠飛等を集計</p>
      <div class="rc-grid">
        <div class="rc-col">
          <div class="rc-col-header">
            <div class="rc-col-title">得点圏打率</div>
          </div>
          <div class="rc-col-body">
            {risp_col_html}
          </div>
        </div>
        <div class="rc-col">
          <div class="rc-col-header">
            <div class="rc-col-title">出塁率</div>
          </div>
          <div class="rc-col-body">
            {obp_col_html}
          </div>
        </div>
        <div class="rc-col">
          <div class="rc-col-header">
            <div class="rc-col-title">打点</div>
          </div>
          <div class="rc-col-body">
            {rbi_col_html}
          </div>
        </div>
      </div>
    </div>
    </div><!-- /#recent-content -->
    """
    if show_season:
        body += _render_season_risp_html(window_games)
    return _html_page("得点圏・出塁・打点", body)


@router.get("/public/risp")
def public_risp(request: Request, window_games: int = 5, view: str | None = None):
    """広島の直近N試合の得点圏打率をYahoo Baseballテキスト速報から算出"""
    try:
        window_games = max(1, min(window_games, 10))
        data = _build_risp_data(window_games)
        if _wants_html(request, view):
            show_season = (view == "season")
            return _render_risp_html(data, window_games, show_season=show_season)
        return _no_cache_json(data)
    except Exception as e:
        return JSONResponse(
            status_code=500,
            content={
                "error": "risp failed",
                "type": type(e).__name__,
                "message": str(e),
            },
        )


# ---------------------------------------------------------------------------
#  プライバシーポリシー
# ---------------------------------------------------------------------------

@router.get("/public/privacy")
def public_privacy(request: Request):
    """プライバシーポリシーページ"""
    body = """
    <style>
      .policy-wrap { max-width: 800px; margin: 0 auto; padding: 0 16px 60px; }
      .policy-wrap h1 { font-size: 22px; font-weight: 700; color: #c8d8f0; margin: 32px 0 8px; }
      .policy-wrap h2 { font-size: 16px; font-weight: 700; color: #a0b8d8; margin: 28px 0 8px; border-left: 3px solid #3a6ea5; padding-left: 10px; }
      .policy-wrap p, .policy-wrap li { font-size: 14px; color: #8899b8; line-height: 1.8; margin: 6px 0; }
      .policy-wrap ul { padding-left: 24px; }
      .policy-wrap .updated { font-size: 12px; color: #5a6e94; margin-bottom: 24px; }
      .policy-wrap a { color: #5b9bd5; text-decoration: none; }
      .policy-wrap a:hover { text-decoration: underline; }
    </style>
    <div class="policy-wrap">
      <h1>プライバシーポリシー</h1>
      <p class="updated">最終更新日：2025年6月1日</p>

      <h2>1. 基本方針</h2>
      <p>鯉男の打席分析室（以下「本サービス」）は、ユーザーの個人情報保護を重要視し、個人情報の保護に関する法律（個人情報保護法）および関連法令を遵守します。</p>

      <h2>2. 収集する情報</h2>
      <p>本サービスでは、以下の情報を収集することがあります。</p>
      <ul>
        <li>アクセスログ（IPアドレス、ブラウザ種別、参照元URL、アクセス日時等）</li>
        <li>Cookieおよびこれに類する技術を用いた利用状況データ</li>
        <li>広告配信サービスによる行動ターゲティング用データ</li>
      </ul>

      <h2>3. Googleアドセンスおよび広告について</h2>
      <p>本サービスでは、Google AdSense などの第三者広告配信サービスを利用する場合があります。これらのサービスは、ユーザーの興味に応じた広告を表示するために Cookie を使用することがあります。Googleによる Cookie の使用については、<a href="https://policies.google.com/technologies/ads" target="_blank" rel="noopener">Googleのポリシー</a>をご確認ください。</p>
      <p>ユーザーは <a href="https://adssettings.google.com/" target="_blank" rel="noopener">広告設定ページ</a> から、パーソナライズ広告を無効にすることができます。</p>

      <h2>4. アクセス解析について</h2>
      <p>本サービスでは、Google Analytics などのアクセス解析ツールを利用する場合があります。アクセス解析ツールは Cookie を使用して利用状況を収集しますが、個人を特定する情報は含まれません。収集されたデータはサービス改善のために使用されます。</p>

      <h2>5. 情報の第三者提供</h2>
      <p>本サービスは、法令に基づく場合を除き、ユーザーの個人情報を第三者に提供・開示することはありません。</p>

      <h2>6. セキュリティ</h2>
      <p>本サービスは、収集した情報の漏洩・紛失・不正アクセス等を防止するため、適切なセキュリティ対策を講じます。</p>

      <h2>7. Cookieの管理</h2>
      <p>ユーザーはブラウザの設定により Cookie を無効にすることができますが、一部の機能が正常に動作しない場合があります。</p>

      <h2>8. プライバシーポリシーの変更</h2>
      <p>本ポリシーは、法令の変更やサービス内容の変更に伴い、予告なく改定される場合があります。最新の内容は本ページにてご確認ください。</p>

      <h2>9. お問い合わせ</h2>
      <p>プライバシーポリシーに関するお問い合わせは、本サービスのお問い合わせ窓口までご連絡ください。</p>

      <p style="margin-top:40px;"><a href="/public/predicted-lineup?window_games=5&use_dh=true&view=html">← トップページへ戻る</a></p>
    </div>
    """
    return _html_page(
        "プライバシーポリシー",
        body,
        description="鯉男の打席分析室のプライバシーポリシーページです。個人情報の取り扱い、Cookieの使用、広告配信に関する方針を説明します。",
    )


# ---------------------------------------------------------------------------
#  利用規約
# ---------------------------------------------------------------------------

@router.get("/public/terms")
def public_terms(request: Request):
    """利用規約ページ"""
    body = """
    <style>
      .terms-wrap { max-width: 800px; margin: 0 auto; padding: 0 16px 60px; }
      .terms-wrap h1 { font-size: 22px; font-weight: 700; color: #c8d8f0; margin: 32px 0 8px; }
      .terms-wrap h2 { font-size: 16px; font-weight: 700; color: #a0b8d8; margin: 28px 0 8px; border-left: 3px solid #3a6ea5; padding-left: 10px; }
      .terms-wrap p, .terms-wrap li { font-size: 14px; color: #8899b8; line-height: 1.8; margin: 6px 0; }
      .terms-wrap ul { padding-left: 24px; }
      .terms-wrap .updated { font-size: 12px; color: #5a6e94; margin-bottom: 24px; }
      .terms-wrap a { color: #5b9bd5; text-decoration: none; }
      .terms-wrap a:hover { text-decoration: underline; }
    </style>
    <div class="terms-wrap">
      <h1>利用規約</h1>
      <p class="updated">最終更新日：2025年6月1日</p>

      <h2>第1条（適用）</h2>
      <p>本規約は、鯉男の打席分析室（以下「本サービス」）の利用に関する条件を定めるものです。ユーザーは本規約に同意したうえで本サービスをご利用ください。</p>

      <h2>第2条（サービスの内容）</h2>
      <p>本サービスは、広島東洋カープの打撃成績・試合データを独自に集計・分析し、ファン向けの統計情報として提供する情報サイトです。</p>

      <h2>第3条（データの利用について）</h2>
      <ul>
        <li>本サービスが提供するデータは、公開されている情報を独自に集計・加工したものです。</li>
        <li>データの正確性・完全性については保証しかねます。情報は参考目的でご利用ください。</li>
        <li>データの無断転載・商用利用はご遠慮ください。</li>
      </ul>

      <h2>第4条（知的財産権）</h2>
      <p>本サービスのコンテンツ（テキスト・デザイン・プログラム等）に関する知的財産権は、本サービス運営者または正当な権利者に帰属します。</p>

      <h2>第5条（禁止事項）</h2>
      <p>ユーザーは、以下の行為を行ってはなりません。</p>
      <ul>
        <li>本サービスへの不正アクセスおよびサーバーへの過度な負荷をかける行為</li>
        <li>本サービスのコンテンツを無断で複製・転載・再配布する行為</li>
        <li>本サービスを商業目的で利用する行為（事前の許可なし）</li>
        <li>法令または公序良俗に反する行為</li>
        <li>その他、本サービスの運営を妨げる行為</li>
      </ul>

      <h2>第6条（免責事項）</h2>
      <p>本サービスは、提供するデータの正確性・最新性・完全性を保証しません。本サービスの利用により生じたいかなる損害についても、本サービス運営者は一切の責任を負いません。</p>
      <p>また、本サービスはプロ野球公式サイトとは無関係の非公式ファンサイトです。</p>

      <h2>第7条（サービスの変更・中断・終了）</h2>
      <p>本サービスは、予告なくサービス内容の変更・一時中断・終了を行う場合があります。これによりユーザーに損害が生じても、本サービス運営者は一切の責任を負いません。</p>

      <h2>第8条（準拠法・管轄）</h2>
      <p>本規約は日本法に準拠します。本サービスに関する紛争については、運営者所在地を管轄する裁判所を専属的合意管轄裁判所とします。</p>

      <h2>第9条（規約の変更）</h2>
      <p>本規約は、必要に応じて予告なく変更される場合があります。変更後の規約はページ上での掲載をもって効力を生じるものとします。</p>

      <p style="margin-top:40px;"><a href="/public/predicted-lineup?window_games=5&use_dh=true&view=html">← トップページへ戻る</a></p>
    </div>
    """
    return _html_page(
        "利用規約",
        body,
        description="鯉男の打席分析室の利用規約ページです。サービスの利用条件、禁止事項、免責事項について説明します。",
    )


# ─────────────────────────────────────────────
# 起動時ウォームアップ
# ─────────────────────────────────────────────

def warmup_cache() -> None:
    """
    サーバー起動直後にバックグラウンドスレッドでキャッシュを温める。
    最初のユーザーリクエストが来る前にデータを用意することで初回表示を高速化する。
    """
    def _warmup():
        try:
            print("[warmup] start")

            # ① 一軍登録選手（NPB公示ページ）
            try:
                _get_active_first_team_position_players()
                print("[warmup] first_team OK")
            except Exception as e:
                print("[warmup] first_team error:", e)

            # ② シーズン守備指標（npbbasement）
            try:
                _get_player_defense()
                print("[warmup] player_defense OK")
            except Exception as e:
                print("[warmup] player_defense error:", e)

            # ③ シーズン打撃成績（proran 全選手・並列）
            try:
                _get_season_position_batting()
                print("[warmup] season_position_batting OK")
            except Exception as e:
                print("[warmup] season_position_batting error:", e)

            # ④ 直近試合（NPB試合結果ページ）
            try:
                _fetch_recent_carp_games(limit=10)
                print("[warmup] recent_games OK")
            except Exception as e:
                print("[warmup] recent_games error:", e)

            # ⑤ 直近打撃成績集計（window=5）
            try:
                _aggregate_recent_batting_stats(window_games=5)
                print("[warmup] recent_batting OK")
            except Exception as e:
                print("[warmup] recent_batting error:", e)

            # ⑥ 予想打順（最も重いメイン処理）
            try:
                _build_simple_predicted_lineup(window_games=5, use_dh=True)
                print("[warmup] predicted_lineup OK")
            except Exception as e:
                print("[warmup] predicted_lineup error:", e)

            print("[warmup] all done")
        except Exception as e:
            print("[warmup] unexpected error:", e)

    t = threading.Thread(target=_warmup, daemon=True, name="warmup-thread")
    t.start()

