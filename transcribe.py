#!/usr/bin/env python3
"""
transcribe - Terminal app for audio transcription with speaker detection.

Run:  uv run python transcribe.py
"""

import json
import math
import os
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
    from rich.table import Table
    from rich.text import Text
except ImportError:
    _need.append("rich")
if _need:
    print(f"Missing: {', '.join(_need)}")
    print(f"Run:  pip install {' '.join(_need)}")
    sys.exit(1)
for bin in ("ffmpeg", "ffprobe"):
    if not shutil.which(bin):
        print(f"{bin} is required:")
        print("  macOS:   brew install ffmpeg")
        print("  Linux:   sudo apt install ffmpeg")
        sys.exit(1)

# ── Config ────────────────────────────────────────────────────────────

MAX_CHUNK_S = 10 * 60  # 10 minutes in seconds

MODELS = {
    "1": ("gemini-3-flash-preview", "Gemini 3 Flash Preview  [dim](latest, recommended)[/]"),
    "2": ("gemini-2.5-flash-preview-04-17", "Gemini 2.5 Flash        [dim](stable)[/]"),
}
DEFAULT_MODEL = "gemini-3-flash-preview"

# ── Console ───────────────────────────────────────────────────────────

C = Console()

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

    C.print()
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

    C.print(f"  [green]>[/] Split into    [bold]{len(parts)}[/] parts [dim](10 min each)[/]")
    return parts, duration


def play_audio_clip(filepath, start_seconds, duration=8):
    """Extract a clip and play it. Returns the Popen process."""
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
    # Try afplay (macOS), then ffplay, then aplay
    for cmd in (["afplay", tmp.name], ["ffplay", "-nodisp", "-autoexit", tmp.name]):
        if shutil.which(cmd[0]):
            proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
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


# ── Secure key storage ────────────────────────────────────────────────

KEYCHAIN_SERVICE = "transcribe-cli"
KEYCHAIN_ACCOUNT = "gemini-api-key"


def _is_macos():
    return sys.platform == "darwin"


def save_api_key(key):
    """Save API key to macOS Keychain or fallback config file."""
    if _is_macos():
        # Delete old entry if exists (ignore errors)
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
        # Linux/other: store in ~/.config/transcribe/key (chmod 600)
        cfg = Path.home() / ".config" / "transcribe"
        cfg.mkdir(parents=True, exist_ok=True)
        kf = cfg / "key"
        kf.write_text(key, encoding="utf-8")
        kf.chmod(0o600)
        return True


def load_api_key():
    """Load API key from macOS Keychain or fallback config file."""
    if _is_macos():
        r = subprocess.run(
            ["security", "find-generic-password",
             "-s", KEYCHAIN_SERVICE, "-a", KEYCHAIN_ACCOUNT, "-w"],
            capture_output=True, text=True,
        )
        if r.returncode == 0 and r.stdout.strip():
            return r.stdout.strip()
    else:
        kf = Path.home() / ".config" / "transcribe" / "key"
        if kf.exists():
            return kf.read_text(encoding="utf-8").strip()
    return None


def delete_api_key():
    """Remove saved API key."""
    if _is_macos():
        subprocess.run(
            ["security", "delete-generic-password",
             "-s", KEYCHAIN_SERVICE, "-a", KEYCHAIN_ACCOUNT],
            capture_output=True,
        )
    else:
        kf = Path.home() / ".config" / "transcribe" / "key"
        if kf.exists():
            kf.unlink()


# ── UI Steps ──────────────────────────────────────────────────────────


def show_header():
    title = Text(justify="center")
    title.append("T R A N S C R I B E\n", style="bold bright_white")
    title.append("audio to text  ·  speaker detection  ·  gemini", style="dim")
    C.print()
    C.print(Panel(title, border_style="bright_blue", padding=(1, 2)))
    C.print()


def step_api_key(arg_key):
    # 1. CLI argument
    if arg_key:
        C.print("  [green]>[/] API key      provided via flag")
        return arg_key

    # 2. Environment variable
    env_key = os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY")
    if env_key:
        C.print("  [green]>[/] API key      loaded from environment")
        return env_key

    # 3. Saved key (Keychain on macOS, config file on Linux)
    saved = load_api_key()
    if saved:
        where = "Keychain" if _is_macos() else "config"
        C.print(f"  [green]>[/] API key      loaded from {where}")
        return saved

    # 4. Prompt and offer to save
    C.print("  [dim]Paste your Gemini API key (input is hidden):[/]")
    key = Prompt.ask("  [bright_blue]>[/] API key     ", password=True)
    if not key.strip():
        C.print("  [red]No key provided. Exiting.[/]")
        sys.exit(1)
    key = key.strip()

    where = "Keychain" if _is_macos() else "~/.config/transcribe/"
    save = Prompt.ask(
        f"  [dim]Save to {where} for next time?[/] [bright_blue]Y[/]/n",
        default="y", show_default=False,
    )
    if save.strip().lower() in ("y", "yes", ""):
        if save_api_key(key):
            C.print(f"  [green]>[/] API key      saved to {where}")
        else:
            C.print("  [yellow]Could not save key[/]")

    return key


