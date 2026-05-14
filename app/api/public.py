# trigger new deploy

from __future__ import annotations

from functools import lru_cache
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import json
import re

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

# 守備位置コード
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

# まずは「器」だけ置く。数値はあとで埋めればOK。
# eligible_positions は「その選手に守らせてもよい位置」
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
CURRENT_SEASON_YEAR = 2026



NPBBASEMENT_FIELDING_URL = "https://npbbasement.com/fielding"
NPBBASEMENT_BASE_URL = "https://npbbasement.com"

POSITION_LABEL_TO_CODE = {
    "捕手": "C",
    "一塁手": "1B",
    "二塁手": "2B",
    "三塁手": "3B",
    "遊撃手": "SS",
    "左翼手": "LF",
    "中堅手": "CF",
    "右翼手": "RF",
    "指名打者": "DH",
}

PRORAN_PLAYER_DETAIL_MORE_URL = "https://proran.jp/player_detail_more.php?id={player_id}&y={year}"
NPBBASEMENT_FIELDING_URL = "https://npbbasement.com/fielding"



# 守備スコア（守備スコア欄に直接出る値）
PLAYER_DEFENSE_FALLBACK = {
    "坂倉 将吾": {"C": 0.30, "1B": 0.20, "3B": -0.20, "DH": 0.00},
    "小園 海斗": {"SS": 0.80, "3B": 0.40},
    "菊池 涼介": {"2B": 1.50},
}



# 今季通算の打撃（今期補正OBP / 今期補正ISO の土台）
SEASON_OVERALL_BATTING = {
    "坂倉 将吾": {"obp": 0.330, "iso": 0.130},
    "小園 海斗": {"obp": 0.310, "iso": 0.110},
    "菊池 涼介": {"obp": 0.290, "iso": 0.080},
    "モンテロ":   {"obp": 0.320, "iso": 0.180},
    "持丸 泰輝": {"obp": 0.310, "iso": 0.150},
    "石原 貴規": {"obp": 0.280, "iso": 0.070},
    "矢野 雅哉": {"obp": 0.290, "iso": 0.050},
    "二俣 翔一": {"obp": 0.300, "iso": 0.090},
    "秋山 翔吾": {"obp": 0.330, "iso": 0.100},
    "大盛 穂":   {"obp": 0.300, "iso": 0.080},
    "野間 峻祥": {"obp": 0.310, "iso": 0.070},
    "平川 蓮":   {"obp": 0.290, "iso": 0.080},
    "ファビアン":{"obp": 0.320, "iso": 0.180},
    "佐々木 泰": {"obp": 0.300, "iso": 0.110},
    "勝田 成":   {"obp": 0.290, "iso": 0.070},
    "堂林 翔太": {"obp": 0.310, "iso": 0.150},
    "末包 昇大": {"obp": 0.310, "iso": 0.180},
    "田村 俊介": {"obp": 0.320, "iso": 0.120},
    "中村 貴浩": {"obp": 0.300, "iso": 0.140},
    "名原 典彦": {"obp": 0.290, "iso": 0.090},
    "岸本 大希": {"obp": 0.290, "iso": 0.080},
    "内田 湘大": {"obp": 0.290, "iso": 0.110},
}



# 今季「その守備位置に入った時」の打撃。
# これが今回の追加要素。
# 書式:
# SEASON_POSITION_BATTING["坂倉 将吾"] = {
#     "C": {"pa": 0, "ab": 0, "obp": 0.000, "iso": 0.000},
#     "1B": {"pa": 0, "ab": 0, "obp": 0.000, "iso": 0.000},
# }

# 守備位置ごとの今季打撃（守備地位の補正OBP / ISO の素材）




# DHあり版
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

