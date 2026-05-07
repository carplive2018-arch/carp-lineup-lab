from __future__ import annotations

import os
import re
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any
from urllib.parse import urljoin

import httpx
from bs4 import BeautifulSoup
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.services.metric_service import BattingStats


TEAM_CODE_MAP = {
    "広島": "C",
    "広島東洋カープ": "C",
    "阪神": "T",
    "読売": "G",
    "巨人": "G",
    "DeNA": "DB",
    "横浜DeNA": "DB",
    "ヤクルト": "S",
    "中日": "D",
    "オリックス": "B",
    "ソフトバンク": "H",
    "日本ハム": "F",
    "ロッテ": "M",
    "楽天": "E",
    "西武": "L",
}

POSITION_ALIASES = {
    "捕": "C",
    "一": "1B",
    "二": "2B",
    "三": "3B",
    "遊": "SS",
    "左": "LF",
    "中": "CF",
    "右": "RF",
    "投": "P",
    "指": "DH",
    "c": "C",
    "1b": "1B",
    "2b": "2B",
    "3b": "3B",
    "ss": "SS",
    "lf": "LF",
    "cf": "CF",
    "rf": "RF",
    "p": "P",
    "dh": "DH",
}


@dataclass
class ParsedPlayerBatting:
    order: int | None
    player_name: str
    position: str | None
    pa: int = 0
    ab: int = 0
    runs: int = 0
    hits: int = 0
    doubles: int = 0
    triples: int = 0
    home_runs: int = 0
    rbi: int = 0
    walks: int = 0
    intentional_walks: int = 0
    hit_by_pitch: int = 0
    strikeouts: int = 0
    sac_bunts: int = 0
    sac_flies: int = 0
    stolen_bases: int = 0
    caught_stealing: int = 0
    gidp: int = 0


@dataclass
class ParsedGamePayload:
    game_date: date
    squad_level: str
    opponent_team_name: str
    opponent_team_code: str
    venue: str | None
    home_away: str | None
    source_url: str
    source_site: str
    runs_for: int
    runs_against: int
    lineup_rows: list[dict[str, Any]]
    batting_rows: list[ParsedPlayerBatting]
    fielding_rows: list[dict[str, Any]]


