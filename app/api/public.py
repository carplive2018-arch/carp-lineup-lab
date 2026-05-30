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

HOME_VENUE_KEYWORDS = ["マツダ"]  # 広島デフォルト（広島固定各所で使用）

# 球団別ホーム球場キーワード（試合結果ページの会場名からホーム/アウェーを判定）
TEAM_HOME_VENUE_KEYWORDS: dict[str, list[str]] = {
    "広島":       ["マツダ"],
    "阪神":       ["甲子園"],
    "巨人":       ["東京ドーム", "ドーム"],
    "DeNA":       ["横浜", "ハマスタ"],
    "中日":       ["ナゴヤ", "バンテリン"],
    "ヤクルト":   ["明治神宮", "ジャパン"],
    "ソフトバンク": ["ペイペイ", "福岡"],
    "西武":       ["ベルーナ", "所沢"],
    "楽天":       ["楽天", "みずほ"],
    "ロッテ":     ["ZOZOマリン", "千葉"],
    "オリックス": ["京セラ", "大阪京セラ"],
    "日本ハム":   ["エスコン", "札幌"],
}

POSITION_BATTING_PRIOR_PA = 60
POSITION_BATTING_PRIOR_AB = 80
# 直近成績のベイズ収縮: PA/AB が少ないほどシーズン実績値に引き寄せる
# OBP は四球・死球・犠飛を含む per-PA 指標 → PA ベース
# ISO は長打のみ per-AB 指標 → AB ベース
# PRIOR_PA/AB が大きいほど「信頼できるとみなすのに必要な打席数が多い」→ 補正が強い
RECENT_OBP_PRIOR_PA  = 18   # 旧12 → 18: 5試合程度（~20PA）でも半分以上引き戻す
RECENT_ISO_PRIOR_AB  = 25   # 旧20 → 25: ISO は長打率なので標本分散が大きい → より強い収縮
RECENT_WOBA_PRIOR_PA = 18   # wOBA の収縮強度（OBP と同等）
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

# Yahoo Baseball チームID マッピング（team_name → Yahoo team_id）
YAHOO_TEAM_ID: dict[str, int] = {
    "広島":       6,
    "阪神":       5,
    "巨人":       1,
    "DeNA":       3,
    "中日":       4,
    "ヤクルト":   2,
    "ソフトバンク": 12,
    "西武":       7,
    "楽天":       376,
    "ロッテ":     9,
    "オリックス": 11,
    "日本ハム":   8,
}

# NPB.jp 試合結果URLのチームコード（team_name → npb code）
NPB_RESULTS_TEAM_CODE: dict[str, str] = {
    "広島":       "c",
    "阪神":       "t",
    "巨人":       "g",
    "DeNA":       "db",
    "中日":       "d",
    "ヤクルト":   "s",
    "ソフトバンク": "h",
    "西武":       "l",
    "楽天":       "e",
    "ロッテ":     "m",
    "オリックス": "b",
    "日本ハム":   "f",
}

# NPB.jp 二軍打撃成績URLのチームコード（広島と同じ形式）
NPB_FARM_STATS_CODE: dict[str, str] = NPB_RESULTS_TEAM_CODE  # 同じコード体系

# NPB.jp 出場選手登録ページ内のチーム正式名ブロック（team_code → 正式名）
NPB_ROSTER_TEAM_FULLNAME: dict[str, str] = {
    "広島":       "広島東洋カープ",
    "阪神":       "阪神タイガース",
    "巨人":       "読売ジャイアンツ",
    "DeNA":       "横浜DeNAベイスターズ",
    "中日":       "中日ドラゴンズ",
    "ヤクルト":   "東京ヤクルトスワローズ",
    "ソフトバンク": "福岡ソフトバンクホークス",
    "西武":       "埼玉西武ライオンズ",
    "楽天":       "東北楽天ゴールデンイーグルス",
    "ロッテ":     "千葉ロッテマリーンズ",
    "オリックス": "オリックス・バファローズ",
    "日本ハム":   "北海道日本ハムファイターズ",
}

# proran.jp チーム別打者一覧URLのチームコード（team_code → proran t= パラメータ）
PRORAN_TEAM_CODE: dict[str, str] = {
    "広島":       "_c",
    "阪神":       "_t",
    "巨人":       "_g",
    "DeNA":       "_de",
    "中日":       "_d",
    "ヤクルト":   "_s",
    "ソフトバンク": "_h",
    "西武":       "_l",
    "楽天":       "_e",
    "ロッテ":     "_m",
    "オリックス": "_b",
    "日本ハム":   "_f",
}

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
    "player_profile": {},  # team_code -> {"value": ..., "expires_at": ...}
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
    # ── 広島東洋カープ（2026シーズン実績） ──
    "坂倉 将吾": {"obp": 0.390, "iso": 0.213},
    "小園 海斗": {"obp": 0.287, "iso": 0.048},
    "菊池 涼介": {"obp": 0.361, "iso": 0.053},
    "モンテロ": {"obp": 0.295, "iso": 0.190},
    "持丸 泰輝": {"obp": 0.325, "iso": 0.123},
    "石原 貴規": {"obp": 0.286, "iso": 0.250},
    "矢野 雅哉": {"obp": 0.235, "iso": 0.200},
    "二俣 翔一": {"obp": 0.282, "iso": 0.139},
    "秋山 翔吾": {"obp": 0.273, "iso": 0.135},
    "大盛 穂": {"obp": 0.233, "iso": 0.077},
    "野間 峻祥": {"obp": 0.281, "iso": 0.017},
    "平川 蓮": {"obp": 0.238, "iso": 0.040},
    "ファビアン": {"obp": 0.194, "iso": 0.108},
    "佐々木 泰": {"obp": 0.234, "iso": 0.085},
    "勝田 成": {"obp": 0.233, "iso": 0.030},
    "二俣 翔一": {"obp": 0.282, "iso": 0.139},
    "名原 典彦": {"obp": 0.444, "iso": 0.223},
    # ── 阪神タイガース（2026シーズン実績） ──
    "佐藤 輝明": {"obp": 0.450, "iso": 0.360},
    "大山 悠輔": {"obp": 0.382, "iso": 0.176},
    "森下 翔太": {"obp": 0.360, "iso": 0.255},
    "中野 拓夢": {"obp": 0.320, "iso": 0.044},
    "近本 光司": {"obp": 0.336, "iso": 0.042},
    "木浪 聖也": {"obp": 0.337, "iso": 0.045},
    "小幡 竜平": {"obp": 0.303, "iso": 0.036},
    "坂本 誠志郎": {"obp": 0.289, "iso": 0.050},
    "前川 右京": {"obp": 0.277, "iso": 0.137},
    "髙寺 望夢": {"obp": 0.376, "iso": 0.108},
    "福島 圭音": {"obp": 0.333, "iso": 0.090},
    "梅野 隆太郎": {"obp": 0.333, "iso": 0.000},
    "伏見 寅威": {"obp": 0.222, "iso": 0.017},
    "立石 正広": {"obp": 0.412, "iso": 0.059},
    "熊谷 敬宥": {"obp": 0.316, "iso": 0.055},
    "嶋村 麟士朗": {"obp": 0.308, "iso": 0.230},
    "岡城 快生": {"obp": 0.240, "iso": 0.084},
    # ── 読売ジャイアンツ（2026シーズン実績） ──
    "キャベッジ": {"obp": 0.282, "iso": 0.182},
    "ダルベック": {"obp": 0.339, "iso": 0.217},
    "吉川 尚輝": {"obp": 0.254, "iso": 0.000},
    "大城 卓三": {"obp": 0.432, "iso": 0.228},
    "増田 陸": {"obp": 0.268, "iso": 0.101},
    "浦田 俊輔": {"obp": 0.307, "iso": 0.052},
    "泉口 友汰": {"obp": 0.277, "iso": 0.111},
    "坂本 勇人": {"obp": 0.232, "iso": 0.118},
    "佐々木 俊輔": {"obp": 0.283, "iso": 0.175},
    "岸田 行倫": {"obp": 0.306, "iso": 0.051},
    "丸 佳浩": {"obp": 0.250, "iso": 0.167},
    "平山 功太": {"obp": 0.325, "iso": 0.139},
    "松本 剛": {"obp": 0.253, "iso": 0.025},
    "中山 礼都": {"obp": 0.206, "iso": 0.034},
    "若林 楽人": {"obp": 0.222, "iso": 0.039},
    "山瀬 慎之助": {"obp": 0.231, "iso": 0.307},
    "小濱 佑斗": {"obp": 0.304, "iso": 0.048},
    # ── 横浜DeNAベイスターズ（2026シーズン実績） ──
    "度会 隆輝": {"obp": 0.352, "iso": 0.107},
    "佐野 恵太": {"obp": 0.335, "iso": 0.119},
    "宮崎 敏郎": {"obp": 0.358, "iso": 0.132},
    "牧 秀悟": {"obp": 0.424, "iso": 0.141},
    "筒香 嘉智": {"obp": 0.359, "iso": 0.185},
    "山本 祐大": {"obp": 0.346, "iso": 0.091},
    "蝦名 達夫": {"obp": 0.308, "iso": 0.059},
    "三森 大貴": {"obp": 0.302, "iso": 0.013},
    "林 琢真": {"obp": 0.286, "iso": 0.130},
    "松尾 汐恩": {"obp": 0.286, "iso": 0.061},
    "京田 陽太": {"obp": 0.299, "iso": 0.041},
    "勝又 温史": {"obp": 0.387, "iso": 0.055},
    "ヒュンメル": {"obp": 0.304, "iso": 0.124},
    "宮下 朝陽": {"obp": 0.150, "iso": 0.103},
    "成瀬 脩人": {"obp": 0.250, "iso": 0.023},
    "戸柱 恭孝": {"obp": 0.375, "iso": 0.071},
    # ── 東京ヤクルトスワローズ（2026シーズン実績） ──
    "サンタナ": {"obp": 0.370, "iso": 0.257},
    "岩田 幸宏": {"obp": 0.301, "iso": 0.058},
    "武岡 龍世": {"obp": 0.331, "iso": 0.128},
    "長岡 秀樹": {"obp": 0.319, "iso": 0.070},
    "古賀 優大": {"obp": 0.306, "iso": 0.057},
    "内山 壮真": {"obp": 0.375, "iso": 0.132},
    "丸山 和郁": {"obp": 0.366, "iso": 0.161},
    "増田 珠": {"obp": 0.416, "iso": 0.169},
    "オスナ": {"obp": 0.271, "iso": 0.076},
    "赤羽 由紘": {"obp": 0.306, "iso": 0.153},
    "鈴木 叶": {"obp": 0.231, "iso": 0.143},
    "並木 秀尊": {"obp": 0.289, "iso": 0.068},
    "伊藤 琉偉": {"obp": 0.253, "iso": 0.112},
    "田中 陽翔": {"obp": 0.227, "iso": 0.024},
    "茂木 栄五郎": {"obp": 0.300, "iso": 0.000},
    # ── 中日ドラゴンズ（2026シーズン実績） ──
    "村松 開人": {"obp": 0.399, "iso": 0.179},
    "細川 成也": {"obp": 0.400, "iso": 0.147},
    "板山 祐太郎": {"obp": 0.364, "iso": 0.300},
    "石伊 雄太": {"obp": 0.297, "iso": 0.169},
    "田中 幹也": {"obp": 0.273, "iso": 0.051},
    "福永 裕基": {"obp": 0.354, "iso": 0.050},
    "鵜飼 航丞": {"obp": 0.333, "iso": 0.165},
    "高橋 周平": {"obp": 0.333, "iso": 0.018},
    "阿部 寿樹": {"obp": 0.386, "iso": 0.158},
    "木下 拓哉": {"obp": 0.265, "iso": 0.042},
    "花田 旭": {"obp": 0.275, "iso": 0.132},
    "石川 昂弥": {"obp": 0.265, "iso": 0.122},
    "カリステ": {"obp": 0.258, "iso": 0.082},
    "土田 龍空": {"obp": 0.250, "iso": 0.207},
    "大島 洋平": {"obp": 0.242, "iso": 0.048},
    "ボスラー": {"obp": 0.290, "iso": 0.113},
    "サノー": {"obp": 0.280, "iso": 0.218},
    "加藤 匠馬": {"obp": 0.389, "iso": 0.066},
    "山本 泰寛": {"obp": 0.276, "iso": 0.116},
    # ── 埼玉西武ライオンズ（2026シーズン実績） ──
    "渡部 聖弥": {"obp": 0.295, "iso": 0.095},
    "平沢 大河": {"obp": 0.376, "iso": 0.131},
    "長谷川 信哉": {"obp": 0.320, "iso": 0.241},
    "滝澤 夏央": {"obp": 0.377, "iso": 0.048},
    "源田 壮亮": {"obp": 0.285, "iso": 0.055},
    "古賀 悠斗": {"obp": 0.354, "iso": 0.085},
    "桑原 将志": {"obp": 0.383, "iso": 0.176},
    "カナリオ": {"obp": 0.308, "iso": 0.109},
    "小島 大河": {"obp": 0.276, "iso": 0.093},
    "山村 崇嘉": {"obp": 0.279, "iso": 0.077},
    "林 安可": {"obp": 0.264, "iso": 0.134},
    "西川 愛也": {"obp": 0.226, "iso": 0.102},
    "石井 一成": {"obp": 0.271, "iso": 0.121},
    "ネビン": {"obp": 0.471, "iso": 0.450},
    "外崎 修汰": {"obp": 0.286, "iso": 0.133},
    "柘植 世那": {"obp": 0.389, "iso": 0.000},
    # ── 東北楽天ゴールデンイーグルス（2026シーズン実績） ──
    "村林 一輝": {"obp": 0.345, "iso": 0.117},
    "辰己 涼介": {"obp": 0.402, "iso": 0.120},
    "黒川 史陽": {"obp": 0.346, "iso": 0.058},
    "太田 光": {"obp": 0.336, "iso": 0.049},
    "浅村 栄斗": {"obp": 0.342, "iso": 0.148},
    "小深田 大翔": {"obp": 0.265, "iso": 0.043},
    "中島 大輔": {"obp": 0.259, "iso": 0.129},
    "小郷 裕哉": {"obp": 0.271, "iso": 0.040},
    "平良 竜哉": {"obp": 0.328, "iso": 0.258},
    "佐藤 直樹": {"obp": 0.304, "iso": 0.265},
    "渡邊 佳明": {"obp": 0.351, "iso": 0.056},
    "伊藤 裕季也": {"obp": 0.245, "iso": 0.250},
    "YG安田": {"obp": 0.220, "iso": 0.158},
    "伊藤 光": {"obp": 0.241, "iso": 0.000},
    # ── 福岡ソフトバンクホークス（2026シーズン実績） ──
    "近藤 健介": {"obp": 0.409, "iso": 0.258},
    "栗原 陵矢": {"obp": 0.335, "iso": 0.270},
    "柳田 悠岐": {"obp": 0.303, "iso": 0.132},
    "周東 佑京": {"obp": 0.354, "iso": 0.078},
    "牧原 大成": {"obp": 0.318, "iso": 0.062},
    "山川 穂高": {"obp": 0.298, "iso": 0.214},
    "今宮 健太": {"obp": 0.271, "iso": 0.050},
    "海野 隆司": {"obp": 0.239, "iso": 0.087},
    "柳町 達": {"obp": 0.299, "iso": 0.069},
    "庄子 雄大": {"obp": 0.413, "iso": 0.079},
    "川瀬 晃": {"obp": 0.200, "iso": 0.000},
    "野村 勇": {"obp": 0.217, "iso": 0.018},
    "谷川原 健太": {"obp": 0.261, "iso": 0.043},
    "中村 晃": {"obp": 0.214, "iso": 0.038},
    "正木 智也": {"obp": 0.433, "iso": 0.178},
    "笹川 吉康": {"obp": 0.200, "iso": 0.125},
    # ── 北海道日本ハムファイターズ（2026シーズン実績） ──
    "清宮 幸太郎": {"obp": 0.351, "iso": 0.176},
    "郡司 裕也": {"obp": 0.326, "iso": 0.071},
    "万波 中正": {"obp": 0.324, "iso": 0.242},
    "水野 達稀": {"obp": 0.345, "iso": 0.114},
    "レイエス": {"obp": 0.362, "iso": 0.179},
    "田宮 裕涼": {"obp": 0.307, "iso": 0.131},
    "奈良間 大己": {"obp": 0.319, "iso": 0.120},
    "野村 佑希": {"obp": 0.285, "iso": 0.188},
    "矢澤 宏太": {"obp": 0.224, "iso": 0.231},
    "西川 遥輝": {"obp": 0.364, "iso": 0.140},
    "淺間 大基": {"obp": 0.250, "iso": 0.189},
    "大塚 瑠晏": {"obp": 0.351, "iso": 0.133},
    "水谷 瞬": {"obp": 0.250, "iso": 0.125},
    "五十幡 亮汰": {"obp": 0.219, "iso": 0.000},
    "カストロ": {"obp": 0.310, "iso": 0.186},
    "細川 凌平": {"obp": 0.474, "iso": 0.461},
    # ── 千葉ロッテマリーンズ（2026シーズン実績） ──
    "西川 史礁": {"obp": 0.349, "iso": 0.111},
    "藤原 恭大": {"obp": 0.405, "iso": 0.094},
    "佐藤 都志也": {"obp": 0.372, "iso": 0.234},
    "友杉 篤輝": {"obp": 0.350, "iso": 0.062},
    "小川 龍成": {"obp": 0.346, "iso": 0.024},
    "寺地 隆成": {"obp": 0.261, "iso": 0.070},
    "ソト": {"obp": 0.282, "iso": 0.138},
    "髙部 瑛斗": {"obp": 0.265, "iso": 0.022},
    "ポランコ": {"obp": 0.288, "iso": 0.189},
    "山口 航輝": {"obp": 0.239, "iso": 0.279},
    "松川 虎生": {"obp": 0.224, "iso": 0.041},
    "上田 希由翔": {"obp": 0.234, "iso": 0.182},
    "池田 来翔": {"obp": 0.297, "iso": 0.027},
    "山本 大斗": {"obp": 0.276, "iso": 0.102},
    # ── オリックス・バファローズ（2026シーズン実績） ──
    "太田 椋": {"obp": 0.372, "iso": 0.156},
    "西川 龍馬": {"obp": 0.301, "iso": 0.083},
    "森 友哉": {"obp": 0.325, "iso": 0.152},
    "宗 佑磨": {"obp": 0.354, "iso": 0.160},
    "中川 圭太": {"obp": 0.322, "iso": 0.107},
    "紅林 弘太郎": {"obp": 0.273, "iso": 0.112},
    "渡部 遼人": {"obp": 0.368, "iso": 0.130},
    "若月 健矢": {"obp": 0.226, "iso": 0.034},
    "シーモア": {"obp": 0.198, "iso": 0.103},
    "廣岡 大志": {"obp": 0.235, "iso": 0.000},
    "来田 涼斗": {"obp": 0.265, "iso": 0.063},
    "大城 滉二": {"obp": 0.313, "iso": 0.062},
    "西野 真弘": {"obp": 0.212, "iso": 0.020},
    "麦谷 祐介": {"obp": 0.148, "iso": 0.200},
    "野口 智哉": {"obp": 0.241, "iso": 0.100},
}

# ── DH有（9人制）打順スロット定義 ──────────────────────────────────────────
# Excel設計書「Book1.xlsx」準拠。OBP/wOBA/RUN/CON/ISO/DEF/Avail の7指標体系。
# leadoff フラグ廃止 → 全打順を統一ウェイト計算に変更。
# min_adj_iso ハードカット廃止 → ソフト減点方式に変更（_slot_score 内で処理）。
# soft_penalty: チーム内パーセンタイル比較による得点調整（_slot_score で適用）。
DH_LINEUP_SLOTS = [
    {
        # 1番：出塁起点（OBP最重視＋走力・コンタクト）
        "order": 1,
        "role": "leadoff_obp",
        "weights": {
            "recent_obp":  0.35,   # OBP 0.35
            "recent_woba": 0.20,   # wOBA 0.20
            "recent_run":  0.15,   # RUN 0.15
            "recent_con":  0.10,   # CON 0.10
            "defense":     0.10,   # DEF 0.10
            "avail":       0.10,   # Avail 0.10
        },
        # OBP 下位30%でソフト減点（×0.85）、recent_pa=0 かつ season_pa 極少なら候補外
        "soft_penalty": {"obp_bottom_pct": 0.30, "penalty": 0.85},
        "exclude_zero_pa": True,
    },
    {
        # 2番：強打の接着剤（wOBA最重視＋OBP・CON）
        "order": 2,
        "role": "strong_connector",
        "weights": {
            "recent_woba": 0.35,   # wOBA 0.35
            "recent_obp":  0.20,   # OBP 0.20
            "recent_con":  0.15,   # CON 0.15
            "recent_iso":  0.10,   # ISO 0.10
            "defense":     0.10,   # DEF 0.10
            "avail":       0.10,   # Avail 0.10
        },
        # CON 下位25%で軽く減点（×0.93）
        "soft_penalty": {"con_bottom_pct": 0.25, "penalty": 0.93},
    },
    {
        # 3番：万能上位（wOBA最重視・全指標バランス）
        "order": 3,
        "role": "versatile_upper",
        "weights": {
            "recent_woba": 0.35,   # wOBA 0.35
            "recent_obp":  0.15,   # OBP 0.15
            "recent_iso":  0.15,   # ISO 0.15
            "recent_con":  0.10,   # CON 0.10
            "defense":     0.10,   # DEF 0.10
            "avail":       0.15,   # Avail 0.15
        },
        # wOBA チーム中央値未満でソフト減点（×0.88）
        "soft_penalty": {"woba_below_median": True, "penalty": 0.88},
    },
    {
        # 4番：主砲（wOBA＋ISO最重視）
        "order": 4,
        "role": "cleanup_power",
        "weights": {
            "recent_woba": 0.45,   # wOBA 0.45
            "recent_iso":  0.25,   # ISO 0.25
            "recent_obp":  0.10,   # OBP 0.10
            "recent_con":  0.05,   # CON 0.05
            "defense":     0.05,   # DEF 0.05
            "avail":       0.10,   # Avail 0.10
        },
        # ISO<0.110 ソフト減点（×0.90）、wOBA 下位50%候補外
        # 生ISO=0.000かつPA>=5 の選手は4番候補から完全除外
        "soft_penalty": {"iso_threshold": 0.110, "penalty": 0.90},
        "hard_cut_woba_bottom_pct": 0.50,
        "hard_cut_iso_zero": True,
    },
    {
        # 5番：返す2枚目（wOBA＋ISO 長打力継続）
        "order": 5,
        "role": "second_slugger",
        "weights": {
            "recent_woba": 0.35,   # wOBA 0.35
            "recent_iso":  0.25,   # ISO 0.25
            "recent_obp":  0.10,   # OBP 0.10
            "recent_con":  0.10,   # CON 0.10
            "defense":     0.05,   # DEF 0.05
            "avail":       0.15,   # Avail 0.15
        },
        # ISO<0.095 ソフト減点（×0.90）
        "soft_penalty": {"iso_threshold": 0.095, "penalty": 0.90},
    },
    {
        # 6番：中軸下の橋（wOBA＋OBP・守備バランス）
        "order": 6,
        "role": "bridge_lower",
        "weights": {
            "recent_woba": 0.25,   # wOBA 0.25
            "recent_obp":  0.20,   # OBP 0.20
            "recent_iso":  0.15,   # ISO 0.15
            "defense":     0.15,   # DEF 0.15
            "recent_con":  0.10,   # CON 0.10
            "avail":       0.15,   # Avail 0.15
        },
        # recent_pa=0 なら中程度の減点（×0.80）
        "soft_penalty": {"zero_pa_penalty": 0.80},
    },
    {
        # 7番：守備込み下位中核（守備＋wOBA）
        "order": 7,
        "role": "glove_core",
        "weights": {
            "defense":     0.25,   # DEF 0.25
            "recent_woba": 0.25,   # wOBA 0.25
            "recent_obp":  0.15,   # OBP 0.15
            "recent_iso":  0.10,   # ISO 0.10
            "recent_con":  0.10,   # CON 0.10
            "avail":       0.15,   # Avail 0.15
        },
        # wOBA 極端に低ければ減点（下位25%で×0.85）
        "soft_penalty": {"woba_bottom_pct": 0.25, "penalty": 0.85},
    },
    {
        # 8番：守備型下位（守備最重視）
        "order": 8,
        "role": "glove_bottom",
        "weights": {
            "defense":     0.35,   # DEF 0.35
            "avail":       0.20,   # Avail 0.20
            "recent_obp":  0.15,   # OBP 0.15
            "recent_woba": 0.15,   # wOBA 0.15
            "recent_con":  0.10,   # CON 0.10
            "recent_iso":  0.05,   # ISO 0.05
        },
        # season_pa 極少は減点（avail < 0.20 で×0.85）
        "soft_penalty": {"low_avail_threshold": 0.20, "penalty": 0.85},
    },
    {
        # 9番（DH有）：第2の1番（OBP＋走力・繋ぎ）
        "order": 9,
        "role": "second_leadoff",
        "weights": {
            "recent_obp":  0.30,   # OBP 0.30
            "recent_run":  0.20,   # RUN 0.20
            "recent_con":  0.15,   # CON 0.15
            "recent_woba": 0.15,   # wOBA 0.15
            "defense":     0.10,   # DEF 0.10
            "avail":       0.10,   # Avail 0.10
        },
        # RUN 下位30% かつ OBP 下位40% で減点（×0.85）
        "soft_penalty": {"run_bottom_pct": 0.30, "obp_bottom_pct_and": 0.40, "penalty": 0.85},
    },
]

