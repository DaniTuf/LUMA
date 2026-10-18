"""Real-browser tests (Playwright) of the whole app on a laptop and on phone-sized touch devices, against tests/e2e/server.py
(real LUMA backend + frontend; Gemini Live, the text models and the judge are scripted fakes).

    python -m pytest tests/e2e -m e2e -q
Set E2E_HEADED=1 to watch it, E2E_SHOTS=<folder> to keep screenshots.
"""
import json
import os
import socket
import subprocess
import sys
import time
import urllib.request

import pytest
from playwright.sync_api import sync_playwright

pytestmark = pytest.mark.e2e
ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
SHOTS = os.environ.get("E2E_SHOTS", "")
EMAIL = "student@tuf.edu.pk"

URDU_Q = "ریکرشن کیا ہوتی ہے؟ آسان انداز میں سمجھائیں۔"


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture(scope="session")
def base():
    port = free_port()
    proc = subprocess.Popen([sys.executable, os.path.join("tests", "e2e", "server.py"), str(port)], cwd=ROOT,
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    url = f"http://127.0.0.1:{port}"
    for _ in range(100):
        try:
            if json.load(urllib.request.urlopen(url + "/api/health", timeout=1))["ok"]:
                break
        except Exception:
            time.sleep(0.2)
    else:
        proc.kill()
        raise RuntimeError("e2e server did not start: " + proc.stdout.read().decode(errors="replace")[-2000:])
    yield url
    proc.terminate()
    try:
        proc.wait(5)
    except Exception:
        proc.kill()


@pytest.fixture(scope="session")
def pw():
    with sync_playwright() as p:
        yield p


@pytest.fixture(scope="session")
def chromium(pw):
    b = pw.chromium.launch(headless=not os.environ.get("E2E_HEADED"), args=[
        "--use-fake-ui-for-media-stream", "--use-fake-device-for-media-stream", "--autoplay-policy=no-user-gesture-required"])
    yield b
    b.close()


DEVICES = {"desktop": dict(viewport={"width": 1280, "height": 800}),
           "pixel7": "Pixel 7",
           "iphone13": "iPhone 13"}


_OPEN = []
_COUNTER = [0]


@pytest.fixture(autouse=True)
def close_browser_contexts():
    """Every test gets fresh browser contexts and its own student: a lesson left open by one test must not block the next one."""
    yield
    while _OPEN:
        try:
            _OPEN.pop().close()
        except Exception:
            pass


def new_page(pw, browser, base, device, email=None, timing=None):
    _COUNTER[0] += 1
    email = email or f"student{_COUNTER[0]}@tuf.edu.pk"
    opts = DEVICES[device]
    kwargs = dict(pw.devices[opts]) if isinstance(opts, str) else dict(opts)
    kwargs.pop("default_browser_type", None)
    ctx = browser.new_context(permissions=["microphone"], base_url=base, **kwargs)
    _OPEN.append(ctx)
    ctx.request.post(base + "/api/auth/dev", data={"email": email})
    page = ctx.new_page()
    page.errors = []
    page.on("pageerror", lambda e: page.errors.append(str(e)))
    page.on("console", lambda m: page.errors.append(m.text) if m.type == "error" and "favicon" not in m.text and "fonts.g" not in m.text
            and "gsi" not in m.text and "ERR_" not in m.text and "Failed to load resource" not in m.text else None)
    if timing:
        page.add_init_script("window.LUMA_LIVE_TIMING = " + json.dumps(timing))
    page.goto("/")
    page.wait_for_selector("#app:not([hidden])")
    page.wait_for_function("document.getElementById('live-toggle') && !document.getElementById('live-toggle').hidden", timeout=10000)
    return page


def set_language(page, code):
    page.evaluate("document.getElementById('settings').open = true")
    page.select_option("#language", code)
    page.evaluate("document.getElementById('settings').open = false")


def ask(page, text, timeout=20000):
    """Type a question like a student would and wait until the lesson says it is the student's turn again."""
    n = page.evaluate("window.__live.turns")
    page.fill("#message", text)
    page.keyboard.press("Enter")
    page.wait_for_function(f"window.__live.turns > {n} && window.__live.state === 'your-turn'", timeout=timeout)


def last_notes(page):
    return page.locator(".msg.assistant .live-notes").last.inner_text()


def last_said(page):
    return page.locator(".msg.assistant .live-says-text").last.text_content()


def snap(page, name):
    if SHOTS:
        os.makedirs(SHOTS, exist_ok=True)
        page.screenshot(path=os.path.join(SHOTS, name + ".png"))


def no_overflow(page):
    return page.evaluate("document.documentElement.scrollWidth <= window.innerWidth + 1")


@pytest.fixture(params=["desktop", "pixel7", "iphone13"])
def device(request):
    return request.param


# ------------------------------------------------------------------ the basics on every device
def test_typed_questions_one_after_another_never_freeze(pw, chromium, base, device):
    """The reported bug, typed: question 1 works, then questions 2..6 must all be answered without refreshing the page."""
    page = new_page(pw, chromium, base, device)
    page.fill("#message", "What is recursion?")
    page.keyboard.press("Enter")
    page.wait_for_function("window.__live.state === 'your-turn'", timeout=20000)
    for i in range(2, 7):
        ask(page, f"Question number {i}: explain recursion again")
        assert "Recursion" in last_notes(page), i
    live = page.evaluate("window.__live")
    assert live["turns"] == 6 and live["audioChunks"] >= 6 and not live["errors"], live
    assert no_overflow(page)
    snap(page, f"{device}-after-6-questions")
    assert page.errors == []


def test_live_button_is_only_an_indicator_and_live_mode_is_always_on(pw, chromium, base, device):
    page = new_page(pw, chromium, base, device)
    btn = page.locator("#live-toggle")
    assert btn.get_attribute("data-state") == "idle"
    ask(page, "What is recursion?")                                  # no button press needed: the first question opens the live lesson
    page.wait_for_function("document.getElementById('live-toggle').dataset.state === 'on'")
    btn.click()                                                      # tapping never switches live mode off
    assert btn.get_attribute("data-state") == "on" and page.evaluate("liveWS !== null")
    page.click("#live-end")                                          # ending the lesson: the indicator goes idle, live mode stays on...
    page.wait_for_function("document.getElementById('live-toggle').dataset.state === 'idle'")
    ask(page, "And a second question after ending the lesson")      # ...and the next question reconnects by itself, with voice and notes
    assert page.evaluate("window.__live.audioChunks") > 0 and "Recursion" in last_notes(page)
    assert page.evaluate("localStorage.getItem('luma_live_auto')") is None


def test_layout_and_controls_fit_the_screen(pw, chromium, base, device):
    page = new_page(pw, chromium, base, device)
    assert no_overflow(page)
    for sel in ("#message", "#send", "#mic", "#live-toggle"):
        box = page.locator(sel).bounding_box()
        vp = page.viewport_size
        assert box and box["x"] >= 0 and box["x"] + box["width"] <= vp["width"] + 1, sel
        assert box["width"] >= 28 and box["height"] >= 28, f"{sel} is too small to tap"
    snap(page, f"{device}-home")


# ------------------------------------------------------------------ language: the selected language decides, a request switches it
def test_selected_language_wins_and_an_explicit_request_switches_text_and_voice(pw, chromium, base, device):
    page = new_page(pw, chromium, base, device)
    set_language(page, "en")
    ask(page, URDU_Q)                                                         # Urdu question, English selected
    assert "Recursion" in last_notes(page) and "Imagine two mirrors" in last_said(page), "English text AND English voice transcript"
    assert page.input_value("#language") == "en"
    ask(page, "I need you to explain this to me in urdu")                    # explicit request
    assert page.input_value("#language") == "ur", "the selector follows the request"
    assert "ریکرشن" in last_notes(page) and "آئینے" in last_said(page), "Urdu text AND Urdu voice transcript"
    assert page.locator(".msg.assistant .body").last.get_attribute("dir") == "rtl"
    ask(page, "What is a loop?")                                              # no new request: stays Urdu
    assert "ریکرشن" in last_notes(page) and page.input_value("#language") == "ur"
    snap(page, f"{device}-urdu")
    set_language(page, "de")                                                  # the student changes the selector himself
    ask(page, "What is a loop?")
    assert "Rekursion" in last_notes(page) and "Spiegel" in last_said(page)
    ask(page, "back to English please")
    assert page.input_value("#language") == "en" and "Recursion" in last_notes(page)
    assert page.evaluate("window.__live.languageSwitches") == ["ur", "de", "en"] or page.evaluate("window.__live.languageSwitches")[0] == "ur"
    assert page.errors == []


def test_voice_that_starts_in_the_wrong_language_is_restarted(pw, chromium, base):
    page = new_page(pw, chromium, base, "desktop")
    set_language(page, "ur")
    ask(page, "WRONGLANG what is recursion")
    assert "آئینے" in last_said(page) and "Imagine" not in last_said(page), "the English attempt was dropped; the Urdu restart is what is kept"


# ------------------------------------------------------------------ the microphone (the fake device plays a continuous tone)
def test_microphone_questions_keep_being_answered_on_a_phone(pw, chromium, base):
    """Phone, mic on: every few seconds the 'student' speaks. All of them must be answered (this was the freeze after question 1)."""
    page = new_page(pw, chromium, base, "pixel7")
    assert page.evaluate("window.__live.halfDuplex") is True, "touch devices use half-duplex so the speaker is not heard as a question"
    page.click("#mic")
    page.wait_for_function("window.__live.state === 'ready' || window.__live.state === 'listening'", timeout=15000)
    page.wait_for_function("document.getElementById('live-mic') && !document.getElementById('live-mic').disabled", timeout=15000)
    page.click("#live-mic")
    page.wait_for_function("window.__live.state === 'listening'", timeout=15000)
    page.wait_for_function("window.__live.turns >= 4", timeout=45000)
    live = page.evaluate("window.__live")
    assert live["turns"] >= 4 and live["audioChunks"] >= 4 and not live["errors"], live
    texts = page.locator(".msg.user .body").all_inner_texts()
    assert len(texts) >= 4 and all("spoken question number" in t for t in texts), texts
    assert len({t for t in texts}) == len(texts), "each question is its own message (never glued together)"
    snap(page, "pixel7-microphone")
    assert page.errors == []


def test_microphone_on_desktop_too(pw, chromium, base):
    page = new_page(pw, chromium, base, "desktop")
    page.click("#mic")
    page.wait_for_function("document.getElementById('live-mic') && !document.getElementById('live-mic').disabled", timeout=15000)
    page.click("#live-mic")
    page.wait_for_function("window.__live.turns >= 3", timeout=45000)
    assert not page.evaluate("window.__live.errors") and page.errors == []


# ------------------------------------------------------------------ connection problems
def test_dropped_websocket_reconnects_by_itself_and_the_next_question_works(pw, chromium, base, device):
    page = new_page(pw, chromium, base, device, timing={"watch": 500})
    ask(page, "What is recursion?")
    conv = page.evaluate("currentConvId")
    page.evaluate("liveWS.close()")                                           # the phone locked / the network switched
    page.wait_for_function("window.__live.reconnects >= 1 && window.__live.state === 'ready'", timeout=15000)
    ask(page, "And what is a base case?")
    assert page.evaluate("currentConvId") == conv, "same conversation after the reconnect"
    assert page.locator(".msg.user .body").count() == 2 and not page.evaluate("window.__live.errors")


def test_gemini_dropping_mid_lesson_is_recovered_by_the_server(pw, chromium, base):
    page = new_page(pw, chromium, base, "desktop")
    ask(page, "What is recursion?")
    urllib.request.urlopen(urllib.request.Request(base + "/__e2e/drop", method="POST"), timeout=5)
    page.wait_for_function("window.__live.state === 'reconnecting' || window.__live.state === 'ready'", timeout=10000)
    page.wait_for_function("window.__live.state === 'ready'", timeout=15000)
    ask(page, "Another question after the drop")
    assert "Recursion" in last_notes(page)


def test_half_open_socket_is_detected_and_replaced(pw, chromium, base):
    """Phones keep a dead socket 'open' silently: no frames at all for a while. The watchdog must replace it."""
    page = new_page(pw, chromium, base, "pixel7", timing={"dead": 1500, "watch": 300, "ping": 400})
    ask(page, "What is recursion?")
    page.evaluate("liveLastMsg = Date.now() - 60000; liveWatch()")
    page.wait_for_function("window.__live.reconnects >= 1 && window.__live.state === 'ready'", timeout=15000)
    ask(page, "Still works?")


def test_going_offline_and_back_online(pw, chromium, base):
    page = new_page(pw, chromium, base, "pixel7", timing={"watch": 500})
    ask(page, "What is recursion?")
    page.context.set_offline(True)
    page.evaluate("liveWS.close()")
    time.sleep(1.0)
    page.context.set_offline(False)
    page.wait_for_function("window.__live.state === 'ready'", timeout=20000)
    ask(page, "Back online")


def test_silent_teacher_does_not_freeze_the_ui(pw, chromium, base):
    """The voice never answers (server closes the turn after LIVE_FIRST_AUDIO_SECONDS): the student is told, and the next question works."""
    page = new_page(pw, chromium, base, "pixel7")
    page.fill("#message", "SILENT question")
    page.keyboard.press("Enter")
    page.wait_for_function("document.getElementById('status').textContent.includes('did not answer')", timeout=20000)
    page.wait_for_function("window.__live.state === 'your-turn'", timeout=5000)
    ask(page, "A normal question now")
    assert "Recursion" in last_notes(page)


def test_typing_while_the_teacher_is_still_talking_interrupts_and_answers_the_new_question(pw, chromium, base):
    page = new_page(pw, chromium, base, "desktop")
    page.fill("#message", "SLOW first question")
    page.keyboard.press("Enter")
    page.wait_for_function("window.__live.state === 'answering'")
    ask(page, "Second question while the first is slow")
    assert page.locator(".msg.user .body").count() == 2


def test_reloading_the_page_starts_cleanly(pw, chromium, base):
    page = new_page(pw, chromium, base, "pixel7")
    ask(page, "What is recursion?")
    page.reload()
    page.wait_for_selector("#app:not([hidden])")
    ask(page, "After a reload")
    assert not page.evaluate("window.__live.errors")


def test_closing_the_tab_releases_the_lesson_so_the_student_can_start_again_at_once(pw, chromium, base):
    """Real uvicorn + Redis lock: leaving (or the phone dropping the tab) must not leave 'already open in another tab' behind."""
    email = "returning@tuf.edu.pk"
    page = new_page(pw, chromium, base, "pixel7", email=email)
    ask(page, "What is recursion?")
    page.context.close()
    page2 = new_page(pw, chromium, base, "pixel7", email=email)
    ask(page2, "Starting again right away")
    assert not page2.evaluate("window.__live.errors")


def test_failure_log_is_collected_on_the_server(base):
    rows = json.load(urllib.request.urlopen(base + "/__e2e/failures", timeout=5))
    assert isinstance(rows, list)
