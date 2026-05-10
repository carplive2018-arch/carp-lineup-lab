from __future__ import annotations

import re
from html import unescape
from html.parser import HTMLParser
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

    table_matches = re.findall(
        r'<table[^>]*class="terhdtbl"[^>]*>(.*?)</table>',
        html,
        flags=re.DOTALL | re.IGNORECASE,
    )

    for table_html in table_matches:
        row_matches = re.findall(
            r'<tr[^>]*class="terlist"[^>]*>(.*?)</tr>',
            table_html,
            flags=re.DOTALL | re.IGNORECASE,
        )

        for row_html in row_matches:
            cells = re.findall(
                r"<t[dh][^>]*>(.*?)</t[dh]>",
                row_html,
                flags=re.DOTALL | re.IGNORECASE,
            )
            cleaned = [_clean_text(cell) for cell in cells]
            if cleaned:
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

    for box_url in _extract_current_scoreboard_game_urls(current_html):
        meta = _build_game_meta_from_box_url(box_url)
        all_games.append({
            "date": meta["date"],
            "date_sort": meta["date_sort"],
            "opponent": meta["opponent"],
            "venue": meta["venue"],
            "round": meta["round"],
            "score": "",
            "result": "",
            "box_url": box_url,
        })

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
def recent_5_batting_stats() -> JSONResponse:
    try:
        data = _aggregate_recent_batting_stats(5)
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


@router.get("/api/stats/batting/recent-10")
def recent_10_batting_stats() -> JSONResponse:
    try:
        data = _aggregate_recent_batting_stats(10)
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


@router.get("/", response_class=HTMLResponse)
def index() -> HTMLResponse:
    return _layout(
        "Carp Lineup Lab",
        """
        <span class="pill">β版 / 非公式</span>
        <h1>Carp Lineup Lab</h1>
        <p class="muted">上に直近5試合の実際のスタメン、下に今日の予想スタメンを表示します。</p>

        <div class="card">
          <h2>直近5試合の実際のスタメン</h2>
          <p id="actual-status" class="muted">読み込み中...</p>
          <div id="actual-games" class="grid"></div>
        </div>

        <div class="card">
          <h2>今日の予想スタメン</h2>
          <p id="today-status" class="muted">読み込み中...</p>
          <div id="today-lineup" class="grid"></div>
        </div>

        <div class="card">
          <h2>新しい打撃成績API</h2>
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
            <li><a href="/api/lineups/today">予想スタメンAPI</a></li>
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

          async function loadTodayLineup() {
            const statusEl = document.getElementById("today-status");
            const lineupEl = document.getElementById("today-lineup");

            try {
              const res = await fetch("/api/lineups/today");
              const data = await res.json();

              if (!data.lineup || !Array.isArray(data.lineup) || data.lineup.length === 0) {
                statusEl.textContent = "今日の予想スタメンを取得できませんでした。";
                return;
              }

              statusEl.innerHTML =
                '更新元: <strong>' + data.source + '</strong> / 人数: <strong>' + data.count + '</strong>';

              lineupEl.innerHTML = data.lineup.map(player => `
                <div class="game-card">
                  <div style="display:flex; justify-content:space-between; align-items:flex-start; gap:12px; flex-wrap:wrap;">
                    <div>
                      <div class="small">${player.batting_order}番 / ${player.position}</div>
                      <div class="date" style="margin-bottom:4px;">${player.player_name}</div>
                    </div>
                    <div style="text-align:right;">
                      <div class="small">recent score</div>
                      <div style="font-size:20px; font-weight:700;">${player.recent_score}</div>
                    </div>
                  </div>
                  <div class="muted">${player.reason}</div>
                </div>
              `).join("");
            } catch (e) {
              statusEl.textContent = "今日の予想スタメンを表示できませんでした。";
            }
          }

          loadActualLineups();
          loadTodayLineup();
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
