"""The language rule: the language SELECTED in the interface decides what is spoken and written. It changes only when the student changes
the selector or explicitly asks for another language. The language the question is asked in does not matter."""
import re

import pytest
from google.genai import types

from sdb import reset_database, sdb  # noqa: F401
from backend import config, db, langpolicy, live
from test_live import FakeSession, audio, cid_of, client, collect, done, fake_notes, heard, install, said, start  # noqa: F401  (fixtures)

URDU_Q = "کمپیوٹر میں ریکرشن کیا ہوتی ہے؟ آسان انداز میں سمجھائیں۔"


def interrupted():
    return types.LiveServerMessage(server_content=types.LiveServerContent(interrupted=True))


def spoke_in(sess, i=0):
    """The language the model was told to elaborate in, from the tool response of its i-th notes call."""
    return re.search(r"aloud, in (\w+)", sess.tool_responses[i][0].response["result"]).group(1)


def lock_notes(sess):
    """Language-lock notes sent to the voice mid-session (client_content without turn_complete)."""
    return [c[0].parts[0].text for c in sess.client_content if c[1] is False and c[0].parts[0].text.startswith("(Language:")]


# ------------------------------------------------------------------ explicit requests (pure function)
@pytest.mark.parametrize("text,expected", [
    ("Explain recursion in Urdu", "ur"), ("I need you to explain this to me in urdu", "ur"), ("talk to me in German", "de"),
    ("Please speak Arabic", "ar"), ("switch to English", "en"), ("now in english please", "en"), ("Can you answer in Persian?", "fa"),
    ("urdu mein samjhao", "ur"), ("اردو میں سمجھائیں", "ur"), ("مجھے اردو میں بتائیں", "ur"), ("इसको उर्दू में बताएं", "ur"),
    ("Erkläre mir das bitte auf Deutsch", "de"), ("sprich auf Englisch", "en"), ("اشرح لي بالعربية", "ar"), ("به فارسی توضیح بده", "fa"),
    ("بالإنجليزية من فضلك", "en"), ("English please", "en"), ("what is recursion in urdu", "ur"), ("go back to English", "en"),
    ("ab jetzt auf Deutsch", "de"), ("reply in english, not urdu", "en"), ("explain in simple Urdu", "ur"), ("speak german", "de"),
    ("please talk to me in Urdu from now on", "ur"), ("Can you explain that in Arabic?", "ar"),
])
def test_explicit_language_requests_are_recognised(text, expected):
    assert langpolicy.explicit_request(text) == expected


@pytest.mark.parametrize("text", [
    "What is recursion?", "Translate hello to Urdu", "How do you say thanks in Urdu", "What does 'the' mean in Urdu",
    "Explain the Urdu language grammar", "ریکرشن کیا ہے؟", "Erkläre mir Rekursion", "how is this used in English literature",
    "Explain arrays in C++ and give an example", "teach me about Arabic numerals", "", "meaning of recursion in urdu", URDU_Q,
    "Why do we use English words in code?", "x" * 700 + " in urdu",
])
def test_ordinary_questions_never_change_the_language(text):
    assert langpolicy.explicit_request(text) is None


@pytest.mark.parametrize("text,target,ok", [
    ("Recursion is a function that calls itself until a base case stops it, and every call waits for the next.", "en", True),
    ("ریکرشن ایک فنکشن ہے جو خود کو کال کرتا ہے اور base case پر رک جاتا ہے، جیسے دو آئینے آمنے سامنے۔", "ur", True),
    ("Recursion is a function that calls itself until a base case stops it, and every call waits for the next.", "ur", False),
    ("recursion aik function hai jo khud ko call karta hai aur base case par ruk jata hai, aap dekhiye", "ur", False),   # Roman Urdu
    ("रिकर्शन एक फंक्शन है जो खुद को कॉल करता है और बेस केस पर रुक जाता है, आप देखिए यह कैसे काम करता है", "ur", False),   # Hindi
    ("ریکرشن ایک فنکشن ہے جو خود کو کال کرتا ہے اور base case پر رک جاتا ہے، جیسے دو آئینے آمنے سامنے۔", "en", False),
    ("Rekursion ist eine Funktion, die sich selbst aufruft, bis ein Basisfall sie stoppt, und das ist wichtig für Bäume.", "de", True),
    ("Recursion is a function that calls itself until a base case stops it, and every call waits for the next.", "de", False),
    ("Rekursion ist eine Funktion, die sich selbst aufruft, bis ein Basisfall sie stoppt, und das ist wichtig für Bäume.", "en", False),
    ("التكرار الذاتي هو دالة تستدعي نفسها حتى تتوقف عند الحالة الأساسية، وهذا مهم جدا في هذه المادة", "ar", True),
    ("بازگشت تابعی است که خودش را صدا می‌زند تا به حالت پایه برسد و این برای درخت‌ها بسیار مهم است", "fa", True),
])
def test_language_check(text, target, ok):
    assert langpolicy.check_language(text, target)[0] is ok


