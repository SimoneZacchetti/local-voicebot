// Voicebot browser client: mic + VAD (Silero via vad-web), WebSocket transport,
// gapless playback with an AudioWorklet, barge-in, the voice orb, and typed chat
// (the same conversation: spoken replies while voice is on, text-only otherwise).
import { VoiceOrb } from "./viz.js";

const $ = (id) => document.getElementById(id);

const VENDOR = new URL("vendor/", location.href).href;
// iOS (iPadOS reports itself as a Mac): stricter audio rules, no speaker-mode trick.
const IOS = /iP(hone|ad|od)/.test(navigator.userAgent) || (/Mac/.test(navigator.userAgent) && navigator.maxTouchPoints > 1);
// The phone's loudspeaker sits next to its mic and some of the bot's voice leaks past echo
// cancellation: on iOS, interrupting the bot needs clearer and longer speech.
const BARGE_PROB = IOS ? 0.85 : 0.7; // speech probability needed to interrupt the bot
const BARGE_MS = IOS ? 500 : 300; // ...sustained for this long
const FRAME_MS = 32; // Silero v5 frame: 512 samples @ 16 kHz
const BARGE_FRAMES = Math.ceil(BARGE_MS / FRAME_MS);
const FLAG_PLAYING = 1;
// Remote clients (the phone over Tailscale) get a small jitter buffer before each burst of
// bot audio: Wi-Fi power saving and cellular add uneven delays that otherwise stutter.
const REMOTE = !["127.0.0.1", "localhost", "[::1]"].includes(location.hostname);
const PREBUFFER_MS = REMOTE ? 200 : 0;

let ws = null;
let readyMsg = null; // "ready" of the open WebSocket (reused when voice starts after chatting)
let vad = null;
let ctx = null;
let player = null;
let mediaDest = null;
let redemptionMs = 550;
let ttsRate = 24000;

let currentTurn = -1; // server turn whose audio we accept
let heard = { turn: -1, seq: -1 }; // last sentence that started playing
let workletActive = false;
let bargePaused = false;
let bargeFrames = 0;
let activeAtSpeechStart = false;
let uttId = 0;
let lastUttSent = 0;
const speechEndAt = new Map(); // utt_id -> performance.now()
const turnUtt = new Map(); // turn -> utt_id
const turns = new Map(); // turn -> {bot: element, text, startedSeq, sentences, interrupted}
let fallbackSpeaking = false;
let fallbackQueue = []; // tts_fallback sentences waiting for the worklet to drain
let replyStreaming = false; // current reply still has audio coming (bridges gaps between sentences)
let running = false;
let starting = false;
let muted = false;
let analyserOut = null; // bot voice
let analyserIn = null; // user mic
let micSource = null;
let micStream = null;
let wakeLock = null;
let reconnectTimer = null;
let reconnectTries = 0;
let hadConnection = false;

// Stable per-device id: after a screen lock or network change the server resumes this
// client's conversation (server/session.py SavedHistory).
const CLIENT_ID = (() => {
  try {
    let id = localStorage.getItem("voicebot-client");
    if (!id) localStorage.setItem("voicebot-client", (id = crypto.randomUUID()));
    return id;
  } catch {
    return crypto.randomUUID(); // private mode: per page load
  }
})();

// ---------------------------------------------------------------- UI

let uiState = "idle";

function setState(state, text) {
  uiState = state;
  document.body.dataset.state = state;
  orb.setState(muted && state === "listening" ? "muted" : state);
  if (text !== undefined) $("status").textContent = text;
}

const wsOpen = () => ws?.readyState === WebSocket.OPEN;

function refreshState() {
  if (!running) return setState("idle", starting ? "Avvio…" : "Premi per iniziare");
  if (!wsOpen() && !starting) return setState("thinking", reconnectTries > 3 ? "Mac non raggiungibile" : "Riconnessione…");
  if (botActive() && !bargePaused) return setState("speaking", "Tocca la sfera o parla per interrompere");
  if (muted) return setState("listening", "Microfono disattivato");
  setState("listening", "Ti ascolto");
}

