#!/usr/bin/env python3
# /// script
# requires-python = ">=3.10"
# dependencies = [
#     "google-genai>=1.65.0",
#     "rich>=14.0.0",
# ]
# ///
"""
transcribe — Terminal app for audio transcription with speaker detection.

Usage:
  uv run transcribe.py              # interactive
  uv run transcribe.py recording.mp3
"""

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
    from rich import box
    from rich.console import Console
    from rich.panel import Panel
    from rich.progress import (
        BarColumn,
        Progress,
        SpinnerColumn,
        TaskProgressColumn,
        TextColumn,
    )
    from rich.prompt import Prompt
    from rich.rule import Rule
    from rich.table import Table
    from rich.text import Text
except ImportError:
    _need.append("rich")
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

VERSION = "0.2.0"
MAX_CHUNK_S = 10 * 60  # 10 minutes

MODELS = {
    "1": ("gemini-3-flash-preview", "Gemini 3 Flash  [dim](recommended)[/]"),
    "2": ("gemini-2.5-flash-preview-04-17", "Gemini 2.5 Flash  [dim](stable)[/]"),
}

TOTAL_STEPS = 7

# ── Console ───────────────────────────────────────────────────────────

C = Console()

# ── UI Helpers ────────────────────────────────────────────────────────


def step_header(num, label):
    """Print a styled step divider."""
    C.print()
    C.print(
        Rule(
            f"[bold bright_blue] {num}/{TOTAL_STEPS} [/]  [dim]{label}[/]",
            style="bright_blue",
            align="left",
        )
    )
    C.print()


# ── Audio helpers (ffmpeg) ────────────────────────────────────────────


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


def split_audio(path, chunk_s, outdir):
    """Split audio file into <=chunk_s second parts. Returns list of paths."""
    duration = probe_audio(path)
    if duration is None or duration <= 0:
        C.print(f"  [red]Cannot read audio file: {path}[/]")
        sys.exit(1)
    if duration <= chunk_s:
        return None, duration

    total_parts = math.ceil(duration / chunk_s)

    parts = []
    with Progress(
        SpinnerColumn(style="bright_blue"),
        TextColumn("[dim]{task.description}[/]"),
        BarColumn(bar_width=30, complete_style="bright_blue", finished_style="green"),
        TaskProgressColumn(),
        console=C,
    ) as prog:
        task = prog.add_task("  Splitting audio", total=total_parts)
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
            prog.advance(task)

    C.print(f"  [green]✓[/] Split into [bold]{len(parts)}[/] parts [dim](10 min each)[/]")
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

    # Platform-specific player, then cross-platform ffplay
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


# ── Timestamp helpers ─────────────────────────────────────────────────


def parse_ts(ts_str):
    """Parse [MM:SS] or [H:MM:SS] to total seconds."""
    ts_str = ts_str.strip("[]")
    parts = ts_str.split(":")
    if len(parts) == 2:
        return int(parts[0]) * 60 + int(parts[1])
    elif len(parts) == 3:
        return int(parts[0]) * 3600 + int(parts[1]) * 60 + int(parts[2])
    return 0


def fmt_ts(seconds):
    """Format seconds as [MM:SS] or [H:MM:SS]."""
    seconds = max(0, int(seconds))
    if seconds >= 3600:
        h = seconds // 3600
        m = (seconds % 3600) // 60
        s = seconds % 60
        return f"[{h}:{m:02d}:{s:02d}]"
    return f"[{seconds // 60:02d}:{seconds % 60:02d}]"


def fmt_srt_ts(seconds):
    """Format seconds as HH:MM:SS,000 for SRT."""
    seconds = max(0, int(seconds))
    h = seconds // 3600
    m = (seconds % 3600) // 60
    s = seconds % 60
    return f"{h:02d}:{m:02d}:{s:02d},000"


def offset_timestamps(text, offset_seconds):
    """Add offset_seconds to all [MM:SS] or [H:MM:SS] timestamps in text."""
    if offset_seconds == 0:
        return text

    def _replace(m):
        original = m.group(0)
        secs = parse_ts(original) + offset_seconds
        return fmt_ts(secs)

    return re.sub(r"\[\d{1,2}:\d{2}(?::\d{2})?\]", _replace, text)


