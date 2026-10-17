import asyncio
import re
import time

import httpx

from . import config

BASE = "https://api.elevenlabs.io/v1"


def _headers():
    if not config.ELEVENLABS_API_KEY:
        raise RuntimeError("ELEVENLABS_API_KEY is not set. Add it to your .env file.")
    return {"xi-api-key": config.ELEVENLABS_API_KEY}


EDGE_VOICES = {
    "en": {"female": "en-US-AvaNeural", "male": "en-US-AndrewNeural"},
    "ur": {"female": "ur-PK-UzmaNeural", "male": "ur-PK-AsadNeural"},
    "ar": {"female": "ar-SA-ZariyahNeural", "male": "ar-SA-HamedNeural"},
    "fa": {"female": "fa-IR-DilaraNeural", "male": "fa-IR-FaridNeural"},
}


async def _edge_tts(text: str, gender: str, language: str) -> bytes:
    import edge_tts

    voice = EDGE_VOICES.get(language, EDGE_VOICES["en"]).get(gender, EDGE_VOICES["en"]["female"])
    audio = b""
    async for chunk in edge_tts.Communicate(text, voice).stream():
        if chunk["type"] == "audio":
            audio += chunk["data"]
    if not audio:
        raise RuntimeError("no audio returned")
    return audio


_tts_cache: dict = {}
_TTS_CACHE_MAX = 300


async def text_to_speech(text: str, gender: str = "female", language: str = "en") -> bytes:
    key = (config.TTS_PROVIDER, text, gender, language)
    if key in _tts_cache:
        return _tts_cache[key]
    audio, from_primary = await _synthesize(text, gender, language)
    if from_primary:  # don't cache fallback audio, so ElevenLabs takes over as soon as it works
        if len(_tts_cache) >= _TTS_CACHE_MAX:
            _tts_cache.pop(next(iter(_tts_cache)))
        _tts_cache[key] = audio
    return audio


async def warm_up():
    """Open a first connection for every voice so the first real reply isn't slow."""
    if config.TTS_PROVIDER == "elevenlabs":
        try:
            await _synthesize("Hello.", "female", "en")  # detects an unusable account before the first real request
        except Exception:
            pass
    for language in EDGE_VOICES:
        for gender in ("female", "male"):
            try:
                await _edge_tts(".", gender, language)
            except Exception:
                pass


async def _edge_with_retry(text: str, gender: str, language: str) -> bytes:
    # The free service sometimes stalls; a quick retry is usually much faster than waiting.
    for attempt in range(3):
        try:
            return await asyncio.wait_for(_edge_tts(text, gender, language), timeout=4 if attempt < 2 else 15)
        except Exception:
            if attempt == 2:
                raise


async def _elevenlabs_tts(text: str, gender: str) -> bytes:
    voice_id = config.ELEVENLABS_VOICE_ID_MALE if gender == "male" else config.ELEVENLABS_VOICE_ID_FEMALE
    async with httpx.AsyncClient(timeout=30) as client:
        res = await client.post(
            f"{BASE}/text-to-speech/{voice_id}",
            headers=_headers(),
            json={"text": text, "model_id": "eleven_multilingual_v2"},
        )
        res.raise_for_status()
        return res.content


_eleven_blocked_until = 0.0


async def _synthesize(text: str, gender: str, language: str) -> tuple[bytes, bool]:
    """Returns (audio, cacheable). cacheable is False only for a transient ElevenLabs
    account failure, so that fallback audio isn't cached and ElevenLabs is retried later."""
    global _eleven_blocked_until
    # ElevenLabs' configured voices (Allison/George) are English speakers; fed Urdu/Arabic/Persian
    # script they go outside their training and can render with the wrong-sounding gender/pitch.
    # The Microsoft neural voices are native speakers of these languages with a verified gender,
    # so always use them for non-English regardless of TTS_PROVIDER.
    if config.TTS_PROVIDER == "elevenlabs" and language == "en":
        if time.time() >= _eleven_blocked_until:
            try:
                return await _elevenlabs_tts(text, gender), True
            except Exception:
                # Account/quota problem: skip ElevenLabs for 10 minutes and use the free voices
                _eleven_blocked_until = time.time() + 600
        return await _edge_with_retry(text, gender, language), False
    return await _edge_with_retry(text, gender, language), True


# A short sample in the target language nudges Whisper to use the right script
# (e.g. Urdu in Arabic script instead of Hindi in Devanagari).
STT_PROMPTS = {
    "ur": "یہ اردو زبان میں ایک تعلیمی سوال ہے۔",
    "ar": "هذا سؤال تعليمي باللغة العربية.",
    "fa": "این یک پرسش آموزشی به زبان فارسی است.",
}
_DEVANAGARI = re.compile(r"[ऀ-ॿ]")


