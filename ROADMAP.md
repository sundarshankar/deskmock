# Roadmap — a 360° job-search companion

The goal of this toolkit (DeskMock + JobHelm, on top of career-ops + DeepInterview) is a
**complete, local-first companion for someone in the market for a new role** — cover the whole
journey, not just one slice:

> **scan → match → apply → connect → interview → prepare → negotiate → land.**

Everything stays local-first and on your own API key. No cloud lock-in, no telemetry.

## The pillars

### 1. Find every matching role (widest possible net)
Pull roles from **as many boards as possible — popular *and* unpopular** — and filter to the
candidate's profile.
- **ATS at the source** (career-ops): Greenhouse · Lever · Ashby · Workday · iCIMS — tens of
  thousands of companies.
- **Consumer aggregators** (JobSpy): LinkedIn · Indeed · Glassdoor · ZipRecruiter · Google Jobs.
- **Niche/remote boards**: WeWorkRemotely · Himalayas · TheMuse · Remotive · RemoteOK.
- **Extensible**: wire in any other open-source scanner/GitHub project that surfaces matching roles.

_Status:_ ATS ✅ · JobSpy ✅ (activated — LinkedIn/Indeed/etc. now feed the pipeline) · niche ✅ ·
"any other project" = ongoing.

### 2. Apply & connect
- Assisted apply (pre-fill fields, attach the right résumé; human clicks submit).
- **Customized résumé** per role, ATS-safe and human-clean.
- Recruiter/HM **outreach + reply drafting** (human reviews and sends).

_Status:_ résumé tailoring ✅ · reply drafting ✅ · field-pack/assisted-apply ✅ · deeper connect = ongoing.

### 3. Prepare for the interview (DeskMock as the prep hub)
DeskMock should be the one place that links the whole prep:
- **Leadership / behavioral** round prep.
- **Technical** round prep.
- **Latest articles** on the role's tech stack (scan recent writing to complement prep).
- **Note-taking** per role/round.
- **Technical-question matching** to the JD (per-dimension question sets grounded in the CV).
- **Flashcards for key concepts** ✅ — SM-2 spaced repetition, per-viewer.
- **Behavioral Story Bank** ✅ — your real STAR stories once, mapped to any question.
- **Adaptive readiness** ✅ — rehearse → per-dimension score → drill the weakest.
- **Curated resource library** per dimension (leadership · technical/SRE · system design · behavioral) — see [PREP-RESOURCES.md](PREP-RESOURCES.md); auto-link the matching ones per JD.
- **DeepInterview** for structured prep + **DeskMock** voice/in-app rehearsal with a scorecard.
- Honest **readiness** driven by real practice, never artifact existence.

_Status:_ mock rehearsal ✅ (in-app + Terminal) · readiness ✅ · question sets ✅ ·
leadership/technical/articles/notes hub = **next**.

### 4. Negotiate & decide
- Salary/comp benchmarking and negotiation talking points (grounded in real achievements).
- Offer walk-through before signing.

_Status:_ career-ops has `negotiation-roi` / `offer-prep` groundwork — surface it in JobHelm = ongoing.

## Principles
- **Truthful, always** — résumés and prep reflect only what the candidate actually did; never inflate.
- **Local-first & private** — data stays on the candidate's machine, on their own key.
- **Human in the loop** — nothing is submitted or sent without the candidate's review.
- **Open-source give-back** — reusable engines are public; personal data stays private.