def strip_timestamps(text):
    """Remove all [MM:SS] or [H:MM:SS] timestamps from text."""
    return re.sub(r"\[\d{1,2}:\d{2}(?::\d{2})?\]\s*", "", text)


def get_speaker_timestamp(text, speaker):
    """Find the first timestamp for a speaker. Returns seconds or None."""
    pattern = rf"\[(\d{{1,2}}:\d{{2}}(?::\d{{2}})?)\]\s*{re.escape(speaker)}:"
    m = re.search(pattern, text)
    if m:
        return parse_ts(m.group(1))
    return None


def transcript_to_srt(text):
    """Convert timestamped transcript to SRT subtitle format."""
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


# ── Secure key storage ────────────────────────────────────────────────

KEYCHAIN_SERVICE = "transcribe-cli"
KEYCHAIN_ACCOUNT = "gemini-api-key"


def _key_file_path():
    """Get the key file path for Linux/Windows."""
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
    """Save API key securely."""
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
    """Load saved API key."""
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
    """Remove saved API key."""
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


# ── UI Steps ──────────────────────────────────────────────────────────


def show_header():
    title = Text(justify="center")
    title.append("\n")
    title.append("T R A N S C R I B E\n", style="bold bright_white")
    title.append("audio to text  ·  speaker detection  ·  gemini\n", style="dim")
    title.append(f"v{VERSION}", style="dim italic")
    C.print(Panel(
        title,
        border_style="bright_blue",
        padding=(1, 4),
        box=box.DOUBLE,
    ))


def step_api_key(arg_key):
    step_header(1, "API Key")

    if arg_key:
        C.print("  [green]✓[/] API key provided via flag")
        return arg_key

    env_key = os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY")
    if env_key:
        C.print("  [green]✓[/] API key loaded from environment")
        return env_key

    saved = load_api_key()
    if saved:
        C.print(f"  [green]✓[/] API key loaded from {_key_store_name()}")
        return saved

    C.print("  [dim]Paste your Gemini API key (input is hidden):[/]")
    key = Prompt.ask("  [bright_blue]>[/] API key", password=True)
    if not key.strip():
        C.print("  [red]No key provided. Exiting.[/]")
        sys.exit(1)
    key = key.strip()

    where = _key_store_name()
    save = Prompt.ask(
        f"  [dim]Save to {where} for next time?[/] [bright_blue]Y[/]/n",
        default="y", show_default=False,
    )
    if save.strip().lower() in ("y", "yes", ""):
        if save_api_key(key):
            C.print(f"  [green]✓[/] API key saved to {where}")
        else:
            C.print("  [yellow]Could not save key[/]")

    return key


def step_model(arg_model):
    step_header(2, "Model")

    if arg_model:
        C.print(f"  [green]✓[/] Using [bold]{arg_model}[/]")
        return arg_model

    C.print("  [dim]Select a model:[/]")
    C.print()
    for key, (_, label) in MODELS.items():
        C.print(f"    [bright_blue]{key}[/]  {label}")
    C.print()
    choice = Prompt.ask(
        "  [bright_blue]>[/] Model",
        default="1", show_default=False,
    )
    model_id = MODELS.get(choice.strip(), (None, None))[0]
    if model_id is None:
        model_id = choice.strip()
    C.print(f"  [green]✓[/] Using [bold]{model_id}[/]")
    return model_id


