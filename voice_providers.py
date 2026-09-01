"""Pluggable text-to-speech providers for Agent Chat.

Dependency-light by design (httpx + stdlib only) so the module can be imported
by app.py, tests, and scripts without dragging in the web stack. app.py owns
request handling, caching, rate limits, and audit; this module owns "turn text
into audio bytes with provider X".

Providers
---------
- ``elevenlabs``  — ElevenLabs ``eleven_flash_v2_5`` (the latency champion).
                    Streams MP3 chunks, so playback can start on first bytes.
                    Needs ``ELEVENLABS_API_KEY``.
- ``google``      — Google Cloud Text-to-Speech REST (Chirp 3 HD voices).
                    Full-body MP3 only (no streaming — fine for short lines).
                    Needs ``GOOGLE_TTS_API_KEY``.
- ``kokoro``      — the EXISTING local neural voice. Pure delegation: app.py
                    hands us its proven synth/readiness callables, so behavior
                    is byte-identical to today.
- ``say``         — the EXISTING macOS system voice, same delegation contract.

Chain resolution
----------------
``resolve_chain(mode, available)`` turns the ``voice_provider`` setting
(``auto|elevenlabs|google|kokoro|say``) into an ordered list of provider ids:

- ``auto``: elevenlabs (if key) -> google (if key) -> openai (if key; the
  pre-existing app-level OpenAI TTS) -> kokoro -> say. With no cloud keys this
  is exactly today's chain — the system stays gracefully dormant until keys
  land.
- an explicit CLOUD pick puts that provider first with the rest of the auto
  chain behind it as a safety net (speech never goes silent because one key
  expired);
- an explicit LOCAL pick (kokoro/say) stays local-only — an explicit "use the
  free local voice" must never silently upgrade to a metered cloud voice.
"""
from __future__ import annotations

import base64
import re
import time
from typing import AsyncIterator, Awaitable, Callable, Optional

import httpx

# Provider ids, in auto-chain preference order. "openai" is the app's
# pre-existing OpenAI TTS path (it lives in app.py; resolve_chain only orders
# it). The two locals are always last: free, offline, always-there fallbacks.
AUTO_ORDER = ("elevenlabs", "google", "openai", "kokoro", "say")
LOCAL_IDS = frozenset({"kokoro", "say"})
PROVIDER_MODES = ("auto",) + AUTO_ORDER
# Providers an individual agent may be cast to on the Settings casting board
# (the voice_agent_providers map). "openai" is deliberately absent — it's the
# legacy default path, not a castable choice — and "Auto" is expressed by
# removing the agent's entry from the map entirely.
AGENT_CASTABLE_IDS = frozenset({"elevenlabs", "google", "kokoro", "say"})

ELEVENLABS_MODEL = "eleven_flash_v2_5"
ELEVENLABS_OUTPUT_FORMAT = "mp3_44100_128"
# "Adam" — ElevenLabs' warm, deep US-male premade voice, present on every
# account. Used when no per-agent map / default-voice setting exists yet; the
# live voice list (list_voices) can override it by name preference below.
ELEVENLABS_DEFAULT_VOICE = "pNInz6obpgDQGcFmaJgB"
ELEVENLABS_WARM_MALE_NAMES = ("Adam", "George", "Josh", "Brian", "Daniel", "Antoni")
# ElevenLabs voice ids are bare alphanumerics — this also guarantees a Kokoro
# id (af_heart etc., underscore) or blend recipe (commas) is never sent to the
# ElevenLabs API by way of the shared device_voice request field.
ELEVENLABS_VOICE_ID_RE = re.compile(r"^[A-Za-z0-9]{8,48}$")

# Chirp 3 HD is Google's most natural line; Charon is one of its male voices.
# Deliberately a *documented default*, not a hardcoded truth: the exact catalog
# shifts, so the Settings card surfaces the live voices:list and stores the
# chosen name in the voice_google_voice setting.
GOOGLE_DEFAULT_VOICE = "en-US-Chirp3-HD-Charon"
GOOGLE_VOICE_NAME_RE = re.compile(r"^[A-Za-z]{2,3}-[A-Za-z0-9-]{2,70}$")

