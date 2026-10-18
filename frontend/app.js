const RTL_LANGUAGES = new Set(["ur", "ar", "fa"]);
const MATH_DELIMS = [
  { left: "$$", right: "$$", display: true },
  { left: "$", right: "$", display: false },
];
const AVATAR_SVG = '<img src="assets/tuf-logo-mark.png" alt="">';

const $ = (id) => document.getElementById(id);
const appEl = $("app");
const chatEl = $("chat");
const emptyEl = $("empty");
const formEl = $("chat-form");
const messageEl = $("message");
const levelEl = $("level");
const languageEl = $("language");
const subjectEl = $("subject");
const statusEl = $("status");
const sendEl = $("send");
const micEl = $("mic");
const speakEl = $("speak");
const speakingEl = $("speaking");

let currentConvId = null; // null = a new chat that hasn't been saved yet
let busy = false;

// ---------- Rendering ----------

// Some models emit \[...\] and \(...\); convert to $$...$$ and $...$ so marked
// doesn't strip the backslashes and KaTeX auto-render picks them up.
function normalizeMath(text) {
  return text
    .replace(/\\\[([\s\S]*?)\\\]/g, (_, m) => `$$${m}$$`)
    .replace(/\\\(([\s\S]*?)\\\)/g, (_, m) => `$${m}$`);
}

function renderInto(el, text) {
  if (!window.marked) { el.textContent = text || ""; return; } // library failed to load: still show the answer
  el.innerHTML = marked.parse(normalizeMath(text || ""));
  if (window.renderMathInElement) renderMathInElement(el, { delimiters: MATH_DELIMS });
}

function scrollToBottom() {
  chatEl.scrollTop = chatEl.scrollHeight;
}

function addMessage(role, text, lang) {
  emptyEl.hidden = true;
  const row = document.createElement("div");
  row.className = `msg ${role}`;
  if (role === "assistant") {
    const avatar = document.createElement("div");
    avatar.className = "avatar";
    avatar.innerHTML = AVATAR_SVG;
    row.appendChild(avatar);
  }
  const body = document.createElement("div");
  body.className = "body";
  if (RTL_LANGUAGES.has(lang)) body.dir = "rtl";
  if (lang) body.lang = lang;
  if (role === "user") body.textContent = text;
  else if (text) renderInto(body, text);
  row.appendChild(body);
  chatEl.appendChild(row);
  scrollToBottom();
  return body;
}

function showTyping(body) {
  body.innerHTML = '<span class="typing"><i></i><i></i><i></i></span>';
}

function setStatus(text) {
  statusEl.textContent = text || "";
}

// ---------- Composer ----------

function updateSend() {
  sendEl.disabled = busy || (!recorder && !messageEl.value.trim());
}

function autoGrow() {
  messageEl.style.height = "auto";
  messageEl.style.height = Math.min(messageEl.scrollHeight, 180) + "px";
}

messageEl.addEventListener("input", () => {
  autoGrow();
  updateSend();
});

messageEl.addEventListener("keydown", (e) => {
  if (e.key === "Enter" && !e.shiftKey && !e.isComposing) {
    e.preventDefault();
    if (!sendEl.disabled) formEl.requestSubmit();
  }
});

const voiceLangEl = $("voice-lang");

function applyLanguageDirection() {
  const rtl = RTL_LANGUAGES.has(languageEl.value);
  messageEl.dir = rtl ? "rtl" : "ltr";
  messageEl.lang = languageEl.value;
  voiceLangEl.value = languageEl.value; // keep the mic's language picker in sync
}
languageEl.addEventListener("change", () => {
  applyLanguageDirection();
  if (typeof liveSyncLanguage === "function") liveSyncLanguage();
});
voiceLangEl.addEventListener("change", () => {
  languageEl.value = voiceLangEl.value;
  applyLanguageDirection();
  if (typeof liveSyncLanguage === "function") liveSyncLanguage();
});

document.querySelectorAll(".chip").forEach((chip) =>
  chip.addEventListener("click", () => {
    messageEl.value = chip.textContent;
    autoGrow();
    updateSend();
    formEl.requestSubmit();
  })
);

$("new-chat").addEventListener("click", () => {
  if (busy) return;
  startNewChat();
  closeMenu();
});

function startNewChat() {
  if (typeof endLive === "function" && liveWS) endLive();
  stopSpeaking();
  currentConvId = null;
  chatEl.querySelectorAll(".msg").forEach((n) => n.remove());
  emptyEl.hidden = false;
  messageEl.value = "";
  autoGrow();
  updateSend();
  setStatus("");
  markActiveChat();
  messageEl.focus();
}

// Mobile sidebar
function closeMenu() {
  appEl.classList.remove("menu-open");
}
$("menu").addEventListener("click", () => appEl.classList.add("menu-open"));
$("backdrop").addEventListener("click", closeMenu);

// ---------- Chat ----------

let sentByVoice = false;

// The server appends this when the model hit its length limit mid-answer.
const TRUNC_MARK = "[[LUMA_TRUNCATED]]";

function addContinueButton(body) {
  const btn = document.createElement("button");
  btn.type = "button";
  btn.className = "continue-btn";
  btn.textContent = "Continue generating";
  btn.addEventListener("click", () => {
    btn.remove();
    messageEl.value = "Continue exactly where you stopped.";
    updateSend();
    formEl.requestSubmit();
  });
  body.appendChild(btn);
}

const GERMAN_WORDS = new Set("der die das den dem des und ist nicht ich du wir sie er es ein eine einen einem einer zu mit auf für von im am an wie was warum wann wo wer welche welcher kann können bitte erkläre erklären erklär mir mich uns wird werden sind sein hat haben oder aber auch noch nur dass wenn dann also kurz einfach beispiel bedeutet unterschied".split(" "));
const ENGLISH_WORDS = new Set("the and is are not you your we they it a an of to in on for with how what why when where who which can could please explain me my us be been has have or but also only that if then so simply example means difference does do".split(" "));

// German or English for Latin-script text; null = too short to tell (keep the selector). Only when the selector is English or German.
function latinLanguage(text) {
  if (!["en", "de"].includes(languageEl.value)) return null;
  const words = text.toLowerCase().match(/[a-zäöüß]+/g) || [];
  const de = words.filter((w) => GERMAN_WORDS.has(w)).length;
  const en = words.filter((w) => ENGLISH_WORDS.has(w)).length;
  if (/[äöüß]/i.test(text) && de >= en) return "de";
  if (de >= 2 && de > en) return "de";
  if (en >= 2 && en > de) return "en";
  return null;
}

// Guess the language from the script of the student's message (null = can't tell / Latin).
function detectLanguage(text) {
  // Hindi script (Devanagari) = a Hindustani question; LUMA answers it in Urdu
  const devanagari = (text.match(/[\u0900-\u097F]/g) || []).length;
  if (devanagari && devanagari >= (text.match(/[A-Za-z]/g) || []).length) return "ur";
  const arabicScript = (text.match(/[؀-ۿ]/g) || []).length;
  const latin = (text.match(/[A-Za-z]/g) || []).length;
  if (arabicScript === 0 || arabicScript < latin) return latin ? latinLanguage(text) : null;
  if (/[ٹڈڑںےہھ]/.test(text)) return "ur";
  if (/[ژی]/.test(text) && /[پچگ]/.test(text) && !/[ىي]/.test(text)) {
    return languageEl.value === "ur" ? "ur" : "fa";
  }
  return ["ur", "ar", "fa"].includes(languageEl.value) ? languageEl.value : "ar";
}

