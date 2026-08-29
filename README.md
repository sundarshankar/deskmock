# DeskMock

**Local-first AI mock interviews, right from your desk.** On-device voice, no cloud voice service, no LiveKit, no GPU.

> **This repo is a small local-first job-search toolkit:** **DeskMock** (the mock interviewer, below) and **[JobHelm](jobhelm/)** — a command center that tracks your whole pipeline, prep readiness, and next actions, with an in-app rehearse. Want the dashboard? Jump to **[jobhelm/](jobhelm/)** — clone and `python3 jobhelm/mission-control.py` for a live demo board.

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

## Setup

DeskMock is a single Python script with **no dependencies to install** (Python standard library only).

**1. Get the code**

```bash
git clone https://github.com/sundarshankar/deskmock.git
cd deskmock
```

**2. Check Python** (3.9 or newer)

```bash
python3 --version
```

**3. Get an LLM key.** DeskMock talks to any OpenAI-compatible API. The easy default is [OpenRouter](https://openrouter.ai):

- Sign up, open **Keys**, and create a key (it starts with `sk-or-`).
- Add a few dollars of credit — a full session on `deepseek-v3.2` costs pennies.
- Export it in the shell you'll run DeskMock from:

  ```bash
  export OPENROUTER_API_KEY=sk-or-your-key-here
  ```

  (Prefer a local model and no key at all? Point DeskMock at it with `--base-url`, e.g. Ollama: `--base-url http://localhost:11434/v1`.)

**4. First run — type your answers, no voice setup needed.** Uses the bundled sample CV + JD so you can try it immediately:

```bash
python3 deskmock.py --clipboard
```

You'll see a question; **type** an answer and press Enter to get feedback and the next question. Type `/done` for your scorecard. That's the whole loop. Add voice later (below).

## Running it

```bash
# your own CV + the real job description
python3 deskmock.py --clipboard --cv my-cv.md --jd role.txt --role "Senior SWE" --company "Acme"

# add read-aloud + hands-free voice once your dictation tool is set up (see Voice setup)
python3 deskmock.py --speak --auto --cv my-cv.md --jd role.txt --role "Senior SWE" --company "Acme"
```

In-session commands: `/done` (scorecard) · `/repeat` (hear the question again) · `/skip`.

### Options

| Flag | Meaning |
|------|---------|
| `--cv <file>` / `--jd <file>` | your CV and the job description (default: bundled samples) |
| `--role` / `--company` | title + company for context |
| `--model <slug>` | LLM model (default `deepseek/deepseek-v3.2` — cheap + reliable JSON) |
| `--base-url <url>` | any OpenAI-compatible endpoint (default OpenRouter) |
| `--speak` | read questions aloud via macOS `say` |
| `--clipboard` | read each answer from the clipboard when you press Enter (recommended) |
| `--auto` | fully hands-free — watch the clipboard and auto-capture your dictation |

## Voice setup (macOS, optional)

**Voice is optional — typing works everywhere.** To go hands-free you need two things:

- **Read-aloud** is built in: add `--speak` and macOS `say` reads each question. Test it first: `say "hello"`.
- **Speaking your answers** needs a dictation tool that turns your speech into text. DeskMock then picks up that text.

Two ways to do the speech-to-text half:

- **macOS Dictation (simplest):** System Settings → Keyboard → **Dictation → On**. It types straight into the Terminal, so you don't even need clipboard mode — run `python3 deskmock.py --speak`, start dictation, speak, then press Enter.
- **[FluidVoice](https://github.com/extrudedawe-dev/fluidvoice) or similar:** set its output mode to **copy-to-clipboard** and pick a hotkey. Then run `python3 deskmock.py --speak --clipboard`, press the hotkey, speak, and press Enter to submit what it copied.

> **Three things that trip people up (all easy):**
> 1. **The dictation app must be running** before you start. If you see `clipboard empty`, the app isn't running or isn't in copy-to-clipboard mode — or just type your answer instead.
> 2. **Some dictation tools hold the keyboard while active.** If pressing Enter does nothing, toggle dictation **off**, click into the Terminal, then press Enter.
> 3. **`--clipboard` is friendlier than `--auto`.** It waits for your Enter so you control the timing; `--auto` is fully hands-free but can fire on stale clipboard text. Start with `--clipboard`.

## Troubleshooting

| Symptom | Cause / fix |
|---------|-------------|
| `clipboard empty — dictate…` on every Enter | Your dictation app isn't running, or isn't in **copy-to-clipboard** mode. Launch it and enable clipboard output — or just **type** your answer and press Enter. |
| Questions aren't read aloud | Make sure you passed `--speak`, your volume is up, and `say "test"` works in a terminal on its own. |
| Pressing Enter doesn't submit | A dictation tool is holding the keyboard. Toggle dictation **off**, click into the Terminal window, then press Enter. |
| `OpenRouter key file not found` or auth errors | `export OPENROUTER_API_KEY=sk-or-...` in the **same** shell you run from, and confirm the key has credit. |
| The echoed answer looks cut off | The `← read N chars` line reports the **full** captured length; the text shown after it is just a preview — your whole answer was captured. |
| `python3: command not found` | Install Python 3.9+ (`brew install python` on macOS) and re-run `python3 --version`. On **Windows** use `python` (not `python3`). |
| JobHelm won't start: `OSError: [Errno 48] Address already in use` | Port 8899 is already taken — usually another JobHelm you left running. Free it, then start again:<br>**macOS/Linux:** `lsof -ti:8899 \| xargs kill -9`<br>**Windows (PowerShell):** `Get-Process -Id (Get-NetTCPConnection -LocalPort 8899).OwningProcess \| Stop-Process -Force`<br>…or just run on another port: `JOBHELM_PORT=8900 python3 jobhelm/mission-control.py`. |
| JobHelm shows the **demo board** (Alex Rivera / Datawright), not your data | You're running it without pointing at your career-ops. Set `JOBHELM_CAREEROPS=/path/to/your/career-ops` (or run from your own career-ops checkout). With no path it defaults to the bundled `sample-data`. |

## Keep your output clean

`clean-markers.mjs` (bundled) audits/strips renderer metadata and hidden Unicode from any files you
generate (e.g. a résumé PDF): `node clean-markers.mjs audit résumé.pdf`.

## JobHelm — the job-search command center

Also in this repo: **[JobHelm](jobhelm/)**, a local-first dashboard that ties the whole search together — a Kanban pipeline board, honest prep-readiness tracking, an **in-app rehearse** (mock interview in the browser, no Terminal needed), recruiter-reply drafting, company briefs, and next-best-actions. It's the command center; DeskMock is the rehearsal engine it drives.

Try it in seconds (ships with sample data, no key or setup needed just to look):

```bash
python3 jobhelm/mission-control.py     # opens http://localhost:8899 with a demo board
```

Full setup, configuration, and how to point it at your own [career-ops](https://github.com/santifer/career-ops) data are in **[jobhelm/README.md](jobhelm/README.md)**.

## Requirements

Python 3.9+ and an OpenAI-compatible API key (`deepseek-v3.2` is pennies per session; any provider works — see JobHelm's config for Gemini's free tier).

**Platform support:** the whole thing — JobHelm's board, prep, résumé tailoring, scanning, and **in-browser rehearsal** — runs on **macOS, Windows, and Linux** (files open with your OS default app on each). The only **macOS-only** extras are DeskMock's read-aloud voice (`--speak`), hands-free dictation (`--auto`), and the Terminal-voice launcher; on Windows/Linux those degrade gracefully — you **type your answers** and rehearse in the browser.

## Contributing

Issues and PRs welcome — see [CONTRIBUTING.md](CONTRIBUTING.md). Good first issues: Linux/Windows support,
a local-Whisper input backend, a `--jd-url` fetcher, and configurable scoring rubrics.

## Credits & inspiration

DeskMock + JobHelm stand on a lot of excellent open-source work — thank you to all of it.

**Inspiration**
- [career-ops](https://github.com/santifer/career-ops) — job-search automation and the local pipeline JobHelm reads and drives
- [DeepInterview](https://github.com/ngoanpv/DeepInterview) — a full voice-avatar mock-interview stack
- Pairs well with local dictation like [FluidVoice](https://github.com/extrudedawe-dev/fluidvoice)

**Integrates / builds on**
- [JobSpy](https://github.com/speedyapply/JobSpy) — LinkedIn/Indeed/Glassdoor/ZipRecruiter/Google scraping (the aggregator leg of 🔎 Scan)
- [OpenRouter](https://openrouter.ai) — default LLM gateway (any OpenAI-compatible endpoint works); [DeepSeek](https://www.deepseek.com) is the default model, and OpenRouter's web plugin powers live article fetch
- [pdf-lib](https://github.com/Hopding/pdf-lib) — metadata stripping in `clean-markers.mjs` (auto-installed on demand)
- macOS built-ins (`say`, dictation, clipboard) for DeskMock's read-aloud voice

**Transitive — via career-ops scripts we shell out to** (Scan, Mark-applied, résumé/prep PDFs):
- [Playwright](https://playwright.dev) — HTML→PDF rendering (`generate-pdf.mjs`)
- [js-yaml](https://github.com/nodeca/js-yaml) — YAML config parsing (`scan.mjs`, `set-status.mjs`)
- career-ops carries its own dependency tree (also `@google/generative-ai`, `dotenv`, …) — see its [package.json](https://github.com/santifer/career-ops/blob/main/package.json). Thanks to all those maintainers too.

**Curated interview-prep resources** — the prep hub points to community lists and guides (full set in [PREP-RESOURCES.md](PREP-RESOURCES.md)). Thanks to their maintainers, including
[dastergon/awesome-sre](https://github.com/dastergon/awesome-sre),
[donnemartin/system-design-primer](https://github.com/donnemartin/system-design-primer),
[ashishps1/awesome-behavioral-interviews](https://github.com/ashishps1/awesome-behavioral-interviews),
[kaushikb9/em-interviews](https://github.com/kaushikb9/em-interviews),
[ronikobrosly/awesome-data-leadership](https://github.com/ronikobrosly/awesome-data-leadership), and others.

DeskMock does not fork or bundle the inspiration projects' code — it's an independent tool. All trademarks and linked resources belong to their respective owners.

## Disclaimer

AI-assisted — review and verify everything; you are responsible for the truthfulness of your résumé. Not legal, financial, or career advice; comp figures are estimates. LLM features send your text to the AI provider you configure. See [DISCLAIMER.md](DISCLAIMER.md).

## License

MIT — see [LICENSE](LICENSE).
