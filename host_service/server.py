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
from .voxcpm_service import VoxCPMServiceController, VoxCPMStartupError

logger = logging.getLogger("toppet.host_service")


def _new_session_id() -> str:
    return uuid.uuid4().hex


def _header_int(value: str | None, default: int) -> int:
    try:
        return int(value) if value is not None else default
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="invalid audio header") from exc


def create_app(
    pipeline: HostPipeline | None = None,
    voxcpm_service: VoxCPMServiceController | None = None,
) -> FastAPI:
    service = pipeline or HostPipeline.from_config()
    voice_service = voxcpm_service
    autostart_voxcpm = os.getenv("TOPPET_VOXCPM_AUTOSTART", "1").lower() not in {
        "0",
        "false",
        "no",
    }
    if voice_service is None and service.requires_voxcpm and autostart_voxcpm:
        voice_service = VoxCPMServiceController(
            max_wait=float(os.getenv("TOPPET_VOXCPM_STARTUP_TIMEOUT", "180"))
        )

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        try:
            if voice_service is not None:
                try:
                    await asyncio.to_thread(voice_service.start_and_wait)
                except VoxCPMStartupError:
                    logger.exception(
                        "Automatic VoxCPM2 startup failed; EdgeTTS fallback remains available"
                    )
            await asyncio.to_thread(service.preload)
            yield
        finally:
            service.close()
            if voice_service is not None:
                await asyncio.to_thread(voice_service.stop)

    app = FastAPI(
        title="TopPet Host Service",
        version="0.2.0",
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

    @app.get("/api/v2/info")
    async def info_v2() -> dict[str, object]:
        return {
            "protocol": "toppet-host/v2",
            "websocket": "/ws",
            "audio": {
                "format": "pcm_s16le",
                "sample_rate": 16_000,
                "channels": 1,
                "bits_per_sample": 16,
                "frame_ms": 20,
                "pcm_bytes_per_frame": 640,
            },
        }

    @app.get("/api/v2/latency")
    async def latency() -> dict[str, object]:
        return {
            "summary": service.latency.summary(),
            "records": service.latency.records(),
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
        try:
            first = await websocket.receive()
            if first.get("type") == "websocket.disconnect":
                return
            command = (
                _json_message(first["text"])
                if first.get("text") is not None
                else {}
            )
            if (
                command.get("type") == "hello"
                and command.get("protocol") == "toppet-host/v2"
            ):
                await _handle_v2_websocket(websocket, service, command)
            else:
                await _handle_v1_websocket(websocket, service, first)
        except Exception as exc:
            logger.exception("WebSocket pipeline failed")
            await _try_send(websocket, {
                "type": "error",
                "session_id": "connection",
                "code": "HOST_PIPELINE_ERROR",
                "message": str(exc),
            })

    return app


async def _handle_v1_websocket(
    websocket: WebSocket,
    service: HostPipeline,
    first_message: dict[str, object],
) -> None:
    """Compatibility implementation of the original one-shot v1 socket."""
    session_id = _new_session_id()
    device_id = "websocket-client"
    sample_rate = 16_000
    channels = 1
    bits_per_sample = 16
    content_type = "application/octet-stream"
    chunks: list[bytes] = []
    message: dict[str, object] | None = first_message
    while True:
        if message is None:
            message = await websocket.receive()
        if message.get("type") == "websocket.disconnect":
            return
        if message.get("bytes") is not None:
            chunks.append(message["bytes"])
            message = None
            continue
        command = _json_message(str(message["text"])) if message.get("text") else {}
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
        elif command_type in {"cancel", "abort"}:
            service.cancel_session(session_id)
            await websocket.send_json({"type": "cancelled", "session_id": session_id})
            return
        message = None

    async def emit(event: dict[str, object]) -> None:
        await websocket.send_json(event)

    try:
        result = await service.process_audio(
            b"".join(chunks),
            session_id=session_id,
            device_id=device_id,
            content_type=content_type,
            sample_rate=sample_rate,
            channels=channels,
            bits_per_sample=bits_per_sample,
            emit=emit,
        )
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
        await _try_send(websocket, {"type": "cancelled", "session_id": session_id})
    except ValueError as exc:
        await _try_send(websocket, {
            "type": "error",
            "session_id": session_id,
            "code": "INVALID_AUDIO_OR_TRANSCRIPT",
            "message": str(exc),
        })


async def _handle_v2_websocket(
    websocket: WebSocket,
    service: HostPipeline,
    hello: dict[str, Any],
) -> None:
    """Long-lived v2 connection with framed microphone and TTS PCM."""
    device_id = str(hello.get("device_id", "top-pet-unknown"))[:128]
    audio = hello.get("audio")
    if not isinstance(audio, dict):
        raise ValueError("v2 hello.audio is required")
    expected_audio = {
        "format": "pcm_s16le",
        "sample_rate": 16_000,
        "channels": 1,
        "bits_per_sample": 16,
        "frame_ms": 20,
    }
    for key, expected in expected_audio.items():
        if audio.get(key) != expected:
            raise ValueError(f"unsupported hello.audio.{key}")

    send_lock = asyncio.Lock()
    active_session: str | None = None
    pcm_chunks: list[bytes] = []
    pending_frame: dict[str, object] | None = None
    expected_sequence = 0
    processing_tasks: set[asyncio.Task] = set()

    async def send_json(message: dict[str, object]) -> None:
        async with send_lock:
            await websocket.send_json(message)

    await send_json({
        "type": "hello_ack",
        "protocol": "toppet-host/v2",
        "device_id": device_id,
        "character_id": service.character_id,
        "connection_id": _new_session_id(),
    })

    def cancel_active() -> str | None:
        nonlocal active_session, pending_frame
        old_session = active_session
        if old_session is not None:
            service.cancel_session(old_session)
        active_session = None
        pending_frame = None
        return old_session

    async def run_pipeline(session_id: str, pcm: bytes) -> None:
        nonlocal active_session
        first_audio_sent = False

        async def emit(event: dict[str, object]) -> None:
            if active_session != session_id:
                return
            await send_json(event)

        async def send_audio(chunk) -> None:
            nonlocal first_audio_sent
            if active_session != session_id:
                return
            async with send_lock:
                if active_session != session_id:
                    return
                await websocket.send_json({
                    "type": "tts_audio",
                    "session_id": session_id,
                    "sequence": chunk.sequence,
                    "bytes": len(chunk.pcm),
                    "is_final": chunk.is_final,
                    "buffer_bytes": 0,
                })
                await websocket.send_bytes(chunk.pcm)
            if not first_audio_sent:
                first_audio_sent = True
                service.latency.record(
                    "first_audio_frame_sent",
                    session_id=session_id,
                    device_id=device_id,
                    sequence=chunk.sequence,
                    buffer_bytes=len(chunk.pcm),
                )

        try:
            await service.process_streamed_audio(
                pcm,
                session_id=session_id,
                device_id=device_id,
                emit=emit,
                send_audio=send_audio,
            )
        except PipelineCancelled:
            if active_session == session_id:
                await send_json({"type": "cancelled", "session_id": session_id})
        except ValueError as exc:
            if active_session == session_id:
                await send_json({
                    "type": "error",
                    "session_id": session_id,
                    "code": "INVALID_AUDIO_OR_TRANSCRIPT",
                    "message": str(exc),
                })
        except Exception as exc:
            logger.exception("v2 pipeline failed session=%s", session_id)
            if active_session == session_id:
                await send_json({
                    "type": "error",
                    "session_id": session_id,
                    "code": "HOST_PIPELINE_ERROR",
                    "message": str(exc),
                })
        finally:
            if active_session == session_id:
                active_session = None

    try:
        while True:
            message = await websocket.receive()
            if message.get("type") == "websocket.disconnect":
                cancel_active()
                return
            binary = message.get("bytes")
            if binary is not None:
                if pending_frame is None or active_session is None:
                    await send_json({
                        "type": "error",
                        "session_id": active_session or "none",
                        "code": "UNFRAMED_PCM",
                        "message": "binary PCM must follow a mic_frame event",
                    })
                    continue
                meta = pending_frame
                pending_frame = None
                session_id = str(meta["session_id"])
                sequence = int(meta["sequence"])
                accept = bool(meta["accept"])
                if session_id != active_session or not accept:
                    continue
                if len(binary) != 640:
                    await send_json({
                        "type": "error",
                        "session_id": session_id,
                        "code": "INVALID_PCM_FRAME_SIZE",
                        "message": f"expected 640 bytes, got {len(binary)}",
                    })
                    continue
                if not pcm_chunks:
                    service.latency.record(
                        "first_audio_frame_received",
                        session_id=session_id,
                        device_id=device_id,
                        sequence=sequence,
                        buffer_bytes=len(binary),
                    )
                pcm_chunks.append(binary)
                continue

            if message.get("text") is None:
                continue
            command = _json_message(message["text"])
            command_type = command.get("type")
            if command_type == "listen_start":
                old_session = cancel_active()
                if old_session is not None:
                    await send_json({"type": "cancelled", "session_id": old_session})
                session_id = str(command.get("session_id", "")).strip()[:128]
                if not session_id:
                    raise ValueError("listen_start.session_id is required")
                active_session = session_id
                pcm_chunks.clear()
                expected_sequence = 0
                service.session_cancel_event(session_id)
                service.latency.record(
                    "session_created", session_id=session_id, device_id=device_id
                )
                await send_json({
                    "type": "state",
                    "session_id": session_id,
                    "state": "LISTENING",
                })
            elif command_type == "mic_frame":
                session_id = str(command.get("session_id", ""))
                sequence = int(command.get("sequence", -1))
                frame_bytes = int(command.get("bytes", -1))
                if pending_frame is not None:
                    await send_json({
                        "type": "error",
                        "session_id": session_id,
                        "code": "MISSING_PCM_PAYLOAD",
                        "message": "previous mic_frame had no binary payload",
                    })
                accept = session_id == active_session and frame_bytes == 640
                if accept and sequence != expected_sequence:
                    await send_json({
                        "type": "error",
                        "session_id": session_id,
                        "code": "PCM_SEQUENCE_MISMATCH",
                        "expected_sequence": expected_sequence,
                        "actual_sequence": sequence,
                    })
                    if sequence > expected_sequence:
                        missing = sequence - expected_sequence
                        pcm_chunks.extend([bytes(640)] * missing)
                    else:
                        accept = False
                if accept:
                    expected_sequence = sequence + 1
                pending_frame = {
                    "session_id": session_id,
                    "sequence": sequence,
                    "accept": accept,
                }
            elif command_type == "listen_stop":
                session_id = str(command.get("session_id", ""))
                if session_id != active_session:
                    continue
                if pending_frame is not None:
                    await send_json({
                        "type": "error",
                        "session_id": session_id,
                        "code": "MISSING_PCM_PAYLOAD",
                        "message": "listen_stop arrived before binary PCM",
                    })
                    pending_frame = None
                pcm = b"".join(pcm_chunks)
                service.latency.record(
                    "audio_end_received",
                    session_id=session_id,
                    device_id=device_id,
                    sequence=max(0, expected_sequence - 1),
                    buffer_bytes=len(pcm),
                )
                task = asyncio.create_task(run_pipeline(session_id, pcm))
                processing_tasks.add(task)
                task.add_done_callback(processing_tasks.discard)
            elif command_type == "abort":
                session_id = str(command.get("session_id", active_session or ""))
                service.cancel_session(session_id)
                if active_session == session_id:
                    active_session = None
                    pcm_chunks.clear()
                    pending_frame = None
                await send_json({"type": "cancelled", "session_id": session_id})
            elif command_type == "ping":
                await send_json({"type": "pong", "session_id": active_session or ""})
            elif command_type != "hello":
                await send_json({
                    "type": "error",
                    "session_id": active_session or "none",
                    "code": "UNKNOWN_EVENT",
                    "message": str(command_type),
                })
    finally:
        cancel_active()
        if processing_tasks:
            await asyncio.gather(*processing_tasks, return_exceptions=True)


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
    parser.add_argument("--asr-model", default="base")
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
