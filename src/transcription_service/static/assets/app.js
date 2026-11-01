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
const mediaPlayer = document.querySelector("#media-player");
const playbackSection = document.querySelector("#playback-section");
const playbackHelp = document.querySelector("#playback-help");
const playbackFile = document.querySelector("#playback-file");
const loadingTitle = document.querySelector(".loading-title");
const loadingCopy = document.querySelector(".loading-copy");
const elapsedTime = document.querySelector("#elapsed-time");

let latestTranscript = "";
let latestResultUrl = "";
let selectedFilename = "transcript";
let isTranscribing = false;
let clientConfig;
let playbackUrl = "";
let progressTimer;
let activeModelProfile = "faster";
const pendingJobStorageKey = "transcription-studio.pending-job";

async function getClientConfig() {
  if (!clientConfig) {
    const response = await fetch("/client-config");
    if (!response.ok) throw new Error("Could not load service configuration");
    clientConfig = await response.json();
  }
  return clientConfig;
}

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
  submitButton.disabled = isTranscribing;
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
  latestResultUrl = "";
  playbackSection.hidden = true;
  clearPlayback();
}

function clearPlayback() {
  mediaPlayer.pause();
  mediaPlayer.removeAttribute("src");
  mediaPlayer.load();
  mediaPlayer.hidden = true;
  if (playbackUrl) URL.revokeObjectURL(playbackUrl);
  playbackUrl = "";
  playbackFile.value = "";
}

function attachPlayback(file) {
  clearPlayback();
  playbackUrl = URL.createObjectURL(file);
  mediaPlayer.classList.toggle("is-audio", !/\.(mp4|mov|webm|mkv)$/i.test(file.name));
  mediaPlayer.src = playbackUrl;
  mediaPlayer.hidden = false;
  playbackHelp.textContent = "Playback uses your local file. Click a timestamp to jump to that segment.";
  updatePlayback();
}

function updatePlayback() {
  const ready = Boolean(playbackUrl) && mediaPlayer.readyState >= 1 && !mediaPlayer.error;
  for (const item of segmentList.children) {
    item.querySelector("button").disabled = !ready;
    const active = ready && mediaPlayer.currentTime >= Number(item.dataset.start)
      && mediaPlayer.currentTime < Number(item.dataset.end);
    if (active) item.setAttribute("aria-current", "true");
    else item.removeAttribute("aria-current");
  }
}

playbackFile.addEventListener("change", () => {
  const file = playbackFile.files[0];
  if (file) attachPlayback(file);
});
for (const event of ["loadedmetadata", "timeupdate", "emptied"]) {
  mediaPlayer.addEventListener(event, updatePlayback);
}
mediaPlayer.addEventListener("error", () => {
  playbackHelp.textContent = "Your browser cannot play this format. The transcript and downloads are still available.";
  updatePlayback();
});

function startProgress(startedAt = Date.now(), modelProfile = "faster") {
  activeModelProfile = modelProfile;
  isTranscribing = true;
  for (const control of form.querySelectorAll("input, select, button")) control.disabled = true;
  form.setAttribute("aria-busy", "true");
  loadingState.hidden = false;
  loadingTitle.textContent = "Uploading your recording…";
  loadingCopy.textContent = "Keep this page open until the upload is accepted.";
  const updateElapsed = () => {
    elapsedTime.textContent = `Elapsed ${formatTimestamp((Date.now() - startedAt) / 1000)}`;
  };
  updateElapsed();
  window.clearInterval(progressTimer);
  progressTimer = window.setInterval(updateElapsed, 1000);
}

