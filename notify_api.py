"""
notify_api.py — Endpoint HTTP /notify para enviar mensajes al grupo de Telegram.

Diseño:
  - Corre en un thread separado para no interferir con el event loop de python-telegram-bot.
  - Solo permite mandar al grupo BAI Group-OPERACIONES (hardcoded).
  - Autenticación por bearer token (env var NOTIFY_SECRET).
  - El TELEGRAM_TOKEN se pasa como parámetro, no se lee del módulo, para mantener una sola fuente de verdad.

Uso desde baigroup_bot.py:
    from notify_api import start_notify_api
    start_notify_api(telegram_token=TELEGRAM_TOKEN)
"""

import os
import logging
import threading

import httpx
import uvicorn
from fastapi import FastAPI, Header, HTTPException
from pydantic import BaseModel, Field

logger = logging.getLogger("notify_api")

# Solo se permite mandar a este chat. Hardcoded a propósito —
# el cliente NO puede elegir destino, es una mitigación clave.
ALLOWED_CHAT_ID = "-5265832156"
MAX_TEXT_LEN = 4000

# Se setea en start_notify_api()
_TELEGRAM_TOKEN: str | None = None


class NotifyRequest(BaseModel):
    text: str = Field(..., min_length=1, max_length=MAX_TEXT_LEN)


def _build_app() -> FastAPI:
    api = FastAPI(title="BAI Group notify API", docs_url=None, redoc_url=None, openapi_url=None)

    notify_secret = os.environ.get("NOTIFY_SECRET")
    if not notify_secret:
        raise RuntimeError("NOTIFY_SECRET no está seteado en las env vars")

    @api.get("/health")
    async def health():
        return {"status": "ok"}

    @api.post("/notify")
    async def notify(req: NotifyRequest, authorization: str = Header(default="")):
        # Auth — bearer token constante en env
        expected = f"Bearer {notify_secret}"
        if authorization != expected:
            logger.warning("notify: auth fallida")
            raise HTTPException(status_code=401, detail="unauthorized")

        if not _TELEGRAM_TOKEN:
            raise HTTPException(status_code=500, detail="telegram token not configured")

        url = f"https://api.telegram.org/bot{_TELEGRAM_TOKEN}/sendMessage"
        payload = {
            "chat_id": ALLOWED_CHAT_ID,
            "text": req.text,
            "disable_web_page_preview": True,
        }

        try:
            async with httpx.AsyncClient(timeout=10) as client:
                r = await client.post(url, json=payload)
            data = r.json()
        except Exception as e:
            logger.exception("notify: error llamando a Telegram")
            raise HTTPException(status_code=502, detail=f"telegram error: {e}")

        if not data.get("ok"):
            logger.error("notify: Telegram rechazó: %s", data)
            raise HTTPException(status_code=502, detail=data.get("description", "telegram rejected"))

        logger.info("notify: enviado al grupo (len=%d)", len(req.text))
        return {"ok": True, "message_id": data.get("result", {}).get("message_id")}

    return api


def start_notify_api(telegram_token: str, port: int | None = None) -> threading.Thread:
    """Arranca el endpoint en un thread daemon. No bloquea."""
    global _TELEGRAM_TOKEN
    _TELEGRAM_TOKEN = telegram_token

    app = _build_app()
    actual_port = port or int(os.environ.get("PORT", 8000))

    config = uvicorn.Config(
        app,
        host="0.0.0.0",
        port=actual_port,
        log_level="info",
        access_log=False,
    )
    server = uvicorn.Server(config)

    def _run():
        # uvicorn arma su propio event loop dentro del thread
        server.run()

    thread = threading.Thread(target=_run, name="notify-api", daemon=True)
    thread.start()
    logger.info("notify_api: escuchando en :%d", actual_port)
    print(f"🌐 notify_api escuchando en puerto {actual_port}")
    return thread