# DHなし版
# 9番は投手固定なので、ここでは 1～8番だけ最適化する
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
    marker = "守備ポジション別成績"
    start = html_text.find(marker)
    if start == -1:
        return {}

    end_candidates = []
    for stop_word in ["対球団別成績", "球場別成績", "</body>"]:
        idx = html_text.find(stop_word, start)
        if idx != -1:
            end_candidates.append(idx)
    end = min(end_candidates) if end_candidates else len(html_text)

    block = html_text[start:end]
    labels = re.findall(r'bg_c_th">([^<]+)</div>', block)

    positions = []
    for label in labels:
        label = label.strip()
        if label in POSITION_LABEL_TO_CODE:
            positions.append(label)

    if not positions:
        return {}

    def extract_row(row_name: str) -> list[str]:
        row_start = block.find(f">{row_name}<")
        if row_start == -1:
            return []
        next_row = block.find('player_detail_more_th', row_start + 1)
        row_block = block[row_start: next_row if next_row != -1 else len(block)]
        return re.findall(r'right">([^<]+)</div>', row_block)

    ab_values = extract_row("打<br>数")
    avg_values = extract_row("打<br>率")
    obp_values = extract_row("出<br>塁<br>率")
    ops_values = extract_row("Ｏ<br>Ｐ<br>Ｓ")

    result: dict[str, dict[str, float]] = {}

    for i, pos_label in enumerate(positions):
        if i >= len(ab_values) or i >= len(avg_values) or i >= len(obp_values) or i >= len(ops_values):
            continue

        ab = _to_float_or_none(ab_values[i])
        avg = _to_float_or_none(avg_values[i])
        obp = _to_float_or_none(obp_values[i])
        ops = _to_float_or_none(ops_values[i])

        if ab is None or avg is None or obp is None or ops is None:
            continue

        iso = ops - obp - avg
        if iso < 0:
            iso = 0.0

        pos_code = POSITION_LABEL_TO_CODE[pos_label]
        result[pos_code] = {
            "pa": int(ab),
            "ab": int(ab),
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


def _normalize_player_name(name: str) -> str:
    if not name:
        return ""
    text = unescape(str(name)).strip()
    text = re.sub(r"\s+", " ", text)
    return text


def _discover_proran_player_ids() -> dict[str, str]:
    html = _fetch_text(PRORAN_TEAM_BATTERS_URL)
    pairs = re.findall(
        r'href="\./player_detail\.php\?id=(\d+)(?:&y=\d+)?".*?>([^<]+)</a>',
        html,
        flags=re.S,
    )
    result = {}
    for player_id, player_name in pairs:
        result[_normalize_player_name(player_name)] = player_id
    return result


def _get_proran_player_ids() -> dict[str, str]:
    try:
        return _discover_proran_player_ids()
    except Exception:
        return {}


def _fetch_proran_position_batting(player_name: str, player_id: str) -> dict:
    url = PRORAN_PLAYER_DETAIL_MORE_URL.format(
        player_id=player_id,
        year=CURRENT_SEASON_YEAR,
    )
    html = _fetch_text(url)
    return _extract_proran_position_table(html)


def _build_season_position_batting_from_proran() -> dict:
    result = {}
    player_ids = _get_proran_player_ids()

    for player_name in PLAYER_PROFILE.keys():
        normalized_name = _normalize_player_name(player_name)
        player_id = player_ids.get(normalized_name)
        if not player_id:
            continue

        try:
            position_stats = _fetch_proran_position_batting(normalized_name, player_id)
            if position_stats:
                result[normalized_name] = position_stats
        except Exception:
            continue

    return result


def _get_season_position_batting() -> dict:
    try:
        data = _build_season_position_batting_from_proran()
        if data:
            return data
    except Exception:
        pass
    return {}




def _fetch_proran_position_batting(player_name: str, player_id: str) -> dict:
    url = PRORAN_PLAYER_DETAIL_MORE_URL.format(
        player_id=player_id,
        year=CURRENT_SEASON_YEAR,
    )
    html = _fetch_text(url)
    return _extract_proran_position_table(html)


    return data



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
    match = re.search(r'JSON\.parse\(`(.*)`\)', js, flags=re.S)
    if not match:
        return []

    raw_json = match.group(1)
    return json.loads(raw_json)




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


def _build_season_position_batting_from_proran() -> dict:
    result = {}
    player_ids = _get_proran_player_ids()

    for player_name in PLAYER_PROFILE.keys():
        normalized_name = _normalize_player_name(player_name)
        player_id = player_ids.get(normalized_name)
        if not player_id:
            continue

        try:
            position_stats = _fetch_proran_position_batting(normalized_name, player_id)
            if position_stats:
                result[normalized_name] = position_stats
        except Exception:
            continue

    return result



def _get_player_defense() -> dict:
    try:
        data = _build_player_defense_from_npbbasement()
        if data:
            return data
    except Exception:
        pass
    return PLAYER_DEFENSE_FALLBACK





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
def _normalize_player_name(name: str) -> str:
    if not name:
        return ""
    text = unescape(str(name)).strip()
    text = re.sub(r"\s+", " ", text)
    return text

def _discover_proran_player_ids() -> dict[str, str]:
    html = _fetch_text(PRORAN_TEAM_BATTERS_URL)
    pairs = re.findall(
        r'href="\./player_detail\.php\?id=(\d+)(?:&y=\d+)?".*?>([^<]+)</a>',
        html,
        flags=re.S,
    )
    result = {}
    for player_id, player_name in pairs:
        result[_normalize_player_name(player_name)] = player_id
    return result

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

    normalized_name = _normalize_player_name(player_name)
    player_stats = SEASON_POSITION_BATTING.get(normalized_name)

    if not player_stats:
        player_ids = _get_proran_player_ids()
        player_id = player_ids.get(normalized_name)

        if player_id:
            try:
                fetched = _fetch_proran_position_batting(normalized_name, player_id)
                if fetched:
                    SEASON_POSITION_BATTING[normalized_name] = fetched
                    player_stats = fetched
            except Exception:
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

            ba = _safe_float(cell(row, "打率"))
            obp = _safe_float(cell(row, "出塁率"))
            slg = _safe_float(cell(row, "長打率"))
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
    team_obp = _safe_float(team_totals.get("on_base_percentage", 0.0))
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

        raw_obp = _safe_float(player.get("on_base_percentage", 0.0))
        raw_iso = _calc_iso_from_stats(player)
        pa = int(player.get("plate_appearances", 0) or 0)
        ab = int(player.get("at_bats", 0) or 0)

        adj_obp_den = pa + RECENT_OBP_PRIOR_PA
        adj_iso_den = ab + RECENT_ISO_PRIOR_AB

        adj_obp = (
            ((pa * raw_obp) + (RECENT_OBP_PRIOR_PA * team_obp)) / adj_obp_den
            if adj_obp_den > 0 else team_obp
        )
        adj_iso = (
            ((ab * raw_iso) + (RECENT_ISO_PRIOR_AB * team_iso)) / adj_iso_den
            if adj_iso_den > 0 else team_iso
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
        name: sample_weight_map.get(name, 0.0) * (
            0.55 * obp_z.get(name, 0.0) + 0.45 * iso_z.get(name, 0.0)
        )
        for name in candidate_names
    }

    recent_bat_value = {
        name: sample_weight_map.get(name, 0.0) * (
            0.60 * obp_z.get(name, 0.0) + 0.40 * iso_z.get(name, 0.0)
        )
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

    return {
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
    defense_score = _safe_float(
        PLAYER_DEFENSE.get(player_name, {}).get(chosen_position, 0.0)
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

    return score



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

    return score

def _build_slot_reason(
    player_name: str,
    chosen_position: str,
    slot: dict,
    recent_maps: dict,
) -> str:
    adj = _get_adjusted_position_batting(player_name, chosen_position)
    adjusted_obp = _safe_float(adj.get("obp")) or 0.0
    adjusted_iso = _safe_float(adj.get("iso")) or 0.0
    defense_score = _safe_float(
        PLAYER_DEFENSE.get(player_name, {}).get(chosen_position, 0.0)
    ) or 0.0
    recent_form = recent_maps.get("recent_form_score", {}).get(player_name, 0.0)

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
            "strikeouts": extra["strikeouts"],
            "sacrifice_bunts": extra["sacrifice_bunts"],
            "sacrifice_flies": extra["sacrifice_flies"],
        })

    return rows
def build_predicted_lineup(
    dh: bool,
    window_games: int = 5,
    predicted_pitcher_name: str = "先発投手",
) -> dict:
    if window_games not in (5, 10):
        raise HTTPException(status_code=400, detail="window_games は 5 または 10 にしてください。")

    slot_defs = DH_LINEUP_SLOTS if dh else NO_DH_LINEUP_SLOTS
    candidate_names = _get_prediction_candidate_names()
    position_list = _position_universe(slot_defs)
    position_to_bit = {pos: idx for idx, pos in enumerate(position_list)}

    recent_maps = _build_recent_score_maps(window_games, candidate_names)
    season_pos_score_maps = {
        pos: _build_season_position_score_map(candidate_names, pos)
        for pos in position_list
    }

    @lru_cache(maxsize=None)
    def dp(slot_idx: int, used_player_mask: int, used_position_mask: int):
        if slot_idx >= len(slot_defs):
            return 0.0, tuple()

        slot = slot_defs[slot_idx]
        best_score = -1000000.0
        best_line = tuple()

        for player_idx, player_name in enumerate(candidate_names):
            if used_player_mask & (1 << player_idx):
                continue

            for pos in slot["allowed_positions"]:
                pos_bit = 1 << position_to_bit[pos]
                if used_position_mask & pos_bit:
                    continue

                score = _slot_score(
                    player_name=player_name,
                    slot=slot,
                    chosen_position=pos,
                    recent_maps=recent_maps,
                    season_pos_score_maps=season_pos_score_maps,
                )

                if score <= -999999:
                    continue

                tail_score, tail_line = dp(
                    slot_idx + 1,
                    used_player_mask | (1 << player_idx),
                    used_position_mask | pos_bit,
                )
                if tail_score <= -999999:
                    continue

                total_score = score + tail_score

                if total_score > best_score:
                    best_score = total_score
                    best_line = (
                        {
                            "order": slot["order"],
                            "position": pos,
                            "position_label": POSITION_LABELS.get(pos, pos),
                            "player_name": player_name,
                            "score": _round3(score),
                            "reason": _build_slot_reason(
                                player_name=player_name,
                                chosen_position=pos,
                                slot=slot,
                                recent_maps=recent_maps,
                            ),
                        },
                    ) + tail_line

        return best_score, best_line

    total_score, lineup_tuple = dp(0, 0, 0)

    if len(lineup_tuple) != len(slot_defs):
        return {
            "status": "error",
            "message": "候補選手や守備位置の設定が足りず、スタメンを組めませんでした。",
            "mode": "dh" if dh else "no_dh",
            "window_games": window_games,
            "lineup": [],
        }

    lineup = list(lineup_tuple)

    if not dh:
        lineup.append(
            {
                "order": 9,
                "position": POS_P,
                "position_label": POSITION_LABELS[POS_P],
                "player_name": predicted_pitcher_name,
                "score": 0.0,
                "reason": "DHなし版のため 9番投手固定",
            }
        )

    lineup.sort(key=lambda x: x["order"])

    return {
        "status": "ok",
        "mode": "dh" if dh else "no_dh",
        "window_games": window_games,
        "model_notes": {
            "recent_weight_source": "recent-5 / recent-10 API",
            "season_position_weight_source": "SEASON_POSITION_BATTING",
            "defense_weight_source": "PLAYER_DEFENSE",
        },
        "total_score": _round3(total_score),
        "lineup": lineup,
    }
@lru_cache(maxsize=2)
def _aggregate_recent_batting_stats(games: int) -> dict:
    
    if games not in (5, 10):
        raise HTTPException(status_code=400, detail="games は 5 または 10 にしてください。")

    recent_games = _fetch_recent_carp_games(games)

    aggregated: dict[str, dict] = {}
    used_games: list[dict] = []
    skipped_games: list[dict] = []

    for game in recent_games:
        box_url = game["box_url"]
        try:
            batting_rows = _parse_carp_batting_rows(box_url)
        except Exception as e:
            skipped_games.append({
                "date": game["date"],
                "box_url": box_url,
                "reason": str(e),
            })
            continue

        used_games.append({
            "date": game["date"],
            "opponent": game["opponent"],
            "venue": game["venue"],
            "round": game["round"],
            "score": game.get("score", ""),
            "result": game.get("result", ""),
            "box_url": box_url,
        })

        seen_names_in_this_game = set()

        for row in batting_rows:
            name = row["player_name"]

            if name not in aggregated:
                aggregated[name] = {
                    "player_name": name,
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

            if name not in seen_names_in_this_game:
                aggregated[name]["games"] += 1
                seen_names_in_this_game.add(name)

            for key in (
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
            ):
                aggregated[name][key] += row[key]

    players = list(aggregated.values())
    
    for player in players:
        pa = (
            player["at_bats"]
            + player["walks"]
            + player["hit_by_pitch"]
            + player["sacrifice_bunts"]
            + player["sacrifice_flies"]
        )
        obp_den = (
            player["at_bats"]
            + player["walks"]
            + player["hit_by_pitch"]
            + player["sacrifice_flies"]
        )

        player["plate_appearances"] = pa
        player["batting_average"] = _round3(player["hits"] / player["at_bats"]) if player["at_bats"] > 0 else 0.0
        
        player["on_base_percentage"] = _round3(
            (player["hits"] + player["walks"] + player["hit_by_pitch"]) / obp_den
        ) if obp_den > 0 else 0.0
    

    players = [
        player for player in players
        if player["at_bats"] > 0 or player["plate_appearances"] > 0
    ]

    
    players.sort(
        key=lambda x: (
            -x["hits"],
            -x["homeruns"],
            -x["rbi"],
            -x["walks"],
            -x["plate_appearances"],
            x["player_name"],
        )
    )

    team_totals = {
        "games": len(used_games),
        "at_bats": sum(p["at_bats"] for p in players),
        "runs": sum(p["runs"] for p in players),
        "hits": sum(p["hits"] for p in players),
        "rbi": sum(p["rbi"] for p in players),
        "steals": sum(p["steals"] for p in players),
        "doubles": sum(p["doubles"] for p in players),
        "triples": sum(p["triples"] for p in players),
        "homeruns": sum(p["homeruns"] for p in players),
        "walks": sum(p["walks"] for p in players),
        "hit_by_pitch": sum(p["hit_by_pitch"] for p in players),
        "strikeouts": sum(p["strikeouts"] for p in players),
        "sacrifice_bunts": sum(p["sacrifice_bunts"] for p in players),
        "sacrifice_flies": sum(p["sacrifice_flies"] for p in players),
    }

    team_totals["plate_appearances"] = (
        team_totals["at_bats"]
        + team_totals["walks"]
        + team_totals["hit_by_pitch"]
        + team_totals["sacrifice_bunts"]
        + team_totals["sacrifice_flies"]
    )

    if team_totals["at_bats"] > 0:
        team_totals["batting_average"] = _round3(team_totals["hits"] / team_totals["at_bats"])
    else:
        team_totals["batting_average"] = 0.0

    obp_den = (
        team_totals["at_bats"]
        + team_totals["walks"]
        + team_totals["hit_by_pitch"]
        + team_totals["sacrifice_flies"]
    )

    if obp_den > 0:
        team_totals["on_base_percentage"] = _round3(
            (team_totals["hits"] + team_totals["walks"] + team_totals["hit_by_pitch"]) / obp_den
        )
    else:
        team_totals["on_base_percentage"] = 0.0

    return {
        "status": "ok",
        "team": "広島東洋カープ",
        "window_games": games,
        "games_found": len(recent_games),
        "games_used": len(used_games),
        "games_skipped": len(skipped_games),

        "source": "NPB公式",
        "source_urls": [
            "https://npb.jp/bis/teams/results_c_index.html",
            "https://npb.jp/scores/",
        ],
        "recent_games": used_games,
        "skipped_games": skipped_games,
        "team_totals": team_totals,
        "players_count": len(players),
        "players": players,
    }


def _fetch_recent_actual_lineups() -> list[dict]:
    url = "https://baseball-data.com/lineup/c.html"
    html = _fetch_html(url)

    table_match = re.search(r'<table class="lineup".*?</table>', html, re.DOTALL)
    if not table_match:
        return []

    table_html = table_match.group(0)
    row_matches = re.findall(r"<tr.*?</tr>", table_html, re.DOTALL)

    games = []

    for row_html in row_matches:
        cells = re.findall(r"<td[^>]*>(.*?)</td>", row_html, re.DOTALL)
        if len(cells) != 10:
            continue

        date = _clean_text(cells[0])
        if "月" not in date:
            continue

        players = [_clean_text(cell) for cell in cells[1:10]]
        if not all(players):
            continue

        lineup = []
        for i, name in enumerate(players, start=1):
            lineup.append({
                "batting_order": i,
                "player_name": name
            })

        games.append({
            "date": date,
            "lineup": lineup
        })

    recent_games = games[-5:]
    recent_games.reverse()
    return recent_games

PLAYER_DEFENSE = _get_player_defense()
SEASON_POSITION_BATTING = _get_season_position_batting()
print("DEBUG_KOZONO_POSITION", SEASON_POSITION_BATTING.get("小園 海斗"))

@router.get("/api/lineups/recent-actual")
def recent_actual_lineups() -> JSONResponse:
    try:
        games = _fetch_recent_actual_lineups()
        return JSONResponse({
            "status": "ok",
            "source": "baseball-data.com",
            "count": len(games),
            "source_url": "https://baseball-data.com/lineup/c.html",
            "games": games
        })
    except Exception as e:
        return JSONResponse({
            "status": "error",
            "message": str(e),
            "games": []
        }, status_code=500)

@router.get("/api/stats/batting/recent-5")
def recent_5_batting_stats():
    try:
        result = _aggregate_recent_batting_stats(5)
        return result
    except Exception as e:
        return JSONResponse(
            status_code=500,
            content={
                "status": "error",
                "message": str(e),
                "recent_games": [],
                "players": [],
            },
        )


@router.get("/api/stats/batting/recent-10")
def recent_10_batting_stats():
    try:
        result = _aggregate_recent_batting_stats(10)
        return result
    except Exception as e:
        return JSONResponse(
            status_code=500,
            content={
                "status": "error",
                "message": str(e),
                "recent_games": [],
                "players": [],
            },
        )




@router.get("/api/stats/batting/recent/{games}")
def recent_batting_stats(games: int) -> JSONResponse:
    try:
        data = _aggregate_recent_batting_stats(games)
        return JSONResponse(data)
    except HTTPException:
        raise
    except Exception as e:
        return JSONResponse({
            "status": "error",
            "message": str(e),
            "players": [],
            "recent_games": [],
        }, status_code=500)

@router.get("/api/lineups/predicted/dh-yes")
def predicted_lineup_dh_yes(window: int = 5) -> JSONResponse:
    try:
        data = build_predicted_lineup(dh=True, window_games=window)
        return JSONResponse(content=data)
    except HTTPException:
        raise
    except Exception as e:
        return JSONResponse(
            status_code=500,
            content={
                "status": "error",
                "mode": "dh",
                "window_games": window,
                "message": str(e),
                "lineup": [],
            },
        )


@router.get("/api/lineups/predicted/dh-no")
def predicted_lineup_dh_no(window: int = 5) -> JSONResponse:
    try:
        data = build_predicted_lineup(dh=False, window_games=window)
        return JSONResponse(content=data)
    except HTTPException:
        raise
    except Exception as e:
        return JSONResponse(
            status_code=500,
            content={
                "status": "error",
                "mode": "no_dh",
                "window_games": window,
                "message": str(e),
                "lineup": [],
            },
        )


@router.get("/api/lineups/today")
def today_lineup() -> JSONResponse:
    try:
        data = build_predicted_lineup(dh=False, window_games=5)

        lineup = [
            {
                "batting_order": row["order"],
                "position": row["position_label"],
                "player_name": row["player_name"],
                "recent_score": row["score"],
                "reason": row["reason"],
            }
            for row in data["lineup"]
        ]

        return JSONResponse(
            content={
                "status": "ok",
                "source": "predicted lineup model",
                "count": len(lineup),
                "lineup": lineup,
            }
        )
    except HTTPException:
        raise
    except Exception as e:
        return JSONResponse(
            status_code=500,
            content={
                "status": "error",
                "message": str(e),
                "lineup": [],
            },
        )

@router.get("/", response_class=HTMLResponse)
def index() -> HTMLResponse:
    return _layout(
        "Carp Lineup Lab",
        """
        <span class="pill">β版 / 非公式</span>
        <h1>Carp Lineup Lab</h1>
        <p class="muted">上に直近5試合の実際のスタメン、下に直近5試合 / 10試合 × DHあり / なし の予想スタメンを表示します。</p>

        <div class="card">
          <h2>直近5試合の実際のスタメン</h2>
          <p id="actual-status" class="muted">読み込み中...</p>
          <div id="actual-games" class="grid"></div>
        </div>

<div class="card">
  <h2>予想スタメン（DHあり / DHなし）</h2>
  <p class="muted">直近5試合 / 10試合 と、DHあり / なし を切り替えて予想スタメンを表示します。</p>

  <div class="segmented">
    <button id="predicted-window-5" class="active" type="button" onclick="setPredictedWindow(5)">直近5試合</button>
    <button id="predicted-window-10" type="button" onclick="setPredictedWindow(10)">直近10試合</button>
  </div>

  <div class="segmented">
    <button id="predicted-mode-dh-yes" class="active" type="button" onclick="setPredictedMode('dh-yes')">DHあり</button>
    <button id="predicted-mode-dh-no" type="button" onclick="setPredictedMode('dh-no')">DHなし</button>
  </div>

  <p id="predicted-status" class="muted">読み込み中...</p>
  <div id="predicted-lineup" class="grid"></div>
</div>


        <div class="card">
          <h2>直近打撃成績</h2>
          <p class="muted">直近5試合と10試合を切り替えて、試合一覧・チーム合計・選手成績を見られます。</p>

          <div class="segmented">
            <button id="batting-btn-5" class="active" type="button" onclick="loadBattingStats(5)">直近5試合</button>
            <button id="batting-btn-10" type="button" onclick="loadBattingStats(10)">直近10試合</button>
          </div>

          <p id="batting-status" class="muted">読み込み中...</p>

          <div id="batting-games" class="grid"></div>

          <div id="batting-team" class="stats-grid"></div>

          <div class="table-wrap">
            <table class="stats-table">
              <thead>
                <tr>
                  <th>選手</th>
                  <th>試合</th>
                  <th>打数</th>
                  <th>安打</th>
                  <th>本塁打</th>
                  <th>打点</th>
                  <th>四球</th>
                  <th>打率</th>
                  <th>出塁率</th>
                </tr>
              </thead>
              <tbody id="batting-players"></tbody>
            </table>
          </div>
        </div>

        <div class="card">
          <h2>新しい打撃成績API</h2>
          <p class="muted">画面の切り替えで使っているAPIです。直接開いて内容確認もできます。</p>
          <ul>
            <li><a href="/api/stats/batting/recent-5">直近5試合の打撃成績</a></li>
            <li><a href="/api/stats/batting/recent-10">直近10試合の打撃成績</a></li>
            <li><a href="/api/stats/batting/recent/5">可変API（5）</a></li>
            <li><a href="/api/stats/batting/recent/10">可変API（10）</a></li>
          </ul>
        </div>

        <div class="card">
          <h2>主なリンク</h2>
          <ul>
            <li><a href="/api/lineups/recent-actual">直近スタメンAPI</a></li>
<li><a href="/api/lineups/predicted/dh-yes?window=5">予想スタメンAPI（直近5試合 / DHあり）</a></li>
<li><a href="/api/lineups/predicted/dh-no?window=5">予想スタメンAPI（直近5試合 / DHなし）</a></li>
<li><a href="/api/lineups/predicted/dh-yes?window=10">予想スタメンAPI（直近10試合 / DHあり）</a></li>
<li><a href="/api/lineups/predicted/dh-no?window=10">予想スタメンAPI（直近10試合 / DHなし）</a></li>

            <li><a href="/docs">APIドキュメント</a></li>
            <li><a href="/data-policy">データ表示ポリシー</a></li>
            <li><a href="/disclaimer">免責</a></li>
            <li><a href="/sources">出典</a></li>
          </ul>
        </div>

        <script>

          async function loadActualLineups() {
            const statusEl = document.getElementById("actual-status");
            const gamesEl = document.getElementById("actual-games");

            try {
              const res = await fetch("/api/lineups/recent-actual");
              const data = await res.json();

              if (!data.games || !Array.isArray(data.games) || data.games.length === 0) {
                statusEl.textContent = "直近スタメンを取得できませんでした。";
                return;
              }

              statusEl.innerHTML =
                '取得元: <a href="' + data.source_url + '" target="_blank" rel="noopener noreferrer">' +
                data.source +
                '</a> / 表示試合数: <strong>' + data.count + '</strong>';

              gamesEl.innerHTML = data.games.map(game => `
                <div class="game-card">
                  <div class="date">${game.date}</div>
                  <ol>
                    ${game.lineup.map(player => `<li>${player.player_name}</li>`).join("")}
                  </ol>
                </div>
              `).join("");
            } catch (e) {
              statusEl.textContent = "直近スタメンを表示できませんでした。";
            }
          }

          function formatDecimal(value) {
            if (value === null || value === undefined) return "-";
            const num = Number(value);
            if (Number.isNaN(num)) return "-";
            return num.toFixed(3).replace(/^0(?=\.)/, "");
          }

          function setBattingTab(activeGames) {
            document.getElementById("batting-btn-5")?.classList.toggle("active", activeGames === 5);
            document.getElementById("batting-btn-10")?.classList.toggle("active", activeGames === 10);
          }

          function renderBattingGames(games) {
            const gamesEl = document.getElementById("batting-games");

            if (!games || games.length === 0) {
              gamesEl.innerHTML = '<p class="muted">試合データがありません。</p>';
              return;
            }

            gamesEl.innerHTML = games.map(game => `
              <div class="game-card">
                <div class="date">${game.date}</div>
                <div>${game.opponent} / ${game.venue} / ${game.round}回戦</div>
                <div style="margin-top: 6px; font-weight: 700;">${game.score} ${game.result}</div>
                <div class="small" style="margin-top: 6px;">
                  <a href="${game.box_url}" target="_blank" rel="noopener noreferrer">ボックススコア</a>
                </div>
              </div>
            `).join("");
          }

          function renderBattingTeamTotals(team) {
            const teamEl = document.getElementById("batting-team");

            teamEl.innerHTML = `
              <div class="stat-box"><div class="stat-label">試合</div><div class="stat-value">${team.games ?? "-"}</div></div>
              <div class="stat-box"><div class="stat-label">打数</div><div class="stat-value">${team.at_bats ?? "-"}</div></div>
              <div class="stat-box"><div class="stat-label">安打</div><div class="stat-value">${team.hits ?? "-"}</div></div>
              <div class="stat-box"><div class="stat-label">本塁打</div><div class="stat-value">${team.homeruns ?? "-"}</div></div>
              <div class="stat-box"><div class="stat-label">打率</div><div class="stat-value">${formatDecimal(team.batting_average)}</div></div>
              <div class="stat-box"><div class="stat-label">出塁率</div><div class="stat-value">${formatDecimal(team.on_base_percentage)}</div></div>
            `;
          }

          function renderBattingPlayers(players) {
            const playersEl = document.getElementById("batting-players");

            if (!players || players.length === 0) {
              playersEl.innerHTML = '<tr><td colspan="9" class="muted">選手データがありません。</td></tr>';
              return;
            }

            playersEl.innerHTML = players.map(player => `
              <tr>
                <td>${player.player_name}</td>
                <td>${player.games}</td>
                <td>${player.at_bats}</td>
                <td>${player.hits}</td>
                <td>${player.homeruns}</td>
                <td>${player.rbi}</td>
                <td>${player.walks}</td>
                <td>${formatDecimal(player.batting_average)}</td>
                <td>${formatDecimal(player.on_base_percentage)}</td>
              </tr>
            `).join("");
          }

          async function loadBattingStats(windowGames) {
            const statusEl = document.getElementById("batting-status");
            const gamesEl = document.getElementById("batting-games");
            const teamEl = document.getElementById("batting-team");
            const playersEl = document.getElementById("batting-players");

            setBattingTab(windowGames);
            statusEl.textContent = "打撃成績を読み込み中...";
            gamesEl.innerHTML = "";
            teamEl.innerHTML = "";
            playersEl.innerHTML = "";

            try {
              const res = await fetch(`/api/stats/batting/recent-${windowGames}`);
              const data = await res.json();

              if (!data || data.status !== "ok") {
                throw new Error(data?.message || "API取得に失敗しました。");
              }

              const resultsUrl = data.source_urls?.[0] || "https://npb.jp/bis/teams/results_c_index.html";
              const scoresUrl = data.source_urls?.[1] || "https://npb.jp/scores/";

              statusEl.innerHTML =
                `取得元: <a href="${resultsUrl}" target="_blank" rel="noopener noreferrer">試合結果</a> / ` +
                `<a href="${scoresUrl}" target="_blank" rel="noopener noreferrer">スコア速報</a> / ` +
                `表示試合数: <strong>${data.games_used}</strong> / ` +
                `選手数: <strong>${data.players_count}</strong>`;

              renderBattingGames(data.recent_games || []);
              renderBattingTeamTotals(data.team_totals || {});
              renderBattingPlayers(data.players || []);
            } catch (e) {
              statusEl.textContent = "打撃成績を表示できませんでした。";
              gamesEl.innerHTML = "";
              teamEl.innerHTML = "";
              playersEl.innerHTML = '<tr><td colspan="9" class="muted">読み込み失敗</td></tr>';
            }
          }
let predictedWindow = 5;
let predictedMode = "dh-yes";

function syncPredictedButtons() {
  document.getElementById("predicted-window-5")?.classList.toggle("active", predictedWindow === 5);
  document.getElementById("predicted-window-10")?.classList.toggle("active", predictedWindow === 10);

  document.getElementById("predicted-mode-dh-yes")?.classList.toggle("active", predictedMode === "dh-yes");
  document.getElementById("predicted-mode-dh-no")?.classList.toggle("active", predictedMode === "dh-no");
}

function setPredictedWindow(windowGames) {
  predictedWindow = windowGames;
  syncPredictedButtons();
  loadPredictedLineup();
}

function setPredictedMode(mode) {
  predictedMode = mode;
  syncPredictedButtons();
  loadPredictedLineup();
}

function renderPredictedLineup(lineup) {
  const lineupEl = document.getElementById("predicted-lineup");

  if (!lineup || !Array.isArray(lineup) || lineup.length === 0) {
    lineupEl.innerHTML = '<p class="muted">予想スタメンを表示できませんでした。</p>';
    return;
  }

  lineupEl.innerHTML = lineup.map(player => `
    <div class="game-card">
      <div style="display:flex; justify-content:space-between; align-items:flex-start; gap:12px; flex-wrap:wrap;">
        <div>
          <div class="small">${player.order}番 / ${player.position_label}</div>
          <div class="date" style="margin-bottom:4px;">${player.player_name}</div>
        </div>
        <div style="text-align:right;">
          <div class="small">model score</div>
          <div style="font-size:20px; font-weight:700;">${player.score ?? "-"}</div>
        </div>
      </div>
      <div class="muted">${player.reason || ""}</div>
    </div>
  `).join("");
}

async function loadPredictedLineup() {
  const statusEl = document.getElementById("predicted-status");
  const lineupEl = document.getElementById("predicted-lineup");

  syncPredictedButtons();
  statusEl.textContent = "予想スタメンを読み込み中...";
  lineupEl.innerHTML = "";

  try {
    const res = await fetch(`/api/lineups/predicted/${predictedMode}?window=${predictedWindow}`);
    const data = await res.json();

    if (!data || data.status !== "ok") {
      throw new Error(data?.message || "予想スタメンAPIの取得に失敗しました。");
    }

    const modeLabel = predictedMode === "dh-yes" ? "DHあり" : "DHなし";

    statusEl.innerHTML =
      `モード: <strong>${modeLabel}</strong> / ` +
      `対象: <strong>直近${predictedWindow}試合</strong> / ` +
      `合計スコア: <strong>${data.total_score ?? "-"}</strong>`;

    renderPredictedLineup(data.lineup || []);
  } catch (e) {
    statusEl.textContent = "予想スタメンを表示できませんでした。";
    lineupEl.innerHTML = '<p class="muted">読み込み失敗</p>';
  }
}

document.addEventListener("DOMContentLoaded", () => {
loadActualLineups();
loadPredictedLineup();
loadBattingStats(5);
});

        </script>
        """,
    )


@router.get("/data-policy", response_class=HTMLResponse)
def data_policy() -> HTMLResponse:
    return _layout(
        "データ表示ポリシー",
        """
        <span class="pill">数値と自作UIのみ</span>
        <h1>データ表示ポリシー</h1>
        <div class="card">
          <ul>
            <li>本サイトは非公式の分析サイトです。</li>
            <li>公式画像、ロゴ、動画、選手写真、スクリーンショットは使用しません。</li>
            <li>公開情報をもとに、独自集計した数値・指標・説明文を表示します。</li>
            <li>出典元へのリンクを表示します。</li>
            <li>権利者から修正または削除要請を受けた場合は、速やかに対応します。</li>
          </ul>
        </div>
        """,
    )


@router.get("/disclaimer", response_class=HTMLResponse)
def disclaimer() -> HTMLResponse:
    return _layout(
        "免責",
        """
        <span class="pill">予想は予想</span>
        <h1>免責</h1>
        <div class="card">
          <ul>
            <li>本サイトの予想スタメンは独自モデルによる推定であり、実際の起用を保証するものではありません。</li>
            <li>データ更新の遅れ、取得失敗、計算誤差が発生する場合があります。</li>
            <li>本サイトの内容によって生じた損害について、運営者は責任を負いません。</li>
          </ul>
        </div>
        """,
    )


@router.get("/sources", response_class=HTMLResponse)
def sources() -> HTMLResponse:
    return _layout(
        "出典",
        """
        <span class="pill">公開情報ベース</span>
        <h1>主な出典</h1>
        <div class="card">
          <ul>
            <li><a href="https://baseball-data.com/lineup/c.html">広島東洋カープ スタメン一覧（打順）</a></li>
            <li><a href="https://npb.jp/bis/teams/results_c_index.html">NPB公式 試合結果</a></li>
            <li><a href="https://npb.jp/scores/">NPB公式 スコア速報</a></li>
            <li><a href="https://npb.jp/bis/2026/stats/idb1_c.html">NPB公式 1軍打撃成績</a></li>
            <li><a href="https://npb.jp/bis/teams/rst_c.html">NPB公式 選手登録一覧</a></li>
          </ul>
        </div>
        """,
    )