function finishProgress() {
  window.clearInterval(progressTimer);
  isTranscribing = false;
  loadingState.hidden = true;
  for (const control of form.querySelectorAll("input, select, button")) control.disabled = false;
  submitButton.disabled = !fileInput.files.length;
  form.removeAttribute("aria-busy");
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
  playbackSection.hidden = false;
  if (!playbackUrl) {
    playbackHelp.textContent = "Choose the original recording to enable playback and clickable timestamps. It stays in your browser.";
  }
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
  const model = result.model.split("/").pop();
  const segmentCount = result.segments.length;
  resultMeta.textContent = `${language} · ${formatTimestamp(result.duration_seconds)} · ${segmentCount} ${segmentCount === 1 ? "segment" : "segments"} · ${model}`;

  for (const segment of result.segments) {
    const item = document.createElement("li");
    item.className = "segment-item";
    item.dataset.start = segment.start;
    item.dataset.end = segment.end;

    const timestamp = document.createElement("button");
    timestamp.type = "button";
    timestamp.className = "segment-time";
    timestamp.textContent = formatTimestamp(segment.start);
    timestamp.setAttribute("aria-label", `Play from ${formatTimestamp(segment.start)}`);
    timestamp.addEventListener("click", async () => {
      mediaPlayer.currentTime = Math.min(segment.start, mediaPlayer.duration);
      try {
        await mediaPlayer.play();
      } catch {
        playbackHelp.textContent = "Press Play on the recording to continue listening.";
      }
    });

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
  updatePlayback();
}

function friendlyError(payload, status) {
  const code = payload?.error?.code;
  const messages = {
    upload_busy: "The service is processing other uploads. Please try again shortly.",
    unsupported_media: "That file could not be read. Try a supported audio or video format.",
    no_audio_stream: "This video does not contain an audio track.",
    media_too_long: "This recording is longer than the current service limit.",
    upload_too_large: "This file is larger than the current upload limit.",
    media_storage_full: "Local media storage is full. Please try again later.",
    job_queue_full: "The transcription queue is full. Please try again later.",
    idempotency_conflict: "This upload key was already used for a different recording.",
    job_service_not_ready: "The job service is still starting. Please try again shortly.",
    rate_limit_exceeded: "Too many uploads were submitted. Wait a moment and try again.",
    invalid_language: "Choose Auto-detect, Hebrew, or English and try again.",
    transcription_failed: "The recording could not be transcribed. Please try again.",
  };
  return messages[code] || payload?.error?.message || `The request failed (${status}). Please try again.`;
}

function wait(milliseconds) {
  return new Promise((resolve) => window.setTimeout(resolve, milliseconds));
}

async function waitForJob(accepted) {
  while (true) {
    const statusResponse = await fetch(accepted.status_url, { headers: { Accept: "application/json" } });
    const job = await statusResponse.json();
    if (!statusResponse.ok) {
      if (statusResponse.status === 404 || statusResponse.status === 410) {
        localStorage.removeItem(pendingJobStorageKey);
      }
      throw new Error(friendlyError(job, statusResponse.status));
    }
    if (job.status === "succeeded") {
      const resultResponse = await fetch(accepted.result_url, { headers: { Accept: "application/json" } });
      const result = await resultResponse.json();
      if (!resultResponse.ok) throw new Error(friendlyError(result, resultResponse.status));
      latestResultUrl = accepted.result_url;
      localStorage.removeItem(pendingJobStorageKey);
      return result;
    }
    if (job.status === "failed" || job.status === "expired") {
      localStorage.removeItem(pendingJobStorageKey);
      throw new Error(job.error_message || `The transcription job ${job.status}.`);
    }
    resultBadge.textContent = job.status.toUpperCase();
    if (job.status === "queued") {
      const retry = job.next_retry_at ? new Date(job.next_retry_at).getTime() : null;
      loadingTitle.textContent = retry ? "Preparing another attempt…" : "Waiting for the worker…";
      loadingCopy.textContent = retry
        ? `Attempt ${job.attempts} of ${job.max_attempts} did not finish. Retrying in ${Math.max(0, Math.ceil((retry - Date.now()) / 1000))}s.`
        : "Your upload is saved. You can reload this page and resume waiting.";
    } else {
      loadingTitle.textContent = "Transcribing your recording…";
      loadingCopy.textContent = `Attempt ${job.attempts} of ${job.max_attempts}. ${activeModelProfile === "larger"
        ? "First use of Advanced downloads several GB."
        : "First use downloads the model; longer recordings take more time."}`;
    }
    await wait(1000);
  }
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
  if (isTranscribing) return;
  const [file] = event.dataTransfer.files;
  if (!file) return;
  const transfer = new DataTransfer();
  transfer.items.add(file);
  fileInput.files = transfer.files;
  setFile(file);
});

