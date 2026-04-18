// Elements
const chat = document.getElementById("chat");
const startBtn = document.getElementById("startBtn");
const endBtn = document.getElementById("endBtn");
const statusPill = document.getElementById("status-pill");
const statusDot = document.getElementById("status-dot");
const statusText = document.getElementById("status-text");
const errorBanner = document.getElementById("error-banner");

// Web Speech Recognition
const SR = window.SpeechRecognition || window.webkitSpeechRecognition;
let rec = SR ? new SR() : null;

let autoListen = false;
let busy = false;
let speaking = false;
let started = false;
let currentAudio = null;
let recognitionActive = false;
let listenTimer = null;
let micPermissionChecked = false;
let lastUserUtterance = "";
let lastUserUtteranceAt = 0;
let ignoreInputUntil = 0;

const DUPLICATE_WINDOW_MS = 2500;
const ECHO_GUARD_MS = 900;

// ---------- UI & Feedback ----------
function setStatus(state) {
  if (!statusPill) return;
  statusPill.className = "status-pill";
  if (state) statusPill.classList.add(state);
  
  if (state === "listening") {
      statusDot.textContent = "●";
      statusText.textContent = "Listening...";
  } else if (state === "processing") {
      statusDot.textContent = "●";
      statusText.textContent = "Processing...";
  } else if (state === "speaking") {
      statusDot.textContent = "●";
      statusText.textContent = "Tap to interrupt";
  } else {
      statusPill.classList.add("ready");
      statusDot.textContent = "●";
      statusText.textContent = started ? "Ready to assist" : "Ready to start";
  }
}

function hidePlaceholder() {
  const placeholder = document.getElementById("chat-placeholder");
  if (placeholder) placeholder.style.display = "none";
}

function showError(msg) {
  if (!errorBanner) return;
  errorBanner.textContent = msg;
  errorBanner.style.display = "block";
  setTimeout(() => { errorBanner.style.display = "none"; }, 5000);
}

function addMessage(text, who) {
  hidePlaceholder();
  const typing = document.getElementById("typing");
  if (typing) typing.remove();

  const div = document.createElement("div");
  div.className = who;
  div.textContent = text;
  chat.appendChild(div);
  
  // Defer scrolling so DOM updates visually prior to the repositioning
  requestAnimationFrame(() => {
    chat.scrollTop = chat.scrollHeight;
  });
}

function showTyping() {
  setStatus("processing");
  const row = document.createElement("div");
  row.id = "typing";
  row.className = "typing";
  row.innerHTML = `<span class="dot"></span><span class="dot"></span><span class="dot"></span> Processing…`;
  chat.appendChild(row);
  
  requestAnimationFrame(() => {
    chat.scrollTop = chat.scrollHeight;
  });
}

function playChime() {
  const ctx = new (window.AudioContext || window.webkitAudioContext)();
  const o = ctx.createOscillator();
  const g = ctx.createGain();
  o.type = "sine";
  o.frequency.value = 660;
  o.connect(g);
  g.connect(ctx.destination);
  g.gain.setValueAtTime(0.0001, ctx.currentTime);
  g.gain.exponentialRampToValueAtTime(0.07, ctx.currentTime + 0.03);
  g.gain.exponentialRampToValueAtTime(0.0001, ctx.currentTime + 0.30);
  o.start();
  o.stop(ctx.currentTime + 0.32);
}

function normalizeTranscript(text) {
  return (text || "")
    .replace(/[\u2018\u2019]/g, "'")
    .replace(/[\u201C\u201D]/g, '"')
    .replace(/\s+/g, " ")
    .replace(/[.,!?;:]+$/g, "")
    .trim();
}

function scheduleListening(delayMs = 0) {
  if (listenTimer) {
    clearTimeout(listenTimer);
    listenTimer = null;
  }
  if (!autoListen || !started) return;

  listenTimer = setTimeout(() => {
    listenTimer = null;
    startListening();
  }, Math.max(0, delayMs));
}

function shouldIgnoreUtterance(text, confidence) {
  const normalized = normalizeTranscript(text);
  if (normalized.length < 2) return true;
  if (Date.now() < ignoreInputUntil) return true;

  const key = normalized.toLowerCase();
  const now = Date.now();
  if (key === lastUserUtterance && (now - lastUserUtteranceAt) < DUPLICATE_WINDOW_MS) {
    return true;
  }

  if (typeof confidence === "number" && confidence > 0 && confidence < 0.35) {
    showError("Low confidence voice capture. Please repeat once.");
    return true;
  }

  lastUserUtterance = key;
  lastUserUtteranceAt = now;
  return false;
}