formEl.addEventListener("submit", async (e) => {
  e.preventDefault();
  if (!liveWS && liveAvailable() && liveAuto && !recorder) {
    const q = messageEl.value.trim();
    if (!q || busy) return;
    messageEl.value = "";
    autoGrow();
    updateSend();
    setStatus("Starting the live teacher…");
    if (await ensureLive()) {
      setStatus("");
      liveSendText(q);
      return;
    }
    messageEl.value = q; // could not start: fall back to the normal text chat below
    autoGrow();
    updateSend();
    setStatus("The live teacher is not available right now, so this is a normal text answer.");
  }
  if (liveWS || liveWanted) {
    const t = messageEl.value.trim();
    if (!t) return;
    if (!liveReady) {
      setStatus("Reconnecting to the live teacher…");
      try {
        await liveWaitReady(25000);
      } catch (_) {
        setStatus("Still reconnecting. Your question is kept: send it again in a moment.");
        return;
      }
      setStatus("");
    }
    messageEl.value = "";
    autoGrow();
    updateSend();
    liveSendText(t);
    return;
  }
  if (recorder) {
    finishRecording(true);
    return;
  }
  const message = messageEl.value.trim();
  if (!message || busy) return;

  const level = levelEl.value;
  const language = languageEl.value; // the SELECTED language decides the answer; the server switches it only on an explicit request
  // Voice in -> voice out; also when "Read answers aloud" is on.
  const voiceMode = sentByVoice || speakEl.checked;
  sentByVoice = false;

  stopSpeaking();
  addMessage("user", message, language);
  messageEl.value = "";
  autoGrow();
  busy = true;
  updateSend();

  const assistantBody = addMessage("assistant", "", language);
  showTyping(assistantBody);
  let fullText = "";
  let streamDone = false;
  // In voice mode the text stays hidden (typing indicator) until the audio actually starts,
  // so text and voice appear together instead of the text running ahead of the voice.
  holdText = voiceMode;
  renderLatest = () => {
    const text = fullText.replace(TRUNC_MARK, "");
    if (!text) return;
    renderInto(assistantBody, streamDone ? text.trim() : text);
    if (streamDone && fullText.includes(TRUNC_MARK) && !assistantBody.querySelector(".continue-btn")) {
      addContinueButton(assistantBody);
    }
    scrollToBottom();
  };
  if (voiceMode) {
    resetSpeechQueue();
    voiceT0 = performance.now();
    firstByteLogged = false;
    console.log("[voice-timing] 0ms: /api/chat request sent");
  }

  // The spoken lesson runs in parallel with the notes and is only heard, never shown or read from the notes.
  const lessonTask = voiceMode
    ? runLesson({ message, level, language, subject: subjectEl.value, conversation_id: currentConvId })
    : null;

  try {
    const res = await fetch("/api/chat", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        message,
        conversation_id: currentConvId,
        level,
        language,
        subject: subjectEl.value,
        voice_mode: false,
        notes_mode: voiceMode, // with voice, the screen gets written notes and the voice gets its own spoken lesson
      }),
    });

    if (res.status === 401) {
      assistantBody.closest(".msg").remove();
      showLogin("Your session expired. Please sign in again.");
      return;
    }
    if (!res.ok) {
      const err = await res.json().catch(() => ({}));
      holdText = false;
      assistantBody.textContent = err.detail || `Error: ${res.status}`;
      return;
    }
    const convId = res.headers.get("X-Conversation-Id");
    if (convId) currentConvId = convId;
    const answerLang = res.headers.get("X-Language");
    if (answerLang && answerLang !== languageEl.value) {
      liveFollowLanguage(answerLang, true);
      assistantBody.dir = RTL_LANGUAGES.has(answerLang) ? "rtl" : "";
      assistantBody.lang = answerLang;
    }

    const reader = res.body.getReader();
    const decoder = new TextDecoder();
    while (true) {
      const { done, value } = await reader.read();
      if (done) break;
      fullText += decoder.decode(value, { stream: true });
      const shown = fullText.replace(TRUNC_MARK, "");
      if (!holdText) renderLatest();
      if (voiceMode && !firstByteLogged) {
        firstByteLogged = true;
        console.log(`[voice-timing] ${Math.round(performance.now() - voiceT0)}ms: first text byte from /api/chat`);
      }
    }
    if (voiceMode) {
      const spoken = await lessonTask;
      // Greetings and small talk have no notes: show what the teacher said instead of nothing.
      if (fullText.includes(NO_NOTES_MARK)) fullText = spoken;
    }
    streamDone = true;
    if (!holdText) renderLatest(); // otherwise rendered when the voice starts
    if (voiceMode && !speechQueue.length && !playerRunning) releaseText(); // nothing to speak
  } catch (err) {
    holdText = false;
    assistantBody.textContent = "Network error: " + err.message;
  } finally {
    busy = false;
    updateSend();
    messageEl.focus();
    if (currentConvId) loadHistory();
  }
});

// ---------- Voice output (speaking animation) ----------
// Audio is generated on the server (neural voices), so it sounds the same in every browser.

let holdText = false;
let renderLatest = () => {};
function releaseText() {
  if (!holdText) return;
  holdText = false;
  renderLatest();
}

let currentAudio = null;
let speakToken = 0;

function setSpeaking(on) {
  speakingEl.hidden = !on;
}

function stopSpeaking() {
  if (typeof liveFlush === "function" && liveWS) liveFlush(true); // Stop: drop the rest of this live answer too
  speakToken++;
  speechQueue = [];
  if (currentAudio) {
    currentAudio.pause();
    currentAudio = null;
  }
  setSpeaking(false);
  releaseText();
}

$("stop-speaking").addEventListener("click", () => {
  stopSpeaking();
  if (liveWS && liveReady) liveWS.send(JSON.stringify({ type: "interrupt" })); // also stop the teacher generating (saves quota)
});
speakEl.addEventListener("change", () => {
  if (!speakEl.checked) stopSpeaking();
});

// ----- Theme (Default = TUF brand colors, Dark) -----

function applyTheme(theme) {
  if (theme === "dark") document.documentElement.dataset.theme = "dark";
  else delete document.documentElement.dataset.theme;
  document.querySelectorAll("#theme-toggle .seg-btn").forEach((b) => {
    const on = b.dataset.theme === theme;
    b.classList.toggle("active", on);
    b.setAttribute("aria-checked", on);
  });
}

let currentTheme = "default";
try {
  currentTheme = localStorage.getItem("luma_theme") || "default";
} catch (_) {}
applyTheme(currentTheme);

document.querySelectorAll("#theme-toggle .seg-btn").forEach((b) =>
  b.addEventListener("click", () => {
    currentTheme = b.dataset.theme;
    try {
      localStorage.setItem("luma_theme", currentTheme);
    } catch (_) {}
    applyTheme(currentTheme);
  })
);

// Voice gender (Female / Male), remembered between visits
let voiceGender = "female";
try {
  voiceGender = localStorage.getItem("tutor_voice_gender") || "female";
} catch (_) {}

function renderGender() {
  document.querySelectorAll("#voice-gender .seg-btn").forEach((b) => {
    const on = b.dataset.gender === voiceGender;
    b.classList.toggle("active", on);
    b.setAttribute("aria-checked", on);
  });
}
document.querySelectorAll("#voice-gender .seg-btn").forEach((b) =>
  b.addEventListener("click", () => {
    voiceGender = b.dataset.gender;
    try {
      localStorage.setItem("tutor_voice_gender", voiceGender);
    } catch (_) {}
    renderGender();
  })
);
renderGender();

// Read the full detailed answer aloud, sentence by sentence, as it streams in — not a
// separate short summary, and not only after the whole answer has finished.
//
// queueSpeechFromText() is called on every streamed chunk with the raw text-so-far. It:
//  1. Holds back anything inside an unclosed code fence / $$ / \[ \] block, so we never
//     read half a formula or half a code sample (that text gets read once the block closes
//     and is stripped out, or is skipped if it stays as a finished block).
//  2. Converts the newly-safe raw text to plain speakable text and splits it into sentences.
//  3. Queues each complete sentence for TTS immediately; the last, possibly-incomplete
//     sentence is held until more text (or the end of the stream) completes it.
let spokenRawLen = 0;
let pendingSpeechText = "";
let speechQueue = [];
let playerRunning = false;
let firstChunkSent = false;
let voiceT0 = 0;
let firstByteLogged = false;

function resetSpeechQueue() {
  speakToken++; // cancel anything from a previous turn
  speechQueue = [];
  spokenRawLen = 0;
  pendingSpeechText = "";
  firstChunkSent = false;
  chunksQueued = 0;
}