def step_model(arg_model):
    if arg_model:
        C.print(f"  [green]>[/] Model         [bold]{arg_model}[/]")
        return arg_model
    C.print()
    C.print("  [dim]Select a model:[/]")
    for key, (model_id, label) in MODELS.items():
        C.print(f"    [bright_blue]{key}[/]  {label}")
    choice = Prompt.ask(
        "  [bright_blue]>[/] Model        ",
        default="1", show_default=False,
    )
    model_id = MODELS.get(choice.strip(), (None, None))[0]
    if model_id is None:
        model_id = choice.strip()
    C.print(f"  [green]>[/] Using         [bold]{model_id}[/]")
    return model_id


def step_audio_file(arg_path=None):
    if arg_path:
        path = arg_path.strip().strip("'\"")
    else:
        C.print()
        C.print("  [dim]Enter path or drag & drop the file here:[/]")
        raw = Prompt.ask("  [bright_blue]>[/] Audio file  ")
        path = raw.strip().strip("'\"")

    p = Path(path)
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
        f"  [green]>[/] Loaded        "
        f"[bold]{p.name}[/]  "
        f"[dim]({m}m {s:02d}s · {size_mb:.1f} MB)[/]"
    )
    return str(p)


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


def gemini_transcribe(client, model, uploaded_file, part_ctx=""):
    prompt = f"""{part_ctx}Transcribe this audio precisely with speaker diarization.

Rules:
- Start every line with a timestamp in [MM:SS] format relative to the start of this audio
- Label each distinct speaker consistently: Speaker 1, Speaker 2, etc.
- Format every line as: [MM:SS] Speaker N: [spoken text]
- New line on each speaker change
- Include ALL speech verbatim — do not summarize, skip, or paraphrase
- Preserve the original language of the audio"""

    # Retry with backoff for rate limits
    for attempt in range(5):
        try:
            resp = client.models.generate_content(
                model=model,
                contents=[prompt, uploaded_file],
                config=types.GenerateContentConfig(temperature=1.0),
            )
            return resp.text
        except Exception as e:
            err = str(e).lower()
            if ("429" in err or "resource_exhausted" in err or "rate" in err) and attempt < 4:
                wait = (2 ** attempt) * 5  # 5s, 10s, 20s, 40s
                time.sleep(wait)
                continue
            raise


def strip_fences(text):
    text = text.strip()
    text = re.sub(r"^```[^\n]*\n?", "", text)
    text = re.sub(r"\n?```\s*$", "", text)
    return text.strip()


def do_transcribe_single(client, model, filepath):
    C.print()
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
    C.print("  [green]>[/] Transcription  [green]complete[/]")
    return strip_fences(text)


def do_transcribe_chunked(client, model, chunk_paths):
    """Two-phase parallel pipeline: upload all, then transcribe all."""
    n = len(chunk_paths)
    C.print()

    # ── Phase 1: Upload ALL files in parallel ─────────────────────
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
    C.print(f"  [green]>[/] Uploaded       [bold]{len(ready)}[/]/{n} files")

    # ── Phase 2: Transcribe ALL in parallel ───────────────────────
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
        with ThreadPoolExecutor(max_workers=len(ready)) as pool:
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

    # ── Cleanup: delete all uploaded files ────────────────────────
    for f in uploaded:
        if f is not None:
            try:
                client.files.delete(name=f.name)
            except Exception:
                pass

    if errors:
        for idx, err in errors:
            C.print(f"  [red]! Part {idx + 1} failed: {err}[/]")

    # ── Merge with timestamp offsets ─────────────────────────────
    results.sort()
    merged_parts = []
    for idx, text in results:
        chunk_text = strip_fences(text)
        chunk_text = offset_timestamps(chunk_text, idx * MAX_CHUNK_S)
        merged_parts.append(chunk_text)

    C.print(f"  [green]>[/] Transcription  [green]complete[/]")
    return "\n\n".join(merged_parts)


# ── Speaker Assignment ────────────────────────────────────────────────


def find_speakers(text):
    return sorted(
        set(re.findall(r"(Speaker \d+):", text)),
        key=lambda s: int(re.search(r"\d+", s).group()),
    )