// ---------------------------------------------------------------- orb + captions

const timeBuf = new Float32Array(512);

function loudness(an) {
  an.getFloatTimeDomainData(timeBuf);
  let sum = 0;
  for (let i = 0; i < timeBuf.length; i++) sum += timeBuf[i] * timeBuf[i];
  return Math.min(1, Math.sqrt(sum / timeBuf.length) * 6);
}

function getLevels() {
  if (uiState === "speaking" && analyserOut) {
    let level = loudness(analyserOut);
    if (level < 0.02 && (fallbackSpeaking || speechSynthesis.speaking)) {
      // Browser speechSynthesis can't be analysed: a slow, plausible swell instead.
      level = 0.3 + 0.12 * Math.sin(performance.now() / 260);
    }
    return { level };
  }
  if ((uiState === "listening" || uiState === "user-speaking") && analyserIn && !muted) return { level: loudness(analyserIn) };
  return { level: 0 };
}

const orb = new VoiceOrb($("viz"), getLevels);
window.voiceOrb = orb; // handy for debugging from the console

function showUserCaption(text) {
  $("cap-user").textContent = text;
  $("cap-bot").textContent = "";
}

function renderCaption() {
  // Karaoke-style: the sentence being spoken, then the next one faded.
  if (!running) return; // text-only chat: renderBot captions the whole reply
  const t = turns.get(currentTurn);
  const el = $("cap-bot");
  if (!t || !t.sentences.length) return;
  const i = heard.turn === currentTurn ? Math.max(heard.seq, 0) : 0;
  const now = t.sentences[i] || "";
  const next = heard.turn === currentTurn && heard.seq >= 0 ? t.sentences[i + 1] || "" : "";
  const spoken = heard.turn === currentTurn && heard.seq >= 0;
  el.innerHTML = spoken
    ? `${esc(now)}${next ? ` <span class="pending next">${esc(next)}</span>` : ""}`
    : `<span class="pending">${esc(now)}</span>`;
}

function describeCall(c) {
  const name = c.name.split("__").pop().replace(/^Hass/, "");
  const a = c.args || {};
  const target = a.name || a.area || a.command || a.message || "";
  return target ? `${name} · ${target}` : name;
}

function answerConfirm(yes) {
  $("confirm").hidden = true;
  ws?.readyState === WebSocket.OPEN && ws.send(JSON.stringify({ type: "confirm_answer", yes }));
}

function toast(text) {
  const el = document.createElement("div");
  el.className = "toast";
  el.textContent = text;
  document.body.appendChild(el);
  setTimeout(() => el.remove(), 4200);
}

function addMsg(cls, html) {
  const el = document.createElement("div");
  el.className = `msg ${cls}`;
  el.innerHTML = html;
  $("log").appendChild(el);
  el.scrollIntoView({ block: "end", behavior: "smooth" });
  return el;
}

function notice(text, popup = false) {
  const el = document.createElement("div");
  el.className = "notice";
  el.textContent = text;
  $("log").appendChild(el);
  if (popup) toast(text);
}

const esc = (s) => s.replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" })[c]);

function turnState(turn) {
  if (!turns.has(turn)) turns.set(turn, { bot: null, text: "", sentences: [], user: null, interrupted: false });
  return turns.get(turn);
}

function renderBot(turn) {
  const t = turnState(turn);
  if (!t.bot) t.bot = addMsg("bot", "");
  t.bot.innerHTML = `${t.lang ? `<span class="badge">${esc(t.lang)}</span>` : ""}${esc(t.text)}`;
  t.bot.classList.toggle("interrupted", t.interrupted);
  t.bot.scrollIntoView({ block: "end", behavior: "smooth" });
  if (turn === currentTurn && (!running || (!t.sentences.length && t.user === null))) {
    // Greeting (no transcript) or text-only chat: caption the whole text.
    $("cap-bot").textContent = t.text;
  }
}

// ---------------------------------------------------------------- playback

function botActive() {
  return workletActive || replyStreaming || fallbackSpeaking || speechSynthesis.speaking || fallbackQueue.length > 0;
}