// The end of the raw text that's safe to read: cuts off before any code fence / $$ / \[..\]
// block that hasn't closed yet, so we never speak a partial formula or code sample.
function safeSpeakablePrefix(raw) {
  let cut = raw.length;
  for (const marker of ["```", "$$"]) {
    let idx = 0, count = 0, lastOpen = -1;
    while ((idx = raw.indexOf(marker, idx)) !== -1) {
      count++;
      if (count % 2 === 1) lastOpen = idx;
      idx += marker.length;
    }
    if (count % 2 === 1) cut = Math.min(cut, lastOpen);
  }
  const opens = [];
  let i = 0;
  while ((i = raw.indexOf("\\[", i)) !== -1) {
    opens.push(i);
    i += 2;
  }
  const closes = (raw.match(/\\\]/g) || []).length;
  if (opens.length > closes) cut = Math.min(cut, opens[closes]);

  // Also hold back an in-progress table row / horizontal rule line until its closing
  // newline arrives — otherwise a streaming chunk boundary can split it (e.g. only the
  // opening "|" of a table row arrives so far) and the raw "|"/"---" leaks into speech.
  const lastNL = raw.lastIndexOf("\n", cut - 1);
  const lineStart = lastNL + 1;
  if (/^\s*(\||-{3,}|\*{3,}|_{3,})/.test(raw.slice(lineStart, cut))) cut = Math.min(cut, lineStart);

  return raw.slice(0, cut);
}

