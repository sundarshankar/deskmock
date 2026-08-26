#!/usr/bin/env python3
"""
JobHelm · Mission Control — a local, interactive command center for the whole job search.

Live view of your pipeline + prep readiness + next-best-actions, with buttons that actually DO things
(scan, mark applied, rehearse a mock). Zero dependencies (Python stdlib only), runs locally, uses your
existing career-ops scripts + DeskMock under the hood.

    python3 mission-control.py        # then open http://localhost:8899
"""
import os, re, glob, json, html, shlex, subprocess, pathlib, datetime, webbrowser, threading
import urllib.request, urllib.error
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

HERE = pathlib.Path(__file__).resolve().parent

# --- your identity (used when drafting replies + resolving your profile) ---
# Set via env, or via <career-ops>/config/profile.yml (name/email/phone/...).
NAME    = os.environ.get("JOBHELM_NAME", "")
MOBILE  = os.environ.get("JOBHELM_MOBILE", "")
EMAIL   = os.environ.get("JOBHELM_EMAIL", "")
# One line describing the roles you target — steers reply drafts. e.g.
# "a platform engineering leader targeting Sr Director/VP roles (remote, $200K+)".
PROFILE = os.environ.get("JOBHELM_PROFILE", "a candidate")
# Pipe-separated location keywords your search should keep (regex, case-insensitive).
LOCATIONS = os.environ.get("JOBHELM_LOCATIONS", r"remote|anywhere|united states|\bus\b|\bu\.s")

HOME = pathlib.Path.home()
# Point these at your own career-ops / DeskMock checkouts.
# Default: the bundled sample data, so it boots to a populated demo board.
CO   = pathlib.Path(os.environ.get("JOBHELM_CAREEROPS", str(HERE / "sample-data")))
MOCK = pathlib.Path(os.environ.get("JOBHELM_MOCK", str(HERE / "sample-data" / "mock")))
PORT = int(os.environ.get("JOBHELM_PORT", "8899"))
HOST = os.environ.get("JOBHELM_HOST", "127.0.0.1")   # Docker sets 0.0.0.0

def read(p):
    try: return pathlib.Path(p).read_text(errors="ignore")
    except Exception: return ""
def slug(s): return re.sub(r"[^a-z0-9]", "", (s or "").lower())

# ---------- data layer ----------
def apps():
    out = []
    for line in read(CO / "data/applications.md").splitlines():
        if not re.match(r"^\|\s*\d+\s*\|", line): continue
        c = [x.strip() for x in line.strip().strip("|").split("|")]
        if len(c) < 6: continue
        out.append(dict(num=c[0], date=c[1], company=c[2], role=c[3], score=c[4], status=c[5],
                        report=(c[7] if len(c)>7 else ""), notes=(c[8] if len(c)>8 else "")))
    return out

def report_file(a):
    m=re.search(r"\(([^)]*reports/([^)]+\.md))\)", a.get("report",""))
    return m.group(2) if m else ""

_AGG=("linkedin.","indeed.","glassdoor.","ziprecruiter.","himalayas.","weworkremotely.","dice.","monster.")
def _is_company(u): return bool(u) and not any(a in u.lower() for a in _AGG)
def saved_jd(a):
    k=slug(a["company"])
    for f in glob.glob(str(CO/"jds/*")):
        if k and k in slug(pathlib.Path(f).name): return pathlib.Path(f).name
    return ""
def jd_url(a):
    # PREFER the company/ATS link over 3rd-party aggregators (LinkedIn/Indeed), which expire fast.
    urls=[]
    rf=report_file(a)
    if rf:
        m=re.search(r"\*\*URL:\*\*\s*(\S+)", read(CO/"reports"/rf))
        if m and m.group(1).startswith("http"): urls.append(m.group(1))
    sj=saved_jd(a)
    if sj: urls += re.findall(r"https?://\S+", read(CO/"jds"/sj))
    urls += re.findall(r"https?://\S+", a.get("notes",""))
    urls=[u.rstrip(".,;)") for u in urls]
    company=[u for u in urls if _is_company(u)]
    return company[0] if company else (urls[0] if urls else "")

def _ignored_rows():
    out=[]
    for ln in read(CO/"data/jobhelm-ignored.tsv").splitlines():
        c=ln.split("\t")
        if c and c[0].startswith("http"):
            out.append(dict(url=c[0], posted=(c[1] if len(c)>1 else ""),
                            company=(c[2] if len(c)>2 else ""), title=(c[3] if len(c)>3 else "")))
    return out
def _ignored_set(): return {r["url"].split("?")[0] for r in _ignored_rows()}
def do_ignore(url, company="", title="", posted=""):
    if not (url or "").startswith("http"): return dict(ok=False, msg="No URL to ignore.")
    if url.split("?")[0] in _ignored_set(): return dict(ok=True, msg="Already ignored.")
    with (CO/"data/jobhelm-ignored.tsv").open("a") as f: f.write(f"{url}\t{posted}\t{company}\t{title}\n")
    return dict(ok=True, msg=f"Ignored{(' — '+company) if company else ''}.")
def do_unignore(url):
    p=CO/"data/jobhelm-ignored.tsv"
    if not p.exists(): return dict(ok=True, msg="ok")
    key=(url or "").split("?")[0]
    lines=[ln for ln in read(p).splitlines() if ln.strip() and ln.split("\t")[0].split("?")[0]!=key]
    p.write_text("\n".join(lines)+("\n" if lines else ""))
    return dict(ok=True, msg="Restored to Discover.")

def pipeline_recent():
    POS = re.compile(r'\b(director|vp|vice president|head|sr\.? director|senior director)\b', re.I)
    DOM = re.compile(r'\b(cloud|platform|infrastructure|infra|sre|reliability|devops|engineering|technolog)', re.I)
    NEG = re.compile(r'\b(sales|account|market|pharma|clinical|medical|nurse|regulatory|manufactur|product manage|program manage|design|ux|human resources|field|scientist|therap|supply chain)\b', re.I)
    LOC = re.compile(LOCATIONS, re.I)
    rows = []
    for line in read(CO / "data/pipeline.md").splitlines():
        if not line.strip().startswith("- [ ]"): continue
        parts = [p.strip() for p in line.split("]",1)[1].split("|")]
        url = parts[0] if parts else ""; company = parts[1] if len(parts)>1 else ""
        title = parts[2] if len(parts)>2 else ""; loc = parts[3] if len(parts)>3 else ""
        mp = re.search(r"posted:\s*([\d-]+)", line); posted = mp.group(1) if mp else ""
        if POS.search(title) and DOM.search(title) and not NEG.search(title) and ((not loc) or LOC.search(loc)):
            rows.append(dict(url=url, company=company, title=title, posted=posted))
    def d(s):
        try: return datetime.date.fromisoformat(s)
        except Exception: return None
    ds = [d(r["posted"]) for r in rows if d(r["posted"])]
    mx = max(ds) if ds else None
    rows = [r for r in rows if d(r["posted"]) and mx and (mx-d(r["posted"])).days <= 7]
    ign = _ignored_set()
    rows = [r for r in rows if r["url"].split("?")[0] not in ign]
    return sorted(rows, key=lambda r: r["posted"], reverse=True)[:40]

prep_files = [pathlib.Path(f).name for f in glob.glob(str(CO / "interview-prep/*.md"))]
def pack(co):  k=slug(co); return next((f for f in prep_files if k and k in slug(f) and "gap" not in f.lower() and "question" not in f.lower()), "")
def gapd(co):  k=slug(co); return next((f for f in prep_files if k and k in slug(f) and "gap" in f.lower()), "")
def n_mock():  return len(glob.glob(str(MOCK / "transcripts/*.md")))

_BOARD_NAMES={"workday":"Workday","icims":"iCIMS","greenhouse":"Greenhouse","lever":"Lever",
              "ashby":"Ashby","weworkremotely":"WeWorkRemotely","himalayas":"Himalayas",
              "remoteok":"RemoteOK","jobspy":"JobSpy"}
def scan_coverage():
    # last run from scan-runs.tsv; board coverage from scan-history.tsv col 3 (source)
    last={}
    runs=[l for l in read(CO/"data/scan-runs.tsv").splitlines() if l.strip() and not l.startswith("timestamp")]
    def g(c,i): return (c[i] if len(c)>i and c[i].strip() else "0")
    if runs:
        c=runs[-1].split("\t")
        last=dict(when=(c[0][:16].replace("T"," ") if c else ""), status=g(c,1),
                  companies=g(c,2), boards=g(c,3), found=g(c,4),
                  f_title=g(c,5), f_location=g(c,7), f_age=g(c,8), f_salary=g(c,9),
                  dupes=g(c,12), new=g(c,13), errors=g(c,14), f_blacklist=g(c,15))
        try:
            last["skipped"]=str(sum(int(g(c,i)) for i in (5,6,7,8,9,10,11,15,16,17,18)))
        except Exception: last["skipped"]="0"
    cnt={}
    for l in read(CO/"data/scan-history.tsv").splitlines():
        c=l.split("\t")
        if len(c)<3 or not c[2].strip(): continue
        src=re.sub(r"-(full|api)$","",c[2].strip().lower())
        cnt[src]=cnt.get(src,0)+1
    top=[dict(board=_BOARD_NAMES.get(k,k.replace("-"," ").title()), n=v)
         for k,v in sorted(cnt.items(), key=lambda x:-x[1])[:5]]
    return dict(last=last, boards=len(cnt), top=top)
def mocks_for(co):
    # transcripts are named  <timestamp>-<company-slug>.md  (DeskMock writes the company in)
    k=slug(co)
    if not k: return 0
    return sum(1 for f in glob.glob(str(MOCK/"transcripts/*.md")) if k in slug(pathlib.Path(f).name))

def real_mocks_for(co):
    # A REAL rep (counts toward readiness) = a transcript for this company that has a scorecard AND
    # at least one substantive answer from you. A hollow --auto/test run (no real answers) does NOT count.
    k=slug(co)
    if not k: return 0
    n=0
    for f in glob.glob(str(MOCK/"transcripts/*.md")):
        if k not in slug(pathlib.Path(f).name): continue
        t=read(f)
        answers=[a for a in re.findall(r"\*\*You:\*\*\s*(.+)", t) if len(a.strip())>15]
        if "SCORECARD" in t and answers: n+=1
    return n

