const form = document.querySelector("#transcription-form");
const fileInput = document.querySelector("#media-file");
const dropzone = document.querySelector("#dropzone");
const submitButton = document.querySelector("#submit-button");
const selectedFile = document.querySelector("#selected-file");
const formError = document.querySelector("#form-error");
const resultTitle = document.querySelector("#result-title");
const resultBadge = document.querySelector("#result-badge");
const emptyState = document.querySelector("#empty-state");
const loadingState = document.querySelector("#loading-state");
const transcriptContent = document.querySelector("#transcript-content");
const noSpeechState = document.querySelector("#no-speech-state");
const resultError = document.querySelector("#result-error");
const transcriptText = document.querySelector("#transcript-text");
const segmentList = document.querySelector("#segment-list");
const segmentsSection = document.querySelector("#segments-section");
const resultMeta = document.querySelector("#result-meta");
const copyFeedback = document.querySelector("#copy-feedback");

let latestTranscript = "";
let selectedFilename = "transcript";

function formatBytes(bytes) {
  if (bytes < 1024 * 1024) return `${Math.max(1, Math.round(bytes / 1024))} KB`;
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
}

function showFormError(message) {
  formError.textContent = message;
  formError.hidden = false;
}

function setFile(file) {
  formError.hidden = true;
  if (!file) {
    fileInput.value = "";
    selectedFile.hidden = true;
    submitButton.disabled = true;
    return;
  }

  document.querySelector("#file-name").textContent = file.name;
  document.querySelector("#file-size").textContent = formatBytes(file.size);
  selectedFile.hidden = false;
  submitButton.disabled = false;
}

function resetResult() {
  emptyState.hidden = true;
  loadingState.hidden = true;
  transcriptContent.hidden = true;
  noSpeechState.hidden = true;
  resultError.hidden = true;
  copyFeedback.textContent = "";
  transcriptText.textContent = "";
  segmentList.replaceChildren();
  segmentsSection.hidden = true;
  latestTranscript = "";
}

function formatTimestamp(seconds) {
  const totalSeconds = Math.max(0, Math.floor(seconds));
  const hours = Math.floor(totalSeconds / 3600);
  const minutes = Math.floor((totalSeconds % 3600) / 60);
  const remainder = totalSeconds % 60;
  const padded = String(remainder).padStart(2, "0");
  if (hours > 0) return `${hours}:${String(minutes).padStart(2, "0")}:${padded}`;
  return `${minutes}:${padded}`;
}

function renderTranscript(result) {
  const text = result.text.trim();
  if (!text) {
    noSpeechState.hidden = false;
    resultTitle.textContent = "Nothing clear came through";
    resultBadge.textContent = "NO SPEECH FOUND";
    return;
  }

  latestTranscript = text;
  transcriptText.textContent = text;
  transcriptText.dir = "auto";
  const languageNames = { en: "English", he: "Hebrew" };
  const language = languageNames[result.language] || result.language.toUpperCase();
  resultMeta.textContent = `${language} · ${formatTimestamp(result.duration_seconds)} · ${result.segments.length} segments`;

  for (const segment of result.segments) {
    const item = document.createElement("li");
    item.className = "segment-item";

    const timestamp = document.createElement("span");
    timestamp.className = "segment-time";
    timestamp.textContent = formatTimestamp(segment.start);

    const textNode = document.createElement("p");
    textNode.className = "segment-copy";
    textNode.dir = "auto";
    textNode.textContent = segment.text;

    item.append(timestamp, textNode);
    segmentList.append(item);
  }

  segmentsSection.hidden = result.segments.length === 0;
  transcriptContent.hidden = false;
  resultTitle.textContent = "Transcript ready";
  resultBadge.textContent = "COMPLETE";
}