async function ensureMicrophoneReady() {
  if (micPermissionChecked) return true;
  if (!navigator.mediaDevices || !navigator.mediaDevices.getUserMedia) return true;

  try {
    const stream = await navigator.mediaDevices.getUserMedia({
      audio: {
        echoCancellation: true,
        noiseSuppression: true,
        autoGainControl: true,
      },
    });
    stream.getTracks().forEach((track) => track.stop());
    micPermissionChecked = true;
    return true;
  } catch (err) {
    console.warn("Microphone permission error:", err);
    showError("Please allow microphone access for better speech recognition.");
    return false;
  }
}

// Push to interrupt: click pill to immediately halt audio stream
if (statusPill) {
    statusPill.onclick = () => {
        if (speaking && currentAudio) {
            currentAudio.pause();
            currentAudio = null;
            speaking = false;
            busy = false;
      ignoreInputUntil = Date.now() + 300;
      scheduleListening(300);
        }
    };
}


// ---------- Session Reset ----------
async function resetServerState() {
  try {
    await fetch("/reset_state", {
      method: "POST",
      headers: {"Content-Type": "application/json"},
    });
  } catch (err) {
    console.warn("Could not reset server state:", err);
  }
}


// ---------- Quick Chips ----------
window.sendChip = async (text) => {
  const canonical = {
    "book table": "I want to book a table.",
    "hours": "What are your opening hours and timings?",
    "menu": "Please share your menu highlights with prices.",
    "cancel": "I want to cancel my reservation.",
  };
  const query = canonical[(text || "").toLowerCase()] || text;

    if (!started) {
        started = true;
        autoListen = true;
        startBtn.disabled = true;
        endBtn.disabled = false;

      await resetServerState();
        
        hidePlaceholder();
        addMessage(text, "user");
    await sendMessage(query);
    } else {
        if (!busy && !speaking) {
            addMessage(text, "user");
      await sendMessage(query);
        }
    }
};


// ---------- Speech Recognition ----------
function startListening() {
  if (!rec || speaking || busy || recognitionActive || !autoListen || !started) return;

  const msUntilReady = ignoreInputUntil - Date.now();
  if (msUntilReady > 0) {
    scheduleListening(msUntilReady);
    return;
  }

  try {
    rec.start();
  } catch (e) {
    const name = e && e.name ? e.name : "";
    if (name !== "InvalidStateError") {
      console.warn("Speech start error:", e);
    }
    scheduleListening(300);
  }
}

function stopListening() {
  if (listenTimer) {
    clearTimeout(listenTimer);
    listenTimer = null;
  }
  if (!rec) return;
  try {
    rec.stop();
  } catch (e) {}
  if (!busy && !speaking) setStatus("");
}

if (!rec) {
    showError("Speech Recognition is not supported in this browser. Try Chrome/Edge.");
    if (startBtn) startBtn.disabled = true;
} else {
  rec.lang = "en-IN";
  rec.continuous = false;
  rec.interimResults = false;
  rec.maxAlternatives = 3;

  rec.onstart = () => {
    recognitionActive = true;
    if (!busy && !speaking) setStatus("listening");
  };
  
  // Handle common speech-recognition failures with graceful recovery.
  rec.onerror = (ev) => {
      recognitionActive = false;
      console.warn("Speech error:", ev.error);

      if (ev.error === "not-allowed" || ev.error === "service-not-allowed" || ev.error === "audio-capture" || ev.error === "microphone") {
          showError("Microphone access denied or unavailable. Please enable it in browser settings.");
          stopListening();
          endSessionFromError();
          return;
      }

      if (ev.error === "network") {
          showError("Speech recognition network issue. Retrying...");
          scheduleListening(1200);
          return;
      }

      if (ev.error === "no-speech") {
          scheduleListening(350);
          return;
      }

      if (ev.error !== "aborted") {
          scheduleListening(500);
      }
  };

  rec.onresult = (ev) => {
    if (speaking || busy || Date.now() < ignoreInputUntil) return;

    for (let i = ev.resultIndex; i < ev.results.length; i++) {
      if (ev.results[i].isFinal) {
        const result = ev.results[i];
        let best = result[0];
        for (let j = 1; j < result.length; j++) {
            if ((result[j].confidence || 0) > (best.confidence || 0)) {
                best = result[j];
            }
        }

        const text = normalizeTranscript(best.transcript);
        if (shouldIgnoreUtterance(text, best.confidence)) continue;

        addMessage(text, "user");
        void sendMessage(text);
        return;
      }
    }
  };

  rec.onend = () => {
    recognitionActive = false;
    if (!autoListen || !started || speaking || busy) return;

    const delay = Math.max(150, ignoreInputUntil - Date.now());
    scheduleListening(delay);
  };
}


