from __future__ import annotations

from fastapi import FastAPI
from fastapi.responses import RedirectResponse

from app.api import public

app = FastAPI(title="carp-lineup-api", version="0.1.0")

app.include_router(public.router)

@app.get("/", include_in_schema=False)
def root():
    return RedirectResponse(url="/docs")

@app.get("/health")
def health():
    return {"status": "ok", "marker": "koidanshi-check-0511"}
