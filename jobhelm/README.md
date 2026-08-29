# JobHelm

**A local-first job-search command center.** One screen to see your whole pipeline, track prep readiness, rehearse interviews, draft recruiter replies, and decide the next best action — running on your machine, on your API key. No cloud account, no telemetry, your data never leaves your device.

JobHelm is a companion dashboard for [career-ops](https://github.com/santifer/career-ops) (the local job-search pipeline) and [DeskMock](https://github.com/sundarshankar/deskmock) (local mock interviews). It reads your career-ops data, drives its scripts, and adds an interactive cockpit on top.

```
Kanban board  →  drill into a role  →  prep readiness + gaps
   ↑ scan / apply / draft replies        ↓ rehearse (in-app or DeskMock) → scorecard
```

## What it does

- **Pipeline board** — Kanban of every application by stage (To apply → Applied → In touch → Interview → Offer), with search + filters
- **Prep readiness** — honest per-role readiness based on *actual practice* (a mock rep with a scorecard), with prep materials tracked separately
- **In-app rehearse** — a full mock interview in the browser: questions read aloud (browser TTS), you type or dictate, feedback + scorecard, saved as a transcript. No Terminal, no extra tooling.
- **Discover** — turns the scan firehose into a ranked shortlist of relevant, recent, in-location roles. Coverage spans **ATS at the source** (Greenhouse, Lever, Ashby, Workday, iCIMS — via career-ops) **plus the consumer aggregators** (LinkedIn, Indeed, Glassdoor, ZipRecruiter, Google — via [JobSpy](https://github.com/speedyapply/JobSpy), when its venv is set up) and niche remote boards. The 🔎 Scan button runs both. See the [roadmap](../ROADMAP.md) for the full 360° vision.
- **Draft replies** — draft recruiter/HM responses in your voice with your contact details; flags spam; never auto-sends
- **Prep packs** — per role, a JD-specific set of **interview questions + model answers**, a **leadership brief**, and a **stack/articles guide**, all grounded in your CV and combined into one printable doc. Company briefs too. A metric-trace check flags any number in the pack that isn't in your CV, so you never rehearse an inflated figure.
- **Next best actions** — follow-ups due, prep gaps, warm-path reminders, at a glance

Everything that calls an LLM (drafts, briefs, question sets, in-app rehearse) uses **any OpenAI-compatible endpoint** on your own key.

## Setup

JobHelm is a single Python script — **no dependencies to install** (standard library only). It ships inside the [DeskMock](https://github.com/sundarshankar/deskmock) repo as `jobhelm/`, so one clone gives you both tools.

> **Node** is only needed later, for the optional **Scan** and **Mark applied** actions (they shell out to career-ops `node` scripts) and for `clean-markers.mjs`. You don't need it to run the demo board or rehearse.

**1. Get the code**

```bash
git clone https://github.com/sundarshankar/deskmock.git
cd deskmock
```

**2. Check Python** (3.9 or newer)

```bash
python3 --version
```

**3. Run it — with the bundled sample data**

```bash
python3 jobhelm/mission-control.py
```

It opens `http://localhost:8899` to a populated **demo board** so you can click around immediately — no key or data required just to look.

**4. Add an LLM key** (for drafts, briefs, question sets, and in-app rehearse). The easy default is [OpenRouter](https://openrouter.ai) — create a key, add a few dollars of credit:

```bash
export OPENROUTER_API_KEY=sk-or-your-key-here
python3 jobhelm/mission-control.py
```

(Prefer a local model? It works with any OpenAI-compatible endpoint — the model/base-url are set on the LLM side.)

**5. Point it at your own job search.** JobHelm reads a [career-ops](https://github.com/santifer/career-ops) checkout (your `applications.md`, `pipeline.md`, `cv.md`, etc.) and, optionally, a [DeskMock](https://github.com/sundarshankar/deskmock) checkout for the Terminal rehearse:

```bash
export JOBHELM_CAREEROPS=/path/to/your/career-ops
export JOBHELM_MOCK=/path/to/your/deskmock      # optional (Terminal voice rehearse)
export JOBHELM_NAME="Your Name"                  # used in reply drafts
export JOBHELM_MOBILE="555-0100"                 # optional
export JOBHELM_EMAIL="you@example.com"           # optional
export JOBHELM_PROFILE="a platform engineering leader targeting Sr Director/VP roles (remote)"
python3 jobhelm/mission-control.py
```

See [`config.example`](config.example) for all variables.

### Docker

```bash
export OPENROUTER_API_KEY=sk-or-...
docker compose up --build          # then open http://localhost:8899
```

Runs the sample board by default. Mount your own career-ops / DeskMock and set `JOBHELM_*` in `docker-compose.yml` to use real data. **Host-only actions** (DeskMock Terminal voice, macOS `open`, and the career-ops `node scan.mjs` / `set-status.mjs` shell-outs) run on the host, not in the container; the board, in-app rehearse, drafts, and briefs all work inside it.

## Configuration

| Variable | Default | Meaning |
|----------|---------|---------|
| `JOBHELM_CAREEROPS` | bundled `sample-data` | path to your career-ops checkout (your data) |
| `JOBHELM_MOCK` | `sample-data/mock` | path to your DeskMock checkout (Terminal rehearse) |
| `JOBHELM_NAME` / `JOBHELM_MOBILE` / `JOBHELM_EMAIL` | empty | your identity, used in reply drafts (or set them in `career-ops/config/profile.yml`) |
| `JOBHELM_PROFILE` | `a candidate` | one line describing the roles you target — steers reply drafts |
| `JOBHELM_LOCATIONS` | `remote\|anywhere\|united states\|us` | regex of location keywords to keep in Discover |
| `JOBHELM_PORT` / `JOBHELM_HOST` | `8899` / `127.0.0.1` | where the server binds |
| `JOBHELM_JOBSPY` | — | path to a Python with [JobSpy](https://github.com/speedyapply/JobSpy) installed (`pip install python-jobspy`); enables the LinkedIn/Indeed/etc. leg of 🔎 Scan |
| `OPENROUTER_API_KEY` / `JOBHELM_API_KEY` | — | your LLM key (or drop an `openrouter.env` next to the script) |
| `JOBHELM_BASE_URL` | OpenRouter | any **OpenAI-compatible** API base (Gemini free tier, OpenAI, Ollama/vLLM). Model IDs must match that provider. |
| `JOBHELM_MODEL` / `JOBHELM_RESUME_MODEL` | deepseek / gpt-4o-mini | model per task — cheap default for bulk prep, a stronger one for résumés |

### Use a different provider (e.g. Gemini's free tier)

JobHelm speaks any OpenAI-compatible endpoint. To run it **free** on Google's Gemini (grab a key at [aistudio.google.com](https://aistudio.google.com)):

```bash
export JOBHELM_BASE_URL="https://generativelanguage.googleapis.com/v1beta/openai"
export JOBHELM_API_KEY="your-gemini-key"
export JOBHELM_MODEL="gemini-2.5-flash"
export JOBHELM_RESUME_MODEL="gemini-2.5-flash"
python3 jobhelm/mission-control.py
```

The default stays OpenRouter (`deepseek-v3.2`). Live-article web search is an OpenRouter feature; on other providers the articles guide falls back to search-queries.

## How it relates to career-ops and DeskMock

- **[career-ops](https://github.com/santifer/career-ops)** is the engine: it scans boards, scores roles against your CV, and stores your pipeline as plain files. JobHelm reads those files and calls its scripts — it is a **view + control layer**, not a replacement.
- **[DeskMock](https://github.com/sundarshankar/deskmock)** is the local voice mock interviewer. JobHelm's "Voice (Terminal)" rehearse launches it; the "Rehearse here" button is an in-app text/voice version that saves a compatible transcript.

Run JobHelm on its own for the demo, or with career-ops (+ DeskMock) for your real search.

## Staying current with the base tools

[career-ops](https://github.com/santifer/career-ops) and [DeepInterview](https://github.com/ngoanpv/DeepInterview) are the upstream "base" projects this stack builds on. To pull the latest into your clone:

**If you cloned upstream directly** — just:

```bash
git pull
```

**If you forked it** (e.g. to contribute back) — add the original as `upstream` once, then fast-forward your `main`:

```bash
# one-time: point "upstream" at the original repo
git remote add upstream https://github.com/santifer/career-ops.git       # career-ops
# git remote add upstream https://github.com/ngoanpv/DeepInterview.git    # DeepInterview

git fetch upstream
git checkout main
git merge --ff-only upstream/main     # clean fast-forward; stops if your main diverged
git push origin main                  # update your fork
```

Keep your own feature/PR branches untouched — rebase them onto the new `main` only when you want the PR refreshed.

career-ops also ships a **built-in updater that never touches your data**:

```bash
node update-system.mjs check          # is an update available?
node update-system.mjs apply          # apply it (your cv.md, tracker, etc. are preserved)
```

> **Heads-up:** an update reverts **system files** (e.g. the résumé `templates/`). Keep customizations in the **user layer** (`config/profile.yml`, `modes/_custom.md`) so they survive — or re-apply template tweaks after updating.

## Troubleshooting

| Symptom | Fix |
|---------|-----|
| Board is empty | You pointed `JOBHELM_CAREEROPS` at a folder with no `data/applications.md`. Unset it to see the sample board, or point at a real career-ops checkout. |
| Drafts / briefs / rehearse say "No key" | `export OPENROUTER_API_KEY=sk-or-...` in the same shell, or drop an `openrouter.env` file next to the script. |
| Scan / "Mark applied" fail | Those shell out to career-ops (`node scan.mjs`, `set-status.mjs`). They need a real career-ops checkout with Node installed — they don't run against the sample data or inside the container. |
| Rehearse "Voice (Terminal)" does nothing | That launches DeskMock; set `JOBHELM_MOCK` to your DeskMock checkout. Or use **Rehearse here** (in-app), which needs no extra setup. |
| Port already in use | `JOBHELM_PORT=8900 python3 jobhelm/mission-control.py` |

## Tests

```bash
python3 jobhelm/test_mission_control.py      # runs against the bundled sample data, no network
```

## Credits

Built on and alongside excellent local-first work:
[career-ops](https://github.com/santifer/career-ops) (job-search automation) and
[DeskMock](https://github.com/sundarshankar/deskmock) (local mock interviews).
Integrates [JobSpy](https://github.com/speedyapply/JobSpy) (board aggregators) and
[OpenRouter](https://openrouter.ai) / [DeepSeek](https://www.deepseek.com) for the LLM layer;
[Playwright](https://playwright.dev) + [pdf-lib](https://github.com/Hopding/pdf-lib) for clean PDFs.
Prep-resource links credit their maintainers — see [PREP-RESOURCES.md](../PREP-RESOURCES.md).
JobHelm doesn't fork or bundle the inspiration projects' code — it's an independent dashboard that pairs with them.

## Disclaimer

AI-assisted — review and verify everything; you are responsible for the truthfulness of your résumé. Not legal, financial, or career advice; comp figures are estimates. LLM features send your text to the AI provider you configure. See [DISCLAIMER.md](../DISCLAIMER.md).

## License

MIT — see [LICENSE](LICENSE).