# ── DH無（8野手＋9番投手）打順スロット定義 ──────────────────────────────────
# Excel設計書「Book1.xlsx」準拠。9番は投手固定のためスロット定義から除外。
# DH有と明確に異なるウェイト体系（8番がOBP/CON重視など）。
NO_DH_LINEUP_SLOTS = [
    {
        # 1番：出塁起点（OBP最重視・DH有より比率高め）
        "order": 1,
        "role": "leadoff_obp",
        "weights": {
            "recent_obp":  0.38,   # OBP 0.38（DH有より+0.03）
            "recent_run":  0.18,   # RUN 0.18
            "recent_con":  0.12,   # CON 0.12
            "recent_woba": 0.12,   # wOBA 0.12（DH有より-0.08）
            "defense":     0.10,   # DEF 0.10
            "avail":       0.10,   # Avail 0.10
        },
        "soft_penalty": {"obp_bottom_pct": 0.30, "penalty": 0.85},
        "exclude_zero_pa": True,
    },
    {
        # 2番：強打の接着剤（wOBA最重視）
        "order": 2,
        "role": "strong_connector",
        "weights": {
            "recent_woba": 0.35,   # wOBA 0.35
            "recent_obp":  0.20,   # OBP 0.20
            "recent_con":  0.15,   # CON 0.15
            "recent_iso":  0.10,   # ISO 0.10
            "defense":     0.10,   # DEF 0.10
            "avail":       0.10,   # Avail 0.10
        },
        "soft_penalty": {"con_bottom_pct": 0.25, "penalty": 0.93},
    },
    {
        # 3番：万能上位（wOBA最重視・DH有と概ね同じ）
        "order": 3,
        "role": "versatile_upper",
        "weights": {
            "recent_woba": 0.35,   # wOBA 0.35
            "recent_obp":  0.18,   # OBP 0.18（DH有より+0.03）
            "recent_iso":  0.12,   # ISO 0.12（DH有より-0.03）
            "recent_con":  0.10,   # CON 0.10
            "defense":     0.10,   # DEF 0.10
            "avail":       0.15,   # Avail 0.15
        },
        "soft_penalty": {"woba_below_median": True, "penalty": 0.88},
    },
    {
        # 4番：主砲（DH有と同一）
        "order": 4,
        "role": "cleanup_power",
        "weights": {
            "recent_woba": 0.45,   # wOBA 0.45
            "recent_iso":  0.25,   # ISO 0.25
            "recent_obp":  0.10,   # OBP 0.10
            "recent_con":  0.05,   # CON 0.05
            "defense":     0.05,   # DEF 0.05
            "avail":       0.10,   # Avail 0.10
        },
        # 生ISO=0.000かつPA>=5 の選手は4番候補から完全除外
        "soft_penalty": {"iso_threshold": 0.110, "penalty": 0.90},
        "hard_cut_woba_bottom_pct": 0.50,
        "hard_cut_iso_zero": True,
    },
    {
        # 5番：返す2枚目（ISO比率をDH有より若干軽く）
        "order": 5,
        "role": "second_slugger",
        "weights": {
            "recent_woba": 0.35,   # wOBA 0.35
            "recent_iso":  0.22,   # ISO 0.22（DH有より-0.03）
            "recent_obp":  0.10,   # OBP 0.10
            "recent_con":  0.10,   # CON 0.10
            "defense":     0.08,   # DEF 0.08（DH有より+0.03）
            "avail":       0.15,   # Avail 0.15
        },
        "soft_penalty": {"iso_threshold": 0.095, "penalty": 0.90},
    },
    {
        # 6番：中軸下の橋（守備比率が高め、Avail最重視）
        "order": 6,
        "role": "bridge_lower",
        "weights": {
            "recent_woba": 0.22,   # wOBA 0.22（DH有より-0.03）
            "recent_obp":  0.20,   # OBP 0.20
            "defense":     0.18,   # DEF 0.18（DH有より+0.03）
            "recent_con":  0.10,   # CON 0.10
            "recent_iso":  0.10,   # ISO 0.10（DH有より-0.05）
            "avail":       0.20,   # Avail 0.20（DH有より+0.05）
        },
        # recent_pa=0 なら強めの減点（×0.75）
        "soft_penalty": {"zero_pa_penalty": 0.75},
    },
    {
        # 7番：守備込み下位中核（守備＋OBP優先）
        "order": 7,
        "role": "glove_core",
        "weights": {
            "defense":     0.25,   # DEF 0.25
            "recent_obp":  0.18,   # OBP 0.18（DH有より+0.03）
            "recent_woba": 0.18,   # wOBA 0.18（DH有より-0.07）
            "recent_con":  0.12,   # CON 0.12（DH有より+0.02）
            "recent_iso":  0.07,   # ISO 0.07（DH有より-0.03）
            "avail":       0.20,   # Avail 0.20（DH有より+0.05）
        },
        # OBP 下位30%で軽く減点（×0.90）
        "soft_penalty": {"obp_bottom_pct": 0.30, "penalty": 0.90},
    },
    {
        # 8番（DH無）：出塁+コンタクト重視（OBP/CON/守備バランス）
        # 9番が投手なので8番は「投手前の出塁役」として設計
        "order": 8,
        "role": "pre_pitcher",
        "weights": {
            "recent_obp":  0.28,   # OBP 0.28（8番がOBP重視に変化）
            "recent_con":  0.18,   # CON 0.18
            "defense":     0.20,   # DEF 0.20
            "recent_woba": 0.12,   # wOBA 0.12
            "recent_iso":  0.05,   # ISO 0.05
            "avail":       0.17,   # Avail 0.17
        },
        # OBP<0.290 で減点（×0.88）、CON 下位25%も減点（×0.93）
        "soft_penalty": {"obp_abs_threshold": 0.290, "obp_penalty": 0.88,
                         "con_bottom_pct": 0.25, "con_penalty": 0.93},
    },
    # 9番は投手固定のためスロット定義なし
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


# ---------------------------------------------------------------------------
# proran.jp PLAYER_PROFILE 自動生成
# ---------------------------------------------------------------------------

_PRORAN_POS_MAP = {
    "捕手": "C", "一塁手": "1B", "二塁手": "2B", "三塁手": "3B",
    "遊撃手": "SS", "左翼手": "LF", "中堅手": "CF", "右翼手": "RF", "指名打者": "DH",
}

# npb.jp roster の粗ポジション → 守備ポジション別成績が無い場合のフォールバック
_NPB_POS_FALLBACK = {
    "投手": ["P"],
    "捕手": ["C"],
    "内野手": ["1B", "2B", "3B", "SS"],
    "外野手": ["LF", "CF", "RF"],
}


def _fetch_proran_player_map() -> dict[str, int]:
    """proran.jp の player_ranking_b.php から {選手名: proran_id} マップを返す。
    全球団・全登録野手を含む。24時間キャッシュ。
    """
    cache = CACHE.setdefault("_proran_player_map", {"value": None, "expires_at": 0})
    if _cache_alive(cache) and cache.get("value"):
        return cache["value"]

    try:
        html = _fetch_text("https://proran.jp/player_ranking_b.php")
    except Exception:
        return cache.get("value") or {}

    # player_data = [["球団名", "選手名", proran_id, ...], ...]
    # 括弧カウントで JSON 配列の正確な終端を見つける
    idx = html.find("player_data =")
    if idx < 0:
        return cache.get("value") or {}
    start = html.find("[", idx)
    if start < 0:
        return cache.get("value") or {}
    depth = 0
    end_pos = -1
    for ci in range(start, min(start + 300000, len(html))):
        c = html[ci]
        if c == "[":
            depth += 1
        elif c == "]":
            depth -= 1
            if depth == 0:
                end_pos = ci + 1
                break
    if end_pos < 0:
        return cache.get("value") or {}
    json_str = html[start:end_pos]
    try:
        data = json.loads(json_str)
    except Exception:
        return cache.get("value") or {}

    result: dict[str, int] = {}
    for row in data:
        if len(row) >= 3:
            name = str(row[1]).strip()
            pid = int(row[2])
            result[name] = pid

    cache["value"] = result
    cache["expires_at"] = _cache_now() + 60 * 60 * 24  # 24時間
    return result


def _fetch_player_positions_from_proran(proran_id: int) -> list[str]:
    """proran.jp の守備ポジション別成績から打数 >= 1 のポジションリストを返す。
    取得失敗時は空リストを返す。
    """
    try:
        url = f"https://proran.jp/player_detail_more.php?id={proran_id}"
        html = _fetch_text(url)
    except Exception:
        return []

    # 「守備ポジション別成績」セクションを抽出
    m = re.search(r"<h1[^>]*>守備ポジション別成績</h1>(.*?)(?=<h1[^>]*>|$)", html, re.DOTALL)
    if not m:
        return []
    section = m.group(1)

    # 行ラベル（ポジション名）
    pos_labels = re.findall(
        r'player_detail_more_ba[^"]*bg_c_th[^>]*>(.*?)</div>', section
    )
    if not pos_labels:
        return []

    # fixed_l ブロック（ラベル列）を除去してデータ列だけ残す
    section_data = re.sub(
        r'<div class="flex_box fixed_l[^>]*>.*?</div>\s*</div>', "", section, flags=re.DOTALL
    )
    data_vals = re.findall(r'class="player_detail_more_ba[^"]*">(.*?)</div>', section_data)

    n_pos = len(pos_labels)
    if n_pos == 0 or len(data_vals) < n_pos:
        return []

    # 先頭の n_pos 個が「打数」列
    eligible = []
    for row_i, pos_ja in enumerate(pos_labels):
        ab_str = data_vals[row_i]  # 打数列（最初の列）
        try:
            if int(ab_str) >= 1:
                pos_en = _PRORAN_POS_MAP.get(pos_ja)
                if pos_en:
                    eligible.append(pos_en)
        except (ValueError, TypeError):
            pass

    return eligible


def _build_player_profiles_from_npb(team_code: str = "広島") -> dict[str, dict]:
    """npb.jp roster + proran.jp 守備ポジション別成績を組み合わせて
    {選手名: {"eligible_positions": [...]}} を返す。
    投手は除外。結果は team_code 別に 6時間キャッシュ。
    """
    # team_code ごとにサブエントリを持つ辞書として管理
    team_cache = CACHE["player_profile"].setdefault(team_code, {"value": None, "expires_at": 0})
    if _cache_alive(team_cache) and team_cache.get("value"):
        return team_cache["value"]

    # --- Step 1: npb.jp roster から選手名・粗ポジション を取得 ---
    roster: list[dict] = []  # [{"name": "小園 海斗", "npb_pos": "内野手"}, ...]
    try:
        html = _fetch_text("https://npb.jp/announcement/roster/")
        sections = re.split(r"(<h5[^>]*>.*?</h5>)", html, flags=re.DOTALL)
        # 略称(team_code)または正式名(NPB_ROSTER_TEAM_FULLNAME)のどちらかがh5に含まれればマッチ
        # 例: "巨人" は h5 に含まれないが "読売ジャイアンツ" は含まれる
        team_fullname = NPB_ROSTER_TEAM_FULLNAME.get(team_code, team_code)
        for i, s in enumerate(sections):
            if (team_code in s or team_fullname in s) and "<h5" in s:
                if i + 1 < len(sections):
                    block = sections[i + 1]
                    rows = re.findall(r"<tr[^>]*>(.*?)</tr>", block, re.DOTALL)
                    for row in rows:
                        cells = re.findall(r"<td[^>]*>(.*?)</td>", row, re.DOTALL)
                        cells_clean = [re.sub(r"<[^>]+>", "", c).strip() for c in cells]
                        if len(cells_clean) >= 3:
                            # cells_clean = [pos, number, name]
                            npb_pos = cells_clean[0]
                            name_raw = cells_clean[2].replace("\u3000", " ").strip()
                            roster.append({"name": name_raw, "npb_pos": npb_pos})
                break
    except Exception:
        pass

    if not roster:
        # フォールバック: キャッシュ済みがあればそれを返す
        return team_cache.get("value") or {}

    # --- Step 2: proran.jp player_map を取得 ---
    proran_map = _fetch_proran_player_map()

    # 姓のみ → proran_id マップも作成（外国人選手向け）
    # 例: "Ｅ．モンテロ" → "モンテロ" でマッチ
    def _resolve_proran_id(name: str) -> int | None:
        # 完全一致
        pid = proran_map.get(name)
        if pid:
            return pid
        # 空白除去
        pid = proran_map.get(name.replace(" ", ""))
        if pid:
            return pid
        # 全角ピリオド・文字を除去して末尾カタカナ部分だけ試す
        # 例: "Ｅ．モンテロ" → "モンテロ"
        name_stripped = re.sub(r"^[Ａ-Ｚa-zA-Z]+[．.]\s*", "", name).strip()
        if name_stripped and name_stripped != name:
            pid = proran_map.get(name_stripped)
            if pid:
                return pid
        return None

    # --- Step 3: 各選手のポジションを決定 ---
    result: dict[str, dict] = {}
    for player in roster:
        name = player["name"]
        npb_pos = player["npb_pos"]

        # 投手はスキップ
        if npb_pos == "投手":
            continue

        # proran ID を選手名から引く（複数パターン試行）
        proran_id = _resolve_proran_id(name)

        if proran_id:
            positions = _fetch_player_positions_from_proran(proran_id)
            # 1打数以上出場のポジションが得られた場合
            if positions:
                result[name] = {"eligible_positions": positions}
                continue

        # proran で取れない/出場ゼロ → npb 粗ポジションからフォールバック
        fallback = _NPB_POS_FALLBACK.get(npb_pos, [])
        if fallback:
            result[name] = {"eligible_positions": list(fallback)}

    team_cache["value"] = result
    team_cache["expires_at"] = _cache_now() + 60 * 60 * 6  # 6時間
    return result


def _update_player_name_aliases(profile: dict[str, dict]) -> None:
    """profile のキー（正式選手名）から PLAYER_NAME_ALIASES を自動補完する。

    追加するエイリアス:
    1. スペース除去版 → 正式名
       例: "小園 海斗" → {"小園海斗": "小園 海斗"}
    2. 外国人選手の姓のみ版 → 正式名
       例: "Ｅ．モンテロ" → {"モンテロ": "Ｅ．モンテロ"}
    3. 日本人選手の姓のみ版 → 正式名（同姓選手がいない場合のみ）
       例: "吉川 尚輝" → {"吉川": "吉川 尚輝"}
       ※ NPB のボックススコアでは「吉川」等、姓のみで記載されることがあるため。
       ※ 同一プロファイル内に同姓選手が複数いる場合は登録しない（曖昧性回避）。

    既存の手書きエントリは上書きしない（手書き優先）。
    """
    # 3) 日本人選手の姓のみ版: 同姓重複チェックのため先にカウント
    surname_count: dict[str, int] = {}
    for full_name in profile:
        # 外国人選手（Ａ-Ｚ or A-Z + ．で始まる名前）はスキップ
        if re.match(r"^[Ａ-Ｚa-zA-Z]+[．.]", full_name):
            continue
        parts = [p for p in full_name.replace("　", " ").split(" ") if p]
        if len(parts) >= 2:
            surname = _normalize_player_name(parts[0])
            surname_count[surname] = surname_count.get(surname, 0) + 1

    for full_name in profile:
        # 1) スペース除去 → 正式名
        no_space = _normalize_player_name(full_name)  # 例: "小園海斗"
        if no_space and no_space not in PLAYER_NAME_ALIASES:
            PLAYER_NAME_ALIASES[no_space] = full_name

        # 2) 外国人選手: "Ｅ．モンテロ" → "モンテロ" (姓のみ)
        surname_only = re.sub(r"^[Ａ-Ｚa-zA-Z]+[．.]\s*", "", full_name).strip()
        if surname_only and surname_only != full_name:
            surname_norm = _normalize_player_name(surname_only)
            if surname_norm and surname_norm not in PLAYER_NAME_ALIASES:
                PLAYER_NAME_ALIASES[surname_norm] = full_name

        # 3) 日本人選手: 姓のみ → 正式名（同姓が1名のみの場合）
        elif not re.match(r"^[Ａ-Ｚa-zA-Z]+[．.]", full_name):
            parts = [p for p in full_name.replace("　", " ").split(" ") if p]
            if len(parts) >= 2:
                surname = _normalize_player_name(parts[0])
                if surname and surname_count.get(surname, 0) == 1:
                    if surname not in PLAYER_NAME_ALIASES:
                        PLAYER_NAME_ALIASES[surname] = full_name


def _get_player_profile(team_code: str = "広島") -> dict[str, dict]:
    """PLAYER_PROFILE を返す。npb.jp+proran.jp 自動生成版を優先し、
    失敗時はハードコード PLAYER_PROFILE にフォールバックする。
    取得成功時は PLAYER_NAME_ALIASES を自動補完する。
    """
    auto = _build_player_profiles_from_npb(team_code)
    if auto:
        _update_player_name_aliases(auto)
        return auto
    return PLAYER_PROFILE


def _normalize_player_name(name: str) -> str:
    if not name:
        return ""
    text = unescape(str(name)).strip()
    text = text.replace("　", "").replace(" ", "")
    text = re.sub(r"\s+", "", text)
    return text


def _canonical_player_name(name: str, team_code: str | None = None) -> str:
    """選手名を正式名（スペースあり）に正規化する。
    team_code が指定された場合は、そのチームのプロファイルも検索対象に追加する。

    検索優先順位:
    1. team_code プロファイルでの完全一致（スペース除去）
    2. PLAYER_NAME_ALIASES での完全一致（スペース除去）
       ただし姓のみ（= normalized が空白なしで1トークン）の場合は後回し
    3. SEASON_OVERALL_BATTING での逆引き
    4. team_code プロファイルでの姓のみマッチ（同姓1名のみ）
    5. PLAYER_NAME_ALIASES での姓のみマッチ（フォールバック）
    6. 広島プロファイルでの姓のみマッチ（後方互換フォールバック）
    """
    normalized = _normalize_player_name(name)

    if not normalized:
        return name

    # ── プロファイルリストを構築 ──
    profiles_to_search = []
    if team_code:
        team_profile = _get_player_profile(team_code)
        if team_profile:
            profiles_to_search.append(team_profile)

    carp_profile = _get_player_profile("広島")
    if carp_profile and (not team_code or team_code != "広島"):
        profiles_to_search.append(carp_profile)

    # ── Step 1: team_code プロファイルで完全一致（スペース除去）優先 ──
    # → PLAYER_NAME_ALIASES より先に確認することで他球団の同姓選手との衝突を防ぐ
    for _profile in profiles_to_search:
        for full_name in _profile.keys():
            if _normalize_player_name(full_name) == normalized:
                return full_name

    # ── Step 2: PLAYER_NAME_ALIASES での完全一致（姓のみキーを除く） ──
    # 「佐藤」のような姓のみキーは Step 4 で team_code 優先で解決するため、
    # ここでは「佐藤輝明」などのスペース除去フルネームのみ信頼する。
    # 姓のみ判定: normalized にスペースがなく、かつ元の name にスペースがない（姓のみ入力）
    is_surname_only = " " not in name.strip() and "　" not in name.strip()
    if not is_surname_only and normalized in PLAYER_NAME_ALIASES:
        return PLAYER_NAME_ALIASES[normalized]

    # ── Step 3: SEASON_OVERALL_BATTING のキーでも逆引き ──
    for full_name in SEASON_OVERALL_BATTING:
        if _normalize_player_name(full_name) == normalized:
            return full_name

    # ── Step 4: team_code プロファイルでの姓のみマッチ（同姓1名のみ） ──
    for _profile in profiles_to_search:
        surname_matches = []
        for full_name in _profile.keys():
            parts = [p for p in full_name.replace("　", " ").split(" ") if p]
            if not parts:
                continue
            surname_normalized = _normalize_player_name(parts[0])
            if surname_normalized == normalized:
                surname_matches.append(full_name)
        if len(surname_matches) == 1:
            return surname_matches[0]

    # ── Step 5: PLAYER_NAME_ALIASES での姓のみフォールバック ──
    if normalized in PLAYER_NAME_ALIASES:
        return PLAYER_NAME_ALIASES[normalized]

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

    profile = _get_player_profile().get(canonical, {})

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


@lru_cache(maxsize=16)
def _discover_proran_player_ids(team_code: str = "広島") -> dict[str, str]:
    """proran.jp のチーム別打者一覧ページから {正規化選手名: proran_id} マップを返す。
    team_code を引数に持つことで lru_cache のキーがチーム別に自動分離される。
    """
    proran_code = PRORAN_TEAM_CODE.get(team_code, "_c")
    url = f"https://proran.jp/team_detail_b.php?t={proran_code}"
    html = _fetch_text(url)
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

    print(f"DEBUG_PRORAN_PLAYER_IDS_COUNT[{team_code}]", len(result))
    return result


def _get_proran_player_ids(team_code: str = "広島") -> dict[str, str]:
    try:
        return _discover_proran_player_ids(team_code)
    except Exception as e:
        print(f"DEBUG_PRORAN_ID_ERROR[{team_code}]", str(e))
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


def _build_season_position_batting_from_proran(team_code: str = "広島") -> dict:
    result = {}
    player_ids = _get_proran_player_ids(team_code)

    # 対象プレイヤーと player_id のペアを収集
    targets: list[tuple[str, str]] = []
    for player_name in _get_player_profile(team_code).keys():
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
            print(f"DEBUG_PRORAN_BUILD_ERROR[{team_code}]", player_name, str(e))
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


def _get_season_position_batting(team_code: str = "広島") -> dict:
    """team_code 別のシーズン打撃成績（proran.jp）を返す。
    team_code ごとにキャッシュエントリを分離して管理する。
    """
    cache_bucket = CACHE.setdefault("season_position_batting_v2", {})
    cache_entry  = cache_bucket.get(team_code, {"value": None, "expires_at": 0})

    if _cache_alive(cache_entry):
        cached_value = cache_entry.get("value")
        if isinstance(cached_value, dict):
            return cached_value

    # キャッシュ期限切れでも古いデータがあればすぐ返し、バックグラウンドで更新
    stale = cache_entry.get("value")
    if stale and isinstance(stale, dict) and stale:
        def _bg_refresh():
            try:
                data = _build_season_position_batting_from_proran(team_code)
                if data and isinstance(data, dict):
                    cache_bucket[team_code] = {
                        "expires_at": _cache_now() + CACHE_TTL_SEASON_POSITION_BATTING,
                        "value": data,
                    }
                    # 広島の場合はグローバル変数も更新（後方互換）
                    if team_code == "広島":
                        global SEASON_POSITION_BATTING
                        SEASON_POSITION_BATTING = data
            except Exception as e:
                print(f"DEBUG_SEASON_POSITION_BATTING_BG_ERROR[{team_code}]", str(e))
        # 一時的に60秒延長して二重更新を防ぐ
        cache_bucket.setdefault(team_code, {})["expires_at"] = _cache_now() + 60
        threading.Thread(target=_bg_refresh, daemon=True, name=f"bg-season-batting-{team_code}").start()
        return stale

    try:
        data = _build_season_position_batting_from_proran(team_code)
        if not isinstance(data, dict):
            data = {}
    except Exception as e:
        print(f"DEBUG_SEASON_POSITION_BATTING_ERROR[{team_code}]", str(e))
        data = {}

    if data:
        cache_bucket[team_code] = {
            "expires_at": _cache_now() + CACHE_TTL_SEASON_POSITION_BATTING,
            "value": data,
        }
        if team_code == "広島":
            global SEASON_POSITION_BATTING
            SEASON_POSITION_BATTING = data
        return data

    # 広島のフォールバック: グローバル変数を使用
    fallback = SEASON_POSITION_BATTING if (team_code == "広島" and isinstance(SEASON_POSITION_BATTING, dict)) else {}
    cache_bucket[team_code] = {
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


def _build_player_defense_from_npbbasement(team_code: str = "広島") -> dict:
    """npbbasement の守備指標から team_code に属する選手だけ抽出して返す。
    npbbasement は全球団データを1ページに持つため URL 変更不要。
    _get_player_profile(team_code) のキーでフィルタするだけで全球団対応できる。
    """
    result = {}
    players = _load_npbbasement_players()

    normalized_profile_names = {
        _normalize_player_name(name): name
        for name in _get_player_profile(team_code).keys()
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


def _get_player_defense(team_code: str = "広島") -> dict[str, dict[str, float]]:
    """team_code 別の守備指標を返す。
    npbbasement は全球団1ページなので、キャッシュキーに team_code を含めて管理する。
    """
    cache_bucket = CACHE.setdefault("player_defense_v2", {})
    cache_entry  = cache_bucket.get(team_code, {"value": None, "expires_at": 0})

    if _cache_alive(cache_entry) and cache_entry.get("value"):
        return cache_entry["value"]

    # キャッシュ期限切れでも古いデータがあればすぐ返し、バックグラウンドで更新
    stale = cache_entry.get("value")
    if stale and isinstance(stale, dict) and stale:
        def _bg_refresh_defense():
            try:
                data = _build_player_defense_from_npbbasement(team_code)
                if data:
                    cache_bucket[team_code] = {
                        "expires_at": _cache_now() + CACHE_TTL_PLAYER_DEFENSE,
                        "value": data,
                    }
                    if team_code == "広島":
                        global PLAYER_DEFENSE
                        PLAYER_DEFENSE = data
            except Exception as e:
                print(f"DEBUG_PLAYER_DEFENSE_BG_ERROR[{team_code}]", str(e))
        cache_bucket.setdefault(team_code, {})["expires_at"] = _cache_now() + 60
        threading.Thread(target=_bg_refresh_defense, daemon=True, name=f"bg-player-defense-{team_code}").start()
        return stale

    try:
        data = _build_player_defense_from_npbbasement(team_code)
        if not data:
            # 広島のみフォールバック定数を持つ
            data = dict(PLAYER_DEFENSE_FALLBACK) if team_code == "広島" else {}
    except Exception:
        data = dict(PLAYER_DEFENSE_FALLBACK) if team_code == "広島" else {}

    cache_bucket[team_code] = {
        "expires_at": _cache_now() + CACHE_TTL_PLAYER_DEFENSE,
        "value": data,
    }
    if team_code == "広島":
        global PLAYER_DEFENSE
        PLAYER_DEFENSE = data
    return data
def _get_adjusted_position_batting(player_name: str, position: str, team_code: str = "広島") -> dict:
    """player_name の position での打撃成績をベイズ補正して返す。
    team_code 別のキャッシュから取得し、なければ proran から即時フェッチする。
    """
    # team_code 別キャッシュを参照
    cache_bucket = CACHE.setdefault("season_position_batting_v2", {})
    season_data: dict = cache_bucket.get(team_code, {}).get("value") or {}

    # キャッシュが空なら同期ロード（初回のみ）
    if not season_data:
        season_data = _get_season_position_batting(team_code) or {}

    canonical_name = _canonical_player_name(player_name, team_code)
    normalized_name = _normalize_player_name(canonical_name)

    player_stats = (
        season_data.get(canonical_name)
        or season_data.get(normalized_name)
        or season_data.get(_normalize_player_name(player_name))
        or {}
    )

    # SEASON_OVERALL_BATTING は全12球団対応済み
    # スペースなし名前でも検索できるよう正規化マッチング
    overall = (
        SEASON_OVERALL_BATTING.get(canonical_name)
        or SEASON_OVERALL_BATTING.get(normalized_name)
        or SEASON_OVERALL_BATTING.get(_normalize_player_name(player_name))
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
        player_ids = _get_proran_player_ids(team_code)
        player_id = player_ids.get(normalized_name)

        if player_id:
            try:
                fetched = _fetch_proran_position_batting(canonical_name, player_id)

                if fetched:
                    season_data[canonical_name] = fetched
                    season_data[normalized_name] = fetched
                    player_stats = fetched
                else:
                    empty_marker = {"__empty__": True}
                    season_data[canonical_name] = empty_marker
                    season_data[normalized_name] = empty_marker
                    player_stats = empty_marker

                # キャッシュを更新（既存エントリがあれば value だけ差し替え）
                entry = cache_bucket.setdefault(team_code, {"value": {}, "expires_at": 0})
                entry["value"] = season_data
                if entry["expires_at"] < _cache_now():
                    entry["expires_at"] = _cache_now() + 60

            except Exception as e:
                print(f"DEBUG_PRORAN_FETCH_ERROR[{team_code}]", canonical_name, str(e))
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


@lru_cache(maxsize=16)
def _fetch_current_first_team_position_players(team_code: str = "広島") -> set[str]:
    """NPB公示「出場選手登録名簿」ページから指定球団の一軍登録選手を取得する。

    ページ構造:
      - ページ上部: 当日の登録/抹消情報（当日変更があった選手のみ）
      - ページ下部: 「出場選手一覧」セクション（全球団の全登録選手を掲載）
    → 「出場選手一覧」セクション内の球団ブロックを優先的に取得する。

    他球団の同姓選手との誤マッチを防ぐため姓のみ(2文字以下)のエイリアスはスキップ。
    PLAYER_PROFILE 登録済み選手名のみを返す（eligible_positions が保証されるため）。
    """
    team_fullname = NPB_ROSTER_TEAM_FULLNAME.get(team_code, "広島東洋カープ")
    team_fullname_esc = re.escape(team_fullname)

    try:
        html = _fetch_html(FIRST_TEAM_MEMBERS_URL)
        text = _clean_text(html)
        normalized = text.replace(" ", "").replace("　", "")

        # ── ① 「出場選手一覧」セクション内の対象球団ブロックを優先取得 ──
        block: str | None = None
        idx_list = normalized.find("出場選手一覧")
        if idx_list >= 0:
            after_list = normalized[idx_list:]
            team_match = re.search(
                rf"{team_fullname_esc}(.*?)以上\d+名",
                after_list,
                re.DOTALL,
            )
            if team_match:
                block = team_match.group(1)
                print(f"DEBUG_FIRST_TEAM[{team_code}]: 出場選手一覧セクションから取得 ({len(block)}文字)")

        # ── ② フォールバック: ページ先頭からのブロック ──
        if not block:
            team_match = re.search(
                rf"{team_fullname_esc}(.*?)以上\d*名",
                normalized,
                re.DOTALL,
            )
            if team_match:
                block = team_match.group(1)
                print(f"DEBUG_FIRST_TEAM[{team_code}]: フォールバック(先頭ブロック) ({len(block)}文字)")

        # ── ③ フォールバック: 次の球団名まで ──
        if not block:
            # 全球団正式名リストをパイプ接続して「次球団まで」の正規表現を構築
            other_fullnames = [
                re.escape(fn)
                for tc, fn in NPB_ROSTER_TEAM_FULLNAME.items()
                if tc != team_code
            ]
            others_pat = "|".join(other_fullnames)
            team_match = re.search(
                rf"{team_fullname_esc}(.*?)(?:{others_pat})",
                normalized,
                re.DOTALL,
            )
            if team_match:
                block = team_match.group(1)
                print(f"DEBUG_FIRST_TEAM[{team_code}]: フォールバック(次球団名区切り) ({len(block)}文字)")

        if not block:
            print(f"DEBUG_FIRST_TEAM_BLOCK_NOT_FOUND[{team_code}]")
            # 広島のみハードコードフォールバックを持つ
            return set(LAST_FIXED_FIRST_TEAM_POSITION_PLAYERS) if team_code == "広島" else set()

        # PLAYER_PROFILE 登録済み選手とブロック内でマッチング
        # 姓のみ（2文字以下）のエイリアスは他球団選手との誤マッチ防止のためスキップ
        result: set[str] = set()
        for name in _get_player_profile(team_code).keys():
            for alias in _name_aliases(name):
                n = _normalize_name(alias)
                if len(n) <= 2:
                    continue
                if n in block:
                    result.add(name)
                    break

        print(f"DEBUG_FIRST_TEAM_FOUND[{team_code}] {len(result)}名を公示ページから取得")
        if result:
            return result
    except Exception as e:
        print(f"DEBUG_FIRST_TEAM_MEMBER_ERROR[{team_code}]", str(e))

    return set(LAST_FIXED_FIRST_TEAM_POSITION_PLAYERS) if team_code == "広島" else set()


def _get_active_first_team_position_players(now: datetime | None = None, team_code: str = "広島") -> set[str]:
    """npb.jp の一軍登録名簿ページを参照して一軍登録選手を返す。
    取得失敗時は広島のみ LAST_FIXED_FIRST_TEAM_POSITION_PLAYERS にフォールバック。
    他球団で取得失敗時は空 set() を返す（呼び出し側でフィルタなし扱い）。
    """
    current = _fetch_current_first_team_position_players(team_code)
    if current:
        return set(current)
    # 広島: ハードコードフォールバック、他球団: 空set（呼び出し側が「not active」→全員表示に切り替える）
    return set(LAST_FIXED_FIRST_TEAM_POSITION_PLAYERS) if team_code == "広島" else set()


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


def _build_farm_score_maps(team_code: str = "広島") -> dict:
    """二軍打撃成績ページの全選手（50PA以上）をスキャンしてスコアを算出する。
    candidate_names 縛りなし。選手名はページ記載の正規化名をそのまま使用。
    返り値の farm_score キーは正規化済み選手名 → スコア(float)。
    """
    try:
        npb_code = NPB_FARM_STATS_CODE.get(team_code, "c")
        farm_url = f"https://npb.jp/bis/{CURRENT_SEASON_YEAR}/stats/idb2_{npb_code}.html"
        html = _fetch_html(farm_url)
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
        print(f"DEBUG_FARM_SCORE_ERROR[{team_code}]", str(e))
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


def _is_home_game(venue: str, team_code: str = "広島") -> bool:
    venue = _clean_text(venue)
    keywords = TEAM_HOME_VENUE_KEYWORDS.get(team_code, HOME_VENUE_KEYWORDS)
    return any(keyword in venue for keyword in keywords)


def _extract_year_from_results_page(html: str) -> str:
    m = re.search(r"(\d{4})年度", html)
    return m.group(1) if m else str(CURRENT_SEASON_YEAR)


def _extract_previous_results_page_url(html: str, team_npb_code: str = "c") -> str | None:
    index_name = f"results_{team_npb_code}_index.html"
    pattern = rf'href="([^"]*results_{re.escape(team_npb_code)}[^"]*.html)"'
    links = re.findall(pattern, html)
    for link in links:
        if index_name in link:
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


def _parse_result_rows_to_games(rows: list[list[str]], year: str, team_npb_code: str = "c") -> list[dict]:
    games: list[dict] = []
    current_month: int | None = None
    # npb_code → team_code の逆引き（ホーム球場判定に使用）
    _code_to_team = {v: k for k, v in NPB_RESULTS_TEAM_CODE.items()}
    team_code_for_home = _code_to_team.get(team_npb_code, "広島")

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
        matchup = f"{team_npb_code}-{opponent_code}" if _is_home_game(venue, team_code_for_home) else f"{opponent_code}-{team_npb_code}"
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
def _fetch_recent_carp_games(limit: int, team_code: str = "広島") -> list[dict]:
    npb_code = NPB_RESULTS_TEAM_CODE.get(team_code, "c")
    results_url = f"https://npb.jp/bis/teams/results_{npb_code}_index.html"
    current_html = _fetch_html(results_url)
    year = _extract_year_from_results_page(current_html)

    all_games: list[dict] = []

    current_rows = _extract_result_rows_from_html(current_html)
    all_games.extend(_parse_result_rows_to_games(current_rows, year, npb_code))

    previous_url = _extract_previous_results_page_url(current_html, npb_code)
    if previous_url:
        try:
            previous_html = _fetch_html(previous_url)
            previous_rows = _extract_result_rows_from_html(previous_html)
            all_games.extend(_parse_result_rows_to_games(previous_rows, year, npb_code))
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


def _parse_carp_batting_rows(box_url: str, team_code: str = "広島") -> list[dict]:
    html = _fetch_html(box_url)

    # ── 生HTMLから選手IDリンク→選手名のマップを構築 ──
    # NPB box.html は 2026年以降、選手セルが姓のみ表示になっているため
    # <a href="/bis/players/{id}.html">姓</a> パターンからIDを取得して
    # 後段の _canonical_player_name でフルネームに解決する際のヒントとして使う
    # （本関数では raw テキストのみを返す; フルネーム解決は呼び出し元の
    #   _aggregate_recent_batting_stats で team_code を使って行う）

    tables = _extract_tables(html)

    batting_tables = [table for table in tables if _is_batting_table(table)]
    if len(batting_tables) < 2:
        raise ValueError(f"打撃表を見つけられませんでした: {box_url}")

    npb_code = NPB_RESULTS_TEAM_CODE.get(team_code, "c")
    carp_is_home = bool(re.search(rf"/scores/\d{{4}}/\d{{4}}/{re.escape(npb_code)}-[a-z]{{1,2}}-\d{{2}}/box\.html", box_url))
    carp_table = batting_tables[1] if carp_is_home else batting_tables[0]

    header = carp_table[0]
    index_map = {name: idx for idx, name in enumerate(header)}

    def cell(row: list[str], name: str) -> str:
        idx = index_map.get(name)
        if idx is None or idx >= len(row):
            return ""
        return row[idx]

    result_start_idx = index_map.get("盗塁", 7) + 1

    # ── 打撃テーブルのHTML断片を取得して選手フルネームを補完 ──
    # _extract_tables はテキストのみ返すが、選手名リンクにフルネームが格納されている場合がある
    # （2026年のbox.htmlは姓のみ表示だが、将来変更に備えてここでも対応）
    # 今は _canonical_player_name + team_code で解決するため特別処理不要

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


def _aggregate_recent_batting_stats(window_games: int, team_code: str = "広島") -> dict:
    # team_code のプロファイルを事前ロードして PLAYER_NAME_ALIASES を更新する。
    # これにより _canonical_player_name が他球団の日本人選手名（姓のみ表記）を
    # 正式名（姓 + 名）へ正しく正規化できるようになる。
    _get_player_profile(team_code)

    cache_bucket = _cache_get_bucket("recent_batting")
    cache_key = f"aggregate:{window_games}:{team_code}"
    cache_entry = cache_bucket.get(cache_key)

    if _cache_alive(cache_entry):
        cached_value = cache_entry.get("value")
        if isinstance(cached_value, dict):
            return cached_value

    games = _fetch_recent_carp_games(window_games, team_code)

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

    # game_snapshots: 試合ごとの選手成績スナップショット配列（新しい試合が index=0）
    # 各要素 = dict[canonical_name -> stat_line]
    game_snapshots: list[dict[str, dict]] = []

    for game in games:
        try:
            rows = _parse_carp_batting_rows(game["box_url"], team_code)
        except Exception as e:
            print("DEBUG_RECENT_GAME_PARSE_ERROR", game.get("box_url"), str(e))
            game_snapshots.append({})
            continue

        seen_in_game: set[str] = set()
        game_stat: dict[str, dict] = {}

        for row in rows:
            canonical_name = _canonical_player_name(row.get("player_name", ""), team_code)
            if not canonical_name:
                continue

            # flat合算用（後方互換性維持）
            stat_line = player_totals.setdefault(
                canonical_name,
                _empty_stat_line(canonical_name),
            )

            # 試合別スナップショット用
            game_line = game_stat.setdefault(
                canonical_name,
                _empty_stat_line(canonical_name),
            )

            if canonical_name not in seen_in_game:
                stat_line["games"] += 1
                game_line["games"] += 1
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
                game_line[key] += value
                team_totals[key] += value

        game_snapshots.append(game_stat)

    # ── 選手名正規化クリーンアップ ──
    # box.html が姓のみ表示の場合、_parse_carp_batting_rows が返す player_name は
    # 「佐藤」など姓のみになることがある。_canonical_player_name は呼び出し時点での
    # PLAYER_NAME_ALIASES に依存するため、プロファイルロード後にキーを再正規化する。
    # （既に _get_player_profile を L2272 で呼び済みなのでここでは ALIASES が最新）
    normalized_totals: dict[str, dict] = {}
    for raw_key, stat_line in player_totals.items():
        canonical = _canonical_player_name(raw_key, team_code)
        existing = normalized_totals.get(canonical)
        if existing:
            # 同一選手が複数キーに分散していた場合はマージ
            for k in ["games", "at_bats", "runs", "hits", "rbi", "steals",
                      "doubles", "triples", "homeruns", "walks",
                      "hit_by_pitch", "strikeouts", "sacrifice_bunts", "sacrifice_flies"]:
                existing[k] = existing.get(k, 0) + stat_line.get(k, 0)
            # games は重複カウントしないよう上書き（二重集計防止のため max を使用）
            existing["games"] = max(existing.get("games", 0), stat_line.get("games", 0))
        else:
            merged = dict(stat_line)
            merged["player_name"] = canonical
            normalized_totals[canonical] = merged
    player_totals = normalized_totals

    # game_snapshots も同様に選手名を正規化
    normalized_snapshots: list[dict[str, dict]] = []
    for snap in game_snapshots:
        norm_snap: dict[str, dict] = {}
        for raw_key, sline in snap.items():
            canonical = _canonical_player_name(raw_key, team_code)
            existing = norm_snap.get(canonical)
            if existing:
                for k in ["games", "at_bats", "runs", "hits", "rbi", "steals",
                          "doubles", "triples", "homeruns", "walks",
                          "hit_by_pitch", "strikeouts", "sacrifice_bunts", "sacrifice_flies"]:
                    existing[k] = existing.get(k, 0) + sline.get(k, 0)
                existing["games"] = max(existing.get("games", 0), sline.get("games", 0))
            else:
                merged = dict(sline)
                merged["player_name"] = canonical
                norm_snap[canonical] = merged
        normalized_snapshots.append(norm_snap)
    game_snapshots = normalized_snapshots

    result = {
        "games": games,
        "player_totals": player_totals,
        "team_totals": team_totals,
        "game_snapshots": game_snapshots,  # 試合別スナップショット（index=0が最新）
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


def _build_recent_batting_response(window_games: int, team_code: str = "広島") -> dict:
    cache_bucket = _cache_get_bucket("recent_batting")
    cache_key = f"response:{window_games}:{team_code}"
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
                agg_bucket.pop(f"aggregate:{window_games}:{team_code}", None)
                aggregated = _aggregate_recent_batting_stats(window_games, team_code)
                _do_build_recent_batting(window_games, aggregated, cache_bucket, cache_key)
            except Exception as e:
                print("DEBUG_RECENT_BATTING_BG_ERROR", str(e))
        cache_bucket[cache_key] = {**cache_entry, "expires_at": _cache_now() + 60}
        threading.Thread(target=_bg_rebuild_recent, daemon=True, name=f"bg-recent-{window_games}-{team_code}").start()
        return stale

    aggregated = _aggregate_recent_batting_stats(window_games, team_code)
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


def _season_snapshot_map(team_code: str = "広島") -> dict[str, dict]:
    """通算成績スナップショットを選手名→dict で返す。

    SEASON_OVERALL_BATTING（手動定数）と _get_adjusted_position_batting（proran）
    を組み合わせて打順評価用の adj_* フィールドを生成する。

    精度打順・ホット打順では「直近 N 試合」を使うが、
    通算打順では「今シーズンの通算成績」をそのまま adj として使う。
    """
    candidate_names = _get_prediction_candidate_names(team_code=team_code)
    result: dict[str, dict] = {}

    for player_name in candidate_names:
        cname = _canonical_player_name(player_name, team_code)

        # ① SEASON_OVERALL_BATTING から通算 OBP / ISO を取得
        overall = (
            SEASON_OVERALL_BATTING.get(cname)
            or SEASON_OVERALL_BATTING.get(player_name)
            or SEASON_OVERALL_BATTING.get(_normalize_player_name(player_name))
            or {}
        )
        s_obp  = float(overall.get("obp",  NPB_LEAGUE_AVG_OBP) or NPB_LEAGUE_AVG_OBP)
        s_iso  = float(overall.get("iso",  NPB_LEAGUE_AVG_ISO)  or NPB_LEAGUE_AVG_ISO)
        # wOBA 近似: シーズン全体 wOBA がなければ OBP ベースで推定
        s_woba = float(overall.get("woba", _LEAGUE_WOBA) or _LEAGUE_WOBA)
        s_con  = float(overall.get("con",  0.77) or 0.77)
        s_run  = float(overall.get("run",  0.05) or 0.05)

        # ② シーズン打席数: proran ポジション別から概算
        eligible = (
            (_get_player_profile(team_code).get(cname) or {}).get("eligible_positions", [])
            or []
        )
        best_pa = 0.0
        for pos in eligible:
            sp = _get_adjusted_position_batting(cname, pos, team_code)
            best_pa = max(best_pa, float(sp.get("pa", 0.0) or 0.0))

        # PA が取れない場合はシーズン途中選手として 30 を設定（フィルタを通過させる）
        s_pa = best_pa if best_pa > 0 else 30.0

        result[cname] = {
            # 表示用（生の観測値として通算成績を格納）
            "games":    0,          # 通算試合数は使わない
            "pa":       int(s_pa),  # pa_floor チェック用（>= 1 を常に満たす）
            "ab":       int(s_pa),
            "obp":      _round3(s_obp),
            "iso":      _round3(s_iso),
            "woba":     _round3(s_woba),
            "con":      _round3(s_con),
            "run":      _round3(s_run),
            # スコア計算用（= 通算値をそのまま adj として使う）
            "adj_obp":  _round3(s_obp),
            "adj_iso":  _round3(s_iso),
            "adj_woba": _round3(s_woba),
            "adj_con":  _round3(s_con),
            "adj_run":  _round3(s_run),
            # prior は不要だが互換性のため格納
            "prior_obp":  _round3(s_obp),
            "prior_iso":  _round3(s_iso),
            "prior_woba": _round3(s_woba),
            "reliability": 1.0,     # 通算成績は常に信頼度 MAX
            "mode":     "season",
            "raw":      overall,
        }

    return result


def _recent_snapshot_map(
    window_games: int,
    team_code: str = "広島",
    mode: str = "precision",
) -> dict[str, dict]:
    """直近 window_games 試合の打撃スナップショットを選手名→dict で返す。

    mode="precision"（デフォルト）:
        ベイズ収縮あり。打席数が少ない選手はシーズン期待値に引き戻す。
        adj = (pa × raw + PRIOR_PA × prior_val) / (pa + PRIOR_PA)

    mode="hot":
        ベイズ収縮なし（PRIOR_PA=0）。直近 window_games 試合の
        加重移動平均（新しい試合を重視）で評価する。
        PA=0 の選手は adj 値がゼロになり自然に下位に沈む。

    mode="season":
        通算成績（SEASON_OVERALL_BATTING）をそのまま adj として使用。
        window_games は無視される。
    """
    # ── season モード：通算スナップショットを返す ──
    if mode == "season":
        return _season_snapshot_map(team_code)

    aggregated = _aggregate_recent_batting_stats(window_games, team_code)
    result: dict[str, dict] = {}

    # hotモードは事前分布の重みをゼロにする
    _obp_prior_pa  = 0 if mode == "hot" else RECENT_OBP_PRIOR_PA
    _iso_prior_ab  = 0 if mode == "hot" else RECENT_ISO_PRIOR_AB
    _woba_prior_pa = 0 if mode == "hot" else RECENT_WOBA_PRIOR_PA

    # ── hot モード: 加重移動平均で「直近ほど重視」スナップショットを合成 ──
    # game_snapshots[0] = 最新試合, game_snapshots[-1] = 最古試合
    # 重み: [N, N-1, ..., 1] → 最新試合の重みが最大
    if mode == "hot":
        game_snapshots: list[dict[str, dict]] = aggregated.get("game_snapshots", [])
        n_snaps = len(game_snapshots)
        if n_snaps > 0:
            # 重みベクトル: index=0（最新）が n_snaps、index=-1（最古）が 1
            weights = [float(n_snaps - i) for i in range(n_snaps)]
            weight_sum = sum(weights)  # n*(n+1)/2

            # 加重合算用バッファ: player_name → {stat: weighted_sum}
            _wt_buf: dict[str, dict[str, float]] = {}
            _wt_pa:  dict[str, float] = {}

            for snap, w in zip(game_snapshots, weights):
                for cname, sline in snap.items():
                    if cname not in _wt_buf:
                        _wt_buf[cname] = {k: 0.0 for k in [
                            "at_bats", "runs", "hits", "rbi", "steals",
                            "doubles", "triples", "homeruns", "walks",
                            "hit_by_pitch", "strikeouts",
                            "sacrifice_bunts", "sacrifice_flies", "games",
                        ]}
                        _wt_pa[cname] = 0.0
                    for k in _wt_buf[cname]:
                        _wt_buf[cname][k] += w * int(sline.get(k, 0) or 0)
                    # 加重 PA（出塁機会）も計算
                    g_pa = (
                        int(sline.get("at_bats", 0) or 0)
                        + int(sline.get("walks", 0) or 0)
                        + int(sline.get("hit_by_pitch", 0) or 0)
                        + int(sline.get("sacrifice_flies", 0) or 0)
                    )
                    _wt_pa[cname] += w * g_pa

            # 加重平均 stat_line を生成（スケールを維持するため weight_sum で除算せず
            # 整数換算して各試合分相当に正規化する）
            # → 指標計算（OBP, ISO, wOBA）はこの合算値から直接計算
            weighted_totals: dict[str, dict] = {}
            for cname, wbuf in _wt_buf.items():
                wt_line = {k: wbuf[k] for k in wbuf}
                wt_line["player_name"] = cname
                weighted_totals[cname] = wt_line

            # weighted_totals を使って指標計算するためにローカル関数を定義
            def _calc_wt_pa(wt: dict) -> float:
                return (
                    wt.get("at_bats", 0.0)
                    + wt.get("walks", 0.0)
                    + wt.get("hit_by_pitch", 0.0)
                    + wt.get("sacrifice_flies", 0.0)
                )

            def _calc_wt_obp(wt: dict) -> float:
                h   = wt.get("hits", 0.0)
                ab  = wt.get("at_bats", 0.0)
                bb  = wt.get("walks", 0.0)
                hbp = wt.get("hit_by_pitch", 0.0)
                sf  = wt.get("sacrifice_flies", 0.0)
                denom = ab + bb + hbp + sf
                return _round3((h + bb + hbp) / denom) if denom > 0 else 0.0

            def _calc_wt_iso(wt: dict) -> float:
                ab = wt.get("at_bats", 0.0)
                if ab <= 0:
                    return 0.0
                tb = (
                    wt.get("hits", 0.0)
                    + wt.get("doubles", 0.0)
                    + 2.0 * wt.get("triples", 0.0)
                    + 3.0 * wt.get("homeruns", 0.0)
                )
                slg = tb / ab
                avg = wt.get("hits", 0.0) / ab
                return _round3(slg - avg)

            def _calc_wt_woba(wt: dict) -> float:
                wt_pa = _calc_wt_pa(wt)
                if wt_pa <= 0:
                    return 0.0
                h   = wt.get("hits", 0.0)
                d2  = wt.get("doubles", 0.0)
                d3  = wt.get("triples", 0.0)
                hr  = wt.get("homeruns", 0.0)
                bb  = wt.get("walks", 0.0)
                hbp = wt.get("hit_by_pitch", 0.0)
                singles = h - d2 - d3 - hr
                woba = (
                    _WOBA_BB  * bb
                  + _WOBA_HBP * hbp
                  + _WOBA_1B  * singles
                  + _WOBA_2B  * d2
                  + _WOBA_3B  * d3
                  + _WOBA_HR  * hr
                ) / wt_pa
                return _round3(woba)

            # 加重合算値から各選手の指標を計算し result に格納
            for player_name, stats in aggregated.get("player_totals", {}).items():
                canonical_name = _canonical_player_name(player_name, team_code)
                # overall（シーズン成績） — hot でも prior は参照用として保持
                overall = (
                    SEASON_OVERALL_BATTING.get(canonical_name)
                    or SEASON_OVERALL_BATTING.get(player_name)
                    or SEASON_OVERALL_BATTING.get(_normalize_player_name(player_name))
                    or {}
                )
                prior_obp  = float(overall.get("obp",  NPB_LEAGUE_AVG_OBP) or NPB_LEAGUE_AVG_OBP)
                prior_iso  = float(overall.get("iso",  NPB_LEAGUE_AVG_ISO)  or NPB_LEAGUE_AVG_ISO)
                prior_woba = float(overall.get("woba", _LEAGUE_WOBA) or _LEAGUE_WOBA)

                wt = weighted_totals.get(canonical_name) or weighted_totals.get(player_name)
                if wt:
                    # 加重指標（最新試合を重視した評価値）
                    wt_pa    = _calc_wt_pa(wt)
                    wt_ab    = wt.get("at_bats", 0.0)
                    wt_obp   = _calc_wt_obp(wt)
                    wt_iso   = _calc_wt_iso(wt)
                    wt_woba  = _calc_wt_woba(wt)
                    wt_so    = wt.get("strikeouts", 0.0)
                    wt_con   = _round3(1.0 - wt_so / wt_pa) if wt_pa > 0 else 0.0
                    wt_g     = wt.get("games", 0.0)
                    wt_st    = wt.get("steals", 0.0)
                    _RUN_CAP = 0.5
                    wt_run   = _round3(min((wt_st / wt_g) / _RUN_CAP, 1.0)) if wt_g > 0 else 0.0

                    # hot モード: adj = 加重移動平均値（PA=0なら0）
                    adj_obp  = wt_obp  if wt_pa > 0 else 0.0
                    adj_iso  = wt_iso  if wt_ab > 0 else 0.0
                    adj_woba = wt_woba if wt_pa > 0 else 0.0
                    adj_con  = wt_con  if wt_pa > 0 else 0.0
                    adj_run  = wt_run  if wt_g  > 0 else 0.0
                    # 表示用の生値は flat合算から計算（UI互換性維持）
                    flat_pa  = _calc_recent_pa(stats)
                    flat_ab  = int(stats.get("at_bats", 0) or 0)
                    raw_obp  = _calc_recent_obp(stats)
                    raw_iso  = _calc_iso_from_stats(stats)
                    raw_woba = _calc_woba(stats, flat_pa)
                    flat_so  = int(stats.get("strikeouts", 0) or 0)
                    raw_con  = _round3(1.0 - flat_so / flat_pa) if flat_pa > 0 else 0.75
                    flat_g   = int(stats.get("games", 0) or 0)
                    flat_st  = int(stats.get("steals", 0) or 0)
                    raw_run  = _round3(min((flat_st / flat_g) / 0.5, 1.0)) if flat_g > 0 else 0.0
                    reliability = 1.0 if wt_pa > 0 else 0.0
                    pa  = flat_pa
                    ab  = flat_ab
                    g   = flat_g
                else:
                    # game_snapshots に出場記録がない選手は flat合算のフォールバック
                    pa  = _calc_recent_pa(stats)
                    ab  = int(stats.get("at_bats", 0) or 0)
                    g   = int(stats.get("games", 0) or 0)
                    raw_obp  = _calc_recent_obp(stats)
                    raw_iso  = _calc_iso_from_stats(stats)
                    raw_woba = _calc_woba(stats, pa)
                    so = int(stats.get("strikeouts", 0) or 0)
                    raw_con  = _round3(1.0 - so / pa) if pa > 0 else 0.75
                    st = int(stats.get("steals", 0) or 0)
                    raw_run  = _round3(min((st / g) / 0.5, 1.0)) if g > 0 else 0.0
                    adj_obp  = raw_obp  if pa > 0 else 0.0
                    adj_iso  = raw_iso  if ab > 0 else 0.0
                    adj_woba = raw_woba if pa > 0 else 0.0
                    adj_con  = raw_con  if pa > 0 else 0.0
                    adj_run  = raw_run  if g  > 0 else 0.0
                    reliability = 1.0 if pa > 0 else 0.0

                result[canonical_name] = {
                    "games":       int(stats.get("games", 0) or 0),
                    "pa":          pa,
                    "ab":          ab,
                    "obp":         raw_obp,
                    "iso":         raw_iso,
                    "woba":        raw_woba,
                    "con":         raw_con,
                    "run":         raw_run,
                    "adj_obp":     _round3(adj_obp),
                    "adj_iso":     _round3(adj_iso),
                    "adj_woba":    _round3(adj_woba),
                    "adj_con":     _round3(adj_con),
                    "adj_run":     _round3(adj_run),
                    "prior_obp":   _round3(prior_obp),
                    "prior_iso":   _round3(prior_iso),
                    "prior_woba":  _round3(prior_woba),
                    "reliability": _round3(reliability),
                    "mode":        mode,
                    "raw": stats,
                }
            return result

    # ── precision モード（または game_snapshots が空の場合のフォールバック）──
    for player_name, stats in aggregated.get("player_totals", {}).items():
        canonical_name = _canonical_player_name(player_name, team_code)
        pa  = _calc_recent_pa(stats)
        ab  = int(stats.get("at_bats", 0) or 0)
        raw_obp  = _calc_recent_obp(stats)
        raw_iso  = _calc_iso_from_stats(stats)
        raw_woba = _calc_woba(stats, pa)

        # ── CON（コンタクト率）: 1 - K/PA ──
        # 三振回避率。高いほど「当てる力」がある
        so = int(stats.get("strikeouts", 0) or 0)
        raw_con = _round3(1.0 - so / pa) if pa > 0 else 0.75  # pa=0 はリーグ平均相当

        # ── RUN（走力スコア）: steals / games の正規化値 ──
        # 直近 games 試合での盗塁ペース (0〜1 スケール)
        # 上限を 0.5盗塁/試合（約1試合おき）として正規化
        g = int(stats.get("games", 0) or 0)
        st = int(stats.get("steals", 0) or 0)
        _RUN_CAP = 0.5  # 1試合0.5盗塁ペースを上限（≒1.0に正規化）
        raw_run = _round3(min((st / g) / _RUN_CAP, 1.0)) if g > 0 else 0.0

        # ── ベイズ収縮: prior = 個人シーズン期待値 or リーグ平均 ──
        overall = (
            SEASON_OVERALL_BATTING.get(canonical_name)
            or SEASON_OVERALL_BATTING.get(player_name)
            or SEASON_OVERALL_BATTING.get(_normalize_player_name(player_name))
            or {}
        )
        prior_obp  = float(overall.get("obp",  NPB_LEAGUE_AVG_OBP) or NPB_LEAGUE_AVG_OBP)
        prior_iso  = float(overall.get("iso",  NPB_LEAGUE_AVG_ISO)  or NPB_LEAGUE_AVG_ISO)
        # wOBA prior: SEASON_OVERALL_BATTING にあればそれを使い、なければリーグ平均
        prior_woba = float(overall.get("woba", _LEAGUE_WOBA) or _LEAGUE_WOBA)
        # CON prior: シーズン成績なければ NPB 平均コンタクト率 0.77
        prior_con  = float(overall.get("con",  0.77) or 0.77)
        # RUN prior: 個人シーズン盗塁ペース（シーズン成績がなければ 0.05 ≒ 低速）
        prior_run  = float(overall.get("run",  0.05) or 0.05)

        # pa=0 でも prior が返るため 0 打席の選手も prior 値を持つ
        adj_obp  = (pa * raw_obp  + _obp_prior_pa  * prior_obp)  / (pa + _obp_prior_pa)
        adj_iso  = (ab * raw_iso  + _iso_prior_ab  * prior_iso)  / (ab + _iso_prior_ab)
        adj_woba = (pa * raw_woba + _woba_prior_pa * prior_woba) / (pa + _woba_prior_pa)
        adj_con  = (pa * raw_con  + _obp_prior_pa  * prior_con)  / (pa + _obp_prior_pa)
        adj_run  = (pa * raw_run  + _obp_prior_pa  * prior_run)  / (pa + _obp_prior_pa)

        # 信頼度 (Avail): 0.0（0打席）〜 1.0（PRIOR_PA打席以上で≒1）
        reliability = pa / (pa + _obp_prior_pa) if pa > 0 else 0.0

        result[canonical_name] = {
            "games":       int(stats.get("games", 0) or 0),
            "pa":          pa,
            "ab":          ab,
            "obp":         raw_obp,            # 表示用（生の観測値）
            "iso":         raw_iso,            # 表示用（生の観測値）
            "woba":        raw_woba,           # 表示用（生の観測値）
            "con":         raw_con,            # 表示用（生の観測値）
            "run":         raw_run,            # 表示用（生の観測値）
            "adj_obp":     _round3(adj_obp),   # スコア計算用
            "adj_iso":     _round3(adj_iso),   # スコア計算用
            "adj_woba":    _round3(adj_woba),  # スコア計算用
            "adj_con":     _round3(adj_con),   # スコア計算用
            "adj_run":     _round3(adj_run),   # スコア計算用
            "prior_obp":   _round3(prior_obp),
            "prior_iso":   _round3(prior_iso),
            "prior_woba":  _round3(prior_woba),
            "reliability": _round3(reliability),
            "mode":        mode,
            "raw": stats,
        }

    return result


def _get_prediction_candidate_names(now: datetime | None = None, team_code: str = "広島") -> list[str]:
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
    active_first_team = _get_active_first_team_position_players(now, team_code)
    candidates: list[str] = list(active_first_team)

    # ② 二軍候補: 50PA以上の全選手からスコア最上位1名を選出
    #    ただし一軍登録済み選手はスキップし、純粋な二軍選手のみを対象とする
    farm_maps = _build_farm_score_maps(team_code)
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

def _defense_value_for(name: str, position: str = "", defense_map: dict | None = None, team_code: str = "広島") -> float:
    canonical_name = _canonical_player_name(name, team_code)
    defense_map = defense_map or _get_player_defense(team_code)

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
    team_code: str = "広島",
    all_recent_vals: dict[str, dict] | None = None,
) -> tuple[float, dict, dict, float]:
    """打順スロット専用スコア計算（Excel設計書 Book1.xlsx 準拠）。

    weights キー:
        recent_obp / recent_iso / recent_woba / recent_con / recent_run
        defense / avail
    各指標を 0〜100 スケールに正規化してウエイト合計で算出。

    ソフト減点（soft_penalty）:
        チーム内パーセンタイル比較、またはしきい値比較によりスコア係数を掛ける。
        ハードカットは廃止し、all-or-nothing でなく連続的な減点とする。

    all_recent_vals: チーム全候補の recent_map 辞書（ソフト減点の分位計算に使用）。
    """
    canonical_name = _canonical_player_name(player_name, team_code)

    recent = recent_map.get(canonical_name, {}).copy() or recent_map.get(
        _normalize_player_name(player_name), {}
    ).copy() or {
        "games": 0, "pa": 0, "ab": 0,
        "obp": 0.0, "iso": 0.0, "woba": 0.0, "con": 0.75, "run": 0.0,
        "adj_obp": NPB_LEAGUE_AVG_OBP, "adj_iso": NPB_LEAGUE_AVG_ISO,
        "adj_woba": _LEAGUE_WOBA, "adj_con": 0.77, "adj_run": 0.05,
        "reliability": 0.0, "raw": {},
    }
    season_pos = _get_adjusted_position_batting(canonical_name, position, team_code)
    defense    = _defense_value_for(canonical_name, position, defense_map)

    # ── ベイズ収縮済み値を取り出す ──
    adj_obp_val  = float(recent.get("adj_obp",  recent.get("obp",  0.0)) or 0.0)
    adj_iso_val  = float(recent.get("adj_iso",  recent.get("iso",  0.0)) or 0.0)
    adj_woba_val = float(recent.get("adj_woba", recent.get("woba", _LEAGUE_WOBA)) or _LEAGUE_WOBA)
    adj_con_val  = float(recent.get("adj_con",  recent.get("con",  0.77)) or 0.77)
    adj_run_val  = float(recent.get("adj_run",  recent.get("run",  0.0))  or 0.0)
    raw_obp_val  = float(recent.get("obp", 0.0) or 0.0)
    pa           = int(recent.get("pa", 0) or 0)

    # Avail（出場可能性）= reliability（ベイズ収縮の信頼度 0〜1）
    avail = float(recent.get("reliability", pa / (pa + RECENT_OBP_PRIOR_PA)) or 0.0)

    # ── exclude_zero_pa: recent_pa=0 かつ season_pa も極少なら候補外 ──
    if slot_def.get("exclude_zero_pa") and pa == 0:
        s_pa = float(season_pos.get("pa", 0.0) or 0.0)
        if s_pa < 10:
            return float("-inf"), recent, season_pos, defense

    # ── OBP/wOBA=0.000 ペナルティ ──
    # 直近の生OBPが 0.000（ヒット・四球・死球なし）で実打席が > 0 の場合、
    # ベイズ prior による下駄を剥いで adj を 0 に強制する
    r_obp  = adj_obp_val  * 100
    r_iso  = adj_iso_val  * 100
    r_woba = adj_woba_val * 100
    r_con  = adj_con_val  * 100
    r_run  = adj_run_val  * 100   # 0〜1 スケール → ×100
    defv   = defense * 10         # 守備補正を同スケールに
    avail_v = avail * 100         # 0〜1 → ×100

    if raw_obp_val == 0.0 and pa > 0:
        r_obp  = 0.0
        r_woba = 0.0

    # ── hard_cut_woba_bottom_pct（4番向け：wOBA下位50%は候補外） ──
    hcut_pct = slot_def.get("hard_cut_woba_bottom_pct")
    if hcut_pct is not None and all_recent_vals:
        woba_vals = sorted(
            [float(v.get("adj_woba", v.get("woba", 0.0)) or 0.0)
             for v in all_recent_vals.values()],
            reverse=True,
        )
        n = len(woba_vals)
        if n > 0:
            cut_idx = int(n * (1.0 - hcut_pct))  # 上位 (1-pct)*n 番目のしきい値
            if cut_idx >= n:
                cut_idx = n - 1
            threshold = woba_vals[cut_idx]
            if adj_woba_val < threshold:
                return float("-inf"), recent, season_pos, defense

    # ── hard_cut_iso_zero（4番向け：生のISO=0.000かつPA>=5の選手は候補外） ──
    # ベイズ収縮によりadj_isoは0にならないため、生の値(raw_iso)でチェックする
    if slot_def.get("hard_cut_iso_zero"):
        raw_pa = int(recent.get("pa", 0) or 0)
        raw_iso_val = float(recent.get("iso", 0.0) or 0.0)
        if raw_pa >= 5 and raw_iso_val == 0.0:
            return float("-inf"), recent, season_pos, defense

    # ── ウエイト加算スコア ──
    weights = slot_def.get("weights", {})
    score = (
        float(weights.get("recent_obp",  0.0) or 0.0) * r_obp
      + float(weights.get("recent_iso",  0.0) or 0.0) * r_iso
      + float(weights.get("recent_woba", 0.0) or 0.0) * r_woba
      + float(weights.get("recent_con",  0.0) or 0.0) * r_con
      + float(weights.get("recent_run",  0.0) or 0.0) * r_run
      + float(weights.get("defense",     0.0) or 0.0) * defv
      + float(weights.get("avail",       0.0) or 0.0) * avail_v
    )

    # ── ソフト減点（soft_penalty）──
    # チーム内パーセンタイル比較またはしきい値比較でスコアに係数を掛ける
    sp = slot_def.get("soft_penalty")
    if sp and all_recent_vals:
        penalty_multiplier = 1.0

        # OBP 下位 X% での減点
        if "obp_bottom_pct" in sp:
            obp_vals = sorted(
                [float(v.get("adj_obp", v.get("obp", 0.0)) or 0.0)
                 for v in all_recent_vals.values()],
            )
            thresh_idx = int(len(obp_vals) * sp["obp_bottom_pct"])
            if thresh_idx >= len(obp_vals):
                thresh_idx = len(obp_vals) - 1
            if adj_obp_val < obp_vals[thresh_idx]:
                penalty_multiplier *= sp.get("penalty", 0.85)

        # CON 下位 X% での減点（単独 penalty キー）
        if "con_bottom_pct" in sp and "con_penalty" not in sp:
            con_vals = sorted(
                [float(v.get("adj_con", v.get("con", 0.77)) or 0.77)
                 for v in all_recent_vals.values()],
            )
            thresh_idx = int(len(con_vals) * sp["con_bottom_pct"])
            if thresh_idx >= len(con_vals):
                thresh_idx = len(con_vals) - 1
            if adj_con_val < con_vals[thresh_idx]:
                penalty_multiplier *= sp.get("penalty", 0.93)

        # CON 下位 X% での減点（con_penalty キー：複数減点が共存する場合）
        if "con_bottom_pct" in sp and "con_penalty" in sp:
            con_vals = sorted(
                [float(v.get("adj_con", v.get("con", 0.77)) or 0.77)
                 for v in all_recent_vals.values()],
            )
            thresh_idx = int(len(con_vals) * sp["con_bottom_pct"])
            if thresh_idx >= len(con_vals):
                thresh_idx = len(con_vals) - 1
            if adj_con_val < con_vals[thresh_idx]:
                penalty_multiplier *= sp["con_penalty"]

        # OBP 絶対値しきい値での減点（obp_abs_threshold）
        if "obp_abs_threshold" in sp:
            if adj_obp_val < sp["obp_abs_threshold"]:
                penalty_multiplier *= sp.get("obp_penalty", 0.88)

        # ISO しきい値でのソフト減点（4番/5番向け）
        if "iso_threshold" in sp:
            if adj_iso_val < sp["iso_threshold"]:
                penalty_multiplier *= sp.get("penalty", 0.90)

        # wOBA チーム中央値未満での減点（3番向け）
        if sp.get("woba_below_median"):
            woba_vals = sorted(
                [float(v.get("adj_woba", v.get("woba", 0.0)) or 0.0)
                 for v in all_recent_vals.values()],
            )
            if len(woba_vals) > 0:
                median_idx = len(woba_vals) // 2
                median_woba = woba_vals[median_idx]
                if adj_woba_val < median_woba:
                    penalty_multiplier *= sp.get("penalty", 0.88)

        # wOBA 下位 X% での減点（7番向け）
        if "woba_bottom_pct" in sp:
            woba_vals = sorted(
                [float(v.get("adj_woba", v.get("woba", 0.0)) or 0.0)
                 for v in all_recent_vals.values()],
            )
            thresh_idx = int(len(woba_vals) * sp["woba_bottom_pct"])
            if thresh_idx >= len(woba_vals):
                thresh_idx = len(woba_vals) - 1
            if adj_woba_val < woba_vals[thresh_idx]:
                penalty_multiplier *= sp.get("penalty", 0.85)

        # recent_pa=0 でのゼロPA減点（6番向け）
        if "zero_pa_penalty" in sp and pa == 0:
            penalty_multiplier *= sp["zero_pa_penalty"]

        # avail が低いときの減点（8番向け）
        if "low_avail_threshold" in sp:
            if avail < sp["low_avail_threshold"]:
                penalty_multiplier *= sp.get("penalty", 0.85)

        # RUN 下位 X% かつ OBP 下位 Y% の複合減点（9番向け）
        if "run_bottom_pct" in sp and "obp_bottom_pct_and" in sp:
            run_vals = sorted(
                [float(v.get("adj_run", v.get("run", 0.0)) or 0.0)
                 for v in all_recent_vals.values()],
            )
            obp_vals2 = sorted(
                [float(v.get("adj_obp", v.get("obp", 0.0)) or 0.0)
                 for v in all_recent_vals.values()],
            )
            r_thresh = int(len(run_vals) * sp["run_bottom_pct"])
            if r_thresh >= len(run_vals):
                r_thresh = len(run_vals) - 1
            o_thresh = int(len(obp_vals2) * sp["obp_bottom_pct_and"])
            if o_thresh >= len(obp_vals2):
                o_thresh = len(obp_vals2) - 1
            if adj_run_val < run_vals[r_thresh] and adj_obp_val < obp_vals2[o_thresh]:
                penalty_multiplier *= sp.get("penalty", 0.85)

        score *= penalty_multiplier

    return score, recent, season_pos, defense



def _ordinal_ja(n: int) -> str:
    """1→'1位', 2→'2位' ... """
    return f"{n}位"


def _build_ranks(all_stats: list[dict]) -> dict[str, dict[str, int]]:
    """全候補選手の各指標ランキングを事前計算。
    返り値: {player_name: {metric_key: rank}}
    """
    metrics = ["recent_obp", "recent_iso", "recent_woba", "recent_con", "recent_run", "season_obp", "season_iso", "defense"]
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
    mode: str = "precision",
) -> str:
    """指標の順位を交えた日本語の根拠文を生成する。"""
    r_obp  = recent.get("obp",  0.0)
    r_iso  = recent.get("iso",  0.0)
    r_woba = recent.get("woba", 0.0)
    r_con  = recent.get("con",  0.75)
    r_run  = recent.get("run",  0.0)

    # season モードは「通算」、それ以外は「直近N試合」
    _pfx = "通算" if mode == "season" else f"直近{window_games}試合"

    player_ranks = ranks.get(player_name, {})

    def rank_tag(metric: str) -> str:
        r = player_ranks.get(metric)
        if r and r <= 3:
            return f"（候補中{_ordinal_ja(r)}）"
        return ""

    r_obp_tag  = rank_tag("recent_obp")
    r_iso_tag  = rank_tag("recent_iso")
    r_woba_tag = rank_tag("recent_woba")
    r_con_tag  = rank_tag("recent_con")
    r_run_tag  = rank_tag("recent_run")
    def_tag    = rank_tag("defense")

    # 打順役割ごとに強調する指標を変える（Excel設計書の役割定義に準拠）
    if role in ("leadoff_obp",):
        # 1番：出塁起点（OBP最重視＋RUN/CON）
        parts = [
            f"{_pfx} 出塁率 {r_obp:.3f}{r_obp_tag}",
            f"wOBA {r_woba:.3f}{r_woba_tag}",
            f"走力 {r_run:.3f}{r_run_tag}",
            f"コンタクト率 {r_con:.3f}{r_con_tag}",
        ]
    elif role in ("strong_connector",):
        # 2番：強打の接着剤（wOBA最重視＋OBP/CON）
        parts = [
            f"{_pfx} wOBA {r_woba:.3f}{r_woba_tag}",
            f"出塁率 {r_obp:.3f}{r_obp_tag}",
            f"コンタクト率 {r_con:.3f}{r_con_tag}",
            f"長打指数 {r_iso:.3f}{r_iso_tag}",
        ]
    elif role in ("versatile_upper",):
        # 3番：万能上位（wOBA最重視＋全指標バランス）
        parts = [
            f"{_pfx} wOBA {r_woba:.3f}{r_woba_tag}",
            f"出塁率 {r_obp:.3f}{r_obp_tag}",
            f"長打指数 {r_iso:.3f}{r_iso_tag}",
            f"コンタクト率 {r_con:.3f}{r_con_tag}",
        ]
    elif role in ("cleanup_power",):
        # 4番：主砲（wOBA最重視＋ISO）
        parts = [
            f"{_pfx} wOBA {r_woba:.3f}{r_woba_tag}",
            f"長打指数 {r_iso:.3f}{r_iso_tag}",
            f"出塁率 {r_obp:.3f}{r_obp_tag}",
        ]
    elif role in ("second_slugger",):
        # 5番：返す2枚目（wOBA＋ISO長打継続）
        parts = [
            f"{_pfx} wOBA {r_woba:.3f}{r_woba_tag}",
            f"長打指数 {r_iso:.3f}{r_iso_tag}",
            f"出塁率 {r_obp:.3f}{r_obp_tag}",
            f"コンタクト率 {r_con:.3f}{r_con_tag}",
        ]
    elif role in ("bridge_lower",):
        # 6番：中軸下の橋（wOBA/OBP/守備バランス）
        parts = [
            f"{_pfx} wOBA {r_woba:.3f}{r_woba_tag}",
            f"出塁率 {r_obp:.3f}{r_obp_tag}",
            f"長打指数 {r_iso:.3f}{r_iso_tag}",
            f"守備補正 {defense:+.3f}{def_tag}",
        ]
    elif role in ("glove_core",):
        # 7番：守備込み下位中核（守備＋wOBA）
        parts = [
            f"守備補正 {defense:+.3f}{def_tag}",
            f"{_pfx} wOBA {r_woba:.3f}{r_woba_tag}",
            f"出塁率 {r_obp:.3f}{r_obp_tag}",
            f"コンタクト率 {r_con:.3f}{r_con_tag}",
        ]
    elif role in ("glove_bottom",):
        # 8番：守備型下位（守備最重視）
        parts = [
            f"守備補正 {defense:+.3f}{def_tag}",
            f"{_pfx} wOBA {r_woba:.3f}{r_woba_tag}",
            f"出塁率 {r_obp:.3f}{r_obp_tag}",
            f"コンタクト率 {r_con:.3f}{r_con_tag}",
        ]
    elif role in ("pre_pitcher",):
        # 8番（DH無）：投手前の出塁役（OBP/CON重視）
        parts = [
            f"{_pfx} 出塁率 {r_obp:.3f}{r_obp_tag}",
            f"コンタクト率 {r_con:.3f}{r_con_tag}",
            f"守備補正 {defense:+.3f}{def_tag}",
            f"wOBA {r_woba:.3f}{r_woba_tag}",
        ]
    elif role in ("second_leadoff",):
        # 9番（DH有）：第2の1番（OBP＋RUN/CON）
        parts = [
            f"{_pfx} 出塁率 {r_obp:.3f}{r_obp_tag}",
            f"走力 {r_run:.3f}{r_run_tag}",
            f"コンタクト率 {r_con:.3f}{r_con_tag}",
            f"wOBA {r_woba:.3f}{r_woba_tag}",
        ]
    else:
        parts = [
            f"{_pfx} wOBA {r_woba:.3f}{r_woba_tag}",
            f"出塁率 {r_obp:.3f}{r_obp_tag}",
            f"長打指数 {r_iso:.3f}{r_iso_tag}",
        ]

    return "、".join(parts)


# ── 打順役割ラベル（日本語）──────────────────────────────────────────────
_ROLE_LABEL_JA: dict[str, str] = {
    "leadoff_obp":       "1番（出塁起点型）",
    "strong_connector":  "2番（強打接着型）",
    "versatile_upper":   "3番（万能上位型）",
    "cleanup_power":     "4番（主砲型）",
    "second_slugger":    "5番（長打継続型）",
    "bridge_lower":      "6番（中軸下橋渡型）",
    "glove_core":        "7番（守備込み中核型）",
    "glove_bottom":      "8番（守備型）",
    "pre_pitcher":       "8番（投手前出塁型）",
    "second_leadoff":    "9番（第2の1番型）",
    # 旧ロール名（後方互換）
    "lead_obp_glove":      "1番（出塁＋守備型）",
    "two_hole_bat":        "2番（バランス型）",
    "three_hole_contact":  "3番（巧打型）",
    "five_hole_power":     "5番（長打補完型）",
    "six_hole_balance":    "6番（総合バランス型）",
    "seven_hole_season":   "7番（シーズン実績型）",
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
    mode: str = "precision",
) -> str:
    """打順決定の論理的な解説文（2〜3文）を生成する。"""
    # season モードは「通算」、それ以外は「直近N試合」
    _pfx = "通算" if mode == "season" else f"直近{window_games}試合"

    # 生の観測値（表示用）
    r_obp  = recent.get("obp",  0.0)
    r_iso  = recent.get("iso",  0.0)
    r_woba = recent.get("woba", 0.0)
    r_con  = recent.get("con",  0.75)
    r_run  = recent.get("run",  0.0)
    # ベイズ補正済み値（スコア計算に使った値）
    adj_obp  = recent.get("adj_obp",  r_obp)
    adj_iso  = recent.get("adj_iso",  r_iso)
    adj_woba = recent.get("adj_woba", r_woba)
    adj_con  = recent.get("adj_con",  r_con)
    s_obp = float(season_pos.get("obp", 0.0) or 0.0)

    pa           = int(recent.get("pa", 0) or 0)
    reliability  = float(recent.get("reliability", 1.0) or 1.0)
    prior_obp    = float(recent.get("prior_obp",  s_obp or NPB_LEAGUE_AVG_OBP) or NPB_LEAGUE_AVG_OBP)
    prior_woba   = float(recent.get("prior_woba", _LEAGUE_WOBA) or _LEAGUE_WOBA)

    # ── 打席数に応じた信頼度注記（season モードは不要） ──
    def _reliability_note() -> str:
        if mode == "season":
            return ""  # 通算成績は常に信頼度MAX
        if pa == 0:
            return (
                f"（注: {_pfx}の打席データなし。"
                f"シーズン期待値 wOBA={prior_woba:.3f} を基準として評価している）"
            )
        if reliability < 0.40:
            return (
                f"（注: {_pfx}は{pa}打席と少ないため、"
                f"直近 wOBA {r_woba:.3f} をシーズン期待値 {prior_woba:.3f} 方向へ補正し"
                f" adj_woba={adj_woba:.3f} として評価している）"
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

    # ── role別に解説文テンプレートを分岐（Excel設計書準拠）──
    if role == "leadoff_obp":
        # 1番：出塁起点（OBP最重視＋RUN/CON）
        sent1 = (
            f"1番打者はOBP（35%）を最重視し、走力（RUN 15%）とコンタクト率（CON 10%）も評価する設計だ。"
            f"wOBA（20%）で出塁の質も加味する。"
        )
        sent2 = (
            f"この選手は{_pfx}の出塁率 {r_obp:.3f}（{rank_str('recent_obp')}）"
            f"、wOBA {r_woba:.3f}（{rank_str('recent_woba')}）"
            f"、コンタクト率 {r_con:.3f}（{rank_str('recent_con')}）の組み合わせで総合スコア {score:.1f} が候補中最高となった。"
        )
        return sent1 + sent2 + _reliability_note()

    elif role == "strong_connector":
        # 2番：強打の接着剤（wOBA最重視＋OBP/CON）
        sent1 = (
            f"2番打者はwOBA（35%）で打撃の総合的な質を最重視し、OBP（20%）・CON（15%）も評価する設計だ。"
            f"1番走者を進めながら自身も長打を狙える「強打の接着剤」を選ぶ。"
        )
        sent2 = (
            f"{_pfx}の wOBA {r_woba:.3f}（{rank_str('recent_woba')}）"
            f"、出塁率 {r_obp:.3f}（{rank_str('recent_obp')}）"
            f"、コンタクト率 {r_con:.3f}（{rank_str('recent_con')}）を評価しスコア {score:.1f} が候補中最高と判定した。"
        )
        return sent1 + sent2 + _reliability_note()

    elif role == "versatile_upper":
        # 3番：万能上位（wOBA最重視・全指標バランス）
        sent1 = (
            f"3番打者はwOBA（35%）を主軸に、OBP（15%）・ISO（15%）・CON（10%）をバランスよく評価する設計だ。"
            f"出塁力・長打力・接触力を備えた「万能型」がこのスロットの理想像だ。"
        )
        if adj_woba < 0.310:
            sent2 = (
                f"この選手のwOBAはチーム中央値をやや下回るが（adj_woba={adj_woba:.3f}）、"
                f"残り候補との比較でスコア {score:.1f} が最高となり3番に配置した。"
            )
        else:
            sent2 = (
                f"{_pfx} wOBA {r_woba:.3f}（{rank_str('recent_woba')}）が示す総合打撃力と、"
                f"出塁率 {r_obp:.3f}・長打指数 {r_iso:.3f}・コンタクト率 {r_con:.3f} が高水準で、"
                f"スコア {score:.1f} が候補中最高となった。"
            )
        return sent1 + sent2 + _reliability_note()

    elif role == "cleanup_power":
        # 4番：主砲（wOBA最重視＋ISO）
        sent1 = (
            f"4番スコアはwOBA（45%）を最重視し、ISO（25%）で長打力を評価する設計だ。"
            f"OBP系は15%に抑え、長打・一発の得点力を最優先にしている。"
        )
        if r_iso < 0.110:
            sent2 = (
                f"{_pfx}長打指数は {r_iso:.3f}（{rank_str('recent_iso')}）とやや低調だが、"
                f"wOBA {r_woba:.3f}（{rank_str('recent_woba')}）を含めたスコア {score:.1f} が残り候補の中で最高となった。"
            )
        else:
            sent2 = (
                f"{_pfx} wOBA {r_woba:.3f}（{rank_str('recent_woba')}）が総合打撃力の高さを示し、"
                f"長打指数 {r_iso:.3f}（{rank_str('recent_iso')}）も加えたスコア {score:.1f} が候補中最高となった。"
            )
        if r_obp == 0.0 and pa > 0:
            sent3 = (
                f"ただし直近出塁率 {r_obp:.3f}（ヒット・四球・死球なし）のペナルティが適用されている点は留意が必要だ。"
            )
        else:
            sent3 = (
                f"出塁率 {r_obp:.3f} も一定水準を保っており、長打力とのバランスがとれた選出となった。"
            )
        return sent1 + sent2 + sent3 + _reliability_note()

    elif role == "second_slugger":
        # 5番：返す2枚目（wOBA＋ISO）
        if r_iso < 0.095:
            sent1 = (
                f"{_pfx}の長打指数は {r_iso:.3f} と低調で、"
                f"本来5番に求める長打力の観点では候補中で恵まれた数値ではない。"
            )
            sent2 = (
                f"5番スコアはwOBA（35%）・ISO（25%）を中心に評価する設計で、"
                f"wOBA {r_woba:.3f}（{rank_str('recent_woba')}）を含めたスコア {score:.1f} が残り候補の中で相対的に最高となり繰り上がり選出となった。"
            )
        else:
            sent1 = (
                f"{_pfx}の wOBA {r_woba:.3f}（{rank_str('recent_woba')}）が示す総合打撃力と、"
                f"長打指数 {r_iso:.3f}（{rank_str('recent_iso')}）が4番に次ぐ水準にある。"
            )
            sent2 = (
                f"5番スコアはwOBA（35%）・ISO（25%）を中心とする設計で、"
                f"コンタクト率 {r_con:.3f}（{rank_str('recent_con')}）も加味したスコア {score:.1f} が候補中最高となり、中軸5番として選出した。"
            )
        return sent1 + sent2 + _reliability_note()

    elif role == "bridge_lower":
        # 6番：中軸下の橋（wOBA/OBP/守備バランス）
        sent1 = (
            f"6番スコアはwOBA（25%）・OBP（20%）・守備（15%）・ISO（15%）・CON（10%）のバランス設計だ。"
            f"中軸4・5番の残塁を返しつつ下位打線の起点にもなれる選手を選ぶ。"
        )
        sent2 = (
            f"{_pfx} wOBA {r_woba:.3f}（{rank_str('recent_woba')}）・出塁率 {r_obp:.3f}（{rank_str('recent_obp')}）"
            f"・守備補正 {defense:+.3f}（{rank_str('defense')}）の組み合わせでスコア {score:.1f} が候補中最高となった。"
        )
        return sent1 + sent2 + _reliability_note()

    elif role == "glove_core":
        # 7番：守備込み下位中核（守備＋wOBA）
        sent1 = (
            f"7番スコアは守備（25%）とwOBA（25%）を同比重で評価し、OBP（15%）・CON（10%）も加算する設計だ。"
        )
        sent2 = (
            f"守備補正 {defense:+.3f}（{rank_str('defense')}）と{_pfx} wOBA {r_woba:.3f}（{rank_str('recent_woba')}）の合算スコア {score:.1f} が候補中最高となり、下位打線の安定役として選出した。"
        )
        return sent1 + sent2 + _reliability_note()

    elif role == "glove_bottom":
        # 8番：守備型下位（守備最重視）
        sent1 = (
            f"8番スコアは守備（35%）・Avail（20%）を重視し、OBP（15%）・wOBA（15%）・CON（10%）が補完する設計だ。"
        )
        if defense > 0:
            sent2 = (
                f"守備補正 {defense:+.3f}（{rank_str('defense')}）が35%のウエイトで効き、"
                f"{_pfx} wOBA {r_woba:.3f}（{rank_str('recent_woba')}）などの打撃系指標が残り45%を補完し"
                f"スコア {score:.1f} が候補中最高となった。"
            )
        else:
            sent2 = (
                f"守備補正 {defense:+.3f} は中立/マイナスだが残り候補の中では相対的に高く、"
                f"{_pfx} wOBA {r_woba:.3f} など打撃系指標も加算したスコア {score:.1f} が候補中最高となった。"
            )
        return sent1 + sent2 + _reliability_note()

    elif role == "pre_pitcher":
        # 8番（DH無）：投手前の出塁役（OBP/CON重視）
        sent1 = (
            f"8番（DH無）は9番が投手のため、打線サイクルの起点として出塁率（OBP 28%）・"
            f"コンタクト率（CON 18%）・守備（DEF 20%）を重視する設計だ。"
        )
        sent2 = (
            f"{_pfx}出塁率 {r_obp:.3f}（{rank_str('recent_obp')}）・コンタクト率 {r_con:.3f}（{rank_str('recent_con')}）"
            f"・守備補正 {defense:+.3f}（{rank_str('defense')}）の組み合わせでスコア {score:.1f} が候補中最高となった。"
        )
        return sent1 + sent2 + _reliability_note()

    elif role == "second_leadoff":
        # 9番（DH有）：第2の1番（OBP＋RUN/CON）
        sent1 = (
            f"9番（DH有）は「第2の1番」として出塁率（OBP 30%）・走力（RUN 20%）・コンタクト率（CON 15%）を重視する設計だ。"
            f"イニング先頭で出塁し上位打線に繋げるのが役割だ。"
        )
        sent2 = (
            f"{_pfx}出塁率 {r_obp:.3f}（{rank_str('recent_obp')}）・走力 {r_run:.3f}（{rank_str('recent_run')}）"
            f"・wOBA {r_woba:.3f}（{rank_str('recent_woba')}）を評価したスコア {score:.1f} が候補中最高となった。"
        )
        return sent1 + sent2 + _reliability_note()

    else:
        # fallback
        return (
            f"{_pfx}の wOBA {r_woba:.3f}・出塁率 {r_obp:.3f}・長打指数 {r_iso:.3f} の総合評価により、"
            f"このスロットへの割り当てスコア {score:.1f} が候補中最高となったため選出した。"
        ) + _reliability_note()


def _build_simple_predicted_lineup(
    window_games: int,
    use_dh: bool,
    team_code: str = "広島",
    mode: str = "precision",
) -> dict:
    cache_bucket = _cache_get_bucket("predicted_lineup")
    cache_key = f"w{window_games}:dh{int(use_dh)}:{team_code}:{mode}"
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
                _do_build_predicted_lineup(window_games, use_dh, cache_bucket, cache_key, team_code, mode=mode)
            except Exception as e:
                print("DEBUG_PREDICTED_LINEUP_BG_ERROR", str(e))
        cache_bucket[cache_key] = {**cache_entry, "expires_at": _cache_now() + 60}
        threading.Thread(target=_bg_rebuild, daemon=True, name=f"bg-lineup-{cache_key}").start()
        return stale

    return _do_build_predicted_lineup(window_games, use_dh, cache_bucket, cache_key, team_code, mode=mode)


def _do_build_predicted_lineup(
    window_games: int,
    use_dh: bool,
    cache_bucket: dict,
    cache_key: str,
    team_code: str = "広島",
    mode: str = "precision",
) -> dict:
    slot_defs = DH_LINEUP_SLOTS if use_dh else NO_DH_LINEUP_SLOTS
    recent_map      = _recent_snapshot_map(window_games, team_code, mode=mode)
    defense_map     = _get_player_defense(team_code)
    candidate_names = _get_prediction_candidate_names(team_code=team_code)

    # ── 全候補の指標を事前集計してランキングを作る ──
    all_stats_for_rank: list[dict] = []
    for player_name in candidate_names:
        cname    = _canonical_player_name(player_name, team_code)
        recent_r = recent_map.get(cname, {}).copy() or recent_map.get(
            _normalize_player_name(player_name), {}
        ).copy() or {
            "obp": 0.0, "iso": 0.0, "woba": 0.0,
            "adj_obp": NPB_LEAGUE_AVG_OBP, "adj_iso": NPB_LEAGUE_AVG_ISO,
            "adj_woba": _LEAGUE_WOBA,
        }
        def_val  = _defense_value_for(cname, "", defense_map)
        eligible = (_get_player_profile(team_code).get(cname) or {}).get("eligible_positions", [])
        best_s_obp, best_s_iso = 0.0, 0.0
        for pos in eligible:
            sp = _get_adjusted_position_batting(cname, pos, team_code)
            best_s_obp = max(best_s_obp, float(sp.get("obp", 0.0) or 0.0))
            best_s_iso = max(best_s_iso, float(sp.get("iso", 0.0) or 0.0))
        all_stats_for_rank.append({
            "name":        cname,
            # ランキングもベイズ補正済み値で評価（打席数の少なさを反映）
            "recent_obp":  float(recent_r.get("adj_obp",  recent_r.get("obp",  0.0)) or 0.0),
            "recent_iso":  float(recent_r.get("adj_iso",  recent_r.get("iso",  0.0)) or 0.0),
            "recent_woba": float(recent_r.get("adj_woba", recent_r.get("woba", 0.0)) or 0.0),
            "recent_con":  float(recent_r.get("adj_con",  recent_r.get("con",  0.77)) or 0.77),
            "recent_run":  float(recent_r.get("adj_run",  recent_r.get("run",  0.0))  or 0.0),
            "season_obp":  best_s_obp,
            "season_iso":  best_s_iso,
            "defense":     def_val,
        })
    ranks = _build_ranks(all_stats_for_rank)

    # ── 全スロット × 全候補 × 全ポジションでスコアを計算し
    #    1番から順にグリーディに最高スコアの選手を割り当て ──
    used_players: set[str]   = set()
    used_positions: set[str] = set()   # 同一守備位置の重複を防ぐ
    lineup: list[dict]       = []

    # ── v1 スロット選出ヘルパー ──────────────────────────────────────────────
    def _pick_best_for_slot(
        slot: dict,
        names: list[str],
        pa_floor: int = 0,          # 0=制限なし, 1=直近打席1以上のみ
    ) -> dict | None:
        """names を走査し slot に最適な選手を返す。
        pa_floor > 0 のとき recent.pa < pa_floor の選手はスキップする。
        """
        pick = None
        for pname in names:
            cname = _canonical_player_name(pname, team_code)
            if cname in used_players:
                continue
            # PA フロア（直近打席数フィルタ）
            if pa_floor > 0:
                r_pa = int((recent_map.get(cname) or {}).get("pa", 0) or 0)
                if r_pa < pa_floor:
                    continue
            eligible_positions = (
                (_get_player_profile(team_code).get(cname) or {}).get("eligible_positions", [])
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
                score, recent_s, season_pos_s, defense_s = _slot_score(
                    cname, position, slot, recent_map, defense_map, team_code,
                    all_recent_vals=recent_map,
                )
                if math.isinf(score) and score < 0:
                    continue
                if best_pos_score is None or score > best_pos_score:
                    best_pos_score  = score
                    best_pos        = position
                    best_recent     = recent_s
                    best_season_pos = season_pos_s
                    best_defense    = defense_s
            if best_pos_score is None:
                continue
            if pick is None or best_pos_score > pick["score"]:
                pick = {
                    "order":       int(slot.get("order", 0) or 0),
                    "position":    best_pos,
                    "player_name": cname,
                    "score":       round(best_pos_score, 3),
                    "recent":      best_recent,
                    "season_pos":  best_season_pos,
                    "defense":     round(best_defense, 3),
                    "role":        slot.get("role", ""),
                }
        return pick
    # ──────────────────────────────────────────────────────────────────────────

    for slot_def in sorted(slot_defs, key=lambda s: s["order"]):

        # ── パス1: PA>=1 の選手のみで最適候補を選出 ──
        best_pick = _pick_best_for_slot(slot_def, candidate_names, pa_floor=1)

        # ── パス2: PA>=1 で誰も取れなかった場合のみ PA=0 も含む全候補で再試行 ──
        if best_pick is None:
            best_pick = _pick_best_for_slot(slot_def, candidate_names, pa_floor=0)

        # ── フォールバック：hard_cut 系制約で全候補が弾かれた場合 ──
        # hard_cut_woba_bottom_pct / hard_cut_iso_zero の両制約を外して再選出する
        if best_pick is None and (
            slot_def.get("hard_cut_woba_bottom_pct") is not None
            or slot_def.get("hard_cut_iso_zero")
        ):
            fallback_slot = {
                k: v for k, v in slot_def.items()
                if k not in ("hard_cut_woba_bottom_pct", "hard_cut_iso_zero")
            }
            # フォールバックもまず PA>=1 → なければ PA=0 の順で試みる
            best_pick = _pick_best_for_slot(fallback_slot, candidate_names, pa_floor=1)
            if best_pick is None:
                best_pick = _pick_best_for_slot(fallback_slot, candidate_names, pa_floor=0)

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
            mode=mode,
        )
        commentary = _build_commentary(
            best_pick["player_name"], position, best_pick["role"],
            recent, season_pos, best_pick["defense"], ranks, window_games,
            best_pick["score"],
            mode=mode,
        )
        lineup.append({
            "order":    best_pick["order"],
            "position": position,
            "player_name": best_pick["player_name"],
            "score":    best_pick["score"],
            "reason":   reason,
            "commentary": commentary,
            "recent": {
                "games": recent.get("games", 0), "pa": recent.get("pa", 0),
                "ab":    recent.get("ab",    0),  "obp": recent.get("obp", 0.0),
                "iso":   recent.get("iso",  0.0), "woba": recent.get("woba", 0.0),
                "con":   recent.get("con",  0.75), "run": recent.get("run", 0.0),
                "adj_con":  recent.get("adj_con",  0.77),
                "adj_run":  recent.get("adj_run",  0.0),
                "adj_woba": recent.get("adj_woba", _LEAGUE_WOBA),
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
        "mode":         mode,
        "generated_at": _now_jst().isoformat(),
        "lineup":       lineup,
    }

    cache_bucket[cache_key] = {
        "value":      result,
        "expires_at": _cache_now() + CACHE_TTL_PREDICTED_LINEUP,
    }
    return result


# ════════════════════════════════════════════════════════════════════
# v2: wOBAランク＋配置ルール方式（並列比較用）
# ════════════════════════════════════════════════════════════════════

# ── v2 配置ルール定義 ──────────────────────────────────────────────
# 各スロットを「どの指標で選ぶか」をシンプルなルールで記述する。
# primary: メイン選出指標（adj_woba / adj_obp / adj_iso / adj_con）
# pool_top_pct: adj_woba上位N%のみを候補とする（None=全員）
# tiebreak: primaryが同値のときのサブ指標
# def_bonus: Trueのとき守備補正をスコアに加算（7・8番向け）
# no_iso_zero: TrueかつPA>=5のとき raw_iso=0 の選手を除外（4番向け）
# role: _build_reason/_build_commentary に渡すロール名
V2_DH_PLACEMENT_RULES: list[dict] = [
    # 4番: wOBA上位50%の中でadj_isoが最大（ISO=0除外）
    {"order": 4, "role": "cleanup_power",
     "primary": "adj_iso",   "pool_top_pct": 0.50,
     "tiebreak": "adj_woba", "no_iso_zero": True,  "def_bonus": False},
    # 3番: 残りからwOBA最大（万能型）
    {"order": 3, "role": "versatile_upper",
     "primary": "adj_woba",  "pool_top_pct": None,
     "tiebreak": "adj_obp",  "no_iso_zero": False, "def_bonus": False},
    # 5番: 残りからwOBA最大（2枚目の長打力）
    {"order": 5, "role": "second_slugger",
     "primary": "adj_woba",  "pool_top_pct": None,
     "tiebreak": "adj_iso",  "no_iso_zero": False, "def_bonus": False},
    # 2番: wOBA上位60%の中でadj_conが最大
    {"order": 2, "role": "strong_connector",
     "primary": "adj_con",   "pool_top_pct": 0.60,
     "tiebreak": "adj_woba", "no_iso_zero": False, "def_bonus": False},
    # 1番: wOBA上位60%の中でadj_obp最大（走力タイブレーク）
    {"order": 1, "role": "leadoff_obp",
     "primary": "adj_obp",   "pool_top_pct": 0.60,
     "tiebreak": "adj_run",  "no_iso_zero": False, "def_bonus": False},
    # 6番: 残りからwOBA最大
    {"order": 6, "role": "bridge_lower",
     "primary": "adj_woba",  "pool_top_pct": None,
     "tiebreak": "adj_obp",  "no_iso_zero": False, "def_bonus": False},
    # 7番: 残りから（守備補正込みで）最大
    {"order": 7, "role": "glove_core",
     "primary": "adj_woba",  "pool_top_pct": None,
     "tiebreak": "defense",  "no_iso_zero": False, "def_bonus": True},
    # 8番: 残りから（守備補正込みで）最大
    {"order": 8, "role": "glove_bottom",
     "primary": "adj_woba",  "pool_top_pct": None,
     "tiebreak": "defense",  "no_iso_zero": False, "def_bonus": True},
    # 9番(DH有): 残りからOBP最大（第2の1番）
    {"order": 9, "role": "second_leadoff",
     "primary": "adj_obp",   "pool_top_pct": None,
     "tiebreak": "adj_run",  "no_iso_zero": False, "def_bonus": False},
]

# DH無し（セ・リーグ等）: 9番=投手固定のため8番まで
V2_NODH_PLACEMENT_RULES: list[dict] = [
    r for r in V2_DH_PLACEMENT_RULES if r["order"] != 9
]


def _v2_player_score(
    entry: dict,          # {adj_woba, adj_obp, adj_iso, adj_con, adj_run, defense, raw_iso, pa}
    rule: dict,
) -> float:
    """v2スコア計算: primary指標 + def_bonus + tiebreak微調整。"""
    primary_key = rule["primary"]
    tiebreak_key = rule.get("tiebreak", "adj_woba")

    # 守備補正は7・8番のみ加算（最大値でも1点前後なので指標スケールと整合）
    def_add = entry.get("defense", 0.0) * 0.3 if rule.get("def_bonus") else 0.0

    score = (
        float(entry.get(primary_key, 0.0) or 0.0)
        + def_add
        + float(entry.get(tiebreak_key, 0.0) or 0.0) * 0.001  # タイブレークは微小係数
    )
    return score


def _do_build_predicted_lineup_v2(
    window_games: int,
    use_dh: bool,
    cache_bucket: dict,
    cache_key: str,
    team_code: str = "広島",
) -> dict:
    """
    v2: wOBAランク＋配置ルール方式。

    【アルゴリズム概要】
    1. 全候補を adj_woba 降順でランク付けしたプールを作成
    2. 配置ルール（V2_DH_PLACEMENT_RULES）を 4→3→5→2→1→6→7→8→9 の順で適用
       - pool_top_pct: adj_woba上位X%に候補を絞る
       - primary指標が最大の選手を選出、no_iso_zero でISO=0を除外
       - ポジション割当は eligible_positions から used_positions を除いた最善を採用
    3. 全スロットを順番確定後に order でソート
    4. reason/commentary を生成して既存フォーマットで返す
    """
    placement_rules = V2_DH_PLACEMENT_RULES if use_dh else V2_NODH_PLACEMENT_RULES

    recent_map      = _recent_snapshot_map(window_games, team_code)
    defense_map     = _get_player_defense(team_code)
    candidate_names = _get_prediction_candidate_names(team_code=team_code)

    # ── ステージ1: 全候補のスナップショットをプールとして整理 ──
    # {canonical_name: {adj_woba, adj_obp, adj_iso, adj_con, adj_run,
    #                   defense, raw_iso, pa, ...}}
    pool: dict[str, dict] = {}
    for player_name in candidate_names:
        cname  = _canonical_player_name(player_name, team_code)
        recent = recent_map.get(cname, {}).copy() or recent_map.get(
            _normalize_player_name(player_name), {}
        ).copy()
        def_val = _defense_value_for(cname, "", defense_map)

        # シーズン成績（ポジション別ベスト）
        eligible = (
            (_get_player_profile(team_code).get(cname) or {})
            .get("eligible_positions", [])
        )
        best_s_obp, best_s_iso = 0.0, 0.0
        for pos in eligible:
            sp = _get_adjusted_position_batting(cname, pos, team_code)
            best_s_obp = max(best_s_obp, float(sp.get("obp", 0.0) or 0.0))
            best_s_iso = max(best_s_iso, float(sp.get("iso", 0.0) or 0.0))

        pool[cname] = {
            # ベイズ収縮済み指標（選出判断に使用）
            "adj_woba":  float(recent.get("adj_woba",  _LEAGUE_WOBA)    or _LEAGUE_WOBA),
            "adj_obp":   float(recent.get("adj_obp",   NPB_LEAGUE_AVG_OBP) or NPB_LEAGUE_AVG_OBP),
            "adj_iso":   float(recent.get("adj_iso",   NPB_LEAGUE_AVG_ISO) or NPB_LEAGUE_AVG_ISO),
            "adj_con":   float(recent.get("adj_con",   0.77)            or 0.77),
            "adj_run":   float(recent.get("adj_run",   0.0)             or 0.0),
            "defense":   def_val,
            # 生の観測値（ハードカット判定用）
            "raw_iso":   float(recent.get("iso",  0.0) or 0.0),
            "pa":        int(recent.get("pa", 0)   or 0),
            # 表示用（recent dict をそのまま保持）
            "_recent":   recent,
            "_season_s_obp": best_s_obp,
            "_season_s_iso": best_s_iso,
            "_eligible": eligible,
        }

    # ランキング用（_build_ranks に渡すリスト）
    all_stats_for_rank: list[dict] = []
    for cname, entry in pool.items():
        all_stats_for_rank.append({
            "name":        cname,
            "recent_obp":  entry["adj_obp"],
            "recent_iso":  entry["adj_iso"],
            "recent_woba": entry["adj_woba"],
            "recent_con":  entry["adj_con"],
            "recent_run":  entry["adj_run"],
            "season_obp":  entry["_season_s_obp"],
            "season_iso":  entry["_season_s_iso"],
            "defense":     entry["defense"],
        })
    ranks = _build_ranks(all_stats_for_rank)

    # adj_woba 降順でプール全体をソートしてランク付き候補リストを作成
    # 複合ソートキー: (PA=0フラグ, -adj_woba)
    # → PA>=1 の選手が常に PA=0 の選手より上位になる
    pool_sorted: list[tuple[str, dict]] = sorted(
        pool.items(),
        key=lambda kv: (1 if kv[1]["pa"] == 0 else 0, -kv[1]["adj_woba"]),
    )
    n_pool = len(pool_sorted)
    # PA>=1 の選手数（pool_top_pct カットの基準に使用）
    n_pool_active = sum(1 for _, e in pool_sorted if e["pa"] >= 1)

    # ── ステージ2: 配置ルールを順番に適用 ──
    used_players: set[str]   = set()
    used_positions: set[str] = set()
    lineup: list[dict]       = []

    for rule in placement_rules:
        order = rule["order"]

        # pool_top_pct: adj_woba 上位 N% に絞る（None=全員）
        # ・カット基準は「PA>=1 の選手数（n_pool_active）」を使う
        #   → PA=0 の選手が上位 N% に入り込まないようにする
        # ・used_players を除いた残り候補から適用
        top_pct = rule.get("pool_top_pct")
        if top_pct is not None and n_pool_active > 0:
            cut_idx = max(1, int(math.ceil(n_pool_active * top_pct)))
            # pool_sorted は (PA=0フラグ, -adj_woba) 複合ソート済みなので
            # 先頭 cut_idx 件は必ず PA>=1 の上位 N% になる
            candidates_for_slot = [
                (cn, entry) for cn, entry in pool_sorted[:cut_idx]
                if cn not in used_players
            ]
            # 上位に絞り込んだ結果が空の場合は PA>=1 全員にフォールバック
            if not candidates_for_slot:
                candidates_for_slot = [
                    (cn, entry) for cn, entry in pool_sorted
                    if cn not in used_players and entry["pa"] >= 1
                ]
        else:
            # pool_top_pct=None のスロット（3・5・6・7・8・9 番）は PA>=1 の全員
            candidates_for_slot = [
                (cn, entry) for cn, entry in pool_sorted
                if cn not in used_players and entry["pa"] >= 1
            ]

        # PA>=1 が 0 人の場合のみ PA=0 も含む全候補にフォールバック
        if not candidates_for_slot:
            candidates_for_slot = [
                (cn, entry) for cn, entry in pool_sorted
                if cn not in used_players
            ]

        # no_iso_zero: raw_iso=0 かつ PA>=5 の選手を除外（4番向け）
        if rule.get("no_iso_zero"):
            filtered = [
                (cn, e) for cn, e in candidates_for_slot
                if not (e["raw_iso"] == 0.0 and e["pa"] >= 5)
            ]
            # 全員除外された場合はフォールバック
            if filtered:
                candidates_for_slot = filtered

        best_pick: dict | None = None
        best_score: float      = float("-inf")

        for cname, entry in candidates_for_slot:
            # ── ポジション割当 ──
            eligible_positions = entry["_eligible"] or [POS_DH]
            available_positions = [
                p for p in eligible_positions
                if p not in used_positions
                and (use_dh or p != POS_DH)
            ]
            if not available_positions:
                continue

            sc = _v2_player_score(entry, rule)

            if sc > best_score:
                best_score = sc
                # ポジションはシーズン守備指標が最も高いものを採用
                best_pos = max(
                    available_positions,
                    key=lambda p: _defense_value_for(cname, p, defense_map, team_code),
                )
                best_pick = {
                    "order":       order,
                    "position":    best_pos,
                    "player_name": cname,
                    "score":       round(sc, 3),
                    "role":        rule["role"],
                    "_entry":      entry,
                }

        if best_pick is None:
            # ── フォールバック: used_positions 制約を緩めて再試行 ──
            # 優先順位: ①DH枠がまだ空いている選手 → ②used_positions 重複を許容
            fallback_all = [
                (cn, e) for cn, e in pool_sorted
                if cn not in used_players
            ]
            # まずDH枠空き選手に絞る（use_dh=Trueかつ DH not in used_positions）
            dh_open = POS_DH not in used_positions and use_dh
            if dh_open:
                dh_candidates = [
                    (cn, e) for cn, e in fallback_all
                    if not e["_eligible"]   # eligible=[] → DH専用
                    or POS_DH in (e["_eligible"] or [])
                ]
                if dh_candidates:
                    fallback_all = dh_candidates
            for cname_fb, entry_fb in fallback_all:
                eligible_fb = entry_fb["_eligible"] or [POS_DH]
                avail_fb = [
                    p for p in eligible_fb
                    if use_dh or p != POS_DH
                ]
                # used_positions 制約を無視（重複ポジションを許容）
                if not avail_fb:
                    # eligible が空の場合も DH で対応
                    if use_dh:
                        avail_fb = [POS_DH]
                    else:
                        continue
                sc_fb = _v2_player_score(entry_fb, rule)
                if sc_fb > best_score:
                    best_score = sc_fb
                    # DH 空きがあれば DH を優先
                    if dh_open and POS_DH in avail_fb:
                        best_pos_fb = POS_DH
                    else:
                        best_pos_fb = max(
                            avail_fb,
                            key=lambda p: _defense_value_for(cname_fb, p, defense_map, team_code),
                        )
                    best_pick = {
                        "order":       order,
                        "position":    best_pos_fb,
                        "player_name": cname_fb,
                        "score":       round(sc_fb, 3),
                        "role":        rule["role"],
                        "_entry":      entry_fb,
                    }

        if best_pick is None:
            continue

        cname   = best_pick["player_name"]
        entry   = best_pick["_entry"]
        pos     = best_pick["position"]
        recent  = entry["_recent"]

        used_players.add(cname)
        used_positions.add(pos)

        # シーズン打撃成績（ポジション別）
        season_pos = _get_adjusted_position_batting(cname, pos, team_code)

        reason = _build_reason(
            cname, pos, best_pick["role"],
            recent, season_pos, entry["defense"], ranks, window_games,
        )
        commentary = _build_commentary(
            cname, pos, best_pick["role"],
            recent, season_pos, entry["defense"], ranks, window_games,
            best_pick["score"],
        )

        lineup.append({
            "order":       best_pick["order"],
            "position":    pos,
            "player_name": cname,
            "score":       best_pick["score"],
            "reason":      reason,
            "commentary":  commentary,
            "recent": {
                "games":    recent.get("games",  0),
                "pa":       recent.get("pa",     0),
                "ab":       recent.get("ab",     0),
                "obp":      recent.get("obp",    0.0),
                "iso":      recent.get("iso",    0.0),
                "woba":     recent.get("woba",   0.0),
                "con":      recent.get("con",    0.75),
                "run":      recent.get("run",    0.0),
                "adj_obp":  recent.get("adj_obp",  NPB_LEAGUE_AVG_OBP),
                "adj_iso":  recent.get("adj_iso",  NPB_LEAGUE_AVG_ISO),
                "adj_woba": recent.get("adj_woba", _LEAGUE_WOBA),
                "adj_con":  recent.get("adj_con",  0.77),
                "adj_run":  recent.get("adj_run",  0.0),
                "prior_woba":  recent.get("prior_woba",  _LEAGUE_WOBA),
                "reliability": recent.get("reliability", 0.0),
            },
            "season_position": {
                "pa":  float(season_pos.get("pa",  0.0) or 0.0),
                "ab":  float(season_pos.get("ab",  0.0) or 0.0),
                "obp": float(season_pos.get("obp", 0.0) or 0.0),
                "iso": float(season_pos.get("iso", 0.0) or 0.0),
            },
            "defense": round(entry["defense"], 3),
            "role":    best_pick["role"],
        })

    lineup.sort(key=lambda x: x["order"])

    result = {
        "use_dh":       use_dh,
        "window_games": window_games,
        "generated_at": _now_jst().isoformat(),
        "lineup":       lineup,
        "algorithm":    "v2_woba_rank",
    }

    cache_bucket[cache_key] = {
        "value":      result,
        "expires_at": _cache_now() + CACHE_TTL_PREDICTED_LINEUP,
    }
    return result


def _build_predicted_lineup_v2(
    window_games: int,
    use_dh: bool,
    team_code: str = "広島",
) -> dict:
    """v2方式のキャッシュ付きエントリポイント。"""
    cache_bucket = _cache_get_bucket("predicted_lineup_v2")
    cache_key    = f"w{window_games}:dh{int(use_dh)}:{team_code}"
    cache_entry  = cache_bucket.get(cache_key)

    if _cache_alive(cache_entry):
        cached = cache_entry.get("value")
        if isinstance(cached, dict):
            return cached

    # stale-while-revalidate
    stale = cache_entry.get("value") if cache_entry else None
    if stale and isinstance(stale, dict):
        def _bg():
            try:
                _do_build_predicted_lineup_v2(
                    window_games, use_dh, cache_bucket, cache_key, team_code
                )
            except Exception as e:
                print("DEBUG_V2_BG_ERROR", str(e))
        cache_bucket[cache_key] = {**cache_entry, "expires_at": _cache_now() + 60}
        threading.Thread(target=_bg, daemon=True, name=f"bg-lineup-v2-{cache_key}").start()
        return stale

    return _do_build_predicted_lineup_v2(
        window_games, use_dh, cache_bucket, cache_key, team_code
    )


def _wants_html(request: Request, view: str | None) -> bool:
    if view == "json":
        return False
    if view in ("html", "season"):
        return True

    accept = (request.headers.get("accept") or "").lower()
    return "text/html" in accept


def _html_page(title: str, body: str, description: str = "", canonical_path: str = "") -> HTMLResponse:
    _desc = description or "NPB全12球団の打撃成績・予想打順・得点圏打率・WAR・走塁守備指標をリアルタイムで分析するファンサイトです。"
    _site_url = "https://www.koidanshi.com"
    _canonical = f"{_site_url}{canonical_path}" if canonical_path else _site_url
    _json_ld = '{"@context":"https://schema.org","@type":"WebSite","name":"鯉男の打席分析室","url":"https://www.koidanshi.com","description":"NPB全12球団の打撃成績・予想打順・得点圏打率・WAR・走塁守備指標をリアルタイムで分析するファンサイト。ベイズ補正・セイバーメトリクス指標を活用したデータ分析を提供します。","inLanguage":"ja","potentialAction":{"@type":"SearchAction","target":{"@type":"EntryPoint","urlTemplate":"https://www.koidanshi.com/public/select?page=predicted-lineup"},"query-input":"required name=search_term_string"}}'
    return HTMLResponse(
        f"""<!doctype html>
<html lang="ja">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{escape(title)} | 鯉男の打席分析室</title>
  <meta name="description" content="{escape(_desc)}">
  <meta name="robots" content="index, follow">
  <link rel="canonical" href="{_canonical}">
  <meta property="og:title" content="{escape(title)} | 鯉男の打席分析室">
  <meta property="og:description" content="{escape(_desc)}">
  <meta property="og:type" content="website">
  <meta property="og:locale" content="ja_JP">
  <meta property="og:url" content="{_canonical}">
  <meta property="og:site_name" content="鯉男の打席分析室">
  <script type="application/ld+json">{_json_ld}</script>
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
    .team-select {{
      display: inline-flex;
      align-items: center;
      color: #d8e4f8;
      background: #0d1628;
      border: 1px solid #2e4070;
      border-radius: 5px;
      padding: 4px 8px;
      font-weight: 700;
      font-size: 12px;
      cursor: pointer;
      flex-shrink: 0;
      appearance: none;
      -webkit-appearance: none;
    }}
    .team-select:hover {{
      background: #172038;
      border-color: #4a6090;
    }}
    .team-select option {{
      background: #0d1628;
      color: #d8e4f8;
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
  <!-- Google AdSense 自動広告 -->
  <script>
    (adsbygoogle = window.adsbygoogle || []).push({{
      google_ad_client: "ca-pub-9923885942831563",
      enable_page_level_ads: true
    }});
  </script>
</head>
<body>
  <!-- サイトヘッダー -->
  <header class="site-header">
    <a class="site-logo" href="/public/top">
      鯉男の打席分析室<span>NPB 全12球団 データ分析</span>
    </a>
    <nav class="site-header-nav">
      <a href="/public/game-recap?view=html">試合一覧</a>
      <a href="/public/risp?view=html">得点圏</a>
      <a href="/public/about">このサイトについて</a>
      <a href="/public/privacy">プライバシーポリシー</a>
    </nav>
  </header>

  <!-- 3カラムレイアウト -->
  <div class="page-layout">

    <!-- 左広告 -->
    <aside class="ad-col">
      <div class="ad-unit" id="ad-left-1">
        <ins class="adsbygoogle"
             style="display:block;width:160px;height:600px"
             data-ad-client="ca-pub-9923885942831563"
             data-ad-slot="1234567890"
             data-ad-format="vertical"></ins>
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
          <a href="/public/top">トップ</a>
          <a href="/public/predicted-lineup?window_games=5&use_dh=true&view=html">予想打順</a>
          <a href="/public/recent-batting?view=html">直近打撃</a>
          <a href="/public/risp?view=html">得点圏</a>
          <a href="/public/game-recap?view=html">試合一覧</a>
          <a href="/public/fielding-baserunning?view=html">走塁・守備</a>
          <a href="/public/war-ranking?view=html">WAR</a>
          <a href="/public/about">このサイトについて</a>
          <a href="/public/privacy">プライバシーポリシー</a>
          <a href="/public/terms">利用規約</a>
        </div>
        <div>© 2025 鯉男の打席分析室 — 非公式ファンサイト。掲載データはYahoo!スポーツ・NPB Basementより取得。</div>
        <div style="margin-top:4px">本サイトはNPB各球団及びNPBとは無関係の個人ファンサイトです。</div>
      </footer>
    </main>

    <!-- 右広告 -->
    <aside class="ad-col">
      <div class="ad-unit" id="ad-right-1">
        <ins class="adsbygoogle"
             style="display:block;width:160px;height:600px"
             data-ad-client="ca-pub-9923885942831563"
             data-ad-slot="0987654321"
             data-ad-format="vertical"></ins>
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


def _render_season_stats_html(active_page: str = "", window_games: int = 5, team_code: str = "広島") -> str:
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
        season_data = _get_season_position_batting(team_code)
        adv_rows    = _get_advanced_stats_rows(team_code)

        # 選手ごとに「全ポジション中で最もPAが多い打撃成績」を集約
        seen: dict[str, dict] = {}
        for player_name in _get_player_profile(team_code).keys():
            cname = _canonical_player_name(player_name, team_code)
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
        season_data = _get_season_position_batting(team_code)
        adv_rows    = _get_advanced_stats_rows(team_code)

        seen: dict[str, dict] = {}
        for player_name in _get_player_profile(team_code).keys():
            cname = _canonical_player_name(player_name, team_code)
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


def _common_nav(active_page: str = "", window_games: int = 5, team_code: str = "広島") -> str:
    """全ページ共通ナビゲーションバー HTML を返す。
    active_page: 'recent-batting' / 'risp' / 'fielding' / 'war' /
                 'predicted-lineup-3t' / 'predicted-lineup-3f' /
                 'predicted-lineup-5t' / 'predicted-lineup-5f' /
                 'predicted-lineup-7t' / 'predicted-lineup-7f' /
                 'predicted-lineup-10t' / 'predicted-lineup-10f' / 'game-recap'
    """
    def _a(label: str, href: str, page_key: str) -> str:
        cls = " active" if active_page == page_key else ""
        return f'<a class="nav-btn{cls}" href="{href}" data-prefetch="{href}">{label}</a>'

    tc = team_code
    wg = window_games

    # チーム選択ドロップダウン
    all_teams = list(YAHOO_TEAM_ID.keys())
    options_html = "".join(
        f'<option value="{t}"{" selected" if t == tc else ""}>{t}</option>'
        for t in all_teams
    )
    # 現在のページを維持しつつ team だけ変えるJS
    team_selector_html = f"""
      <div class="nav-section">
        <span class="nav-label">球団</span>
        <div class="nav-group" style="display:flex;gap:4px;align-items:center;">
          <select id="team-selector" class="team-select">
            {options_html}
          </select>
          <button id="team-switch-btn" class="nav-btn" style="background:#ffd54a;color:#06100a;border-color:#ffd54a;font-weight:800;cursor:pointer;">切替</button>
        </div>
      </div>
      <script>
      (function() {{
        var btn = document.getElementById('team-switch-btn');
        if (btn) {{
          btn.addEventListener('click', function() {{
            var sel = document.getElementById('team-selector');
            if (!sel) return;
            var u = new URL(window.location.href);
            u.searchParams.set('team', sel.value);
            window.location.href = u.toString();
          }});
        }}
      }})();
      </script>"""

    nav_html = f"""
    <nav class="nav-bar">
      {team_selector_html}
      <div class="nav-section">
        <span class="nav-label">予想打順</span>
        <div class="nav-group">
          {_a("DH有", f"/public/predicted-lineup?window_games={wg}&use_dh=true&team={tc}",  "predicted-lineup-" + str(wg) + "t")}
          {_a("DH無", f"/public/predicted-lineup?window_games={wg}&use_dh=false&team={tc}", "predicted-lineup-" + str(wg) + "f")}
        </div>
      </div>
      <div class="nav-section">
        <span class="nav-label">打撃</span>
        <div class="nav-group">
          {_a("直近打撃",  f"/public/recent-batting?window_games={wg}&team={tc}", "recent-batting")}
          {_a("得点圏",    f"/public/risp?window_games={wg}&view=html&team={tc}", "risp")}
        </div>
      </div>
      <div class="nav-section">
        <span class="nav-label">指標</span>
        <div class="nav-group">
          {_a("走塁・守備", f"/public/fielding-baserunning?team={tc}", "fielding")}
          {_a("WAR",       f"/public/war-ranking?team={tc}",          "war")}
        </div>
      </div>
      <div class="nav-section">
        <span class="nav-label">試合</span>
        <div class="nav-group">
          {_a("試合一覧", f"/public/game-recap?team={tc}", "game-recap")}
        </div>
      </div>
    </nav>"""
    return nav_html


def _render_recent_batting_html(data: dict, show_season: bool = False, team_code: str = "広島") -> HTMLResponse:
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
            <a class="nav-btn{_rb_cls(5)}"  href="/public/recent-batting?window_games=5&team={team_code}">直近 5試合</a>
            <a class="nav-btn{_rb_cls(10)}" href="/public/recent-batting?window_games=10&team={team_code}">直近 10試合</a>
          </div>
        </div>
        <div class="nav-section">
          <span class="nav-label">表示</span>
          <div class="nav-group">
            <a class="nav-btn{'' if show_season else ' active'}" href="/public/recent-batting?window_games={wg}&team={team_code}">直近</a>
            <a class="nav-btn{' active' if show_season else ''}" href="/public/recent-batting?window_games={wg}&view=season&team={team_code}">通算</a>
          </div>
        </div>
      </div>
      {_common_nav("recent-batting", wg, team_code)}
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
        body += _render_season_stats_html("recent-batting", wg, team_code)
    return _html_page("直近打撃成績", body)


def _render_compare_html(combined: dict, team_code: str = "広島") -> HTMLResponse:
    """v1とv2の打順を横並びで比較するHTMLを生成する。"""
    team     = combined.get("team", team_code)
    wg       = combined.get("window_games", 5)
    use_dh   = combined.get("use_dh", True)
    v1_lineup = combined.get("v1", {}).get("lineup", [])
    v2_lineup = combined.get("v2", {}).get("lineup", [])
    dh_label = "DH有" if use_dh else "DH無"

    # 打順番号をキーにしたマップ
    v1_map = {p["order"]: p for p in v1_lineup}
    v2_map = {p["order"]: p for p in v2_lineup}
    orders = sorted(set(list(v1_map.keys()) + list(v2_map.keys())))

    def _fmt_entry(p: dict | None, side: str) -> str:
        if not p:
            return "<td colspan='4'>—</td>"
        name  = p.get("player_name", "?")
        pos   = p.get("position", "")
        rec   = p.get("recent", {})
        iso   = float(rec.get("iso",  0.0) or 0.0)
        woba  = float(rec.get("woba", 0.0) or 0.0)
        adj_w = float(rec.get("adj_woba", 0.0) or 0.0)
        role  = p.get("role", "")
        role_label = _ROLE_LABEL_JA.get(role, role)

        color = "#1a3a5c" if side == "v1" else "#2a1a4a"
        return (
            f"<td style='background:{color};padding:6px 10px;font-weight:700;color:#e8f0fe'>"
            f"{name}<span style='font-size:10px;color:#8494b8;margin-left:6px'>[{pos}]</span></td>"
            f"<td style='padding:6px 10px;color:#aab4c8;font-size:12px'>{role_label}</td>"
            f"<td style='padding:6px 10px;text-align:right;color:#7dd3fc'>{woba:.3f}"
            f"<span style='color:#6b7280;font-size:10px'> adj:{adj_w:.3f}</span></td>"
            f"<td style='padding:6px 10px;text-align:right;color:#fbbf24'>{iso:.3f}</td>"
        )

    rows_html = ""
    for order in orders:
        p1 = v1_map.get(order)
        p2 = v2_map.get(order)
        same = p1 and p2 and p1.get("player_name") == p2.get("player_name")
        diff_mark = "" if same else "⚡"
        rows_html += (
            f"<tr>"
            f"<td style='padding:6px 10px;text-align:center;font-weight:700;"
            f"color:#f59e0b;width:40px'>{order}番</td>"
            f"{_fmt_entry(p1, 'v1')}"
            f"<td style='padding:6px 10px;text-align:center;color:#f59e0b;"
            f"font-size:16px'>{diff_mark}</td>"
            f"{_fmt_entry(p2, 'v2')}"
            f"</tr>"
        )

    # サマリー: 一致選手数
    same_count = sum(
        1 for o in orders
        if v1_map.get(o) and v2_map.get(o)
        and v1_map[o].get("player_name") == v2_map[o].get("player_name")
    )
    total = len(orders)

    body = f"""
<div style="max-width:1100px;margin:0 auto;padding:16px">
  <h2 style="color:#e8f0fe;margin-bottom:4px">
    予想打順 比較: {team}
    <span style="font-size:13px;color:#8494b8;margin-left:12px">
      直近{wg}試合 / {dh_label}
    </span>
  </h2>
  <p style="color:#6b7280;font-size:12px;margin-bottom:16px">
    ⚡ マークは v1 と v2 で選手が異なる打順 /
    一致: <strong style="color:#34d399">{same_count}</strong> / {total} 打順
  </p>

  <div style="overflow-x:auto">
  <table style="width:100%;border-collapse:collapse;background:#0d1b2e;
                color:#c9d4e8;font-size:13px">
    <thead>
      <tr style="background:#1a2540;border-bottom:2px solid #2a3f6f">
        <th style="padding:8px 10px;width:40px"></th>
        <th colspan="4" style="padding:8px 10px;color:#60a5fa;text-align:center">
          v1 — スロット重み付け方式
        </th>
        <th style="padding:8px 10px;width:30px"></th>
        <th colspan="4" style="padding:8px 10px;color:#c084fc;text-align:center">
          v2 — wOBAランク＋配置ルール方式
        </th>
      </tr>
      <tr style="background:#111d2e;color:#6b7280;font-size:11px">
        <th></th>
        <th style="padding:4px 10px">選手名</th>
        <th style="padding:4px 10px">役割</th>
        <th style="padding:4px 10px;text-align:right">wOBA</th>
        <th style="padding:4px 10px;text-align:right">ISO</th>
        <th></th>
        <th style="padding:4px 10px">選手名</th>
        <th style="padding:4px 10px">役割</th>
        <th style="padding:4px 10px;text-align:right">wOBA</th>
        <th style="padding:4px 10px;text-align:right">ISO</th>
      </tr>
    </thead>
    <tbody>
      {rows_html}
    </tbody>
  </table>
  </div>

  <div style="margin-top:20px;display:grid;grid-template-columns:1fr 1fr;gap:16px">
    <div>
      <h3 style="color:#60a5fa;font-size:14px;margin-bottom:8px">
        v1 コメンタリー（スロット方式）
      </h3>
      {"".join(
          f'<div style="margin-bottom:10px;padding:10px;background:#0d1b2e;'
          f'border-left:3px solid #1e40af;border-radius:4px">'
          f'<span style="color:#f59e0b;font-weight:700">{p["order"]}番</span> '
          f'<span style="color:#e8f0fe">{p.get("player_name","")}</span> '
          f'<span style="color:#6b7280;font-size:11px">[{p.get("position","")}]</span><br>'
          f'<span style="color:#9ca3af;font-size:12px">{p.get("reason","")}</span>'
          f'</div>'
          for p in sorted(v1_lineup, key=lambda x: x["order"])
      )}
    </div>
    <div>
      <h3 style="color:#c084fc;font-size:14px;margin-bottom:8px">
        v2 コメンタリー（wOBAランク方式）
      </h3>
      {"".join(
          f'<div style="margin-bottom:10px;padding:10px;background:#0d1b2e;'
          f'border-left:3px solid #7c3aed;border-radius:4px">'
          f'<span style="color:#f59e0b;font-weight:700">{p["order"]}番</span> '
          f'<span style="color:#e8f0fe">{p.get("player_name","")}</span> '
          f'<span style="color:#6b7280;font-size:11px">[{p.get("position","")}]</span><br>'
          f'<span style="color:#9ca3af;font-size:12px">{p.get("reason","")}</span>'
          f'</div>'
          for p in sorted(v2_lineup, key=lambda x: x["order"])
      )}
    </div>
  </div>

  <div style="margin-top:20px;padding:12px;background:#111d2e;
              border-radius:6px;font-size:11px;color:#6b7280">
    <strong style="color:#8494b8">v2 アルゴリズム概要:</strong>
    ① 全候補を adj_woba 降順でランク付け
    → ② 4番: wOBA上位50%かつISO最大（ISO=0除外）
    → ③ 3番: 残りwOBA最大
    → ④ 5番: 残りwOBA最大
    → ⑤ 2番: wOBA上位60%かつCON最大
    → ⑥ 1番: wOBA上位60%かつOBP最大（RUN補助）
    → ⑦ 6〜8番: 残りwOBA降順（7・8番は守備補正加味）
    → ⑧ 9番(DH有): 残りOBP最大
  </div>
</div>
"""
    return _html_page(f"打順比較 {team}", body)


def _render_predicted_lineup_html(
    data: dict,
    team_code: str = "広島",
    mode: str = "precision",
) -> HTMLResponse:
    lineup = data.get("lineup", [])

    # ── 打順行（1行レイアウト）を生成 ──
    rows_html = []
    for item in lineup:
        recent   = item.get("recent", {}) or {}
        season   = item.get("season_position", {}) or {}
        order    = int(item.get("order", 0) or 0)
        pos_code = str(item.get("position", "") or "")
        pos_ja   = POSITION_LABELS.get(pos_code, pos_code)
        r_pa     = int(recent.get("pa", 0) or 0)
        r_games  = int(recent.get("games", 0) or 0)
        r_obp    = float(recent.get("obp",  0.0) or 0.0)
        r_iso    = float(recent.get("iso",  0.0) or 0.0)
        r_woba   = float(recent.get("woba", 0.0) or 0.0)
        r_con    = float(recent.get("con",  0.75) or 0.75)
        r_run    = float(recent.get("run",  0.0)  or 0.0)
        s_obp    = float(season.get("obp", 0.0) or 0.0)
        s_iso    = float(season.get("iso", 0.0) or 0.0)
        s_pa     = float(season.get("pa", 0.0) or 0.0)
        r_avg    = float(recent.get("avg", 0.0) or 0.0)
        r_slg    = round(r_avg + r_iso, 3)
        r_ops    = round(r_obp + r_slg, 3)
        defv     = float(item.get("defense", 0.0) or 0.0)
        score    = float(item.get("score",   0.0) or 0.0)
        def_cls  = "def-pos" if defv > 0 else ("def-neg" if defv < 0 else "")
        reason      = escape(str(item.get("reason", "")))
        commentary  = escape(str(item.get("commentary", "")))
        wg_val      = int(data.get("window_games", 5) or 5)

        # 直近出場なし（pa=0）または打席が5未満の場合はシーズン補正値をメイン表示する
        # pa < 5 の場合は信頼性が低いためシーズン補正値を優先
        MIN_RELIABLE_PA = 5
        no_recent = (r_pa == 0)
        few_recent = (0 < r_pa < MIN_RELIABLE_PA)
        use_season = no_recent or few_recent
        if use_season:
            # シーズン補正値をメインに表示
            disp_obp   = s_obp
            disp_iso   = s_iso
            s_slg      = round(s_obp + s_iso, 3)  # 概算（obp+iso ≈ slg 近似）
            disp_slg   = s_iso  # ISOはSLG-AVGなので長打率の代わりにISO表示
            disp_ops   = round(s_obp + s_obp + s_iso, 3)  # 概算OPS≈2*obp+iso
            disp_woba  = None   # データなし・少ないので wOBA は非表示
            disp_con   = None   # 同上
            disp_run   = None   # 同上
            stat_badge = f'<span class="lu-stat-badge lu-badge-season">シーズン補正値</span>'
            if no_recent:
                stat_note = f'<div class="lu-no-recent-note">直近{wg_val}試合の打席データなし</div>'
            else:
                stat_note = f'<div class="lu-no-recent-note">直近{wg_val}試合は{r_pa}打席（サンプル少）</div>'
            obp_label  = "出塁率"
            iso_label  = "長打指数"
            slg_label  = "長打率"
            ops_label  = "OPS"
        else:
            disp_obp   = r_obp
            disp_iso   = r_iso
            disp_slg   = r_slg
            disp_ops   = r_ops
            disp_woba  = r_woba
            disp_con   = r_con
            disp_run   = r_run
            stat_badge = f'<span class="lu-stat-badge lu-badge-recent">直近{wg_val}試合</span>'
            stat_note  = ""
            obp_label  = "出塁率"
            iso_label  = "長打指数"
            slg_label  = "長打率"
            ops_label  = "OPS"

        # シーズン補正出塁・補正長打は非表示（廃止）
        season_extra = ""

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
            {stat_badge}
            {stat_note}
            <div class="lu-stats-grid">
              <div class="lu-stat">
                <div class="lu-slabel">{obp_label}</div>
                <div class="lu-sval">{disp_obp:.3f}</div>
              </div>
              <div class="lu-stat">
                <div class="lu-slabel">{iso_label}</div>
                <div class="lu-sval">{disp_iso:.3f}</div>
              </div>
              <div class="lu-stat">
                <div class="lu-slabel">{slg_label}</div>
                <div class="lu-sval">{disp_slg:.3f}</div>
              </div>
              <div class="lu-stat lu-stat-ops">
                <div class="lu-slabel">{ops_label}</div>
                <div class="lu-sval">{disp_ops:.3f}</div>
              </div>
              {season_extra}
              {'<div class="lu-stat lu-stat-woba"><div class="lu-slabel">wOBA</div><div class="lu-sval">' + f'{disp_woba:.3f}' + '</div></div>' if disp_woba is not None else ''}
              {'<div class="lu-stat lu-stat-con"><div class="lu-slabel">コンタクト</div><div class="lu-sval">' + f'{disp_con:.3f}' + '</div></div>' if disp_con is not None else ''}
              {'<div class="lu-stat lu-stat-run"><div class="lu-slabel">走力</div><div class="lu-sval">' + f'{disp_run:.3f}' + '</div></div>' if disp_run is not None else ''}
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
    _mode  = mode  # precision or hot

    def _lu_cls(w, d):
        return " active" if (w == wg and d == dh) else ""

    def _rb_cls2(w):
        return " active" if w == wg else ""

    # ── モード切替ナビ用 URL ──
    _base_url_p = f"/public/predicted-lineup?window_games={wg}&use_dh={str(dh).lower()}&team={team_code}&view=html&mode=precision"
    _base_url_h = f"/public/predicted-lineup?window_games={wg}&use_dh={str(dh).lower()}&team={team_code}&view=html&mode=hot"
    _mode_p_cls = " mode-active" if _mode == "precision" else ""
    _mode_h_cls = " mode-active" if _mode == "hot" else ""
    _mode_s_cls = " mode-active" if _mode == "season" else ""
    _base_url_s = f"/public/predicted-lineup?window_games={wg}&use_dh={str(dh).lower()}&team={team_code}&view=html&mode=season"

    # ── 直近試合数選択ボタン（3/5/7/10）— season モード時は非表示 ──
    _wg_switcher_style = ' style="display:none"' if _mode == "season" else ''
    _wg_options = [3, 5, 7, 10]
    _wg_btns_list = []
    for _w in _wg_options:
        _w_cls = " wg-active" if _w == wg else ""
        _w_url = f"/public/predicted-lineup?window_games={_w}&use_dh={str(dh).lower()}&team={team_code}&view=html&mode={_mode}"
        _wg_btns_list.append(
            f'<a class="wg-btn{_w_cls}" href="{_w_url}">{_w}試合</a>'
        )
    _wg_btns = "\n".join(_wg_btns_list)

    # ── モード説明バナー ──
    if _mode == "hot":
        _mode_banner = f"""
        <div class="mode-banner mode-banner-hot">
          <span class="mode-banner-icon">🔥</span>
          <div class="mode-banner-body">
            <div class="mode-banner-title">ホット打順（直近{wg}試合・加重移動平均ベース）</div>
            <div class="mode-banner-desc">
              ベイズ補正なし。直近{wg}試合の成績を<strong>加重移動平均</strong>（新しい試合を重視）で評価します。
              最新試合の重みを最大に、最古試合の重みを最小に設定。直近{wg}試合に出場していない選手は候補から外れます。
            </div>
          </div>
        </div>"""
    elif _mode == "season":
        _mode_banner = f"""
        <div class="mode-banner mode-banner-season">
          <span class="mode-banner-icon">📊</span>
          <div class="mode-banner-body">
            <div class="mode-banner-title">通算打順（シーズン通算成績ベース）</div>
            <div class="mode-banner-desc">
              直近試合データに依存しない、シーズン通算成績（OBP・ISO・wOBA 等）をそのまま指標として使用します。
              直近の好不調に左右されない安定した評価です。
            </div>
          </div>
        </div>"""
    else:
        _mode_banner = f"""
        <div class="mode-banner mode-banner-precision">
          <span class="mode-banner-icon">🎯</span>
          <div class="mode-banner-body">
            <div class="mode-banner-title">精度打順（ベイズ補正あり）</div>
            <div class="mode-banner-desc">
              直近{wg}試合の成績にシーズン通算成績を組み合わせてベイズ補正した指標で打順を決定します。
              打席数が少なくてもサンプル誤差が抑えられ、安定した評価になります。
            </div>
          </div>
        </div>"""

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

      /* ── モード切替 ── */
      .mode-switcher {{
        display: flex;
        gap: 6px;
        margin: 10px 0 2px;
        flex-wrap: wrap;
      }}
      .mode-btn {{
        display: inline-flex;
        align-items: center;
        gap: 5px;
        padding: 5px 14px;
        font-size: 12px;
        font-weight: 700;
        border-radius: 6px;
        border: 1.5px solid #1a2d50;
        background: #0c1424;
        color: #6878a0;
        text-decoration: none;
        transition: background 0.15s, border-color 0.15s, color 0.15s;
        cursor: pointer;
      }}
      .mode-btn:hover {{ background: #1a2d50; color: #c8d8f4; }}
      .mode-btn.mode-active {{
        background: #1a2d50;
        border-color: #ffd54a;
        color: #ffd54a;
      }}
      .mode-btn-precision.mode-active {{ border-color: #56cff8; color: #56cff8; background: #0d2035; }}
      .mode-btn-hot.mode-active       {{ border-color: #ff8c42; color: #ff8c42; background: #1e0f00; }}
      .mode-btn-season.mode-active    {{ border-color: #4acc88; color: #4acc88; background: #041a10; }}

      /* ── 直近試合数切替 ── */
      .wg-switcher {{
        display: flex;
        align-items: center;
        gap: 5px;
        margin: 6px 0 2px;
        flex-wrap: wrap;
      }}
      .wg-label {{
        font-size: 11px;
        color: #6878a0;
        font-weight: 600;
        margin-right: 2px;
      }}
      .wg-btn {{
        display: inline-flex;
        align-items: center;
        padding: 4px 11px;
        font-size: 11.5px;
        font-weight: 700;
        border-radius: 6px;
        border: 1.5px solid #1a2d50;
        background: #0c1424;
        color: #6878a0;
        text-decoration: none;
        transition: background 0.15s, border-color 0.15s, color 0.15s;
        cursor: pointer;
      }}
      .wg-btn:hover {{ background: #1a2d50; color: #c8d8f4; }}
      .wg-btn.wg-active {{
        background: #0d2035;
        border-color: #4aaa88;
        color: #4aaa88;
        font-weight: 800;
      }}

      .mode-banner {{
        display: flex;
        gap: 10px;
        align-items: flex-start;
        padding: 10px 14px;
        border-radius: 8px;
        margin-bottom: 6px;
        border: 1px solid;
      }}
      .mode-banner-precision {{
        background: #071a2e;
        border-color: #1a4060;
      }}
      .mode-banner-hot {{
        background: #1e0d00;
        border-color: #5c2a00;
      }}
      .mode-banner-season {{
        background: rgba(4,26,16,0.85);
        border-color: #1a5e38;
      }}
      .mode-banner-icon {{ font-size: 20px; flex-shrink: 0; margin-top: 1px; }}
      .mode-banner-title {{
        font-size: 13px;
        font-weight: 700;
        color: #c8d8f4;
        margin-bottom: 3px;
      }}
      .mode-banner-desc {{
        font-size: 11.5px;
        color: #7a8eb0;
        line-height: 1.7;
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
      <!-- モード切替 -->
      <div class="mode-switcher">
        <a class="mode-btn mode-btn-precision{_mode_p_cls}" href="{_base_url_p}">
          🎯 精度打順
        </a>
        <a class="mode-btn mode-btn-hot{_mode_h_cls}" href="{_base_url_h}">
          🔥 ホット打順
        </a>
        <a class="mode-btn mode-btn-season{_mode_s_cls}" href="{_base_url_s}">
          📊 通算打順
        </a>
      </div>
      <!-- 直近試合数選択 -->
      <div class="wg-switcher"{_wg_switcher_style}>
        <span class="wg-label">直近</span>
        {_wg_btns}
      </div>
      {_common_nav("predicted-lineup-" + str(wg) + ("t" if dh else "f"), wg, team_code)}
    </div>

    {_mode_banner}

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
def public_recent_batting(request: Request, window_games: int = 5, team: str = "広島", view: str | None = None):
    try:
        window_games = max(1, min(window_games, 10))
        data = _build_recent_batting_response(window_games, team_code=team)

        if _wants_html(request, view):
            show_season = (view == "season")
            return _render_recent_batting_html(data, show_season=show_season, team_code=team)

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

@router.get("/internal/cache-clear")
def internal_cache_clear(team: str | None = None):
    """内部用: recent_batting / predicted_lineup キャッシュをクリアする。
    team を指定した場合はそのチームのみ、省略した場合は全チーム。"""
    rb = CACHE.get("recent_batting", {})
    pl = CACHE.get("predicted_lineup", {})
    cleared = []
    if team:
        for bucket, label in [(rb, "recent_batting"), (pl, "predicted_lineup")]:
            keys_to_del = [k for k in bucket if team in k]
            for k in keys_to_del:
                del bucket[k]
                cleared.append(f"{label}:{k}")
    else:
        rb.clear()
        pl.clear()
        cleared.append("recent_batting:ALL")
        cleared.append("predicted_lineup:ALL")
    return {"cleared": cleared}


@router.get("/internal/debug-recent")
def internal_debug_recent(team: str = "阪神", window_games: int = 5):
    """内部デバッグ用: recent_snapshot_map の内容をそのまま返す。"""
    try:
        snap = _recent_snapshot_map(window_games, team)
        result = {}
        for name, d in snap.items():
            result[name] = {
                "games": d.get("games", 0),
                "pa":    d.get("pa", 0),
                "obp":   d.get("obp", 0.0),
                "iso":   d.get("iso", 0.0),
            }
        return {"team": team, "window_games": window_games, "players": result}
    except Exception as e:
        return {"error": str(e)}


@router.get("/internal/debug-aliases")
def internal_debug_aliases(pattern: str = "佐藤"):
    """内部デバッグ用: PLAYER_NAME_ALIASES のうち pattern を含むエントリを返す。"""
    from app.api.public import PLAYER_NAME_ALIASES
    matched = {k: v for k, v in PLAYER_NAME_ALIASES.items() if pattern in k or pattern in v}
    return {"pattern": pattern, "aliases": matched, "total": len(PLAYER_NAME_ALIASES)}


@router.get("/public/predicted-lineup")
def public_predicted_lineup(
    request: Request,
    window_games: int = 5,
    use_dh: bool = True,
    team: str = "広島",
    view: str | None = None,
    mode: str = "precision",
):
    try:
        window_games = max(1, min(window_games, 10))
        _mode = mode if mode in ("precision", "hot", "season") else "precision"
        data = _build_simple_predicted_lineup(
            window_games=window_games, use_dh=use_dh, team_code=team, mode=_mode
        )

        # ── キャッシュ内の `recent` が古い（pa=0）場合の補正 ──
        # predicted_lineup キャッシュは TTL=20分。その間に recent データが変化しても
        # キャッシュから古い recent.pa=0 が返ることがある。
        # ここで毎リクエスト最新の _recent_snapshot_map から recent を注入して補正する。
        try:
            snap = _recent_snapshot_map(window_games, team, mode=_mode)
            for item in data.get("lineup", []):
                cname = item.get("player_name", "")
                snap_entry = snap.get(cname)
                if snap_entry:
                    item["recent"] = {
                        "games":     snap_entry.get("games",     0),
                        "pa":        snap_entry.get("pa",        0),
                        "ab":        snap_entry.get("ab",        0),
                        "obp":       snap_entry.get("obp",       0.0),
                        "iso":       snap_entry.get("iso",       0.0),
                        "woba":      snap_entry.get("woba",      0.0),
                        "con":       snap_entry.get("con",       0.75),
                        "run":       snap_entry.get("run",       0.0),
                        "adj_obp":   snap_entry.get("adj_obp",   NPB_LEAGUE_AVG_OBP),
                        "adj_iso":   snap_entry.get("adj_iso",   NPB_LEAGUE_AVG_ISO),
                        "adj_woba":  snap_entry.get("adj_woba",  _LEAGUE_WOBA),
                        "adj_con":   snap_entry.get("adj_con",   0.77),
                        "adj_run":   snap_entry.get("adj_run",   0.0),
                        "prior_woba": snap_entry.get("prior_woba", _LEAGUE_WOBA),
                        "reliability": snap_entry.get("reliability", 0.0),
                    }
        except Exception:
            pass  # スナップショット補正に失敗してもキャッシュ値で続行

        if _wants_html(request, view):
            return _render_predicted_lineup_html(data, team_code=team, mode=_mode)

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


@router.get("/public/predicted-lineup-compare")
def public_predicted_lineup_compare(
    request: Request,
    window_games: int = 5,
    use_dh: bool = True,
    team: str = "広島",
    view: str | None = None,
):
    """
    v1（スロット重み付け）と v2（wOBAランク＋配置ルール）の打順を並べて比較する。

    view=html  → HTMLで横並び比較表示
    view=json  → JSONで両方の結果を返す
    デフォルト → Accept: text/html なら HTML、それ以外は JSON
    """
    try:
        window_games = max(1, min(window_games, 10))

        # ── v1（既存方式）──
        data_v1 = _build_simple_predicted_lineup(
            window_games=window_games, use_dh=use_dh, team_code=team
        )
        # スナップショット補正（v1と同じ処理）
        try:
            snap = _recent_snapshot_map(window_games, team)
            for item in data_v1.get("lineup", []):
                cname = item.get("player_name", "")
                snap_entry = snap.get(cname)
                if snap_entry:
                    item["recent"] = {
                        "games":      snap_entry.get("games",     0),
                        "pa":         snap_entry.get("pa",        0),
                        "ab":         snap_entry.get("ab",        0),
                        "obp":        snap_entry.get("obp",       0.0),
                        "iso":        snap_entry.get("iso",       0.0),
                        "woba":       snap_entry.get("woba",      0.0),
                        "con":        snap_entry.get("con",       0.75),
                        "run":        snap_entry.get("run",       0.0),
                        "adj_obp":    snap_entry.get("adj_obp",   NPB_LEAGUE_AVG_OBP),
                        "adj_iso":    snap_entry.get("adj_iso",   NPB_LEAGUE_AVG_ISO),
                        "adj_woba":   snap_entry.get("adj_woba",  _LEAGUE_WOBA),
                        "adj_con":    snap_entry.get("adj_con",   0.77),
                        "adj_run":    snap_entry.get("adj_run",   0.0),
                        "prior_woba": snap_entry.get("prior_woba", _LEAGUE_WOBA),
                        "reliability": snap_entry.get("reliability", 0.0),
                    }
        except Exception:
            pass

        # ── v2（新方式）──
        data_v2 = _build_predicted_lineup_v2(
            window_games=window_games, use_dh=use_dh, team_code=team
        )

        combined = {
            "team":         team,
            "use_dh":       use_dh,
            "window_games": window_games,
            "generated_at": _now_jst().isoformat(),
            "v1":           data_v1,
            "v2":           data_v2,
        }

        if _wants_html(request, view):
            return _render_compare_html(combined, team_code=team)

        return _no_cache_json(combined)

    except Exception as e:
        return JSONResponse(
            status_code=500,
            content={
                "error": "predicted-lineup-compare failed",
                "type": type(e).__name__,
                "message": str(e),
            },
        )


# ─────────────────────────────────────────────
# 走塁・守備指標 / WAR一覧  共通データ取得
# ─────────────────────────────────────────────

def _build_advanced_stats_rows(team_code: str = "広島") -> list[dict]:
    """
    npbbasement の今シーズン通算データから
    走塁指標・守備指標・WAR を選手ごとに集約して返す。
    _get_player_profile(team_code) に登録済みの野手のみ対象。
    npbbasement は全球団データを1ページに掲載しているため URL 変更不要。
    """
    players = _load_npbbasement_players()
    profile_names = set(_get_player_profile(team_code).keys())

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


def _get_advanced_stats_rows(team_code: str = "広島") -> list[dict]:
    """キャッシュ付き advanced stats 取得（12時間）"""
    cache_key = f"advanced_stats:{team_code}"
    cache_entry = CACHE.get(cache_key, {})
    if _cache_alive(cache_entry) and cache_entry.get("value"):
        return cache_entry["value"]
    rows = _build_advanced_stats_rows(team_code)
    CACHE[cache_key] = {
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


def _render_fielding_baserunning_html(rows: list[dict], show_season: bool = False, team_code: str = "広島") -> HTMLResponse:

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
            <a class="nav-btn{'' if show_season else ' active'}" href="/public/fielding-baserunning?team={team_code}">直近</a>
            <a class="nav-btn{' active' if show_season else ''}" href="/public/fielding-baserunning?view=season&team={team_code}">通算</a>
          </div>
        </div>
      </div>
      {_common_nav("fielding", 5, team_code)}
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
        body += _render_season_stats_html("fielding", team_code=team_code)
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


def _render_war_ranking_html(rows: list[dict], show_season: bool = False, team_code: str = "広島") -> HTMLResponse:

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
            <a class="nav-btn{'' if show_season else ' active'}" href="/public/war-ranking?team={team_code}">直近</a>
            <a class="nav-btn{' active' if show_season else ''}" href="/public/war-ranking?view=season&team={team_code}">通算</a>
          </div>
        </div>
      </div>
      {_common_nav("war", 5, team_code)}
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
        body += _render_season_stats_html("war", team_code=team_code)
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
def public_fielding_baserunning(request: Request, team: str = "広島", view: str | None = None):
    try:
        rows = _get_advanced_stats_rows(team_code=team)
        if _wants_html(request, view):
            show_season = (view == "season")
            return _render_fielding_baserunning_html(rows, show_season=show_season, team_code=team)
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
def public_war_ranking(request: Request, team: str = "広島", view: str | None = None):
    try:
        rows = _get_advanced_stats_rows(team_code=team)
        if _wants_html(request, view):
            show_season = (view == "season")
            return _render_war_ranking_html(rows, show_season=show_season, team_code=team)
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

@lru_cache(maxsize=64)
def _parse_carp_batting_risp(box_url: str, team_code: str = "広島") -> dict[str, dict]:
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

    npb_code = NPB_RESULTS_TEAM_CODE.get(team_code, "c")
    carp_is_home = bool(re.search(rf"/scores/\d{{4}}/\d{{4}}/{re.escape(npb_code)}-[a-z]{{1,2}}-\d{{2}}/box\.html", box_url))
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


def _build_hot_batters_data(window_games: int = 5, team_code: str = "広島") -> dict:
    """
    直近 window_games 試合の 打率・出塁率・チャンス打率 TOP選手を算出。
    """
    cache_key = f"hot_batters:{window_games}:{team_code}"
    cache_entry = CACHE.get(cache_key, {})
    if _cache_alive(cache_entry) and cache_entry.get("value"):
        return cache_entry["value"]

    # ── 打率・出塁率は既存の集計を再利用 ──
    recent_data  = _build_recent_batting_response(window_games, team_code)
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
            risp = _parse_carp_batting_risp(box_url, team_code)
        except Exception:
            continue
        for raw_name, s in risp.items():
            cname = _canonical_player_name(raw_name, team_code)
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
        for pname in _get_player_profile():
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


def _render_hot_batters_html(data: dict, show_season: bool = False, team_code: str = "広島") -> HTMLResponse:
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
              <a class="nav-btn{_wg_cls(5)}"  href="/public/hot-batters?window_games=5&team={team_code}">直近 5試合</a>
              <a class="nav-btn{_wg_cls(10)}" href="/public/hot-batters?window_games=10&team={team_code}">直近 10試合</a>
            </div>
          </div>
          <div class="nav-section">
            <span class="nav-label">表示</span>
            <div class="nav-group">
              <a class="nav-btn{'' if show_season else ' active'}" href="/public/hot-batters?window_games={wg}&team={team_code}">直近</a>
              <a class="nav-btn{' active' if show_season else ''}" href="/public/hot-batters?window_games={wg}&view=season&team={team_code}">通算</a>
            </div>
          </div>
        </div>
        {_common_nav("", wg, team_code)}
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
        body += _render_season_stats_html("hot-batters", wg, team_code)

    return _html_page("ホットバッター", body)


@router.get("/public/hot-batters")
def public_hot_batters(request: Request, window_games: int = 5, team: str = "広島", view: str | None = None):
    try:
        window_games = max(1, min(window_games, 10))
        data = _build_hot_batters_data(window_games, team_code=team)
        if _wants_html(request, view):
            show_season = (view == "season")
            return _render_hot_batters_html(data, show_season=show_season, team_code=team)
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
    """テキスト速報HTMLを解析し、指定チーム打者の全打席を返す。

    Yahoo Baseball テキスト速報の HTML 構造:
      <header class="bb-liveText__head bb-liveText__head--npbTeam{N}"> → 当該チームの攻撃イニング
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
    yahoo_id = YAHOO_TEAM_ID.get(carp_team_name, CARP_TEAM_ID)
    team_class = f"bb-liveText__head--npbTeam{yahoo_id}"

    def _strip(s: str) -> str:
        s = re.sub(r"<[^>]+>", " ", s)
        return re.sub(r"\s+", " ", s).strip()

    at_bats: list[dict] = []

    # HTML を「イニングブロック」ごとに分割
    # 各ブロックは <header class="bb-liveText__head ..."> から始まる
    sections = re.split(r"(?=<header\s[^>]*bb-liveText__head)", html)

    for sec in sections:
        # 指定チームのイニングのみ対象
        if team_class not in sec:
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


def _fetch_risp_for_game(game_id: str, team_code: str = "広島") -> list[dict]:
    """1試合分のテキスト速報から指定チーム打者の打席データを取得（キャッシュ付き）"""
    cache_bucket = _cache_get_bucket("risp")
    cache_key = f"game:{game_id}:{team_code}"
    cache_entry = cache_bucket.get(cache_key)
    if _cache_alive(cache_entry):
        return cache_entry.get("value", [])

    url = YAHOO_GAME_TEXT_URL.format(game_id=game_id)
    try:
        html = _fetch_html(url)
        at_bats = _parse_text_report(html, team_code)
        cache_bucket[cache_key] = {"value": at_bats, "expires_at": _cache_now() + CACHE_TTL_RISP}
        print(f"DEBUG_RISP_GAME[{team_code}] {game_id}: {len(at_bats)} at-bats parsed")
        return at_bats
    except Exception as e:
        print(f"DEBUG_RISP_GAME_ERROR {game_id}: {e}")
        return []


def _fetch_carp_finished_game_ids_from_team_schedule(team_code: str = "広島") -> list[str]:
    """指定チームのスケジュールページから「試合終了」のゲームIDを古い順で返す。

    URL: https://baseball.yahoo.co.jp/npb/teams/{yahoo_team_id}/schedule
    このページには当該チームの試合のみが含まれる。
    「試合終了」テキストを持つリンクのゲームIDを順に抽出する。
    重複除去・順序維持（古い順）で返す。
    """
    yahoo_id = YAHOO_TEAM_ID.get(team_code, CARP_TEAM_ID)
    url = f"https://baseball.yahoo.co.jp/npb/teams/{yahoo_id}/schedule"
    try:
        html = _fetch_html(url)
    except Exception as e:
        print(f"DEBUG_RISP_TEAM_SCHEDULE_ERROR: {e}")
        return []

    # /npb/game/(ID)/index">試合終了 パターンで抽出
    raw_ids = re.findall(r'/npb/game/(\d+)/index[^"]*">\s*試合終了', html)

    # 重複除去（順序維持）
    seen: set[str] = set()
    unique_ids: list[str] = []
    for gid in raw_ids:
        if gid not in seen:
            seen.add(gid)
            unique_ids.append(gid)

    print(f"DEBUG_RISP_TEAM_SCHEDULE[{team_code}]: {len(unique_ids)} finished games found")
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


def _get_recent_carp_game_ids(num_games: int = 5, team_code: str = "広島") -> list[tuple[str, str]]:
    """直近 num_games 試合の (game_id, date_str) リストを返す（新しい順）。

    指定チームのスケジュールページから「試合終了」ゲームIDを取得し、
    テキスト速報ページに当該チームの攻撃イニングが存在するものだけを採用する。
    各試合の日付はテキスト速報ページ本文内「YYYY年M月D日」から取得する。

    Returns:
        [(game_id, 'YYYY-MM-DD'), ...] 新しい順
    """
    cache_bucket = _cache_get_bucket("risp")
    cache_key = f"game_ids:{num_games}:{team_code}"
    cache_entry = cache_bucket.get(cache_key)
    if _cache_alive(cache_entry):
        return cache_entry.get("value", [])

    yahoo_id = YAHOO_TEAM_ID.get(team_code, CARP_TEAM_ID)
    team_class = f"bb-liveText__head--npbTeam{yahoo_id}"

    # チームスケジュールから完了試合IDを取得（古い順）
    all_finished = _fetch_carp_finished_game_ids_from_team_schedule(team_code)

    if not all_finished:
        return []

    # 新しい順（末尾から）に走査し、当該チーム出場確認済みの num_games 件を収集
    found: list[tuple[str, str]] = []
    for gid in reversed(all_finished):
        if len(found) >= num_games:
            break
        url = YAHOO_GAME_TEXT_URL.format(game_id=gid)
        try:
            html = _fetch_html(url)
            # 当該チームの攻撃イニングが存在する試合のみ採用
            if team_class not in html:
                print(f"DEBUG_RISP_SKIP {gid}: no {team_code} inning found, skipping")
                continue
            date_str = _get_game_date_from_text_page(gid, html)
            found.append((gid, date_str))
            print(f"DEBUG_RISP_GAME_ID[{team_code}] {gid}: date={date_str}")
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


def _build_game_recap(game_id: str, date_str: str, html: str, team_code: str = "広島") -> dict:
    """テキスト速報HTMLから試合要約を生成"""
    # タイトルから対戦チーム
    title_m = re.search(r"(\d{4})年(\d{1,2})月(\d{1,2})日\s+(.+?)vs\.(.+?)\s+試合", html)
    if title_m:
        team1 = _shorten_team(title_m.group(4))
        team2 = _shorten_team(title_m.group(5))
    else:
        team1, team2 = "?", team_code

    # 対象チームが先攻か後攻かを判定
    is_home = bool(re.search(rf"後攻:{re.escape(team_code)}|ホーム:{re.escape(team_code)}", html))
    away_team = team2 if is_home else team1
    home_team = team1 if is_home else team2
    carp_team = team_code
    opp_team  = away_team if carp_team == home_team else home_team

    # 最終スコア（最後のスコア表記）
    score_all = re.findall(r"([^\s]{1,6})\s+(\d+)\s*-\s*(\d+)\s+([^\s]{1,6})", html)
    carp_score: int | None = None
    opp_score: int | None = None
    if score_all:
        last = score_all[-1]
        # どちらが対象チーム？（team_code の先頭1文字 or 完全一致で判定）
        s1, n1, n2, s2 = last
        tc1 = team_code[0] if team_code else "广"
        if tc1 in s1 or team_code in s1:
            carp_score, opp_score = int(n1), int(n2)
        elif tc1 in s2 or team_code in s2:
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
    at_bats = _parse_text_report(html, team_code)

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
    tc_esc = re.escape(team_code)
    starter_m = re.search(rf"{tc_esc}が([^\s、」<]{{2,8}})(?:が|は|の)?(?:先発|登板|マウンド)", html)
    if not starter_m:
        starter_m = re.search(rf"先発ピッチャーは.*?{tc_esc}が([^\s、」<]{{2,8}})", html)
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
        summary_parts.append(f"{team_code}打線はノーヒット。")
    elif total_hits <= 3:
        summary_parts.append(f"{team_code}の安打は{total_hits}本に終わった。")

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


def _build_game_recap_data(num_games: int = 10, team_code: str = "広島") -> dict:
    """直近 num_games 試合の要約データを構築（キャッシュ10分）"""
    cache_bucket = _cache_get_bucket("risp")
    cache_key    = f"game_recap:{num_games}:{team_code}"
    cache_entry  = cache_bucket.get(cache_key)
    if _cache_alive(cache_entry):
        cached = cache_entry.get("value")
        if isinstance(cached, dict):
            return cached

    yahoo_id     = YAHOO_TEAM_ID.get(team_code, CARP_TEAM_ID)
    team_class   = f"bb-liveText__head--npbTeam{yahoo_id}"
    all_finished = _fetch_carp_finished_game_ids_from_team_schedule(team_code)
    games: list[dict] = []
    for gid in reversed(all_finished):
        if len(games) >= num_games:
            break
        try:
            html = _fetch_html(YAHOO_GAME_TEXT_URL.format(game_id=gid))
            if team_class not in html:
                continue
            date_str = _get_game_date_from_text_page(gid, html)
            recap = _build_game_recap(gid, date_str, html, team_code)
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


def _render_game_recap_html(data: dict, team_code: str = "広島") -> HTMLResponse:
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
      <div class="muted">{escape(str(data.get("team_name", team_code)))} 直近試合 / 生成 {generated_at}</div>
      {_common_nav("game-recap", 5, team_code)}
    </div>

    <div class="card">
      <div class="card-title">直近試合 結果・要約</div>
      {cards_html}
    </div>
    """
    return _html_page("試合一覧", body)


@router.get("/public/game-recap")
def public_game_recap(request: Request, team: str = "広島", view: str | None = None):
    """指定球団の直近試合一覧と要約"""
    try:
        data = _build_game_recap_data(num_games=10, team_code=team)
        if _wants_html(request, view):
            return _render_game_recap_html(data, team_code=team)
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


def _build_season_risp_data(team_code: str = "広島") -> dict:
    """今シーズン全試合の得点圏打率データを構築（通算ランキング用）。

    直近版と同じ `_fetch_risp_for_game` を使い、チームスケジュールの
    全完了試合を集計する。計算コストが高いため6時間キャッシュ。
    """
    cache_bucket = _cache_get_bucket("risp")
    cache_key = f"season_risp:{team_code}"
    cache_entry = cache_bucket.get(cache_key)
    if _cache_alive(cache_entry):
        cached = cache_entry.get("value")
        if isinstance(cached, dict):
            return cached

    all_finished = _fetch_carp_finished_game_ids_from_team_schedule(team_code)
    if not all_finished:
        result = {"games_found": 0, "players": [], "generated_at": _now_jst().isoformat()}
        cache_bucket[cache_key] = {"value": result, "expires_at": _cache_now() + 60 * 30}
        return result

    player_stats: dict[str, dict] = {}
    games_found = 0

    for gid in all_finished:
        try:
            at_bats = _fetch_risp_for_game(gid, team_code)
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
    cache_bucket[f"season_risp:{team_code}"] = {"value": result, "expires_at": _cache_now() + 60 * 60 * 6}
    return result


def _render_season_risp_html(window_games: int, team_code: str = "広島") -> str:
    """通算得点圏ランキング HTML（得点圏打率・出塁率・打点の3カラム）。

    直近版 `_render_risp_html` と同じUIで、今シーズン全試合の通算データを表示。
    最低出場要件: 得点圏打数 >= 5 / OBP は打席数 >= 15
    """
    data = _build_season_risp_data(team_code)
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


def _build_risp_data(window_games: int = 5, team_code: str = "広島") -> dict:
    """直近 window_games 試合の得点圏打率データを構築"""
    cache_bucket = _cache_get_bucket("risp")
    cache_key = f"risp:{window_games}:{team_code}"
    cache_entry = cache_bucket.get(cache_key)
    if _cache_alive(cache_entry):
        cached = cache_entry.get("value")
        if isinstance(cached, dict):
            return cached

    game_list = _get_recent_carp_game_ids(window_games, team_code)

    # 選手別集計
    # player_name → {risp_ab, risp_hit, total_ab, total_hit, bb, hbp, sf, rbi}
    player_stats: dict[str, dict] = {}
    game_details: list[dict] = []

    for game_id, date_str in game_list:
        at_bats = _fetch_risp_for_game(game_id, team_code)
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


def _render_risp_html(data: dict, window_games: int, show_season: bool = False, team_code: str = "広島") -> HTMLResponse:
    """得点圏打率ページのHTML生成 — 3カラムランキング（得点圏打率・出塁率・打点）"""
    players    = data.get("players", [])
    games_found = data.get("games_found", 0)
    generated_at = data.get("generated_at", "")
    game_list  = data.get("game_list", [])

    # ─── 一軍登録選手セットを取得（正規化済み = スペース除去）───
    try:
        active_set = _fetch_current_first_team_position_players(team_code)
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
            <a class="nav-btn{'active' if window_games==3 else ''}" href="/public/risp?window_games=3&view=html&team={team_code}">直近3試合</a>
            <a class="nav-btn {'active' if window_games==5 else ''}" href="/public/risp?window_games=5&view=html&team={team_code}">直近5試合</a>
            <a class="nav-btn {'active' if window_games==10 else ''}" href="/public/risp?window_games=10&view=html&team={team_code}">直近10試合</a>
          </div>
        </div>
        <div class="nav-section">
          <span class="nav-label">表示</span>
          <div class="nav-group">
            <a class="nav-btn{'' if show_season else ' active'}" href="/public/risp?window_games={window_games}&view=html&team={team_code}">直近</a>
            <a class="nav-btn{' active' if show_season else ''}" href="/public/risp?window_games={window_games}&view=season&team={team_code}">通算</a>
          </div>
        </div>
      </div>
      {_common_nav("risp", window_games, team_code)}
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
        body += _render_season_risp_html(window_games, team_code)
    return _html_page("得点圏・出塁・打点", body)


@router.get("/public/risp")
def public_risp(request: Request, window_games: int = 5, team: str = "広島", view: str | None = None):
    """指定チームの直近N試合の得点圏打率をYahoo Baseballテキスト速報から算出"""
    try:
        window_games = max(1, min(window_games, 10))
        data = _build_risp_data(window_games, team_code=team)
        if _wants_html(request, view):
            show_season = (view == "season")
            return _render_risp_html(data, window_games, show_season=show_season, team_code=team)
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
        canonical_path="/public/privacy",
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
      <p>本サービスは、NPBプロ野球12球団（セ・リーグ：広島東洋カープ・阪神タイガース・読売ジャイアンツ・横浜DeNAベイスターズ・中日ドラゴンズ・東京ヤクルトスワローズ、パ・リーグ：福岡ソフトバンクホークス・埼玉西武ライオンズ・東北楽天ゴールデンイーグルス・千葉ロッテマリーンズ・オリックス・バファローズ・北海道日本ハムファイターズ）の打撃成績・試合データを独自に集計・分析し、ファン向けの統計情報として提供する情報サイトです。</p>

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
        canonical_path="/public/terms",
    )
# ---------------------------------------------------------------------------

@router.get("/public/about", include_in_schema=False)
def public_about(request: Request):
    """サイト紹介・About ページ"""
    body = """
    <style>
      .about-wrap { max-width: 860px; margin: 0 auto; padding: 0 16px 60px; }
      .about-wrap h1 { font-size: 24px; font-weight: 700; color: #ffd54a; margin: 32px 0 8px; }
      .about-wrap h2 { font-size: 17px; font-weight: 700; color: #a0b8d8; margin: 32px 0 8px; border-left: 4px solid #ffd54a; padding-left: 12px; }
      .about-wrap h3 { font-size: 14px; font-weight: 700; color: #c8d8f4; margin: 20px 0 6px; }
      .about-wrap p, .about-wrap li { font-size: 14px; color: #8899b8; line-height: 1.9; margin: 8px 0; }
      .about-wrap ul { padding-left: 24px; }
      .about-wrap .updated { font-size: 12px; color: #5a6e94; margin-bottom: 24px; }
      .about-wrap a { color: #5b9bd5; text-decoration: none; }
      .about-wrap a:hover { text-decoration: underline; }
      .about-highlight {
        background: #0c1424; border: 1px solid #1a2540; border-left: 4px solid #ffd54a;
        border-radius: 8px; padding: 16px 20px; margin: 20px 0;
      }
      .about-highlight p { color: #c8d8f4; margin: 0; line-height: 1.8; }
      .feature-table { width: 100%; border-collapse: collapse; margin: 16px 0; }
      .feature-table th { background: #0f1d35; color: #8494b8; font-size: 12px; font-weight: 600;
        text-align: left; padding: 8px 12px; border-bottom: 1px solid #1a2540; }
      .feature-table td { color: #a0b4cc; font-size: 13px; padding: 9px 12px;
        border-bottom: 1px solid #111c30; vertical-align: top; line-height: 1.7; }
      .feature-table tr:last-child td { border-bottom: none; }
      .metric-grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(240px, 1fr)); gap: 12px; margin: 16px 0; }
      .metric-card { background: #0c1424; border: 1px solid #1a2540; border-radius: 8px; padding: 14px 16px; }
      .metric-name { font-size: 13px; font-weight: 700; color: #ffd54a; margin-bottom: 4px; }
      .metric-desc { font-size: 12px; color: #6878a0; line-height: 1.7; }
      .data-source-list { list-style: none; padding: 0; }
      .data-source-list li { display: flex; gap: 10px; align-items: flex-start;
        font-size: 13px; color: #8494b8; padding: 6px 0; border-bottom: 1px solid #111c30; }
      .data-source-list li:last-child { border-bottom: none; }
      .ds-badge { background: #1a3060; color: #7aa8d8; font-size: 10px; font-weight: 700;
        padding: 2px 7px; border-radius: 999px; white-space: nowrap; flex-shrink: 0; margin-top: 2px; }
      @media (max-width: 600px) {
        .about-wrap h1 { font-size: 20px; }
        .about-wrap h2 { font-size: 15px; }
        .metric-grid { grid-template-columns: 1fr; }
      }
    </style>
    <div class="about-wrap">
      <h1>このサイトについて</h1>
      <p class="updated">最終更新日：2025年6月1日</p>

      <div class="about-highlight">
        <p>
          「鯉男の打席分析室」は、NPBプロ野球12球団の打撃データをセイバーメトリクス指標で
          リアルタイム分析する非公式ファンサイトです。<br>
          直近試合のホットな打者・チームの打線構成を、データに基づいて客観的に可視化することを目的に運営しています。
        </p>
      </div>

      <h2>サイトの特徴</h2>
      <p>
        本サービスは、公開されているNPBの試合データを独自に収集・加工し、
        以下のような分析機能を12球団すべてに対して無料で提供しています。
      </p>

      <table class="feature-table">
        <thead>
          <tr><th>機能</th><th>概要</th></tr>
        </thead>
        <tbody>
          <tr>
            <td>📋 今日の予想打順</td>
            <td>直近5〜10試合のデータをベイズ補正してスコアリング。出塁率・長打力・守備力を打順役割（1番リードオフ / 4番クリーンアップ等）別ウェイトで評価し、最適な9人の打順を自動算出します。DH制対応。</td>
          </tr>
          <tr>
            <td>📊 直近打撃成績ランキング</td>
            <td>直近5〜10試合の打率・出塁率・OPS・ISO（長打指数）・wOBAをリアルタイム集計。シーズン通算との比較も可能です。</td>
          </tr>
          <tr>
            <td>🏃 得点圏・出塁・打点ランキング</td>
            <td>テキスト速報を独自解析し、得点圏打率・出塁率・打点を集計。シーズン通算ランキングも対応。</td>
          </tr>
          <tr>
            <td>🧤 走塁・守備指標</td>
            <td>UBR（走塁貢献度）・TZR（守備範囲得点）・捕手フレーミング等のセイバーメトリクス指標をシーズン通算で表示します。</td>
          </tr>
          <tr>
            <td>📈 WAR ランキング</td>
            <td>打撃・走塁・守備を総合した選手貢献度指標 WAR をランキング表示。シーズンを通じてチームに何勝もたらしたかを一覧で確認できます。</td>
          </tr>
          <tr>
            <td>🔥 ホットバッター</td>
            <td>直近の調子が突出して良い選手を独自スコアで抽出。wOBA・OPS・打率の直近上昇率を複合評価します。</td>
          </tr>
        </tbody>
      </table>

      <h2>使用している指標について</h2>
      <p>本サービスでは以下のセイバーメトリクス指標を中心に分析を行っています。</p>

      <div class="metric-grid">
        <div class="metric-card">
          <div class="metric-name">wOBA（加重出塁率）</div>
          <div class="metric-desc">単打・二塁打・本塁打・四球など各打席結果に得点価値ウェイトを付けた総合的な打撃指標。打者の実質的な攻撃力を測ります。</div>
        </div>
        <div class="metric-card">
          <div class="metric-name">ISO（純長打率）</div>
          <div class="metric-desc">長打率 − 打率。単打以外の「余分な塁打ち能力」を示す長打力指標です。</div>
        </div>
        <div class="metric-card">
          <div class="metric-name">OBP（出塁率）</div>
          <div class="metric-desc">安打・四球・死球による出塁の合計を打席数で割った値。1番・2番打者の評価に特に重要。</div>
        </div>
        <div class="metric-card">
          <div class="metric-name">OPS</div>
          <div class="metric-desc">出塁率 + 長打率。計算の単純さに比べて打者評価の精度が高い実用的指標。</div>
        </div>
        <div class="metric-card">
          <div class="metric-name">UBR（走塁貢献度）</div>
          <div class="metric-desc">走塁による得点貢献を数値化した指標。盗塁・進塁の判断力などを反映します。</div>
        </div>
        <div class="metric-card">
          <div class="metric-name">TZR（守備範囲得点）</div>
          <div class="metric-desc">守備位置ごとの平均的な守備者に対して何点分多く（少なく）守備で貢献したかを示す指標。</div>
        </div>
        <div class="metric-card">
          <div class="metric-name">WAR（勝利貢献度）</div>
          <div class="metric-desc">打撃・走塁・守備を総合し、平均的な選手に比べて何勝分チームに貢献したかを示す指標。</div>
        </div>
        <div class="metric-card">
          <div class="metric-name">ベイズ補正</div>
          <div class="metric-desc">直近打席数が少ない選手の指標をリーグ平均（事前分布）に向けて補正し、サンプル誤差を軽減する統計的手法。</div>
        </div>
      </div>

      <h2>データソース</h2>
      <ul class="data-source-list">
        <li>
          <span class="ds-badge">主要</span>
          <span>Yahoo!スポーツ（試合テキスト速報・打撃成績・出場選手情報）</span>
        </li>
        <li>
          <span class="ds-badge">補助</span>
          <span>NPB Basement（守備指標・走塁指標・WAR等のセイバーメトリクスデータ）</span>
        </li>
        <li>
          <span class="ds-badge">補助</span>
          <span>NPB公式サイト（一軍登録選手・守備位置情報）</span>
        </li>
      </ul>
      <p style="font-size:12px;color:#5a6e94;margin-top:8px">
        ※ 本サービスが提供するデータは上記ソースを独自集計・加工したものです。
        データの正確性・完全性については保証しかねます。情報は参考目的でご利用ください。
      </p>

      <h2>運営について</h2>
      <p>
        本サービスは、NPBプロ野球を愛するファンによる個人運営の非公式サイトです。
        NPB各球団および日本野球機構（NPB）とは一切関係ありません。
      </p>
      <p>
        サイト名の「鯉男」は広島東洋カープのファンを指すスラングに由来しますが、
        分析対象はセ・パ両リーグ全12球団を網羅しています。
      </p>
      <p>
        データ分析手法の改善・新機能の追加など、継続的にサービスを改善しています。
        ご意見・ご要望・不具合報告などは、各SNSやメール等でお気軽にお知らせください。
      </p>

      <h2>免責事項</h2>
      <p>
        本サービスが提供する予想打順・統計データはあくまで参考情報です。
        実際の試合結果・選手起用とは異なる場合があります。
        本サービスの利用により生じたいかなる損害についても、運営者は一切の責任を負いません。
      </p>

      <div style="margin-top:40px;display:flex;gap:16px;flex-wrap:wrap;">
        <a href="/public/top">← トップページへ戻る</a>
        <a href="/public/privacy">プライバシーポリシー</a>
        <a href="/public/terms">利用規約</a>
      </div>
    </div>
    """
    return _html_page(
        "このサイトについて",
        body,
        description="鯉男の打席分析室はNPBプロ野球12球団の打撃データをセイバーメトリクスで分析する非公式ファンサイトです。予想打順・wOBA・WAR・守備走塁指標をリアルタイムで提供します。",
        canonical_path="/public/about",
    )


# ─────────────────────────────────────────────
# トップ（表紙）ページ  /public/top
# ─────────────────────────────────────────────

# 球団別カラー・リーグ・愛称定義
_TEAM_INFO: list[dict] = [
    # セ・リーグ
    {"code": "広島",       "full": "広島東洋カープ",           "color": "#e4002b", "sub": "#fff", "emoji": "⚾", "league": "セ"},
    {"code": "阪神",       "full": "阪神タイガース",           "color": "#ffe100", "sub": "#222", "emoji": "⚾", "league": "セ"},
    {"code": "巨人",       "full": "読売ジャイアンツ",         "color": "#f97300", "sub": "#fff", "emoji": "⚾", "league": "セ"},
    {"code": "DeNA",       "full": "横浜DeNAベイスターズ",     "color": "#003087", "sub": "#fff", "emoji": "⚾", "league": "セ"},
    {"code": "中日",       "full": "中日ドラゴンズ",           "color": "#003087", "sub": "#fff", "emoji": "⚾", "league": "セ"},
    {"code": "ヤクルト",   "full": "東京ヤクルトスワローズ",   "color": "#00529b", "sub": "#fff", "emoji": "⚾", "league": "セ"},
    # パ・リーグ
    {"code": "ソフトバンク","full": "福岡ソフトバンクホークス","color": "#f5a623", "sub": "#222", "emoji": "⚾", "league": "パ"},
    {"code": "西武",       "full": "埼玉西武ライオンズ",       "color": "#00529b", "sub": "#fff", "emoji": "⚾", "league": "パ"},
    {"code": "楽天",       "full": "東北楽天ゴールデンイーグルス","color": "#8c1b37","sub": "#fff", "emoji": "⚾", "league": "パ"},
    {"code": "ロッテ",     "full": "千葉ロッテマリーンズ",     "color": "#000e2f", "sub": "#fff", "emoji": "⚾", "league": "パ"},
    {"code": "オリックス", "full": "オリックス・バファローズ", "color": "#0032a0", "sub": "#fff", "emoji": "⚾", "league": "パ"},
    {"code": "日本ハム",   "full": "北海道日本ハムファイターズ","color": "#003f8f", "sub": "#fff", "emoji": "⚾", "league": "パ"},
]


@router.get("/public/top", include_in_schema=False)
def public_top(request: Request):
    """サイトトップ（表紙）ページ — 12球団への入口 + サイト機能紹介"""

    def _team_card(t: dict) -> str:
        code = t["code"]
        full = t["full"]
        color = t["color"]
        sub   = t["sub"]
        # 各機能ページへのリンク（予想打順をメインに）
        href = f"/public/predicted-lineup?window_games=5&team={code}&view=html"
        # 略称バッジの最大2文字
        badge = code[:3] if code == "DeNA" else code[:3]
        return f"""
        <a class="team-card" href="{href}" style="--tc:{color};--ts:{sub};">
          <div class="tc-badge">{badge}</div>
          <div class="tc-full">{full}</div>
          <div class="tc-arrow">→</div>
        </a>"""

    central_cards = "".join(_team_card(t) for t in _TEAM_INFO if t["league"] == "セ")
    pacific_cards = "".join(_team_card(t) for t in _TEAM_INFO if t["league"] == "パ")

    body = f"""
    <style>
      /* ── トップページ固有スタイル ── */
      .top-hero {{
        text-align: center;
        padding: 40px 16px 32px;
      }}
      .top-hero-title {{
        font-size: clamp(22px, 5vw, 36px);
        font-weight: 900;
        color: #ffd54a;
        letter-spacing: -0.02em;
        line-height: 1.2;
      }}
      .top-hero-title span {{
        color: #c8d8f4;
        font-weight: 400;
        font-size: 0.55em;
        display: block;
        margin-top: 6px;
        letter-spacing: 0.02em;
      }}
      .top-hero-desc {{
        margin-top: 14px;
        font-size: 13px;
        color: #8494b8;
        line-height: 1.8;
        max-width: 520px;
        margin-left: auto;
        margin-right: auto;
      }}

      /* ── リーグセクション ── */
      .league-section {{
        margin-top: 32px;
      }}
      .league-label {{
        font-size: 11px;
        font-weight: 700;
        color: #5a6e94;
        letter-spacing: 0.12em;
        text-transform: uppercase;
        padding: 0 4px 8px;
        border-bottom: 1px solid #1a2540;
        margin-bottom: 12px;
      }}
      .team-grid {{
        display: grid;
        grid-template-columns: repeat(3, 1fr);
        gap: 10px;
      }}
      @media (max-width: 480px) {{
        .team-grid {{ grid-template-columns: repeat(2, 1fr); gap: 8px; }}
      }}

      /* ── 球団カード ── */
      .team-card {{
        display: flex;
        flex-direction: column;
        align-items: center;
        gap: 7px;
        padding: 16px 10px 14px;
        background: #0c1424;
        border: 1px solid #1a2540;
        border-top: 3px solid var(--tc);
        border-radius: 10px;
        text-decoration: none;
        color: #e8edf8;
        transition: transform 0.15s, border-color 0.15s, background 0.15s;
        position: relative;
        cursor: pointer;
      }}
      .team-card:hover {{
        transform: translateY(-3px);
        background: #101c30;
        border-color: var(--tc);
        box-shadow: 0 6px 20px rgba(0,0,0,0.4);
      }}
      .tc-badge {{
        background: var(--tc);
        color: var(--ts);
        font-size: 12px;
        font-weight: 900;
        padding: 3px 10px;
        border-radius: 999px;
        letter-spacing: 0.02em;
        white-space: nowrap;
      }}
      .tc-full {{
        font-size: 11px;
        color: #8494b8;
        text-align: center;
        line-height: 1.4;
      }}
      .tc-arrow {{
        font-size: 12px;
        color: #ffd54a;
        font-weight: 700;
      }}

      /* ── 機能紹介カード ── */
      .features-section {{
        margin-top: 40px;
      }}
      .features-title {{
        font-size: 16px;
        font-weight: 800;
        color: #c8d8f4;
        padding-bottom: 8px;
        border-bottom: 1px solid #1a2540;
        margin-bottom: 20px;
      }}
      .features-title::before {{
        content: "⚡";
        margin-right: 6px;
      }}
      .feature-list {{
        display: flex;
        flex-direction: column;
        gap: 14px;
      }}
      .feature-item {{
        display: flex;
        gap: 14px;
        align-items: flex-start;
        background: #0c1424;
        border: 1px solid #1a2540;
        border-radius: 10px;
        padding: 16px;
      }}
      .feature-icon {{
        font-size: 26px;
        flex-shrink: 0;
        width: 40px;
        text-align: center;
      }}
      .feature-body {{}}
      .feature-name {{
        font-size: 14px;
        font-weight: 800;
        color: #ffd54a;
        margin-bottom: 4px;
      }}
      .feature-desc {{
        font-size: 12px;
        color: #8494b8;
        line-height: 1.7;
      }}
      .feature-link {{
        display: inline-block;
        margin-top: 8px;
        font-size: 11px;
        color: #56cff8;
        border: 1px solid #1a4060;
        border-radius: 4px;
        padding: 2px 8px;
        text-decoration: none;
      }}
      .feature-link:hover {{ background: #1a4060; }}

      /* ── 使い方セクション ── */
      .howto-section {{
        margin-top: 40px;
        background: #0c1424;
        border: 1px solid #1a2540;
        border-radius: 12px;
        padding: 20px;
      }}
      .howto-title {{
        font-size: 14px;
        font-weight: 800;
        color: #c8d8f4;
        margin-bottom: 14px;
      }}
      .howto-title::before {{
        content: "📖";
        margin-right: 6px;
      }}
      .howto-steps {{
        display: flex;
        flex-direction: column;
        gap: 10px;
      }}
      .howto-step {{
        display: flex;
        gap: 12px;
        align-items: flex-start;
        font-size: 12px;
        color: #8494b8;
        line-height: 1.6;
      }}
      .step-num {{
        background: #ffd54a;
        color: #06100a;
        font-weight: 900;
        font-size: 11px;
        width: 20px;
        height: 20px;
        border-radius: 50%;
        display: flex;
        align-items: center;
        justify-content: center;
        flex-shrink: 0;
        margin-top: 1px;
      }}
    </style>

    <!-- ヒーローエリア -->
    <div class="top-hero">
      <div class="top-hero-title">
        鯉男の打席分析室
        <span>NPB 全12球団 データ分析ファンサイト</span>
      </div>
      <p class="top-hero-desc">
        直近5〜10試合の打撃成績・得点圏打率・守備走塁指標・WARを独自集計し、<br>
        AIスコアリングによる今日の予想打順を12球団分リアルタイムで提供します。
      </p>
    </div>

    <!-- 球団選択 セ・リーグ -->
    <div class="card">
      <div class="card-title">球団を選んでください</div>

      <div class="league-section">
        <div class="league-label">🔵 セントラル・リーグ</div>
        <div class="team-grid">
          {central_cards}
        </div>
      </div>

      <div class="league-section" style="margin-top:24px">
        <div class="league-label">🟠 パシフィック・リーグ</div>
        <div class="team-grid">
          {pacific_cards}
        </div>
      </div>
    </div>

    <!-- 機能紹介 -->
    <div class="features-section">
      <div class="features-title">このサイトでできること</div>
      <div class="feature-list">

        <div class="feature-item">
          <div class="feature-icon">📋</div>
          <div class="feature-body">
            <div class="feature-name">今日の予想打順</div>
            <div class="feature-desc">
              直近5試合の打撃データをベイズ補正してスコアリング。
              出塁率・長打率・守備力を打順ごとの役割（リードオフ/クリーンアップ等）に応じたウェイトで評価し、
              最適な9人の打順を自動算出します。DH制の有無も対応。
            </div>
            <a class="feature-link" href="/public/select?page=predicted-lineup">チームを選んで見る →</a>
          </div>
        </div>

        <div class="feature-item">
          <div class="feature-icon">📊</div>
          <div class="feature-body">
            <div class="feature-name">直近打撃成績ランキング</div>
            <div class="feature-desc">
              直近5〜10試合の打率・出塁率・OPS・ISO（長打指数）・wOBAをリアルタイム集計。
              シーズン通算成績との比較も可能。今ホットな打者が一目でわかります。
            </div>
            <a class="feature-link" href="/public/select?page=recent-batting">チームを選んで見る →</a>
          </div>
        </div>

        <div class="feature-item">
          <div class="feature-icon">🏃</div>
          <div class="feature-body">
            <div class="feature-name">得点圏・出塁・打点ランキング</div>
            <div class="feature-desc">
              ヤフースポーツのテキスト速報を独自解析し、得点圏打率・出塁率・打点を集計。
              今シーズン通算ランキングも対応。プレッシャー下での勝負強さを可視化します。
            </div>
            <a class="feature-link" href="/public/select?page=risp">チームを選んで見る →</a>
          </div>
        </div>

        <div class="feature-item">
          <div class="feature-icon">🧤</div>
          <div class="feature-body">
            <div class="feature-name">走塁・守備指標（UBR / TZR / Framing）</div>
            <div class="feature-desc">
              UBR（走塁貢献度）・TZR（守備範囲）・捕手フレーミング等の
              セイバーメトリクス指標をシーズン通算で表示。
              見えにくい貢献を数値で確認できます。
            </div>
            <a class="feature-link" href="/public/select?page=fielding-baserunning">チームを選んで見る →</a>
          </div>
        </div>

        <div class="feature-item">
          <div class="feature-icon">📈</div>
          <div class="feature-body">
            <div class="feature-name">WAR ランキング</div>
            <div class="feature-desc">
              打撃・走塁・守備を総合した選手貢献度指標 WAR をランキング表示。
              シーズンを通じてチームに何勝もたらしたかを一覧で確認できます。
            </div>
            <a class="feature-link" href="/public/select?page=war-ranking">チームを選んで見る →</a>
          </div>
        </div>

        <div class="feature-item">
          <div class="feature-icon">🔥</div>
          <div class="feature-body">
            <div class="feature-name">ホットバッター</div>
            <div class="feature-desc">
              直近の調子が突出して良い選手を独自スコアで抽出。
              wOBA・OPS・打率の直近上昇率を複合評価し、
              今日スタメンで注目すべき打者をピックアップします。
            </div>
            <a class="feature-link" href="/public/select?page=hot-batters">チームを選んで見る →</a>
          </div>
        </div>

      </div>
    </div>

    <!-- 使い方 -->
    <div class="howto-section">
      <div class="howto-title">使い方</div>
      <div class="howto-steps">
        <div class="howto-step">
          <div class="step-num">1</div>
          <div>上の球団カードから応援チームを選ぶ</div>
        </div>
        <div class="howto-step">
          <div class="step-num">2</div>
          <div>「予想打順」ページが開く — 直近データに基づく今日の打線が確認できる</div>
        </div>
        <div class="howto-step">
          <div class="step-num">3</div>
          <div>ページ上部のナビから「直近打撃」「得点圏」「守備走塁」「WAR」等に移動して詳細分析</div>
        </div>
        <div class="howto-step">
          <div class="step-num">4</div>
          <div>直近5試合 / 10試合のタブを切り替えてトレンドを確認</div>
        </div>
      </div>
    </div>
    """

    # トップページ専用の _html_page 呼び出し
    desc = "NPB全12球団の予想打順・直近打撃成績・得点圏打率・守備走塁・WARをリアルタイム分析するデータファンサイト。"
    return _html_page("トップ", body, description=desc)


# ─────────────────────────────────────────────
# チーム選択ページ  /public/select
# ─────────────────────────────────────────────

# 各ページの表示名・URLテンプレート定義
_PAGE_META: dict[str, dict] = {
    "predicted-lineup": {
        "label":    "今日の予想打順",
        "icon":     "📋",
        "url_tmpl": "/public/predicted-lineup?window_games=5&team={team}&view=html",
        "desc":     "直近データをAIスコアリングして最適な9人の打順を算出します。",
    },
    "recent-batting": {
        "label":    "直近打撃成績ランキング",
        "icon":     "📊",
        "url_tmpl": "/public/recent-batting?team={team}&view=html",
        "desc":     "直近5〜10試合の打率・OPS・wOBA等をリアルタイム集計します。",
    },
    "risp": {
        "label":    "得点圏・出塁・打点ランキング",
        "icon":     "🏃",
        "url_tmpl": "/public/risp?team={team}&view=html",
        "desc":     "得点圏打率・出塁率・打点を独自集計。シーズン通算対応。",
    },
    "fielding-baserunning": {
        "label":    "走塁・守備指標",
        "icon":     "🧤",
        "url_tmpl": "/public/fielding-baserunning?team={team}&view=html",
        "desc":     "UBR・TZR・捕手フレーミング等のセイバーメトリクス指標。",
    },
    "war-ranking": {
        "label":    "WAR ランキング",
        "icon":     "📈",
        "url_tmpl": "/public/war-ranking?team={team}&view=html",
        "desc":     "打撃・走塁・守備を総合した選手貢献度指標WARをランキング表示。",
    },
    "hot-batters": {
        "label":    "ホットバッター",
        "icon":     "🔥",
        "url_tmpl": "/public/hot-batters?team={team}&view=html",
        "desc":     "直近の調子が突出して良い選手を独自スコアで抽出します。",
    },
}


@router.get("/public/select", include_in_schema=False)
def public_select(request: Request, page: str = "predicted-lineup"):
    """チーム選択ページ — 機能を選んだあとチームを選ぶ中間ページ"""

    meta = _PAGE_META.get(page, _PAGE_META["predicted-lineup"])
    page_label = meta["label"]
    page_icon  = meta["icon"]
    page_desc  = meta["desc"]

    def _team_card(t: dict) -> str:
        code  = t["code"]
        full  = t["full"]
        color = t["color"]
        sub   = t["sub"]
        url   = meta["url_tmpl"].format(team=code)
        return f"""
        <a class="team-card" href="{url}" style="--tc:{color};--ts:{sub};">
          <div class="tc-badge">{code}</div>
          <div class="tc-full">{full}</div>
          <div class="tc-arrow">→</div>
        </a>"""

    central_cards = "".join(_team_card(t) for t in _TEAM_INFO if t["league"] == "セ")
    pacific_cards = "".join(_team_card(t) for t in _TEAM_INFO if t["league"] == "パ")

    # 他の機能への切替ナビ
    other_pages_html = ""
    for pg_key, pg_meta in _PAGE_META.items():
        active_cls = ' style="background:#1a2d50;border-color:#ffd54a;color:#ffd54a;"' if pg_key == page else ""
        other_pages_html += (
            f'<a class="nav-btn" href="/public/select?page={pg_key}"{active_cls}>'
            f'{pg_meta["icon"]} {pg_meta["label"]}</a>'
        )

    body = f"""
    <style>
      /* チーム選択ページ — /public/top と共通スタイルを再利用 */
      .select-hero {{
        text-align: center;
        padding: 28px 16px 20px;
      }}
      .select-back {{
        display: inline-flex;
        align-items: center;
        gap: 6px;
        font-size: 12px;
        color: #5a6e94;
        text-decoration: none;
        margin-bottom: 16px;
        border: 1px solid #1a2540;
        border-radius: 4px;
        padding: 4px 10px;
        transition: color 0.15s, background 0.15s;
      }}
      .select-back:hover {{ color: #c8d8f4; background: #0c1424; }}
      .select-page-name {{
        font-size: clamp(18px, 4vw, 26px);
        font-weight: 900;
        color: #ffd54a;
        margin-bottom: 6px;
      }}
      .select-page-desc {{
        font-size: 12px;
        color: #8494b8;
        margin-bottom: 0;
      }}
      .league-section {{ margin-top: 24px; }}
      .league-label {{
        font-size: 11px;
        font-weight: 700;
        color: #5a6e94;
        letter-spacing: 0.12em;
        padding: 0 4px 8px;
        border-bottom: 1px solid #1a2540;
        margin-bottom: 12px;
      }}
      .team-grid {{
        display: grid;
        grid-template-columns: repeat(3, 1fr);
        gap: 10px;
      }}
      @media (max-width: 480px) {{
        .team-grid {{ grid-template-columns: repeat(2, 1fr); gap: 8px; }}
      }}
      .team-card {{
        display: flex;
        flex-direction: column;
        align-items: center;
        gap: 7px;
        padding: 16px 10px 14px;
        background: #0c1424;
        border: 1px solid #1a2540;
        border-top: 3px solid var(--tc);
        border-radius: 10px;
        text-decoration: none;
        color: #e8edf8;
        transition: transform 0.15s, border-color 0.15s, background 0.15s;
        cursor: pointer;
      }}
      .team-card:hover {{
        transform: translateY(-3px);
        background: #101c30;
        border-color: var(--tc);
        box-shadow: 0 6px 20px rgba(0,0,0,0.4);
      }}
      .tc-badge {{
        background: var(--tc);
        color: var(--ts);
        font-size: 12px;
        font-weight: 900;
        padding: 3px 10px;
        border-radius: 999px;
        letter-spacing: 0.02em;
        white-space: nowrap;
      }}
      .tc-full {{ font-size: 11px; color: #8494b8; text-align: center; line-height: 1.4; }}
      .tc-arrow {{ font-size: 12px; color: #ffd54a; font-weight: 700; }}
      /* 他機能切替ナビ */
      .other-pages {{
        margin-top: 32px;
        padding-top: 20px;
        border-top: 1px solid #1a2540;
      }}
      .other-pages-title {{
        font-size: 11px;
        color: #5a6e94;
        margin-bottom: 10px;
        letter-spacing: 0.08em;
      }}
      .other-pages-nav {{
        display: flex;
        flex-wrap: wrap;
        gap: 8px;
      }}
    </style>

    <!-- 戻るリンク + ページタイトル -->
    <div class="select-hero">
      <a class="select-back" href="/public/top">← 機能一覧に戻る</a>
      <div class="select-page-name">{page_icon} {page_label}</div>
      <p class="select-page-desc">{page_desc}<br>チームを選ぶと該当ページへ移動します。</p>
    </div>

    <!-- 球団選択カード -->
    <div class="card">
      <div class="card-title">チームを選んでください</div>

      <div class="league-section">
        <div class="league-label">🔵 セントラル・リーグ</div>
        <div class="team-grid">{central_cards}</div>
      </div>

      <div class="league-section" style="margin-top:24px">
        <div class="league-label">🟠 パシフィック・リーグ</div>
        <div class="team-grid">{pacific_cards}</div>
      </div>
    </div>

    <!-- 他の機能への切替 -->
    <div class="other-pages">
      <div class="other-pages-title">他の機能を見る</div>
      <div class="other-pages-nav">
        {other_pages_html}
      </div>
    </div>
    """

    desc_meta = f"NPB全12球団の{page_label}をチームごとに確認できます。"
    return _html_page(f"{page_label} — チーム選択", body, description=desc_meta)


# ─────────────────────────────────────────────
# 起動時ウォームアップ
# ─────────────────────────────────────────────

def warmup_cache() -> None:
    """
    サーバー起動直後にバックグラウンドスレッドでキャッシュを温める。
    最初のユーザーリクエストが来る前にデータを用意することで初回表示を高速化する。

    方針:
    - 広島は優先スレッドで ①〜⑥ を順番に実行（既存動作を維持）
    - 残り11球団はチームごとに独立したデーモンスレッドで並列実行
      各スレッドは player_profile → recent_games → recent_batting → predicted_lineup の順
      1チームの失敗が他チームに影響しないよう try/except を独立させている
    - 共有リソース（_get_active_first_team_position_players）は広島スレッドのみで実行
      _get_player_defense, _get_season_position_batting は team_code 別になったため
      各球団の _warmup_team(tc) 内で個別に呼び出す
    """

    # --- チーム別ウォームアップ（全球団共通処理） ---
    def _warmup_team(tc: str) -> None:
        """1球団分の team_code 依存キャッシュをプリフェッチする。"""
        try:
            # player_profile（proran.jp スクレイピング）
            _get_player_profile(tc)
            print(f"[warmup:{tc}] player_profile OK")
        except Exception as e:
            print(f"[warmup:{tc}] player_profile error:", e)

        try:
            # 直近試合リスト（NPB試合結果ページ）
            _fetch_recent_carp_games(limit=10, team_code=tc)
            print(f"[warmup:{tc}] recent_games OK")
        except Exception as e:
            print(f"[warmup:{tc}] recent_games error:", e)

        try:
            # 直近打撃成績集計（window=5）
            _aggregate_recent_batting_stats(window_games=5, team_code=tc)
            print(f"[warmup:{tc}] recent_batting OK")
        except Exception as e:
            print(f"[warmup:{tc}] recent_batting error:", e)

        try:
            # シーズン守備指標（team_code 別）
            _get_player_defense(tc)
            print(f"[warmup:{tc}] player_defense OK")
        except Exception as e:
            print(f"[warmup:{tc}] player_defense error:", e)

        try:
            # シーズン打撃成績（team_code 別）
            _get_season_position_batting(tc)
            print(f"[warmup:{tc}] season_position_batting OK")
        except Exception as e:
            print(f"[warmup:{tc}] season_position_batting error:", e)

        try:
            # 予想打順（最も重いメイン処理）
            _build_simple_predicted_lineup(window_games=5, use_dh=True, team_code=tc)
            print(f"[warmup:{tc}] predicted_lineup OK")
        except Exception as e:
            print(f"[warmup:{tc}] predicted_lineup error:", e)

        print(f"[warmup:{tc}] done")

    # --- 広島優先スレッド（①〜⑥ を順番に実行） ---
    def _warmup_carp() -> None:
        try:
            print("[warmup] start (広島優先)")

            # ① 一軍登録選手（NPB公示ページ・チーム非依存）
            try:
                _get_active_first_team_position_players()
                print("[warmup] first_team OK")
            except Exception as e:
                print("[warmup] first_team error:", e)

            # ②③ 広島分を _warmup_team で実行（player_defense / season_position_batting も含む）
            _warmup_team("広島")

            print("[warmup] 広島 all done")

            # 残り11球団を並列バックグラウンドスレッドで実行
            # 広島スレッドが ①〜③ のキャッシュを温めた後に起動することで
            # 共有リソースの競合を最小化する
            other_teams = [tc for tc in YAHOO_TEAM_ID if tc != "広島"]
            for tc in other_teams:
                threading.Thread(
                    target=_warmup_team,
                    args=(tc,),
                    daemon=True,
                    name=f"warmup-{tc}",
                ).start()

        except Exception as e:
            print("[warmup] unexpected error:", e)

    t = threading.Thread(target=_warmup_carp, daemon=True, name="warmup-広島")
    t.start()

