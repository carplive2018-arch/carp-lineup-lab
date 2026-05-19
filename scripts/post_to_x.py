#!/usr/bin/env python3
"""
X（Twitter）自動投稿スクリプト
Railway Cron Job から呼び出される

投稿スケジュール:
  - 予想スタメン  : 毎朝 9:00 JST（試合日のみ）
  - 成績ランキング: 毎朝 9:05 JST（試合日のみ）
  - 試合結果      : 毎日 22:30 JST（直近試合結果を投稿）

環境変数（Railway Variables に設定）:
  X_API_KEY              : API Key（Consumer Key）
  X_API_SECRET           : API Secret（Consumer Secret）
  X_ACCESS_TOKEN         : Access Token
  X_ACCESS_TOKEN_SECRET  : Access Token Secret
  SITE_URL               : サイトURL（省略時: https://recent-data-on-the-carp.site）
  POST_MODE              : lineup / ranking / result（何を投稿するか）
  DH_MODE                : true / false（DHあり/なし、デフォルト: true）
  WINDOW_GAMES           : 直近N試合（デフォルト: 5）
"""

import os
import sys
import json
import datetime
import textwrap
import httpx

# ── 定数 ──────────────────────────────────────────────
SITE_URL     = os.environ.get("SITE_URL", "https://recent-data-on-the-carp.site")
POST_MODE    = os.environ.get("POST_MODE", "lineup")   # lineup / ranking / result
DH_MODE      = os.environ.get("DH_MODE", "true").lower() == "true"
WINDOW_GAMES = int(os.environ.get("WINDOW_GAMES", "5"))

X_API_KEY             = os.environ.get("X_API_KEY", "")
X_API_SECRET          = os.environ.get("X_API_SECRET", "")
X_ACCESS_TOKEN        = os.environ.get("X_ACCESS_TOKEN", "")
X_ACCESS_TOKEN_SECRET = os.environ.get("X_ACCESS_TOKEN_SECRET", "")

DRY_RUN = os.environ.get("DRY_RUN", "false").lower() == "true"

# ポジション日本語マップ
POS_JA = {
    "C":   "捕",
    "1B":  "一",
    "2B":  "二",
    "3B":  "三",
    "SS":  "遊",
    "LF":  "左",
    "CF":  "中",
    "RF":  "右",
    "DH":  "D",
    "SP":  "先",
    "RP":  "救",
}

# ── APIクライアント初期化 ──────────────────────────────
def _get_twitter_client():
    """tweepy クライアントを返す（X API v2）"""
    try:
        import tweepy
    except ImportError:
        print("ERROR: tweepy がインストールされていません。pip install tweepy")
        sys.exit(1)

    if not all([X_API_KEY, X_API_SECRET, X_ACCESS_TOKEN, X_ACCESS_TOKEN_SECRET]):
        print("ERROR: X API の認証情報が設定されていません。")
        print("  X_API_KEY, X_API_SECRET, X_ACCESS_TOKEN, X_ACCESS_TOKEN_SECRET")
        print("  を Railway Variables に設定してください。")
        sys.exit(1)

    client = tweepy.Client(
        consumer_key=X_API_KEY,
        consumer_secret=X_API_SECRET,
        access_token=X_ACCESS_TOKEN,
        access_token_secret=X_ACCESS_TOKEN_SECRET,
    )
    return client


# ── サイトAPIからデータ取得 ────────────────────────────
def _fetch_json(path: str, params: dict | None = None) -> dict:
    """サイト内部APIからJSONデータを取得"""
    url = f"{SITE_URL}{path}"
    try:
        resp = httpx.get(url, params=params or {}, timeout=30.0)
        resp.raise_for_status()
        return resp.json()
    except Exception as e:
        print(f"ERROR: API取得失敗 {url}: {e}")
        return {}


def _fetch_lineup() -> dict:
    return _fetch_json(
        "/public/predicted-lineup",
        {"use_dh": str(DH_MODE).lower(), "window_games": WINDOW_GAMES, "view": "json"},
    )


def _fetch_risp() -> dict:
    return _fetch_json("/public/risp", {"window_games": WINDOW_GAMES, "view": "json"})


def _fetch_recent_batting() -> dict:
    return _fetch_json("/public/recent-batting", {"window_games": WINDOW_GAMES, "view": "json"})


def _fetch_game_recap() -> dict:
    return _fetch_json("/public/game-recap", {"view": "json"})


# ── ツイート本文生成 ──────────────────────────────────

def _pos_ja(pos) -> str:
    pos_str = pos.value if hasattr(pos, "value") else str(pos)
    return POS_JA.get(pos_str, pos_str)


