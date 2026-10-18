"""Guarded answer stream = llm.stream_tutor_reply + the judge + checkpoints.

    question ──► writer (Gemini/Groq/Claude, with retry + failover + mid-answer continuation)
                    │ the first JUDGE_HEAD_CHARS characters are HELD back
                    ├──► judge (local checks, then a small model, in parallel while the writer keeps producing text)
                    │        ok      -> held text is released, the rest streams straight through
                    │        not ok   -> the writer is stopped and restarted with the judge's instruction (JUDGE_MAX_RETRIES times)
                    └──► checkpoint writer (Redis): every released piece is saved in the background so a dead worker / provider can resume

Latency: the judge runs while the writer is still generating, so the student waits about max(head, judge), not head + judge.
Nothing is shown before it passes. If the judge is slow or down it fails open (see judge.py). After the last retry the answer is delivered
anyway (JUDGE_FAIL_POLICY=deliver) or replaced by a short apology (block), and either way the failure is logged.
"""
import asyncio
import logging

from . import config, judge, langpolicy, llm, state

log = logging.getLogger("luma.guard")

_END = object()
APOLOGY = {
    "en": "Sorry, I could not prepare a good answer right now. Please ask again in a moment.",
    "ur": "معذرت، میں ابھی اچھا جواب تیار نہیں کر سکا۔ براہ کرم تھوڑی دیر بعد دوبارہ پوچھیں۔",
    "ar": "عذرًا، لم أستطع تحضير إجابة جيدة الآن. يرجى المحاولة مرة أخرى بعد قليل.",
    "fa": "متأسفم، اکنون نتوانستم پاسخ خوبی آماده کنم. لطفاً کمی بعد دوباره بپرسید.",
    "de": "Entschuldigung, ich konnte gerade keine gute Antwort vorbereiten. Bitte frage in einem Moment noch einmal.",
}


def _is_no_notes_prefix(head: str) -> bool:
    h = head.strip()
    return bool(h) and (llm.NO_NOTES_MARK.startswith(h) or h.startswith(llm.NO_NOTES_MARK))


async def _attempt(message, level, language, history, subject, voice_mode, notes_mode, lesson_mode, instruction, resume_text,
                   last_attempt, use_model, writer, stats, outcome):
    """One run of the writer. Yields the pieces that may be shown. Sets outcome['failed'] to the verdict when the judge rejected the start."""
    gen = llm.stream_tutor_reply(message, level, language, history, subject, voice_mode, notes_mode, lesson_mode,
                                 extra_instructions=instruction, continue_text=resume_text)
    q: asyncio.Queue = asyncio.Queue()

    async def pump():
        try:
            async for piece in gen:
                await q.put(piece)
            await q.put(_END)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            await q.put(exc)

    pump_task = asyncio.create_task(pump())
    item_task = asyncio.ensure_future(q.get())
    verdict_task: asyncio.Task | None = None
    held: list[str] = []
    head_len = 0
    released = False
    ended = False

    def start_judge():
        return asyncio.create_task(judge.judge(message, "".join(held), language, level, subject, use_model=use_model))

    def take(v: judge.Verdict) -> bool:
        """Record a verdict. True = release the held text."""
        stats["verdicts"].append(v)
        stats["judge_ms"] += v.ms
        if v.ok:
            return True
        if last_attempt:
            return config.JUDGE_FAIL_POLICY != "block"
        outcome["failed"] = v
        return False

    try:
        while True:
            wait_for = {item_task}
            if verdict_task is not None and not verdict_task.done() and not released:
                wait_for.add(verdict_task)
            done, _ = await asyncio.wait(wait_for, return_when=asyncio.FIRST_COMPLETED)

            if verdict_task is not None and verdict_task in done and not released:
                v = verdict_task.result()
                if take(v):
                    released = True
                    if not v.ok:
                        await _log_reject(v, language, outcome, delivered=True)
                    for h in held:
                        writer.add(h)
                        yield h
                    held = []
                elif outcome.get("failed") is not None:
                    return
                else:                                              # block policy on the last attempt
                    outcome["blocked"] = v
                    await _log_reject(v, language, outcome, delivered=False)
                    return

            if item_task in done:
                item = item_task.result()
                if item is _END:
                    ended = True
                elif isinstance(item, BaseException):
                    # The writer died with text still held: show it if the cheap local checks pass (the student gets what exists), then fail.
                    if not released and held and judge.local("".join(held), language).ok and not _is_no_notes_prefix("".join(held)):
                        for h in held:
                            writer.add(h)
                            yield h
                    raise item
                else:
                    if released:
                        writer.add(item)
                        yield item
                    else:
                        held.append(item)
                        head_len += len(item)
                        head = "".join(held)
                        if _is_no_notes_prefix(head):
                            if head.strip().startswith(llm.NO_NOTES_MARK):
                                released = True
                                for h in held:
                                    yield h
                                held = []
                        elif verdict_task is None and head_len >= config.JUDGE_HEAD_CHARS:
                            verdict_task = start_judge()
                if not ended:
                    item_task = asyncio.ensure_future(q.get())

            if ended:
                if not released:
                    head = "".join(held)
                    if head.strip() and not _is_no_notes_prefix(head):
                        v = await (verdict_task or start_judge())
                        if take(v):
                            if not v.ok:
                                await _log_reject(v, language, outcome, delivered=True)
                        elif outcome.get("failed") is None:
                            outcome["blocked"] = v
                            await _log_reject(v, language, outcome, delivered=False)
                            return
                        else:
                            return
                    for h in held:
                        writer.add(h)
                        yield h
                return
    finally:
        for t in (item_task, verdict_task):
            if t is not None and not t.done():
                t.cancel()
        pump_task.cancel()
        await asyncio.gather(pump_task, item_task, *([verdict_task] if verdict_task else []), return_exceptions=True)
        try:
            await gen.aclose()
        except Exception:
            pass