// Called synchronously in the tap handler, before any await: iOS only lets audio start
// (and keeps the speaker route) when the context is created and resumed inside the gesture.
function unlockAudio() {
  if (navigator.audioSession) navigator.audioSession.type = "play-and-record";
  ctx = new AudioContext({ sampleRate: ttsRate, latencyHint: "interactive" });
  ctx.resume().catch(() => {});
  ctx.onstatechange = onContextState;
  if (IOS) {
    // Unlock speechSynthesis (fallback voice) in the same gesture.
    const u = new SpeechSynthesisUtterance(" ");
    u.volume = 0;
    speechSynthesis.speak(u);
  }
}

// A phone call, Siri or another app took the audio: playback and mic stop until a tap.
function onContextState() {
  if (!running || !ctx || document.hidden) return;
  $("resume").hidden = ctx.state === "running";
}

async function resumeAudio() {
  $("resume").hidden = true;
  try {
    await ctx?.resume();
    if (micStream?.getAudioTracks().some((t) => t.readyState === "ended") && vad) {
      // The system killed the mic: let vad-web reacquire it through getStream.
      await vad.pause();
      await vad.start();
    }
  } catch (e) {
    console.warn(e);
  }
  onContextState();
}

async function setupAudio() {
  if (ctx.sampleRate !== ttsRate) {
    // The server's TTS rate differs from the one guessed in the tap: rebuild (desktop only, no gesture needed).
    await ctx.close().catch(() => {});
    unlockAudio();
  }
  await ctx.resume();
  await ctx.audioWorklet.addModule("player-worklet.js");
  player = new AudioWorkletNode(ctx, "pcm-player", {
    outputChannelCount: [1],
    processorOptions: { prebuffer: Math.round((ctx.sampleRate * PREBUFFER_MS) / 1000) },
  });
  analyserOut = ctx.createAnalyser();
  analyserOut.fftSize = 512;
  analyserOut.smoothingTimeConstant = 0.55;
  player.connect(analyserOut);
  player.port.onmessage = (e) => {
    const m = e.data;
    if (m.type === "started") {
      if (heard.turn !== currentTurn || m.seq > heard.seq) heard = { turn: currentTurn, seq: m.seq };
      onFirstAudio(currentTurn);
      renderCaption();
    } else if (m.type === "active") {
      workletActive = true;
      refreshState();
    } else if (m.type === "idle") {
      workletActive = false;
      drainFallback();
      refreshState();
    }
  };
  routeAudio();
}

function routeAudio() {
  if (!analyserOut) return;
  analyserOut.disconnect();
  const audioEl = $("bot-audio");
  if ($("aec-route").checked) {
    // Playing through a media element helps Chrome treat it as far-end audio for echo cancellation.
    mediaDest ??= ctx.createMediaStreamDestination();
    analyserOut.connect(mediaDest);
    audioEl.srcObject = mediaDest.stream;
    audioEl.play().catch(() => {});
  } else {
    audioEl.srcObject = null;
    analyserOut.connect(ctx.destination);
  }
}

function flushPlayback() {
  player?.port.postMessage({ type: "flush" });
  // Chrome keeps a paused synthesizer paused across cancel(): resume first.
  if (speechSynthesis.paused) speechSynthesis.resume();
  speechSynthesis.cancel();
  fallbackQueue = [];
  fallbackSpeaking = false;
  replyStreaming = false;
  bargePaused = false;
}

function pausePlayback() {
  bargePaused = true;
  player?.port.postMessage({ type: "pause" });
  // iOS cannot reliably resume a paused utterance: drop the fallback sentence instead.
  if (speechSynthesis.speaking) IOS ? speechSynthesis.cancel() : speechSynthesis.pause();
  setState("user-speaking", "Ti ascolto…");
}

function resumePlayback() {
  if (!bargePaused) return;
  bargePaused = false;
  player?.port.postMessage({ type: "resume" });
  if (speechSynthesis.paused) speechSynthesis.resume();
  if (!workletActive) drainFallback();
  refreshState();
}

