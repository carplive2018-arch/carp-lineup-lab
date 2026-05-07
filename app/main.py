from __future__ import annotations

from fastapi import FastAPI

from app.api.router import api_router

app = FastAPI(title="carp-lineup-api")
app.include_router(api_router)


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}