// Turn markdown/LaTeX into plain words for the voice to read.
function toSpeechText(text) {
  return text
    .replace(/```[\s\S]*?```/g, " Here's a code example on screen. ")
    .replace(/\$\$[\s\S]*?\$\$/g, " ")
    .replace(/\\\[[\s\S]*?\\\]/g, " ")
    .replace(/\|.*\|/g, " ")
    .replace(/^\s*[-*_]{3,}\s*$/gm, "")
    .replace(/^#+\s*/gm, "")
    .replace(/[*_`#$\\]/g, "")
    .replace(/\n{2,}/g, ". ");
}

function queueSpeechFromText(rawSoFar) {
  const safe = safeSpeakablePrefix(rawSoFar);
  if (safe.length <= spokenRawLen) return;
  pendingSpeechText += toSpeechText(safe.slice(spokenRawLen));
  spokenRawLen = safe.length;
  drainCompleteSentences(false);
}

function flushSpeechQueue(finalShownText) {
  // Pick up anything after the last safe cut point (e.g. a trailing formula/code block
  // that never got a chance to "close" mid-stream) plus whatever sentence was pending.
  if (finalShownText.length > spokenRawLen) {
    pendingSpeechText += toSpeechText(finalShownText.slice(spokenRawLen));
    spokenRawLen = finalShownText.length;
  }
  drainCompleteSentences(true);
}

// Merge into chunks of at least MIN_CHUNK characters before speaking — otherwise things
// like numbered-list markers ("1.") and abbreviations ("e.g.") each end a "sentence" and
// would fire off a separate one-word TTS request, sounding choppy and hammering the free
// voice service. The very first chunk is exempt so speech still starts immediately.
// The Gemini voice makes a phrase in about as long as it takes to say, and all chunks are requested at once. So the first chunk is
// short (starts quickly) and each next one is longer: it is ready just before the previous one finishes playing, with few joins.
const CHUNK_STEPS = [45, 90, 150, 230, 320];
let chunksQueued = 0;
const chunkNeed = (i) => CHUNK_STEPS[Math.min(i, CHUNK_STEPS.length - 1)];
// Don't wait for the model's first full sentence before speaking anything — a long first
// clause with no punctuation yet would mean several seconds of dead air. Once this many
// characters have piled up, cut at the last word boundary and speak that fragment right
// away; this is the only cut that ever happens mid-sentence.
// Too small (one word) and it finishes playing before the next chunk's audio is ready,
// leaving an audible gap; ~45 chars plays for ~3s, enough to cover the next TTS request.
const FIRST_CHUNK_MIN = 45;

function drainCompleteSentences(isFinal) {
  if (!firstChunkSent && pendingSpeechText.trim().length >= FIRST_CHUNK_MIN) {
    let cut = pendingSpeechText.length;
    if (!isFinal) {
      const lastSpace = pendingSpeechText.lastIndexOf(" ");
      if (lastSpace > 0) cut = lastSpace;
    }
    const head = pendingSpeechText.slice(0, cut).trim();
    if (head) {
      enqueueSpeech(head);
      firstChunkSent = true;
      pendingSpeechText = pendingSpeechText.slice(cut);
    }
  }

  const parts = pendingSpeechText.match(/[^.!?؟۔\n]+[.!?؟۔]?\s*/g) || [];
  if (!parts.length) {
    if (isFinal) pendingSpeechText = "";
    return;
  }
  const lastEndsClean = /[.!?؟۔]\s*$/.test(parts[parts.length - 1]);
  const usable = isFinal || lastEndsClean ? parts : parts.slice(0, -1);
  const leftoverPart = isFinal || lastEndsClean ? "" : parts[parts.length - 1];

  const ready = [];
  let cur = "";
  for (const part of usable) {
    cur += part;
    if (cur.trim().length >= chunkNeed(chunksQueued + ready.length)) {
      ready.push(cur);
      firstChunkSent = true;
      cur = "";
    }
  }
  if (isFinal && cur.trim()) {
    ready.push(cur);
    cur = "";
  }
  pendingSpeechText = cur + leftoverPart;

  for (const s of ready) {
    const trimmed = s.trim();
    if (trimmed) enqueueSpeech(trimmed);
  }
}

const NO_NOTES_MARK = "[[LUMA_NO_NOTES]]";

// Streams the spoken lesson from /api/lesson into the speech queue as it is written. Returns the full spoken text.
async function runLesson(body) {
  let text = "";
  try {
    const res = await fetch("/api/lesson", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    if (!res.ok) throw new Error(res.status);
    const reader = res.body.getReader();
    const decoder = new TextDecoder();
    while (true) {
      const { done, value } = await reader.read();
      if (done) break;
      text += decoder.decode(value, { stream: true });
      queueSpeechFromText(text);
    }
    flushSpeechQueue(text);
  } catch (err) {
    releaseText();
    setStatus("Voice is unavailable right now (" + err.message + "). The written answer is above.");
  }
  return text.replace(TRUNC_MARK, "").trim();
}

async function fetchSpeech(text, label) {
  const t = Math.round(performance.now() - voiceT0);
  console.log(`[voice-timing] ${t}ms: /api/tts request sent (${label}, ${text.length} chars)`);
  const res = await fetch("/api/tts", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ text, gender: voiceGender, language: languageEl.value, voice: liveVoiceEl.value || undefined }),
  });
  console.log(`[voice-timing] ${Math.round(performance.now() - voiceT0)}ms: /api/tts response received (${label})`);
  if (!res.ok) throw new Error(res.status);
  return res.blob();
}

function playBlob(blob, label) {
  return new Promise((resolve) => {
    const audio = new Audio(URL.createObjectURL(blob));
    currentAudio = audio;
    audio.onplaying = () => releaseText();
    audio.addEventListener("playing", () => console.log(`[voice-timing] ${Math.round(performance.now() - voiceT0)}ms: audio actually playing (${label})`));
    audio.onended = audio.onpause = audio.onerror = () => resolve();
    audio.play().catch(() => resolve());
  });
}

let speechSeq = 0;

function enqueueSpeech(text) {
  const token = speakToken;
  const label = `#${++speechSeq}`;
  chunksQueued++;
  const job = { token, label, promise: fetchSpeech(text, label) };
  job.promise.catch(() => {});
  speechQueue.push(job);
  setSpeaking(true);
  runPlayer();
}

async function runPlayer() {
  if (playerRunning) return;
  playerRunning = true;
  while (speechQueue.length) {
    const job = speechQueue.shift();
    if (job.token !== speakToken) continue; // cancelled/superseded
    try {
      const blob = await job.promise;
      if (job.token !== speakToken) continue;
      await playBlob(blob, job.label);
    } catch (err) {
      if (job.token === speakToken) {
        releaseText();
        setStatus("Voice is unavailable right now (" + err.message + "). The written answer is above.");
      }
    }
  }
  playerRunning = false;
  releaseText();
  setSpeaking(false);
}

// ---------- Voice input (listening animation) ----------

let recorder = null;
let audioCtx = null;
let rafId = null;
let timerId = null;
const bars = Array.from(document.querySelectorAll("#bars i"));

function startMeter(stream) {
  audioCtx = new (window.AudioContext || window.webkitAudioContext)();
  const analyser = audioCtx.createAnalyser();
  analyser.fftSize = 64;
  audioCtx.createMediaStreamSource(stream).connect(analyser);
  const data = new Uint8Array(analyser.frequencyBinCount);
  const tick = () => {
    analyser.getByteFrequencyData(data);
    bars.forEach((bar, i) => {
      const v = data[Math.min(i + 1, data.length - 1)] / 255;
      bar.style.height = Math.max(4, Math.round(v * 32)) + "px";
    });
    rafId = requestAnimationFrame(tick);
  };
  tick();
}

function stopMeter() {
  cancelAnimationFrame(rafId);
  clearInterval(timerId);
  if (audioCtx) audioCtx.close().catch(() => {});
  audioCtx = null;
  bars.forEach((b) => (b.style.height = ""));
}

function startTimer() {
  const label = $("rec-time");
  const start = Date.now();
  label.textContent = "0:00";
  timerId = setInterval(() => {
    const s = Math.floor((Date.now() - start) / 1000);
    label.textContent = `${Math.floor(s / 60)}:${String(s % 60).padStart(2, "0")}`;
  }, 250);
}

let autoSend = false;
let sttSocket = null; // live transcription socket for the recording in progress, if any
let sttChunks = []; // every recorded chunk, kept in case the socket drops and we fall back

function finishRecording(send) {
  autoSend = send;
  if (recorder && recorder.state !== "inactive") recorder.stop();
}

// Enter sends while recording (the textarea is hidden, so listen globally)
document.addEventListener("keydown", (e) => {
  if (recorder && e.key === "Enter" && !e.shiftKey) {
    e.preventDefault();
    finishRecording(true);
  }
});

function wsUrl(path) {
  return (location.protocol === "https:" ? "wss://" : "ws://") + location.host + path;
}

function useTranscript(text, spokenLang) {
  if (!text || !text.trim()) {
    setStatus("I couldn't hear anything. Please try again a little closer to the microphone.");
    return;
  }
  // (the language the student SPOKE does not change the answer language; only the selector or an explicit request does)
  messageEl.value = (messageEl.value ? messageEl.value + " " : "") + text.trim();
  autoGrow();
  updateSend();
  messageEl.focus();
  setStatus("");
  if (autoSend && messageEl.value.trim()) {
    sentByVoice = true;
    formEl.requestSubmit();
  }
}

async function fallbackTranscribe() {
  setStatus("Transcribing…");
  const fd = new FormData();
  const mime = (sttChunks[0] && sttChunks[0].type) || "audio/webm";
  fd.append("file", new Blob(sttChunks, { type: mime }), mime.includes("mp4") ? "audio.m4a" : mime.includes("ogg") ? "audio.ogg" : "audio.webm");
  // The chosen language is a hint; the server double-checks it against auto-detection
  fd.append("language", languageEl.value);
  try {
    const res = await fetch("/api/stt", { method: "POST", body: fd });
    if (!res.ok) throw new Error(res.status);
    const { text, language: spokenLang } = await res.json();
    useTranscript(text, spokenLang);
  } catch (err) {
    setStatus("Transcription failed: " + err.message);
  }
}

micEl.addEventListener("click", async () => {
  if (!liveWS && liveAvailable() && liveAuto) {
    setStatus("Starting the live teacher…");
    if (await ensureLive()) {
      setStatus("");
      await startLiveMic();
      return;
    }
    setStatus("The live teacher is not available right now; using the standard microphone.");
  }
  if (liveWS || liveWanted) {
    liveMic ? stopLiveMic() : startLiveMic();
    return;
  }
  if (recorder) {
    finishRecording(false);
    return;
  }
  stopSpeaking();
  try {
    const stream = await navigator.mediaDevices.getUserMedia({ audio: true });
    sttChunks = [];

    // Live captions: stream each recorded chunk to the server as it's produced, over a
    // WebSocket, so the transcript updates while the user is still talking rather than
    // only after they stop. If the socket never connects, ondataavailable below still
    // buffers every chunk locally, and onstop falls back to the old one-shot upload.
    try {
      sttSocket = new WebSocket(wsUrl("/api/stt/stream"));
      sttSocket.addEventListener("open", () => {
        sttSocket.send(JSON.stringify({ type: "language", language: languageEl.value }));
      });
      sttSocket.addEventListener("message", (ev) => {
        let data;
        try {
          data = JSON.parse(ev.data);
        } catch (_) {
          return;
        }
        if (data.final) {
          if (sttSocket) {
            sttSocket.close();
            sttSocket = null;
          }
          if (data.error) setStatus("Transcription failed: " + data.error);
          else useTranscript(data.text, data.language);
          autoSend = false;
          return;
        }
        if (data.error) return; // a single bad interim pass isn't worth showing; keep listening
        if (recorder) {
          setStatus(data.text ? data.text : "Listening… press Send or Enter to send, or tap the square to stop");
        }
      });
      sttSocket.addEventListener("error", () => {
        sttSocket = null;
      });
      sttSocket.addEventListener("close", () => {
        sttSocket = null;
      });
    } catch (_) {
      sttSocket = null;
    }

    recorder = new MediaRecorder(stream);
    recorder.ondataavailable = (e) => {
      if (!e.data || !e.data.size) return;
      sttChunks.push(e.data);
      if (sttSocket && sttSocket.readyState === WebSocket.OPEN) {
        e.data.arrayBuffer().then((buf) => {
          if (sttSocket && sttSocket.readyState === WebSocket.OPEN) sttSocket.send(buf);
        });
      }
    };
    recorder.onstop = async () => {
      stream.getTracks().forEach((t) => t.stop());
      stopMeter();
      recorder = null;
      updateSend();
      formEl.classList.remove("recording");

      if (sttSocket && sttSocket.readyState === WebSocket.OPEN) {
        setStatus("Transcribing…");
        sttSocket.send(JSON.stringify({ type: "stop" }));
        // useTranscript() fires from the socket's "message" handler once the final
        // transcript arrives (or from its "error"/"close" handling); nothing else to do.
        return;
      }
      if (sttSocket) {
        try { sttSocket.close(); } catch (_) {}
        sttSocket = null;
      }
      await fallbackTranscribe();
      autoSend = false;
    };
    recorder.start(1200); // emit a chunk every ~1.2s so the live caption keeps updating
    formEl.classList.add("recording");
    updateSend();
    setStatus("Listening… press Send or Enter to send, or tap the square to stop");
    startMeter(stream);
    startTimer();
  } catch (err) {
    setStatus("Microphone unavailable: " + err.message);
  }
});

// ---------- Live teacher (Gemini Live, through the server) ----------
// The server relays to Gemini 3.8 Live (the API key never reaches this page). Two channels per answer:
//   VOICE  -> binary frames, 24 kHz PCM16, played here as they arrive (the teacher explains, it does not read the notes)
//   NOTES  -> {"type":"notes"} frames, rendered like any other answer (Markdown + LaTeX) while the voice keeps talking
// Microphone -> 16 kHz PCM16 binary frames. Typed questions go as {"type":"text"}.

const liveToggleEl = $("live-toggle");
const liveBarEl = $("live-bar");
const liveStateEl = $("live-state");
const liveMicEl = $("live-mic");
const liveVoiceEl = $("live-voice");

// Teacher voice picker (Gemini prebuilt voices). Recommended teaching voices first; the choice is remembered.
const LIVE_VOICE_PICKS = ["Sulafat", "Kore", "Aoede", "Leda", "Despina", "Erinome", "Gacrux", "Charon", "Orus", "Achird", "Sadaltager", "Puck", "Zephyr"];
function populateLiveVoices(cfg) {
  const all = cfg.live_voices || [];
  if (!all.length) return;
  const byId = Object.fromEntries(all.map((v) => [v.id, v.style]));
  const order = [...LIVE_VOICE_PICKS.filter((id) => byId[id]), ...all.map((v) => v.id).filter((id) => !LIVE_VOICE_PICKS.includes(id)).sort()];
  liveVoiceEl.innerHTML = "";
  order.forEach((id) => liveVoiceEl.appendChild(new Option(`${id} (${byId[id]})`, id)));
  let saved = "";
  try { saved = localStorage.getItem("luma_live_voice") || ""; } catch (_) {}
  const dflt = (cfg.live_default_voices || {})[voiceGender] || order[0];
  liveVoiceEl.value = byId[saved] ? saved : dflt;
  $("live-voice-field").hidden = false;
}
let previewAudio = null;
$("live-voice-preview").addEventListener("click", async () => {
  const btn = $("live-voice-preview");
  if (previewAudio) { previewAudio.pause(); previewAudio = null; }
  btn.disabled = true;
  const label = btn.textContent;
  btn.textContent = "…";
  try {
    const res = await fetch("/api/live/preview", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ voice: liveVoiceEl.value, language: languageEl.value }),
    });
    if (!res.ok) throw new Error((await res.json().catch(() => ({}))).detail || res.status);
    previewAudio = new Audio(URL.createObjectURL(await res.blob()));
    previewAudio.onended = () => (previewAudio = null);
    await previewAudio.play();
    setStatus("");
  } catch (err) {
    setStatus("Voice preview failed: " + err.message);
  } finally {
    btn.disabled = false;
    btn.textContent = label;
  }
});
liveVoiceEl.addEventListener("change", () => {
  try { localStorage.setItem("luma_live_voice", liveVoiceEl.value); } catch (_) {}
  if (liveWS) setStatus("The new voice is used from your next live lesson.");
});

