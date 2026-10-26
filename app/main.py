import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.config import get_settings
from app.routers import health, sms, web

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")


@asynccontextmanager
async def lifespan(app):
    for w in get_settings().validate_startup():
        logging.getLogger("dh").warning(w)
    yield


app = FastAPI(title=get_settings().app_name, lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
app.include_router(health.router)
app.include_router(sms.router)
app.include_router(web.router)