function queueFallback(turn, seq, lang, text) {
  // Never talk over the neural voice of the same reply: wait until the worklet drains.
  fallbackQueue.push({ turn, seq, lang, text });
  if (!workletActive) drainFallback();
}

function drainFallback() {
  if (bargePaused) return;
  while (fallbackQueue.length) {
    const f = fallbackQueue.shift();
    if (f.turn === currentTurn) speakFallback(f.turn, f.seq, f.lang, f.text);
  }
}

function speakFallback(turn, seq, lang, text) {
  const u = new SpeechSynthesisUtterance(text);
  u.lang = lang;
  const voice = speechSynthesis.getVoices().find((v) => v.lang === lang) || speechSynthesis.getVoices().find((v) => v.lang.startsWith(lang.slice(0, 2)));
  if (voice) u.voice = voice;
  u.onstart = () => {
    if (turn !== currentTurn) return;
    fallbackSpeaking = true;
    if (heard.turn !== turn || seq > heard.seq) heard = { turn, seq };
    onFirstAudio(turn);
    renderCaption();
    refreshState();
  };
  u.onend = u.onerror = () => {
    fallbackSpeaking = speechSynthesis.pending;
    refreshState();
  };
  speechSynthesis.speak(u);
}

function onFirstAudio(turn) {
  const t = turnState(turn);
  if (t.firstAudio) return;
  t.firstAudio = performance.now();
  const utt = turnUtt.get(turn);
  const end = speechEndAt.get(utt);
  if (end) {
    const ms = Math.round(t.firstAudio - end);
    $("latency").textContent = `risposta in ${ms} ms`;
    $("latency").title = `Dalla fine della frase al primo audio, più ${redemptionMs} ms di attesa del silenzio`;
  }
}

// ---------------------------------------------------------------- WebSocket

function connect() {
  return new Promise((resolve, reject) => {
    const sock = new WebSocket(`${location.protocol === "https:" ? "wss" : "ws"}://${location.host}/ws?client=${CLIENT_ID}`);
    let opened = false;
    ws = sock;
    sock.binaryType = "arraybuffer";
    sock.onmessage = (e) => {
      if (typeof e.data === "string") {
        const m = JSON.parse(e.data);
        if (m.type === "ready") {
          opened = true;
          resolve((readyMsg = m));
          if (hadConnection) onReconnected(m);
          hadConnection = true;
        }
        onControl(m);
      } else {
        onAudio(e.data);
      }
    };
    sock.onerror = () => reject(new Error("Mac non raggiungibile (acceso? Tailscale attivo?)"));
    sock.onclose = () => {
      reject(new Error("connessione chiusa dal server")); // no-op once resolved
      if (ws !== sock) return; // closed by teardown
      ws = readyMsg = null;
      if (!running) {
        if (opened) notice("Connessione persa con il server.");
        return; // the chat reconnects on the next message
      }
      // Voice on: screen lock, Wi-Fi <-> cellular, Mac restarted... keep the mic and retry.
      flushPlayback();
      scheduleReconnect();
      refreshState();
    };
  });
}

let connecting = null;

function scheduleReconnect() {
  if (reconnectTimer || document.hidden || !running) return;
  const delay = Math.min(10000, 500 * 2 ** reconnectTries);
  reconnectTries += 1;
  reconnectTimer = setTimeout(async () => {
    reconnectTimer = null;
    try {
      await ensureConnected();
    } catch {
      refreshState();
      scheduleReconnect();
    }
  }, delay);
}

function onReconnected(ready) {
  reconnectTries = 0;
  clearTimeout(reconnectTimer);
  reconnectTimer = null;
  if (running) ws.send(JSON.stringify({ type: "hello", greet: false }));
  if (!ready.resumed && $("log").querySelector(".msg")) notice("Il server è ripartito: conversazione nuova.");
  refreshState();
}

