# Transcribe

A terminal GUI app that transcribes audio files with speaker detection using Google Gemini.

Built with [Textual](https://textual.textualize.io/) for a proper GUI experience right in your terminal.

## Features

- **Terminal GUI** — real buttons, inputs, progress bars, and radio selectors — not just a CLI
- **Speaker detection** — automatically identifies speakers and lets you assign real names
- **Audio preview** — play a voice sample of each speaker to identify who's who
- **Long audio support** — files over 10 minutes are split and transcribed in parallel
- **Multiple formats** — plain text, timestamped text, or SRT subtitles
- **Verbatim transcription** — preserves filler words, pauses, laughter, and other sounds
- **Settings dialog** — change your API key anytime with `Ctrl+K`
- **Secure key storage** — macOS Keychain, Windows AppData, or Linux `~/.config`
- **Cross-platform** — works on macOS, Linux, and Windows

## Quick Start

**1. Install prerequisites:**

```bash
# macOS
brew install ffmpeg uv

# Linux
sudo apt install ffmpeg
curl -LsSf https://astral.sh/uv/install.sh | sh

# Windows
winget install ffmpeg
powershell -c "irm https://astral.sh/uv/install.ps1 | iex"
```

**2. Run:**

```bash
git clone https://github.com/marcelwachter/transcribe.git
cd transcribe
uv run transcribe.py
```

That's it — no `pip install`, no virtual env setup. `uv` handles everything automatically.

You can also run directly from GitHub without cloning:

```bash
uv run https://raw.githubusercontent.com/marcelwachter/transcribe/main/transcribe.py
```

## Usage

```bash
# Launch the GUI
uv run transcribe.py

# Pre-load an audio file
uv run transcribe.py recording.mp3

# Pre-set API key
uv run transcribe.py -k YOUR_KEY

# Remove saved API key
uv run transcribe.py --reset-key
```

## Keyboard Shortcuts

| Key | Action |
|-----|--------|
| `Ctrl+K` | Open settings (change API key) |
| `Ctrl+Q` | Quit |
| `Tab` | Move between fields |

## API Key

Your Gemini API key is resolved in this order:

1. `-k` flag
2. `GEMINI_API_KEY` or `GOOGLE_API_KEY` environment variable
3. Saved key (macOS Keychain / Windows AppData / Linux `~/.config/transcribe/key`)
4. Settings dialog on first launch

Get a free API key at [aistudio.google.com](https://aistudio.google.com/apikey).

## How It Works

1. Select a model and enter an audio file path
2. Click **Transcribe** — long files are automatically split and processed in parallel
3. Assign real names to detected speakers (with audio preview)
4. Choose output format: plain text, timestamped, or SRT subtitles
5. Save your transcript

## License

MIT