_VOICES_CACHE_TTL_S = 600.0


def resolve_chain(mode: str, available: dict) -> list[str]:
    """Order provider ids for one synthesis attempt. See module docstring."""
    mode = (mode or "auto").strip().lower()
    auto = [pid for pid in AUTO_ORDER if available.get(pid)]
    if mode not in AUTO_ORDER:  # "auto", "", or anything unknown
        return auto
    if mode in LOCAL_IDS:
        local = [pid for pid in AUTO_ORDER if pid in LOCAL_IDS and available.get(pid)]
        if mode in local:
            return [mode] + [pid for pid in local if pid != mode]
        return local
    if not available.get(mode):
        return auto
    return [mode] + [pid for pid in auto if pid != mode]


def resolve_chain_for_agent(
    agent_id: str, mode: str, available: dict, agent_providers: Optional[dict]
) -> list[str]:
    """Per-agent casting layered over resolve_chain: when the casting board's
    ``voice_agent_providers`` map names a provider for this agent, that pick
    carries EXACTLY the semantics of an explicit global pick — a cloud pick
    leads with the rest of the auto chain behind it as a safety net, a local
    pick stays local-only and never silently upgrades to a metered cloud
    voice. Agents without an entry fall through to the global ``mode``."""
    try:
        pick = str((agent_providers or {}).get((agent_id or "").strip().lower(), "") or "")
    except Exception:
        pick = ""
    pick = pick.strip().lower()
    if pick in AUTO_ORDER:
        return resolve_chain(pick, available)
    return resolve_chain(mode, available)


class VoiceProviderError(RuntimeError):
    """Raised when a provider cannot produce audio (bad key, HTTP error…)."""


class VoiceProvider:
    """Interface. synthesize() returns (audio_bytes, mime); stream() yields
    audio chunks for providers that support it (default: one full-body chunk,
    so callers can treat every provider uniformly)."""

    id: str = ""
    label: str = ""
    media_type: str = "audio/mpeg"
    extension: str = ".mp3"
    supports_streaming: bool = False
    needs_key: bool = False

    def key(self) -> str:
        return ""

    def key_set(self) -> bool:
        return bool(self.key())

    def available(self) -> bool:
        return not self.needs_key or self.key_set()

    async def synthesize(
        self, text: str, voice: str, speed: Optional[float] = None
    ) -> tuple[bytes, str]:
        raise NotImplementedError

    async def stream(
        self, text: str, voice: str, speed: Optional[float] = None
    ) -> AsyncIterator[bytes]:
        audio, _mime = await self.synthesize(text, voice, speed)
        yield audio

    async def list_voices(self) -> list[dict]:
        return []


