from __future__ import annotations

from fastapi import APIRouter
from fastapi.responses import HTMLResponse

router = APIRouter(tags=["public"])


def _layout(title: str, body: str) -> HTMLResponse:
    return HTMLResponse(
        f"""
        <!doctype html>
        <html lang=\"ja\">
        <head>
          <meta charset=\"utf-8\" />
          <meta name=\"viewport\" content=\"width=device-width, initial-scale=1\" />
          <title>{title}</title>
          <style>
            body {{ font-family: -apple-system, BlinkMacSystemFont, 'Hiragino Sans', sans-serif; margin: 0; background: #0b1020; color: #f5f7fb; }}
            .wrap {{ max-width: 860px; margin: 0 auto; padding: 40px 20px 72px; }}
            .card {{ background: #121a31; border: 1px solid #26304d; border-radius: 18px; padding: 24px; margin-top: 18px; }}
            a {{ color: #9fc2ff; }}
            h1, h2 {{ line-height: 1.3; }}
            ul {{ line-height: 1.8; }}
            .muted {{ color: #a9b5d1; }}
            .pill {{ display: inline-block; padding: 6px 10px; border-radius: 999px; background: #243154; color: #cfe0ff; font-size: 12px; }}
          </style>
        </head>
        <body>
          <div class=\"wrap\">{body}</div>
        </body>
        </html>
        """
    )


@router.get("/", response_class=HTMLResponse)
def index() -> HTMLResponse:
    return _layout(
        "Carp Lineup Lab",
        """
        <span class=\"pill\">β版 / 非公式</span>
        <h1>Carp Lineup Lab</h1>
        <p class=\"muted\">広島東洋カープの1軍・2軍成績から、独自ロジックで翌日の予想スタメンを出す分析サイトです。</p>
        <div class=\"card\">
          <h2>主な入口</h2>
          <ul>
            <li><a href=\"/health\">/health</a> - 稼働確認</li>
            <li><a href=\"/docs\">/docs</a> - APIドキュメント</li>
            <li><a href=\"/api/lineups/today\">/api/lineups/today</a> - 当日の予想スタメンAPI</li>
            <li><a href=\"/data-policy\">/data-policy</a> - データ表示ポリシー</li>
            <li><a href=\"/disclaimer\">/disclaimer</a> - 免責</li>
            <li><a href=\"/sources\">/sources</a> - 出典</li>
          </ul>
        </div>
        """,
    )


@router.get("/data-policy", response_class=HTMLResponse)
def data_policy() -> HTMLResponse:
    return _layout(
        "データ表示ポリシー",
        """
        <span class=\"pill\">数値と自作UIのみ</span>
        <h1>データ表示ポリシー</h1>
        <div class=\"card\">
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
        <span class=\"pill\">予想は予想</span>
        <h1>免責</h1>
        <div class=\"card\">
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
        <span class=\"pill\">公開情報ベース</span>
        <h1>主な出典</h1>
        <div class=\"card\">
          <ul>
            <li><a href=\"https://npb.jp/bis/2026/stats/idb1_c.html\">NPB公式 1軍打撃成績</a></li>
            <li><a href=\"https://npb.jp/bis/2026/stats/idb2_c.html\">NPB公式 2軍打撃成績</a></li>
            <li><a href=\"https://www.carp.co.jp/farm\">カープ公式 ファーム情報</a></li>
            <li><a href=\"https://baseball-data.com/lineup/c.html\">過去スタメン参考</a></li>
          </ul>
        </div>
        """,
    )
