#!/usr/bin/env python3
import argparse
import io
import json
import os
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

# Let any op MPS can't run fall back to CPU instead of erroring; must be set
# before torch initializes.
os.environ.setdefault("PYTORCH_ENABLE_MPS_FALLBACK", "1")

import numpy as np
import soundfile as sf
import torch
from kokoro import KPipeline


def _pick_device() -> str:
    """Prefer Apple-GPU (MPS): CPU synthesis runs ~0.25x realtime, so a long
    reply takes ~a minute and reads as 'voice does not play'. Override with
    KOKORO_DEVICE=cpu if MPS ever misbehaves."""
    dev = os.environ.get("KOKORO_DEVICE", "").strip().lower()
    if dev:
        return dev
    if torch.backends.mps.is_available():
        return "mps"
    if torch.cuda.is_available():
        return "cuda"
    return "cpu"


VOICE_IDS = {
    # Female (American)
    "af_heart", "af_bella", "af_nicole", "af_sky",
    # Female (British)
    "bf_emma",
    # Male (American) — pitch-verified male via KPipeline (am_onyx ~87Hz … am_eric ~152Hz)
    "am_michael", "am_adam", "am_eric", "am_liam",
    "am_puck", "am_fenrir", "am_onyx", "am_echo",
    # Male (British) — render male with American G2P (bm_lewis ~92Hz … bm_george ~142Hz)
    "bm_george", "bm_lewis", "bm_daniel", "bm_fable",
}
# NB: any voice NOT in this set is silently replaced with af_heart (a FEMALE voice)
# in do_POST below — so this whitelist MUST stay in sync with the app's
# VOICE_KOKORO_VOICES catalog, or per-agent male voices come out female.
DEVICE = _pick_device()
print(f"[kokoro] synthesis device: {DEVICE}", flush=True)
PIPELINE = KPipeline(lang_code="a", repo_id="hexgrad/Kokoro-82M", device=DEVICE)
GENERATION_LOCK = threading.Lock()


def resolve_voice(voice: str) -> str:
    """Map a requested voice to what KPipeline should synthesize. A single known
    voice id passes through. A comma-separated blend (e.g. 'am_onyx,am_onyx,am_fenrir')
    is filtered to known ids and averaged by KPipeline on the fly — repeat an id to
    weight it. Unknown/empty input falls back to af_heart (a female voice)."""
    parts = [p.strip() for p in str(voice or "").split(",")]
    valid = [p for p in parts if p in VOICE_IDS]
    if not valid:
        return "af_heart"
    return ",".join(valid[:16])  # cap blend length defensively


def synthesize(text: str, voice: str, speed: float) -> bytes:
    with GENERATION_LOCK:
        chunks = [audio for _, _, audio in PIPELINE(text, voice=voice, speed=speed)]
    if not chunks:
        raise RuntimeError("Kokoro returned no audio")
    audio = chunks[0] if len(chunks) == 1 else np.concatenate(chunks)
    output = io.BytesIO()
    sf.write(output, audio, 24000, format="WAV", subtype="PCM_16")
    return output.getvalue()


class Handler(BaseHTTPRequestHandler):
    server_version = "AgentChatKokoro/1.0"

    def log_message(self, fmt, *args):
        print(f"[kokoro] {self.address_string()} {fmt % args}", flush=True)

    def send_json(self, status: int, payload: dict):
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path != "/health":
            self.send_json(404, {"error": "not found"})
            return
        self.send_json(200, {"status": "ok", "provider": "kokoro-local"})

    def do_POST(self):
        if self.path != "/speech":
            self.send_json(404, {"error": "not found"})
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            length = 0
        if length <= 0 or length > 32 * 1024:
            self.send_json(413, {"error": "invalid request size"})
            return
        try:
            payload = json.loads(self.rfile.read(length))
            text = str(payload.get("text") or "").strip()
            voice = str(payload.get("voice") or "af_heart")
            speed = max(0.7, min(1.3, float(payload.get("speed") or 0.95)))
            if not text or len(text) > 8000:
                raise ValueError("invalid text")
            voice = resolve_voice(voice)
            started = time.perf_counter()
            audio = synthesize(text, voice, speed)
            synthesis_ms = (time.perf_counter() - started) * 1000
        except Exception as exc:
            self.send_json(500, {"error": str(exc)[:300]})
            return
        self.send_response(200)
        self.send_header("Content-Type", "audio/wav")
        self.send_header("Content-Length", str(len(audio)))
        self.send_header("X-Voice-Name", voice)
        self.send_header("X-Kokoro-Synthesis-Ms", f"{synthesis_ms:.1f}")
        self.end_headers()
        self.wfile.write(audio)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=55013)
    args = parser.parse_args()
    server = ThreadingHTTPServer((args.host, args.port), Handler)
    print(f"Kokoro voice ready on http://{args.host}:{args.port}", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