class StatsRepository:
    def __init__(self, db: Session | None = None, use_mock_if_empty: bool = True) -> None:
        self.db = db
        self.use_mock_if_empty = use_mock_if_empty
        self._sync_cache: dict[str, list[ParsedGamePayload]] = {}

    # -----------------------------------------------------
    # public read API for lineup service
    # -----------------------------------------------------

    def get_player_inputs_for_lineup(self, opponent_pitcher_hand: str | None) -> list[dict[str, Any]]:
        rows = self._get_player_inputs_from_db(opponent_pitcher_hand)
        if rows or not self.use_mock_if_empty:
            return rows
        return self._get_mock_player_inputs(opponent_pitcher_hand)

    def _get_player_inputs_from_db(self, opponent_pitcher_hand: str | None) -> list[dict[str, Any]]:
        if self.db is None:
            return []

        season_year = date.today().year
        player_rows = self.db.execute(
            text(
                """
                SELECT
                    p.id AS player_id,
                    p.player_name,
                    COALESCE(NULLIF(p.bats, ''), 'R') AS bats
                FROM players p
                WHERE p.team_code = 'C'
                ORDER BY p.id
                """
            )
        ).mappings().all()

        results: list[dict[str, Any]] = []

        for player in player_rows:
            batting_games = self.db.execute(
                text(
                    """
                    SELECT
                        g.game_date,
                        b.pa,
                        b.ab,
                        b.runs,
                        b.hits,
                        b.doubles,
                        b.triples,
                        b.home_runs,
                        b.rbi,
                        b.walks,
                        b.intentional_walks,
                        b.hit_by_pitch,
                        b.strikeouts,
                        b.sac_bunts,
                        b.sac_flies,
                        b.stolen_bases,
                        b.caught_stealing,
                        b.gidp
                    FROM player_game_batting_stats b
                    JOIN games g ON g.id = b.game_id
                    WHERE b.player_id = :player_id
                      AND EXTRACT(YEAR FROM g.game_date) = :season_year
                    ORDER BY g.game_date DESC, g.id DESC
                    """
                ),
                {"player_id": player["player_id"], "season_year": season_year},
            ).mappings().all()

            if not batting_games:
                continue

            fielding_rows = self.db.execute(
                text(
                    """
                    SELECT
                        f.position,
                        SUM(f.innings_defended) AS innings_defended,
                        SUM(CASE WHEN f.started_flag THEN 1 ELSE 0 END) AS starts
                    FROM player_game_fielding_stats f
                    JOIN games g ON g.id = f.game_id
                    WHERE f.player_id = :player_id
                      AND EXTRACT(YEAR FROM g.game_date) = :season_year
                    GROUP BY f.position
                    ORDER BY SUM(f.innings_defended) DESC, f.position
                    """
                ),
                {"player_id": player["player_id"], "season_year": season_year},
            ).mappings().all()

            eligible_positions = [str(r["position"]) for r in fielding_rows if r["position"]]
            if not eligible_positions:
                eligible_positions = ["DH"]

            defense_by_position = self._calc_defense_by_position(fielding_rows)
            defense_score = self._calc_defense_score(fielding_rows)
            position_scarcity = round(1.0 / max(1, len(set(eligible_positions))), 6)
            recent_playing_time = self._calc_recent_playing_time(batting_games)
            handedness_bonus = self._calc_handedness_bonus(player["bats"], opponent_pitcher_hand)

            last_5 = self._aggregate_batting_stats(batting_games[:5])
            last_10 = self._aggregate_batting_stats(batting_games[:10])
            last_15 = self._aggregate_batting_stats(batting_games[:15])
            season = self._aggregate_batting_stats(batting_games)

            results.append(
                {
                    "player_id": int(player["player_id"]),
                    "player_name": str(player["player_name"]),
                    "eligible_positions": eligible_positions,
                    "last_5": last_5,
                    "last_10": last_10,
                    "last_15": last_15,
                    "season": season,
                    "defense_score": defense_score,
                    "position_scarcity": position_scarcity,
                    "recent_playing_time": recent_playing_time,
                    "opponent_handedness_bonus": handedness_bonus,
                    "defense_by_position": defense_by_position,
                }
            )

        return results

    # -----------------------------------------------------
    # public sync API for jobs
    # -----------------------------------------------------

    def sync_daily_data(self, target_date: date) -> dict[str, Any]:
        payloads = self._load_daily_payloads(target_date)
        games = self.sync_games_for_date(target_date)
        lineups = self.sync_game_lineups_for_date(target_date)
        batting = self.sync_batting_stats_for_date(target_date)
        fielding = self.sync_fielding_stats_for_date(target_date)
        return {
            "target_date": target_date.isoformat(),
            "payloads": len(payloads),
            "games": games.get("games_upserted", 0),
            "lineups": lineups.get("lineup_rows_upserted", 0),
            "batting": batting.get("batting_rows_upserted", 0),
            "fielding": fielding.get("fielding_rows_upserted", 0),
        }

    def sync_games_for_date(self, target_date: date) -> dict[str, Any]:
        if self.db is None:
            raise RuntimeError("DB session is required")
        payloads = self._load_daily_payloads(target_date)
        count = 0
        for payload in payloads:
            self._upsert_game(payload)
            count += 1
        return {"games_upserted": count}

    def sync_game_lineups_for_date(self, target_date: date) -> dict[str, Any]:
        if self.db is None:
            raise RuntimeError("DB session is required")
        payloads = self._load_daily_payloads(target_date)
        count = 0
        for payload in payloads:
            game_id = self._find_game_id(payload)
            if game_id is None:
                game_id = self._upsert_game(payload)
            for row in payload.lineup_rows:
                player_id = self._upsert_player(row["player_name"])
                self._upsert_game_lineup(
                    game_id=game_id,
                    player_id=player_id,
                    batting_order=row.get("batting_order"),
                    position=row.get("position"),
                    starter_flag=bool(row.get("starter_flag", True)),
                )
                count += 1
        return {"lineup_rows_upserted": count}

    def sync_batting_stats_for_date(self, target_date: date) -> dict[str, Any]:
        if self.db is None:
            raise RuntimeError("DB session is required")
        payloads = self._load_daily_payloads(target_date)
        count = 0
        for payload in payloads:
            game_id = self._find_game_id(payload)
            if game_id is None:
                game_id = self._upsert_game(payload)
            for row in payload.batting_rows:
                player_id = self._upsert_player(row.player_name)
                self._upsert_batting_stats(
                    game_id=game_id,
                    player_id=player_id,
                    squad_level=payload.squad_level,
                    row=row,
                )
                count += 1
        return {"batting_rows_upserted": count}

    def sync_fielding_stats_for_date(self, target_date: date) -> dict[str, Any]:
        if self.db is None:
            raise RuntimeError("DB session is required")
        payloads = self._load_daily_payloads(target_date)
        count = 0
        for payload in payloads:
            game_id = self._find_game_id(payload)
            if game_id is None:
                game_id = self._upsert_game(payload)
            for row in payload.fielding_rows:
                player_id = self._upsert_player(str(row["player_name"]))
                self._upsert_fielding_stats(
                    game_id=game_id,
                    player_id=player_id,
                    squad_level=payload.squad_level,
                    position=str(row["position"]),
                    innings_defended=float(row.get("innings_defended", 0.0)),
                    started_flag=bool(row.get("started_flag", False)),
                )
                count += 1
        return {"fielding_rows_upserted": count}

    # -----------------------------------------------------
    # sync helpers
    # -----------------------------------------------------

    def _load_daily_payloads(self, target_date: date) -> list[ParsedGamePayload]:
        cache_key = target_date.isoformat()
        if cache_key in self._sync_cache:
            return self._sync_cache[cache_key]

        urls = self._build_daily_source_urls(target_date)
        payloads: list[ParsedGamePayload] = []
        for meta in urls:
            try:
                html = self._fetch_html(meta["url"])
                payload = self._parse_game_page(
                    html=html,
                    target_date=target_date,
                    source_url=meta["url"],
                    squad_level=meta["squad_level"],
                    source_site=meta["source_site"],
                )
                if payload.batting_rows:
                    payloads.append(payload)
            except Exception as exc:  # noqa: BLE001
                # 1件落ちても残りは進める
                print(f"[WARN] source parse failed: {meta['url']} {exc}")

        self._sync_cache[cache_key] = payloads
        return payloads

    def _build_daily_source_urls(self, target_date: date) -> list[dict[str, str]]:
        ymd = target_date.strftime("%Y%m%d")
        urls = [
            {
                "url": f"https://www.carp.co.jp/farm/schedule/result/{ymd}",
                "squad_level": "2gun",
                "source_site": "carp",
            }
        ]

        first_team_template = os.getenv("CARP_1GUN_RESULT_URL_TEMPLATE", "").strip()
        first_team_url = os.getenv("CARP_1GUN_RESULT_URL", "").strip()
        extra_urls = os.getenv("EXTRA_GAME_SOURCE_URLS", "").strip()

        if first_team_template:
            urls.append(
                {
                    "url": first_team_template.format(ymd=ymd, yyyy=ymd[:4], mm=ymd[4:6], dd=ymd[6:8]),
                    "squad_level": "1gun",
                    "source_site": "custom",
                }
            )
        elif first_team_url:
            urls.append({"url": first_team_url, "squad_level": "1gun", "source_site": "custom"})

        if extra_urls:
            for raw in extra_urls.split(","):
                url = raw.strip()
                if not url:
                    continue
                urls.append({"url": url, "squad_level": "1gun", "source_site": "custom"})

        return urls

    def _fetch_html(self, url: str) -> str:
        headers = {"User-Agent": "CarpLineupLabBot/1.0 (+non-commercial analysis)"}
        with httpx.Client(timeout=20, follow_redirects=True, headers=headers) as client:
            response = client.get(url)
            response.raise_for_status()
            response.encoding = response.encoding or "utf-8"
            return response.text

    def _parse_game_page(
        self,
        html: str,
        target_date: date,
        source_url: str,
        squad_level: str,
        source_site: str,
    ) -> ParsedGamePayload:
        soup = BeautifulSoup(html, "html.parser")
        title_text = self._collect_title_text(soup)
        body_text = soup.get_text(" ", strip=True)
        opponent_name = self._extract_opponent_name(title_text + " " + body_text)
        opponent_code = TEAM_CODE_MAP.get(opponent_name, opponent_name[:3] if opponent_name else "UNK")
        venue = self._extract_venue(title_text + " " + body_text)
        runs_for, runs_against = self._extract_score(title_text + " " + body_text)
        batting_rows = self._extract_batting_rows(soup)
        lineup_rows = self._build_lineup_rows_from_batting(batting_rows)
        fielding_rows = self._build_fielding_rows_from_lineup(lineup_rows)
        return ParsedGamePayload(
            game_date=target_date,
            squad_level=squad_level,
            opponent_team_name=opponent_name or "不明",
            opponent_team_code=opponent_code,
            venue=venue,
            home_away="home" if "広島" in title_text or "マツダ" in title_text else None,
            source_url=source_url,
            source_site=source_site,
            runs_for=runs_for,
            runs_against=runs_against,
            lineup_rows=lineup_rows,
            batting_rows=batting_rows,
            fielding_rows=fielding_rows,
        )

    def _collect_title_text(self, soup: BeautifulSoup) -> str:
        chunks: list[str] = []
        for selector in ("title", "h1", "h2"):
            for node in soup.select(selector):
                text_value = node.get_text(" ", strip=True)
                if text_value:
                    chunks.append(text_value)
        return " ".join(chunks)

    def _extract_opponent_name(self, text_value: str) -> str | None:
        for name in TEAM_CODE_MAP:
            if name != "広島" and name in text_value:
                return name
        return None

    def _extract_venue(self, text_value: str) -> str | None:
        m = re.search(r"(?:球場|スタジアム|ドーム|マツダ)[^\s、。]*", text_value)
        return m.group(0) if m else None

    def _extract_score(self, text_value: str) -> tuple[int, int]:
        m = re.search(r"(\d+)\s*[-ー]\s*(\d+)", text_value)
        if not m:
            return 0, 0
        return int(m.group(1)), int(m.group(2))

    def _extract_batting_rows(self, soup: BeautifulSoup) -> list[ParsedPlayerBatting]:
        rows: list[ParsedPlayerBatting] = []
        for table in soup.find_all("table"):
            headers = [self._norm(cell.get_text(" ", strip=True)) for cell in table.find_all("th")]
            if not self._looks_like_batting_table(headers):
                continue

            trs = table.find_all("tr")
            if not trs:
                continue
            header_cells = [self._norm(x.get_text(" ", strip=True)) for x in trs[0].find_all(["th", "td"])]
            header_index = {name: idx for idx, name in enumerate(header_cells)}

            for tr in trs[1:]:
                cells = [td.get_text(" ", strip=True) for td in tr.find_all(["td", "th"])]
                if not cells or len(cells) < 2:
                    continue
                row = self._parse_batting_row(cells, header_index)
                if row is not None:
                    rows.append(row)
            if rows:
                break
        return rows

    def _looks_like_batting_table(self, headers: list[str]) -> bool:
        header_text = " ".join(headers)
        if "選手" not in header_text:
            return False
        required_hits = 0
        for token in ("打席", "打数", "安打", "四球", "三振"):
            if token in header_text:
                required_hits += 1
        return required_hits >= 3

    def _parse_batting_row(self, cells: list[str], header_index: dict[str, int]) -> ParsedPlayerBatting | None:
        player_name = self._value_by_candidates(cells, header_index, ["選手", "打者", "氏名"])
        if not player_name or player_name in {"合計", "計"}:
            return None
        player_name = self._clean_player_name(player_name)

        order_raw = self._value_by_candidates(cells, header_index, ["打順", "順"])
        position_raw = self._value_by_candidates(cells, header_index, ["守備", "位置", "POS"])

        return ParsedPlayerBatting(
            order=self._to_int(order_raw),
            player_name=player_name,
            position=self._normalize_position(position_raw),
            pa=self._to_int(self._value_by_candidates(cells, header_index, ["打席"])) or 0,
            ab=self._to_int(self._value_by_candidates(cells, header_index, ["打数"])) or 0,
            runs=self._to_int(self._value_by_candidates(cells, header_index, ["得点"])) or 0,
            hits=self._to_int(self._value_by_candidates(cells, header_index, ["安打"])) or 0,
            doubles=self._to_int(self._value_by_candidates(cells, header_index, ["二塁打"])) or 0,
            triples=self._to_int(self._value_by_candidates(cells, header_index, ["三塁打"])) or 0,
            home_runs=self._to_int(self._value_by_candidates(cells, header_index, ["本塁打"])) or 0,
            rbi=self._to_int(self._value_by_candidates(cells, header_index, ["打点"])) or 0,
            walks=self._to_int(self._value_by_candidates(cells, header_index, ["四球"])) or 0,
            intentional_walks=self._to_int(self._value_by_candidates(cells, header_index, ["故意四"])) or 0,
            hit_by_pitch=self._to_int(self._value_by_candidates(cells, header_index, ["死球"])) or 0,
            strikeouts=self._to_int(self._value_by_candidates(cells, header_index, ["三振"])) or 0,
            sac_bunts=self._to_int(self._value_by_candidates(cells, header_index, ["犠打"])) or 0,
            sac_flies=self._to_int(self._value_by_candidates(cells, header_index, ["犠飛"])) or 0,
            stolen_bases=self._to_int(self._value_by_candidates(cells, header_index, ["盗塁"])) or 0,
            caught_stealing=self._to_int(self._value_by_candidates(cells, header_index, ["盗塁刺"])) or 0,
            gidp=self._to_int(self._value_by_candidates(cells, header_index, ["併殺打"])) or 0,
        )

    def _build_lineup_rows_from_batting(self, batting_rows: list[ParsedPlayerBatting]) -> list[dict[str, Any]]:
        lineup_rows: list[dict[str, Any]] = []
        for row in batting_rows:
            if row.order is None or row.order < 1 or row.order > 9:
                continue
            lineup_rows.append(
                {
                    "batting_order": row.order,
                    "player_name": row.player_name,
                    "position": row.position,
                    "starter_flag": True,
                }
            )
        return lineup_rows

    def _build_fielding_rows_from_lineup(self, lineup_rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        for row in lineup_rows:
            position = row.get("position")
            if not position or position == "DH":
                continue
            rows.append(
                {
                    "player_name": row["player_name"],
                    "position": position,
                    "innings_defended": 9.0,
                    "started_flag": True,
                }
            )
        return rows

    # -----------------------------------------------------
    # DB upserts
    # -----------------------------------------------------

    def _upsert_player(self, player_name: str) -> int:
        assert self.db is not None
        existing = self.db.execute(
            text(
                "SELECT id FROM players WHERE team_code = 'C' AND player_name = :player_name ORDER BY id LIMIT 1"
            ),
            {"player_name": player_name},
        ).scalar_one_or_none()
        if existing is not None:
            return int(existing)

        inserted = self.db.execute(
            text(
                """
                INSERT INTO players (player_name, team_code, created_at, updated_at)
                VALUES (:player_name, 'C', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
                ON CONFLICT (team_code, player_name)
                DO UPDATE SET updated_at = CURRENT_TIMESTAMP
                RETURNING id
                """
            ),
            {"player_name": player_name},
        ).scalar_one()
        return int(inserted)

    def _upsert_game(self, payload: ParsedGamePayload) -> int:
        assert self.db is not None
        sql = text(
            """
            INSERT INTO games (
                game_date, team_code, squad_level, opponent_team_code, opponent_team_name,
                source_url, source_site, venue, home_away, game_status,
                runs_for, runs_against, created_at, updated_at
            ) VALUES (
                :game_date, 'C', :squad_level, :opponent_team_code, :opponent_team_name,
                :source_url, :source_site, :venue, :home_away, 'final',
                :runs_for, :runs_against, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP
            )
            ON CONFLICT (game_date, squad_level, opponent_team_name, source_url)
            DO UPDATE SET
                opponent_team_code = EXCLUDED.opponent_team_code,
                venue = EXCLUDED.venue,
                home_away = EXCLUDED.home_away,
                runs_for = EXCLUDED.runs_for,
                runs_against = EXCLUDED.runs_against,
                updated_at = CURRENT_TIMESTAMP
            RETURNING id
            """
        )
        return int(
            self.db.execute(
                sql,
                {
                    "game_date": payload.game_date,
                    "squad_level": payload.squad_level,
                    "opponent_team_code": payload.opponent_team_code,
                    "opponent_team_name": payload.opponent_team_name,
                    "source_url": payload.source_url,
                    "source_site": payload.source_site,
                    "venue": payload.venue,
                    "home_away": payload.home_away,
                    "runs_for": payload.runs_for,
                    "runs_against": payload.runs_against,
                },
            ).scalar_one()
        )

    def _find_game_id(self, payload: ParsedGamePayload) -> int | None:
        assert self.db is not None
        return self.db.execute(
            text(
                """
                SELECT id
                FROM games
                WHERE game_date = :game_date
                  AND squad_level = :squad_level
                  AND opponent_team_name = :opponent_team_name
                  AND source_url = :source_url
                LIMIT 1
                """
            ),
            {
                "game_date": payload.game_date,
                "squad_level": payload.squad_level,
                "opponent_team_name": payload.opponent_team_name,
                "source_url": payload.source_url,
            },
        ).scalar_one_or_none()

    def _upsert_game_lineup(
        self,
        game_id: int,
        player_id: int,
        batting_order: int | None,
        position: str | None,
        starter_flag: bool,
    ) -> None:
        assert self.db is not None
        self.db.execute(
            text(
                """
                INSERT INTO game_lineups (
                    game_id, player_id, batting_order, position, starter_flag, created_at, updated_at
                ) VALUES (
                    :game_id, :player_id, :batting_order, :position, :starter_flag, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP
                )
                ON CONFLICT (game_id, player_id)
                DO UPDATE SET
                    batting_order = EXCLUDED.batting_order,
                    position = EXCLUDED.position,
                    starter_flag = EXCLUDED.starter_flag,
                    updated_at = CURRENT_TIMESTAMP
                """
            ),
            {
                "game_id": game_id,
                "player_id": player_id,
                "batting_order": batting_order,
                "position": position,
                "starter_flag": starter_flag,
            },
        )

    def _upsert_batting_stats(self, game_id: int, player_id: int, squad_level: str, row: ParsedPlayerBatting) -> None:
        assert self.db is not None
        self.db.execute(
            text(
                """
                INSERT INTO player_game_batting_stats (
                    game_id, player_id, squad_level, batting_order, position,
                    pa, ab, runs, hits, doubles, triples, home_runs, rbi,
                    walks, intentional_walks, hit_by_pitch, strikeouts,
                    sac_bunts, sac_flies, stolen_bases, caught_stealing, gidp,
                    created_at, updated_at
                ) VALUES (
                    :game_id, :player_id, :squad_level, :batting_order, :position,
                    :pa, :ab, :runs, :hits, :doubles, :triples, :home_runs, :rbi,
                    :walks, :intentional_walks, :hit_by_pitch, :strikeouts,
                    :sac_bunts, :sac_flies, :stolen_bases, :caught_stealing, :gidp,
                    CURRENT_TIMESTAMP, CURRENT_TIMESTAMP
                )
                ON CONFLICT (game_id, player_id)
                DO UPDATE SET
                    squad_level = EXCLUDED.squad_level,
                    batting_order = EXCLUDED.batting_order,
                    position = EXCLUDED.position,
                    pa = EXCLUDED.pa,
                    ab = EXCLUDED.ab,
                    runs = EXCLUDED.runs,
                    hits = EXCLUDED.hits,
                    doubles = EXCLUDED.doubles,
                    triples = EXCLUDED.triples,
                    home_runs = EXCLUDED.home_runs,
                    rbi = EXCLUDED.rbi,
                    walks = EXCLUDED.walks,
                    intentional_walks = EXCLUDED.intentional_walks,
                    hit_by_pitch = EXCLUDED.hit_by_pitch,
                    strikeouts = EXCLUDED.strikeouts,
                    sac_bunts = EXCLUDED.sac_bunts,
                    sac_flies = EXCLUDED.sac_flies,
                    stolen_bases = EXCLUDED.stolen_bases,
                    caught_stealing = EXCLUDED.caught_stealing,
                    gidp = EXCLUDED.gidp,
                    updated_at = CURRENT_TIMESTAMP
                """
            ),
            {
                "game_id": game_id,
                "player_id": player_id,
                "squad_level": squad_level,
                "batting_order": row.order,
                "position": row.position,
                "pa": row.pa,
                "ab": row.ab,
                "runs": row.runs,
                "hits": row.hits,
                "doubles": row.doubles,
                "triples": row.triples,
                "home_runs": row.home_runs,
                "rbi": row.rbi,
                "walks": row.walks,
                "intentional_walks": row.intentional_walks,
                "hit_by_pitch": row.hit_by_pitch,
                "strikeouts": row.strikeouts,
                "sac_bunts": row.sac_bunts,
                "sac_flies": row.sac_flies,
                "stolen_bases": row.stolen_bases,
                "caught_stealing": row.caught_stealing,
                "gidp": row.gidp,
            },
        )

    def _upsert_fielding_stats(
        self,
        game_id: int,
        player_id: int,
        squad_level: str,
        position: str,
        innings_defended: float,
        started_flag: bool,
    ) -> None:
        assert self.db is not None
        self.db.execute(
            text(
                """
                INSERT INTO player_game_fielding_stats (
                    game_id, player_id, squad_level, position, innings_defended, started_flag,
                    created_at, updated_at
                ) VALUES (
                    :game_id, :player_id, :squad_level, :position, :innings_defended, :started_flag,
                    CURRENT_TIMESTAMP, CURRENT_TIMESTAMP
                )
                ON CONFLICT (game_id, player_id, position)
                DO UPDATE SET
                    innings_defended = EXCLUDED.innings_defended,
                    started_flag = EXCLUDED.started_flag,
                    updated_at = CURRENT_TIMESTAMP
                """
            ),
            {
                "game_id": game_id,
                "player_id": player_id,
                "squad_level": squad_level,
                "position": position,
                "innings_defended": innings_defended,
                "started_flag": started_flag,
            },
        )

    # -----------------------------------------------------
    # aggregators
    # -----------------------------------------------------

    def _aggregate_batting_stats(self, rows: list[dict[str, Any]]) -> BattingStats:
        total = {
            "pa": 0,
            "ab": 0,
            "runs": 0,
            "hits": 0,
            "doubles": 0,
            "triples": 0,
            "home_runs": 0,
            "rbi": 0,
            "walks": 0,
            "intentional_walks": 0,
            "hit_by_pitch": 0,
            "strikeouts": 0,
            "sac_bunts": 0,
            "sac_flies": 0,
            "stolen_bases": 0,
            "caught_stealing": 0,
            "gidp": 0,
        }
        for row in rows:
            for key in total:
                total[key] += int(row.get(key, 0) or 0)
        return BattingStats(**total)

    def _calc_defense_by_position(self, rows: list[dict[str, Any]]) -> dict[str, float]:
        total_innings = sum(float(r.get("innings_defended", 0.0) or 0.0) for r in rows)
        if total_innings <= 0:
            return {}
        return {
            str(r["position"]): round(float(r.get("innings_defended", 0.0) or 0.0) / total_innings, 6)
            for r in rows
            if r.get("position")
        }

    def _calc_defense_score(self, rows: list[dict[str, Any]]) -> float:
        total_innings = sum(float(r.get("innings_defended", 0.0) or 0.0) for r in rows)
        if total_innings <= 0:
            return 0.0
        coverage_bonus = min(total_innings / 90.0, 1.0)
        multi_pos_bonus = min(len(rows) * 0.08, 0.24)
        return round(min(coverage_bonus + multi_pos_bonus, 1.0), 6)

    def _calc_recent_playing_time(self, batting_games: list[dict[str, Any]]) -> float:
        recent = batting_games[:10]
        pa = sum(int(r.get("pa", 0) or 0) for r in recent)
        return round(min(pa / 40.0, 1.0), 6)

    def _calc_handedness_bonus(self, bats: str | None, opponent_pitcher_hand: str | None) -> float:
        if not opponent_pitcher_hand:
            return 0.0
        bats_value = (bats or "R").upper()
        hand = opponent_pitcher_hand.upper()
        if bats_value == "S":
            return 0.05
        if hand == "R" and bats_value == "L":
            return 0.05
        if hand == "L" and bats_value == "R":
            return 0.03
        return -0.02

    # -----------------------------------------------------
    # small parsing utils
    # -----------------------------------------------------

    def _value_by_candidates(self, cells: list[str], header_index: dict[str, int], names: list[str]) -> str | None:
        for name in names:
            if name in header_index:
                idx = header_index[name]
                if idx < len(cells):
                    return cells[idx]
        return None

    def _clean_player_name(self, value: str) -> str:
        value = re.sub(r"\s+", " ", value).strip()
        value = re.sub(r"\([^)]*\)", "", value).strip()
        return value

    def _normalize_position(self, value: str | None) -> str | None:
        if not value:
            return None
        raw = value.strip()
        if raw in POSITION_ALIASES:
            return POSITION_ALIASES[raw]
        low = raw.lower()
        return POSITION_ALIASES.get(low, raw.upper())

    def _to_int(self, value: str | None) -> int | None:
        if value is None:
            return None
        m = re.search(r"-?\d+", value.replace(",", ""))
        if not m:
            return None
        return int(m.group(0))

    def _norm(self, value: str) -> str:
        return re.sub(r"\s+", "", value)

    # -----------------------------------------------------
    # mock fallback
    # -----------------------------------------------------

    def _get_mock_player_inputs(self, opponent_pitcher_hand: str | None) -> list[dict[str, Any]]:
        bonus = 0.05 if (opponent_pitcher_hand or "R") == "R" else -0.02
        return [
            {
                "player_id": 104,
                "player_name": "坂倉 将吾",
                "eligible_positions": ["C", "1B", "DH"],
                "last_5": BattingStats(pa=23, ab=19, runs=4, hits=8, doubles=2, triples=0, home_runs=1, rbi=4, walks=4, intentional_walks=0, hit_by_pitch=0, strikeouts=2, sac_bunts=0, sac_flies=0, stolen_bases=0, caught_stealing=0, gidp=0),
                "last_10": BattingStats(pa=42, ab=36, runs=6, hits=12, doubles=3, triples=0, home_runs=1, rbi=6, walks=5, intentional_walks=0, hit_by_pitch=1, strikeouts=5, sac_bunts=0, sac_flies=0, stolen_bases=0, caught_stealing=0, gidp=1),
                "last_15": BattingStats(pa=61, ab=52, runs=8, hits=16, doubles=4, triples=0, home_runs=2, rbi=8, walks=7, intentional_walks=0, hit_by_pitch=1, strikeouts=8, sac_bunts=0, sac_flies=0, stolen_bases=0, caught_stealing=0, gidp=1),
                "season": BattingStats(pa=91, ab=74, runs=11, hits=20, doubles=5, triples=0, home_runs=3, rbi=13, walks=13, intentional_walks=0, hit_by_pitch=1, strikeouts=11, sac_bunts=0, sac_flies=1, stolen_bases=1, caught_stealing=0, gidp=2),
                "defense_score": 0.78,
                "position_scarcity": 0.50,
                "recent_playing_time": 0.92,
                "opponent_handedness_bonus": bonus,
                "defense_by_position": {"C": 0.7, "1B": 0.2, "DH": 0.1},
            },
            {
                "player_id": 110,
                "player_name": "モンテロ",
                "eligible_positions": ["1B", "DH"],
                "last_5": BattingStats(pa=20, ab=18, runs=2, hits=6, doubles=1, triples=0, home_runs=1, rbi=5, walks=2, intentional_walks=0, hit_by_pitch=0, strikeouts=4, sac_bunts=0, sac_flies=0, stolen_bases=0, caught_stealing=0, gidp=1),
                "last_10": BattingStats(pa=39, ab=34, runs=3, hits=10, doubles=2, triples=0, home_runs=2, rbi=8, walks=4, intentional_walks=0, hit_by_pitch=1, strikeouts=8, sac_bunts=0, sac_flies=0, stolen_bases=0, caught_stealing=0, gidp=2),
                "last_15": BattingStats(pa=58, ab=50, runs=5, hits=13, doubles=3, triples=0, home_runs=3, rbi=11, walks=6, intentional_walks=0, hit_by_pitch=1, strikeouts=11, sac_bunts=0, sac_flies=0, stolen_bases=0, caught_stealing=0, gidp=3),
                "season": BattingStats(pa=64, ab=58, runs=5, hits=14, doubles=3, triples=0, home_runs=3, rbi=11, walks=5, intentional_walks=0, hit_by_pitch=1, strikeouts=13, sac_bunts=0, sac_flies=0, stolen_bases=0, caught_stealing=0, gidp=3),
                "defense_score": 0.61,
                "position_scarcity": 0.50,
                "recent_playing_time": 0.81,
                "opponent_handedness_bonus": bonus,
                "defense_by_position": {"1B": 0.8, "DH": 0.2},
            },
        ]
