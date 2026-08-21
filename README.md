# DeskMock

**Local-first AI mock interviews, right from your desk.** On-device voice, no cloud voice service, no LiveKit, no GPU.

DeskMock is a local-first CLI mock interviewer. You **speak your answers** using a local dictation tool
(on-device speech-to-text), an **OpenAI-compatible LLM** (cloud or local) plays a rigorous interviewer and
coaches you, and macOS **`say`** reads the questions aloud. Your **voice never leaves your machine** — only
text reaches the model, on *your* API key. Point the model at a local endpoint (vLLM/Ollama) and nothing
leaves your machine at all.

```
🔊  Interviewer reads a question aloud
🎙  You speak your answer (local dictation → clipboard)
🤖  Feedback + power phrases + the next question
🔁  …on a loop, then a scorecard
```

## Why

Most AI mock-interview tools are cloud SaaS or require a real-time voice stack (LiveKit + cloud STT/TTS,
often a GPU). That excludes anyone who is **privacy-conscious, offline-ish, or on modest hardware**.
DeskMock takes the opposite stance: **keep the voice local, keep the brain swappable, keep it a single
command.** It works with any OpenAI-compatible endpoint (OpenRouter, a local vLLM/Ollama, etc.).

## Quick start

```bash
export OPENROUTER_API_KEY=sk-or-...          # or any OpenAI-compatible key
python3 deskmock.py --speak --auto           # uses the bundled sample CV + JD
# or with your own:
python3 deskmock.py --speak --auto --cv my-cv.md --jd role.txt --role "Senior SWE" --company "Acme"
```

Type your answers, or go voice with `--auto` (see below). Commands in-session: `/done` `/skip` `/repeat`.

### Options

| Flag | Meaning |
|------|---------|
| `--cv <file>` / `--jd <file>` | your CV and the job description (default: bundled samples) |
| `--role` / `--company` | title + company for context |
| `--model <slug>` | LLM model (default `deepseek/deepseek-v3.2` — cheap + reliable JSON) |
| `--base-url <url>` | any OpenAI-compatible endpoint (default OpenRouter) |
| `--speak` | read questions aloud via macOS `say` |
| `--auto` | hands-free — watch the clipboard and auto-capture your dictation |
| `--clipboard` | read each answer from the clipboard on Enter |

## Voice setup (macOS)

The voice half is just two local pieces plus your dictation tool:

1. **A desktop dictation tool** that can **copy transcribed text to the clipboard** — e.g.
   [FluidVoice](https://github.com/extrudedawe-dev/fluidvoice) (local, on-device) or macOS Dictation.
   Set its output mode to **copy-to-clipboard** and pick a hotkey.
2. **macOS `say`** (built in) reads questions aloud via `--speak`.

Then run `python3 deskmock.py --speak --auto`, press your dictation hotkey, speak your answer, and DeskMock
auto-captures it from the clipboard. Say **“done”** to finish and get a scorecard.

## Keep your output clean

`clean-markers.mjs` (bundled) audits/strips renderer metadata and hidden Unicode from any files you
generate (e.g. a résumé PDF): `node clean-markers.mjs audit résumé.pdf`.

## Requirements

Python 3.9+, macOS for `--speak`/`--auto` (Linux/Windows contributions welcome — see below). An
OpenAI-compatible API key with a little credit (`deepseek-v3.2` is pennies per session).

## Contributing

Issues and PRs welcome — see [CONTRIBUTING.md](CONTRIBUTING.md). Good first issues: Linux/Windows support,
a local-Whisper input backend, a `--jd-url` fetcher, and configurable scoring rubrics.

## Credits & inspiration

Built as a local-first alternative after working with two excellent projects — inspired by
[career-ops](https://github.com/santifer/career-ops) (job-search automation) and
[DeepInterview](https://github.com/ngoanpv/DeepInterview) (a full voice-avatar mock-interview stack),
and pairs well with local dictation tools like [FluidVoice](https://github.com/extrudedawe-dev/fluidvoice).
DeskMock does not fork or bundle their code; it's an independent tool with its own focus.

## License

MIT — see [LICENSE](LICENSE).
