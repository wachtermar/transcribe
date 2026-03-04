#!/usr/bin/env python3
# /// script
# requires-python = ">=3.10"
# dependencies = [
#     "google-genai>=1.65.0",
#     "textual>=3.0.0",
# ]
# ///
"""
transcribe — Terminal GUI for audio transcription with speaker detection.

Usage:
  uv run transcribe.py              # interactive GUI
  uv run transcribe.py recording.mp3
"""

import argparse
import json
import math
import os
import random
import re
import shutil
import subprocess
import sys
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

# ── Dependency Check ──────────────────────────────────────────────────

_need = []
try:
    from google import genai
    from google.genai import types
except ImportError:
    _need.append("google-genai")
try:
    from textual import work
    from textual.app import App, ComposeResult
    from textual.binding import Binding
    from textual.containers import Container, Horizontal, VerticalScroll
    from textual.screen import ModalScreen
    from textual.widgets import (
        Button, Footer, Header, Input, Label,
        ProgressBar, RadioButton, RadioSet, Static, TextArea,
    )
except ImportError:
    _need.append("textual")
if _need:
    print(f"Missing: {', '.join(_need)}")
    print("Run:  uv run transcribe.py  (auto-installs dependencies)")
    sys.exit(1)

for _bin in ("ffmpeg", "ffprobe"):
    if not shutil.which(_bin):
        print(f"\n  {_bin} is required but not found.\n")
        if sys.platform == "darwin":
            print("  Install:  brew install ffmpeg")
        elif sys.platform == "win32":
            print("  Install:  winget install ffmpeg")
            print("       or:  choco install ffmpeg")
        else:
            print("  Install:  sudo apt install ffmpeg")
        print()
        sys.exit(1)

# ── Config ────────────────────────────────────────────────────────────

VERSION = "0.4.0"
MAX_CHUNK_S = 10 * 60

MODELS = [
    ("gemini-3-flash-preview", "Gemini 3 Flash  (recommended)"),
    ("gemini-2.5-flash-preview-04-17", "Gemini 2.5 Flash  (stable)"),
]

# Free-tier rate limits (from actual API error responses)
FREE_TIER_RPD = 20  # requests per day per model, confirmed from quota errors

# ── Local request counter (session-level quota tracking) ─────────────
_requests_used = 0


def get_requests_used():
    return _requests_used


def remaining_quota():
    return max(0, FREE_TIER_RPD - _requests_used)


class DailyQuotaExhausted(Exception):
    """Raised when the free-tier daily quota is exhausted."""
    pass


def estimate_rate_limit_impact(duration_s):
    """Estimate whether free-tier rate limits will cause problems.

    Returns (num_chunks, severity) where severity is:
      "ok"       — single chunk, should work fine
      "warn"     — multiple chunks, warn about free-tier limits
      "blocked"  — exceeds remaining daily quota, will fail with free key
    """
    if duration_s <= MAX_CHUNK_S:
        return 1, "ok"

    num_chunks = math.ceil(duration_s / MAX_CHUNK_S)
    left = remaining_quota()

    if num_chunks > left:
        return num_chunks, "blocked"
    else:
        # Any multi-chunk file risks hitting the 20 RPD free-tier limit
        return num_chunks, "warn"


# ── Audio helpers ────────────────────────────────────────────────────


def probe_audio(path):
    """Return duration in seconds via ffprobe."""
    r = subprocess.run(
        [
            "ffprobe", "-v", "quiet",
            "-print_format", "json",
            "-show_format", str(path),
        ],
        capture_output=True, text=True,
    )
    if r.returncode != 0:
        return None
    info = json.loads(r.stdout).get("format", {})
    return float(info.get("duration", 0))


def split_audio(path, chunk_s, outdir, on_progress=None):
    """Split audio into chunks. Calls on_progress(current, total) if provided."""
    duration = probe_audio(path)
    if duration is None or duration <= 0:
        return None, 0
    if duration <= chunk_s:
        return None, duration

    total_parts = math.ceil(duration / chunk_s)
    parts = []
    start = 0.0
    idx = 0
    while start < duration:
        out = os.path.join(outdir, f"part_{idx:03d}.mp3")
        subprocess.run(
            [
                "ffmpeg", "-y", "-v", "quiet",
                "-i", str(path),
                "-ss", str(start),
                "-t", str(chunk_s),
                "-acodec", "libmp3lame", "-b:a", "128k",
                out,
            ],
            capture_output=True,
        )
        parts.append(out)
        start += chunk_s
        idx += 1
        if on_progress:
            on_progress(idx, total_parts)

    return parts, duration