// Keep the phone screen on while talking (iOS 16.4+, reliable in home-screen apps from 18.4).
async function keepAwake(on) {
  try {
    if (on && !wakeLock && "wakeLock" in navigator && !document.hidden) {
      wakeLock = await navigator.wakeLock.request("screen");
      wakeLock.addEventListener("release", () => (wakeLock = null));
    } else if (!on && wakeLock) {
      await wakeLock.release();
    }
  } catch {
    wakeLock = null;
  }
}

function ensureConnected() {
  if (ws?.readyState === WebSocket.OPEN && readyMsg) return Promise.resolve(readyMsg);
  connecting ??= connect().finally(() => (connecting = null));
  return connecting;
}

function onAudio(buf) {
  const dv = new DataView(buf);
  const turn = dv.getUint32(0, true);
  const seq = dv.getUint32(4, true);
  if (turn !== currentTurn || !player) return; // stale audio from an interrupted turn, or voice off
  const i16 = new Int16Array(buf, 8);
  const f32 = new Float32Array(i16.length);
  for (let i = 0; i < i16.length; i++) f32[i] = i16[i] / 32768;
  player.port.postMessage({ type: "push", seq, pcm: f32 }, [f32.buffer]);
}

function onControl(m) {
  switch (m.type) {
    case "ready": {
      ttsRate = m.tts_rate;
      if (m.resumed && !$("log").querySelector(".msg")) notice(`Conversazione ripresa (${m.resumed} messaggi)`);
      redemptionMs = m.vad?.redemption_ms ?? redemptionMs;
      const p = m.providers;
      $("providers").textContent = `STT ${p.stt} · LLM ${p.llm_model} (${p.llm}) · TTS ${p.tts}`;
      break;
    }
    case "turn_start": {
      $("confirm").hidden = true;
      // A new reply starts: anything still playing from the previous turn is obsolete.
      const prev = turns.get(currentTurn);
      if (prev && botActive() && prev.text) {
        prev.interrupted = true;
        renderBot(currentTurn);
      }
      flushPlayback();
      currentTurn = m.turn;
      heard = { turn: m.turn, seq: -1 };
      turnUtt.set(m.turn, m.utt_id);
      turnState(m.turn).lang = m.lang;
      if (m.utt_id) setState("thinking", "Ci penso…");
      break;
    }
    case "transcript": {
      // If the previous turn's reply was never heard, the server merged it into this one.
      const prevTurn = turns.get(m.turn - 1);
      if (m.merged && prevTurn && prevTurn.user) {
        prevTurn.user.remove();
        prevTurn.bot?.remove();
      }
      turnState(m.turn).user = addMsg("user", `<span class="badge">${esc(m.lang)}</span>${esc(m.text)}`);
      showUserCaption(m.text);
      break;
    }
    case "token": {
      if (m.turn !== currentTurn) break;
      turnState(m.turn).text += m.text;
      renderBot(m.turn);
      break;
    }
    case "sentence":
      if (m.turn === currentTurn) replyStreaming = true;
      turnState(m.turn).sentences[m.seq] = m.text;
      if (m.turn === currentTurn) renderCaption();
      if (!turnState(m.turn).text) {
        // Greeting: no tokens are streamed, show the sentence itself.
        turnState(m.turn).text = m.text;
        renderBot(m.turn);
      }
      break;
    case "tts_fallback":
      if (m.turn !== currentTurn) break;
      replyStreaming = true;
      if (!turnState(m.turn).text) {
        turnState(m.turn).text = m.text;
        renderBot(m.turn);
      }
      turnState(m.turn).sentences[m.seq] = m.text;
      queueFallback(m.turn, m.seq, m.lang, m.text);
      break;
    case "skip":
      console.debug("skip", m);
      if (m.utt_id === lastUttSent) {
        resumePlayback();
        refreshState();
      }
      break;
    case "turn_end": {
      if (m.turn === currentTurn) replyStreaming = false;
      const x = m.metrics || {};
      if (x.stt_ms !== undefined) {
        const parts = [`STT ${x.stt_ms}`, `LLM TTFT ${x.llm_ttft_ms ?? "–"}`, `primo audio ${x.first_audio_ms ?? "–"}`].join(" · ");
        $("latency").title += `\nServer (ms): ${parts}`;
        console.info(`turn ${m.turn}: ${parts} ms`);
      }
      if (!botActive()) refreshState();
      break;
    }
    case "tool": {
      if (m.turn !== currentTurn) break;
      const what = m.calls.map((c) => describeCall(c)).join(", ");
      addMsg("action", esc(`⚙ ${what}`));
      setState("thinking", "Eseguo…");
      break;
    }
    case "confirm":
      $("confirm").hidden = false;
      $("status").textContent = m.action;
      break;
    case "error":
      notice(m.fallback ? `${m.stage}: ${m.msg} → uso ${m.fallback}` : `Errore ${m.stage}: ${m.msg}`, true);
      break;
    case "reset_done":
      $("log").innerHTML = "";
      $("cap-user").textContent = $("cap-bot").textContent = "";
      notice("Nuova conversazione");
      break;
  }
}

