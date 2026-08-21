#!/usr/bin/env python3
"""
DeskMock — private AI mock interviews, right from your desk.

A local-first CLI mock interviewer: you speak your answers with any desktop dictation tool
(local speech-to-text), an OpenAI-compatible LLM plays the interviewer and coaches you, and
macOS `say` reads the questions aloud. No cloud voice service, no LiveKit, no GPU.

Audio never leaves your machine — only text reaches the LLM.

Quick start:
  export OPENROUTER_API_KEY=sk-or-...        # or any OpenAI-compatible key
  python3 deskmock.py --speak --auto         # uses sample-cv.md + sample-jd.txt

Options:
  --cv <file>        your CV/résumé in markdown/text (default: sample-cv.md)
  --jd <file>        the job description (default: sample-jd.txt)
  --role "..."       role title      --company "..."   company name
  --model <slug>     LLM model (default: deepseek/deepseek-v3.2)
  --base-url <url>   OpenAI-compatible endpoint (default: https://openrouter.ai/api/v1)
  --speak            read questions aloud with macOS `say`
  --auto             hands-free: watch the clipboard and auto-capture dictation
  --clipboard        read each answer from the clipboard on Enter

In-interview: type/speak your answer; commands: /done  /skip  /repeat  /help
"""
import argparse, json, os, subprocess, sys, urllib.request, urllib.error, datetime, pathlib

HERE = pathlib.Path(__file__).resolve().parent

def load_key(base_url):
    # env var wins, then ./openrouter.env, then ~/.deskmock/key
    for src in (os.environ.get("OPENROUTER_API_KEY"), os.environ.get("OPENAI_API_KEY")):
        if src: return src.strip()
    for p in (HERE / "openrouter.env", pathlib.Path.home() / ".deskmock" / "key"):
        if p.exists(): return p.read_text().strip()
    sys.exit("No API key. Set OPENROUTER_API_KEY (or OPENAI_API_KEY), or create ./openrouter.env "
             f"with your key for {base_url}.")

def read_text(p, limit=None):
    try:
        t = pathlib.Path(p).read_text()
        return t[:limit] if limit else t
    except Exception as e:
        return f"(could not read {p}: {e})"

def llm(messages, key, model, base_url, temperature=0.6, max_tokens=1200):
    body = json.dumps({"model": model, "messages": messages,
                       "temperature": temperature, "max_tokens": max_tokens}).encode()
    req = urllib.request.Request(f"{base_url.rstrip('/')}/chat/completions", data=body,
        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json",
                 "HTTP-Referer": "https://github.com/deskmock", "X-Title": "DeskMock"})
    try:
        with urllib.request.urlopen(req, timeout=120) as r:
            return json.load(r)["choices"][0]["message"]["content"].strip()
    except urllib.error.HTTPError as e:
        return f"[LLM error {e.code}] {e.read().decode()[:200]}"
    except Exception as e:
        return f"[LLM error] {e!r}"

def speak(text):
    line = text
    for l in text.splitlines():
        if l.strip().upper().startswith("QUESTION:"):
            line = l.split(":", 1)[1].strip(); break
    try: subprocess.Popen(["say", "-r", "185", line])
    except Exception: pass

def read_answer(auto, clipboard):
    if auto:
        import time
        def clip():
            try: return subprocess.run(["pbpaste"], capture_output=True, text=True).stdout
            except Exception: return ""
        baseline = clip()
        print("\n\033[2m🎙  dictate now (copy-to-clipboard) — say “done” to finish. Capturing…\033[0m", flush=True)
        last, stable = None, 0
        while True:
            time.sleep(1.0); cur = clip()
            if cur.strip() and cur != baseline:
                if cur == last: stable += 1
                else: stable, last = 0, cur
                if stable >= 1:
                    t = cur.strip(); low = t.lower().strip(" .,!?")
                    if low in ("done","stop","finish","i am done","i'm done"): return "/done"
                    if low in ("skip","next","next question"): return "/skip"
                    if low in ("repeat","again"): return "/repeat"
                    print(f"\033[2m  ← captured {len(t)} chars\033[0m"); return t
    if clipboard:
        try: subprocess.run(["pbcopy"], input=b"")
        except Exception: pass
        print("\n\033[2m(dictate → clipboard, then Enter to submit; or type /done)\033[0m")
    while True:
        print("\033[1mYou ▸\033[0m ", end="", flush=True)
        try: line = input()
        except EOFError: return "/done"
        line = line.strip()
        if line in ("/done","/skip","/repeat","/help"): return line
        if clipboard and line == "":
            try: clip = subprocess.run(["pbpaste"], capture_output=True, text=True).stdout.strip()
            except Exception: clip = ""
            if clip: print(f"\033[2m  ← {len(clip)} chars from clipboard\033[0m"); return clip
            print("\033[33m  clipboard empty — dictate, then Enter\033[0m"); continue
        if line: return line

