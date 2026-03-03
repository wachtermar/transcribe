# Transcribe

A terminal app that transcribes audio files with speaker detection using Google Gemini.

```
╭──────────────────────────────────────────────────────╮
│               T R A N S C R I B E                    │
│      audio to text  ·  speaker detection  ·  gemini  │
╰──────────────────────────────────────────────────────╯
```

## Features

- **Speaker detection** — automatically identifies different speakers and lets you assign real names
- **Audio preview** — play a voice sample of each speaker to identify who's who
- **Long audio support** — files over 10 minutes are split and transcribed in parallel
- **Timestamps** — optional `[MM:SS]` timestamps on every line
- **Secure API key storage** — saved in macOS Keychain (or `~/.config` on Linux)
- **Multiple models** — choose between Gemini 3 Flash and Gemini 2.5 Flash

## Requirements

- Python 3.10+
- [uv](https://docs.astral.sh/uv/) (recommended) or pip
- ffmpeg

```bash
# macOS
brew install ffmpeg uv

# Linux
sudo apt install ffmpeg
curl -LsSf https://astral.sh/uv/install.sh | sh
```

## Quick Start

```bash
git clone https://github.com/marcelwachter/transcribe.git
cd transcribe
uv run python transcribe.py
```

That's it. The app walks you through everything:

1. **API key** — paste your Gemini key once, it's saved to Keychain for next time
2. **Model** — pick Gemini 3 Flash (recommended) or 2.5 Flash
3. **Audio file** — enter a path or drag & drop
4. **Transcription** — automatic splitting + parallel processing for long files
5. **Speaker names** — see who said what, play audio samples, assign real names
6. **Save** — press Enter to save with a default filename

## Usage

```bash
# Interactive (recommended)
uv run python transcribe.py

# With arguments
uv run python transcribe.py recording.mp3
uv run python transcribe.py -k YOUR_KEY interview.m4a -o notes.txt
uv run python transcribe.py -t recording.mp3   # include timestamps
```

## Options

| Flag | Description |
|------|-------------|
| `-k`, `--api-key` | Gemini API key |
| `-o`, `--output` | Save transcript to file |
| `-m`, `--model` | Model name |
| `-t`, `--timestamps` | Include `[MM:SS]` timestamps in output |
| `--no-speakers` | Skip speaker name assignment |
| `--reset-key` | Remove saved API key |

## API Key

Your Gemini API key is resolved in this order:

1. `-k` flag
2. `GEMINI_API_KEY` or `GOOGLE_API_KEY` environment variable
3. Saved key (macOS Keychain / Linux `~/.config/transcribe/key`)
4. Interactive prompt (with option to save)

Get a free API key at [aistudio.google.com](https://aistudio.google.com/apikey).

## How It Works

1. Audio is analyzed with `ffprobe` for duration
2. Files over 10 minutes are split into chunks with `ffmpeg`
3. All chunks are uploaded to Gemini in parallel
4. All chunks are transcribed in parallel (with rate limit retry)
5. Timestamps are offset and merged into a single transcript
6. Speakers are detected and you can assign names (with audio preview)

## License

MIT
