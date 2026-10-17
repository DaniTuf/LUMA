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
languageEl.addEventListener("change", applyLanguageDirection);
voiceLangEl.addEventListener("change", () => {
  languageEl.value = voiceLangEl.value;
  applyLanguageDirection();
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

// Guess the language from the script of the student's message (null = can't tell / Latin).
function detectLanguage(text) {
  // Hindi script (Devanagari) = a Hindustani question; LUMA answers it in Urdu
  const devanagari = (text.match(/[\u0900-\u097F]/g) || []).length;
  if (devanagari && devanagari >= (text.match(/[A-Za-z]/g) || []).length) return "ur";
  const arabicScript = (text.match(/[؀-ۿ]/g) || []).length;
  const latin = (text.match(/[A-Za-z]/g) || []).length;
  if (arabicScript === 0 || arabicScript < latin) return null;
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
  if (liveWS) {
    const t = messageEl.value.trim();
    if (!t || !liveReady) return;
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
  const language = detectLanguage(message) || languageEl.value;
  if (language !== languageEl.value) {
    languageEl.value = language;
    applyLanguageDirection();
  }
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
        voice_mode: voiceMode,
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
      // Speak sentences as they finish streaming in, in parallel with the text
      // rendering on screen — not after the whole answer is done.
      if (voiceMode) queueSpeechFromText(shown);
    }
    const shownText = fullText.replace(TRUNC_MARK, "");
    streamDone = true;
    if (!holdText) renderLatest(); // otherwise rendered when the voice starts
    // Not trimmed: flushSpeechQueue's offsets must line up with the untrimmed text used while streaming.
    if (voiceMode) {
      flushSpeechQueue(shownText);
      if (!speechQueue.length && !playerRunning) releaseText(); // nothing to speak
    }
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
const MIN_CHUNK = 70;
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
    if (cur.trim().length >= (firstChunkSent ? MIN_CHUNK : FIRST_CHUNK_MIN)) {
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

async function fetchSpeech(text, label) {
  const t = Math.round(performance.now() - voiceT0);
  console.log(`[voice-timing] ${t}ms: /api/tts request sent (${label}, ${text.length} chars)`);
  const res = await fetch("/api/tts", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ text, gender: voiceGender, language: languageEl.value }),
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
  if (spokenLang && spokenLang !== languageEl.value) {
    languageEl.value = spokenLang; // the server heard a different language than selected
    applyLanguageDirection();
  }
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
  fd.append("file", new Blob(sttChunks, { type: "audio/webm" }), "audio.webm");
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
  if (liveWS) {
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
let liveOut = null; // AudioContext used for playback (24 kHz)
let liveNext = 0; // when the next audio chunk should start
let liveSources = [];
let liveDropAudio = false; // "Stop" pressed: ignore the rest of this answer's audio
let liveMic = null; // { stream, ctx, node }
let liveTurn = null; // the assistant bubble of the current answer
let liveUser = null; // the user bubble of a spoken question
window.__live = { state: "off", audioChunks: 0, audioBytes: 0, scheduledSeconds: 0, notes: 0, captions: 0, turns: 0, errors: [] };

function liveSetState(text, state) {
  liveStateEl.textContent = text;
  window.__live.state = state || text;
}

function liveEnsureOut() {
  if (!liveOut) liveOut = new (window.AudioContext || window.webkitAudioContext)({ sampleRate: 24000 });
  if (liveOut.state === "suspended") liveOut.resume();
  return liveOut;
}

function livePlay(buf) {
  window.__live.audioChunks++;
  window.__live.audioBytes += buf.byteLength;
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
  setSpeaking(true);
  src.onended = () => {
    liveSources = liveSources.filter((s) => s !== src);
    if (!liveSources.length) setSpeaking(false);
  };
}

function liveFlush(dropRest) {
  liveSources.forEach((s) => {
    try { s.stop(); } catch (_) {}
  });
  liveSources = [];
  liveNext = 0;
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

function liveHandle(msg) {
  switch (msg.type) {
    case "ready":
      liveReady = true;
      liveMicEl.disabled = false;
      liveSetState(msg.resumed ? "Live: continuing your lesson…" : "Live: type a question or turn the mic on", "ready");
      break;
    case "language":
      // the server detected the language of the question (e.g. Urdu while the selector said English): follow it
      if (msg.language && msg.language !== languageEl.value) {
        languageEl.value = msg.language;
        applyLanguageDirection();
      }
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
      const t = liveEnsureTurn();
      t.notesMd += msg.text;
      liveScheduleRender();
      break;
    }
    case "notes_done":
      liveScheduleRender();
      break;
    case "notes_none":
      if (liveTurn) liveTurn.noNotes = true; // greeting / small talk / out of scope: the teacher just answers by voice
      break;
    case "caption": {
      window.__live.captions++;
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
        t.saysEl.hidden = false;
      }
      break;
    }
    case "interrupted":
      liveFlush(false); // the student spoke over the teacher
      break;
    case "turn_complete":
      if (liveTurn && !liveTurn.notesMd && !liveTurn.noNotes && liveTurn.said.length > 400) {
        liveTurn.notesMd = liveTurn.said; // the model skipped the notes: don't leave the answer empty
        liveRenderNotes();
      }
      liveTurn = null;
      liveUser = null;
      liveDropAudio = false;
      liveSetState("Live: your turn", "your-turn");
      if (currentConvId) loadHistory();
      break;
    case "limit":
    case "error":
      setStatus(msg.message || "The live teacher stopped.");
      window.__live.errors.push(msg.message || msg.type);
      if (msg.type === "limit" || !liveReady) endLive();
      break;
  }
}

async function startLive() {
  if (liveWS) return;
  stopSpeaking();
  liveReady = false;
  liveDropAudio = false;
  liveTurn = liveUser = null;
  liveBarEl.hidden = false;
  liveMicEl.disabled = true;
  messageEl.placeholder = "Ask the live teacher…";
  liveSetState("Connecting…", "connecting");
  setStatus("");
  liveEnsureOut(); // created inside the click so the browser allows audio
  const ws = new WebSocket(wsUrl("/api/live"));
  ws.binaryType = "arraybuffer";
  liveWS = ws;
  ws.onopen = () =>
    ws.send(JSON.stringify({
      type: "start", level: levelEl.value, language: languageEl.value, subject: subjectEl.value,
      gender: voiceGender, voice: liveVoiceEl.value || undefined, conversation_id: currentConvId,
    }));
  ws.onmessage = (ev) => {
    if (typeof ev.data === "string") {
      try { liveHandle(JSON.parse(ev.data)); } catch (_) {}
    } else {
      livePlay(ev.data);
    }
  };
  ws.onerror = () => window.__live.errors.push("socket error");
  ws.onclose = () => {
    if (liveWS === ws) {
      if (!liveReady) setStatus("Could not start the live teacher. Please try again, or sign in again.");
      endLive();
    }
  };
}

function endLive() {
  const ws = liveWS;
  liveWS = null;
  liveReady = false;
  stopLiveMic();
  if (ws) {
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
  const asked = detectLanguage(text); // Urdu/Arabic/Persian script: switch the selector to the language of the question
  if (asked && asked !== languageEl.value) {
    languageEl.value = asked;
    applyLanguageDirection();
  }
  addMessage("user", text, languageEl.value);
  liveWS.send(JSON.stringify({ type: "text", text }));
  liveSetState("Live: the teacher is answering…", "answering");
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
  try {
    const stream = await navigator.mediaDevices.getUserMedia({
      audio: { channelCount: 1, echoCancellation: true, noiseSuppression: true, autoGainControl: true },
    });
    const ctx = new (window.AudioContext || window.webkitAudioContext)();
    const url = URL.createObjectURL(new Blob([PCM_WORKLET], { type: "application/javascript" }));
    await ctx.audioWorklet.addModule(url);
    const node = new AudioWorkletNode(ctx, "pcm16k");
    node.port.onmessage = (e) => {
      if (liveWS && liveWS.readyState === 1) liveWS.send(e.data);
    };
    const mute = ctx.createGain();
    mute.gain.value = 0; // keep the node pulled by the graph without playing the mic back
    ctx.createMediaStreamSource(stream).connect(node);
    node.connect(mute).connect(ctx.destination);
    liveMic = { stream, ctx, node };
    liveMicEl.textContent = "Mic: on";
    liveMicEl.classList.add("on");
    liveSetState("Live: listening to you…", "listening");
  } catch (err) {
    setStatus("Microphone unavailable: " + err.message);
  }
}

function stopLiveMic() {
  if (!liveMic) return;
  const m = liveMic;
  liveMic = null;
  m.stream.getTracks().forEach((t) => t.stop());
  m.ctx.close().catch(() => {});
  liveMicEl.textContent = "Mic: off";
  liveMicEl.classList.remove("on");
  if (liveWS && liveWS.readyState === 1) liveWS.send(JSON.stringify({ type: "mic_stop" }));
}

function liveAvailable() {
  return !!(appConfig.live_enabled && window.WebSocket && (window.AudioContext || window.webkitAudioContext));
}
let liveAuto = true; // default: every question and the microphone go to the live teacher
try { liveAuto = localStorage.getItem("luma_live_auto") !== "0"; } catch (_) {}
function renderLiveMode() {
  const on = liveAvailable() && liveAuto;
  liveToggleEl.classList.toggle("on", on);
  liveToggleEl.setAttribute("aria-pressed", on ? "true" : "false");
  // the old read-aloud switch (Edge TTS, sentence by sentence) is only offered when the live teacher is off or unavailable
  const readAloud = speakEl.closest("label");
  if (readAloud) readAloud.hidden = on;
  if (on) speakEl.checked = false;
}
function liveWaitReady(ms = 20000) {
  return new Promise((resolve, reject) => {
    const t0 = Date.now();
    const iv = setInterval(() => {
      if (liveReady) { clearInterval(iv); resolve(); }
      else if (!liveWS || Date.now() - t0 > ms) { clearInterval(iv); reject(new Error("live teacher not ready")); }
    }, 100);
  });
}
async function ensureLive() {
  if (liveWS && liveReady) return true;
  if (!liveWS) await startLive();
  try { await liveWaitReady(); return true; } catch (_) { return false; }
}
liveToggleEl.addEventListener("click", () => {
  liveAuto = !liveAuto;
  try { localStorage.setItem("luma_live_auto", liveAuto ? "1" : "0"); } catch (_) {}
  if (!liveAuto && liveWS) endLive();
  renderLiveMode();
  setStatus(liveAuto ? "Live teacher on: your questions are explained by voice with notes on screen." : "Live teacher off: normal text chat.");
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