let liveWS = null;
let liveReady = false;
let liveWanted = false; // the student wants a lesson: if the socket drops (phone locked, network switch) it is re-opened by itself
let liveEverReady = false; // this lesson has worked at least once (so a drop is a "reconnect", not "could not start")
let liveReconnects = 0;
let liveReconnectTimer = null;
let livePingTimer = null;
let liveWatchTimer = null;
let liveLastMsg = 0; // last frame of any kind from the server (proves the socket is alive)
let liveLastActivity = 0; // last sign that the current answer is progressing (voice, notes, captions)
let liveMicWanted = false; // the mic was on: turn it on again after a reconnect
let liveMuteUntil = 0; // phones: the mic is muted while the teacher talks (and a moment after) so the speaker is not heard as a new question
let liveOut = null; // AudioContext used for playback (24 kHz)
let liveNext = 0; // when the next audio chunk should start
let liveSources = [];
let liveDropAudio = false; // "Stop" pressed: ignore the rest of this answer's audio
let liveMic = null; // { stream, ctx, node }
let liveTurn = null; // the assistant bubble of the current answer
let liveUser = null; // the user bubble of a spoken question
// Phones and tablets have the speaker next to the microphone: without echo cancellation of the highest quality the teacher's own voice
// is heard as the student speaking, interrupts the teacher, and the lesson appears to freeze. So on touch devices the mic is half-duplex.
const LIVE_HALF_DUPLEX = (() => {
  try {
    const saved = localStorage.getItem("luma_live_duplex");
    if (saved) return saved === "half";
  } catch (_) {}
  return !!(window.matchMedia && window.matchMedia("(pointer: coarse)").matches);
})();
// Timings (milliseconds); the browser tests shorten them.
const LIVE_T = Object.assign({ ping: 15000, dead: 40000, stuck: 45000, watch: 5000, maxReconnects: 6 }, window.LUMA_LIVE_TIMING || {});
window.__live = { state: "off", audioChunks: 0, audioBytes: 0, scheduledSeconds: 0, notes: 0, captions: 0, turns: 0, errors: [], reconnects: 0, pings: 0, languageSwitches: [], halfDuplex: LIVE_HALF_DUPLEX };

function liveSetState(text, state) {
  liveStateEl.textContent = text;
  window.__live.state = state || text;
}

function liveEnsureOut() {
  if (!liveOut) {
    const AC = window.AudioContext || window.webkitAudioContext;
    try {
      liveOut = new AC({ sampleRate: 24000 });
    } catch (_) {
      liveOut = new AC(); // some phones refuse a custom rate; buffers are resampled when played
    }
    liveOut.onstatechange = () => {
      if (liveOut && liveOut.state !== "running" && liveWanted && !document.hidden) liveOut.resume().catch(() => {});
    };
  }
  if (liveOut.state !== "running") liveOut.resume().catch(() => {}); // "suspended" and, on iOS, "interrupted" (after a call or app switch)
  return liveOut;
}

function livePlay(buf) {
  window.__live.audioChunks++;
  window.__live.audioBytes += buf.byteLength;
  liveLastActivity = Date.now();
  if (liveDropAudio) return;
  const ctx = liveEnsureOut();
  const even = buf.byteLength & ~1;
  const i16 = new Int16Array(buf.slice(0, even));
  if (!i16.length) return;
  const f32 = new Float32Array(i16.length);
  for (let i = 0; i < i16.length; i++) f32[i] = i16[i] / 32768;
  const ab = ctx.createBuffer(1, f32.length, 24000);
  ab.copyToChannel(f32, 0);
  const src = ctx.createBufferSource();
  src.buffer = ab;
  src.connect(ctx.destination);
  const lead = liveNext <= ctx.currentTime ? 0.15 : 0; // after silence, pre-roll a little so the first words are not clipped
  const start = Math.max(ctx.currentTime + 0.03 + lead, liveNext);
  src.start(start);
  liveNext = start + ab.duration;
  window.__live.scheduledSeconds += ab.duration;
  liveSources.push(src);
  liveMuteUntil = Date.now() + (liveNext - ctx.currentTime) * 1000 + 600; // the mic stays muted until the speaker is quiet (phones)
  setSpeaking(true);
  src.onended = () => {
    liveSources = liveSources.filter((s) => s !== src);
    if (!liveSources.length) {
      setSpeaking(false);
      liveMuteUntil = Date.now() + 500;
    }
  };
}

function liveFlush(dropRest) {
  liveSources.forEach((s) => {
    try { s.stop(); } catch (_) {}
  });
  liveSources = [];
  liveNext = 0;
  liveMuteUntil = 0;
  if (dropRest) liveDropAudio = true;
  if (liveWS) setSpeaking(false);
}

function liveEnsureTurn() {
  if (liveTurn) return liveTurn;
  const body = addMessage("assistant", "", languageEl.value);
  body.dir = RTL_LANGUAGES.has(languageEl.value) ? "rtl" : "";
  body.innerHTML =
    '<div class="live-notes"></div>' +
    '<details class="live-says" hidden><summary>What the teacher said (voice transcript)</summary><div class="live-says-text"></div></details>';
  liveTurn = { body, notesEl: body.querySelector(".live-notes"), saysEl: body.querySelector(".live-says"), saysText: body.querySelector(".live-says-text"), notesMd: "", said: "" };
  window.__live.turns++;
  return liveTurn;
}

let liveRenderPending = false;
function liveScheduleRender() {
  const t = liveTurn;
  if (!t || liveRenderPending) return;
  liveRenderPending = true;
  requestAnimationFrame(() => {
    liveRenderPending = false;
    renderInto(t.notesEl, t.notesMd);
    scrollToBottom();
  });
}
function liveRenderNotes() {
  renderInto(liveTurn.notesEl, liveTurn.notesMd);
  scrollToBottom();
}

const LANGUAGE_LABELS = { en: "English", ur: "Urdu", ar: "Arabic", fa: "Persian", de: "German" };
function liveFollowLanguage(lang, announce) {
  if (!lang || lang === languageEl.value) return;
  languageEl.value = lang;
  applyLanguageDirection();
  window.__live.languageSwitches.push(lang);
  if (announce) setStatus(`Switched to ${LANGUAGE_LABELS[lang] || lang} as you asked.`);
}