def standing_gaps():
    items=[]
    for f in glob.glob(str(CO/"interview-prep/*gap*.md")):
        grab=False
        for ln in read(f).splitlines():
            if re.match(r"^#+\s*(gaps|to address|missing)",ln,re.I): grab=True; continue
            if grab and ln.startswith("#"): grab=False
            if grab:
                m=re.match(r"^\s*(?:[-*]|\d+\.)\s+(.*)",ln)
                if m: items.append(re.sub(r"\*\*|`","",m.group(1)).strip())
    seen,out=set(),[]
    for it in items:
        k=it.lower()[:40]
        if k not in seen and len(it)>3: seen.add(k); out.append(it)
    return out[:8]

def readiness(co):
    # HONEST: readiness = actual practice with feedback (real scored mock reps). Prep materials
    # (pack / gap / questions) are shown separately as assets — having them is NOT being ready.
    return min(100, real_mocks_for(co)*50)  # 1 real rep = 50%, 2 = maxed

def has_questions(co):
    k=slug(co); return bool(next((f for f in prep_files if k and k in slug(f) and "question" in f.lower()),""))
def contacts_by_co():
    m={}
    for line in read(CO/"data/contacts.tsv").splitlines():
        c=line.split("\t")
        if len(c)>=4 and c[0].strip(): m.setdefault(slug(c[1]),[]).append(f'{c[0]} — {c[3]}')
    return m

def profile():
    t=read(CO/"config/profile.yml")
    def g(k):
        m=re.search(rf'^\s*{k}:\s*"?([^"\n#]+)"?', t, re.M)
        return (m.group(1).strip() if m else "")
    return dict(name=g("name") or NAME or "Candidate", email=g("email") or EMAIL,
                phone=g("phone") or MOBILE, location=g("location"),
                linkedin=g("linkedin"), website=g("website"))

def resume_for(company, num):
    # the tailored PDF for this role, from career-ops/output/. Prefer NNN- prefix, then company slug.
    files=[pathlib.Path(f).name for f in glob.glob(str(CO/"output/*.pdf"))]
    pre=str(num).zfill(3)
    for f in files:
        if f.startswith(pre+"-"): return f  # NNN-*.pdf convention from career-ops
    k=slug(company)
    for f in files:
        n=f.lower()
        if k and k in slug(f) and not any(x in n for x in ("gap","pack","master","toolkit","prep")): return f
    return ""

def build_state():
    A=[a for a in apps() if a["status"].upper()!="SKIP"]
    active=[a for a in A if a["status"].upper() not in ("REJECTED","DISCARDED")]
    pipe=pipeline_recent(); nm=n_mock(); sg=standing_gaps(); cb=contacts_by_co()
    def cnt(*s): return sum(1 for a in A if a["status"].lower() in [x.lower() for x in s])
    STAGES=["evaluated","applied","responded","interview","offer"]
    def nextact(a):
        s=a["status"].lower()
        if not pack(a["company"]) and s in ("applied","interview","responded"): return "Generate DeskMock questions"
        if s=="evaluated": return "Apply"
        if s in ("applied",): return "Follow up + prep"
        if s in ("interview","responded"): return "Prep hard — interview stage"
        return "Monitor"
    pipeline=[dict(num=a["num"],company=a["company"],role=a["role"],score=a["score"],status=a["status"],
                   ready=readiness(a["company"]),haspack=bool(pack(a["company"])),
                   hasq=has_questions(a["company"]),hasgap=bool(gapd(a["company"])),
                   nmock=real_mocks_for(a["company"]),
                   materials=(30 if pack(a["company"]) else 0)+(20 if gapd(a["company"]) else 0)+(15 if has_questions(a["company"]) else 0),
                   hasresume=bool(resume_for(a["company"],a["num"])),
                   contact=(cb.get(slug(a["company"]),[""])[0]),
                   jd=jd_url(a), report=report_file(a), jdsaved=bool(saved_jd(a)),
                   gaps=sg[:2], nextact=nextact(a),
                   stage=(STAGES.index(a["status"].lower())+1 if a["status"].lower() in STAGES else 0))
              for a in sorted(active,key=lambda x:(-(readiness(x["company"])), x["company"].lower()))]
    # next best actions
    na=[]
    for d,co in followups_due():
        na.append(dict(kind="followup",label=f"Follow-up due {d} — {co}"))
    unprepped=[a for a in active if a["status"].lower() in ("applied","interview","responded") and not pack(a["company"])]
    for a in unprepped[:3]:
        na.append(dict(kind="prep",label=f"Build prep pack — {a['company']} ({a['role']})",co=a["company"]))
    if nm < 3:
        na.append(dict(kind="mock",label=f"Rehearse — only {nm} mock session(s) logged (aim 3+)",co=""))
    if pipe:
        na.append(dict(kind="review",label=f"Review {len(pipe)} new matches"))
    na.append(dict(kind="call",label="Follow up your warm paths & recruiter contacts"))
    avg=round(sum(readiness(a["company"]) for a in active)/len(active)) if active else 0
    return dict(
        stats=dict(tracked=len([a for a in A if a["status"].upper()!="SKIP"]),applied=cnt("Applied"),
                   interviewing=cnt("Interview","Responded"),offers=cnt("Offer","Hired"),
                   newmatches=len(pipe),prep_avg=avg,mock=nm),
        pipeline=pipeline, new_matches=pipe, next_actions=na, standing_gaps=sg,
        profile=profile(), scan=scan_coverage(), setup=setup_status(),
        ignored=list(reversed(_ignored_rows()))[:40],
        ts=datetime.datetime.now().strftime("%Y-%m-%d %H:%M"))

# ---------- actions ----------
def run(cmd, cwd):
    try:
        r=subprocess.run(cmd,cwd=cwd,capture_output=True,text=True,timeout=180)
        return (r.returncode==0, (r.stdout or r.stderr)[-1500:])
    except Exception as e:
        return (False, str(e))

def do_scan():
    ok,out=run(["node","scan.mjs"],CO)
    if not ok: return dict(ok=False, msg=f"Scan failed: {out[-200:]}")
    L=scan_coverage().get("last",{})
    msg=(f"Scan done — {L.get('companies','?')} ATS companies · {L.get('found','?')} found · "
         f"{L.get('new','0')} NEW. Skipped {L.get('skipped','?')} "
         f"({L.get('f_title','0')} title, {L.get('f_location','0')} location, {L.get('dupes','0')} dupes).")
    # widen the net — JobSpy (LinkedIn/Indeed/Glassdoor/ZipRecruiter/Google) if its venv is set up
    jv=HOME/"src/findingnemo/.jobspy-venv/bin/python"
    if jv.exists() and (CO/"jobspy-scan.py").exists():
        try:
            r=subprocess.run([str(jv),"jobspy-scan.py","--days","7"],cwd=CO,capture_output=True,text=True,timeout=600)
            mj=re.search(r"appended (\d+) to pipeline", r.stdout or "")
            msg+=f" + LinkedIn/Indeed/Glassdoor/ZipRecruiter/Google: {mj.group(1) if mj else '0'} NEW."
        except Exception as e:
            msg+=f" (aggregator scan skipped: {str(e)[:60]})"
    return dict(ok=True, msg=msg)

def do_apply(num):
    ok,out=run(["node","set-status.mjs",str(num),"Applied","--note","Marked Applied via JobHelm Mission Control"],CO)
    return dict(ok=ok, msg="Marked Applied ✓" if ok else f"Failed: {out[-200:]}")

def do_select(url, company="", title="", posted=""):
    company=(company or "").strip(); title=(title or "").strip()
    if not (company and title): return dict(ok=False, msg="Missing role details.")
    for a in apps():
        if slug(a["company"])==slug(company) and slug(a["role"])==slug(title):
            return dict(ok=True, msg=f"{company} — {title} is already on your board.")
    nums=[int(a["num"]) for a in apps() if a["num"].isdigit()]
    num=str((max(nums)+1) if nums else 1)
    date=datetime.datetime.now().strftime("%Y-%m-%d")
    note=f"Selected from Discover {date}; pending evaluation."
    tsv=f"{num}\t{date}\t{company}\t{title}\tEvaluated\tN/A\t❌\t\t{note}\t{url}\n"
    try:
        d=CO/"batch/tracker-additions"; d.mkdir(parents=True, exist_ok=True)
        (d/f"{num.zfill(3)}-{slug(company) or 'role'}.tsv").write_text(tsv)
    except Exception as e:
        return dict(ok=False, msg=f"Write failed: {e}")
    ok,out=run(["node","merge-tracker.mjs"],CO)
    if not ok: return dict(ok=False, msg=f"Add failed: {out[-200:]}")
    return dict(ok=True, msg=f"✓ Added {company} to your board (To apply). Open it to tailor a résumé.")

def do_discard(num):
    ok,out=run(["node","set-status.mjs",str(num),"Discarded","--note","Discarded via JobHelm — posting closed / no longer available"],CO)
    return dict(ok=ok, msg="Marked Discarded ✓ — removed from the active board." if ok else f"Failed: {out[-200:]}")

def do_reject(num):
    ok,out=run(["node","set-status.mjs",str(num),"Rejected","--note","Marked Rejected via JobHelm"],CO)
    return dict(ok=ok, msg="Marked Rejected ✓ — removed from the active board." if ok else f"Failed: {out[-200:]}")

def _mock_running():
    try:
        r=subprocess.run(["pgrep","-f","interview.py"],capture_output=True,text=True,timeout=5)
        return r.returncode==0 and bool(r.stdout.strip())
    except Exception:
        return False