async def guarded_reply(message: str, level: str, language: str, history: list[dict], subject: str = "", *, voice_mode: bool = False,
                        notes_mode: bool = False, lesson_mode: bool = False, turn_id: str | None = None, resume_text: str = "",
                        extra_instructions: str = "", stats: dict | None = None):
    """Yield the answer in `language`. `resume_text` is what an earlier worker already showed (it is NOT yielded again; the writer continues it).
    `stats` receives {"attempts", "verdicts", "judge_ms"} for tests and logs."""
    stats = stats if stats is not None else {}
    stats.update(attempts=0, verdicts=[], judge_ms=0)
    writer = state.CheckpointWriter(turn_id)
    try:
        if not config.JUDGE_ENABLED:
            stats["attempts"] = 1
            async for piece in llm.stream_tutor_reply(message, level, language, history, subject, voice_mode, notes_mode, lesson_mode,
                                                      extra_instructions=extra_instructions, continue_text=resume_text):
                writer.add(piece)
                yield piece
            return
        instruction = extra_instructions
        total = 1 + max(0, config.JUDGE_MAX_RETRIES)
        for n in range(total):
            stats["attempts"] = n + 1
            outcome: dict = {}
            async for piece in _attempt(message, level, language, history, subject, voice_mode, notes_mode, lesson_mode, instruction,
                                        resume_text, n == total - 1, not lesson_mode, writer, stats, outcome):
                yield piece
            if "blocked" in outcome:
                yield APOLOGY.get(language, APOLOGY["en"])
                return
            failed = outcome.get("failed")
            if failed is None:
                return
            await _log_reject(failed, language, outcome, delivered=False)
            instruction = (extra_instructions + " " if extra_instructions else "") + "REVIEWER FEEDBACK on your previous attempt (fix this): " + failed.instruction
    finally:
        await writer.close()


async def _log_reject(v: judge.Verdict, language: str, outcome: dict, delivered: bool) -> None:
    if outcome.get("logged_" + str(delivered)):
        return
    outcome["logged_" + str(delivered)] = True
    await state.log_failure("judge_reject", "; ".join(v.issues) or "rejected", language=language, source=v.source,
                            action="delivered_anyway" if delivered else "regenerate")


def final_check(text: str, language: str) -> tuple[bool, str]:
    """After the whole answer was streamed (non-blocking use): did it drift out of the language later on?"""
    return langpolicy.check_language(text, language, min_letters=80)
