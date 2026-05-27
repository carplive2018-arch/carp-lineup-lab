from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import PlainTextResponse, RedirectResponse

from app.api import public


@asynccontextmanager
async def lifespan(app: FastAPI):
    # ── 起動時: バックグラウンドでキャッシュをウォームアップ ──
    public.warmup_cache()
    yield
    # ── シャットダウン時: 特に何もしない ──


app = FastAPI(title="carp-lineup-api", version="0.1.0", lifespan=lifespan)

app.include_router(public.router)


@app.get("/", include_in_schema=False)
def root():
    return RedirectResponse(url="/public/top")


@app.get("/health")
def health():
    return {"status": "ok", "marker": "koidanshi-check-0511"}


@app.get("/ads.txt", include_in_schema=False)
def ads_txt():
    ads_path = Path(__file__).parent.parent / "ads.txt"
    content = ads_path.read_text(encoding="utf-8")
    return PlainTextResponse(content=content, media_type="text/plain; charset=utf-8")


@app.get("/robots.txt", include_in_schema=False)
def robots_txt():
    content = (
        "User-agent: *\n"
        "Allow: /\n"
        "Disallow: /internal/\n"
        "\n"
        "Sitemap: https://www.koidanshi.com/sitemap.xml\n"
    )
    return PlainTextResponse(content=content, media_type="text/plain; charset=utf-8")


@app.get("/sitemap.xml", include_in_schema=False)
def sitemap_xml():
    _base = "https://www.koidanshi.com"
    _teams = [
        "広島", "阪神", "巨人", "DeNA", "中日", "ヤクルト",
        "ソフトバンク", "西武", "楽天", "ロッテ", "オリックス", "日本ハム",
    ]
    urls = []

    # 静的ページ
    static_pages = [
        ("", "1.0", "daily"),
        ("/public/top", "1.0", "daily"),
        ("/public/about", "0.9", "monthly"),
        ("/public/privacy", "0.5", "monthly"),
        ("/public/terms", "0.5", "monthly"),
    ]
    for path, priority, freq in static_pages:
        urls.append(
            f"  <url>\n"
            f"    <loc>{_base}{path}</loc>\n"
            f"    <changefreq>{freq}</changefreq>\n"
            f"    <priority>{priority}</priority>\n"
            f"  </url>"
        )

    # 球団別ページ
    team_pages = [
        ("/public/predicted-lineup?window_games=5&team={t}&view=html", "0.9", "daily"),
        ("/public/recent-batting?team={t}&view=html", "0.8", "daily"),
        ("/public/risp?team={t}&view=html", "0.8", "daily"),
        ("/public/fielding-baserunning?team={t}&view=html", "0.7", "weekly"),
        ("/public/war-ranking?team={t}&view=html", "0.7", "weekly"),
        ("/public/hot-batters?team={t}&view=html", "0.7", "daily"),
        ("/public/game-recap?team={t}&view=html", "0.7", "daily"),
    ]
    for team in _teams:
        for path_tmpl, priority, freq in team_pages:
            path = path_tmpl.replace("{t}", team)
            urls.append(
                f"  <url>\n"
                f"    <loc>{_base}{path}</loc>\n"
                f"    <changefreq>{freq}</changefreq>\n"
                f"    <priority>{priority}</priority>\n"
                f"  </url>"
            )

    xml = (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">\n'
        + "\n".join(urls)
        + "\n</urlset>"
    )
    return PlainTextResponse(content=xml, media_type="application/xml; charset=utf-8")