def test_code_and_math_do_not_count_towards_the_language():
    urdu = "ریکرشن کی مثال یہ ہے اور اس میں ہر قدم پر مسئلہ چھوٹا ہو جاتا ہے۔\n```python\ndef fact(n):\n    return 1 if n <= 1 else n * fact(n - 1)\n```\n$n! = n \\cdot (n-1)!$"
    assert langpolicy.check_language(urdu, "ur")[0] is True
    assert langpolicy.check_language("short", "ur")[0] is True, "too short to judge"


# ------------------------------------------------------------------ typed questions
def test_english_selected_urdu_question_still_gets_english_everywhere(client, monkeypatch):
    """The reported flow: the interface says English, the question is in Urdu -> English text AND English voice instructions."""
    sess = FakeSession([[audio(10), said("Think of two mirrors."), done()]])
    install(monkeypatch, sess)
    fake_notes(monkeypatch, ["# Recursion\nA function that calls itself."])
    with client.websocket_connect("/api/live") as ws:
        start(ws, language="en")
        ready = ws.receive_json()
        assert ready["language"] == "en"
        ws.send_json({"type": "text", "text": URDU_Q})
        frames, _ = collect(ws)
    assert spoke_in(sess) == "English", "notes and voice are in the SELECTED language"
    assert not [f for f in frames if f["type"] == "language"], "the page is not told to switch"
    assert "only in English" in sess.sent_text[0] and "اردو" not in sess.sent_text[0]
    assert [f for f in frames if f["type"] == "turn_start"][0]["language"] == "en"


def test_urdu_selected_english_question_gets_urdu(client, monkeypatch):
    sess = FakeSession([[audio(10), said("آئینے کی مثال سوچیں۔"), done()]])
    install(monkeypatch, sess)
    fake_notes(monkeypatch, ["# ریکرشن\nایک فنکشن۔"])
    with client.websocket_connect("/api/live") as ws:
        start(ws, language="ur")
        ws.receive_json()
        ws.send_json({"type": "text", "text": "What is recursion?"})
        collect(ws)
    assert spoke_in(sess) == "Urdu" and "اردو رسم الخط" in sess.sent_text[0]


def test_explicit_request_switches_text_and_voice_and_it_sticks(client, monkeypatch):
    """English first; 'explain this in urdu' switches text + voice to Urdu; the next plain question stays Urdu; 'back to English' returns."""
    sess = FakeSession([[audio(10), said("Think of two mirrors."), done()],
                        [audio(10), said("آئینے کی مثال۔"), done()],
                        [audio(10), said("ایک اور مثال۔"), done()],
                        [audio(10), said("Back to English."), done()]])
    install(monkeypatch, sess)
    fake_notes(monkeypatch, ["# Notes\nbody text"])
    with client.websocket_connect("/api/live") as ws:
        start(ws, language="en")
        ws.receive_json()
        ws.send_json({"type": "text", "text": "What is recursion?"})
        collect(ws)
        ws.send_json({"type": "text", "text": "I need you to explain this to me in urdu"})
        f2, _ = collect(ws)
        ws.send_json({"type": "text", "text": "And what about loops?"})
        f3, _ = collect(ws)
        ws.send_json({"type": "text", "text": "ok now back to English please"})
        f4, _ = collect(ws)
    assert [spoke_in(sess, i) for i in range(4)] == ["English", "Urdu", "Urdu", "English"]
    lang2 = [f for f in f2 if f["type"] == "language"]
    assert lang2 == [{"type": "language", "language": "ur", "reason": "requested"}]
    assert not [f for f in f3 if f["type"] == "language"], "no new switch for a plain question"
    assert [f for f in f4 if f["type"] == "language"][0]["language"] == "en"
    assert "اردو رسم الخط" in sess.sent_text[1] and "اردو رسم الخط" in sess.sent_text[2] and "only in English" in sess.sent_text[3]
    locks = lock_notes(sess)
    assert len(locks) == 2 and "اردو" in locks[0] and "English" in locks[1], "the voice itself is told about each switch"
    assert [f for f in f2 if f["type"] == "turn_start"][0]["language"] == "ur"


