"""v1 路由总装。"""

from fastapi import APIRouter

from app.api.v1 import datasets, runs, system

api_router = APIRouter()
api_router.include_router(datasets.router)
api_router.include_router(runs.router)
api_router.include_router(system.router)