class ElevenLabsProvider(VoiceProvider):
    id = "elevenlabs"
    label = "ElevenLabs"
    supports_streaming = True
    needs_key = True

    def __init__(
        self,
        key_getter: Callable[[], str],
        model: str = ELEVENLABS_MODEL,
        voice_map_getter: Optional[Callable[[], dict]] = None,
        default_voice_getter: Optional[Callable[[], str]] = None,
        timeout: float = 60.0,
    ):
        self._key = key_getter
        self.model = model
        self._voice_map = voice_map_getter or (lambda: {})
        self._default_voice = default_voice_getter or (lambda: "")
        self._timeout = timeout
        self._voices_cache: list[dict] = []
        self._voices_cache_ts: float = 0.0

    def key(self) -> str:
        try:
            return (self._key() or "").strip()
        except Exception:
            return ""

    def voice_for_agent(self, agent_id: str, requested_voice: str = "") -> str:
        """Explicit valid voice id wins; then the per-persona map
        (voice_elevenlabs_voices setting), then the install default voice, then
        the warm-male premade — so Lead & co. sound right with zero setup."""
        requested_voice = (requested_voice or "").strip()
        if ELEVENLABS_VOICE_ID_RE.match(requested_voice):
            return requested_voice
        try:
            mapped = self._voice_map().get((agent_id or "").lower(), "")
        except Exception:
            mapped = ""
        if isinstance(mapped, str) and ELEVENLABS_VOICE_ID_RE.match(mapped.strip()):
            return mapped.strip()
        default = (self._default_voice() or "").strip()
        if ELEVENLABS_VOICE_ID_RE.match(default):
            return default
        # Prefer a warm male from the cached live list (fresh accounts always
        # carry the premades); fall back to the documented constant.
        for name in ELEVENLABS_WARM_MALE_NAMES:
            for voice in self._voices_cache:
                if str(voice.get("name", "")).strip().lower() == name.lower():
                    return str(voice.get("id") or ELEVENLABS_DEFAULT_VOICE)
        return ELEVENLABS_DEFAULT_VOICE

    def _headers(self) -> dict:
        key = self.key()
        if not key:
            raise VoiceProviderError("ElevenLabs API key is not configured")
        return {"xi-api-key": key, "Content-Type": "application/json"}

    @staticmethod
    def _error_detail(response) -> str:
        """ElevenLabs puts actionable text in detail.message (e.g. the pasted
        value was the key ID, not the sk_… secret). Surface it, bounded."""
        try:
            detail = response.json().get("detail")
            message = detail.get("message") if isinstance(detail, dict) else detail
            return str(message or "").strip()[:200]
        except Exception:
            return ""

    def _raise_http(self, what: str, response) -> None:
        detail = self._error_detail(response)
        raise VoiceProviderError(
            f"ElevenLabs {what} returned HTTP {response.status_code}"
            + (f" — {detail}" if detail else "")
        )

    def _tts_url(self, voice: str, streaming: bool) -> str:
        voice = (voice or "").strip()
        if not ELEVENLABS_VOICE_ID_RE.match(voice):
            raise VoiceProviderError(f"invalid ElevenLabs voice id: {voice!r}")
        suffix = "/stream" if streaming else ""
        return (
            f"https://api.elevenlabs.io/v1/text-to-speech/{voice}{suffix}"
            f"?output_format={ELEVENLABS_OUTPUT_FORMAT}"
        )

    def _payload(self, text: str) -> dict:
        return {"text": text, "model_id": self.model}

    async def synthesize(
        self, text: str, voice: str, speed: Optional[float] = None
    ) -> tuple[bytes, str]:
        async with httpx.AsyncClient(timeout=self._timeout) as client:
            response = await client.post(
                self._tts_url(voice, streaming=False),
                headers=self._headers(),
                json=self._payload(text),
            )
        if response.status_code >= 400:
            self._raise_http("speech", response)
        if not response.content:
            raise VoiceProviderError("ElevenLabs speech returned empty audio")
        return response.content, self.media_type

    async def stream(
        self, text: str, voice: str, speed: Optional[float] = None
    ) -> AsyncIterator[bytes]:
        got_audio = False
        async with httpx.AsyncClient(timeout=self._timeout) as client:
            async with client.stream(
                "POST",
                self._tts_url(voice, streaming=True),
                headers=self._headers(),
                json=self._payload(text),
            ) as response:
                if response.status_code >= 400:
                    self._raise_http("stream", response)
                async for chunk in response.aiter_bytes():
                    if chunk:
                        got_audio = True
                        yield chunk
        if not got_audio:
            raise VoiceProviderError("ElevenLabs stream returned empty audio")

    async def clone_voice(
        self, name: str, files: list[tuple[str, bytes, str]], description: str = ""
    ) -> dict:
        """Instant Voice Clone: multipart POST /v1/voices/add with the caller's
        recording(s). ``files`` is [(filename, audio_bytes, mime), …].

        Deliberately does NOT reuse _headers(): its Content-Type:
        application/json would clobber the multipart boundary httpx computes —
        only xi-api-key is sent, httpx owns the rest.
        """
        key = self.key()
        if not key:
            raise VoiceProviderError("ElevenLabs API key is not configured")
        data = {"name": name}
        if description:
            data["description"] = description
        async with httpx.AsyncClient(timeout=self._timeout) as client:
            response = await client.post(
                "https://api.elevenlabs.io/v1/voices/add",
                headers={"xi-api-key": key},
                data=data,
                files=[
                    ("files", (filename, payload, mime))
                    for filename, payload, mime in files
                ],
            )
        if response.status_code >= 400:
            self._raise_http("voice clone", response)
        try:
            body = response.json() or {}
        except Exception:
            body = {}
        voice_id = str(body.get("voice_id") or "").strip()
        if not ELEVENLABS_VOICE_ID_RE.match(voice_id):
            raise VoiceProviderError(
                f"ElevenLabs voice clone returned an unexpected voice id: {voice_id!r}"
            )
        # Drop the voices cache so the new voice shows up in pickers immediately
        # instead of after the TTL lapses.
        self._voices_cache = []
        self._voices_cache_ts = 0.0
        return {"id": voice_id, "name": name}

    async def list_voices(self) -> list[dict]:
        if not self.key_set():
            return []
        now = time.monotonic()
        if self._voices_cache and now - self._voices_cache_ts < _VOICES_CACHE_TTL_S:
            return self._voices_cache
        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.get(
                "https://api.elevenlabs.io/v1/voices", headers=self._headers()
            )
        if response.status_code >= 400:
            self._raise_http("voices", response)
        voices = []
        for entry in (response.json() or {}).get("voices", []):
            if not isinstance(entry, dict):
                continue
            voice_id = str(entry.get("voice_id") or "").strip()
            if not ELEVENLABS_VOICE_ID_RE.match(voice_id):
                continue
            labels = entry.get("labels") if isinstance(entry.get("labels"), dict) else {}
            descriptors = " · ".join(
                str(labels[k]) for k in ("gender", "accent", "description") if labels.get(k)
            )
            voices.append({
                "id": voice_id,
                "name": str(entry.get("name") or voice_id),
                "lang": str(labels.get("accent") or "en"),
                "description": descriptors,
            })
        self._voices_cache = voices
        self._voices_cache_ts = now
        return voices


