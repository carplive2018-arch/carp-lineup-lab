# Railway Cron Job 設定
# このファイルを Railway ダッシュボードの「New Service → Cron」で使用する
#
# 設定手順:
#   1. Railway プロジェクト → 「+ New Service」→「GitHub Repo」を選択
#   2. 同じリポジトリ（carp-lineup-lab）を選択
#   3. 「Start Command」に各ジョブのコマンドを設定
#   4. 「Cron Schedule」にスケジュールを設定
#   5. 「Variables」に X_API_KEY 等を設定
#
# ──────────────────────────────────────────────────────────────────
# ジョブ① 予想スタメン投稿（毎朝 9:00 JST = 0:00 UTC）
# ──────────────────────────────────────────────────────────────────
#   Start Command : python scripts/post_to_x.py
#   Cron Schedule : 0 0 * * *
#   Variables:
#     POST_MODE=lineup
#     DH_MODE=true
#     WINDOW_GAMES=5
#     X_API_KEY=<your_api_key>
#     X_API_SECRET=<your_api_secret>
#     X_ACCESS_TOKEN=<your_access_token>
#     X_ACCESS_TOKEN_SECRET=<your_access_token_secret>
#     SITE_URL=https://recent-data-on-the-carp.site
#
# ──────────────────────────────────────────────────────────────────
# ジョブ② 成績ランキング投稿（毎朝 9:05 JST = 0:05 UTC）
# ──────────────────────────────────────────────────────────────────
#   Start Command : python scripts/post_to_x.py
#   Cron Schedule : 5 0 * * *
#   Variables:
#     POST_MODE=ranking
#     WINDOW_GAMES=5
#     X_API_KEY=<your_api_key>
#     ...（同上）
#
# ──────────────────────────────────────────────────────────────────
# ジョブ③ 試合結果投稿（毎日 22:30 JST = 13:30 UTC）
# ──────────────────────────────────────────────────────────────────
#   Start Command : python scripts/post_to_x.py
#   Cron Schedule : 30 13 * * *
#   Variables:
#     POST_MODE=result
#     X_API_KEY=<your_api_key>
#     ...（同上）
#
# ──────────────────────────────────────────────────────────────────
# テスト実行（DRY_RUN モード）
# ──────────────────────────────────────────────────────────────────
#   DRY_RUN=true を Variables に追加すると、
#   実際には投稿せず本文のみ標準出力に表示します。
