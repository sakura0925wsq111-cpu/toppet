"""Local authenticated hardware actions. Speech never calls PetBrain or an LLM."""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
import hmac
import ipaddress
import math
import os
from pathlib import Path
import secrets
import threading
import time
import uuid

import numpy as np

from fastapi import HTTPException, Request
from pydantic import BaseModel, Field

from common.paths import LOGS_DIR
from common.audio_capture import save_pcm
from .playback_windows import PlaybackWindowWriter, MIN_WINDOW_BYTES
from .subtitle import paginate_subtitle_text


EMOTIONS = {"NEUTRAL", "HAPPY", "HEART", "SHY", "SAD", "ANGRY", "SURPRISED",
            "SLEEPY", "WORKING", "GREETING", "CELEBRATE", "READING", "EATING",
            "DRINKING", "COOL"}
TERMINAL = {"completed", "cancelled", "failed"}


class SpeakRequest(BaseModel):
    device_id: str = Field(min_length=1, max_length=128)
    text: str = Field(min_length=1, max_length=120)
    emotion: str = "NEUTRAL"
    request_id: str = Field(min_length=1, max_length=96)
    input_session_id: str | None = Field(default=None, pattern=r"^[A-Za-z0-9_-]{1,47}$")


class StopRequest(BaseModel):
    action_id: str = Field(min_length=1, max_length=48)


@dataclass
class DeviceLink:
    device_id: str
    capable: bool
    send_json: object
    send_audio: object
    activate: object
    release: object
    legacy_busy: bool = False
    legacy_session: str | None = None
    action_id: str | None = None
    resume_input: object = None


@dataclass
class Action:
    action_id: str
    request_id: str
    device_id: str
    text: str
    emotion: str
    link: DeviceLink
    input_session_id: str | None = None
    state: str = "accepted"
    error: str | None = None
    bytes_sent: int = 0
    output_complete: bool = False
    ready: asyncio.Event = field(default_factory=asyncio.Event)
    finished: asyncio.Event = field(default_factory=asyncio.Event)
    cancel: threading.Event = field(default_factory=threading.Event)
    task: asyncio.Task | None = None
    timing_origin: float = field(default_factory=time.monotonic)
    timings_ms: dict = field(default_factory=dict)

    def mark(self, node):
        self.timings_ms.setdefault(node, round((time.monotonic() - self.timing_origin) * 1000, 2))

    @property
    def wire_session(self):
        return self.input_session_id or self.action_id

    def snapshot(self):
        return {"action_id": self.action_id, "request_id": self.request_id,
                "device_id": self.device_id, "state": self.state,
                "bytes_sent": self.bytes_sent, "error": self.error,
                "input_session_id": self.input_session_id,
                "speech_mode": "buffered-full-context" if self.input_session_id else "streaming",
                "timings_ms": dict(self.timings_ms),
                "playback_confirmed": self.state == "completed"}