def step_assign_speakers(text, speakers, filepath):
    C.print()

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
    C.print("  [dim]Assign real names  (Enter = keep label, [bold]p[/bold] = play voice sample):[/]")
    C.print()

    renames = {}
    active_player = None  # (process, tmp_file)

    for spk in speakers:
        while True:
            try:
                name = Prompt.ask(
                    f"    [bright_blue]{spk}[/] [dim]->[/]",
                    default="", show_default=False,
                )
            except (EOFError, KeyboardInterrupt):
                C.print()
                # Clean up player
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
                continue  # Re-prompt for actual name
            else:
                if name.strip():
                    renames[spk] = name.strip()
                break  # Move to next speaker

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
        C.print(f"  [green]>[/] Renamed {len(renames)} speaker(s)")

    return text


# ── Output ────────────────────────────────────────────────────────────


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


def step_save(text, filepath):
    stem = Path(filepath).stem
    default_name = f"transcript_{stem}.txt"

    C.print()
    C.print(f"  [dim]Press Enter to save, or type a different path. Type [bold]n[/bold] to skip.[/]")
    save = Prompt.ask(
        "  [bright_blue]>[/] Save to      ",
        default=default_name, show_default=True,
    )

    if save.strip().lower() in ("n", "no"):
        C.print("  [dim]Skipped.[/]")
        return

    path = save.strip().strip("'\"")
    try:
        Path(path).write_text(text, encoding="utf-8")
        C.print(f"  [green]>[/] Saved to [bold]{path}[/]")
    except Exception as e:
        C.print(f"  [red]Could not save: {e}[/]")


# ── Main ──────────────────────────────────────────────────────────────


def main():
    import argparse

    ap = argparse.ArgumentParser(add_help=False)
    ap.add_argument("audio", nargs="?", default=None)
    ap.add_argument("-k", "--api-key", default=None)
    ap.add_argument("-o", "--output", default=None)
    ap.add_argument("-m", "--model", default=None)
    ap.add_argument("-t", "--timestamps", action="store_true")
    ap.add_argument("--no-speakers", action="store_true")
    ap.add_argument("--reset-key", action="store_true")
    ap.add_argument("-h", "--help", action="store_true")
    args = ap.parse_args()

    if args.reset_key:
        delete_api_key()
        where = "Keychain" if _is_macos() else "config"
        C.print(f"  [green]>[/] API key removed from {where}")
        sys.exit(0)

    if args.help:
        show_header()
        C.print("  [bold]Usage:[/]  python transcribe.py [audio_file]")
        C.print()
        C.print("  [dim]Options:[/]")
        C.print("    [bright_blue]-k[/]  --api-key      Gemini API key")
        C.print("    [bright_blue]-o[/]  --output       Save transcript to file")
        C.print("    [bright_blue]-m[/]  --model        Model name")
        C.print("    [bright_blue]-t[/]  --timestamps   Include timestamps in output")
        C.print("    [bright_blue]    --no-speakers[/]  Skip speaker name assignment")
        C.print("    [bright_blue]    --reset-key[/]    Remove saved API key")
        C.print()
        C.print("  [dim]Or just run [bold]python transcribe.py[/bold] and follow the prompts.[/]")
        C.print()
        sys.exit(0)

    show_header()

    # 1. API key
    key = step_api_key(args.api_key)
    client = genai.Client(api_key=key)

    # 2. Model
    model = step_model(args.model)

    # 3. Audio file
    filepath = step_audio_file(args.audio)

    # 4. Split if needed + transcribe
    with tempfile.TemporaryDirectory() as tmpdir:
        chunk_paths, duration = split_audio(filepath, MAX_CHUNK_S, tmpdir)

        if chunk_paths is None:
            transcript = do_transcribe_single(client, model, filepath)
        else:
            transcript = do_transcribe_chunked(client, model, chunk_paths)

    # 5. Speaker names (uses timestamped version for audio preview)
    speakers = find_speakers(transcript)
    if speakers and not args.no_speakers:
        transcript = step_assign_speakers(transcript, speakers, filepath)

    # 6. Strip timestamps if not requested
    if not args.timestamps:
        transcript = strip_timestamps(transcript)

    # 7. Show transcript
    step_output(transcript)

    # 8. Save
    if args.output:
        try:
            Path(args.output).write_text(transcript, encoding="utf-8")
            C.print(f"\n  [green]>[/] Saved to [bold]{args.output}[/]")
        except Exception as e:
            C.print(f"\n  [red]Could not save: {e}[/]")
    else:
        step_save(transcript, filepath)

    C.print()


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        C.print("\n  [yellow]Interrupted.[/]")
        sys.exit(130)