function liveHandle(msg) {
  liveLastMsg = Date.now();
  switch (msg.type) {
    case "ready":
      liveReady = true;
      liveEverReady = true;
      liveReconnects = 0;
      liveLastActivity = Date.now();
      liveMicEl.disabled = false;
      liveSetState(msg.resumed ? "Live: continuing your lesson…" : "Live: type a question or turn the mic on", "ready");
      // the selector may have changed while we were connecting or reconnecting
      if (languageEl.value !== msg.language && liveWS && liveWS.readyState === 1) liveWS.send(JSON.stringify({ type: "set_language", language: languageEl.value }));
      if (liveMicWanted && !liveMic) startLiveMic();
      break;
    case "ping":
      window.__live.pings++;
      break;
    case "pong":
      break;
    case "turn_start":
      // every question opens a new turn: nothing of the previous answer can leak into it, and nothing can stay stuck
      liveTurn = liveUser = null;
      liveDropAudio = false;
      liveLastActivity = Date.now();
      if (msg.language && msg.language !== languageEl.value) liveFollowLanguage(msg.language, false);
      break;
    case "language":
      // the student changed the selector or explicitly asked for another language ("explain this in Urdu"): follow it
      liveFollowLanguage(msg.language, msg.reason === "requested");
      if (liveTurn) {
        liveTurn.body.dir = RTL_LANGUAGES.has(msg.language) ? "rtl" : "";
        liveTurn.body.lang = msg.language;
      }
      break;
    case "reconnecting":
      liveReady = false;
      liveSetState("Connection hiccup, reconnecting…", "reconnecting");
      break;
    case "conversation":
      currentConvId = msg.id;
      loadHistory();
      break;
    case "notes_delta": {
      window.__live.notes++;
      liveLastActivity = Date.now();
      const t = liveEnsureTurn();
      t.notesEl.classList.remove("interim"); // the real notes replace the provisional transcript text
      t.notesMd += msg.text;
      liveScheduleRender();
      break;
    }
    case "notes_done":
      liveScheduleRender();
      break;
    case "notes_none":
      // greeting / small talk / out of scope: the teacher just answers by voice, so what the teacher says IS the text on screen
      if (liveTurn) {
        liveTurn.noNotes = true;
        liveShowTranscriptAsText(liveTurn);
      }
      break;
    case "notes_failed":
      // the written notes could not be produced: the voice transcript stays on screen as the answer, and the student is told
      setStatus(msg.message || "");
      if (liveTurn) liveShowTranscriptAsText(liveTurn);
      break;
    case "caption": {
      window.__live.captions++;
      liveLastActivity = Date.now();
      if (msg.who === "user") {
        if (!liveUser) {
          emptyEl.hidden = true;
          liveUser = addMessage("user", "", languageEl.value);
        }
        liveUser.textContent += (liveUser.textContent ? " " : "") + msg.text.trim();
        scrollToBottom();
      } else {
        const t = liveEnsureTurn();
        t.said += msg.text;
        t.saysText.textContent = t.said;
        t.saysEl.hidden = !!t.notesMd ? false : true;
        if (!t.notesMd) {
          // voice and text together: until the written notes arrive, the words being spoken are shown on screen as they are spoken
          t.notesEl.classList.add("interim");
          t.notesEl.textContent = t.said;
          scrollToBottom();
        }
      }
      break;
    }
    case "interrupted":
      liveFlush(false); // the student spoke over the teacher (or the voice is being restarted in the right language)
      break;
    case "voice_restart":
      // the voice started in the wrong language and is being asked again: forget the wrong transcript
      if (liveTurn) {
        liveTurn.said = "";
        liveTurn.saysText.textContent = "";
        if (liveTurn.notesEl.classList.contains("interim")) liveTurn.notesEl.textContent = "";
      }
      break;
    case "turn_complete":
      if (liveTurn && !liveTurn.notesMd && liveTurn.said) liveShowTranscriptAsText(liveTurn); // never leave a spoken answer without text
      liveTurn = null;
      liveUser = null;
      liveDropAudio = false;
      liveSetState("Live: your turn", "your-turn");
      if (currentConvId) loadHistory();
      break;
    case "notice":
      setStatus(msg.message || "");
      break;
    case "limit":
    case "error":
      setStatus(msg.message || "The live teacher stopped.");
      window.__live.errors.push(msg.message || msg.type);
      if (msg.type === "limit" || !liveReady) endLive();
      break;
  }
}

// The teacher's words become the on-screen answer (small talk, or the notes could not be written): rendered like any other answer.
function liveShowTranscriptAsText(t) {
  if (t.notesMd || !t.said) return;
  t.notesEl.classList.remove("interim");
  t.notesMd = t.said;
  t.saysEl.hidden = true;
  liveRenderNotesFor(t);
}
function liveRenderNotesFor(t) {
  renderInto(t.notesEl, t.notesMd);
  scrollToBottom();
}

function liveStopTimers() {
  clearInterval(livePingTimer);
  clearInterval(liveWatchTimer);
  clearTimeout(liveReconnectTimer);
  livePingTimer = liveWatchTimer = liveReconnectTimer = null;
}

async function startLive() {
  if (liveWS) return;
  clearTimeout(liveReconnectTimer);
  stopSpeaking();
  liveWanted = true;
  liveReady = false;
  liveDropAudio = false;
  liveTurn = liveUser = null;
  liveBarEl.hidden = false;
  liveMicEl.disabled = true;
  messageEl.placeholder = "Ask the live teacher…";
  liveSetState(liveEverReady ? "Reconnecting…" : "Connecting…", liveEverReady ? "reconnecting" : "connecting");
  if (!liveEverReady) setStatus("");
  liveEnsureOut(); // created inside the click so the browser allows audio
  const ws = new WebSocket(wsUrl("/api/live"));
  ws.binaryType = "arraybuffer";
  liveWS = ws;
  liveLastMsg = Date.now();
  ws.onopen = () => {
    ws.send(JSON.stringify({
      type: "start", level: levelEl.value, language: languageEl.value, subject: subjectEl.value,
      gender: voiceGender, voice: liveVoiceEl.value || undefined, conversation_id: currentConvId,
    }));
    clearInterval(livePingTimer);
    livePingTimer = setInterval(() => {
      if (liveWS === ws && ws.readyState === 1) ws.send(JSON.stringify({ type: "ping" }));
    }, LIVE_T.ping);
    clearInterval(liveWatchTimer);
    liveWatchTimer = setInterval(liveWatch, LIVE_T.watch);
  };
  ws.onmessage = (ev) => {
    liveLastMsg = Date.now();
    if (typeof ev.data === "string") {
      try { liveHandle(JSON.parse(ev.data)); } catch (_) {}
    } else {
      livePlay(ev.data);
    }
  };
  ws.onerror = () => window.__live.errors.push("socket error");
  ws.onclose = () => liveSocketClosed(ws);
}

// The socket is gone. Before the lesson ever worked this is "could not start"; afterwards it is a dropped connection (phone locked,
// network switched, server restarted) and the lesson is re-opened by itself with the same conversation, so the student never has to refresh.
function liveSocketClosed(ws) {
  if (liveWS !== ws) return;
  liveWS = null;
  liveReady = false;
  liveStopTimersKeepWanted();
  stopLiveMic(true);
  liveFlush(false);
  liveTurn = liveUser = null;
  if (liveWanted && liveEverReady && liveReconnects < LIVE_T.maxReconnects) {
    const delay = Math.min(400 * 2 ** liveReconnects, 5000);
    liveReconnects++;
    window.__live.reconnects++;
    liveSetState("Connection lost, reconnecting…", "reconnecting");
    liveReconnectTimer = setTimeout(() => {
      if (liveWanted && !liveWS) startLive();
    }, delay);
    return;
  }
  if (!liveEverReady) setStatus("Could not start the live teacher. Please try again, or sign in again.");
  else if (liveWanted) setStatus("The live teacher connection was lost. Tap the Live button to start again.");
  endLive();
}

function liveStopTimersKeepWanted() {
  clearInterval(livePingTimer);
  clearInterval(liveWatchTimer);
  livePingTimer = liveWatchTimer = null;
}

// Every few seconds: is the socket still alive, and is the current answer still progressing?
function liveWatch() {
  const now = Date.now();
  if (liveWS && liveWS.readyState === 1 && now - liveLastMsg > LIVE_T.dead) {
    // nothing at all (not even the server's keep-alive ping): the socket is half-open, which phones do silently. Drop it and reconnect.
    const dead = liveWS;
    dead.onclose = null;
    try { dead.close(); } catch (_) {}
    liveSocketClosed(dead);
    return;
  }
  const st = window.__live.state;
  if ((st === "answering" || st === "listening") && now - liveLastActivity > LIVE_T.stuck && liveSources.length === 0) {
    // the answer stopped progressing and the server did not say why: free the UI so the next question can be asked
    liveTurn = liveUser = null;
    liveDropAudio = false;
    liveSetState("Live: your turn", "your-turn");
    setStatus("The teacher did not answer. Please ask again.");
  }
}