// ---------- Send to Server ----------
async function sendMessage(message) {
  busy = true;
  stopListening();
  showTyping();
  playChime();

  try {
      const res = await fetch("/send_message", {
        method: "POST",
        headers: {"Content-Type":"application/json"},
        body: JSON.stringify({ message })
      });
      
      if (!res.ok) throw new Error("Server disconnected");
      
      const data = await res.json();
      addMessage(data.response, "bot");

      if (data.audio) {
        speaking = true;
        setStatus("speaking");
        currentAudio = new Audio("data:audio/mp3;base64," + data.audio);
        currentAudio.play().catch((err) => {
          console.warn("Audio playback failed:", err);
          speaking = false;
          busy = false;
          currentAudio = null;
          scheduleListening(250);
        });
        currentAudio.onended = () => { 
            ignoreInputUntil = Date.now() + ECHO_GUARD_MS;
            speaking = false; 
            busy = false; 
            currentAudio = null; 
            scheduleListening(ECHO_GUARD_MS);
        };
        currentAudio.onerror = () => {
            speaking = false;
            busy = false;
            currentAudio = null;
            scheduleListening(250);
        };
      } else {
        ignoreInputUntil = Date.now() + 180;
        speaking = false;
        busy = false;
        scheduleListening(180);
      }
      
  } catch (err) {
      console.error(err);
      const typing = document.getElementById("typing");
      if (typing) typing.remove();
      
      addMessage("Network Error: Could not reach the AI.", "error-msg");
      busy = false;
      speaking = false;
      scheduleListening(350);
  }
}


function endSessionFromError() {
  autoListen = false;
  started = false;
  recognitionActive = false;
  ignoreInputUntil = 0;
  startBtn.disabled = false;
  endBtn.disabled = true;
  setStatus("");
  stopListening();
  void resetServerState();
}


// ---------- Start / End Session ----------
if (startBtn) {
    startBtn.onclick = async () => {
      if (started) return;

  const micReady = await ensureMicrophoneReady();
  if (!micReady) return;

      started = true;
      autoListen = true;
      startBtn.disabled = true;
      endBtn.disabled = false;
      
      hidePlaceholder();
      setStatus("processing");

      await resetServerState();

      try {
        const res = await fetch("/send_message", {
          method: "POST",
          headers: {"Content-Type":"application/json"},
          body: JSON.stringify({ message: "__WELCOME__" })
        });
        
        if (!res.ok) throw new Error("Server connection rejected");

        const data = await res.json();
        addMessage(data.response, "bot");

        if (data.audio) {
          speaking = true;
          setStatus("speaking");
          currentAudio = new Audio("data:audio/mp3;base64," + data.audio);
          currentAudio.play().catch((err) => {
              console.warn("Welcome audio playback failed:", err);
              speaking = false;
              currentAudio = null;
              scheduleListening(220);
          });
          currentAudio.onended = () => { 
              ignoreInputUntil = Date.now() + ECHO_GUARD_MS;
              speaking = false; 
              currentAudio = null; 
              scheduleListening(ECHO_GUARD_MS);
          };
          currentAudio.onerror = () => {
              speaking = false;
              currentAudio = null;
              scheduleListening(220);
          };
        } else {
          scheduleListening(220);
        }
      } catch (err) {
          showError("Failed to wake up VAANI server.");
          endSessionFromError();
      }
    };
}

if (endBtn) {
    endBtn.onclick = () => {
      autoListen = false;
      started = false;
  recognitionActive = false;
  ignoreInputUntil = 0;
      stopListening();
      startBtn.disabled = false;
      endBtn.disabled = true;
      
      if (currentAudio) {
          currentAudio.pause();
          currentAudio = null;
      }
      speaking = false;
      busy = false;

      void resetServerState();
      
      addMessage("Call ended.", "bot");
      setStatus("");
    };
}