def build_lineup_tweet(data: dict) -> str:
    """予想スタメン投稿文を生成"""
    lineup = data.get("lineup", [])
    if not lineup:
        return ""

    dh_label = "（DHあり）" if data.get("use_dh") else "（DHなし）"
    window   = data.get("window_games", WINDOW_GAMES)

    now_jst  = datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=9)))
    date_str = now_jst.strftime("%-m/%-d")

    lines = [f"【{date_str} 広島 予想スタメン】{dh_label}"]
    lines.append(f"直近{window}試合成績ベースの予測")
    lines.append("")

    for entry in sorted(lineup, key=lambda x: x.get("order", 99)):
        order    = entry.get("order", "?")
        pos      = entry.get("position", "")
        pos_str  = pos.value if hasattr(pos, "value") else str(pos)
        pos_ja   = POS_JA.get(pos_str, pos_str)
        name     = entry.get("player_name", "?")
        lines.append(f"{order}番【{pos_ja}】{name}")

    lines.append("")
    lines.append(f"#広島東洋カープ #carp")
    lines.append(f"📊 {SITE_URL}")

    tweet = "\n".join(lines)

    # 280文字（日本語140字）チェック
    if len(tweet) > 280:
        # URLを除いたコンパクト版にフォールバック
        lines_compact = lines[:-1]  # URL行削除
        tweet = "\n".join(lines_compact)

    return tweet


def build_ranking_tweet(risp_data: dict, batting_data: dict) -> list[str]:
    """成績ランキング投稿文を生成（スレッド：最大2ツイート）"""
    tweets = []
    window = WINDOW_GAMES

    now_jst  = datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=9)))
    date_str = now_jst.strftime("%-m/%-d")

    # ── ツイート1：得点圏打率 ──
    # risp APIは 'player' キー、risp_ab >= 3 の選手のみ
    players = risp_data.get("players", [])
    risp_qualified = [
        p for p in players
        if p.get("risp_ab", 0) >= 3
    ]
    risp_qualified.sort(key=lambda p: p.get("risp_avg", 0.0), reverse=True)

    lines1 = [f"【直近{window}試合 得点圏成績】{date_str}"]
    lines1.append("")
    if risp_qualified:
        for i, p in enumerate(risp_qualified[:5], 1):
            name     = p.get("player", p.get("name", "?"))   # risp API は 'player' キー
            avg      = p.get("risp_avg", 0.0)
            risp_ab  = p.get("risp_ab", 0)
            risp_hit = p.get("risp_hit", 0)
            rbi      = p.get("rbi", 0)
            medal    = ["🥇", "🥈", "🥉", "4️⃣", "5️⃣"][i - 1]
            lines1.append(
                f"{medal} {name}　.{int(avg * 1000):03d}（{risp_hit}/{risp_ab}）打点{rbi}"
            )
    else:
        lines1.append("（データなし）")

    lines1.append("")
    lines1.append(f"#広島東洋カープ #carp")
    tweets.append("\n".join(lines1))

    # ── ツイート2：出塁率・打率・打点トップ3 ──
    # recent-batting API は 'player_name' キー
    batters = batting_data.get("players", []) if batting_data else []
    # pa >= 5 の選手のみ
    qualified = [p for p in batters if p.get("pa", 0) >= 5]

    # 出塁率ランキング
    obp_rank = sorted(qualified, key=lambda p: float(p.get("obp", 0.0) or 0.0), reverse=True)[:3]
    # 打率ランキング
    avg_rank = sorted(qualified, key=lambda p: float(p.get("avg", 0.0) or 0.0), reverse=True)[:3]
    # 打点ランキング
    rbi_rank = sorted(qualified, key=lambda p: int(p.get("rbi", 0) or 0), reverse=True)[:3]

    lines2 = [f"【直近{window}試合 打撃成績ランキング】"]
    lines2.append("")

    if obp_rank:
        lines2.append("📈 出塁率")
        for i, p in enumerate(obp_rank, 1):
            name = p.get("player_name", p.get("name", "?"))   # recent-batting API は 'player_name'
            obp  = float(p.get("obp", 0.0) or 0.0)
            lines2.append(f"  {i}. {name}　.{int(obp * 1000):03d}")

    lines2.append("")

    if avg_rank:
        lines2.append("🎯 打率")
        for i, p in enumerate(avg_rank, 1):
            name = p.get("player_name", p.get("name", "?"))
            avg  = float(p.get("avg", 0.0) or 0.0)
            lines2.append(f"  {i}. {name}　.{int(avg * 1000):03d}")

    lines2.append("")

    if rbi_rank:
        lines2.append("💪 打点")
        for i, p in enumerate(rbi_rank, 1):
            name = p.get("player_name", p.get("name", "?"))
            rbi  = int(p.get("rbi", 0) or 0)
            lines2.append(f"  {i}. {name}　{rbi}打点")

    lines2.append("")
    lines2.append(f"📊 {SITE_URL}")
    tweets.append("\n".join(lines2))

    return tweets