class HardwareControl:
    def __init__(self, tts_engine, *, token=None, handshake_timeout=3.0, playback_timeout=90.0):
        self.tts = tts_engine
        self._token = token
        self.handshake_timeout = handshake_timeout
        self.playback_timeout = playback_timeout
        self.speaker_gain_db = float(os.getenv("TOPPET_SPEAKER_GAIN_DB", "0"))
        if not math.isfinite(self.speaker_gain_db) or not -40 <= self.speaker_gain_db <= 0:
            raise ValueError("TOPPET_SPEAKER_GAIN_DB must be between -40 and 0 dB")
        self.devices: dict[str, DeviceLink] = {}
        self.actions: dict[str, Action] = {}

    def token(self):
        if self._token is None:
            path = Path(os.getenv("TOPPET_CONTROL_TOKEN_FILE", str(LOGS_DIR / "hardware-control.token")))
            path.parent.mkdir(parents=True, exist_ok=True)
            try:
                self._token = path.read_text(encoding="utf-8").strip()
            except FileNotFoundError:
                value = secrets.token_urlsafe(32)
                try:
                    with path.open("x", encoding="utf-8") as handle:
                        handle.write(value)
                    self._token = value
                except FileExistsError:
                    self._token = path.read_text(encoding="utf-8").strip()
            if not self._token:
                raise RuntimeError("hardware control token is empty")
        return self._token

    def authorize(self, request: Request):
        try:
            local = ipaddress.ip_address(request.client.host).is_loopback
        except (ValueError, AttributeError):
            local = False
        if not local:
            raise HTTPException(403, "hardware control is local-only")
        if not hmac.compare_digest(request.headers.get("authorization", ""), "Bearer " + self.token()):
            raise HTTPException(401, "invalid hardware control token")

    def attach(self, link):
        previous = self.devices.get(link.device_id)
        if previous is not None:
            self.detach(previous)
        self.devices[link.device_id] = link

    def detach(self, link):
        if self.devices.get(link.device_id) is link:
            self.devices.pop(link.device_id)
        action = self.actions.get(link.action_id)
        if action and action.state not in TERMINAL:
            self._fail(action, "DEVICE_DISCONNECTED")

    def _fail(self, action, error):
        action.error = error
        action.state = "failed"
        action.cancel.set()
        action.ready.set()
        action.finished.set()

    def event(self, link, command):
        kind = command.get("type")
        session = command.get("session_id")
        action = self.actions.get(link.action_id)
        if kind == "listen_start":
            link.legacy_busy = True
            link.legacy_session = session
            if action and action.state not in TERMINAL:
                action.state = "cancelled"
                action.cancel.set()
                action.ready.set()
                action.finished.set()
        elif (session == link.legacy_session and
              (kind == "abort" or (kind == "latency" and command.get("node") == "playback_finished"))):
            link.legacy_busy = False
            link.legacy_session = None
        if not action or session != action.wire_session or action.state in TERMINAL:
            return
        if kind == "control_ready":
            if command.get("status") != "ready":
                self._fail(action, "DEVICE_BUSY")
            action.ready.set()
        elif kind == "abort":
            action.state = "cancelled"
            action.cancel.set()
            action.ready.set()
            action.finished.set()
        elif kind == "latency":
            if command.get("node") == "first_i2s_write":
                action.mark('device_first_i2s')
                action.state = "playing"
            elif (command.get("node") == "playback_finished" and action.output_complete
                  and action.bytes_sent > 0):
                action.mark('device_playback_finished')
                action.state = "completed"
                action.finished.set()

    def status(self):
        return {"service": "toppet-hardware-control", "devices": [
            {"device_id": link.device_id, "online": True,
             "remote_control": link.capable, "busy": link.legacy_busy or bool(link.action_id),
             "action": self.actions[link.action_id].snapshot() if link.action_id in self.actions else None}
            for link in self.devices.values()]}

    def speak(self, body: SpeakRequest):
        text = body.text.strip()
        if not text or body.emotion not in EMOTIONS:
            raise HTTPException(422, "blank text or unsupported emotion")
        for action in self.actions.values():
            if action.request_id == body.request_id:
                if (action.device_id, action.text, action.emotion, action.input_session_id) != (body.device_id, text, body.emotion, body.input_session_id):
                    raise HTTPException(409, "request_id already used with different arguments")
                return action.snapshot()
        link = self.devices.get(body.device_id)
        if link is None:
            raise HTTPException(409, "DEVICE_OFFLINE")
        if not link.capable:
            raise HTTPException(409, "FIRMWARE_UPGRADE_REQUIRED: remote_control_v1")
        if link.action_id or (link.legacy_busy and not body.input_session_id):
            raise HTTPException(409, "DEVICE_BUSY")
        action_id = "ctl-" + uuid.uuid4().hex
        if body.input_session_id:
            if not callable(link.resume_input) or not link.resume_input(body.input_session_id):
                raise HTTPException(409, "STALE_HARDWARE_INPUT_SESSION")
        elif not link.activate(action_id):
            raise HTTPException(409, "DEVICE_BUSY")
        if len(self.actions) >= 128:
            for old_id, old in list(self.actions.items()):
                if old.state in TERMINAL:
                    del self.actions[old_id]
                    break
            else:
                if not body.input_session_id:
                    link.release(action_id)
                raise HTTPException(429, "too many hardware actions")
        action = Action(action_id, body.request_id, body.device_id, text, body.emotion, link,
                        input_session_id=body.input_session_id)
        self.actions[action_id] = action
        link.action_id = action_id
        action.task = asyncio.create_task(self._run(action))
        return action.snapshot()

    def get(self, action_id):
        if action_id not in self.actions:
            raise HTTPException(404, "unknown action")
        return self.actions[action_id].snapshot()

    async def stop(self, action_id):
        if action_id not in self.actions:
            raise HTTPException(404, "unknown action")
        action = self.actions[action_id]
        if action.state not in TERMINAL:
            action.state = "cancelled"
            action.cancel.set()
            action.ready.set()
            action.finished.set()
            await action.link.send_json({"type": "cancelled", "session_id": action.wire_session})
        return action.snapshot()

    async def _run(self, action):
        link = action.link
        loop = asyncio.get_running_loop()
        first_send_at = None

        def check():
            if action.cancel.is_set():
                raise InterruptedError("hardware action cancelled")

        async def emit(kind, **values):
            check()
            await link.send_json({"type": kind, "session_id": action.wire_session, **values})

        async def audio(chunk):
            nonlocal first_send_at
            check()
            if first_send_at is None:
                first_send_at = loop.time()
            await link.send_audio(action.wire_session, chunk)
            action.mark('first_audio_sent')
            action.bytes_sent += len(chunk.pcm)
            if action.input_session_id:
                # Bound queued old audio to about two seconds. Do not flood
                # TCP with an entire utterance ahead of a later cancel event.
                deadline = first_send_at + max(0, action.bytes_sent - 64000) / 32000
                if deadline > loop.time():
                    await asyncio.sleep(deadline - loop.time())
                else:
                    await asyncio.sleep(0)
                check()

        writer = PlaybackWindowWriter(window_bytes=MIN_WINDOW_BYTES, emit=emit,
                                      send_audio=audio, check_cancel=check, on_first_audio=lambda _: None)

        def synthesize(*, buffered=False):
            source = iter(self.tts.synthesize_stream(action.text, cancel_event=action.cancel))
            seen = False
            expected_sequence = 0
            pcm = bytearray()
            try:
                for chunk in source:
                    check()
                    if not chunk.pcm:
                        continue
                    if ((chunk.sample_rate, chunk.channels, chunk.bits_per_sample) != (16000, 1, 16)
                            or len(chunk.pcm) % 2 or chunk.sequence != expected_sequence):
                        raise ValueError("invalid TTS PCM format or sequence")
                    expected_sequence += 1
                    action.mark('first_tts_pcm')
                    seen = True
                    output_pcm = chunk.pcm
                    if action.input_session_id and self.speaker_gain_db:
                        samples = np.frombuffer(output_pcm, dtype='<i2').astype(np.float64)
                        output_pcm = np.rint(samples * 10 ** (self.speaker_gain_db / 20)).astype('<i2').tobytes()
                    if buffered:
                        pcm.extend(output_pcm)
                    else:
                        asyncio.run_coroutine_threadsafe(writer.write(output_pcm), loop).result(timeout=10)
                if not seen:
                    raise ValueError("TTS stream returned no PCM")
                return bytes(pcm)
            finally:
                close = getattr(source, "close", None)
                if callable(close):
                    close()

        try:
            action.state = "starting"
            if action.input_session_id:
                action.ready.set()  # The physical listen session already owns the device.
            else:
                await emit("control_start")
            await asyncio.wait_for(action.ready.wait(), self.handshake_timeout)
            check()
            action.state = "synthesizing"
            action.mark('synthesis_started')
            await emit("emotion", emotion=action.emotion)
            await emit("tts_start", format="pcm_s16le", sample_rate=16000, channels=1, bits_per_sample=16)
            if action.input_session_id:
                # One full-context synthesis preserves prosody. Buffer it once
                # before transport so GPU chunk jitter cannot starve the device.
                # Caption pages use real PCM length, without extra TTS requests.
                pcm = await asyncio.wait_for(asyncio.to_thread(synthesize, buffered=True), timeout=90)
                action.mark('synthesis_complete')
                check()
                await asyncio.to_thread(save_pcm, "tts-device", action.action_id, pcm,
                                        input_session_id=action.input_session_id, text=action.text,
                                        speaker_gain_db=self.speaker_gain_db,
                                        note="Samples prepared for device; not proof of speaker playback")
                pages = paginate_subtitle_text(action.text)
                weights = [max(1, len(page.strip())) for page in pages]
                total_weight = sum(weights)
                start, cumulative = 0, 0
                for index, (page, weight) in enumerate(zip(pages, weights)):
                    check()
                    cumulative += weight
                    end = len(pcm) if index == len(pages) - 1 else int(len(pcm) * cumulative / total_weight) // 2 * 2
                    writer.begin_sentence(page, action.emotion)
                    await writer.write(pcm[start:end])
                    writer.end_sentence(page)
                    start = end
            else:
                writer.begin_sentence(action.text, action.emotion)
                await asyncio.wait_for(asyncio.to_thread(synthesize), timeout=90)
                check()
                writer.end_sentence(action.text)
            await writer.finish()
            action.output_complete = True
            await emit("tts_stop", state="IDLE")
            if action.state not in TERMINAL:
                action.state = "awaiting_playback"
            await asyncio.wait_for(action.finished.wait(), self.playback_timeout)
        except InterruptedError:
            if action.state not in TERMINAL:
                action.state = "cancelled"
        except asyncio.TimeoutError:
            self._fail(action, "HANDSHAKE_TIMEOUT" if not action.ready.is_set() else "SPEECH_OR_PLAYBACK_TIMEOUT")
        except Exception:
            self._fail(action, "HARDWARE_OUTPUT_FAILED")
        finally:
            action.cancel.set()
            if action.state in {"failed", "cancelled"}:
                try:
                    await link.send_json({"type": "cancelled", "session_id": action.wire_session})
                except Exception:
                    pass
            link.release(action.wire_session)
            if action.input_session_id and link.legacy_session == action.input_session_id:
                link.legacy_busy = False
                link.legacy_session = None
            if link.action_id == action.action_id:
                link.action_id = None

    async def close(self):
        tasks = []
        for action in self.actions.values():
            if action.state not in TERMINAL:
                self._fail(action, "HOST_STOPPED")
            if action.task is not None:
                tasks.append(action.task)
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)


def install_control_routes(app, control):
    @app.get("/api/control/devices")
    async def devices(request: Request):
        control.authorize(request)
        return control.status()

    @app.post("/api/control/speak", status_code=202)
    async def speak(body: SpeakRequest, request: Request):
        control.authorize(request)
        return control.speak(body)

    @app.get("/api/control/actions/{action_id}")
    async def action(action_id: str, request: Request):
        control.authorize(request)
        return control.get(action_id)

    @app.post("/api/control/stop")
    async def stop(body: StopRequest, request: Request):
        control.authorize(request)
        return await control.stop(body.action_id)