def do_mock(num=None, co=""):
    role=""
    if num:
        a=next((x for x in apps() if x["num"]==str(num)),None)
        if a: co=a["company"]; role=a["role"]
    if _mock_running():
        return dict(ok=True, msg="A DeskMock session is already open in Terminal — use that window (say “done” to finish it) before starting another.")
    try:
        extra=" --speak --clipboard"  # accepts typing OR dictation (more robust than --auto)
        if co:   extra+=" --company "+shlex.quote(co)
        if role: extra+=" --role "+shlex.quote(role)
        script=f"cd {shlex.quote(str(MOCK))} && ./mock{extra}"
        subprocess.Popen(["osascript","-e",f'tell application "Terminal" to do script "{script}"'])
        tail=f" for {co}" if co else ""
        return dict(ok=True, msg=f"Launched DeskMock in a new Terminal{tail} — the transcript will count toward this role's readiness.")
    except Exception as e:
        return dict(ok=False, msg=str(e))

def load_key():
    for v in (os.environ.get("OPENROUTER_API_KEY"), os.environ.get("OPENAI_API_KEY")):
        if v: return v.strip()
    for p in (CO/"openrouter.env", HERE/"openrouter.env"):
        if p.exists(): return p.read_text().strip()
    return ""

def setup_status():
    cv=(CO/"cv.md").exists() and len(read(CO/"cv.md").strip())>60
    prof=(CO/"config/profile.yml").exists()
    return dict(cv=cv, profile=prof, key=bool(load_key()), ready=(cv and bool(load_key())))

def do_setup(d):
    written=[]
    resume=(d.get("resume") or "").strip()
    name=(d.get("name") or "").strip(); email=(d.get("email") or "").strip()
    phone=(d.get("phone") or "").strip(); loc=(d.get("location") or "").strip()
    titles=(d.get("titles") or "").strip(); key=(d.get("key") or "").strip()
    try:
        if resume:
            (CO/"cv.md").write_text(resume if resume.lstrip().startswith("#") else f"# {name or 'Candidate'}\n{loc}\n\n{resume}\n")
            written.append("cv.md (résumé)")
        if name or email or loc:
            (CO/"config").mkdir(parents=True, exist_ok=True)
            (CO/"config/profile.yml").write_text(f'name: "{name}"\nemail: "{email}"\nphone: "{phone}"\nlocation: "{loc}"\n')
            written.append("profile")
        if key.startswith("sk-"):
            (CO/"openrouter.env").write_text(key); written.append("API key")
        if titles:
            (CO/"jobhelm-targets.txt").write_text(titles); written.append("target roles")
    except Exception as e:
        return dict(ok=False, msg=f"Setup failed: {e}")
    if not written:
        return dict(ok=False, msg="Nothing to save — add at least your résumé and API key.")
    return dict(ok=True, msg="Saved "+", ".join(written)+". You're set up — hit Scan to start.")

def _scrub_contact_placeholders(t):
    # some models emit [PHONE]/[Your Number]/[EMAIL] rather than the real value — swap them in.
    t=re.sub(r"\[[^\]]*\b(phone|mobile|cell|number|contact)\b[^\]]*\]", MOBILE, t, flags=re.I)
    t=re.sub(r"\[[^\]]*\b(e-?mail)\b[^\]]*\]", EMAIL, t, flags=re.I)
    return t

def do_draft(message):
    if not (message or "").strip():
        return dict(ok=False, msg="Paste the recruiter message first.")
    key=load_key()
    if not key: return dict(ok=False, msg="No OpenRouter key found (set OPENROUTER_API_KEY or ../openrouter.env).")
    cv=read(CO/"cv.md")[:2500]
    p=profile(); who=p["name"]; phone=p["phone"]; mail=p["email"]
    contact=(f"When the reply involves scheduling or a call, include the mobile {phone} in the sign-off. " if phone else "")
    contact+=(f"Include the email {mail} only when sharing contact details is natural. " if mail else "")
    sys=(f"You draft short, professional reply messages on behalf of {who}, {PROFILE}. Rules: address the "
         f"sender by their first name if it appears in their message; sign every reply as '{who}'. {contact}"
         f"NEVER use bracketed placeholders like [Name], [Your Name], or [PERSON_NAME] — use the real names or "
         f"omit gracefully. Voice: warm, concise, plain-ASCII, no smart quotes or em-dashes, no invented facts. "
         f"If the message looks like spam or a scam (vague, off-target, asks for money/SSN, free-email "
         f"recruiter), DO NOT draft a reply — instead return a one-line warning that it looks like spam and why. "
         f"Otherwise return only the reply text.\n\nCandidate background (for grounding, do not fabricate beyond "
         f"this):\n{cv}")
    body=json.dumps({"model":"deepseek/deepseek-v3.2","temperature":0.5,"max_tokens":500,
        "messages":[{"role":"system","content":sys},
                    {"role":"user","content":"Draft a reply to this message:\n\n"+message[:3000]}]}).encode()
    req=urllib.request.Request("https://openrouter.ai/api/v1/chat/completions",data=body,
        headers={"Authorization":"Bearer "+key,"Content-Type":"application/json","X-Title":"JobHelm"})
    try:
        with urllib.request.urlopen(req,timeout=60) as r:
            txt=json.load(r)["choices"][0]["message"]["content"].strip()
        txt=_scrub_contact_placeholders(txt)
        return dict(ok=True, msg="Draft ready — review before sending.", draft=txt)
    except Exception as e:
        return dict(ok=False, msg=f"Draft failed: {e}")

def do_open_jd(num):
    a=next((x for x in apps() if x["num"]==str(num)),None)
    f=saved_jd(a) if a else ""
    if not f: return dict(ok=False, msg="No archived JD saved for this role yet.")
    try:
        subprocess.Popen(["open", str(CO/"jds"/f)])
        return dict(ok=True, msg=f"Opened saved JD: {f}")
    except Exception as e:
        return dict(ok=False, msg=str(e))

def do_open(co):
    f = pack(co) or gapd(co)
    if not f: return dict(ok=False, msg=f"No prep pack yet for {co}.")
    try:
        subprocess.Popen(["open", str(CO/"interview-prep"/f)])
        return dict(ok=True, msg=f"Opened {f}")
    except Exception as e:
        return dict(ok=False, msg=str(e))

def do_open_resume(num):
    a=next((x for x in apps() if x["num"]==str(num)),None)
    if not a: return dict(ok=False, msg="Role not found.")
    r=resume_for(a["company"], num)
    if not r: return dict(ok=False, msg=f"No tailored resume PDF for {a['company']} in output/ — generate one first.")
    try:
        subprocess.Popen(["open", str(CO/"output"/r)])
        return dict(ok=True, msg=f"Opened {r} — drag it into the application's resume field.")
    except Exception as e:
        return dict(ok=False, msg=str(e))

MOCK_SYS = ("You are a seasoned, friendly-but-rigorous hiring panel interviewer for the role below. "
    "Interview ONE question at a time; never dump multiple. First turn: a one-line warm intro then question 1. "
    "After each candidate answer: (1) FEEDBACK - 2-3 crisp bullets on clarity, structure (STAR?), and "
    "specificity/evidence; (2) POWER PHRASES - 2 sharper expressions they could reuse; (3) then ask the next "
    "question, escalating depth and probing the role's gaps. Keep under ~180 words per turn. End every "
    "interviewing turn with the question on its own final line prefixed exactly 'QUESTION:'. Ground everything "
    "in the candidate's real CV and the role focus below; never invent facts about the candidate. When told the "
    "interview is over, STOP interviewing and produce a SCORECARD: scores 1-10 for Clarity, Structure, "
    "Specificity, Technical depth, Executive presence; 3 strongest moments; 3 things to fix; 8 power phrases.")

def _mock_messages(num, history):
    a=next((x for x in apps() if x["num"]==str(num)),None)
    role=a["role"] if a else "the role"; company=a["company"] if a else "the company"
    cv=read(CO/"cv.md")[:4000]
    gp=gapd(company); gap=read(CO/"interview-prep"/gp)[:2000] if gp else "(no gap doc — probe common platform/infra leadership gaps)"
    ctx=f"ROLE: {role} at {company}\n\n=== CANDIDATE CV ===\n{cv}\n\n=== ROLE FOCUS / GAPS ===\n{gap}\n"
    msgs=[{"role":"system","content":MOCK_SYS+"\n\n"+ctx}]
    hist=[h for h in (history or []) if h.get("role") in ("user","assistant") and (h.get("content") or "").strip()]
    if hist and hist[0]["role"]=="assistant":
        msgs.append({"role":"user","content":"Begin the interview."})  # keep roles well-formed
    for h in hist: msgs.append({"role":h["role"],"content":h["content"][:3000]})
    return msgs, a

def do_mock_start(num):
    key=load_key()
    if not key: return dict(ok=False,msg="No OpenRouter key.")
    msgs,a=_mock_messages(num,[])
    if not a: return dict(ok=False,msg="Role not found.")
    msgs.append({"role":"user","content":"Begin the interview. Intro + question 1 only."})
    try: return dict(ok=True, turn=_llm(msgs,key,900))
    except Exception as e: return dict(ok=False,msg=f"Start failed: {e}")

def do_mock_reply(num, history):
    key=load_key()
    if not key: return dict(ok=False,msg="No OpenRouter key.")
    if not history or history[-1].get("role")!="user" or not (history[-1].get("content") or "").strip():
        return dict(ok=False,msg="Type an answer first.")
    msgs,a=_mock_messages(num,history)
    try: return dict(ok=True, turn=_llm(msgs,key,900))
    except Exception as e: return dict(ok=False,msg=f"Reply failed: {e}")

def do_mock_finish(num, history):
    key=load_key()
    if not key: return dict(ok=False,msg="No OpenRouter key.")
    msgs,a=_mock_messages(num,history)
    msgs.append({"role":"user","content":"The interview is over. Give me the SCORECARD now."})
    try: card=_llm(msgs,key,900)
    except Exception as e: return dict(ok=False,msg=f"Scorecard failed: {e}")
    saved=""
    try:
        lines=[f'**{"Interviewer" if h.get("role")=="assistant" else "You"}:** {h.get("content","")}'
               for h in (history or []) if (h.get("content") or "").strip()]
        lines.append(f'**SCORECARD:** {card}')
        outdir=MOCK/"transcripts"; outdir.mkdir(parents=True,exist_ok=True)
        ts=datetime.datetime.now().strftime("%Y-%m-%d-%H%M")
        company=a["company"] if a else "role"
        out=outdir/f"{ts}-{slug(company)}.md"
        out.write_text(f"# Mock interview (in-app) — {(a['role']+' @ '+company) if a else ''} ({ts})\n\n"+"\n\n".join(lines))
        saved=out.name
    except Exception as e:
        saved=f"(save failed: {e})"
    return dict(ok=True, scorecard=card, saved=saved)

