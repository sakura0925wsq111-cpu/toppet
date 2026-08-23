"""HTTP/WebSocket server for the TopPet ESP32 host link.

Run from the repository root:

    python -m host_service.server --host 0.0.0.0 --port 8765
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import logging
import os
import threading
import uuid
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, Header, HTTPException, Request, WebSocket
from fastapi.responses import JSONResponse
import uvicorn

from core.config import PetConfig

from .pipeline import HostPipeline, PipelineCancelled

logger = logging.getLogger("toppet.host_service")


def _new_session_id() -> str:
    return uuid.uuid4().hex


def _header_int(value: str | None, default: int) -> int:
    try:
        return int(value) if value is not None else default
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="invalid audio header") from exc


def create_app(pipeline: HostPipeline | None = None) -> FastAPI:
    service = pipeline or HostPipeline.from_config()

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        yield
        service.close()

    app = FastAPI(
        title="TopPet Host Service",
        version="0.1.0",
        lifespan=lifespan,
    )
    app.state.pipeline = service

    @app.get("/health")
    async def health() -> dict[str, object]:
        return {
            "status": "ready",
            "service": "toppet-host",
            "version": app.version,
            "character_id": service.character_id,
            "asr": service.asr_name,
            "tts": service.tts_name,
        }

    @app.get("/api/v1/info")
    async def info() -> dict[str, object]:
        return {
            "protocol": "toppet-host/v1",
            "character_id": service.character_id,
            "audio_input": {
                "formats": ["audio/wav", "application/octet-stream"],
                "sample_rate": 16_000,
                "channels": 1,
                "bits_per_sample": 16,
            },
            "websocket": "/ws",
            "http_voice": "/api/v1/voice",
        }

    @app.post("/api/v1/cancel/{session_id}")
    async def cancel(session_id: str) -> dict[str, object]:
        return {"cancelled": service.cancel_session(session_id), "session_id": session_id}

    @app.post("/api/v1/voice")
    async def voice(
        request: Request,
        x_top_pet_device_id: str | None = Header(default=None),
        x_top_pet_session_id: str | None = Header(default=None),
        x_top_pet_sample_rate: str | None = Header(default=None),
        x_top_pet_channels: str | None = Header(default=None),
        x_top_pet_bits: str | None = Header(default=None),
    ) -> JSONResponse:
        payload = await request.body()
        session_id = x_top_pet_session_id or _new_session_id()
        device_id = x_top_pet_device_id or "http-client"
        try:
            result = await service.process_audio(
                payload,
                session_id=session_id,
                device_id=device_id,
                content_type=request.headers.get("content-type", ""),
                sample_rate=_header_int(x_top_pet_sample_rate, 16_000),
                channels=_header_int(x_top_pet_channels, 1),
                bits_per_sample=_header_int(x_top_pet_bits, 16),
            )
        except PipelineCancelled as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        except Exception as exc:
            logger.exception("HTTP voice pipeline failed")
            raise HTTPException(status_code=500, detail=str(exc)) from exc

        return JSONResponse(
            {
                "protocol": "toppet-host/v1",
                "session_id": result.session_id,
                "device_id": result.device_id,
                "character_id": result.character_id,
                "transcript": result.transcript,
                "emotion": result.emotion,
                "reply": result.reply,
                "audio_format": result.audio_format,
                "audio_base64": base64.b64encode(result.audio).decode("ascii"),
            }
        )

    @app.websocket("/ws")
    async def websocket_endpoint(websocket: WebSocket) -> None:
        await websocket.accept()
        session_id = _new_session_id()
        device_id = "websocket-client"
        sample_rate = 16_000
        channels = 1
        bits_per_sample = 16
        content_type = "application/octet-stream"
        chunks: list[bytes] = []

        try:
            while True:
                message = await websocket.receive()
                if message.get("type") == "websocket.disconnect":
                    return
                if message.get("bytes") is not None:
                    chunks.append(message["bytes"])
                    continue
                if message.get("text") is None:
                    continue
                command = _json_message(message["text"])
                command_type = command.get("type")
                if command_type == "hello":
                    device_id = str(command.get("device_id", device_id))
                    await websocket.send_json({
                        "type": "hello_ack",
                        "protocol": "toppet-host/v1",
                        "session_id": session_id,
                        "character_id": service.character_id,
                    })
                elif command_type == "audio_start":
                    session_id = str(command.get("session_id", session_id))
                    sample_rate = int(command.get("sample_rate", 16_000))
                    channels = int(command.get("channels", 1))
                    bits_per_sample = int(command.get("bits_per_sample", 16))
                    content_type = str(command.get("content_type", content_type))
                    chunks.clear()
                    await websocket.send_json({
                        "type": "state",
                        "session_id": session_id,
                        "state": "LISTENING",
                    })
                elif command_type == "audio_end":
                    break
                elif command_type == "cancel":
                    service.cancel_session(session_id)
                    await websocket.send_json({
                        "type": "cancelled",
                        "session_id": session_id,
                    })
                    return

            async def emit(event: dict[str, object]) -> None:
                await websocket.send_json(event)

            processing_task = asyncio.create_task(service.process_audio(
                b"".join(chunks),
                session_id=session_id,
                device_id=device_id,
                content_type=content_type,
                sample_rate=sample_rate,
                channels=channels,
                bits_per_sample=bits_per_sample,
                emit=emit,
            ))
            receive_task = asyncio.create_task(websocket.receive())
            while True:
                done, _pending = await asyncio.wait(
                    {processing_task, receive_task},
                    return_when=asyncio.FIRST_COMPLETED,
                )
                if processing_task in done:
                    receive_task.cancel()
                    await asyncio.gather(receive_task, return_exceptions=True)
                    result = processing_task.result()
                    break

                message = receive_task.result()
                if message.get("type") == "websocket.disconnect":
                    service.cancel_session(session_id)
                    processing_task.cancel()
                    return
                if message.get("text") is not None:
                    command = _json_message(message["text"])
                    if command.get("type") == "cancel":
                        service.cancel_session(session_id)
                        try:
                            await processing_task
                        except (PipelineCancelled, asyncio.CancelledError):
                            pass
                        await _try_send(websocket, {
                            "type": "cancelled",
                            "session_id": session_id,
                        })
                        return
                receive_task = asyncio.create_task(websocket.receive())
            await websocket.send_json({
                "type": "audio_start",
                "session_id": result.session_id,
                "format": result.audio_format,
                "bytes": len(result.audio),
            })
            for offset in range(0, len(result.audio), 2048):
                await websocket.send_bytes(result.audio[offset:offset + 2048])
            await websocket.send_json({
                "type": "audio_end",
                "session_id": result.session_id,
                "state": "IDLE",
            })
        except PipelineCancelled:
            await _try_send(websocket, {
                "type": "cancelled",
                "session_id": session_id,
            })
        except ValueError as exc:
            await _try_send(websocket, {
                "type": "error",
                "session_id": session_id,
                "code": "INVALID_AUDIO_OR_TRANSCRIPT",
                "message": str(exc),
            })
        except Exception as exc:
            logger.exception("WebSocket pipeline failed")
            await _try_send(websocket, {
                "type": "error",
                "session_id": session_id,
                "code": "HOST_PIPELINE_ERROR",
                "message": str(exc),
            })

    return app


def _json_message(text: str) -> dict[str, Any]:
    import json

    value = json.loads(text)
    if not isinstance(value, dict):
        raise ValueError("WebSocket command must be a JSON object")
    return value


async def _try_send(websocket: WebSocket, message: dict[str, object]) -> None:
    try:
        await websocket.send_json(message)
    except Exception:
        logger.debug("WebSocket closed before error event could be sent")


def main() -> None:
    parser = argparse.ArgumentParser(description="TopPet host HTTP/WebSocket service")
    parser.add_argument("--host", default=os.getenv("TOPPET_HOST", "0.0.0.0"))
    parser.add_argument("--port", type=int, default=int(os.getenv("TOPPET_PORT", "8765")))
    parser.add_argument("--character", default="personal")
    parser.add_argument("--asr-model", default="small")
    parser.add_argument("--asr-device", default="cpu")
    parser.add_argument("--asr-compute-type", default="int8")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    pipeline = HostPipeline.from_config(
        PetConfig(),
        character_id=args.character,
        asr_model=args.asr_model,
        asr_device=args.asr_device,
        asr_compute_type=args.asr_compute_type,
    )
    app = create_app(pipeline)
    uvicorn.run(app, host=args.host, port=args.port, log_level="info")


if __name__ == "__main__":
    main()