document.addEventListener("visibilitychange", () => {
  if (document.hidden) return;
  if (liveOut) liveEnsureOut();
  if (!liveWanted) return;
  if (!liveWS || liveWS.readyState !== 1) {
    clearTimeout(liveReconnectTimer);
    if (!liveWS) startLive();
  } else {
    liveWS.send(JSON.stringify({ type: "ping" })); // prove the socket still works after the phone was asleep
    liveLastMsg = Math.min(liveLastMsg, Date.now() - LIVE_T.dead + 8000); // ...and give it 8 s to answer
  }
});
window.addEventListener("pageshow", () => document.dispatchEvent(new Event("visibilitychange")));
window.addEventListener("online", () => document.dispatchEvent(new Event("visibilitychange")));

function endLive() {
  const ws = liveWS;
  liveWS = null;
  liveWanted = false;
  liveEverReady = false;
  liveReady = false;
  liveReconnects = 0;
  liveMicWanted = false;
  liveStopTimers();
  stopLiveMic(true);
  if (ws) {
    ws.onclose = null;
    try { if (ws.readyState === 1) ws.send(JSON.stringify({ type: "stop" })); ws.close(); } catch (_) {}
  }
  liveFlush(false);
  liveTurn = liveUser = null;
  liveBarEl.hidden = true;
  messageEl.placeholder = "Ask an academic question…";
  window.__live.state = "off";
  if (currentConvId) loadHistory();
}

function liveSendText(text) {
  liveFlush(false); // typing while the teacher talks interrupts it
  liveDropAudio = false;
  liveTurn = liveUser = null;
  // The answer language is the one selected in the interface. It is NOT guessed from the question: only the selector or an explicit
  // request ("explain this in Urdu") changes it, and the server tells us when it does.
  addMessage("user", text, languageEl.value);
  liveLastActivity = Date.now();
  liveWS.send(JSON.stringify({ type: "text", text }));
  liveSetState("Live: the teacher is answering…", "answering");
}

function liveSyncLanguage() {
  if (liveWS && liveReady && liveWS.readyState === 1) liveWS.send(JSON.stringify({ type: "set_language", language: languageEl.value }));
}

// 16 kHz mono PCM16 from the microphone (the Live API's input format), 100 ms per frame.
const PCM_WORKLET = `
class Pcm16k extends AudioWorkletProcessor {
  constructor() { super(); this.acc = []; this.pos = 0; this.out = []; this.ratio = sampleRate / 16000; }
  process(inputs) {
    const ch = inputs[0] && inputs[0][0];
    if (!ch) return true;
    for (let i = 0; i < ch.length; i++) this.acc.push(ch[i]);
    while (this.pos + 1 < this.acc.length) {
      const i = Math.floor(this.pos), f = this.pos - i;
      this.out.push(this.acc[i] * (1 - f) + this.acc[i + 1] * f);
      this.pos += this.ratio;
    }
    const drop = Math.floor(this.pos);
    this.acc = this.acc.slice(drop);
    this.pos -= drop;
    while (this.out.length >= 1600) {
      const chunk = this.out.splice(0, 1600), i16 = new Int16Array(1600);
      for (let k = 0; k < 1600; k++) { const v = Math.max(-1, Math.min(1, chunk[k])); i16[k] = v < 0 ? v * 32768 : v * 32767; }
      this.port.postMessage(i16.buffer, [i16.buffer]);
    }
    return true;
  }
}
registerProcessor('pcm16k', Pcm16k);`;

async function startLiveMic() {
  if (liveMic || !liveWS) return;
  liveMicWanted = true;
  liveEnsureOut(); // the tap that turned the mic on also unlocks audio playback on phones
  try {
    const stream = await navigator.mediaDevices.getUserMedia({
      audio: { channelCount: 1, echoCancellation: true, noiseSuppression: true, autoGainControl: true },
    });
    if (!liveWS) { stream.getTracks().forEach((t) => t.stop()); return; } // the lesson ended while the permission prompt was open
    const ctx = new (window.AudioContext || window.webkitAudioContext)();
    if (ctx.state !== "running") await ctx.resume().catch(() => {});
    const url = URL.createObjectURL(new Blob([PCM_WORKLET], { type: "application/javascript" }));
    await ctx.audioWorklet.addModule(url);
    URL.revokeObjectURL(url);
    const node = new AudioWorkletNode(ctx, "pcm16k");
    node.port.onmessage = (e) => {
      if (!liveWS || liveWS.readyState !== 1 || !liveReady) return;
      if (LIVE_HALF_DUPLEX && Date.now() < liveMuteUntil) return; // phones: do not send the teacher's own voice back as a "question"
      liveWS.send(e.data);
    };
    const mute = ctx.createGain();
    mute.gain.value = 0; // keep the node pulled by the graph without playing the mic back
    ctx.createMediaStreamSource(stream).connect(node);
    node.connect(mute).connect(ctx.destination);
    liveMic = { stream, ctx, node };
    liveMicEl.textContent = "Mic: on";
    liveMicEl.classList.add("on");
    liveSetState("Live: listening to you…", "listening");
    liveLastActivity = Date.now();
  } catch (err) {
    liveMicWanted = false;
    setStatus("Microphone unavailable: " + err.message);
  }
}

function stopLiveMic(silent) {
  if (!silent) liveMicWanted = false;
  if (!liveMic) return;
  const m = liveMic;
  liveMic = null;
  m.stream.getTracks().forEach((t) => t.stop());
  m.ctx.close().catch(() => {});
  liveMicEl.textContent = "Mic: off";
  liveMicEl.classList.remove("on");
  if (!silent && liveWS && liveWS.readyState === 1) liveWS.send(JSON.stringify({ type: "mic_stop" }));
}

function liveAvailable() {
  return !!(appConfig.live_enabled && window.WebSocket && (window.AudioContext || window.webkitAudioContext));
}
// Live mode is ALWAYS on: every question and the microphone go to the live teacher, which opens (and re-opens) by itself. The Live button is
// only an indicator of the connection: filled = a lesson is connected, pulsing = connecting / reconnecting, outlined = idle (the next
// question or a tap connects it). It cannot switch live mode off.
const liveAuto = true;
function renderLiveIndicator() {
  const st = liveReady ? "on" : (liveWS || liveWanted) ? "connecting" : "idle";
  if (liveToggleEl.dataset.state !== st) liveToggleEl.dataset.state = st;
  liveToggleEl.title = {
    on: "Live teacher connected: it writes the notes and explains them aloud",
    connecting: "Connecting to the live teacher…",
    idle: "Live teacher ready: ask a question (or tap) and it connects",
  }[st];
}
function renderLiveMode() {
  const on = liveAvailable();
  renderLiveIndicator();
  // the old read-aloud switch (Edge TTS, sentence by sentence) is only offered when the live teacher is unavailable on this server/browser
  const readAloud = speakEl.closest("label");
  if (readAloud) readAloud.hidden = on;
  if (on) speakEl.checked = false;
}
setInterval(renderLiveIndicator, 400);
// Resolves when the lesson is ready. While a reconnect is in progress (liveWanted) it keeps waiting instead of giving up at once.
function liveWaitReady(ms = 20000) {
  return new Promise((resolve, reject) => {
    const t0 = Date.now();
    const iv = setInterval(() => {
      if (liveReady) { clearInterval(iv); resolve(); }
      else if ((!liveWS && !liveWanted) || Date.now() - t0 > ms) { clearInterval(iv); reject(new Error("live teacher not ready")); }
    }, 100);
  });
}
async function ensureLive() {
  if (liveWS && liveReady) return true;
  if (!liveWS) await startLive();
  try { await liveWaitReady(); return true; } catch (_) { return false; }
}
liveToggleEl.addEventListener("click", async () => {
  // a tap never turns live mode off: it just connects the lesson now (also unlocks audio on phones) if it is not connected yet
  liveEnsureOut();
  if (liveReady) {
    setStatus("Live teacher connected: ask your question, it writes the notes and explains them aloud.");
    return;
  }
  setStatus("Connecting the live teacher…");
  const ok = await ensureLive();
  setStatus(ok ? "Live teacher connected. Ask your question." : "Could not connect the live teacher yet. It will keep trying when you ask.");
});
$("live-end").addEventListener("click", endLive);
liveMicEl.addEventListener("click", () => (liveMic ? stopLiveMic() : startLiveMic()));