SYSTEM = """You are a seasoned, friendly-but-rigorous interviewer for the role below. Interview ONE
question at a time — never dump multiple. First turn: a one-line intro, then question 1. After EACH answer:
(1) FEEDBACK — 2-3 crisp bullets on clarity, structure (STAR?), and specificity; (2) POWER PHRASES — 2
sharper expressions the candidate could have used; (3) the next question, escalating depth and probing gaps.
Keep it under ~180 words. End every turn with the question on its own final line prefixed "QUESTION:".
Ground everything in the candidate's CV and the role below; never invent facts about the candidate.
When the candidate says the interview is over, produce a SCORECARD: scores 1-10 for Clarity, Structure,
Specificity, Technical depth, Executive presence; 3 strongest moments; 3 things to fix; 8 power phrases."""

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--role", default="the target role")
    ap.add_argument("--company", default="the company")
    ap.add_argument("--cv", default=str(HERE / "sample-cv.md"))
    ap.add_argument("--jd", default=str(HERE / "sample-jd.txt"))
    ap.add_argument("--model", default="deepseek/deepseek-v3.2")
    ap.add_argument("--base-url", default="https://openrouter.ai/api/v1")
    ap.add_argument("--speak", action="store_true")
    ap.add_argument("--auto", action="store_true")
    ap.add_argument("--clipboard", action="store_true")
    args = ap.parse_args()

    key = load_key(args.base_url)
    cv = read_text(args.cv, 6000)
    jd = read_text(args.jd, 4000)
    ctx = f"ROLE: {args.role} at {args.company}\n\n=== CANDIDATE CV ===\n{cv}\n\n=== JOB DESCRIPTION ===\n{jd}\n"
    messages = [{"role": "system", "content": SYSTEM + "\n\n" + ctx}]

    banner = f"  DeskMock — {args.role} @ {args.company}  "
    print("\n\033[1;36m" + "="*len(banner) + f"\n{banner}\n" + "="*len(banner) + "\033[0m")
    print(f"\033[2mmodel={args.model}  speak={'on' if args.speak else 'off'}  "
          f"input={'auto' if args.auto else 'clipboard' if args.clipboard else 'type'}\033[0m")

    messages.append({"role": "user", "content": "Begin the interview. Intro + question 1 only."})
    turn = llm(messages, key, args.model, args.base_url)
    transcript = []
    while True:
        print(f"\n\033[1;33mInterviewer ▸\033[0m {turn}")
        if args.speak: speak(turn)
        messages.append({"role": "assistant", "content": turn}); transcript.append(("Interviewer", turn))
        ans = read_answer(args.auto, args.clipboard)
        if ans == "/help":
            print("\033[2m/done finish+scorecard · /skip next · /repeat re-ask\033[0m"); continue
        if ans == "/repeat":
            if args.speak: speak(turn)
            continue
        if ans == "/skip":
            messages.append({"role": "user", "content": "(skip — next question)"})
            turn = llm(messages, key, args.model, args.base_url); continue
        if ans == "/done":
            messages.append({"role": "user", "content": "The interview is over. Give me the SCORECARD now."})
            card = llm(messages, key, args.model, args.base_url)
            print(f"\n\033[1;32m===== SCORECARD =====\033[0m\n{card}\n"); transcript.append(("SCORECARD", card)); break
        print("\033[2m… evaluating\033[0m"); transcript.append(("You", ans))
        messages.append({"role": "user", "content": ans})
        turn = llm(messages, key, args.model, args.base_url)

    out = HERE / "transcripts"
    out.mkdir(exist_ok=True)
    ts = datetime.datetime.now().strftime("%Y-%m-%d-%H%M")
    (out / f"{ts}.md").write_text("\n\n".join(f"**{w}:** {t}" for w, t in transcript))
    print(f"\033[2mTranscript saved: {out / (ts + '.md')}\033[0m")

if __name__ == "__main__":
    main()
