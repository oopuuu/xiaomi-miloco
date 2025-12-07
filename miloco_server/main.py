# Copyright (C) 2025 Xiaomi Corporation
# This software may be used and distributed according to the terms of the Xiaomi Miloco License Agreement.

"""
MILOCO Server main application entry point.
Provides FastAPI application setup, middleware configuration, and server startup.
"""

import logging
import threading
import time
import webbrowser

import uvicorn
from fastapi import FastAPI, Request, Response
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from miloco_server.utils.mediamtx import rtsp_server
from miloco_server.config import LITE_MODE  # [新增]

from miloco_server.config import APP_CONFIG, IMAGE_DIR, SERVER_CONFIG, STATIC_DIR
from miloco_server.controller import (
    auth_router,
    chat_router,
    ha_router,
    mcp_router,
    miot_router,
    model_router,
    trigger_router,
    web_router,
)
from miloco_server.middleware.auth_middleware import AuthStaticFiles
from miloco_server.middleware.exception_handler import handle_exception
from miloco_server.service.manager import get_manager
from miloco_server.utils.database import init_database
from miloco_server.utils.normal_util import get_uvicorn_log_config, update_localhost_cert

logger = logging.getLogger(__name__)

app = FastAPI(
    title=APP_CONFIG["title"],
    description=APP_CONFIG["description"],
    version=APP_CONFIG["version"]
)


@app.middleware("http")
async def catch_all_exceptions_middleware(request: Request, call_next):
    """Global exception handling middleware"""
    try:
        return await call_next(request)
    except Exception as exc:  # pylint: disable=broad-exception-caught
        return handle_exception(request, exc)


app.mount("/assets", StaticFiles(directory=str(STATIC_DIR / "assets")), name="assets")
app.mount("/static/camera/images", AuthStaticFiles(directory=str(IMAGE_DIR)), name="images")

# [关键修改] 核心路由 (所有模式必须加载)
app.include_router(web_router)
app.include_router(auth_router, prefix="/api")
app.include_router(miot_router, prefix="/api")
app.include_router(ha_router, prefix="/api")
app.include_router(mcp_router, prefix="/api")

# [关键修改] 可选路由 (根据 Lite Mode 决定是否加载)
if not LITE_MODE:
    logger.info("Loading full feature routers (Chat, Trigger, Model)...")
    app.include_router(chat_router, prefix="/api")
    app.include_router(trigger_router, prefix="/api")
    app.include_router(model_router, prefix="/api")
else:
    logger.info("Lite Mode active: Skipping Chat, Trigger, and Model routers.")


@app.get("/{full_path:path}")
async def spa_handler(full_path: str):
    """SPA route handler - catch all unmatched GET requests"""
    if full_path.startswith("api/"):
        return Response(status_code=404, content="404 Not Found")

    index_file = STATIC_DIR / "index.html"
    if index_file.exists():
        return FileResponse(str(index_file))
    else:
        return Response(status_code=404, content="404 Not Found")


@app.on_event("startup")
async def startup_event():
    """Application initialization operations on startup"""
    logger.info(f"Initializing application (Lite Mode: {LITE_MODE})...")

    # 总是启动 RTSP Server
    rtsp_server.start()

    try:
        init_database()
        logger.info("Database initialization completed")
    except Exception as e:
        logger.error("Database initialization failed: %s", e)
        raise

    logger.info("Application initialization completed")

    try:
        # initialize 方法内部已经处理了 LITE_MODE 的逻辑，这里直接调用即可
        await get_manager().initialize(callback=open_browser_async)
        logger.info("Manager initialization completed")
    except Exception as e:
        logger.error("Manager initialization failed: %s", e)
        raise


@app.on_event("shutdown")
async def shutdown_event():
    """Cleanup operations when application shuts down"""
    # [关键修改] 安全停止后台任务
    manager = get_manager()
    if not LITE_MODE and getattr(manager, "trigger_rule_runner", None):
        logger.info("Stopping trigger rule runner...")
        manager.trigger_rule_runner.stop()

    rtsp_server.stop()
    logger.info("Application is shutting down...")
    logger.info("Application has been shut down")


def _open_browser():
    """Delayed browser opening"""
    time.sleep(2)
    port = SERVER_CONFIG["port"]
    url = f"https://127.0.0.1:{port}"
    webbrowser.open(url)


def open_browser_async():
    """Open browser asynchronously"""
    browser_thread = threading.Thread(target=_open_browser)
    browser_thread.daemon = True
    browser_thread.start()


def start_server():
    """Start server and automatically open browser"""
    logger.debug("Debug log test - if you see this message, debug logging is enabled")
    logger.info(f"Starting Miloco server (Lite Mode: {LITE_MODE})...")

    log_config = get_uvicorn_log_config()
    update_localhost_cert(cert_path=SERVER_CONFIG["ssl_certfile"], key_path=SERVER_CONFIG["ssl_keyfile"])

    uvicorn.run(
        app,
        host=SERVER_CONFIG["host"],
        port=SERVER_CONFIG["port"],
        log_level=SERVER_CONFIG["log_level"],
        log_config=log_config,
        ssl_certfile=SERVER_CONFIG["ssl_certfile"],
        ssl_keyfile=SERVER_CONFIG["ssl_keyfile"]
    )