function friendlyError(payload, status) {
  const code = payload?.error?.code;
  const messages = {
    model_not_ready: "The transcription model is still starting. Wait a moment and try again.",
    model_busy: "The model is busy with another recording. Please try again shortly.",
    unsupported_media: "That file could not be read. Try a supported audio or video format.",
    no_audio_stream: "This video does not contain an audio track.",
    media_too_long: "This recording is longer than the current service limit.",
    upload_too_large: "This file is larger than the current upload limit.",
    invalid_language: "Choose Auto-detect, Hebrew, or English and try again.",
    transcription_failed: "The recording could not be transcribed. Please try again.",
  };
  return messages[code] || payload?.error?.message || `The request failed (${status}). Please try again.`;
}

fileInput.addEventListener("change", () => setFile(fileInput.files[0]));

document.querySelector("#remove-file").addEventListener("click", () => {
  setFile(null);
  fileInput.focus();
});

for (const eventName of ["dragenter", "dragover"]) {
  dropzone.addEventListener(eventName, (event) => {
    event.preventDefault();
    dropzone.classList.add("is-dragging");
  });
}

for (const eventName of ["dragleave", "drop"]) {
  dropzone.addEventListener(eventName, (event) => {
    event.preventDefault();
    dropzone.classList.remove("is-dragging");
  });
}

dropzone.addEventListener("drop", (event) => {
  const [file] = event.dataTransfer.files;
  if (!file) return;
  const transfer = new DataTransfer();
  transfer.items.add(file);
  fileInput.files = transfer.files;
  setFile(file);
});

form.addEventListener("submit", async (event) => {
  event.preventDefault();
  const file = fileInput.files[0];
  if (!file) {
    showFormError("Choose an audio or video file first.");
    return;
  }

  formError.hidden = true;
  resetResult();
  loadingState.hidden = false;
  resultTitle.textContent = "Transcribing your recording…";
  resultBadge.textContent = "IN PROGRESS";
  submitButton.disabled = true;
  form.setAttribute("aria-busy", "true");

  const formData = new FormData(form);
  selectedFilename = file.name.replace(/\.[^.]+$/, "") || "transcript";

  try {
    const response = await fetch("/v1/transcriptions", { method: "POST", body: formData });
    const payload = await response.json();
    loadingState.hidden = true;

    if (!response.ok) {
      resultError.textContent = friendlyError(payload, response.status);
      resultError.hidden = false;
      resultTitle.textContent = "We couldn’t transcribe this file";
      resultBadge.textContent = "NEEDS ATTENTION";
      return;
    }

    renderTranscript(payload);
    resultTitle.focus();
  } catch {
    loadingState.hidden = true;
    resultError.textContent = "Could not reach the transcription service. Check that it is running and try again.";
    resultError.hidden = false;
    resultTitle.textContent = "Connection problem";
    resultBadge.textContent = "OFFLINE";
  } finally {
    submitButton.disabled = !fileInput.files.length;
    form.removeAttribute("aria-busy");
  }
});

document.querySelector("#copy-button").addEventListener("click", async () => {
  if (!latestTranscript) return;
  try {
    await navigator.clipboard.writeText(latestTranscript);
    copyFeedback.textContent = "Transcript copied to clipboard.";
  } catch {
    copyFeedback.textContent = "Clipboard access was unavailable. Select the transcript text to copy it.";
  }
});

document.querySelector("#download-button").addEventListener("click", () => {
  if (!latestTranscript) return;
  const blob = new Blob([`${latestTranscript}\n`], { type: "text/plain;charset=utf-8" });
  const url = URL.createObjectURL(blob);
  const link = document.createElement("a");
  link.href = url;
  link.download = `${selectedFilename}.txt`;
  link.click();
  window.setTimeout(() => URL.revokeObjectURL(url), 1000);
});

async function updateServiceStatus() {
  const status = document.querySelector("#service-status");
  const statusText = document.querySelector("#service-status-text");
  try {
    const response = await fetch("/health/ready", { headers: { Accept: "application/json" } });
    if (!response.ok) throw new Error("not ready");
    status.classList.add("is-ready");
    status.classList.remove("is-unavailable");
    statusText.textContent = "Model ready";
  } catch {
    status.classList.add("is-unavailable");
    statusText.textContent = "Model unavailable";
  }
}

updateServiceStatus();