function sendUtterance(f32) {
  uttId += 1;
  const header = new Int32Array([uttId, heard.turn, heard.seq, activeAtSpeechStart || botActive() || bargePaused ? FLAG_PLAYING : 0]);
  const pcm = new Int16Array(f32.length);
  for (let i = 0; i < f32.length; i++) {
    const s = Math.max(-1, Math.min(1, f32[i]));
    pcm[i] = s < 0 ? s * 32768 : s * 32767;
  }
  const out = new Uint8Array(header.byteLength + pcm.byteLength);
  out.set(new Uint8Array(header.buffer), 0);
  out.set(new Uint8Array(pcm.buffer), header.byteLength);
  if (!wsOpen()) return; // reconnecting: the sentence is lost, the status says so
  speechEndAt.set(uttId, performance.now());
  lastUttSent = uttId;
  ws.send(out.buffer);
}

// ---------------------------------------------------------------- VAD

async function setupVad() {
  vad = await window.vad.MicVAD.new({
    model: "v5",
    audioContext: ctx, // one context for mic and playback (vad-web resamples to 16 kHz)
    baseAssetPath: VENDOR,
    onnxWASMBasePath: VENDOR,
    positiveSpeechThreshold: 0.5,
    negativeSpeechThreshold: 0.35,
    redemptionMs,
    preSpeechPadMs: 300,
    minSpeechMs: 150,
    submitUserSpeechOnPause: false,
    getStream: async () => {
      const stream = await navigator.mediaDevices.getUserMedia({
        audio: { channelCount: 1, echoCancellation: true, noiseSuppression: true, autoGainControl: true },
      });
      micStream = stream;
      stream.getAudioTracks().forEach((t) => (t.onended = () => onContextState()));
      // Tap the mic for the orb (not routed to the speakers).
      micSource?.disconnect();
      micSource = ctx.createMediaStreamSource(stream);
      analyserIn = ctx.createAnalyser();
      analyserIn.fftSize = 512;
      analyserIn.smoothingTimeConstant = 0.5;
      micSource.connect(analyserIn);
      return stream;
    },
    // Mute = disable the track, keep the stream: no new permission prompt on iOS, the orb's
    // analyser stays attached. A track the system ended is reacquired through getStream.
    pauseStream: async (stream) => stream.getAudioTracks().forEach((t) => (t.enabled = false)),
    resumeStream: async (stream) => {
      const live = stream?.getAudioTracks().filter((t) => t.readyState === "live") ?? [];
      if (!live.length) return vad.options.getStream();
      live.forEach((t) => (t.enabled = true));
      return stream;
    },
    onFrameProcessed: (p) => {
      // Barge-in: sustained, confident speech while the bot is talking pauses playback.
      // The reply is only dropped once the server confirms a real transcript (turn_start).
      if (botActive() && !bargePaused) {
        bargeFrames = p.isSpeech > BARGE_PROB ? bargeFrames + 1 : 0;
        if (bargeFrames >= BARGE_FRAMES) {
          bargeFrames = 0;
          pausePlayback();
        }
      } else {
        bargeFrames = 0;
      }
    },
    onSpeechStart: () => {
      ws?.readyState === WebSocket.OPEN && ws.send(JSON.stringify({ type: "speech_start" }));
      activeAtSpeechStart = botActive() || bargePaused;
      if (!activeAtSpeechStart) setState("user-speaking", "Ti ascolto…");
      if (!activeAtSpeechStart) $("cap-bot").textContent = "";
    },
    onSpeechEnd: (audio) => {
      sendUtterance(audio);
      if (!botActive() || bargePaused) setState("thinking", "Ci penso…");
    },
    onVADMisfire: () => {
      resumePlayback();
      refreshState();
    },
  });
  await vad.start();
}