async def _groq_transcribe(audio: bytes, filename: str, content_type: str, language: str | None = None) -> dict:
    """One Whisper call. Returns {"text", "lang" (Whisper's language name), "conf" (mean segment log-prob)}."""
    data = {"model": config.GROQ_STT_MODEL, "temperature": "0", "response_format": "verbose_json"}
    if language:
        data["language"] = language
        if language in STT_PROMPTS:
            data["prompt"] = STT_PROMPTS[language]
    async with httpx.AsyncClient(timeout=120) as client:
        for attempt in range(4):
            res = await client.post(
                f"{config.GROQ_BASE_URL}/audio/transcriptions",
                headers={"Authorization": f"Bearer {config.GROQ_API_KEY}"},
                data=data,
                files={"file": (filename, audio, content_type)},
            )
            if res.status_code == 429 and attempt < 3:
                # Free-tier rate limit: wait as long as Groq asks (capped), then retry
                try:
                    wait = float(res.headers.get("retry-after", "3"))
                except ValueError:
                    wait = 3.0
                await asyncio.sleep(min(max(wait, 1.0), 20.0))
                continue
            res.raise_for_status()
            body = res.json()
            logprobs = [s["avg_logprob"] for s in body.get("segments", []) if "avg_logprob" in s]
            return {
                "text": (body.get("text") or "").strip(),
                "lang": (body.get("language") or "").lower(),
                "conf": sum(logprobs) / len(logprobs) if logprobs else -5.0,
            }


# Whisper reports Urdu speech as "urdu" or "hindi" (they sound the same); we always want Urdu script.
_WHISPER_LANG = {"english": "en", "urdu": "ur", "hindi": "ur", "arabic": "ar", "persian": "fa"}
_CONF_MARGIN = 0.10  # the other language must be clearly more confident to override the user's choice


async def speech_to_text_partial(audio: bytes, filename: str, content_type: str, language: str | None) -> dict:
    """A single fast Whisper pass, for the live caption shown while the user is still
    talking. speech_to_text() below runs two passes (hinted + auto-detect) plus retries to
    get the most accurate final language/script, which is too slow to call on every audio
    chunk during streaming. The caption here trades a little accuracy for speed; the final
    transcript on stop still goes through the full speech_to_text()."""
    if config.VOICE_PROVIDER != "groq":
        return await speech_to_text(audio, filename, content_type, language)
    if not config.GROQ_API_KEY:
        raise RuntimeError("GROQ_API_KEY is not set. Add it to your .env file.")
    hint = language if language in ("en", "ur", "ar", "fa") else None
    result = await _groq_transcribe(audio, filename, content_type, hint)
    code = hint or _WHISPER_LANG.get(result["lang"], "en")
    return {"text": result["text"], "language": code}


async def speech_to_text(audio: bytes, filename: str, content_type: str, language: str | None) -> dict:
    """Returns {"text": ..., "language": code}.

    Two Whisper passes run together: one forced to the language the user selected, and one
    auto-detecting. The user's choice wins unless auto-detection found a *different* language and
    is clearly more confident (i.e. the person spoke another language than the one selected).
    """
    if config.VOICE_PROVIDER == "groq":
        if not config.GROQ_API_KEY:
            raise RuntimeError("GROQ_API_KEY is not set. Add it to your .env file.")
        hint = language if language in ("en", "ur", "ar", "fa") else None
        hinted, auto = await asyncio.gather(
            _groq_transcribe(audio, filename, content_type, hint),
            _groq_transcribe(audio, filename, content_type, None),
        )
        auto_code = _WHISPER_LANG.get(auto["lang"])
        chosen_code = hint
        result = hinted
        if hint is None:
            chosen_code, result = auto_code or "en", auto
        elif auto_code and auto_code != hint and auto["conf"] > hinted["conf"] + _CONF_MARGIN:
            chosen_code = auto_code
            # redo with the detected language forced, so we get the right script (Urdu, not Hindi)
            result = await _groq_transcribe(audio, filename, content_type, auto_code)
        if chosen_code == "ur" and _DEVANAGARI.search(result["text"]):
            result = await _groq_transcribe(audio, filename, content_type, "ur")
        return {"text": result["text"], "language": chosen_code}

    data = {"model_id": "scribe_v1"}
    if language:
        data["language_code"] = language
    async with httpx.AsyncClient(timeout=120) as client:
        res = await client.post(
            f"{BASE}/speech-to-text",
            headers=_headers(),
            data=data,
            files={"file": (filename, audio, content_type)},
        )
        res.raise_for_status()
        return {"text": res.json().get("text", ""), "language": language or "en"}
