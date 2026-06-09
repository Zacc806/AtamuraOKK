from fastapi.routing import APIRouter

from AtamuraOKK.web.api import echo, monitoring, v1

api_router = APIRouter()
api_router.include_router(monitoring.router)
api_router.include_router(echo.router, prefix="/echo", tags=["echo"])
api_router.include_router(v1.router, prefix="/v1")