// ---------------------------------------------------------------- lifecycle

function resetClientState() {
  turns.clear();
  turnUtt.clear();
  speechEndAt.clear();
  heard = { turn: -1, seq: -1 };
  currentTurn = -1;
  bargeFrames = 0;
  activeAtSpeechStart = false;
  workletActive = false;
  flushPlayback();
}

async function start() {
  if (running || starting) return;
  starting = true;
  $("start").disabled = true;
  // Chat already open: keep its WebSocket, server history and client turns.
  if (ws?.readyState !== WebSocket.OPEN) resetClientState();
  setState("thinking", "Avvio…");
  try {
    unlockAudio();
    const ready = await ensureConnected();
    ttsRate = ready.tts_rate;
    await setupAudio();
    await setupVad();
    if (ws.readyState !== WebSocket.OPEN) throw new Error("connessione chiusa dal server");
  } catch (err) {
    console.error(err);
    await teardown();
    setState("idle", `Impossibile avviare: ${err.message || err}`);
    return;
  } finally {
    starting = false;
    $("start").disabled = false;
  }
  running = true;
  reconnectTries = 0;
  keepAwake(true);
  $("start").hidden = true;
  document.querySelector(".controls").hidden = false;
  refreshState();
  // No greeting in the middle of a conversation already started in the chat.
  ws.send(JSON.stringify({ type: "hello", greet: !$("log").querySelector(".msg") }));
}

// Voice only: the WebSocket stays open, so the conversation goes on in the chat.
async function teardown() {
  const [v, c] = [vad, ctx];
  vad = ctx = player = mediaDest = analyserOut = analyserIn = micSource = micStream = null;
  $("resume").hidden = true;
  try { await v?.destroy(); } catch {}
  await c?.close().catch(() => {});
  resetClientState();
}

async function stop() {
  if (botActive()) silenceBot();
  ws?.readyState === WebSocket.OPEN && ws.send(JSON.stringify({ type: "voice", on: false }));
  running = false;
  clearTimeout(reconnectTimer);
  reconnectTimer = null;
  keepAwake(false);
  setMuted(false);
  $("start").hidden = false;
  document.querySelector(".controls").hidden = true;
  $("cap-user").textContent = $("cap-bot").textContent = "";
  $("latency").textContent = "";
  await teardown();
  refreshState();
}

async function setMuted(on) {
  muted = on;
  document.body.classList.toggle("muted", on);
  $("mute").setAttribute("aria-label", on ? "Attiva microfono" : "Disattiva microfono");
  try {
    if (vad) await (on ? vad.pause() : vad.start());
  } catch (e) {
    console.warn(e);
  }
  refreshState();
}

function toggleDrawer(open) {
  const d = $("drawer");
  open ??= !d.classList.contains("open");
  d.classList.toggle("open", open);
  d.setAttribute("aria-hidden", String(!open));
}

$("start").addEventListener("click", start);
$("confirm-yes").addEventListener("click", () => answerConfirm(true));
$("confirm-no").addEventListener("click", () => answerConfirm(false));
$("mute").addEventListener("click", () => setMuted(!muted));
$("history-btn").addEventListener("click", () => toggleDrawer());
$("drawer-close").addEventListener("click", () => toggleDrawer(false));
$("scrim").addEventListener("click", () => toggleDrawer(false));
document.addEventListener("keydown", (e) => {
  if (e.key === "Escape") toggleDrawer(false);
  if (e.code === "Space" && e.target === document.body && running) {
    e.preventDefault();
    setMuted(!muted);
  }
});
function silenceBot() {
  // The server stops generating and keeps only what was heard.
  if (ws?.readyState === WebSocket.OPEN) {
    const seq = heard.turn === currentTurn ? heard.seq : -1;
    ws.send(JSON.stringify({ type: "interrupt", heard_turn: currentTurn, heard_seq: seq }));
  }
  const t = turns.get(currentTurn);
  if (t) { t.interrupted = true; if (t.bot) renderBot(currentTurn); }
  flushPlayback();
  currentTurn = -1;
  refreshState();
}

