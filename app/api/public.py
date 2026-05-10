from __future__ import annotations

import re
from urllib.request import Request, urlopen

from fastapi import APIRouter
from fastapi.responses import HTMLResponse, JSONResponse

router = APIRouter(tags=["public"])


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
          </style>
        </head>
        <body>
          <div class="wrap">{body}</div>
        </body>
        </html>
        """
    )


def _clean_text(value: str) -> str:
    value = re.sub(r"<br\s*/?>", " ", value, flags=re.IGNORECASE)
    value = re.sub(r"<[^>]+>", "", value)
    value = value.replace("&nbsp;", " ")
    value = value.replace("&#039;", "'")
    value = re.sub(r"\s+", " ", value).strip()
    return value



def _fetch_recent_actual_lineups() -> list[dict]:
    url = "https://baseball-data.com/lineup/c.html"
    req = Request(url, headers={"User-Agent": "Mozilla/5.0"})
    html = urlopen(req, timeout=15).read().decode("utf-8", errors="ignore")

pattern = re.compile(
    r"<tr[^>]*>\s*"
    r"<td[^>]*>(.*?)</td>\s*"
    r"<td[^>]*>(.*?)</td>\s*"
    r"<td[^>]*>(.*?)</td>\s*"
    r"<td[^>]*>(.*?)</td>\s*"
    r"<td[^>]*>(.*?)</td>\s*"
    r"<td[^>]*>(.*?)</td>\s*"
    r"<td[^>]*>(.*?)</td>\s*"
    r"<td[^>]*>(.*?)</td>\s*"
    r"<td[^>]*>(.*?)</td>\s*"
    r"<td[^>]*>(.*?)</td>\s*"
    r"</tr>",
    re.DOTALL,
)


    rows = pattern.findall(html)
    games = []

    for row in rows:
        date = _clean_text(row[0])
        if "月" not in date:
            continue

        players = [_clean_text(cell) for cell in row[1:10]]
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


@router.get("/", response_class=HTMLResponse)
def index() -> HTMLResponse:
    return _layout(
        "Carp Lineup Lab",
        """
        <span class="pill">β版 / 非公式</span>
        <h1>直近5試合の実際のスタメン</h1>
        <p class="muted">外部公開ページから取得した、広島東洋カープの直近スタメンです。</p>

        <div class="card">
          <p id="status" class="muted">読み込み中...</p>
          <div id="games" class="grid"></div>
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
          async function loadRecentActualLineups() {
            const statusEl = document.getElementById("status");
            const gamesEl = document.getElementById("games");

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
              statusEl.textContent = "いまは表示できません。少ししてからもう一度開いてください。";
            }
          }

          loadRecentActualLineups();
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
            <li><a href="https://npb.jp/bis/2026/stats/idb1_c.html">NPB公式 1軍打撃成績</a></li>
            <li><a href="https://npb.jp/bis/teams/rst_c.html">NPB公式 選手登録一覧</a></li>
          </ul>
        </div>
        """,
    )
