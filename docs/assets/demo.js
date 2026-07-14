const byId = (id) => document.getElementById(id);

const transcript = [
  { seconds: 0, speaker: "Speaker 1", text: "Thanks for walking through the release plan." },
  { seconds: 8, speaker: "Speaker 2", text: "The long recording is split into bounded parts first." },
  { seconds: 17, speaker: "Speaker 1", text: "Then each result is merged on a shared timeline." },
];

let tier = "free";
let format = "timestamps";

function pad(value) { return String(value).padStart(2, "0"); }
function timestamp(seconds) { return `${pad(Math.floor(seconds / 60))}:${pad(seconds % 60)}`; }
function srtTimestamp(seconds) { return `00:${timestamp(seconds)},000`; }

function renderTranscript() {
  const names = {
    "Speaker 1": byId("speaker-one").value.trim() || "Speaker 1",
    "Speaker 2": byId("speaker-two").value.trim() || "Speaker 2",
  };
  let output;
  if (format === "plain") {
    output = transcript.map((line) => `${names[line.speaker]}: ${line.text}`).join("\n");
  } else if (format === "srt") {
    output = transcript.map((line, index) => {
      const end = transcript[index + 1]?.seconds ?? line.seconds + 5;
      return `${index + 1}\n${srtTimestamp(line.seconds)} --> ${srtTimestamp(end)}\n${names[line.speaker]}: ${line.text}`;
    }).join("\n\n");
  } else {
    output = transcript.map((line) => `[${timestamp(line.seconds)}] ${names[line.speaker]}: ${line.text}`).join("\n");
  }
  byId("transcript-output").textContent = output;
}

function setTier(next) {
  tier = next;
  document.querySelectorAll("[data-tier]").forEach((button) => {
    const active = button.dataset.tier === tier;
    button.classList.toggle("active", active);
    button.setAttribute("aria-pressed", String(active));
  });
}

function runPreflight() {
  const minutes = Math.max(1, Math.min(600, Number(byId("duration").value) || 1));
  const used = Math.max(0, Math.min(20, Number(byId("used").value) || 0));
  byId("duration").value = minutes;
  byId("used").value = used;
  const chunks = Math.ceil(minutes / 10);
  const remaining = Math.max(0, 20 - used);
  const severity = tier === "paid" ? "ok" : chunks > remaining ? "blocked" : chunks > 1 ? "warn" : "ok";
  const concurrency = tier === "paid" ? Math.min(chunks, 5) : 1;

  byId("chunks").textContent = chunks;
  byId("remaining").textContent = tier === "paid" ? "—" : remaining;
  byId("concurrency").textContent = concurrency;

  const badge = byId("status-badge");
  badge.className = `status-badge ${severity}`;
  badge.textContent = severity === "ok" ? "Ready" : severity === "warn" ? "Review quota" : "Stop before call";

  const message = severity === "blocked"
    ? `${chunks} chunk${chunks === 1 ? "" : "s"} need${chunks === 1 ? "s" : ""} ${chunks} model request${chunks === 1 ? "" : "s"}, but the entered local free-key guard has ${remaining} slot${remaining === 1 ? "" : "s"} remaining. The terminal app holds before provider work.`
    : severity === "warn"
      ? `${chunks} bounded chunks fit inside the entered local guard. Free-key work remains sequential and provider capacity is still not observable here.`
      : tier === "paid"
        ? `${chunks} bounded chunks can use up to ${concurrency} concurrent transcription workers after upload.`
        : "A single bounded chunk can proceed without a long-audio quota warning.";
  byId("result-message").textContent = message;

  const stageValues = severity === "blocked" ? [100, 0, 0] : [100, 100, 100];
  document.querySelectorAll(".stage").forEach((stage, index) => {
    stage.querySelector("i").style.setProperty("--w", `${stageValues[index]}%`);
    stage.querySelector("output").textContent = severity === "blocked" && index > 0 ? "held" : "ready";
  });
}

document.querySelectorAll("[data-tier]").forEach((button) => button.addEventListener("click", () => setTier(button.dataset.tier)));
document.querySelectorAll("[data-format]").forEach((button) => button.addEventListener("click", () => {
  format = button.dataset.format;
  document.querySelectorAll("[data-format]").forEach((item) => {
    const active = item.dataset.format === format;
    item.classList.toggle("active", active);
    item.setAttribute("aria-pressed", String(active));
  });
  renderTranscript();
}));
byId("run-preflight").addEventListener("click", runPreflight);
byId("speaker-one").addEventListener("input", renderTranscript);
byId("speaker-two").addEventListener("input", renderTranscript);

runPreflight();
renderTranscript();