// ---------- Accounts & chat history ----------

const loginEl = $("login");
const loginErrorEl = $("login-error");
const historyEl = $("history");
let appConfig = {};

function showLogin(message = "") {
  stopSpeaking();
  appEl.hidden = true;
  loginEl.hidden = false;
  loginErrorEl.textContent = message;
  renderLoginButton();
}

function showApp(user) {
  loginEl.hidden = true;
  appEl.hidden = false;
  const label = user.name || user.email;
  $("user-name").textContent = label;
  $("user-email").textContent = user.email;
  const pic = $("user-pic");
  const initial = $("user-initial");
  if (user.picture) {
    pic.referrerPolicy = "no-referrer";
    pic.src = user.picture;
    pic.hidden = false;
    initial.hidden = true;
  } else {
    pic.hidden = true;
    initial.hidden = false;
    initial.textContent = label[0].toUpperCase();
  }
  startNewChat();
  loadHistory();
}

function renderLoginButton() {
  const domain = appConfig.allowed_email_domain || "tuf.edu.pk";
  $("login-domain").textContent = "@" + domain;
  $("dev-login").hidden = !appConfig.dev_login;
  // email-only sign-in (no Google configured): say so instead of promising a Google button
  const devOnly = appConfig.dev_login && !appConfig.google_client_id;
  const note = document.querySelector(".login-note");
  if (note && devOnly) note.innerHTML = "Enter your university email <b>@" + domain + "</b> to continue.";
  const tag = document.querySelector(".dev-tag");
  if (tag && devOnly) tag.hidden = true;
  const holder = $("g-btn");
  holder.innerHTML = "";
  if (!appConfig.google_client_id) {
    if (!appConfig.dev_login) loginErrorEl.textContent = "Sign-in is not configured yet. Please contact the administrator.";
    return;
  }
  let tries = 0;
  const draw = () => {
    if (!window.google || !google.accounts) {
      if (++tries < 50) return setTimeout(draw, 200);
      loginErrorEl.textContent = "Could not load Google sign-in. Check your internet connection and refresh.";
      return;
    }
    google.accounts.id.initialize({ client_id: appConfig.google_client_id, callback: onGoogleCredential });
    google.accounts.id.renderButton(holder, { theme: "filled_black", size: "large", shape: "pill", text: "signin_with", width: 280 });
  };
  draw();
}

async function onGoogleCredential(resp) {
  loginErrorEl.textContent = "";
  try {
    const res = await fetch("/api/auth/google", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ credential: resp.credential }),
    });
    const data = await res.json().catch(() => ({}));
    if (!res.ok) {
      loginErrorEl.textContent = data.detail || "Sign-in failed.";
      return;
    }
    showApp(data);
  } catch (err) {
    loginErrorEl.textContent = "Network error: " + err.message;
  }
}

$("dev-login").addEventListener("submit", async (e) => {
  e.preventDefault();
  loginErrorEl.textContent = "";
  const res = await fetch("/api/auth/dev", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ email: $("dev-email").value }),
  });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) {
    loginErrorEl.textContent = data.detail || "Sign-in failed.";
    return;
  }
  showApp(data);
});

$("logout").addEventListener("click", async () => {
  await fetch("/api/auth/logout", { method: "POST" }).catch(() => {});
  if (window.google && google.accounts) google.accounts.id.disableAutoSelect();
  historyEl.innerHTML = "";
  showLogin();
});

// ----- Chat list -----

const TRASH_SVG =
  '<svg viewBox="0 0 24 24" width="15" height="15" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M3 6h18M8 6V4h8v2M19 6l-1 14H6L5 6M10 11v6M14 11v6"/></svg>';
const PENCIL_SVG =
  '<svg viewBox="0 0 24 24" width="15" height="15" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M12 20h9M16.5 3.5a2.1 2.1 0 0 1 3 3L7 19l-4 1 1-4Z"/></svg>';

function groupLabel(ts) {
  const startOfDay = (d) => new Date(d.getFullYear(), d.getMonth(), d.getDate());
  const days = Math.round((startOfDay(new Date()) - startOfDay(new Date(ts * 1000))) / 86400000);
  if (days <= 0) return "Today";
  if (days === 1) return "Yesterday";
  if (days < 7) return "Previous 7 days";
  if (days < 30) return "Previous 30 days";
  return "Older";
}

async function loadHistory() {
  let list;
  try {
    const res = await fetch("/api/conversations");
    if (res.status === 401) return showLogin("Your session expired. Please sign in again.");
    if (!res.ok) return;
    list = await res.json();
  } catch (_) {
    return;
  }
  historyEl.innerHTML = "";
  if (!list.length) {
    historyEl.innerHTML = '<div class="history-empty">Your chats will appear here.</div>';
    return;
  }
  let lastGroup = "";
  for (const c of list) {
    const group = groupLabel(c.updated);
    if (group !== lastGroup) {
      const label = document.createElement("div");
      label.className = "history-label";
      label.textContent = group;
      historyEl.appendChild(label);
      lastGroup = group;
    }
    const item = document.createElement("div");
    item.className = "chat-item";
    item.dataset.id = c.id;
    const open = document.createElement("button");
    open.type = "button";
    open.className = "chat-title";
    open.textContent = c.title;
    open.title = c.title;
    open.dir = "auto";
    open.addEventListener("click", () => openConversation(c.id));
    const rename = document.createElement("button");
    rename.type = "button";
    rename.className = "chat-act";
    rename.title = "Rename";
    rename.setAttribute("aria-label", "Rename chat");
    rename.innerHTML = PENCIL_SVG;
    rename.addEventListener("click", () => renameConversation(c.id, c.title));
    const del = document.createElement("button");
    del.type = "button";
    del.className = "chat-act danger";
    del.title = "Delete";
    del.setAttribute("aria-label", "Delete chat");
    del.innerHTML = TRASH_SVG;
    del.addEventListener("click", () => deleteConversation(c.id));
    item.append(open, rename, del);
    historyEl.appendChild(item);
  }
  markActiveChat();
}

function markActiveChat() {
  historyEl.querySelectorAll(".chat-item").forEach((el) => el.classList.toggle("active", el.dataset.id === currentConvId));
}

async function openConversation(id) {
  if (busy || id === currentConvId) {
    closeMenu();
    return;
  }
  const res = await fetch("/api/conversations/" + id);
  if (res.status === 401) return showLogin("Your session expired. Please sign in again.");
  if (!res.ok) {
    setStatus("Could not open that chat.");
    return;
  }
  const conv = await res.json();
  startNewChat();
  currentConvId = conv.id;
  for (const m of conv.messages) {
    const lang = detectLanguage(m.content) || languageEl.value;
    addMessage(m.role, m.content, RTL_LANGUAGES.has(lang) ? lang : "en");
  }
  markActiveChat();
  closeMenu();
  scrollToBottom();
}

async function renameConversation(id, current) {
  const title = prompt("Rename chat", current);
  if (!title || !title.trim() || title.trim() === current) return;
  await fetch("/api/conversations/" + id, {
    method: "PATCH",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ title: title.trim() }),
  });
  loadHistory();
}

async function deleteConversation(id) {
  if (!confirm("Delete this chat? This cannot be undone.")) return;
  await fetch("/api/conversations/" + id, { method: "DELETE" });
  if (id === currentConvId) startNewChat();
  loadHistory();
}

// ----- Start -----

applyLanguageDirection();
updateSend();

(async function boot() {
  try {
    appConfig = await (await fetch("/api/config")).json();
    liveToggleEl.hidden = !(appConfig.live_enabled && window.WebSocket && (window.AudioContext || window.webkitAudioContext));
    if (appConfig.live_enabled) populateLiveVoices(appConfig);
    renderLiveMode();
    const list = $("subjects");
    if (list && Array.isArray(appConfig.subjects) && appConfig.subjects.length) {
      list.innerHTML = "";
      appConfig.subjects.forEach((s) => list.appendChild(new Option(s, s)));
    }
  } catch (_) {}
  try {
    const res = await fetch("/api/auth/me");
    if (res.ok) return showApp(await res.json());
  } catch (_) {}
  showLogin();
})();

