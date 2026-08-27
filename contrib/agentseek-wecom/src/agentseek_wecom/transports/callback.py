from __future__ import annotations

import asyncio
import json
from typing import Any, Literal

import uvicorn
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import Response
from loguru import logger

from agentseek_wecom.addressing import ConversationAddress, callback_conversation_address
from agentseek_wecom.config import WeComSettings
from agentseek_wecom.crypto import WeComCryptoError, WeComJsonCrypto
from agentseek_wecom.transport import InboundMessageHandler


class AiBotCallbackTransport:
    """HTTP callback transport for a WeCom AI Bot."""

    def __init__(self, *, settings: WeComSettings, tenant_id: str) -> None:
        self.settings = settings
        self.tenant_id = tenant_id.strip() or "default"
        self._inbound_handler: InboundMessageHandler | None = None
        self._crypto: WeComJsonCrypto | None = None
        self._server: uvicorn.Server | None = None
        self._server_task: asyncio.Task[None] | None = None
        self.app: FastAPI | None = self._build_app()

    @property
    def kind(self) -> Literal["aibot_callback"]:
        return "aibot_callback"

    def bind_inbound(self, handler: InboundMessageHandler) -> None:
        self._inbound_handler = handler

    def address_for(
        self,
        data: dict[str, Any],
        *,
        plaintext_userid: str | None = None,
    ) -> ConversationAddress:
        return callback_conversation_address(
            data,
            tenant_id=self.tenant_id,
            plaintext_userid=plaintext_userid,
        )

    async def start(self, stop_event: asyncio.Event) -> None:
        del stop_event
        if not self.settings.enabled:
            return
        self._crypto = self._build_crypto()
        if self._server_task is not None and not self._server_task.done():
            return
        app = self.app
        if app is None:
            raise RuntimeError("AI Bot callback transport requires an ASGI application")
        config = uvicorn.Config(
            app,
            host=self.settings.host,
            port=self.settings.port,
            loop="asyncio",
            log_level="info",
        )
        self._server = uvicorn.Server(config)
        self._server_task = asyncio.create_task(
            self._server.serve(),
            name="agentseek-wecom.callback-server",
        )
        await self._wait_until_started()

    async def stop(self) -> None:
        server = self._server
        task = self._server_task
        self._server = None
        self._server_task = None
        if server is not None:
            server.should_exit = True
        if task is None or task.done():
            return
        try:
            async with asyncio.timeout(max(0.1, self.settings.shutdown_timeout_seconds)):
                await task
        except TimeoutError:
            logger.warning("wecom.callback-server graceful shutdown timed out; cancelling server task")
            task.cancel()
            _, pending = await asyncio.wait(
                {task},
                timeout=max(0.1, self.settings.shutdown_timeout_seconds),
            )
            if pending:
                logger.error("wecom.callback-server task did not stop after cancellation")

    def _build_app(self) -> FastAPI:
        app = FastAPI()

        @app.get(self.settings.callback_path)
        async def verify_url(
            request: Request,
            msg_signature: str,
            timestamp: str,
            nonce: str,
            echostr: str,
            botid: str | None = None,
        ) -> Response:
            del request, botid
            crypto = self._require_crypto()
            try:
                plain_echo = crypto.verify_url(
                    msg_signature=msg_signature,
                    timestamp=timestamp,
                    nonce=nonce,
                    echostr=echostr,
                )
            except WeComCryptoError as exc:
                logger.warning("wecom.verify_url failed: {}", exc)
                plain_echo = "verify fail"
            return Response(content=plain_echo, media_type="text/plain")

        @app.post(self.settings.callback_path)
        async def handle_message(
            request: Request,
            botid: str | None = None,
            msg_signature: str | None = None,
            timestamp: str | None = None,
            nonce: str | None = None,
        ) -> Response:
            if not msg_signature or not timestamp or not nonce:
                raise HTTPException(status_code=400, detail="missing WeCom callback query parameters")

            crypto = self._require_crypto()
            encrypted_body = await request.body()
            try:
                plain_text = crypto.decrypt_message(
                    post_data=encrypted_body,
                    msg_signature=msg_signature,
                    timestamp=timestamp,
                    nonce=nonce,
                )
                data = json.loads(plain_text)
            except (WeComCryptoError, json.JSONDecodeError) as exc:
                logger.warning("wecom.decrypt failed: {}", exc)
                raise HTTPException(status_code=400, detail="decrypt failed") from exc

            if botid and not data.get("aibotid"):
                data["aibotid"] = botid

            handler = self._inbound_handler
            if handler is None:
                raise RuntimeError("AI Bot callback transport inbound handler is not bound")
            response_plain = await handler(data)
            if response_plain is None:
                return Response(content="success", media_type="text/plain")

            encrypted = crypto.encrypt_message(response_plain, nonce=nonce, timestamp=timestamp)
            return Response(content=encrypted.to_json(), media_type="text/plain")

        @app.get("/health")
        async def health() -> dict[str, Any]:
            return {"ok": True, "channel": "wecom", "enabled": self.settings.enabled}

        return app

    def _require_crypto(self) -> WeComJsonCrypto:
        if self._crypto is not None:
            return self._crypto
        self._crypto = self._build_crypto()
        return self._crypto

    def _build_crypto(self) -> WeComJsonCrypto:
        if not self.settings.token or not self.settings.encoding_aes_key:
            raise RuntimeError("WeCom token and EncodingAESKey are required")
        return WeComJsonCrypto(
            token=self.settings.token,
            encoding_aes_key=self.settings.encoding_aes_key,
            receive_id=self.settings.receive_id,
        )

    async def _wait_until_started(self) -> None:
        for _ in range(100):
            server = self._server
            if server is not None and server.started:
                return
            await asyncio.sleep(0.05)