// ---------------------------------------------------------------- chat

async function sendText(text) {
  text = text.trim();
  if (!text) return false;
  try {
    await ensureConnected();
  } catch (err) {
    notice(`Impossibile connettersi: ${err.message || err}`, true);
    return false;
  }
  // What the user got of the current reply: heard (voice on) or read (text only).
  const t = turns.get(currentTurn);
  const seen = running ? (heard.turn === currentTurn ? heard.seq : -1) : (t ? t.sentences.length - 1 : -1);
  const playing = running && (botActive() || bargePaused);
  ws.send(JSON.stringify({ type: "text", text, heard_turn: currentTurn, heard_seq: seen, playing }));
  if (running) setState("thinking", "Ci penso…");
  return true;
}

function fitChatInput() {
  const el = $("chat-input");
  el.style.height = "auto";
  el.style.height = `${el.scrollHeight}px`;
  $("chat-send").disabled = !el.value.trim();
}

$("chat").addEventListener("submit", async (e) => {
  e.preventDefault();
  const el = $("chat-input");
  if (await sendText(el.value)) {
    el.value = "";
    fitChatInput();
  }
});
$("chat-input").addEventListener("input", fitChatInput);
$("chat-input").addEventListener("keydown", (e) => {
  if (e.key === "Enter" && !e.shiftKey && !e.isComposing) {
    e.preventDefault();
    $("chat").requestSubmit();
  }
});
$("chat-btn").addEventListener("click", () => {
  toggleDrawer(true);
  $("chat-input").focus();
});

$("viz").addEventListener("click", () => {
  if (!running) start();
  else if (botActive()) silenceBot(); // tap to silence the bot
});
$("stop").addEventListener("click", stop);
$("reset").addEventListener("click", () => {
  flushPlayback();
  ws?.readyState === WebSocket.OPEN && ws.send(JSON.stringify({ type: "reset" }));
  if (ws?.readyState !== WebSocket.OPEN) $("log").innerHTML = "";
});
$("aec-route").addEventListener("change", routeAudio);
$("resume").addEventListener("click", resumeAudio);

// Screen locked / app switched: iOS suspends the page and usually drops the socket and mic.
document.addEventListener("visibilitychange", () => {
  if (!running) return;
  if (document.hidden) {
    if (botActive()) silenceBot(); // the server keeps only what was heard
    // Wind audio down ourselves (silent output, muted mic, suspended context) instead of
    // letting iOS cut a live session a moment after the lock, which can make a sound.
    micStream?.getAudioTracks().forEach((t) => (t.enabled = false));
    ctx?.suspend().catch(() => {});
    return;
  }
  keepAwake(true);
  if (!muted) micStream?.getAudioTracks().forEach((t) => (t.enabled = true));
  ctx?.resume().catch(() => {}).finally(onContextState); // not allowed without a tap: "tap to resume"
  if (micStream?.getAudioTracks().some((t) => t.readyState === "ended")) $("resume").hidden = false;
  if (!wsOpen()) {
    reconnectTries = 0;
    clearTimeout(reconnectTimer);
    reconnectTimer = null;
    scheduleReconnect();
  }
  refreshState();
});

if ("serviceWorker" in navigator && window.isSecureContext) {
  navigator.serviceWorker.register("sw.js").catch((e) => console.warn("service worker", e));
}
if (IOS) $("aec-route").closest("label").hidden = true; // iOS: always the built-in echo cancellation
speechSynthesis.getVoices();
fitChatInput();
refreshState();
