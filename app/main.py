from __future__ import annotations

import secrets
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.middleware.sessions import SessionMiddleware

from . import config, settings
from .db import SessionLocal, init_db
from .models import Notification, User
from .web import BASE, current_user, is_public, login_redirect, render


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    settings.invalidate()
    db = SessionLocal()
    try:
        if not settings.get("EBAY_VERIFICATION_TOKEN"):
            settings.set_one(db, "EBAY_VERIFICATION_TOKEN", "fd_" + secrets.token_urlsafe(40)[:60].replace("-", "_"))
    finally:
        db.close()
    from .services import scheduler
    scheduler.start()
    print(f"[{config.APP_NAME}] v{config.APP_VERSION} ready — database: {config.redacted_db_url()}")
    yield
    scheduler.shutdown()


app = FastAPI(title=config.APP_NAME, version=config.APP_VERSION, lifespan=lifespan,
              docs_url=None, redoc_url=None, openapi_url=None)
app.mount("/static", StaticFiles(directory=str(BASE / "static")), name="static")


@app.middleware("http")
async def auth_guard(request: Request, call_next):
    path = request.url.path
    if is_public(path):
        return await call_next(request)
    if config.AUTH_MODE != "none":
        if current_user(request) is None:
            db = SessionLocal()
            try:
                has_users = db.query(User.id).first() is not None
            finally:
                db.close()
            if not has_users:
                from fastapi.responses import RedirectResponse
                return RedirectResponse("/setup", status_code=303)
            if path.startswith("/api/"):
                return JSONResponse({"error": "unauthorized"}, status_code=401)
            return login_redirect(request)
    db = SessionLocal()
    try:
        request.state.unread = db.query(Notification).filter(Notification.read.is_(False)).count()
    finally:
        db.close()
    return await call_next(request)


app.add_middleware(SessionMiddleware, secret_key=config.SECRET_KEY, session_cookie="flipdesk_session",
                   max_age=60 * 60 * 24 * 30, same_site="lax", https_only=config.SESSION_COOKIE_SECURE)


@app.exception_handler(StarletteHTTPException)
async def http_error(request: Request, exc: StarletteHTTPException):
    if request.url.path.startswith(("/api/", "/ebay/account-deletion", "/static/")):
        return JSONResponse({"error": exc.detail}, status_code=exc.status_code)
    return render(request, "error.html", {"code": exc.status_code, "detail": exc.detail}, status_code=exc.status_code)


@app.get("/health")
def health():
    from sqlalchemy import text

    from .db import engine
    try:
        with engine.connect() as c:
            c.execute(text("SELECT 1"))
        return {"status": "ok", "version": config.APP_VERSION}
    except Exception as exc:  # pragma: no cover
        return JSONResponse({"status": "error", "error": str(exc)}, status_code=503)


@app.get("/favicon.ico")
def favicon():
    return PlainTextResponse("", status_code=204)


from .routers import auth, data, ebay_setup, inventory, ledger, reports, sales, system  # noqa: E402

for r in (auth.router, inventory.router, sales.router, ledger.router, reports.router, ebay_setup.router,
          system.router, data.router):
    app.include_router(r)
