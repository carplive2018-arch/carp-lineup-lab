from fastapi import APIRouter
from app.api.lineup import router as lineup_router
from app.api.public import router as public_router
from app.api.admin import router as admin_router

api_router = APIRouter()
api_router.include_router(public_router)
api_router.include_router(lineup_router)
api_router.include_router(admin_router)
