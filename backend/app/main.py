from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.middleware.cors import CORSMiddleware

from backend.app.core.config import get_settings
from backend.app.core.bootstrap import ensure_superadmin
from backend.app.database.session import SessionLocal
from backend.app.routers.auth import router as auth_router
from backend.app.routers.categories import router as categories_router
from backend.app.routers.products import router as products_router
from backend.app.routers.orders import router as orders_router
from backend.app.routers.payments import router as payments_router
from backend.app.routers.admin_settings import router as admin_settings_router
from backend.app.routers.reports import router as reports_router

settings = get_settings()


@asynccontextmanager
async def lifespan(_: FastAPI):
    with SessionLocal() as db:
        ensure_superadmin(db)
    yield


app = FastAPI(title=settings.app_name, version="0.1.0", lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origin_list,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)
app.include_router(auth_router)
app.include_router(categories_router)
app.include_router(products_router)
app.include_router(orders_router)
app.include_router(payments_router)
app.include_router(admin_settings_router)
app.include_router(reports_router)


@app.get("/", include_in_schema=False)
def storefront_root(request: Request):
    if request.url.port == 8000:
        return HTMLResponse("<h1>Luxe Beauty Hub</h1><p>Start the storefront with <code>python server.py</code> on port 8000. The FastAPI backend should run on port 8010.</p><a href='http://localhost:8010/docs'>Open API docs</a>", status_code=200)
    return RedirectResponse(url="http://localhost:8000/", status_code=307)


@app.get("/api", tags=["system"])
def api_root() -> dict[str, str]:
    return {
        "service": settings.app_name,
        "status": "ok",
        "storefront": "http://localhost:8000/",
        "docs": "/docs",
        "health": "/health",
    }


@app.get("/health", tags=["system"])
def health() -> dict[str, str]:
    return {"status": "ok", "environment": settings.app_env}