def test_changing_the_selector_during_a_lesson_changes_the_next_answers(client, monkeypatch):
    sess = FakeSession([[audio(10), said("Guten Tag."), done()]])
    install(monkeypatch, sess)
    fake_notes(monkeypatch, ["# Hash\nA hash table maps keys to buckets."])
    with client.websocket_connect("/api/live") as ws:
        start(ws, language="en")
        ws.receive_json()
        ws.send_json({"type": "set_language", "language": "de"})
        assert ws.receive_json() == {"type": "language", "language": "de", "reason": "selector"}
        ws.send_json({"type": "set_language", "language": "klingon"})       # invalid values are ignored
        ws.send_json({"type": "text", "text": "What is a hash table?"})
        collect(ws)
    assert spoke_in(sess) == "German" and "Deutsch" in sess.sent_text[0]
    assert "NUR auf Deutsch" in lock_notes(sess)[0]


def test_reconnect_keeps_the_current_language(client, monkeypatch):
    """After Gemini drops and we reconnect, the new session's system prompt must already be in the CURRENT language."""
    first = FakeSession([[audio(30), said("آئینے۔")]])
    first.drop_when_empty = True
    second = FakeSession([[audio(20), said("جاری ہے۔"), done()]])
    sessions, cfgs = [first, second], []

    def connect(cfg):
        cfgs.append(cfg)
        return type("C", (), {"__aenter__": lambda s: _aw(sessions[len(cfgs) - 1]), "__aexit__": lambda s, *a: _aw(False)})()

    async def _aw(v):
        return v

    monkeypatch.setattr(live, "_connect", connect)
    monkeypatch.setattr(config, "LIVE_RECONNECTS", 2)
    fake_notes(monkeypatch, ["# x\ny"])
    with client.websocket_connect("/api/live") as ws:
        start(ws, language="en")
        ws.receive_json()
        ws.send_json({"type": "text", "text": "explain recursion in urdu"})
        collect(ws)
    assert "chose English" in cfgs[0].system_instruction and "chose Urdu" in cfgs[1].system_instruction


# ------------------------------------------------------------------ spoken questions
def test_spoken_request_for_another_language_switches_voice_and_notes(client, monkeypatch):
    sess = FakeSession([[heard("please explain recursion in urdu"), audio(10), said("آئینے۔"), done()]])
    install(monkeypatch, sess)
    fake_notes(monkeypatch, ["# ریکرشن\nایک فنکشن جو خود کو کال کرتا ہے۔"])
    with client.websocket_connect("/api/live") as ws:
        start(ws, language="en")
        ws.receive_json()
        ws.send_bytes(b"\x00\x00" * 1600)
        frames, _ = collect(ws)
    assert spoke_in(sess) == "Urdu"
    assert [f for f in frames if f["type"] == "language"][0] == {"type": "language", "language": "ur", "reason": "requested"}
    assert "اردو" in lock_notes(sess)[0]


def test_spoken_urdu_question_while_english_selected_stays_english(client, monkeypatch):
    sess = FakeSession([[heard("ریکرشن کیا ہوتی ہے"), audio(10), said("Think of mirrors."), done()]])
    install(monkeypatch, sess)
    fake_notes(monkeypatch, ["# Recursion\nA function that calls itself."])
    with client.websocket_connect("/api/live") as ws:
        start(ws, language="en")
        ws.receive_json()
        ws.send_bytes(b"\x00\x00" * 1600)
        frames, _ = collect(ws)
    assert spoke_in(sess) == "English" and not [f for f in frames if f["type"] == "language"]