def build_result_tweet(data: dict) -> str:
    """直近試合結果投稿文を生成"""
    games = data.get("games", [])
    if not games:
        return ""

    # 最新試合（先頭）
    g = games[0]

    result    = g.get("result", "?")
    opp       = g.get("opp_team", "?")
    c_score   = g.get("carp_score")
    o_score   = g.get("opp_score")
    date      = g.get("date", "")
    hr        = g.get("hr_players", [])
    timely    = g.get("timely_players", [])
    multi     = g.get("multi_hit", [])
    starter   = g.get("starter")
    total_hits = g.get("total_hits", 0)

    # 結果絵文字
    result_emoji = {"勝": "🔴⚾✨", "負": "😔", "分": "🟡"}.get(result, "⚾")

    score_str = f"{c_score}－{o_score}" if c_score is not None else "-"

    lines = [
        f"【試合結果】{date} 広島 vs {opp}",
        f"{result_emoji} {result}　{score_str}",
        "",
    ]

    if starter:
        lines.append(f"先発：{starter}")

    if hr:
        lines.append("本塁打：" + "、".join(hr))
    if timely:
        lines.append("タイムリー：" + "、".join(timely[:3]))
    if multi:
        lines.append("マルチ安打：" + "、".join(multi[:3]))
    if total_hits > 0:
        lines.append(f"チーム安打：{total_hits}本")

    lines.append("")
    lines.append(f"#広島東洋カープ #carp")
    lines.append(f"📊 {SITE_URL}")

    return "\n".join(lines)


# ── 投稿処理 ──────────────────────────────────────────
def post_tweet(client, text: str, reply_to_id: str | None = None) -> str | None:
    """1ツイートを投稿してIDを返す"""
    if not text.strip():
        print("SKIP: 本文が空のためスキップ")
        return None

    print(f"\n{'='*60}")
    print(f"投稿内容（{len(text)}文字）:")
    print(text)
    print('='*60)

    if DRY_RUN:
        print("[DRY_RUN] 実際の投稿はスキップ")
        return "dry_run_id"

    try:
        kwargs = {"text": text}
        if reply_to_id:
            kwargs["in_reply_to_tweet_id"] = reply_to_id
        resp = client.create_tweet(**kwargs)
        tweet_id = str(resp.data["id"])
        print(f"投稿成功: https://twitter.com/i/web/status/{tweet_id}")
        return tweet_id
    except Exception as e:
        print(f"ERROR: 投稿失敗: {e}")
        return None


def post_thread(client, tweets: list[str]) -> None:
    """スレッド形式で複数ツイートを投稿"""
    prev_id = None
    for tweet_text in tweets:
        tweet_id = post_tweet(client, tweet_text, reply_to_id=prev_id)
        if tweet_id and tweet_id != "dry_run_id":
            prev_id = tweet_id


# ── メイン ────────────────────────────────────────────
def main():
    print(f"POST_MODE={POST_MODE}, DH_MODE={DH_MODE}, WINDOW_GAMES={WINDOW_GAMES}")
    print(f"DRY_RUN={DRY_RUN}")
    print(f"SITE_URL={SITE_URL}")

    client = _get_twitter_client()

    if POST_MODE == "lineup":
        # 予想スタメン投稿
        print("データ取得中: predicted-lineup")
        data = _fetch_lineup()
        if not data.get("lineup"):
            print("ERROR: 打順データが空です")
            sys.exit(1)
        tweet = build_lineup_tweet(data)
        post_tweet(client, tweet)

    elif POST_MODE == "ranking":
        # 成績ランキング投稿（スレッド）
        print("データ取得中: risp + recent-batting")
        risp_data    = _fetch_risp()
        batting_data = _fetch_recent_batting()
        tweets = build_ranking_tweet(risp_data, batting_data)
        if not tweets:
            print("ERROR: ランキングデータが空です")
            sys.exit(1)
        post_thread(client, tweets)

    elif POST_MODE == "result":
        # 試合結果投稿
        print("データ取得中: game-recap")
        data = _fetch_game_recap()
        if not data.get("games"):
            print("ERROR: 試合データが空です")
            sys.exit(1)
        tweet = build_result_tweet(data)
        post_tweet(client, tweet)

    else:
        print(f"ERROR: 不明な POST_MODE: {POST_MODE}")
        print("  lineup / ranking / result のいずれかを指定してください")
        sys.exit(1)

    print("\n完了")


if __name__ == "__main__":
    main()