def _llm(msgs, key, max_tokens=900):
    body=json.dumps({"model":"deepseek/deepseek-v3.2","temperature":0.5,"max_tokens":max_tokens,"messages":msgs}).encode()
    req=urllib.request.Request("https://openrouter.ai/api/v1/chat/completions",data=body,
        headers={"Authorization":"Bearer "+key,"Content-Type":"application/json","X-Title":"JobHelm"})
    with urllib.request.urlopen(req,timeout=90) as r:
        return json.load(r)["choices"][0]["message"]["content"].strip()

def _gen_questions_for(company, role):
    key=load_key()
    if not key: return (False,"no key")
    cv=read(CO/"cv.md")[:2500]
    sys=("Generate a focused mock-interview question set for a specific role, grounded in the candidate's CV. "
         "Return markdown: 8-10 likely interview questions across behavioral, technical, and leadership, each with "
         "2-3 KEY POINTS the candidate should hit (in their own words, from their real experience) and 2 POWER "
         "PHRASES (crisp vocabulary). No invented facts. Realistic for the role/level.")
    usr=f"Role: {role} at {company}.\n\nCandidate CV:\n{cv}\n\nProduce the question set."
    try:
        md=_llm([{"role":"system","content":sys},{"role":"user","content":usr}],key,1400)
    except Exception as e:
        return (False,str(e))
    out=CO/"interview-prep"/f"{slug(company)}-questions.md"
    out.write_text(f"# Mock questions — {role} @ {company}\n_(auto-generated by JobHelm — practice these in DeskMock)_\n\n{md}\n")
    prep_files.append(out.name)
    return (True,out.name)

def do_questions(num):
    a=next((x for x in apps() if x["num"]==str(num)),None)
    if not a: return dict(ok=False,msg="Role not found.")
    ok,info=_gen_questions_for(a["company"],a["role"])
    return dict(ok=ok, msg=(f"Generated {info} ✓ — rehearse in DeskMock." if ok else f"Failed: {info}"))

def do_questions_all():
    done=0; fail=0
    targets=[a for a in apps() if a["status"].lower() in ("applied","interview","responded")
             and not next((f for f in prep_files if slug(a["company"]) in slug(f) and "question" in f.lower()),"")]
    for a in targets:
        ok,_=_gen_questions_for(a["company"],a["role"]);
        done+=1 if ok else 0; fail+=0 if ok else 1
    return dict(ok=True, msg=f"Generated question sets for {done} applied role(s)" + (f"; {fail} failed" if fail else "") + ".")

def do_brief(company):
    if not (company or "").strip(): return dict(ok=False,msg="No company.")
    key=load_key()
    if not key: return dict(ok=False,msg="No OpenRouter key.")
    sys=("Give a tight company brief for a candidate about to talk to a recruiter. 4-6 short lines: what the "
         "company does, size/industry, business model, and one 'talking angle' for a platform/infrastructure "
         "leadership candidate. Concise, plain-ASCII. If the company is obscure/small and you are not confident, "
         "SAY SO and stick to what's likely true — never fabricate funding, headcount, or news.")
    try:
        txt=_llm([{"role":"system","content":sys},{"role":"user","content":f"Company: {company}"}],key,450)
        return dict(ok=True, msg="Brief ready (verify specifics before quoting).", brief=txt)
    except Exception as e:
        return dict(ok=False, msg=f"Brief failed: {e}")

def followups_due():
    # follow-ups.md rows look like:  - next #<appNum> <due-date> (set <date>)
    A={a["num"]: a for a in apps()}
    DEAD={"rejected","skip","discarded","hired","offer"}
    today=datetime.date.today()
    seen=set(); out=[]
    for ln in read(CO/"data/follow-ups.md").splitlines():
        if not ln.strip().startswith("-"): continue
        md=re.search(r"(20\d\d-\d\d-\d\d)", ln)
        if not md: continue
        try: d=datetime.date.fromisoformat(md.group(1))
        except Exception: continue
        if d>today: continue  # not due yet — skip future reminders
        mn=re.search(r"#(\d+)", ln)
        key=mn.group(1) if mn else ln.strip()
        if key in seen: continue  # one reminder per role (earliest due wins)
        a=A.get(mn.group(1)) if mn else None
        if a and a["status"].lower() in DEAD: continue  # closed roles don't need chasing
        seen.add(key)
        co=(a["company"] if a else "") or ("role #"+mn.group(1) if mn else ln.strip()[:40])
        out.append((d.isoformat(), co))
    out.sort()
    return out[:5]

