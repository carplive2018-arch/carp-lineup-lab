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
    return RedirectResponse(url="/public/predicted-lineup?window_games=5&use_dh=true&view=html")


@app.get("/health")
def health():
    return {"status": "ok", "marker": "koidanshi-check-0511"}


@app.get("/ads.txt", include_in_schema=False)
def ads_txt():
    ads_path = Path(__file__).parent.parent / "ads.txt"
    content = ads_path.read_text(encoding="utf-8")
    return PlainTextResponse(content=content, media_type="text/plain; charset=utf-8")