# ------------------------------------------------------------------ the voice itself is checked
def test_voice_speaking_the_wrong_language_is_restarted_once(client, monkeypatch):
    english = "Sure! Let me explain how recursion works in programming, step by step, with a small example."
    sess = FakeSession([[audio(10), said(english)],                                   # wrong: English although Urdu is selected
                        [interrupted(), audio(10), said("آئینے کی مثال سوچیں، ایک آئینہ دوسرے میں دیکھتا ہے۔"), done()]])
    install(monkeypatch, sess)
    fake_notes(monkeypatch, ["# ریکرشن\nایک فنکشن جو خود کو کال کرتا ہے۔"])
    with client.websocket_connect("/api/live") as ws:
        start(ws, language="ur")
        ws.receive_json()
        ws.send_json({"type": "text", "text": "What is recursion?"})
        frames, _ = collect(ws)
    fixes = [c for c in sess.client_content if c[1] is True]
    assert len(fixes) == 1 and "Language correction" in fixes[0][0].parts[0].text and "What is recursion?" in fixes[0][0].parts[0].text
    assert any(f["type"] == "interrupted" for f in frames), "the browser is told to drop the wrong-language audio it queued"
    assert frames[-1]["type"] == "turn_complete"


def test_voice_in_the_right_language_is_left_alone(client, monkeypatch):
    sess = FakeSession([[audio(10), said("ریکرشن ایک فنکشن ہے جو خود کو کال کرتا ہے اور base case پر رک جاتا ہے۔"), done()]])
    install(monkeypatch, sess)
    fake_notes(monkeypatch, ["# ریکرشن\nایک فنکشن جو خود کو کال کرتا ہے۔"])
    with client.websocket_connect("/api/live") as ws:
        start(ws, language="ur")
        ws.receive_json()
        ws.send_json({"type": "text", "text": "What is recursion?"})
        collect(ws)
    assert [c for c in sess.client_content if c[1] is True] == []


# ------------------------------------------------------------------ normal chat follows the same rule
def test_chat_uses_the_selected_language_and_switches_only_on_request(client, monkeypatch):
    from backend import llm
    seen = []

    async def gen(message, level, language, history, subject="", voice_mode=False, notes_mode=False, lesson_mode=False,
                  extra_instructions="", continue_text=""):
        seen.append(language)
        yield {"en": "Hello there, this is a perfectly ordinary answer that is long enough to be judged by the guard.",
               "de": "Hallo, das ist eine ganz normale Antwort, die und das ist wichtig für die Prüfung durch den Wächter lang genug ist."}[language]

    monkeypatch.setattr(llm, "stream_tutor_reply", gen)
    r1 = client.post("/api/chat", json={"message": URDU_Q, "language": "en"})
    assert r1.status_code == 200 and r1.headers["X-Language"] == "en"
    r2 = client.post("/api/chat", json={"message": "explain that in German please", "language": "en"})
    assert r2.headers["X-Language"] == "de"
    assert seen == ["en", "de"]


def test_english_lesson_about_germany_is_not_mistaken_for_german():
    """Real incident: the voice was restarted mid-answer because a lesson on German history contained German names and terms."""
    text = ("Let us start with the history of Germany. Before 1871 there was no single German state, only many kingdoms and duchies such as "
            "Prussia and Bavaria. The Weimar Republic, the Reichstag, the Wehrmacht and the Bundesrepublik Deutschland are names you will "
            "meet again and again, and the word Kaiser simply means emperor. Why did the Treaty of Versailles matter so much?")
    assert langpolicy.check_language(text, "en")[0] is True


def test_a_genuinely_german_answer_is_still_caught():
    de = ("Rekursion ist ein Verfahren, bei dem eine Funktion sich selbst aufruft, bis ein einfacher Fall erreicht ist, und das ist für die "
          "Prüfung sehr wichtig, weil man es nicht nur bei Bäumen, sondern auch bei vielen anderen Aufgaben braucht, wenn man sie klar lösen will.")
    assert langpolicy.check_language(de, "en")[0] is False