# ---------- http ----------
PAGE = """<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>JobHelm · Mission Control</title><style>
:root{--bg:#f4f5f7;--card:#fff;--ink:#161619;--muted:#697086;--line:#e3e6eb;--soft:#eceef2;--accent:#0b6b53;--accent2:#1d4ed8;--amber:#c26a06;--red:#b91c1c;--shadow:0 1px 2px rgba(16,24,40,.06),0 1px 3px rgba(16,24,40,.1)}
@media(prefers-color-scheme:dark){:root{--bg:#0e1014;--card:#171a21;--ink:#e6e8ec;--muted:#98a1b3;--line:#262b36;--soft:#1f232c;--accent:#34d399;--accent2:#60a5fa;--amber:#f59e0b;--red:#f87171;--shadow:0 1px 2px rgba(0,0,0,.4)}}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--ink);font:14px/1.5 -apple-system,BlinkMacSystemFont,Segoe UI,Roboto,sans-serif;-webkit-font-smoothing:antialiased}
button{font:inherit;border:1px solid var(--line);background:var(--card);color:var(--ink);border-radius:8px;padding:6px 12px;cursor:pointer;transition:filter .12s,background .12s}
button:hover{filter:brightness(1.04)}button.p{background:var(--accent);color:#fff;border-color:var(--accent)}
button.sm{padding:4px 9px;font-size:12px}button:disabled{opacity:.5;cursor:default}
input,textarea{font:inherit;background:var(--bg);color:var(--ink);border:1px solid var(--line);border-radius:8px;padding:7px 10px}
input:focus,textarea:focus{outline:2px solid var(--accent2);outline-offset:-1px}
.muted{color:var(--muted)}.sm{font-size:12px}a{color:var(--accent2);text-decoration:none}a:hover{text-decoration:underline}
/* header */
header{position:sticky;top:0;z-index:20;background:var(--card);border-bottom:1px solid var(--line);padding:10px 18px;display:flex;align-items:center;gap:14px;flex-wrap:wrap}
.brand{font-size:16px;font-weight:700;display:flex;align-items:center;gap:7px;white-space:nowrap}
.brand small{font-weight:400;color:var(--muted);font-size:11px}
.grow{flex:1}
.tabs{display:flex;gap:4px;background:var(--soft);padding:3px;border-radius:9px}
.tabs button{border:0;background:transparent;padding:5px 12px;border-radius:7px;font-size:13px;color:var(--muted)}
.tabs button.on{background:var(--card);color:var(--ink);box-shadow:var(--shadow);font-weight:600}
.htools{display:flex;gap:7px;align-items:center;flex-wrap:wrap}
/* stat strip */
.strip{display:flex;gap:9px;overflow-x:auto;padding:14px 18px 4px}
.stat{background:var(--card);border:1px solid var(--line);border-radius:11px;padding:9px 14px;min-width:92px;box-shadow:var(--shadow)}
.stat .n{font-size:21px;font-weight:700;line-height:1.1}.stat .l{font-size:10px;color:var(--muted);text-transform:uppercase;letter-spacing:.04em;margin-top:2px}
.stat.hl .n{color:var(--accent)}
/* controls */
.controls{display:flex;gap:10px;align-items:center;padding:12px 18px 6px;flex-wrap:wrap}
.controls input#q{flex:1;min-width:180px;max-width:340px}
.chips{display:flex;gap:6px;flex-wrap:wrap}
.fchip{border:1px solid var(--line);background:var(--card);color:var(--muted);border-radius:20px;padding:4px 11px;font-size:12px;cursor:pointer;user-select:none}
.fchip.on{background:var(--accent);color:#fff;border-color:var(--accent)}
/* layout */
.main{display:grid;grid-template-columns:1fr 312px;gap:16px;padding:8px 18px 40px;align-items:start}
@media(max-width:900px){.main{grid-template-columns:1fr}}
/* board */
.board{display:flex;gap:12px;overflow-x:auto;padding-bottom:8px}
.col{flex:0 0 268px;background:var(--soft);border-radius:12px;padding:9px;min-height:120px}
.col h3{font-size:11px;text-transform:uppercase;letter-spacing:.05em;color:var(--muted);margin:2px 4px 9px;display:flex;justify-content:space-between}
.col h3 .c{background:var(--card);border-radius:20px;padding:0 8px;color:var(--ink)}
.kc{background:var(--card);border:1px solid var(--line);border-radius:11px;padding:11px 12px;margin-bottom:9px;cursor:pointer;box-shadow:var(--shadow);transition:transform .1s,box-shadow .1s,border-color .1s}
.kc:hover{transform:translateY(-1px);border-color:var(--accent);box-shadow:0 4px 14px rgba(16,24,40,.1)}
.kc .top{display:flex;justify-content:space-between;gap:8px;align-items:flex-start}
.kc .co{font-weight:700;font-size:14px;line-height:1.25}
.kc .ro{font-size:12px;color:var(--muted);margin:1px 0 8px}
.kc .score{font-size:11px;font-weight:700;color:var(--accent2);white-space:nowrap;background:var(--soft);border-radius:6px;padding:1px 6px}
.kc .rb{height:5px;background:var(--soft);border-radius:5px;overflow:hidden;margin:6px 0 4px}.kc .rb i{display:block;height:100%}
.kc .meta{display:flex;justify-content:space-between;align-items:center;font-size:11px;color:var(--muted);margin-top:2px}
.kc .pc{display:flex;gap:3px}
.dot{width:7px;height:7px;border-radius:50%;display:inline-block;background:var(--line)}.dot.on{background:var(--accent)}
.kc .qa{margin-top:9px;display:flex;gap:6px}
.kc .warm{color:var(--accent);font-weight:600}
.empty{color:var(--muted);font-size:12px;text-align:center;padding:16px 4px}
/* rail */
.rail{display:flex;flex-direction:column;gap:14px;position:sticky;top:64px}
.panel{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:13px 15px;box-shadow:var(--shadow)}
.panel h2{font-size:11px;text-transform:uppercase;letter-spacing:.05em;color:var(--accent);margin:0 0 10px}
.na li{list-style:none;margin:0 0 8px;display:flex;justify-content:space-between;gap:8px;align-items:center;font-size:12.5px}
.na{margin:0;padding:0}.na .b{white-space:nowrap}
.glist{margin:0;padding-left:16px;font-size:12.5px}.glist li{margin:5px 0}
/* discover */
.disc{padding:8px 18px 40px}
table{width:100%;border-collapse:collapse}td,th{text-align:left;padding:8px 8px;border-bottom:1px solid var(--line);vertical-align:top}
th{font-size:11px;color:var(--muted);text-transform:uppercase}
/* drawer */
#scrim{position:fixed;inset:0;background:rgba(10,12,16,.45);opacity:0;pointer-events:none;transition:.2s;z-index:30}
#scrim.show{opacity:1;pointer-events:auto}
#drawer{position:fixed;top:0;right:0;height:100%;width:min(460px,94vw);background:var(--bg);border-left:1px solid var(--line);transform:translateX(100%);transition:transform .22s cubic-bezier(.4,0,.2,1);z-index:31;overflow-y:auto;box-shadow:-8px 0 30px rgba(0,0,0,.18)}
#drawer.show{transform:translateX(0)}
.dhub{background:linear-gradient(135deg,var(--accent),var(--accent2));color:#fff;padding:18px 18px 16px}
.dhub .x{float:right;background:rgba(255,255,255,.2);border:0;color:#fff;border-radius:8px;width:30px;height:30px;font-size:16px}
.dhub .co{font-size:19px;font-weight:700}.dhub .ro{font-size:13px;opacity:.94;margin-top:2px}
.dhub .badge{display:inline-block;margin-top:9px;background:rgba(255,255,255,.22);border-radius:20px;padding:2px 11px;font-size:12px}
.dbody{padding:16px 18px 30px}
.sec{margin-bottom:16px}.sec .h{font-size:11px;text-transform:uppercase;letter-spacing:.05em;color:var(--muted);margin-bottom:7px}
.rowb{display:flex;align-items:center;gap:8px}.rowb .rb{flex:1;height:8px;background:var(--soft);border-radius:5px;overflow:hidden}.rowb .rb i{display:block;height:100%}
.chip{display:inline-block;font-size:11px;padding:2px 9px;border-radius:20px;border:1px solid var(--line);margin:0 5px 5px 0;color:var(--muted)}
.chip.on{background:var(--accent);color:#fff;border-color:var(--accent)}
.actions{display:flex;gap:8px;flex-wrap:wrap}
.briefbox,.draftbox{white-space:pre-wrap;background:var(--card);border:1px solid var(--line);border-radius:9px;padding:11px;margin-top:9px;font-size:12.5px}
.bmk{display:inline-block;background:var(--soft);border:1px dashed var(--accent);color:var(--accent);border-radius:8px;padding:5px 11px;font-size:12px;text-decoration:none;cursor:grab;font-weight:600}
.mockconvo{max-height:46vh;overflow-y:auto;border:1px solid var(--line);border-radius:9px;padding:10px;background:var(--card);margin-bottom:10px}
.mturn{margin-bottom:10px;font-size:13px;line-height:1.5}.mturn b{font-size:11px;text-transform:uppercase;letter-spacing:.04em;color:var(--muted)}
.mturn.mi{border-left:3px solid var(--accent2);padding-left:9px}.mturn.mu{border-left:3px solid var(--accent);padding-left:9px}
.mturn.mc{border-left:3px solid var(--amber);padding-left:9px}
.fieldpack{white-space:pre-wrap;background:var(--card);border:1px solid var(--line);border-radius:8px;padding:9px;font-size:12px;margin-top:6px}
details summary{color:var(--accent2)}
#toast{position:fixed;bottom:20px;left:50%;transform:translateX(-50%);background:var(--ink);color:var(--bg);padding:10px 18px;border-radius:9px;font-size:13px;opacity:0;transition:.25s;pointer-events:none;z-index:40;box-shadow:0 6px 20px rgba(0,0,0,.25)}
#toast.show{opacity:1}
.spin{display:inline-block;width:13px;height:13px;border:2px solid var(--line);border-top-color:var(--accent);border-radius:50%;animation:sp .7s linear infinite;vertical-align:-2px;margin-right:5px}
@keyframes sp{to{transform:rotate(360deg)}}
</style></head><body>

<header>
  <div class="brand">🧭 JobHelm <small id="ts">loading…</small></div>
  <div class="grow"></div>
  <div class="tabs">
    <button id="tab-board" class="on" onclick="view('board')">Board</button>
    <button id="tab-discover" onclick="view('discover')">Discover</button>
  </div>
  <div class="htools">
    <button onclick="openSetup()" title="Enter your résumé, profile, and API key">🚀 Setup</button>
    <button onclick="openDraft()">✍️ Draft reply</button>
    <button class="p" onclick="doScan()">🔎 Scan</button>
    <button onclick="load()" title="Refresh">🔄</button>
  </div>
</header>

<div class="strip" id="strip"></div>

<!-- BOARD VIEW -->
<div id="v-board">
  <div class="controls">
    <input id="q" placeholder="Search company or role…" oninput="renderBoard()">
    <div class="chips" id="stagechips"></div>
  </div>
  <div class="main">
    <div class="board" id="board"></div>
    <div class="rail">
      <div class="panel"><h2>🔥 Next best actions</h2><ul class="na" id="na"></ul></div>
      <div class="panel"><h2>📌 Standing gaps · study focus</h2><ul class="glist" id="gaps"></ul></div>
    </div>
  </div>
</div>

<!-- DISCOVER VIEW -->
<div id="v-discover" style="display:none">
  <div class="disc">
    <div class="panel" style="margin-bottom:16px"><h2>🛰 Scan coverage</h2><div id="scancov" class="sm muted">loading…</div></div>
    <div class="panel" style="margin-bottom:16px"><h2>🆕 New matches · last 7 days (profile + location filtered)</h2>
      <table><thead><tr><th>Company</th><th>Role</th><th>Posted</th><th></th></tr></thead><tbody id="nm"></tbody></table>
    </div>
    <div class="panel" style="margin-bottom:16px"><details><summary style="cursor:pointer;font-weight:600;color:var(--muted)">🚫 Ignored / seen (<span id="igncount">0</span>) — hidden from Discover</summary>
      <table style="margin-top:10px"><thead><tr><th>Company</th><th>Role</th><th>Posted</th><th></th></tr></thead><tbody id="ignored"></tbody></table>
    </details></div>
  </div>
</div>

<!-- DRAWER -->
<div id="scrim" onclick="closeDrawer()"></div>
<div id="drawer"><div id="drawer-inner"></div></div>
<div id="toast"></div>

<script>
var DATA={pipeline:[],next_actions:[],standing_gaps:[],new_matches:[],stats:{}};
var STAGES=[["evaluated","To apply"],["applied","Applied"],["responded","In touch"],["interview","Interview"],["offer","Offer"]];
var FILTER=null; // stage key or null

function toast(m,persist){var t=document.getElementById('toast');t.innerHTML=m;t.classList.add('show');if(!persist)setTimeout(function(){t.classList.remove('show')},3200)}
function rc(p){return p>=65?'var(--accent)':p>=35?'var(--amber)':'var(--red)'}
function esc(s){return (s||'').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;')}
function byNum(n){return DATA.pipeline.find(function(p){return String(p.num)===String(n)})}
function fieldPack(){var P=DATA.profile||{};return ['Name: '+(P.name||''),'Email: '+(P.email||''),'Phone: '+(P.phone||''),'Location: '+(P.location||''),'LinkedIn: '+(P.linkedin||''),'Website: '+(P.website||'')].join('\\n');}
function bmHref(){
  var P=DATA.profile||{}; var nm=(P.name||'').trim().split(' ');
  var d={fn:nm[0]||'',ln:nm.slice(1).join(' '),email:P.email||'',phone:P.phone||'',li:P.linkedin||'',loc:P.location||''};
  var body="(function(){var P="+JSON.stringify(d)+";function s(q,v){var e=document.querySelector(q);if(e&&v){e.focus();e.value=v;e.dispatchEvent(new Event('input',{bubbles:true}));e.dispatchEvent(new Event('change',{bubbles:true}));return 1}return 0}s('#first_name',P.fn);s('#last_name',P.ln);s('#email',P.email);s('#phone',P.phone);s('#job_application_location',P.loc);var ls=document.querySelectorAll('label');for(var i=0;i<ls.length;i++){if(/linkedin/i.test(ls[i].textContent)){var id=ls[i].getAttribute('for');if(id)s('#'+id,P.li)}}alert('JobHelm autofill done: name, email, phone, location, LinkedIn. Review, attach resume, submit.');})();";
  return 'javascript:'+encodeURIComponent(body);
}
function stageOf(status){var s=(status||'').toLowerCase();for(var i=0;i<STAGES.length;i++){if(s.indexOf(STAGES[i][0])>=0)return STAGES[i][0]}
  if(s.indexOf('hired')>=0)return 'offer'; return 'applied'}

function view(v){
  document.getElementById('v-board').style.display=v==='board'?'':'none';
  document.getElementById('v-discover').style.display=v==='discover'?'':'none';
  document.getElementById('tab-board').classList.toggle('on',v==='board');
  document.getElementById('tab-discover').classList.toggle('on',v==='discover');
}

async function load(){
  try{DATA=await (await fetch('/api/data')).json();}catch(e){toast('Load failed — is the server running?');return}
  var sc=DATA.scan||{last:{},top:[],boards:0}; var when=(sc.last&&sc.last.when)?sc.last.when:'—';
  document.getElementById('ts').textContent='live · '+DATA.ts+' · 🛰 scan '+when+' · '+location.host;
  if(DATA.setup && !DATA.setup.ready && !window._setupPrompted){window._setupPrompted=true;
    toast('👋 Welcome — click 🚀 Setup to add your résumé, profile, and API key to get started.',true);
    setTimeout(function(){var t=document.getElementById('toast');if(t)t.classList.remove('show');},7000);}
  var s=DATA.stats;
  var T=[['Tracked',s.tracked,0],['Applied',s.applied,0],['Interviewing',s.interviewing,0],['Offers',s.offers,0],['Avg ready',s.prep_avg+'%',1],['Mocks',s.mock,0],['New',s.newmatches,0]];
  document.getElementById('strip').innerHTML=T.map(function(t){return '<div class="stat'+(t[2]?' hl':'')+'"><div class="n">'+t[1]+'</div><div class="l">'+t[0]+'</div></div>'}).join('');
  (function(){var el=document.getElementById('scancov'); if(!el)return;
    if(!sc.top||!sc.top.length){el.innerHTML='No scan history yet — run a scan (🔎 Scan) to populate coverage.';return;}
    var L=sc.last||{};
    var pill=function(t){return '<span style="display:inline-block;background:var(--soft);border:1px solid var(--line);border-radius:20px;padding:3px 11px;font-size:12px;margin:0 5px 5px 0">'+t+'</span>';};
    var top=sc.top.map(function(b){return pill(esc(b.board)+' <b>'+Number(b.n).toLocaleString()+'</b>');}).join('');
    var pairs=[[L.f_title,'off-target title'],[L.f_location,'location'],[L.f_age,'too old'],[L.f_salary,'salary'],[L.dupes,'already seen'],[L.f_blacklist,'blacklist']];
    var brk=pairs.filter(function(p){return p[0]&&p[0]!=='0';}).map(function(p){return Number(p[0]).toLocaleString()+' '+p[1];}).join(' · ')||'none';
    el.innerHTML='<div style="color:var(--ink)"><b>Last scan:</b> '+esc(when)+(L.found?' · '+Number(L.found).toLocaleString()+' roles found':'')+(L.companies?' · '+esc(L.companies)+' companies':'')+((L.new&&L.new!=="0")?' · <b style="color:var(--accent)">'+esc(L.new)+' new</b>':' · <span class="muted">0 new</span>')+'</div>'+
      '<div style="margin-top:6px;color:var(--muted);font-size:12px"><b>Skipped '+(L.skipped?Number(L.skipped).toLocaleString():'0')+'</b> — '+brk+'</div>'+
      '<div style="margin-top:9px;color:var(--ink)"><b>'+sc.boards+' job boards covered</b> — top 5 by volume:</div>'+
      '<div style="margin-top:6px">'+top+'</div>';
  })();
  // stage filter chips
  document.getElementById('stagechips').innerHTML='<span class="fchip'+(FILTER===null?' on':'')+'" onclick="setFilter(null)">All</span>'+
    STAGES.map(function(st){var n=DATA.pipeline.filter(function(p){return stageOf(p.status)===st[0]}).length;
      return '<span class="fchip'+(FILTER===st[0]?' on':'')+'" onclick="setFilter(\\''+st[0]+'\\')">'+st[1]+' '+n+'</span>'}).join('');
  renderBoard();
  // rail: next actions
  document.getElementById('na').innerHTML=DATA.next_actions.map(function(a){
    var b=a.kind==='mock'?'<button class="sm b" onclick="act(\\'mock\\',{})">Rehearse</button>'
       :a.kind==='review'?'<button class="sm b" onclick="view(\\'discover\\')">Review</button>'
       :a.kind==='prep'&&a.co?'<button class="sm b" onclick="genForCo(\\''+encodeURIComponent(a.co)+'\\')">Prep</button>':'';
    return '<li><span>'+esc(a.label)+'</span>'+b+'</li>'}).join('')||'<li class="muted">All clear.</li>';
  document.getElementById('gaps').innerHTML=(DATA.standing_gaps.length?DATA.standing_gaps:['Run a gap analysis to populate this.']).map(function(g){return '<li>'+esc(g)+'</li>'}).join('');
  // discover
  document.getElementById('nm').innerHTML=(DATA.new_matches.length?DATA.new_matches:[{company:'None new 🎉',title:'',posted:'',url:''}]).map(function(m){
    var args="(this,\\''+encodeURIComponent(m.url||'')+'\\',\\''+encodeURIComponent(m.company||'')+'\\',\\''+encodeURIComponent(m.title||'')+'\\',\\''+esc(m.posted||'')+'\\')";
    var sel=(m.company&&m.title)?'<button class="sm p" title="Add to your board (To apply)" onclick="selectMatch'+args+'">➕ Select</button> ':'';
    var ig=m.url?'<button class="sm" title="Hide from Discover" onclick="ignoreMatch'+args+'">🚫 Ignore</button>':'';
    return '<tr><td><b>'+esc(m.company)+'</b></td><td class="sm">'+esc(m.title)+'</td><td class="sm muted">'+esc(m.posted)+'</td><td style="white-space:nowrap">'+(m.url?'<a href="'+esc(m.url)+'" target="_blank">open ↗</a> ':'')+sel+ig+'</td></tr>'}).join('');
  var ign=DATA.ignored||[];
  var ib=document.getElementById('ignored');
  if(ib) ib.innerHTML = ign.length ? ign.map(function(m){
    return '<tr><td><b>'+esc(m.company)+'</b></td><td class="sm">'+esc(m.title)+'</td><td class="sm muted">'+esc(m.posted)+'</td><td>'+(m.url?'<a href="'+esc(m.url)+'" target="_blank">open ↗</a> ':'')+'<button class="sm" onclick="unignoreMatch(\\''+encodeURIComponent(m.url)+'\\')">↩ Restore</button></td></tr>'}).join('') : '<tr><td colspan="4" class="muted sm">Nothing ignored yet.</td></tr>';
  var ic=document.getElementById('igncount'); if(ic) ic.textContent=ign.length;
  if(_openNum!=null){var p=byNum(_openNum);if(p)renderDrawer(p);}
}
async function ignoreMatch(btn,url,co,title,posted){
  if(btn){var tr=btn.closest('tr'); if(tr)tr.style.opacity='.4';}
  await postJSON('/api/ignore',{url:decodeURIComponent(url),company:decodeURIComponent(co),title:decodeURIComponent(title),posted:posted});
  toast('Ignored — hidden from Discover'); load();
}
async function unignoreMatch(url){ await postJSON('/api/unignore',{url:decodeURIComponent(url)}); toast('Restored to Discover'); load(); }
async function selectMatch(btn,url,co,title,posted){
  if(btn){btn.disabled=true; btn.textContent='… adding';}
  var r=await postJSON('/api/select',{url:decodeURIComponent(url),company:decodeURIComponent(co),title:decodeURIComponent(title),posted:posted});
  toast(r.msg);
  if(r.ok){ if(btn){var tr=btn.closest('tr'); if(tr)tr.style.opacity='.4';} setTimeout(function(){load();view('board');},700); }
  else if(btn){btn.disabled=false; btn.textContent='➕ Select';}
}

function setFilter(k){FILTER=k;load();}

function card(p){
  var pc='<span class="dot'+(p.haspack?' on':'')+'" title="prep pack"></span><span class="dot'+(p.hasq?' on':'')+'" title="questions"></span><span class="dot'+(p.hasgap?' on':'')+'" title="gap analysis"></span>';
  var qa='';
  var st=stageOf(p.status);
  if(st==='evaluated') qa='<button class="sm p" onclick="event.stopPropagation();act(\\'apply\\',{num:\\''+p.num+'\\'})">Mark applied</button>';
  else if(!p.hasq) qa='<button class="sm" onclick="event.stopPropagation();act(\\'questions\\',{num:\\''+p.num+'\\'})">Gen Qs</button>';
  else qa='<button class="sm" onclick="event.stopPropagation();openMock(\\''+p.num+'\\')">Rehearse</button>';
  return '<div class="kc" onclick="openDrawer(\\''+p.num+'\\')">'+
    '<div class="top"><div><div class="co">'+esc(p.company)+'</div></div><span class="score">'+esc(p.score)+'</span></div>'+
    '<div class="ro">'+esc(p.role)+'</div>'+
    '<div class="rb"><i style="width:'+p.ready+'%;background:'+rc(p.ready)+'"></i></div>'+
    '<div class="meta"><span style="color:'+rc(p.ready)+'">'+p.ready+'% ready</span><span class="pc">'+pc+'</span></div>'+
    (p.contact?'<div class="meta"><span class="warm">🤝 warm path</span></div>':'')+
    '<div class="qa">'+qa+'</div>'+
  '</div>';
}

function renderBoard(){
  var q=(document.getElementById('q').value||'').toLowerCase();
  var cols=STAGES.filter(function(st){return FILTER===null||FILTER===st[0]});
  document.getElementById('board').innerHTML=cols.map(function(st){
    var items=DATA.pipeline.filter(function(p){return stageOf(p.status)===st[0] &&
      (!q || (p.company+' '+p.role).toLowerCase().indexOf(q)>=0)});
    return '<div class="col"><h3>'+st[1]+'<span class="c">'+items.length+'</span></h3>'+
      (items.length?items.map(card).join(''):'<div class="empty">—</div>')+'</div>';
  }).join('');
}

/* ---------- drawer ---------- */
var _openNum=null;
function openDrawer(num){_openNum=num;var p=byNum(num);if(!p)return;renderDrawer(p);
  document.getElementById('drawer').classList.add('show');document.getElementById('scrim').classList.add('show');document.body.style.overflow='hidden';}
function closeDrawer(){_openNum=null;document.getElementById('drawer').classList.remove('show');document.getElementById('scrim').classList.remove('show');document.body.style.overflow='';}
function renderDrawer(p){
  var chips='<span class="chip'+(p.haspack?' on':'')+'">Prep pack '+(p.haspack?'✓':'—')+'</span>'+
            '<span class="chip'+(p.hasq?' on':'')+'">Questions '+(p.hasq?'✓':'—')+'</span>'+
            '<span class="chip'+(p.hasgap?' on':'')+'">Gap analysis '+(p.hasgap?'✓':'—')+'</span>';
  var gaps=(p.gaps&&p.gaps.length)?p.gaps.map(function(g){return '<li>'+esc(g)+'</li>'}).join(''):'<li class="muted">—</li>';
  var h='<div class="dhub"><button class="x" onclick="closeDrawer()">×</button>'+
    '<div class="co">'+esc(p.company)+'</div><div class="ro">'+esc(p.role)+'</div>'+
    '<span class="badge">'+esc(p.status)+' · score '+esc(p.score)+'</span></div><div class="dbody">'+
    '<div class="sec"><div class="h">Readiness — real practice with feedback</div><div class="rowb"><div class="rb"><i style="width:'+p.ready+'%;background:'+rc(p.ready)+'"></i></div><b style="color:'+rc(p.ready)+'">'+p.ready+'%</b></div>'+
      '<div class="sm muted" style="margin-top:7px">🎙 '+(p.nmock||0)+' real mock rep(s) — '+((p.nmock||0)>=2?'ready':'do '+(2-(p.nmock||0))+' more to be ready')+'</div>'+
      '<div class="h" style="margin-top:13px">Materials assembled <span style="text-transform:none;font-weight:400;color:var(--muted)">(useful, but not the same as being ready)</span></div>'+
      '<div style="margin-top:5px">'+chips+'</div></div>'+
    '<div class="sec"><div class="h">🔗 Job posting</div>'+
      (p.jd?('<a href="'+esc(p.jd)+'" target="_blank" rel="noopener">View JD ↗</a> <span class="sm muted">'+(/(linkedin|indeed|glassdoor|ziprecruiter)/i.test(p.jd)?'(3rd-party — may expire)':'(company / ATS)')+'</span>'):'<span class="sm muted">no saved link</span>')+
      '<div style="margin-top:6px">'+
      '<a class="sm" href="https://www.google.com/search?q='+encodeURIComponent(p.company+' careers '+p.role)+'" target="_blank" rel="noopener">🔎 Find on company careers ↗</a>'+
      (p.jdsaved?' &nbsp; <button class="sm" onclick="act(\\'open_jd\\',{num:\\''+p.num+'\\'})">📄 Saved JD</button>':' &nbsp; <span class="sm muted">no archived copy yet</span>')+
      '</div></div>'+
    '<div class="sec"><div class="h">▶ Next action</div><b>'+esc(p.nextact)+'</b></div>'+
    '<div class="sec"><div class="h">⚠️ Gaps to close</div><ul class="glist">'+gaps+'</ul></div>'+
    '<div class="sec"><div class="h">🤝 Warm path</div>'+(p.contact?esc(p.contact):'<span class="muted">none yet — find a contact</span>')+'</div>'+
    '<div class="sec"><div class="h">🏢 Company brief</div><button class="sm" onclick="brief(\\''+p.num+'\\')">Generate brief</button><div id="briefbox" style="display:none" class="briefbox"></div></div>'+
    '<div class="sec"><div class="h">🚀 Assisted apply <span style="text-transform:none;font-weight:400;color:var(--muted)">(fills fields; you attach résumé + submit)</span></div>'+
      (p.hasresume?'<button class="sm p" onclick="act(\\'open_resume\\',{num:\\''+p.num+'\\'})">📄 Open tailored résumé</button> ':'<span class="sm muted">no tailored résumé PDF in output/ yet. </span>')+
      '<a class="bmk" href="'+bmHref()+'" onclick="toast(\\'Drag me to your bookmarks bar, then click me on a Greenhouse form\\');return false">🔖 JobHelm Autofill</a>'+
      '<div class="sm muted" style="margin-top:6px">Drag the bookmarklet up to your bookmarks bar once. On a Greenhouse application form, click it to fill name, email, phone, location, LinkedIn. You attach the résumé and click Submit yourself.</div>'+
      '<details style="margin-top:7px"><summary class="sm" style="cursor:pointer">Copy field pack (works on any ATS)</summary><pre class="fieldpack">'+fieldPack()+'</pre></details>'+
    '</div>'+
    '<div class="sec"><div class="h">Actions</div><div class="actions">'+
      '<button class="sm p" onclick="act(\\'questions\\',{num:\\''+p.num+'\\'})">🧠 Gen questions</button>'+
      (p.haspack?'<button class="sm" onclick="act(\\'open\\',{co:byNum(\\''+p.num+'\\').company})">📂 Open pack</button>':'')+
      '<button class="sm p" onclick="openMock(\\''+p.num+'\\')">🎤 Rehearse here</button>'+
      '<button class="sm" onclick="act(\\'mock\\',{num:\\''+p.num+'\\'})">🎙 Voice (Terminal)</button>'+
      (stageOf(p.status)==='evaluated'?'<button class="sm" onclick="act(\\'apply\\',{num:\\''+p.num+'\\'})">✓ Mark applied</button>':'')+
    '</div>'+
    '<div class="actions" style="margin-top:8px">'+
      '<button class="sm" title="Posting closed / cancelled / no longer available" onclick="if(confirm(\\'Discard '+esc(p.company)+'? (posting closed / not available) — it drops off the active board.\\'))act(\\'discard\\',{num:\\''+p.num+'\\'})">🗑 Discard (not available)</button>'+
      '<button class="sm" title="Rejected by the company" onclick="if(confirm(\\'Mark '+esc(p.company)+' Rejected? It drops off the active board.\\'))act(\\'reject\\',{num:\\''+p.num+'\\'})">✗ Rejected</button>'+
    '</div></div>'+
    '<div class="sec"><div class="h">✍️ Draft a reply (review before sending)</div>'+
      '<textarea id="dmsg" style="width:100%;min-height:64px" placeholder="Paste recruiter/HM message for '+esc(p.company)+'…"></textarea>'+
      '<div style="margin-top:7px"><button class="sm p" onclick="draft(\\'dmsg\\',\\'ddraft\\')">Draft reply</button> <span class="sm muted">flags spam · adds your mobile</span></div>'+
      '<div id="ddraft" style="display:none" class="draftbox"></div></div>'+
    '</div>';
  document.getElementById('drawer-inner').innerHTML=h;
}

/* ---------- in-app rehearsal (no Terminal / FluidVoice) ---------- */
var MOCK={num:null,history:[]};
async function postJSON(u,b){return (await fetch(u,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(b||{})})).json();}
function speakQ(text){
  try{
    if(!window.speechSynthesis)return;
    var line=text, ls=text.split('\\n');
    for(var i=0;i<ls.length;i++){var s=ls[i].replace(/[*#`>_]/g,'').trim();
      if(s.toUpperCase().indexOf('QUESTION:')===0){line=s.substring(s.indexOf(':')+1).trim();break;}}
    speechSynthesis.cancel(); var u=new SpeechSynthesisUtterance(line); u.rate=1.0; speechSynthesis.speak(u);
  }catch(e){}
}
function renderMock(){
  var el=document.getElementById('mockconvo'); if(!el)return;
  el.innerHTML=MOCK.history.map(function(h){
    var mc=h.role==='scorecard'; var who=mc?'Scorecard':(h.role==='assistant'?'Interviewer':'You');
    var cls=mc?'mc':(h.role==='assistant'?'mi':'mu');
    return '<div class="mturn '+cls+'"><b>'+who+'</b><div>'+esc(h.content).replace(/\\n/g,'<br>')+'</div></div>';
  }).join(''); el.scrollTop=el.scrollHeight;
}
async function openMock(num){
  var p=byNum(num); if(!p)return; MOCK={num:num,history:[]};
  document.getElementById('drawer-inner').innerHTML=
    '<div class="dhub"><button class="x" onclick="closeMock()">×</button><div class="co">🎤 Rehearse — '+esc(p.company)+'</div><div class="ro">'+esc(p.role)+'</div></div>'+
    '<div class="dbody"><div id="mockconvo" class="mockconvo"><div class="sm muted"><span class="spin"></span>starting…</div></div>'+
    '<textarea id="mockans" style="width:100%;min-height:80px" placeholder="Type your answer (or use your OS dictation right here), then Submit…"></textarea>'+
    '<div style="margin-top:8px;display:flex;gap:8px;flex-wrap:wrap">'+
      '<button class="sm p" onclick="mockSubmit()">Submit answer</button>'+
      '<button class="sm" onclick="mockRepeat()">🔊 Repeat question</button>'+
      '<button class="sm" onclick="mockFinish()">Finish → scorecard</button></div>'+
    '<div class="sm muted" style="margin-top:6px">In-app rehearsal — no Terminal, no FluidVoice. Questions are read aloud by the browser; type or dictate in the box. Finishing saves a transcript and counts as a real rep.</div></div>';
  document.getElementById('drawer').classList.add('show');document.getElementById('scrim').classList.add('show');document.body.style.overflow='hidden';
  var r=await postJSON('/api/mock_start',{num:num});
  if(r.ok){MOCK.history.push({role:'assistant',content:r.turn});renderMock();speakQ(r.turn);}
  else{document.getElementById('mockconvo').innerHTML='<div class="sm" style="color:var(--red)">'+esc(r.msg)+'</div>';}
}
async function mockSubmit(){
  var ta=document.getElementById('mockans'); var a=(ta.value||'').trim(); if(!a){toast('Type an answer first');return}
  MOCK.history.push({role:'user',content:a}); ta.value=''; renderMock();
  var el=document.getElementById('mockconvo'); el.insertAdjacentHTML('beforeend','<div class="sm muted" id="mockwait"><span class="spin"></span>evaluating…</div>'); el.scrollTop=el.scrollHeight;
  var r=await postJSON('/api/mock_reply',{num:MOCK.num,history:MOCK.history});
  var w=document.getElementById('mockwait'); if(w)w.remove();
  if(r.ok){MOCK.history.push({role:'assistant',content:r.turn});renderMock();speakQ(r.turn);}
  else{toast(r.msg);}
}
function mockRepeat(){var last=null;for(var i=MOCK.history.length-1;i>=0;i--){if(MOCK.history[i].role==='assistant'){last=MOCK.history[i].content;break;}}if(last)speakQ(last);}
async function mockFinish(){
  if(!MOCK.history.some(function(h){return h.role==='user'})){toast('Answer at least one question first');return}
  var el=document.getElementById('mockconvo'); el.insertAdjacentHTML('beforeend','<div class="sm muted" id="mockwait"><span class="spin"></span>scoring…</div>'); el.scrollTop=el.scrollHeight;
  var r=await postJSON('/api/mock_finish',{num:MOCK.num,history:MOCK.history});
  var w=document.getElementById('mockwait'); if(w)w.remove();
  if(r.ok){MOCK.history.push({role:'scorecard',content:r.scorecard});renderMock();if(window.speechSynthesis)speechSynthesis.cancel();toast('Saved '+(r.saved||'')+' — counts as a real rep');setTimeout(load,700);}
  else{toast(r.msg);}
}
function closeMock(){if(window.speechSynthesis)speechSynthesis.cancel();closeDrawer();}

/* generic global draft (no role) */
function openDraft(){_openNum=null;
  document.getElementById('drawer-inner').innerHTML='<div class="dhub"><button class="x" onclick="closeDrawer()">×</button><div class="co">✍️ Draft a reply</div><div class="ro">Paste any recruiter or hiring-manager message</div></div>'+
    '<div class="dbody"><div class="sec"><textarea id="dmsg" style="width:100%;min-height:120px" placeholder="Paste the message here…"></textarea>'+
    '<div style="margin-top:9px"><button class="sm p" onclick="draft(\\'dmsg\\',\\'ddraft\\')">Draft reply</button> <span class="sm muted">flags spam · adds your mobile · never auto-sent</span></div>'+
    '<div id="ddraft" style="display:none" class="draftbox"></div></div></div>';
  document.getElementById('drawer').classList.add('show');document.getElementById('scrim').classList.add('show');document.body.style.overflow='hidden';}

function openSetup(){_openNum=null;
  var s=DATA.setup||{};
  function fld(id,label,ph,ta){return '<div class="sec"><div class="h">'+label+'</div>'+(ta?
    '<textarea id="set_'+id+'" style="width:100%;min-height:150px" placeholder="'+ph+'"></textarea>':
    '<input id="set_'+id+'" style="width:100%" placeholder="'+ph+'">')+'</div>';}
  document.getElementById('drawer-inner').innerHTML=
    '<div class="dhub"><button class="x" onclick="closeDrawer()">×</button><div class="co">🚀 Getting started</div><div class="ro">Enter your details once — JobHelm writes them locally. Nothing leaves your machine.</div></div>'+
    '<div class="dbody">'+
    '<div class="sm muted" style="margin-bottom:10px">Status: résumé '+(s.cv?'✅':'—')+' · profile '+(s.profile?'✅':'—')+' · API key '+(s.key?'✅':'—')+'</div>'+
    fld('name','Your name','e.g. Alex Rivera')+
    '<div style="display:flex;gap:8px"><div style="flex:1">'+fld('email','Email','you@example.com')+'</div><div style="flex:1">'+fld('phone','Phone (optional)','555-0100')+'</div></div>'+
    fld('location','Location / work preference','e.g. Remote (US) or Atlanta, GA')+
    fld('titles','Target roles (comma-separated)','e.g. Director Platform Engineering, VP Infrastructure, Head of SRE')+
    fld('key','OpenRouter API key','sk-or-... (get one at openrouter.ai) — powers drafts, briefs, rehearse')+
    fld('resume','Paste your résumé','Paste your full résumé here (plain text or markdown). This becomes your cv.md.',true)+
    '<div style="margin-top:6px"><button class="sm p" onclick="saveSetup()">💾 Save &amp; get started</button> <span id="setupmsg" class="sm muted"></span></div>'+
    '<div class="sm muted" style="margin-top:8px">Writes to <code>cv.md</code>, <code>config/profile.yml</code>, and <code>openrouter.env</code> — all local. Edit anytime.</div>'+
    '</div>';
  document.getElementById('drawer').classList.add('show');document.getElementById('scrim').classList.add('show');document.body.style.overflow='hidden';}
async function saveSetup(){
  var g=function(id){var e=document.getElementById('set_'+id);return e?e.value:'';};
  var body={name:g('name'),email:g('email'),phone:g('phone'),location:g('location'),titles:g('titles'),key:g('key'),resume:g('resume')};
  document.getElementById('setupmsg').textContent='saving…';
  var r=await postJSON('/api/setup',body);
  document.getElementById('setupmsg').textContent=r.msg;
  if(r.ok){toast(r.msg);setTimeout(function(){load();closeDrawer();},900);}
}

/* ---------- actions ---------- */
var _busy={};
async function act(kind,args){
  if(_busy[kind]){toast('Still working on that — one moment…');return}
  _busy[kind]=true; setTimeout(function(){_busy[kind]=false},4000);
  toast('<span class="spin"></span>Working…',true);
  try{var r=await (await fetch('/api/'+kind,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(args||{})})).json();toast(r.msg);}
  catch(e){toast('Action failed.');_busy[kind]=false;return}
  setTimeout(load,500);}  // _busy clears on the 4s timer above — covers the mock process-startup window
async function doScan(){
  if(_busy['scan']){toast('A scan is already running — one moment…');return}
  _busy['scan']=true;
  var t0=Date.now();
  var tick=setInterval(function(){var s=Math.round((Date.now()-t0)/1000);toast('<span class="spin"></span>Scanning job boards… '+s+'s (usually 30–60s). Results will appear under Discover → Scan coverage.',true);},1000);
  try{
    var r=await (await fetch('/api/scan',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({})})).json();
    clearInterval(tick); _busy['scan']=false;
    await load();
    view('discover');
    toast(r.msg||'Scan complete.');
    var el=document.getElementById('scancov'); if(el){el.parentElement.style.boxShadow='0 0 0 2px var(--accent)';setTimeout(function(){el.parentElement.style.boxShadow='';},1800);}
  }catch(e){clearInterval(tick);_busy['scan']=false;toast('Scan failed — see terminal.');}
}
async function genForCo(enc){var co=decodeURIComponent(enc);var p=DATA.pipeline.find(function(x){return x.company===co});if(p)act('questions',{num:p.num});else toast('Role not found');}
async function brief(num){var box=document.getElementById('briefbox');box.style.display='block';box.innerHTML='<span class="spin"></span>researching…';
  var p=byNum(num);var r=await (await fetch('/api/brief',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({co:p.company})})).json();
  box.textContent=r.brief||r.msg;}
async function draft(inId,outId){var m=document.getElementById(inId).value;if(!m.trim()){toast('Paste a message first');return}
  var o=document.getElementById(outId);o.style.display='block';o.innerHTML='<span class="spin"></span>drafting…';
  var r=await (await fetch('/api/draft',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({message:m})})).json();
  o.textContent=r.draft||r.msg;toast(r.msg);}

document.addEventListener('keydown',function(e){if(e.key==='Escape')closeDrawer()});
load();
</script></body></html>"""