form.addEventListener("submit", async (event) => {
  event.preventDefault();
  if (isTranscribing) return;
  const file = fileInput.files[0];
  if (!file) {
    showFormError("Choose an audio or video file first.");
    return;
  }

  formError.hidden = true;
  resetResult();
  resultTitle.textContent = "Transcribing your recording…";
  resultBadge.textContent = "IN PROGRESS";

  // Capture values before disabling controls: disabled fields are omitted by FormData.
  const formData = new FormData(form);
  const startedAt = Date.now();
  startProgress(startedAt, formData.get("model_profile"));
  attachPlayback(file);
  selectedFilename = file.name.replace(/\.[^.]+$/, "") || "transcript";

  try {
    const config = await getClientConfig();
    if (file.size > config.max_upload_bytes) {
      loadingState.hidden = true;
      showFormError(`Choose a file smaller than ${formatBytes(config.max_upload_bytes)}.`);
      resultTitle.textContent = "Choose a smaller file";
      resultBadge.textContent = "FILE TOO LARGE";
      return;
    }
    const response = await fetch(config.transcription_url, { method: "POST", body: formData });
    let payload = await response.json();

    if (!response.ok) {
      resultError.textContent = friendlyError(payload, response.status);
      resultError.hidden = false;
      resultTitle.textContent = "We couldn’t transcribe this file";
      resultBadge.textContent = "NEEDS ATTENTION";
      return;
    }

    localStorage.setItem(pendingJobStorageKey, JSON.stringify({
      status_url: payload.status_url,
      result_url: payload.result_url,
      filename: selectedFilename,
      started_at: startedAt,
      model_profile: activeModelProfile,
    }));
    payload = await waitForJob(payload);
    renderTranscript(payload);
    resultTitle.focus();
  } catch (error) {
    loadingState.hidden = true;
    resultError.textContent = error.message || "Could not reach the transcription service. Check that it is running and try again.";
    resultError.hidden = false;
    resultTitle.textContent = "Transcription needs attention";
    resultBadge.textContent = "NEEDS ATTENTION";
  } finally {
    finishProgress();
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

document.querySelector("#download-button").addEventListener("click", async () => {
  if (!latestResultUrl) return;
  const format = document.querySelector("#download-format").value;
  copyFeedback.textContent = "Preparing download…";
  try {
    const response = await fetch(`${latestResultUrl}?format=${format}`);
    if (!response.ok) {
      const payload = await response.json();
      throw new Error(friendlyError(payload, response.status));
    }
    const blob = await response.blob();
    const url = URL.createObjectURL(blob);
    const link = document.createElement("a");
    link.href = url;
    link.download = `${selectedFilename}.${format}`;
    link.click();
    copyFeedback.textContent = `${format.toUpperCase()} download ready.`;
    window.setTimeout(() => URL.revokeObjectURL(url), 1000);
  } catch (error) {
    copyFeedback.textContent = error.message || "The download could not be prepared.";
  }
});

async function restorePendingJob() {
  let pending;
  try {
    pending = JSON.parse(localStorage.getItem(pendingJobStorageKey) || "null");
  } catch {
    localStorage.removeItem(pendingJobStorageKey);
    return;
  }
  if (!pending) return;
  if (typeof pending.status_url !== "string" || typeof pending.result_url !== "string") {
    localStorage.removeItem(pendingJobStorageKey);
    return;
  }

  selectedFilename = pending.filename || "transcript";
  resetResult();
  startProgress(Number.isFinite(pending.started_at) ? pending.started_at : Date.now(), pending.model_profile);
  resultTitle.textContent = "Recovering your transcription…";
  resultBadge.textContent = "IN PROGRESS";
  try {
    const result = await waitForJob(pending);
    loadingState.hidden = true;
    renderTranscript(result);
    resultTitle.focus();
  } catch (error) {
    loadingState.hidden = true;
    resultError.textContent = error.message || "Could not recover the transcription job.";
    resultError.hidden = false;
    resultTitle.textContent = "Recovery needs attention";
    resultBadge.textContent = "NEEDS ATTENTION";
  } finally {
    finishProgress();
  }
}

async function updateServiceStatus() {
  const status = document.querySelector("#service-status");
  const statusText = document.querySelector("#service-status-text");
  try {
    const response = await fetch("/health/ready", { headers: { Accept: "application/json" } });
    if (!response.ok) throw new Error("not ready");
    status.classList.add("is-ready");
    status.classList.remove("is-unavailable");
    statusText.textContent = "Job service ready";
  } catch {
    status.classList.add("is-unavailable");
    statusText.textContent = "Job service unavailable";
  }
}

updateServiceStatus();
restorePendingJob();