def play_audio_clip(filepath, start_seconds, duration=8):
    """Extract a clip and play it. Returns (Popen process, tmp_path)."""
    tmp = tempfile.NamedTemporaryFile(suffix=".wav", delete=False)
    tmp.close()
    subprocess.run(
        [
            "ffmpeg", "-y", "-v", "quiet",
            "-i", str(filepath),
            "-ss", str(start_seconds),
            "-t", str(duration),
            tmp.name,
        ],
        capture_output=True,
    )

    if sys.platform == "darwin":
        players = [["afplay", tmp.name]]
    elif sys.platform == "win32":
        players = [
            ["powershell", "-c",
             f"(New-Object Media.SoundPlayer '{tmp.name}').PlaySync()"],
        ]
    else:
        players = []
    players.append(["ffplay", "-nodisp", "-autoexit", tmp.name])

    for cmd in players:
        if shutil.which(cmd[0]):
            proc = subprocess.Popen(
                cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
            return proc, tmp.name
    return None, tmp.name


def open_file_dialog():
    """Open a native file picker dialog. Returns selected path or None."""
    if sys.platform == "darwin":
        r = subprocess.run(
            ["osascript", "-e",
             'POSIX path of (choose file with prompt "Select audio file"'
             ' of type {"public.audio"})'],
            capture_output=True, text=True,
        )
        if r.returncode == 0 and r.stdout.strip():
            return r.stdout.strip()
    elif sys.platform == "win32":
        ps = (
            "Add-Type -AssemblyName System.Windows.Forms;"
            "$d = New-Object System.Windows.Forms.OpenFileDialog;"
            "$d.Title = 'Select audio file';"
            "$d.Filter = 'Audio files|*.mp3;*.wav;*.m4a;*.aac;*.ogg;*.flac;"
            "*.wma;*.opus|All files|*.*';"
            "if ($d.ShowDialog() -eq 'OK') { $d.FileName }"
        )
        r = subprocess.run(
            ["powershell", "-NoProfile", "-Command", ps],
            capture_output=True, text=True,
        )
        if r.returncode == 0 and r.stdout.strip():
            return r.stdout.strip()
    else:
        for cmd in [
            ["zenity", "--file-selection", "--title=Select audio file",
             "--file-filter=Audio files | *.mp3 *.wav *.m4a *.aac *.ogg *.flac *.wma *.opus"],
            ["kdialog", "--getopenfilename", ".",
             "Audio files (*.mp3 *.wav *.m4a *.aac *.ogg *.flac *.wma *.opus)"],
        ]:
            if shutil.which(cmd[0]):
                r = subprocess.run(cmd, capture_output=True, text=True)
                if r.returncode == 0 and r.stdout.strip():
                    return r.stdout.strip()
                break
    return None


# ── Timestamp helpers ────────────────────────────────────────────────


def parse_ts(ts_str):
    ts_str = ts_str.strip("[]")
    parts = ts_str.split(":")
    if len(parts) == 2:
        return int(parts[0]) * 60 + int(parts[1])
    elif len(parts) == 3:
        return int(parts[0]) * 3600 + int(parts[1]) * 60 + int(parts[2])
    return 0


def fmt_ts(seconds):
    seconds = max(0, int(seconds))
    if seconds >= 3600:
        h = seconds // 3600
        m = (seconds % 3600) // 60
        s = seconds % 60
        return f"[{h}:{m:02d}:{s:02d}]"
    return f"[{seconds // 60:02d}:{seconds % 60:02d}]"


def fmt_srt_ts(seconds):
    seconds = max(0, int(seconds))
    h = seconds // 3600
    m = (seconds % 3600) // 60
    s = seconds % 60
    return f"{h:02d}:{m:02d}:{s:02d},000"


def offset_timestamps(text, offset_seconds):
    if offset_seconds == 0:
        return text

    def _replace(m):
        secs = parse_ts(m.group(0)) + offset_seconds
        return fmt_ts(secs)

    return re.sub(r"\[\d{1,2}:\d{2}(?::\d{2})?\]", _replace, text)


def strip_timestamps(text):
    return re.sub(r"\[\d{1,2}:\d{2}(?::\d{2})?\]\s*", "", text)


def get_speaker_timestamp(text, speaker):
    pattern = rf"\[(\d{{1,2}}:\d{{2}}(?::\d{{2}})?)\]\s*{re.escape(speaker)}:"
    m = re.search(pattern, text)
    if m:
        return parse_ts(m.group(1))
    return None


def transcript_to_srt(text):
    lines = text.strip().split("\n")
    entries = []
    for line in lines:
        line = line.strip()
        if not line:
            continue
        m = re.match(r"\[(\d{1,2}:\d{2}(?::\d{2})?)\]\s*(.*)", line)
        if m:
            ts_seconds = parse_ts(m.group(1))
            content = m.group(2).strip()
            if content:
                entries.append((ts_seconds, content))
    if not entries:
        return text
    srt_parts = []
    for i, (start, content) in enumerate(entries):
        if i + 1 < len(entries):
            end = entries[i + 1][0]
        else:
            words = len(content.split())
            end = start + max(3, min(words // 2, 10))
        srt_parts.append(str(i + 1))
        srt_parts.append(f"{fmt_srt_ts(start)} --> {fmt_srt_ts(end)}")
        srt_parts.append(content)
        srt_parts.append("")
    return "\n".join(srt_parts)


# ── Secure key storage ───────────────────────────────────────────────

KEYCHAIN_SERVICE = "transcribe-cli"
KEYCHAIN_ACCOUNT = "gemini-api-key"


def _key_file_path():
    if sys.platform == "win32":
        base = Path(os.environ.get("APPDATA", str(Path.home())))
    else:
        base = Path.home() / ".config"
    return base / "transcribe" / "key"


def _key_store_name():
    if sys.platform == "darwin":
        return "Keychain"
    elif sys.platform == "win32":
        return "AppData"
    return "config"


def save_api_key(key):
    if sys.platform == "darwin":
        subprocess.run(
            ["security", "delete-generic-password",
             "-s", KEYCHAIN_SERVICE, "-a", KEYCHAIN_ACCOUNT],
            capture_output=True,
        )
        r = subprocess.run(
            ["security", "add-generic-password",
             "-s", KEYCHAIN_SERVICE, "-a", KEYCHAIN_ACCOUNT,
             "-w", key, "-U"],
            capture_output=True, text=True,
        )
        return r.returncode == 0
    else:
        kf = _key_file_path()
        kf.parent.mkdir(parents=True, exist_ok=True)
        kf.write_text(key, encoding="utf-8")
        if sys.platform != "win32":
            kf.chmod(0o600)
        return True


def load_api_key():
    if sys.platform == "darwin":
        r = subprocess.run(
            ["security", "find-generic-password",
             "-s", KEYCHAIN_SERVICE, "-a", KEYCHAIN_ACCOUNT, "-w"],
            capture_output=True, text=True,
        )
        if r.returncode == 0 and r.stdout.strip():
            return r.stdout.strip()
    else:
        kf = _key_file_path()
        if kf.exists():
            return kf.read_text(encoding="utf-8").strip()
    return None


def delete_api_key():
    if sys.platform == "darwin":
        subprocess.run(
            ["security", "delete-generic-password",
             "-s", KEYCHAIN_SERVICE, "-a", KEYCHAIN_ACCOUNT],
            capture_output=True,
        )
    else:
        kf = _key_file_path()
        if kf.exists():
            kf.unlink()


# ── Config persistence ───────────────────────────────────────────────

def _config_path():
    if sys.platform == "win32":
        base = Path(os.environ.get("APPDATA", str(Path.home())))
    else:
        base = Path.home() / ".config"
    return base / "transcribe" / "config.json"


def load_config():
    p = _config_path()
    if p.exists():
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {}


def save_config(cfg):
    p = _config_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(cfg), encoding="utf-8")


# ── Transcription engine ─────────────────────────────────────────────


def upload_and_wait(client, filepath):
    f = client.files.upload(file=filepath)
    for _ in range(180):
        state = str(getattr(f.state, "name", f.state) or "")
        if state != "PROCESSING":
            break
        time.sleep(1)
        f = client.files.get(name=f.name)
    return f


def _parse_retry_delay(error_str):
    m = re.search(r"retryDelay['\"]?\s*:\s*['\"]?(\d+(?:\.\d+)?)\s*s", error_str)
    if m:
        return float(m.group(1))
    m = re.search(r"retry in (\d+(?:\.\d+)?)\s*s", error_str, re.IGNORECASE)
    if m:
        return float(m.group(1))
    return None


def _is_daily_quota_error(error_str):
    """Check if the error is a daily quota exhaustion (not retryable)."""
    s = error_str.lower()
    return "perday" in s or "per_day" in s or "freetier" in s or "free_tier" in s


def gemini_transcribe(client, model, uploaded_file, part_ctx="", on_retry=None):
    prompt = f"""{part_ctx}Transcribe this audio precisely with speaker diarization.

Rules:
- Start every line with a timestamp in [MM:SS] format relative to the start of this audio
- Label each distinct speaker consistently: Speaker 1, Speaker 2, etc.
- Format every line as: [MM:SS] Speaker N: [spoken text]
- New line on each speaker change or after a significant pause
- Include ALL speech verbatim — do not summarize, skip, or paraphrase
- Preserve filler words (um, uh, like, you know, etc.)
- Note significant pauses: [pause] or [long pause]
- Note non-speech sounds in brackets: [laughter], [applause], [music], [noise], [cough], [crosstalk]
- Preserve the original language of the audio"""

    max_retries = 10
    for attempt in range(max_retries):
        try:
            resp = client.models.generate_content(
                model=model,
                contents=[prompt, uploaded_file],
                config=types.GenerateContentConfig(temperature=1.0),
            )
            global _requests_used
            _requests_used += 1
            return resp.text
        except Exception as e:
            err = str(e)
            err_lower = err.lower()
            retryable = (
                "429" in err_lower or "503" in err_lower or "500" in err_lower
                or "resource_exhausted" in err_lower or "unavailable" in err_lower
                or "high demand" in err_lower or "internal" in err_lower
                or "rate" in err_lower
            )

            # Daily quota exhaustion — retrying is pointless
            if retryable and _is_daily_quota_error(err):
                raise DailyQuotaExhausted(
                    "Free-tier daily quota exhausted. "
                    "Upgrade to a paid API key at ai.google.dev, "
                    "or wait until the quota resets (midnight Pacific Time)."
                ) from e

            if retryable and attempt < max_retries - 1:
                api_delay = _parse_retry_delay(err)
                if api_delay:
                    wait = api_delay + random.uniform(1, 5)
                else:
                    wait = (2 ** attempt) * 5
                wait = min(wait, 120)
                is_rate_limit = "429" in err_lower or "resource_exhausted" in err_lower or "rate" in err_lower
                if on_retry:
                    on_retry(attempt + 1, max_retries, wait, is_rate_limit)
                time.sleep(wait)
                continue
            raise


def strip_fences(text):
    text = text.strip()
    text = re.sub(r"^```[^\n]*\n?", "", text)
    text = re.sub(r"\n?```\s*$", "", text)
    return text.strip()


def find_speakers(text):
    return sorted(
        set(re.findall(r"(Speaker \d+):", text)),
        key=lambda s: int(re.search(r"\d+", s).group()),
    )


def do_transcribe_single(client, model, filepath, on_status=None, on_retry=None):
    if on_status:
        on_status("Uploading...")
    f = upload_and_wait(client, filepath)
    try:
        if on_status:
            on_status("Transcribing...")
        text = gemini_transcribe(client, model, f, on_retry=on_retry)
    finally:
        try:
            client.files.delete(name=f.name)
        except Exception:
            pass
    return strip_fences(text)


MAX_CONCURRENT_TRANSCRIBE = 5


def do_transcribe_chunked(client, model, chunk_paths, on_upload=None, on_transcribe=None, on_retry=None, sequential=False):
    n = len(chunk_paths)

    # Phase 1: Upload all files (parallel is fine — uploads don't count against generate quota)
    uploaded = [None] * n
    upload_errors = []
    upload_done = [0]

    with ThreadPoolExecutor(max_workers=n) as pool:
        futs = {
            pool.submit(upload_and_wait, client, p): i
            for i, p in enumerate(chunk_paths)
        }
        for fut in as_completed(futs):
            idx = futs[fut]
            try:
                uploaded[idx] = fut.result()
            except Exception as e:
                upload_errors.append((idx, str(e)))
            upload_done[0] += 1
            if on_upload:
                on_upload(upload_done[0], n)

    ready = [(i, f) for i, f in enumerate(uploaded) if f is not None]

    # Phase 2: Transcribe — concurrent for paid keys, sequential for free tier
    results = []
    errors = []
    quota_exhausted = False
    done_count = [0]

    def _transcribe_one(idx, ufile):
        ctx = f"(Segment {idx + 1} of {n} of a longer recording.) "
        text = gemini_transcribe(client, model, ufile, ctx, on_retry=on_retry)
        return idx, text

    if sequential:
        # Sequential mode for free-tier keys
        for step, (idx, ufile) in enumerate(ready):
            if quota_exhausted:
                errors.append((idx, "Skipped — daily quota exhausted"))
                done_count[0] += 1
                if on_transcribe:
                    on_transcribe(done_count[0], len(ready))
                continue

            try:
                _, text = _transcribe_one(idx, ufile)
                results.append((idx, text))
            except DailyQuotaExhausted as e:
                quota_exhausted = True
                errors.append((idx, str(e)))
            except Exception as e:
                errors.append((idx, str(e)))

            done_count[0] += 1
            if on_transcribe:
                on_transcribe(done_count[0], len(ready))
    else:
        # Concurrent mode for paid keys
        with ThreadPoolExecutor(max_workers=MAX_CONCURRENT_TRANSCRIBE) as pool:
            futs = {
                pool.submit(_transcribe_one, idx, ufile): idx
                for idx, ufile in ready
            }
            for fut in as_completed(futs):
                idx = futs[fut]
                try:
                    _, text = fut.result()
                    results.append((idx, text))
                except DailyQuotaExhausted as e:
                    quota_exhausted = True
                    errors.append((idx, str(e)))
                except Exception as e:
                    errors.append((idx, str(e)))

                done_count[0] += 1
                if on_transcribe:
                    on_transcribe(done_count[0], len(ready))

    # Cleanup
    for f in uploaded:
        if f is not None:
            try:
                client.files.delete(name=f.name)
            except Exception:
                pass

    # Merge only successful results (don't embed errors in transcript)
    results.sort()
    merged_parts = []
    for idx, text in results:
        chunk_text = strip_fences(text)
        chunk_text = offset_timestamps(chunk_text, idx * MAX_CHUNK_S)
        merged_parts.append(chunk_text)

    return "\n\n".join(merged_parts), errors, quota_exhausted


# ══════════════════════════════════════════════════════════════════════
#  Textual GUI
# ══════════════════════════════════════════════════════════════════════

APP_CSS = """
Screen {
    background: $surface;
}

/* ── State containers ────────────────────────────────────────────── */

#setup, #processing, #speakers, #result {
    height: 1fr;
    padding: 1 4;
}

.section-label {
    text-style: bold;
    color: $accent;
    margin: 1 0 0 0;
}

.hint {
    color: $text-muted;
    margin: 0 0 1 0;
}

/* ── Setup ───────────────────────────────────────────────────────── */

#setup-toolbar {
    height: auto;
    align: right middle;
    margin: 0 0 0 0;
}

#settings-btn {
    min-width: 14;
}

.file-row {
    height: auto;
}

#file-input {
    width: 1fr;
    margin: 0 0 0 0;
}

#browse-btn {
    min-width: 12;
    margin: 0 0 0 1;
}

#file-info {
    color: $text-muted;
    height: 1;
    margin: 0 0 1 0;
}

#rate-limit-warning {
    color: $warning;
    margin: 0 0 1 0;
    padding: 1 2;
    border: solid $warning 50%;
    display: none;
}

#transcribe-btn {
    margin: 1 0;
    width: 100%;
}

/* ── Processing ──────────────────────────────────────────────────── */

.progress-row {
    height: auto;
    margin: 0 0 1 0;
}

.progress-label {
    margin: 0 0 0 0;
}

/* ── Speakers ────────────────────────────────────────────────────── */

.speaker-row {
    height: auto;
    margin: 0 0 1 0;
    padding: 1;
    border: solid $accent 30%;
}

.speaker-sample {
    color: $text-muted;
    margin: 0 0 0 0;
}

.speaker-controls {
    layout: horizontal;
    height: auto;
    margin: 1 0 0 0;
}

.speaker-input {
    width: 1fr;
    margin: 0 1 0 0;
}

.play-btn {
    min-width: 10;
}

#speakers-continue {
    margin: 1 0;
    width: 100%;
}

/* ── Result ──────────────────────────────────────────────────────── */

#format-select {
    margin: 0 0 1 0;
}

#transcript-view {
    height: 1fr;
    min-height: 10;
    margin: 0 0 1 0;
}

.save-row {
    layout: horizontal;
    height: auto;
    margin: 0 0 1 0;
}

.save-input {
    width: 1fr;
    margin: 0 1 0 0;
}

.save-btn {
    min-width: 12;
}

#summary {
    text-align: center;
    color: $success;
    margin: 1 0;
    text-style: bold;
}

#new-btn {
    margin: 1 0;
    width: 100%;
}

/* ── Settings modal ──────────────────────────────────────────────── */

SettingsScreen {
    align: center middle;
}

#settings-dialog {
    width: 70;
    height: auto;
    max-height: 28;
    border: thick $accent;
    padding: 1 2;
    background: $surface;
}

#settings-dialog .section-label {
    margin: 0 0 1 0;
}

#key-status {
    margin: 0 0 1 0;
}

#key-input {
    margin: 0 0 1 0;
}

.settings-buttons {
    layout: horizontal;
    height: auto;
}

.settings-buttons Button {
    margin: 0 1 0 0;
}
"""


# ── Settings Modal ───────────────────────────────────────────────────


class SettingsScreen(ModalScreen):
    """Modal dialog for API key and plan management."""

    BINDINGS = [Binding("escape", "close", "Close")]

    def __init__(self, current_key: str = "", paid_key: bool = False):
        super().__init__()
        self._current_key = current_key
        self._paid_key = paid_key

    def compose(self) -> ComposeResult:
        with Container(id="settings-dialog"):
            yield Static("Settings", classes="section-label")
            if self._current_key:
                masked = self._current_key[:8] + "..." + self._current_key[-4:]
                yield Static(
                    f"  API Key:  saved  ({masked})",
                    id="key-status",
                )
            else:
                yield Static(
                    "  API Key:  not set",
                    id="key-status",
                )
            yield Input(
                placeholder="Paste new API key...",
                password=True,
                id="key-input",
            )
            with RadioSet(id="key-tier"):
                yield RadioButton("Free API key", value=not self._paid_key)
                yield RadioButton("Paid API key  (faster, no daily limit)", value=self._paid_key)
            with Horizontal(classes="settings-buttons"):
                yield Button("Save", id="save-key", variant="primary")
                yield Button("Delete", id="delete-key", variant="error")
                yield Button("Close", id="close-settings")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        tier_index = self.query_one("#key-tier", RadioSet).pressed_index
        is_paid = tier_index == 1

        if event.button.id == "save-key":
            new_key = self.query_one("#key-input", Input).value.strip()
            if new_key:
                if save_api_key(new_key):
                    cfg = load_config()
                    cfg["paid_key"] = is_paid
                    save_config(cfg)
                    self.app.notify(f"API key saved to {_key_store_name()}")
                    self.dismiss({"key": new_key, "paid": is_paid})
                else:
                    self.app.notify("Could not save key", severity="error")
            else:
                # No new key — just save the tier preference
                cfg = load_config()
                cfg["paid_key"] = is_paid
                save_config(cfg)
                self.app.notify("Settings saved")
                self.dismiss({"key": None, "paid": is_paid})
        elif event.button.id == "delete-key":
            delete_api_key()
            cfg = load_config()
            cfg["paid_key"] = False
            save_config(cfg)
            self.app.notify("API key deleted")
            self.dismiss({"key": "", "paid": False})
        elif event.button.id == "close-settings":
            self.dismiss(None)

    def action_close(self) -> None:
        self.dismiss(None)


# ── Main App ─────────────────────────────────────────────────────────


class TranscribeApp(App):
    TITLE = "TRANSCRIBE"
    SUB_TITLE = "audio to text · speaker detection · gemini"
    CSS = APP_CSS

    BINDINGS = [
        Binding("ctrl+k", "settings", "Settings"),
        Binding("ctrl+q", "quit", "Quit"),
    ]

    def __init__(self, initial_file=None, initial_key=None):
        super().__init__()
        self._initial_file = initial_file
        self._initial_key = initial_key
        self.api_key = ""
        self.paid_key = False
        self.audio_path = ""
        self.audio_duration = 0.0
        self.selected_model = MODELS[0][0]
        self.raw_transcript = ""
        self.speaker_list: list[str] = []
        self.active_player = None
        self.selected_format = 0  # 0=text, 1=timestamps, 2=srt, 3=all

    def compose(self) -> ComposeResult:
        yield Header(show_clock=False)

        # ── State 1: Setup ───────────────────────────────────────────
        with VerticalScroll(id="setup"):
            with Container(id="setup-toolbar"):
                yield Button("Settings", id="settings-btn")
            yield Static("Model", classes="section-label")
            with RadioSet(id="model-select"):
                for i, (_, label) in enumerate(MODELS):
                    yield RadioButton(label, value=(i == 0))
            yield Static("Audio File", classes="section-label")
            with Horizontal(classes="file-row"):
                yield Input(
                    placeholder="Enter path or drag & drop a file...",
                    id="file-input",
                )
                yield Button("Browse", id="browse-btn")
            yield Static("", id="file-info")
            yield Static("", id="rate-limit-warning")
            yield Button(
                "Transcribe",
                id="transcribe-btn",
                variant="primary",
                disabled=True,
            )

        # ── State 2: Processing ──────────────────────────────────────
        with VerticalScroll(id="processing"):
            yield Static("Processing...", id="status-label", classes="section-label")
            with Container(classes="progress-row"):
                yield Static("", id="split-label", classes="progress-label")
                yield ProgressBar(id="split-progress", total=100)
            with Container(classes="progress-row"):
                yield Static("", id="upload-label", classes="progress-label")
                yield ProgressBar(id="upload-progress", total=100)
            with Container(classes="progress-row"):
                yield Static("", id="transcribe-label", classes="progress-label")
                yield ProgressBar(id="transcribe-progress", total=100)

        # ── State 3: Speakers ────────────────────────────────────────
        with VerticalScroll(id="speakers"):
            yield Static("Assign Speaker Names", classes="section-label")
            yield Static(
                "Edit names below, then click Continue. Click Play to hear a sample.",
                classes="hint",
            )
            yield Container(id="speaker-list")
            yield Button("Continue", id="speakers-continue", variant="primary")

        # ── State 4: Result ──────────────────────────────────────────
        with VerticalScroll(id="result"):
            yield Static("Output Format", classes="section-label")
            with RadioSet(id="format-select"):
                yield RadioButton("Plain text", value=True)
                yield RadioButton("Text + timestamps")
                yield RadioButton("SRT subtitles")
                yield RadioButton("All formats")
            yield Static("Transcript", classes="section-label")
            yield TextArea(id="transcript-view", read_only=True)
            yield Static("Save", classes="section-label")
            with Horizontal(classes="save-row", id="save-text-row"):
                yield Input(id="save-path", placeholder="Save path...", classes="save-input")
                yield Button("Save", id="save-btn", variant="primary", classes="save-btn")
            with Horizontal(classes="save-row", id="save-srt-row"):
                yield Input(id="save-srt-path", placeholder="SRT path...", classes="save-input")
                yield Button("Save SRT", id="save-srt-btn", variant="primary", classes="save-btn")
            yield Static("", id="summary")
            yield Button("New Transcription", id="new-btn")

        yield Footer()

    def on_mount(self) -> None:
        # Load config
        cfg = load_config()
        self.paid_key = cfg.get("paid_key", False)

        # Load API key
        if self._initial_key:
            self.api_key = self._initial_key
        else:
            self.api_key = (
                load_api_key()
                or os.environ.get("GEMINI_API_KEY", "")
                or os.environ.get("GOOGLE_API_KEY", "")
            )

        # Show only setup
        self._switch_to("setup")

        # Pre-fill file if provided
        if self._initial_file:
            self.query_one("#file-input", Input).value = self._initial_file

        # If no key, open settings
        if not self.api_key:
            self.set_timer(0.3, self.action_settings)

    def _switch_to(self, state_id: str) -> None:
        """Show one state container, hide the rest."""
        for sid in ("setup", "processing", "speakers", "result"):
            self.query_one(f"#{sid}").display = (sid == state_id)

    # ── Event Handlers ───────────────────────────────────────────────

    def on_input_changed(self, event: Input.Changed) -> None:
        if event.input.id == "file-input":
            self._validate_file(event.value)

    def on_button_pressed(self, event: Button.Pressed) -> None:
        btn_id = event.button.id or ""

        if btn_id == "settings-btn":
            self.action_settings()
            return

        if btn_id == "browse-btn":
            self._browse_file()
            return

        if btn_id == "transcribe-btn":
            if not self.api_key:
                self.notify("No API key set — click Settings to add one.", severity="error")
                return
            if not self.audio_path:
                self.notify("Select an audio file first.", severity="warning")
                return
            self._start_transcription()

        elif btn_id == "speakers-continue":
            self._finish_speakers()

        elif btn_id == "save-btn":
            self._save_text()

        elif btn_id == "save-srt-btn":
            self._save_srt()

        elif btn_id == "new-btn":
            self._reset()

        elif btn_id.startswith("play-"):
            try:
                idx = int(btn_id.split("-")[1])
                if idx < len(self.speaker_list):
                    self._play_speaker(self.speaker_list[idx])
            except (ValueError, IndexError):
                pass

    def on_radio_set_changed(self, event: RadioSet.Changed) -> None:
        if event.radio_set.id == "model-select":
            if event.index < len(MODELS):
                self.selected_model = MODELS[event.index][0]
                self._update_rate_limit_warning()
        elif event.radio_set.id == "format-select":
            self.selected_format = event.index
            self._update_transcript_view()

    # ── Actions ──────────────────────────────────────────────────────

    def action_settings(self) -> None:
        self.push_screen(
            SettingsScreen(self.api_key, paid_key=self.paid_key),
            callback=self._on_settings_result,
        )

    def _on_settings_result(self, result) -> None:
        if result is not None:
            if result["key"] is not None:
                self.api_key = result["key"]
            self.paid_key = result["paid"]
            self._update_rate_limit_warning()

    # ── File Browse ──────────────────────────────────────────────────

    @work(thread=True, exclusive=True, group="browse")
    def _browse_file(self) -> None:
        path = open_file_dialog()
        if path:
            self.call_from_thread(self._set_file_path, path)

    def _set_file_path(self, path: str) -> None:
        self.query_one("#file-input", Input).value = path

    # ── File Validation ──────────────────────────────────────────────

    def _validate_file(self, raw: str) -> None:
        path = raw.strip().strip("'\"")
        p = Path(path)
        if p.exists() and p.is_file():
            duration = probe_audio(str(p))
            if duration and duration > 0:
                self.audio_path = str(p.resolve())
                self.audio_duration = duration
                size_mb = p.stat().st_size / (1024 * 1024)
                m, s = int(duration // 60), int(duration % 60)
                self.query_one("#file-info", Static).update(
                    f"  {m}m {s:02d}s  ·  {size_mb:.1f} MB"
                )
                self.query_one("#transcribe-btn", Button).disabled = False
                self._update_rate_limit_warning()
                return
        self.audio_path = ""
        self.audio_duration = 0.0
        self.query_one("#file-info", Static).update("")
        self.query_one("#transcribe-btn", Button).disabled = True
        self._hide_rate_limit_warning()

    def _update_rate_limit_warning(self) -> None:
        """Show or hide the rate limit warning based on file size and key tier."""
        warning = self.query_one("#rate-limit-warning", Static)
        if not self.audio_duration or self.paid_key:
            self._hide_rate_limit_warning()
            return

        num_chunks, severity = estimate_rate_limit_impact(self.audio_duration)

        used = get_requests_used()
        left = remaining_quota()

        if severity == "blocked":
            warning.update(
                f"  ! This file needs {num_chunks} chunks but only {left}/{FREE_TIER_RPD}\n"
                f"    free-tier requests remain today ({used} used this session).\n"
                f"    Transcription will be incomplete. Use a paid key: ai.google.dev"
            )
            warning.display = True
        elif severity == "warn":
            warning.update(
                f"  ! This file needs {num_chunks} chunks ({left}/{FREE_TIER_RPD}\n"
                f"    free-tier requests remain, {used} used this session).\n"
                f"    May fail or be incomplete. A paid key is recommended."
            )
            warning.display = True
        else:
            self._hide_rate_limit_warning()

    def _hide_rate_limit_warning(self) -> None:
        warning = self.query_one("#rate-limit-warning", Static)
        warning.update("")
        warning.display = False

    # ── Transcription ────────────────────────────────────────────────

    def _start_transcription(self) -> None:
        self._switch_to("processing")
        self.query_one("#status-label", Static).update("Starting...")
        for pid in ("split", "upload", "transcribe"):
            self.query_one(f"#{pid}-progress", ProgressBar).update(progress=0, total=100)
            self.query_one(f"#{pid}-label", Static).update("")
        self._run_transcription()

    @work(thread=True, exclusive=True)
    def _run_transcription(self) -> None:
        try:
            client = genai.Client(api_key=self.api_key)
            filepath = self.audio_path
            model = self.selected_model

            with tempfile.TemporaryDirectory() as tmpdir:
                # Split
                def on_split(current, total):
                    self.call_from_thread(
                        self._set_progress, "split", current, total, "Splitting audio"
                    )

                self.call_from_thread(self._set_status, "Analyzing audio...")
                chunks, duration = split_audio(filepath, MAX_CHUNK_S, tmpdir, on_progress=on_split)

                if chunks is None:
                    # Single file — no splitting needed
                    self.call_from_thread(self._set_progress, "split", 1, 1, "No splitting needed")

                    def on_status(msg):
                        self.call_from_thread(self._set_status, msg)

                    def on_retry(attempt, max_retries, wait, is_rate_limit):
                        if is_rate_limit:
                            msg = f"Rate limited — waiting {int(wait)}s (retry {attempt}/{max_retries}). Free API keys have low limits."
                        else:
                            msg = f"API error — retrying in {int(wait)}s ({attempt}/{max_retries})"
                        self.call_from_thread(self._set_status, msg)

                    self.call_from_thread(self._set_progress, "upload", 0, 1, "Uploading")
                    self.call_from_thread(self._set_progress, "transcribe", 0, 1, "Transcribing")

                    transcript = do_transcribe_single(
                        client, model, filepath, on_status=on_status, on_retry=on_retry
                    )

                    self.call_from_thread(self._set_progress, "upload", 1, 1, "Uploaded")
                    self.call_from_thread(self._set_progress, "transcribe", 1, 1, "Transcribed")
                else:
                    # Chunked
                    n = len(chunks)
                    self.call_from_thread(
                        self._set_progress, "split", n, n, f"Split into {n} parts"
                    )

                    def on_upload(current, total):
                        self.call_from_thread(
                            self._set_progress, "upload", current, total, "Uploading"
                        )

                    def on_transcribe(current, total):
                        used = get_requests_used()
                        self.call_from_thread(
                            self._set_progress, "transcribe", current, total,
                            f"Transcribing  [{used}/{FREE_TIER_RPD} daily requests used]"
                        )

                    def on_retry(attempt, max_retries, wait, is_rate_limit):
                        if is_rate_limit:
                            msg = f"Rate limited — waiting {int(wait)}s (retry {attempt}/{max_retries}). Free API keys have low limits."
                        else:
                            msg = f"API error — retrying in {int(wait)}s ({attempt}/{max_retries})"
                        self.call_from_thread(self._set_status, msg)

                    transcript, errors, quota_hit = do_transcribe_chunked(
                        client, model, chunks,
                        on_upload=on_upload,
                        on_transcribe=on_transcribe,
                        on_retry=on_retry,
                        sequential=not self.paid_key,
                    )

                    if quota_hit:
                        ok_count = n - len(errors)
                        used = get_requests_used()
                        self.call_from_thread(
                            self.notify,
                            f"Daily quota exhausted ({used}/{FREE_TIER_RPD} used) — "
                            f"only {ok_count}/{n} parts transcribed. "
                            f"Use a paid API key for longer files.",
                            severity="error",
                            timeout=15,
                        )
                    elif errors:
                        for idx, err in errors:
                            self.call_from_thread(
                                self.notify,
                                f"Part {idx + 1} failed: {err[:120]}",
                                severity="error",
                                timeout=10,
                            )

            self.raw_transcript = transcript

            if not transcript.strip():
                self.call_from_thread(
                    self._on_error,
                    "All parts failed. Your free API key's daily quota "
                    "is likely exhausted. Use a paid key or try again tomorrow.",
                )
                return

            self.speaker_list = find_speakers(transcript)

            if errors:
                failed = [idx + 1 for idx, _ in errors]
                self.call_from_thread(
                    self._set_status,
                    f"Done (parts {', '.join(map(str, failed))} missing — quota limit)",
                )
            else:
                self.call_from_thread(self._set_status, "Transcription complete")

            if self.speaker_list:
                self.call_from_thread(self._show_speakers)
            else:
                self.call_from_thread(self._show_result)

        except DailyQuotaExhausted as e:
            self.call_from_thread(self._on_error, str(e))
        except Exception as e:
            self.call_from_thread(self._on_error, str(e))

    def _set_status(self, msg: str) -> None:
        self.query_one("#status-label", Static).update(msg)

    def _set_progress(self, phase: str, current: int, total: int, label: str) -> None:
        self.query_one(f"#{phase}-progress", ProgressBar).update(
            total=total, progress=current,
        )
        self.query_one(f"#{phase}-label", Static).update(f"  {label}  ({current}/{total})")

    def _on_error(self, error: str) -> None:
        self.notify(f"Error: {error[:200]}", severity="error", timeout=10)
        self._switch_to("setup")

    # ── Speakers ─────────────────────────────────────────────────────

    def _show_speakers(self) -> None:
        container = self.query_one("#speaker-list", Container)
        container.remove_children()

        for i, spk in enumerate(self.speaker_list):
            m = re.search(rf"{re.escape(spk)}:\s*(.+)", self.raw_transcript)
            sample = ""
            if m:
                sample = m.group(1)[:70]
                if len(m.group(1)) > 70:
                    sample += "..."

            row = Container(classes="speaker-row")
            container.mount(row)
            row.mount(Static(f'{spk}:  "{sample}"', classes="speaker-sample"))
            controls = Horizontal(classes="speaker-controls")
            row.mount(controls)
            controls.mount(
                Input(value="", placeholder=f"Name for {spk}...", id=f"name-{i}", classes="speaker-input")
            )
            controls.mount(
                Button("Play", id=f"play-{i}", classes="play-btn")
            )

        self._switch_to("speakers")

    def _play_speaker(self, speaker: str) -> None:
        # Kill existing player
        if self.active_player:
            self.active_player[0].terminate()
            try:
                os.unlink(self.active_player[1])
            except OSError:
                pass
            self.active_player = None

        ts = get_speaker_timestamp(self.raw_transcript, speaker)
        if ts is not None:
            proc, tmp_file = play_audio_clip(self.audio_path, ts)
            if proc:
                self.active_player = (proc, tmp_file)
                self.notify(f"Playing {speaker} from {fmt_ts(ts)}")
            else:
                self.notify("No audio player found", severity="warning")
                try:
                    os.unlink(tmp_file)
                except OSError:
                    pass
        else:
            self.notify(f"No timestamp found for {speaker}", severity="warning")

    def _finish_speakers(self) -> None:
        # Kill any active player
        if self.active_player:
            self.active_player[0].terminate()
            try:
                os.unlink(self.active_player[1])
            except OSError:
                pass
            self.active_player = None

        # Apply renames
        renames = {}
        for i, spk in enumerate(self.speaker_list):
            try:
                inp = self.query_one(f"#name-{i}", Input)
                name = inp.value.strip()
                if name:
                    renames[spk] = name
            except Exception:
                pass

        for old, new in renames.items():
            self.raw_transcript = re.sub(
                rf"\b{re.escape(old)}:", f"{new}:", self.raw_transcript
            )

        if renames:
            self.notify(f"Renamed {len(renames)} speaker(s)")

        self._show_result()

    # ── Result ───────────────────────────────────────────────────────

    def _show_result(self) -> None:
        stem = Path(self.audio_path).stem
        audio_dir = Path(self.audio_path).parent.resolve()
        self.query_one("#save-path", Input).value = str(audio_dir / f"transcript_{stem}.txt")
        self.query_one("#save-srt-path", Input).value = str(audio_dir / f"transcript_{stem}.srt")

        # Default: hide SRT row, show text row
        self.query_one("#save-srt-row").display = False
        self.query_one("#save-text-row").display = True
        self.selected_format = 0

        self._update_transcript_view()

        # Summary
        n = len(self.speaker_list) if self.speaker_list else 0
        m, s = int(self.audio_duration // 60), int(self.audio_duration % 60)
        self.query_one("#summary", Static).update(
            f"Done  ·  {n} speaker{'s' if n != 1 else ''}  ·  {m}m {s:02d}s audio"
        )

        self._switch_to("result")

    def _update_transcript_view(self) -> None:
        """Update transcript display and save rows based on format selection."""
        if not self.raw_transcript:
            return

        ta = self.query_one("#transcript-view", TextArea)
        text_row = self.query_one("#save-text-row")
        srt_row = self.query_one("#save-srt-row")

        if self.selected_format == 0:  # Plain text
            ta.text = strip_timestamps(self.raw_transcript)
            text_row.display = True
            srt_row.display = False
        elif self.selected_format == 1:  # Text + timestamps
            ta.text = self.raw_transcript
            text_row.display = True
            srt_row.display = False
        elif self.selected_format == 2:  # SRT
            ta.text = transcript_to_srt(self.raw_transcript)
            text_row.display = False
            srt_row.display = True
        elif self.selected_format == 3:  # All
            ta.text = self.raw_transcript
            text_row.display = True
            srt_row.display = True

    def _save_text(self) -> None:
        path_str = self.query_one("#save-path", Input).value.strip()
        if not path_str:
            self.notify("Enter a save path", severity="warning")
            return

        if self.selected_format in (0, 3):
            text = strip_timestamps(self.raw_transcript)
        else:
            text = self.raw_transcript

        path = Path(path_str).resolve()
        try:
            path.write_text(text, encoding="utf-8")
            self.notify(f"Saved to {path}")
        except Exception as e:
            self.notify(f"Could not save: {e}", severity="error")

    def _save_srt(self) -> None:
        path_str = self.query_one("#save-srt-path", Input).value.strip()
        if not path_str:
            self.notify("Enter a save path", severity="warning")
            return

        srt = transcript_to_srt(self.raw_transcript)
        path = Path(path_str).resolve()
        try:
            path.write_text(srt, encoding="utf-8")
            self.notify(f"Saved SRT to {path}")
        except Exception as e:
            self.notify(f"Could not save: {e}", severity="error")

    def _reset(self) -> None:
        """Reset for a new transcription."""
        # Kill any active player
        if self.active_player:
            self.active_player[0].terminate()
            try:
                os.unlink(self.active_player[1])
            except OSError:
                pass
            self.active_player = None

        self.audio_path = ""
        self.audio_duration = 0.0
        self.raw_transcript = ""
        self.speaker_list = []
        self.selected_format = 0
        self.query_one("#file-input", Input).value = ""
        self.query_one("#file-info", Static).update("")
        self.query_one("#transcribe-btn", Button).disabled = True
        self._hide_rate_limit_warning()
        self._switch_to("setup")


# ── Entry Point ──────────────────────────────────────────────────────


def main():
    ap = argparse.ArgumentParser(
        description="Audio transcription with speaker detection",
        add_help=True,
    )
    ap.add_argument("audio", nargs="?", default=None, help="Audio file path")
    ap.add_argument("-k", "--api-key", default=None, help="Gemini API key")
    ap.add_argument("--reset-key", action="store_true", help="Remove saved API key")
    args = ap.parse_args()

    if args.reset_key:
        delete_api_key()
        print(f"  API key removed from {_key_store_name()}")
        sys.exit(0)

    app = TranscribeApp(
        initial_file=args.audio,
        initial_key=args.api_key,
    )
    app.run()


if __name__ == "__main__":
    main()