class H(BaseHTTPRequestHandler):
    def log_message(self,*a): pass
    def _send(self,code,body,ct="application/json"):
        b=body.encode() if isinstance(body,str) else body
        self.send_response(code);self.send_header("Content-Type",ct);self.send_header("Content-Length",str(len(b)))
        self.end_headers();self.wfile.write(b)
    def do_GET(self):
        p=urlparse(self.path).path
        if p=="/": self._send(200,PAGE,"text/html")
        elif p=="/api/data": self._send(200,json.dumps(build_state()))
        else: self._send(404,"{}")
    def do_POST(self):
        p=urlparse(self.path).path
        ln=int(self.headers.get("Content-Length") or 0)
        args=json.loads(self.rfile.read(ln) or "{}") if ln else {}
        if   p=="/api/scan":  self._send(200,json.dumps(do_scan()))
        elif p=="/api/apply": self._send(200,json.dumps(do_apply(args.get("num"))))
        elif p=="/api/select": self._send(200,json.dumps(do_select(args.get("url",""),args.get("company",""),args.get("title",""),args.get("posted",""))))
        elif p=="/api/setup": self._send(200,json.dumps(do_setup(args)))
        elif p=="/api/ignore": self._send(200,json.dumps(do_ignore(args.get("url",""),args.get("company",""),args.get("title",""),args.get("posted",""))))
        elif p=="/api/unignore": self._send(200,json.dumps(do_unignore(args.get("url",""))))
        elif p=="/api/discard": self._send(200,json.dumps(do_discard(args.get("num"))))
        elif p=="/api/reject": self._send(200,json.dumps(do_reject(args.get("num"))))
        elif p=="/api/mock":  self._send(200,json.dumps(do_mock(args.get("num"),args.get("co",""))))
        elif p=="/api/draft": self._send(200,json.dumps(do_draft(args.get("message",""))))
        elif p=="/api/open":  self._send(200,json.dumps(do_open(args.get("co",""))))
        elif p=="/api/open_jd": self._send(200,json.dumps(do_open_jd(args.get("num"))))
        elif p=="/api/open_resume": self._send(200,json.dumps(do_open_resume(args.get("num"))))
        elif p=="/api/questions": self._send(200,json.dumps(do_questions(args.get("num"))))
        elif p=="/api/questions_all": self._send(200,json.dumps(do_questions_all()))
        elif p=="/api/brief": self._send(200,json.dumps(do_brief(args.get("co",""))))
        elif p=="/api/mock_start":  self._send(200,json.dumps(do_mock_start(args.get("num"))))
        elif p=="/api/mock_reply":  self._send(200,json.dumps(do_mock_reply(args.get("num"),args.get("history",[]))))
        elif p=="/api/mock_finish": self._send(200,json.dumps(do_mock_finish(args.get("num"),args.get("history",[]))))
        else: self._send(404,"{}")

if __name__=="__main__":
    srv=ThreadingHTTPServer((HOST,PORT),H)
    print(f"JobHelm Mission Control → http://localhost:{PORT}  (bind {HOST}:{PORT}, Ctrl-C to stop)")
    if HOST in ("127.0.0.1","localhost"):
        try: threading.Timer(0.8,lambda: webbrowser.open(f"http://localhost:{PORT}")).start()
        except Exception: pass
    srv.serve_forever()