class GoogleChirpProvider(VoiceProvider):
    id = "google"
    label = "Google (Chirp 3 HD)"
    needs_key = True

    def __init__(
        self,
        key_getter: Callable[[], str],
        voice_getter: Optional[Callable[[], str]] = None,
        voice_map_getter: Optional[Callable[[], dict]] = None,
        timeout: float = 60.0,
    ):
        self._key = key_getter
        self._voice = voice_getter or (lambda: GOOGLE_DEFAULT_VOICE)
        self._voice_map = voice_map_getter or (lambda: {})
        self._timeout = timeout
        self._voices_cache: list[dict] = []
        self._voices_cache_ts: float = 0.0

    def key(self) -> str:
        try:
            return (self._key() or "").strip()
        except Exception:
            return ""

    def voice_name(self, requested_voice: str = "") -> str:
        requested_voice = (requested_voice or "").strip()
        if GOOGLE_VOICE_NAME_RE.match(requested_voice):
            return requested_voice
        configured = (self._voice() or "").strip()
        if GOOGLE_VOICE_NAME_RE.match(configured):
            return configured
        return GOOGLE_DEFAULT_VOICE

    def voice_for_agent(self, agent_id: str, requested_voice: str = "") -> str:
        """Mirrors ElevenLabsProvider.voice_for_agent precedence exactly:
        explicit valid voice name wins; then the per-persona map (the
        voice_google_voices setting), then the install default voice
        (voice_google_voice), then GOOGLE_DEFAULT_VOICE."""
        requested_voice = (requested_voice or "").strip()
        if GOOGLE_VOICE_NAME_RE.match(requested_voice):
            return requested_voice
        try:
            mapped = self._voice_map().get((agent_id or "").lower(), "")
        except Exception:
            mapped = ""
        if isinstance(mapped, str) and GOOGLE_VOICE_NAME_RE.match(mapped.strip()):
            return mapped.strip()
        return self.voice_name()

    def _headers(self) -> dict:
        key = self.key()
        if not key:
            raise VoiceProviderError("Google TTS API key is not configured")
        # Header auth (not ?key=) keeps the key out of URLs and access logs.
        return {"X-Goog-Api-Key": key, "Content-Type": "application/json"}

    @staticmethod
    def _error_detail(response) -> str:
        """Google wraps actionable text in error.message (API not enabled, key
        restricted to the wrong API, quota…). Surface it, bounded — the exact
        counterpart of ElevenLabsProvider._error_detail."""
        try:
            error = response.json().get("error")
            message = error.get("message") if isinstance(error, dict) else error
            return str(message or "").strip()[:200]
        except Exception:
            return ""

    def _raise_http(self, what: str, response) -> None:
        detail = self._error_detail(response)
        raise VoiceProviderError(
            f"Google {what} returned HTTP {response.status_code}"
            + (f" — {detail}" if detail else "")
        )

    @staticmethod
    def _language_code(voice_name: str) -> str:
        parts = (voice_name or "").split("-")
        return "-".join(parts[:2]) if len(parts) >= 2 else "en-US"

    async def synthesize(
        self, text: str, voice: str, speed: Optional[float] = None
    ) -> tuple[bytes, str]:
        voice_name = self.voice_name(voice)
        audio_config: dict = {"audioEncoding": "MP3"}
        if speed is not None:
            audio_config["speakingRate"] = max(0.25, min(4.0, float(speed)))
        payload = {
            "input": {"text": text},
            "voice": {
                "languageCode": self._language_code(voice_name),
                "name": voice_name,
            },
            "audioConfig": audio_config,
        }
        async with httpx.AsyncClient(timeout=self._timeout) as client:
            response = await client.post(
                "https://texttospeech.googleapis.com/v1/text:synthesize",
                headers=self._headers(),
                json=payload,
            )
        if response.status_code >= 400:
            self._raise_http("TTS", response)
        content = str((response.json() or {}).get("audioContent") or "")
        if not content:
            raise VoiceProviderError("Google TTS returned empty audio")
        try:
            audio = base64.b64decode(content)
        except Exception as exc:  # malformed base64
            raise VoiceProviderError(f"Google TTS returned undecodable audio: {exc}")
        if not audio:
            raise VoiceProviderError("Google TTS returned empty audio")
        return audio, self.media_type

    async def list_voices(self) -> list[dict]:
        if not self.key_set():
            return []
        now = time.monotonic()
        if self._voices_cache and now - self._voices_cache_ts < _VOICES_CACHE_TTL_S:
            return self._voices_cache
        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.get(
                "https://texttospeech.googleapis.com/v1/voices?languageCode=en-US",
                headers=self._headers(),
            )
        if response.status_code >= 400:
            self._raise_http("voices", response)
        voices = []
        for entry in (response.json() or {}).get("voices", []):
            if not isinstance(entry, dict):
                continue
            name = str(entry.get("name") or "").strip()
            # Chirp 3 HD is the natural line this integration targets; keep the
            # earlier Chirp HD generation selectable too.
            if "Chirp" not in name or not GOOGLE_VOICE_NAME_RE.match(name):
                continue
            langs = entry.get("languageCodes") or []
            gender = str(entry.get("ssmlGender") or "").title()
            voices.append({
                "id": name,
                "name": name.replace("en-US-", "").replace("-", " "),
                "lang": str(langs[0]) if langs else "en-US",
                "description": gender,
            })
        self._voices_cache = voices
        self._voices_cache_ts = now
        return voices


class DelegatingProvider(VoiceProvider):
    """Wraps one of app.py's EXISTING synth paths (Kokoro / macOS say) without
    re-implementing it — same callables, same bytes, same failure modes."""

    def __init__(
        self,
        synth: Callable[[str, str], Awaitable[bytes]],
        ready: Callable[[], bool],
        catalog: Optional[Callable[[], list]] = None,
    ):
        self._synth = synth
        self._ready = ready
        self._catalog = catalog or (lambda: [])

    def available(self) -> bool:
        try:
            return bool(self._ready())
        except Exception:
            return False

    async def synthesize(
        self, text: str, voice: str, speed: Optional[float] = None
    ) -> tuple[bytes, str]:
        return await self._synth(text, voice), self.media_type

    async def list_voices(self) -> list[dict]:
        try:
            return list(self._catalog())
        except Exception:
            return []


class KokoroProvider(DelegatingProvider):
    id = "kokoro"
    label = "Kokoro (free, local)"
    media_type = "audio/wav"
    extension = ".wav"


class SayProvider(DelegatingProvider):
    id = "say"
    label = "macOS voice"
    media_type = "audio/mp4"
    extension = ".m4a"
