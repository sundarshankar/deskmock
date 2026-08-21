# Contributing to DeskMock

Thanks for your interest! DeskMock is a small, focused tool — a **local-first CLI mock interviewer**.
Contributions that keep it simple, private, and dependency-light are very welcome.

## Ways to help

- **Bugs / ideas** — open an issue with steps to reproduce or a clear proposal.
- **Pull requests** — fork, branch, and open a PR against `main` with a short description.

## Good first issues

- **Linux / Windows support** — the voice path (`--speak`, `--auto`) is macOS-only today (`say` + `pbpaste`).
  Add `espeak`/`spd-say` for TTS and `xclip`/`wl-paste`/`clip` for the clipboard, selected by platform.
- **Local Whisper input backend** — an alternative to clipboard capture that transcribes mic audio locally.
- **`--jd-url`** — fetch a job description from a URL instead of a file.
- **Configurable rubric** — let users supply their own scorecard dimensions.
- **Tests** — a small harness that mocks the LLM call and checks the interview loop.

## Guidelines

- **Keep it dependency-light.** The core is Python stdlib; avoid heavy deps unless clearly worth it.
- **Privacy first.** Never send audio off-device; only text should reach the model.
- **No secrets in the repo.** Keys come from env vars or a gitignored file, never committed.
- Match the existing style; keep changes focused and small where possible.

## Local dev

```bash
export OPENROUTER_API_KEY=sk-or-...
python3 deskmock.py --cv sample-cv.md --jd sample-jd.txt   # type answers, no voice needed to test
```

By contributing, you agree your contributions are licensed under the project's MIT License.