def step_audio_file(arg_path=None):
    step_header(3, "Audio File")

    if arg_path:
        path = arg_path.strip().strip("'\"")
    else:
        C.print("  [dim]Enter path or drag & drop the file here:[/]")
        raw = Prompt.ask("  [bright_blue]>[/] Audio file")
        path = raw.strip().strip("'\"")

    p = Path(path).resolve()
    if not p.exists():
        C.print(f"  [red]File not found: {path}[/]")
        sys.exit(1)

    duration = probe_audio(str(p))
    if duration is None or duration <= 0:
        C.print(f"  [red]Cannot read audio: {path}[/]")
        sys.exit(1)

    size_mb = p.stat().st_size / (1024 * 1024)
    m, s = int(duration // 60), int(duration % 60)
    C.print(
        f"  [green]✓[/] Loaded [bold]{p.name}[/]  "
        f"[dim]({m}m {s:02d}s · {size_mb:.1f} MB)[/]"
    )
    return str(p), duration


def step_format(args):
    """Interactive output format selection. Returns format dict."""
    step_header(6, "Output Format")

    # If CLI flags set, use those
    if args.timestamps or args.srt:
        fmt = {"txt": True, "timestamps": args.timestamps, "srt": args.srt}
        parts = []
        if args.timestamps:
            parts.append("text + timestamps")
        else:
            parts.append("text")
        if args.srt:
            parts.append("SRT")
        C.print(f"  [green]✓[/] Format: [bold]{' + '.join(parts)}[/]")
        return fmt

    C.print("  [dim]Select output format:[/]")
    C.print()
    C.print("    [bright_blue]1[/]  Plain text                [dim](default)[/]")
    C.print("    [bright_blue]2[/]  Plain text with timestamps")
    C.print("    [bright_blue]3[/]  SRT subtitles")
    C.print("    [bright_blue]4[/]  All formats")
    C.print()
    choice = Prompt.ask("  [bright_blue]>[/] Format", default="1", show_default=False)

    fmt = {"txt": True, "timestamps": False, "srt": False}
    choice = choice.strip()
    if choice == "2":
        fmt["timestamps"] = True
        C.print("  [green]✓[/] Format: [bold]text with timestamps[/]")
    elif choice == "3":
        fmt["srt"] = True
        fmt["txt"] = False
        C.print("  [green]✓[/] Format: [bold]SRT subtitles[/]")
    elif choice == "4":
        fmt["timestamps"] = True
        fmt["srt"] = True
        C.print("  [green]✓[/] Format: [bold]all formats[/]")
    else:
        C.print("  [green]✓[/] Format: [bold]plain text[/]")

    return fmt


# ── Transcription Engine ─────────────────────────────────────────────


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
    """Parse the suggested retry delay (seconds) from an API error."""
    # Match "retryDelay": "47s" or retryDelay: '47.123s'
    m = re.search(r"retryDelay['\"]?\s*:\s*['\"]?(\d+(?:\.\d+)?)\s*s", error_str)
    if m:
        return float(m.group(1))
    # Match "retry in 47.123456s"
    m = re.search(r"retry in (\d+(?:\.\d+)?)\s*s", error_str, re.IGNORECASE)
    if m:
        return float(m.group(1))
    return None


def gemini_transcribe(client, model, uploaded_file, part_ctx=""):
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
            return resp.text
        except Exception as e:
            err = str(e).lower()
            retryable = (
                "429" in err
                or "503" in err
                or "500" in err
                or "resource_exhausted" in err
                or "unavailable" in err
                or "high demand" in err
                or "internal" in err
                or "rate" in err
            )
            if retryable and attempt < max_retries - 1:
                # Use the API's suggested delay if available
                api_delay = _parse_retry_delay(str(e))
                if api_delay:
                    wait = api_delay + random.uniform(1, 5)
                else:
                    wait = (2 ** attempt) * 5
                time.sleep(wait)
                continue
            raise


def strip_fences(text):
    text = text.strip()
    text = re.sub(r"^```[^\n]*\n?", "", text)
    text = re.sub(r"\n?```\s*$", "", text)
    return text.strip()


def do_transcribe_single(client, model, filepath):
    with Progress(
        SpinnerColumn(style="bright_blue"),
        TextColumn("[dim]{task.description}[/]"),
        console=C,
        transient=True,
    ) as prog:
        prog.add_task("  Uploading & transcribing...", total=None)
        f = upload_and_wait(client, filepath)
        try:
            text = gemini_transcribe(client, model, f)
        finally:
            try:
                client.files.delete(name=f.name)
            except Exception:
                pass
    C.print("  [green]✓[/] Transcription complete")
    return strip_fences(text)


def do_transcribe_chunked(client, model, chunk_paths):
    """Two-phase parallel pipeline: upload all, then transcribe all."""
    n = len(chunk_paths)

    # Phase 1: Upload
    uploaded = [None] * n
    upload_errors = []
    with Progress(
        SpinnerColumn(style="bright_blue"),
        TextColumn("[dim]{task.description}[/]"),
        BarColumn(bar_width=30, complete_style="bright_blue", finished_style="green"),
        TaskProgressColumn(),
        console=C,
    ) as prog:
        task = prog.add_task("  Uploading parts", total=n)
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
                prog.advance(task)

    if upload_errors:
        for idx, err in upload_errors:
            C.print(f"  [red]! Upload failed for part {idx + 1}: {err}[/]")

    ready = [(i, f) for i, f in enumerate(uploaded) if f is not None]
    C.print(f"  [green]✓[/] Uploaded [bold]{len(ready)}[/]/{n} files")

    # Phase 2: Transcribe (limited concurrency to avoid rate limits)
    max_concurrent = min(len(ready), 5)

    def _transcribe_one(idx, ufile):
        ctx = f"(Segment {idx + 1} of {n} of a longer recording.) "
        return idx, gemini_transcribe(client, model, ufile, ctx)

    results = []
    errors = []
    with Progress(
        SpinnerColumn(style="bright_blue"),
        TextColumn("[dim]{task.description}[/]"),
        BarColumn(bar_width=30, complete_style="bright_blue", finished_style="green"),
        TaskProgressColumn(),
        console=C,
    ) as prog:
        task = prog.add_task("  Transcribing parts", total=len(ready))
        with ThreadPoolExecutor(max_workers=max_concurrent) as pool:
            futs = {
                pool.submit(_transcribe_one, i, f): i
                for i, f in ready
            }
            for fut in as_completed(futs):
                try:
                    idx, text = fut.result()
                    results.append((idx, text))
                except Exception as e:
                    idx = futs[fut]
                    errors.append((idx, str(e)))
                    results.append((idx, f"[Error in part {idx + 1}: {e}]"))
                prog.advance(task)

    # Cleanup uploaded files
    for f in uploaded:
        if f is not None:
            try:
                client.files.delete(name=f.name)
            except Exception:
                pass

    if errors:
        for idx, err in errors:
            C.print(f"  [red]! Part {idx + 1} failed: {err}[/]")

    # Merge with timestamp offsets
    results.sort()
    merged_parts = []
    for idx, text in results:
        chunk_text = strip_fences(text)
        chunk_text = offset_timestamps(chunk_text, idx * MAX_CHUNK_S)
        merged_parts.append(chunk_text)

    C.print(f"  [green]✓[/] Transcription complete")
    return "\n\n".join(merged_parts)


# ── Speaker Assignment ────────────────────────────────────────────────


def find_speakers(text):
    return sorted(
        set(re.findall(r"(Speaker \d+):", text)),
        key=lambda s: int(re.search(r"\d+", s).group()),
    )


def step_assign_speakers(text, speakers, filepath):
    step_header(5, "Speakers")

    table = Table(
        box=box.ROUNDED,
        border_style="bright_blue",
        show_header=True,
        header_style="bold",
        padding=(0, 1),
    )
    table.add_column("#", style="bright_blue bold", width=12)
    table.add_column("Sample", style="dim")

    for spk in speakers:
        m = re.search(rf"{re.escape(spk)}:\s*(.+)", text)
        sample = m.group(1)[:80] if m else ""
        if m and len(m.group(1)) > 80:
            sample += "..."
        table.add_row(spk, sample)

    C.print(table)
    C.print()
    C.print("  [dim]Assign real names  (Enter = keep, [bold]p[/bold] = play voice sample):[/]")
    C.print()

    renames = {}
    active_player = None

    for spk in speakers:
        while True:
            try:
                name = Prompt.ask(
                    f"    [bright_blue]{spk}[/] [dim]→[/]",
                    default="", show_default=False,
                )
            except (EOFError, KeyboardInterrupt):
                C.print()
                if active_player:
                    active_player[0].terminate()
                    try:
                        os.unlink(active_player[1])
                    except OSError:
                        pass
                return text

            # Stop any playing audio
            if active_player:
                active_player[0].terminate()
                try:
                    os.unlink(active_player[1])
                except OSError:
                    pass
                active_player = None

            if name.strip().lower() == "p":
                ts = get_speaker_timestamp(text, spk)
                if ts is not None:
                    C.print(f"      [dim]Playing from {fmt_ts(ts)}...[/]")
                    proc, tmp_file = play_audio_clip(filepath, ts)
                    if proc:
                        active_player = (proc, tmp_file)
                    else:
                        C.print("      [yellow]No audio player found[/]")
                        try:
                            os.unlink(tmp_file)
                        except OSError:
                            pass
                else:
                    C.print("      [yellow]No timestamp found for this speaker[/]")
                continue
            else:
                if name.strip():
                    renames[spk] = name.strip()
                break

    # Clean up any lingering player
    if active_player:
        active_player[0].terminate()
        try:
            os.unlink(active_player[1])
        except OSError:
            pass

    for old, new in renames.items():
        text = re.sub(rf"\b{re.escape(old)}:", f"{new}:", text)

    if renames:
        C.print()
        C.print(f"  [green]✓[/] Renamed {len(renames)} speaker(s)")

    return text


# ── Output & Save ────────────────────────────────────────────────────


def step_output(text):
    C.print()
    C.print(
        Panel(
            text,
            title="[bold]Transcript[/]",
            title_align="left",
            border_style="green",
            padding=(1, 2),
        )
    )


def step_save(txt_text, srt_text, filepath, fmt):
    step_header(7, "Save")

    audio_dir = Path(filepath).parent.resolve()
    stem = Path(filepath).stem
    save_txt = fmt.get("txt", True)
    save_srt = fmt.get("srt", False)

    if save_txt and txt_text:
        default_txt = str(audio_dir / f"transcript_{stem}.txt")
        C.print("  [dim]Press Enter to save, type a path, or [bold]n[/bold] to skip.[/]")
        save = Prompt.ask(
            "  [bright_blue]>[/] Save text to",
            default=default_txt, show_default=True,
        )
        if save.strip().lower() not in ("n", "no"):
            path = Path(save.strip().strip("'\"")).resolve()
            try:
                path.write_text(txt_text, encoding="utf-8")
                C.print(f"  [green]✓[/] Saved to [bold]{path}[/]")
            except Exception as e:
                C.print(f"  [red]Could not save: {e}[/]")
        else:
            C.print("  [dim]Skipped text.[/]")

    if save_srt and srt_text:
        default_srt = str(audio_dir / f"transcript_{stem}.srt")
        C.print()
        save = Prompt.ask(
            "  [bright_blue]>[/] Save SRT to ",
            default=default_srt, show_default=True,
        )
        if save.strip().lower() not in ("n", "no"):
            path = Path(save.strip().strip("'\"")).resolve()
            try:
                path.write_text(srt_text, encoding="utf-8")
                C.print(f"  [green]✓[/] Saved to [bold]{path}[/]")
            except Exception as e:
                C.print(f"  [red]Could not save SRT: {e}[/]")
        else:
            C.print("  [dim]Skipped SRT.[/]")

    if not save_txt and not save_srt:
        C.print("  [dim]Nothing to save.[/]")


# ── Main ──────────────────────────────────────────────────────────────


def main():
    import argparse

    ap = argparse.ArgumentParser(add_help=False)
    ap.add_argument("audio", nargs="?", default=None)
    ap.add_argument("-k", "--api-key", default=None)
    ap.add_argument("-o", "--output", default=None)
    ap.add_argument("-m", "--model", default=None)
    ap.add_argument("-t", "--timestamps", action="store_true")
    ap.add_argument("--srt", action="store_true")
    ap.add_argument("--no-speakers", action="store_true")
    ap.add_argument("--reset-key", action="store_true")
    ap.add_argument("-h", "--help", action="store_true")
    args = ap.parse_args()

    if args.reset_key:
        delete_api_key()
        C.print(f"  [green]✓[/] API key removed from {_key_store_name()}")
        sys.exit(0)

    if args.help:
        show_header()
        C.print()
        C.print("  [bold]Usage:[/]  uv run transcribe.py [audio_file]")
        C.print()
        C.print("  [dim]Options:[/]")
        C.print("    [bright_blue]-k[/]  --api-key        Gemini API key")
        C.print("    [bright_blue]-o[/]  --output         Save transcript to file")
        C.print("    [bright_blue]-m[/]  --model          Model name")
        C.print("    [bright_blue]-t[/]  --timestamps     Include timestamps in output")
        C.print("    [bright_blue]    --srt[/]            Save as SRT subtitle file")
        C.print("    [bright_blue]    --no-speakers[/]    Skip speaker name assignment")
        C.print("    [bright_blue]    --reset-key[/]      Remove saved API key")
        C.print()
        C.print("  [dim]Just run [bold]uv run transcribe.py[/bold] and follow the prompts.[/]")
        C.print()
        sys.exit(0)

    show_header()

    # Step 1: API key
    key = step_api_key(args.api_key)
    client = genai.Client(api_key=key)

    # Step 2: Model
    model = step_model(args.model)

    # Step 3: Audio file
    filepath, duration = step_audio_file(args.audio)

    # Step 4: Transcribe
    step_header(4, "Transcribe")

    with tempfile.TemporaryDirectory() as tmpdir:
        chunk_paths, _ = split_audio(filepath, MAX_CHUNK_S, tmpdir)
        if chunk_paths is None:
            transcript = do_transcribe_single(client, model, filepath)
        else:
            transcript = do_transcribe_chunked(client, model, chunk_paths)

    # Step 5: Speaker names
    speakers = find_speakers(transcript)
    if speakers and not args.no_speakers:
        transcript = step_assign_speakers(transcript, speakers, filepath)

    # Step 6: Output format
    fmt = step_format(args)

    # Generate outputs
    srt_text = transcript_to_srt(transcript) if fmt.get("srt") else None
    show_ts = fmt.get("timestamps") or (fmt.get("srt") and not fmt.get("txt"))
    display_text = transcript if show_ts else strip_timestamps(transcript)
    txt_output = transcript if fmt.get("timestamps") else strip_timestamps(transcript)

    # Show transcript
    step_output(display_text)

    # Step 7: Save
    if args.output:
        try:
            Path(args.output).write_text(txt_output, encoding="utf-8")
            C.print(f"\n  [green]✓[/] Saved to [bold]{args.output}[/]")
        except Exception as e:
            C.print(f"\n  [red]Could not save: {e}[/]")
        if srt_text:
            srt_path = Path(args.output).with_suffix(".srt")
            try:
                srt_path.write_text(srt_text, encoding="utf-8")
                C.print(f"  [green]✓[/] Saved to [bold]{srt_path}[/]")
            except Exception as e:
                C.print(f"  [red]Could not save SRT: {e}[/]")
    else:
        step_save(txt_output, srt_text, filepath, fmt)

    # Summary footer
    n_speakers = len(speakers) if speakers else 0
    m, s = int(duration // 60), int(duration % 60)
    fmt_parts = []
    if fmt.get("txt"):
        if fmt.get("timestamps"):
            fmt_parts.append("text + timestamps")
        else:
            fmt_parts.append("text")
    if fmt.get("srt"):
        fmt_parts.append("SRT")
    if not fmt_parts:
        fmt_parts.append("text")

    C.print()
    C.print(Rule(style="dim"))
    C.print(
        f"  [green]✓[/] [bold]Done[/]  [dim]·  "
        f"{n_speakers} speaker{'s' if n_speakers != 1 else ''}  ·  "
        f"{m}m {s:02d}s audio  ·  "
        f"{' + '.join(fmt_parts)}[/]"
    )
    C.print()


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        C.print("\n  [yellow]Interrupted.[/]")
        sys.exit(130)
