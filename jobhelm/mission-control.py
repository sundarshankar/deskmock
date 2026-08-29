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

def match_score(title, loc):
    # zero-LLM heuristic fit (1.0-5.0) from title+location vs the profile. A triage signal, not a full eval.
    t=(title or "").lower(); l=(loc or "").lower(); s=2.5
    if re.search(r'\bvp\b|vice president|head of|\bcto\b|senior director|sr\.? director', t): s+=1.5
    elif re.search(r'\bdirector\b', t): s+=1.2
    elif re.search(r'\bmanager\b|\blead\b', t): s+=0.3
    if re.search(r'platform engineering|cloud infrastructure|site reliab|\bsre\b|infrastructure|\binfra\b', t): s+=1.0
    elif re.search(r'platform|cloud|reliability|devops', t): s+=0.6
    elif re.search(r'engineering|technolog', t): s+=0.3
    if re.search(r'remote|anywhere|atlanta|georgia|\bga\b', l): s+=0.5
    elif (not l) or re.search(r'united states|\bus\b', l): s+=0.2
    return round(min(5.0, max(1.0, s)), 1)

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
            rows.append(dict(url=url, company=company, title=title, posted=posted, score=match_score(title, loc)))
    def d(s):
        try: return datetime.date.fromisoformat(s)
        except Exception: return None
    ds = [d(r["posted"]) for r in rows if d(r["posted"])]
    mx = max(ds) if ds else None
    rows = [r for r in rows if d(r["posted"]) and mx and (mx-d(r["posted"])).days <= 7]
    ign = _ignored_set()
    rows = [r for r in rows if r["url"].split("?")[0] not in ign]
    rows.sort(key=lambda r: (r["score"], r["posted"]), reverse=True)   # best fit first
    seen=set(); uniq=[]                                                # collapse same role from
    for r in rows:                                                     # different boards (LinkedIn + ATS)
        k=(slug(r["company"]), slug(r["title"]))
        if k in seen: continue
        seen.add(k); uniq.append(r)
    return uniq[:40]

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
        profile=profile(), scan=scan_coverage(), setup=setup_status(), readiness=readiness_summary(),
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
    # widen the net — JobSpy (LinkedIn/Indeed/Glassdoor/ZipRecruiter/Google) if its venv is set up.
    # Point JOBHELM_JOBSPY at the python in a venv where `pip install python-jobspy` has been run.
    _jv=os.environ.get("JOBHELM_JOBSPY","")
    jv=pathlib.Path(_jv) if _jv else None
    if jv and jv.exists() and (CO/"jobspy-scan.py").exists():
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

def do_unselect(num):
    ok,out=run(["node","set-status.mjs",str(num),"Discarded","--note","Unselected from board via JobHelm"],CO)
    return dict(ok=ok, msg="↩ Unselected — removed from your board." if ok else f"Failed: {out[-200:]}")

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
    msgs.append({"role":"user","content":"The interview is over. Give me the SCORECARD now — strengths, gaps, and "
        "concrete next steps. End with EXACTLY this line (integers 1-5): "
        "SCORES: Leadership n/5 | Technical n/5 | Behavioral n/5 | Communication n/5"})
    try: card=_llm(msgs,key,900)
    except Exception as e: return dict(ok=False,msg=f"Scorecard failed: {e}")
    _record_readiness(a["company"] if a else "role", card)
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

def _llm(msgs, key, max_tokens=900, model=None, json_mode=False, web=False):
    payload={"model":(model or "deepseek/deepseek-v3.2"),"temperature":0.5,"max_tokens":max_tokens,"messages":msgs}
    if json_mode: payload["response_format"]={"type":"json_object"}
    if web: payload["plugins"]=[{"id":"web","max_results":6}]
    body=json.dumps(payload).encode()
    req=urllib.request.Request("https://openrouter.ai/api/v1/chat/completions",data=body,
        headers={"Authorization":"Bearer "+key,"Content-Type":"application/json","X-Title":"JobHelm"})
    with urllib.request.urlopen(req,timeout=90) as r:
        return json.load(r)["choices"][0]["message"]["content"].strip()

# rehearsal-safety: every metric you'd say out loud must trace to your CV
_ATTR_RULE=("When you cite a metric, use the CV's EXACT figure for that specific employer — never round, "
            "inflate, or attribute a global/aggregate number (e.g. total engineers led) to a single company. ")

def _jd_text_for(a):
    # the actual posting for this role: archived JD file (career-ops jds/), else tracker notes
    f=saved_jd(a)
    if f:
        t=read(CO/"jds"/f)
        if t.strip(): return t[:4000], "archived JD"
    note=(a.get("note") or a.get("notes") or "").strip()
    return (note[:1500], "tracker note") if note else ("", "")

def _gen_questions_for(a):
    company, role = a["company"], a["role"]
    key=load_key()
    if not key: return (False,"no key")
    cv=read(CO/"cv.md")[:3500]
    jd, jdsrc = _jd_text_for(a)
    sys=("Generate a focused interview prep pack for a SPECIFIC job posting, grounded in the candidate's real CV. "
         "Return markdown: 8-10 likely questions across Behavioral, Technical, and Leadership (tag each with its "
         "dimension). "
         + ("Target THIS posting: pull the questions from the job description's named stack, responsibilities, and "
            "must-haves — a technical question for each key technology/practice it names, leadership/behavioral "
            "questions for the scope and challenges it implies. " if jd else
            "No JD text is available, so target the role title and seniority. ")
         + "For EACH question provide:\n"
         "1. **Question**\n"
         "2. **Model answer** — a ready-to-review answer written in the candidate's first-person voice, grounded "
         "ONLY in their real CV. Use STAR (Situation, Task, Action, Result) for behavioral/leadership; a "
         "structured technical explanation for technical. Confident, specific, plain-ASCII, no smart quotes or "
         "em-dashes. CRITICAL: never invent facts, metrics, employers, or projects. "+_ATTR_RULE+"Where a specific "
         "number or example would strengthen the answer but is NOT in the CV, insert a bracketed prompt like "
         "[add your metric] or [add a specific example] instead of making one up.\n"
         "3. **Power phrases** — 2 crisp expressions to reuse.\n"
         "Write in first person throughout. Do NOT add a candidate-name line or any name placeholder like "
         "[PERSON_NAME]/[Candidate Name] — the reader is the candidate.\n"
         "Realistic for the role and seniority. The goal: the candidate can scan, internalize, and rehearse these.")
    usr=(f"Role: {role} at {company}.\n\n"
         + (f"JOB DESCRIPTION (target the questions to THIS posting):\n{jd}\n\n" if jd else "")
         + f"Candidate CV (the ONLY source of truth for facts):\n{cv}"
         + (("\n\nREAL questions already asked at "+company+" (from a past interview - PREPARE THESE FIRST):\n"+"\n".join("- "+q for d,q in question_bank_for(company)[:8])) if question_bank_for(company) else "")
         + "\n\nProduce the prep pack with model answers.")
    try:
        md=_llm([{"role":"system","content":sys},{"role":"user","content":usr}],key,3200)
    except Exception as e:
        return (False,str(e))
    md=re.sub(r'^[*\s>_-]*Candidate:\s*\[[^\]]*\]\s*$','',md,flags=re.I|re.M)
    md=re.sub(r'\[(?:person[_ ]?name|candidate[_ ]?name|your[_ ]?name|full[_ ]?name|name)\]','',md,flags=re.I)
    tag = f"tailored to the {jdsrc}" if jd else "role-level (no JD on file)"
    out=CO/"interview-prep"/f"{slug(company)}-questions.md"
    out.write_text(f"# Interview prep — {role} @ {company}\n_(questions + model answers, {tag}, grounded in your CV. Review, personalize the [brackets], then rehearse in DeskMock.)_\n\n{md}\n")
    prep_files.append(out.name)
    return (True, out.name + (f" ({jdsrc})" if jd else " (no JD — role-level)"))

def do_questions(num):
    a=next((x for x in apps() if x["num"]==str(num)),None)
    if not a: return dict(ok=False,msg="Role not found.")
    rt=_gen_qset_for(a,"technical"); rb=_gen_qset_for(a,"behavioral")
    ok = rt[0] or rb[0]
    got=[lbl for r,lbl in ((rt,"technical"),(rb,"behavioral")) if r[0]]
    if not ok: return dict(ok=False, msg=f"Failed: {rt[1]}")
    return dict(ok=True, msg="Generated "+" + ".join(got)+" Q&A ✓ — rehearse in DeskMock.")

def do_questions_all():
    done=0; fail=0
    targets=[a for a in apps() if a["status"].lower() in ("applied","interview","responded")
             and not next((f for f in prep_files if slug(a["company"]) in slug(f) and "technical" in f.lower()),"")]
    for a in targets:
        rt=_gen_qset_for(a,"technical"); rb=_gen_qset_for(a,"behavioral")
        done+=1 if (rt[0] or rb[0]) else 0; fail+=0 if (rt[0] or rb[0]) else 1
    return dict(ok=True, msg=f"Generated technical + behavioral Q&A for {done} applied role(s)" + (f"; {fail} failed" if fail else "") + ".")

# curated open-source prep resources by dimension (mirrors ../PREP-RESOURCES.md)
CURATED={
 "leadership":[("kaushikb9/em-interviews","EM/Director/VP interview questions"),
   ("engineering-management/awesome-engineering-management","EM & leadership practice"),
   ("ronikobrosly/awesome-data-leadership","data/eng leadership"),
   ("kuchin/awesome-cto","CTO/exec-level playbook")],
 "behavioral":[("ashishps1/awesome-behavioral-interviews","STAR prep, common questions"),
   ("yangshun/tech-interview-handbook","behavioral + general")],
 "technical":[("dastergon/awesome-sre","the canonical SRE reading list"),
   ("seifrajhi/awesome-platform-engineering-tools","platform engineering landscape"),
   ("wmariuss/awesome-devops","DevOps practices & tooling")],
 "system":[("donnemartin/system-design-primer","the standard system-design ref"),
   ("ashishps1/awesome-system-design-resources","patterns & case studies")],
}
def _reslinks(*cats):
    out=[]
    for c in cats:
        for n,d in CURATED.get(c,[]): out.append(f"- [{n}](https://github.com/{n}) — {d}")
    return "\n".join(out)

def _untraced_metrics(md, cv):
    cvn=re.sub(r'[,\s]','',cv.lower())
    bad=[]
    # metric-like tokens only; units must be ATTACHED so "2025 Kubernetes" / "1. Building" aren't misread
    pat=r'\$\d[\d,\.]*\s?[kmb]?\+?|\d[\d,\.]*%|\d[\d,\.]*(?:billion|million)\b|\d[\d,\.]*[kmb]\+?(?![a-z])|\d{3,}\+?'
    for t in re.findall(pat, md, re.I):
        core=re.sub(r'[,\s]','',t.lower())
        if re.fullmatch(r'(19|20)\d\d', core): continue
        base=re.sub(r'(\+|%|billion|million|[kmb])+$','',core)
        if core in cvn or (base and base in cvn): continue
        bad.append(t.strip())
    return sorted(set(bad))

def _gen_leadership_for(a):
    company,role=a["company"],a["role"]
    key=load_key()
    if not key: return (False,"no key")
    cv=read(CO/"cv.md")[:3000]; jd,jdsrc=_jd_text_for(a)
    sys=("Create a LEADERSHIP round prep brief for a senior platform/cloud/infrastructure leader (Director/VP/Head). "
         "Grounded ONLY in the candidate's real CV; never invent facts, metrics, employers, or teams. "+_ATTR_RULE+
         "Plain-ASCII, first person, no name or [PERSON_NAME] placeholder. Markdown with these sections:\n"
         + ("Tailor to the JD below — reference the org scope, team size, and challenges it names.\n" if jd else "")
         + "## Leadership themes this round will probe\n(org design & scaling teams, hiring/retention, stakeholder & "
         "exec influence, reliability-vs-cost tradeoffs, driving operational culture, delivery under constraint) — "
         "pick the 4-5 most relevant to THIS role.\n"
         "## My leadership narrative\n3-4 first-person talking points that map the candidate's REAL experience to "
         "those themes (STAR-ish; use [add a specific example]/[add your metric] where the CV lacks a detail).\n"
         "## Likely leadership questions\n5-6 questions with a one-line angle each on how to answer from the CV.")
    real=[q for d,q in question_bank_for(company) if d.lower().startswith("lead")]
    realblk=("\n\nREAL leadership questions already asked at "+company+" (prepare these):\n"+"\n".join("- "+q for q in real[:6])) if real else ""
    usr=f"Role: {role} at {company}.\n\n"+(f"JOB DESCRIPTION:\n{jd}\n\n" if jd else "")+f"Candidate CV (only source of truth):\n{cv}{realblk}\n\nProduce the leadership brief."
    try: md=_llm([{"role":"system","content":sys},{"role":"user","content":usr}],key,2600)
    except Exception as e: return (False,str(e))
    md=re.sub(r'\[(?:person[_ ]?name|candidate[_ ]?name|your[_ ]?name|full[_ ]?name|name)\]','',md,flags=re.I)
    out=CO/"interview-prep"/f"{slug(company)}-leadership.md"
    out.write_text(f"# Leadership prep — {role} @ {company}\n_(grounded in your CV; personalize the [brackets]. {'Tailored to '+jdsrc if jd else 'role-level'}.)_\n\n{md}\n\n## Curated leadership & behavioral resources\n{_reslinks('leadership','behavioral')}\n")
    prep_files.append(out.name)
    return (True,out.name)

def _gen_articles_for(a):
    company,role=a["company"],a["role"]
    key=load_key()
    if not key: return (False,"no key")
    jd,jdsrc=_jd_text_for(a)
    web=os.environ.get("JOBHELM_WEB","1")!="0"   # live article fetch via OpenRouter web search (set 0 to disable)
    if web:
        sys=("You build a STACK READING GUIDE so a candidate sounds current in a technical interview. Use the WEB "
             "SEARCH RESULTS available to you to find REAL, RECENT articles. From the role (and JD if given), pick "
             "the 5-7 key technologies/practices that matter (Kubernetes, IaC/Terraform, service mesh, observability/"
             "SRE, FinOps, CI/CD, platform engineering, AI infra). For EACH: **what to be current on** (one line), "
             "**why it matters for THIS role** (one line), and **1-2 recent articles** as markdown links "
             "[title](url). Cite ONLY genuine technical sources — engineering blogs, project docs, conference "
             "talks, or news ABOUT THE TECHNOLOGY. NEVER cite a job listing, recruiting site, or a company "
             "careers/greenhouse/lever page — those are not articles; discard them. Use only real URLs the search "
             "returned; if you found no genuine technical article for a topic, write 'Search: <query>' instead of "
             "inventing a link. Never fabricate a URL, headline, or date. Plain-ASCII, no name placeholder.")
    else:
        sys=("You build a STACK READING GUIDE so a candidate sounds current in a technical interview. "
             "From the role (and JD if given), identify the 5-7 key technologies/practices that matter. For EACH: "
             "**what to be current on** (one line) and **why it matters for THIS role** (one line). Then a short "
             "**'go deeper'** list of search queries to run for THIS WEEK's articles (do NOT fabricate headlines, "
             "dates, or URLs — give the search query instead). Plain-ASCII, no name placeholder.")
    usr=f"Role: {role} at {company}.\n\n"+(f"JOB DESCRIPTION:\n{jd}\n\n" if jd else "")+"Produce the stack reading guide with recent real articles."
    try: md=_llm([{"role":"system","content":sys},{"role":"user","content":usr}],key,2400,web=web)
    except Exception:
        try: md=_llm([{"role":"system","content":sys},{"role":"user","content":usr}],key,2200)  # fall back w/o web
        except Exception as e: return (False,str(e))
    md=re.sub(r'\[(?:person[_ ]?name|candidate[_ ]?name|your[_ ]?name|full[_ ]?name|name)\]','',md,flags=re.I)
    out=CO/"interview-prep"/f"{slug(company)}-articles.md"
    sub="recent real articles fetched live" if web else "run the search queries for this week's writing"
    out.write_text(f"# Stack & articles — {role} @ {company}\n_(key topics to be current on{' from '+jdsrc if jd else ''}; {sub}.)_\n\n{md}\n\n## Curated technical, SRE & system-design resources\n{_reslinks('technical','system')}\n")
    prep_files.append(out.name)
    return (True,out.name)

def _md2html(md):
    out=[]; lst=None
    def close():
        nonlocal lst
        if lst: out.append(f"</{lst}>"); lst=None
    def inline(s):
        s=html.escape(s)
        s=re.sub(r'\[([^\]]+)\]\((https?://[^\s)]+)\)', r'<a href="\2">\1</a>', s)
        s=re.sub(r'\*\*([^*]+)\*\*', r'<strong>\1</strong>', s)
        s=re.sub(r'(?<!\*)\*([^*]+)\*(?!\*)', r'<em>\1</em>', s)
        s=re.sub(r'`([^`]+)`', r'<code>\1</code>', s)
        return s
    for raw in (md or "").split('\n'):
        ln=raw.rstrip()
        if not ln.strip(): close(); continue
        m=re.match(r'(#{1,4})\s+(.*)', ln)
        if m: close(); n=len(m.group(1)); out.append(f"<h{n}>{inline(m.group(2))}</h{n}>"); continue
        if ln.strip() in ('---','***','___'): close(); out.append('<hr>'); continue
        m=re.match(r'\s*[-*+]\s+(.*)', ln)
        if m:
            if lst!='ul': close(); out.append('<ul>'); lst='ul'
            out.append(f"<li>{inline(m.group(1))}</li>"); continue
        m=re.match(r'\s*\d+[\.\)]\s+(.*)', ln)
        if m:
            if lst!='ol': close(); out.append('<ol>'); lst='ol'
            out.append(f"<li>{inline(m.group(1))}</li>"); continue
        close(); out.append(f"<p>{inline(ln)}</p>")
    close()
    return "\n".join(out)

_PREP_CSS=("<style>body{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,Helvetica,Arial,sans-serif;"
  "color:#1a1a1a;line-height:1.5;max-width:760px;margin:0 auto;padding:36px 40px}"
  "h1{font-size:21px;border-bottom:2px solid #1a1a2e;padding-bottom:6px;margin:30px 0 12px}"
  "h2{font-size:16px;color:#1a1a2e;margin:22px 0 6px}h3{font-size:13.5px;margin:15px 0 4px}"
  "p{margin:7px 0}li{margin:4px 0}a{color:#1a1a2e;text-decoration:none}code{background:#f2f2f4;padding:1px 5px;"
  "border-radius:3px;font-size:12px}hr{border:none;border-top:1px solid #ddd;margin:18px 0}em{color:#444}"
  ".pgbreak{page-break-before:always}</style>")

# ---- prep dimensions: attack each interview round separately ----
PREP_DIMS=[("leadership","🎯 Leadership"),("technical","🛠 Technical"),("behavioral","💬 Behavioral"),("articles","📰 Stack & Articles")]
_QSET={
 "behavioral":("Behavioral (STAR)",
   "Generate 6 behavioral questions a senior-leadership panel asks — leading through change, conflict/disagreement, "
   "failure and what you learned, influencing without authority, hiring and growing people, delivering under "
   "constraint. Answer EACH in STAR (Situation, Task, Action, Result), first person.",
   ("behavioral",)),
 "technical":("Technical",
   "Generate 7 technical questions from the JD's named stack and this role's domain (architecture at scale, "
   "reliability/SRE, cloud & Kubernetes, IaC/CI-CD, observability, cost/FinOps, incident/DR). Answer EACH with a "
   "structured, specific technical explanation grounded in the candidate's real experience.",
   ("technical","system")),
}
def _gen_qset_for(a, dim):
    company,role=a["company"],a["role"]; key=load_key()
    if not key: return (False,"no key")
    label,brief,rescats=_QSET[dim]
    cv=read(CO/"cv.md")[:3500]; jd,jdsrc=_jd_text_for(a)
    sys=(f"Build a focused {label} interview-prep set for a SPECIFIC job posting, grounded in the candidate's real CV. "
         + ("Target THIS posting's stack, responsibilities, and must-haves. " if jd else "Target the role title and seniority. ")
         + brief + " For EACH question provide: **Question**; **Model answer** (ready-to-review, candidate's first-person "
         "voice, grounded ONLY in the CV; plain-ASCII, no smart quotes/em-dashes); **Power phrases** (2 reusable lines). "
         + _ATTR_RULE + "Where a number/example would help but is NOT in the CV, insert [add your metric]/[add a specific "
         "example] — never invent. First person throughout; no candidate-name line or [PERSON_NAME] placeholder.")
    # real questions this company already asked (from past debriefs) — prioritize preparing them
    real=[q for d,q in question_bank_for(company) if d.lower().startswith(dim[:4])]
    realblk=("\n\nREAL questions already asked at "+company+" (from a past interview — PREPARE THESE FIRST, then add "
             "others):\n"+"\n".join("- "+q for q in real[:8])) if real else ""
    usr=f"Role: {role} at {company}.\n\n"+(f"JOB DESCRIPTION (target the questions to THIS posting):\n{jd}\n\n" if jd else "")+f"Candidate CV (only source of truth):\n{cv}{realblk}\n\nProduce the {label} set."
    try: md=_llm([{"role":"system","content":sys},{"role":"user","content":usr}],key,2800)
    except Exception as e: return (False,str(e))
    md=re.sub(r'^[*\s>_-]*Candidate:\s*\[[^\]]*\]\s*$','',md,flags=re.I|re.M)
    md=re.sub(r'\[(?:person[_ ]?name|candidate[_ ]?name|your[_ ]?name|full[_ ]?name|name)\]','',md,flags=re.I)
    tag=f"tailored to the {jdsrc}" if jd else "role-level (no JD on file)"
    if real: tag+=f" · includes {len(real)} real asked-question(s)"
    out=CO/"interview-prep"/f"{slug(company)}-{dim}.md"
    out.write_text(f"# {label} prep — {role} @ {company}\n_(model answers, {tag}, grounded in your CV. Personalize [brackets], rehearse in DeskMock.)_\n\n{md}\n\n## Curated {label} resources\n{_reslinks(*rescats)}\n")
    prep_files.append(out.name)
    return (True,out.name)

def _make_prep_pack_file(company, role):
    # Combine the three prep .md files into one document. Always produce HTML (no deps);
    # render a clean PDF too IF career-ops' generate-pdf.mjs is available (Playwright).
    parts=[]
    for s,_l in PREP_DIMS:
        f=CO/"interview-prep"/f"{slug(company)}-{s}.md"
        if f.exists() and read(f).strip():
            cls=' class="pgbreak"' if parts else ''
            parts.append(f"<section{cls}>"+_md2html(read(f))+"</section>")
    if not parts: return (False, None, "no pack files to render")
    doc=f"<!doctype html><html><head><meta charset='utf-8'>{_PREP_CSS}</head><body>"+"\n".join(parts)+"</body></html>"
    outhtml=CO/"interview-prep"/f"{slug(company)}-prep.html"; outhtml.write_text(doc)
    if (CO/"generate-pdf.mjs").exists():
        outpdf=CO/"interview-prep"/f"{slug(company)}-prep-pack.pdf"
        ok,gout=run(["node","generate-pdf.mjs",str(outhtml),str(outpdf),"--format=letter"],CO)
        if ok:
            cm=HERE.parent/"clean-markers.mjs"   # bundled at the deskmock repo root
            if cm.exists(): run(["node",str(cm),"clean",str(outpdf),"--author",(NAME or "Candidate")], cm.parent)
            return (True, outpdf, outpdf.name)
    return (True, outhtml, outhtml.name+" (HTML — open in a browser, print to PDF)")

def do_preppack(num):
    a=next((x for x in apps() if x["num"]==str(num)),None)
    if not a: return dict(ok=False,msg="Role not found.")
    r={}
    r["leadership"]=_gen_leadership_for(a)
    r["technical"]=_gen_qset_for(a,"technical")
    r["behavioral"]=_gen_qset_for(a,"behavioral")
    r["articles"]=_gen_articles_for(a)
    ok=all(v[0] for v in r.values())
    jd,jdsrc=_jd_text_for(a)
    LBL={"leadership":"leadership","technical":"technical Q&A","behavioral":"behavioral Q&A","articles":"stack & articles"}
    parts=[(k,LBL[k]) for k in ("leadership","technical","behavioral","articles")]
    got=[label for k,label in parts if r[k][0]]
    fail=[f"{label}: {r[k][1]}" for k,label in parts if not r[k][0]]
    cv=read(CO/"cv.md")
    blob="\n".join(read(CO/"interview-prep"/f"{slug(a['company'])}-{s}.md") for s,_ in PREP_DIMS)
    untraced=_untraced_metrics(blob, cv)
    pdfok,_pth,pdfinfo=_make_prep_pack_file(a["company"],a["role"])
    tag = f" (JD-tailored via {jdsrc})" if jd else " (role-level — no JD on file)"
    msg=f"Prep pack for {a['company']}{tag}: "+", ".join(got)+(" ✓" if ok else "")
    msg+= (" · metrics: all trace to your CV ✓" if not untraced
           else " · ⚠️ VERIFY these metrics (not found verbatim in CV): "+", ".join(untraced[:6]))
    msg+= (f" · doc: {pdfinfo}" if pdfok else f" · doc failed: {pdfinfo}")
    if fail: msg+=" — failed: "+"; ".join(fail)
    return dict(ok=ok, msg=msg, untraced=untraced)

def do_open_prep_pdf(num):
    a=next((x for x in apps() if x["num"]==str(num)),None)
    if not a: return dict(ok=False,msg="Role not found.")
    sc=slug(a['company'])
    pdf=CO/"interview-prep"/f"{sc}-prep-pack.pdf"; htmlf=CO/"interview-prep"/f"{sc}-prep.html"
    target = pdf if pdf.exists() else (htmlf if htmlf.exists() else None)
    if target is None:
        if not any((CO/"interview-prep"/f"{sc}-{s}.md").exists() for s,_l in PREP_DIMS):
            return dict(ok=False,msg="No prep pack yet — click 'Generate prep pack' first.")
        ok,target,info=_make_prep_pack_file(a["company"],a["role"])
        if not ok: return dict(ok=False,msg=f"Could not build prep doc: {info}")
    try: subprocess.Popen(["open", str(target)])
    except Exception as e: return dict(ok=False,msg=str(e))
    return dict(ok=True,msg=f"Opened {target.name}")


def do_open_dimension(num, dim):
    a=next((x for x in apps() if x["num"]==str(num)),None)
    if not a: return dict(ok=False,msg="Role not found.")
    if dim not in dict(PREP_DIMS): return dict(ok=False,msg="Unknown prep dimension.")
    company=a["company"]; md=CO/"interview-prep"/f"{slug(company)}-{dim}.md"
    if not md.exists() or not read(md).strip():
        return dict(ok=False,msg=f"No {dict(PREP_DIMS)[dim]} prep yet — click 'Generate prep pack' first.")
    return dict(ok=True,msg=f"Opened {_render_open(md, f'{slug(company)}-{dim}')}")

def do_preppack_selected():
    dead={"rejected","discarded","skip","hired"}
    live=[a for a in apps() if a["status"].lower() not in dead]
    done=[]; failed=[]
    for a in live:
        r=do_preppack(a["num"])
        (done if r["ok"] else failed).append(a["company"])
    return dict(ok=True, msg=f"Prep packs built for {len(done)} selected role(s)"+(f"; issues: {', '.join(failed)}" if failed else "")+".")

# ---- Behavioral Story Bank: your real STAR stories once, mapped to any question ----
def _render_open(md_path, stem):
    doc=f"<!doctype html><html><head><meta charset='utf-8'>{_PREP_CSS}</head><body>"+_md2html(read(md_path))+"</body></html>"
    outhtml=CO/"interview-prep"/f"{stem}.html"; outhtml.write_text(doc); target=outhtml
    if (CO/"generate-pdf.mjs").exists():
        pdf=CO/"interview-prep"/f"{stem}.pdf"; ok,_g=run(["node","generate-pdf.mjs",str(outhtml),str(pdf),"--format=letter"],CO)
        if ok:
            cm=HERE.parent/"clean-markers.mjs"
            if cm.exists(): run(["node",str(cm),"clean",str(pdf),"--author",(NAME or "Candidate")], cm.parent)
            target=pdf
    try: subprocess.Popen(["open", str(target)])
    except Exception: pass
    return target.name

def do_story_bank():
    key=load_key()
    if not key: return dict(ok=False,msg="No OpenRouter key.")
    cv=read(CO/"cv.md")[:5000]
    sys=("Build a reusable BEHAVIORAL STORY BANK from the candidate's real CV, so they can answer ANY behavioral "
         "interview question by mapping it to a story they actually lived. Extract 8-12 DISTINCT stories that span a "
         "range of competencies. For EACH story use exactly this markdown:\n"
         "### <short memorable title>\n"
         "**Competencies:** 2-4 tags from {Leadership, Scaling Teams, Hiring/Retention, Conflict/Disagreement, "
         "Influence without authority, Reliability/Incident, Cost/FinOps, Migration/Transformation, Delivery under "
         "pressure, Failure/Learning, Stakeholder/Exec}\n"
         "**Situation:** ...\n**Task:** ...\n**Action:** ...\n**Result:** ...\n"
         "Ground EVERY story ONLY in the CV. Where a specific number or detail would strengthen it but is NOT in the "
         "CV, insert [add your metric] or [add a specific example] — NEVER invent facts, metrics, or projects. "
         +_ATTR_RULE+
         " Cover diverse competencies so common questions each have a matching story: leading change, conflict, "
         "failure/learning, influencing up, scaling a team, a hard cost-vs-reliability tradeoff, delivery under "
         "pressure. First person, plain-ASCII, no [PERSON_NAME].\n"
         "End with:\n## Question -> Story map\nA markdown table: | Behavioral question | Use this story | — 8-10 common "
         "questions mapped to the best story above.")
    usr=f"Candidate CV (the ONLY source of truth for facts):\n{cv}\n\nProduce the story bank + question-to-story map."
    try: md=_llm([{"role":"system","content":sys},{"role":"user","content":usr}],key,3600)
    except Exception as e: return dict(ok=False,msg=f"Failed: {e}")
    md=re.sub(r'\[(?:person[_ ]?name|candidate[_ ]?name|your[_ ]?name|full[_ ]?name|name)\]','',md,flags=re.I)
    out=CO/"interview-prep"/"story-bank.md"
    out.write_text("# Behavioral Story Bank — grounded in your CV\n_(Your real STAR stories, tagged by competency. "
                   "Master these ~10, then map any behavioral question to one. Personalize the [brackets].)_\n\n"+md+"\n")
    prep_files.append(out.name)
    n=md.count('### '); untraced=_untraced_metrics(md, read(CO/"cv.md"))
    name=_render_open(out, "story-bank")
    msg=f"Story Bank built ✓ — {n} stories ({name})"
    msg+= (" · metrics all trace to your CV ✓" if not untraced else " · ⚠️ verify: "+", ".join(untraced[:6]))
    return dict(ok=True,msg=msg)

# ---- adaptive readiness: rehearse -> per-dimension score -> drill the weakest ----
DIMS_SCORE=["Leadership","Technical","Behavioral","Communication"]
def _record_readiness(company, card):
    m=re.search(r'SCORES:\s*(.+)', card or "")
    if not m: return
    rows=[]
    for dim in DIMS_SCORE:
        mm=re.search(dim+r'\s*[:=]?\s*(\d(?:\.\d)?)\s*/\s*5', m.group(1), re.I)
        if mm: rows.append((dim, mm.group(1)))
    if not rows: return
    f=CO/"data"/"readiness.tsv"; f.parent.mkdir(parents=True,exist_ok=True)
    ts=datetime.datetime.now().strftime("%Y-%m-%d %H:%M")
    with f.open("a") as fh:
        for dim,sc in rows: fh.write(f"{ts}\t{slug(company)}\t{dim}\t{sc}\n")

def readiness_summary():
    hist={d:[] for d in DIMS_SCORE}
    for ln in read(CO/"data"/"readiness.tsv").splitlines():
        c=ln.split("\t")
        if len(c)>=4 and c[2] in hist:
            try: hist[c[2]].append(float(c[3]))
            except Exception: pass
    dims=[]
    for d in DIMS_SCORE:
        h=hist[d]
        if not h: dims.append(dict(dim=d,score=None,reps=0,trend=0,spark=[]))
        else: dims.append(dict(dim=d,score=round(sum(h)/len(h),1),latest=h[-1],reps=len(h),
                               trend=round(h[-1]-h[0],1) if len(h)>1 else 0,spark=h[-8:]))
    scored=[x for x in dims if x["reps"]>0]
    weakest=min(scored,key=lambda x:x["score"])["dim"] if scored else None
    return dict(dims=dims, weakest=weakest, total_reps=sum(x["reps"] for x in dims))


def do_drill(dim=""):
    key=load_key()
    if not key: return dict(ok=False,msg="No OpenRouter key.")
    dim=dim if dim in DIMS_SCORE else (readiness_summary().get("weakest") or "Behavioral")
    cv=read(CO/"cv.md")[:3500]
    sys=(f"The candidate scores WEAKEST at {dim} in mock interviews, so drill it hard. Generate 6 ESCALATING {dim} "
         "questions — tougher and more probing than a normal set. For EACH: **Question**; **Model answer** grounded "
         "ONLY in the candidate's real CV (STAR for behavioral/leadership; structured technical for technical); "
         "**Interviewer follow-up** that pressure-tests depth. "+_ATTR_RULE+"Never invent facts; use [add your metric]/"
         "[add a specific example] for gaps. First person, plain-ASCII, no [PERSON_NAME].")
    usr=f"Dimension to drill: {dim}.\n\nCandidate CV (only source of truth):\n{cv}\n\nProduce the escalated {dim} drill."
    try: md=_llm([{"role":"system","content":sys},{"role":"user","content":usr}],key,2800)
    except Exception as e: return dict(ok=False,msg=f"Failed: {e}")
    md=re.sub(r'\[(?:person[_ ]?name|candidate[_ ]?name|your[_ ]?name|full[_ ]?name|name)\]','',md,flags=re.I)
    out=CO/"interview-prep"/f"drill-{slug(dim)}.md"
    out.write_text(f"# {dim} drill — escalated (your weakest area)\n_(harder probing questions + model answers, "
                   f"grounded in your CV. Rehearse these, then re-mock to lift your {dim} score.)_\n\n{md}\n")
    prep_files.append(out.name)
    name=_render_open(out, f"drill-{slug(dim)}")
    return dict(ok=True, msg=f"🎯 {dim} drill generated + opened ({name}) — your weakest area. Rehearse, then re-mock to raise the score.")

def do_negotiation(num):
    a=next((x for x in apps() if x["num"]==str(num)),None)
    if not a: return dict(ok=False,msg="Role not found.")
    key=load_key()
    if not key: return dict(ok=False,msg="No OpenRouter key.")
    company=a["company"]; role=a["role"]
    cv=read(CO/"cv.md")[:3000]; jd,jdsrc=_jd_text_for(a); prof=read(CO/"config/profile.yml")[:1500]
    sys=("Create a SALARY NEGOTIATION prep brief for a senior platform/infrastructure leadership candidate. Markdown "
         "sections:\n"
         "## Comp benchmark (ESTIMATE — verify)\nA rough total-comp range for THIS role/level/location: base, target "
         "bonus, equity. State PLAINLY these are estimates and the candidate must verify on levels.fyi, Glassdoor, and "
         "Blind before anchoring. NEVER present a number as a fact.\n"
         "## Your leverage\n4-5 points drawn from the candidate's REAL CV that justify top-of-band (scope, scale, "
         "outcomes, scarcity of the skill set). Grounded ONLY in the CV.\n"
         "## Anchoring & counters\nConcrete scripts: how to respond when asked for a number, how to counter a first "
         "offer, how to use a competing offer honestly, and the non-salary levers (equity, sign-on, title, remote, "
         "start date, review timeline).\n"
         "## Scenarios\n3 common scenarios (a lowball, 'this is our max', an exploding offer) with a calm, specific "
         "response to each.\n"
         "First person, plain-ASCII, no [PERSON_NAME]. "+_ATTR_RULE+"Never invent the candidate's own numbers.")
    usr=(f"Role: {role} at {company}.\n\n"+(f"Candidate comp target/floor (from profile):\n{prof}\n\n" if prof.strip() else "")
         +(f"JOB DESCRIPTION:\n{jd}\n\n" if jd else "")+f"Candidate CV (only source of truth for their own facts):\n{cv}\n\nProduce the negotiation brief.")
    try: md=_llm([{"role":"system","content":sys},{"role":"user","content":usr}],key,2800)
    except Exception as e: return dict(ok=False,msg=f"Failed: {e}")
    md=re.sub(r'\[(?:person[_ ]?name|candidate[_ ]?name|your[_ ]?name|full[_ ]?name|name)\]','',md,flags=re.I)
    out=CO/"interview-prep"/f"{slug(company)}-negotiation.md"
    out.write_text(f"# Negotiation prep — {role} @ {company}\n_(Leverage from your CV + scripts. Comp figures are "
                   f"ESTIMATES — verify on levels.fyi / Glassdoor / Blind before anchoring.)_\n\n{md}\n")
    prep_files.append(out.name)
    name=_render_open(out, f"{slug(company)}-negotiation")
    return dict(ok=True, msg=f"💰 Negotiation prep for {company} generated + opened ({name}). Comp numbers are estimates — verify before you anchor.")

# ---- post-interview debrief -> question bank (real questions sharpen future prep) ----
def do_debrief(num, notes):
    a=next((x for x in apps() if x["num"]==str(num)),None)
    if not a: return dict(ok=False,msg="Role not found.")
    notes=(notes or "").strip()
    if not notes: return dict(ok=False,msg="Paste your interview notes first (what was asked, how it went).")
    key=load_key()
    if not key: return dict(ok=False,msg="No OpenRouter key.")
    company=a["company"]; role=a["role"]
    sys=("Structure the candidate's raw post-interview notes into a clean debrief. Use ONLY what they wrote — never "
         "invent questions, answers, or outcomes. Markdown sections:\n"
         "## Questions I was asked\n(a list; tag each with [Behavioral]/[Technical]/[Leadership]/[Other]; clean up the "
         "wording but keep the meaning)\n## What went well\n## What to improve\n## Follow-ups & next steps\n"
         "Then, at the very end, output a machine block EXACTLY like:\n```questions\n[Technical] the question text\n"
         "[Behavioral] the question text\n```\n(one asked-question per line, tag in brackets). Plain-ASCII, first person.")
    usr=f"Role: {role} at {company}.\n\nMy raw notes:\n{notes[:3500]}\n\nStructure the debrief + machine block."
    try: md=_llm([{"role":"system","content":sys},{"role":"user","content":usr}],key,2200)
    except Exception as e: return dict(ok=False,msg=f"Failed: {e}")
    qb=re.search(r'```questions\s*(.*?)```', md, re.S); nq=0
    if qb:
        f=CO/"data"/"question-bank.tsv"; f.parent.mkdir(parents=True,exist_ok=True)
        with f.open("a") as fh:
            for ln in qb.group(1).splitlines():
                ln=ln.strip()
                if not ln: continue
                mt=re.match(r'\[([^\]]+)\]\s*(.+)', ln); dim=mt.group(1) if mt else "Other"; q=(mt.group(2) if mt else ln).strip()
                if q: fh.write(f"{slug(company)}\t{dim}\t{q}\n"); nq+=1
    body=re.sub(r'```questions.*?```','',md,flags=re.S).strip()
    out=CO/"interview-prep"/f"{slug(company)}-debrief.md"
    ts=datetime.datetime.now().strftime("%Y-%m-%d")
    prior=read(out)
    header=prior if prior else f"# Interview debriefs — {role} @ {company}\n"
    out.write_text(header+f"\n## Debrief — {ts}\n\n{body}\n\n---\n")
    prep_files.append(out.name)
    return dict(ok=True, msg=f"Debrief saved ✓ — {nq} real questions added to your question bank. They'll sharpen future prep for {company} and similar roles.")

def question_bank_for(company):
    k=slug(company); out=[]
    for ln in read(CO/"data"/"question-bank.tsv").splitlines():
        c=ln.split("\t")
        if len(c)>=3 and c[0]==k: out.append((c[1],c[2]))
    return out


def do_open_story_bank():
    f=CO/"interview-prep"/"story-bank.md"
    if not f.exists(): return dict(ok=False,msg="No Story Bank yet — click '📖 Story Bank' to build it.")
    return dict(ok=True,msg=f"Opened {_render_open(f,'story-bank')}")

def flashcards_data():
    f=CO/"interview-prep"/"flashcards.json"
    if not f.exists(): return {"cards":[]}
    try: return {"cards": json.loads(read(f))}
    except Exception: return {"cards":[]}

def do_flashcards():
    key=load_key()
    if not key: return dict(ok=False,msg="No OpenRouter key.")
    cv=read(CO/"cv.md")[:5000]
    sb=CO/"interview-prep"/"story-bank.md"
    sbtxt=("\n\nSTORY BANK (use titles as answers to behavioral triggers):\n"+read(sb)[:2500]) if sb.exists() else ""
    sys=("Build a deck of 24 interview FLASHCARDS for active-recall practice, grounded ONLY in the candidate's CV. "
         "Return a JSON object {\"cards\":[{\"front\":\"...\",\"back\":\"...\",\"dim\":\"fact|behavioral|technical\"}]}. Mix:\n"
         "- ~10 'recall your own numbers' cards (dim=fact): front asks for a specific fact/metric from the CV, back is "
         "the EXACT figure (e.g. front 'eHealth data-center migration time & efficiency gain?' back '11 months, +38%').\n"
         "- ~7 behavioral-trigger cards (dim=behavioral): front is a behavioral question type, back names the best "
         "story + a one-line STAR skeleton.\n"
         "- ~7 technical-concept cards (dim=technical): front a concept/term central to the candidate's domain "
         "(SRE, Kubernetes, FinOps, IaC, service mesh, incident mgmt), back a crisp 1-2 line explanation.\n"
         "CRITICAL: never invent facts, metrics, employers, or projects — every 'fact' back must come from the CV. "
         "Keep fronts short (a prompt), backs tight (1-3 lines). Plain-ASCII, no [PERSON_NAME]. JSON only.")
    usr=f"Candidate CV (the ONLY source of truth):\n{cv}{sbtxt}\n\nProduce the flashcard JSON."
    try:
        raw=_llm([{"role":"system","content":sys},{"role":"user","content":usr}],key,3200,json_mode=True)
        cards=json.loads(raw).get("cards",[])
    except Exception as e:
        return dict(ok=False,msg=f"Failed: {e}")
    # tag ids + scrub
    import hashlib
    clean=[]; seen=set()
    for c in cards:
        if not (isinstance(c,dict) and c.get("front") and c.get("back")): continue
        front=re.sub(r'\[(?:person[_ ]?name|your[_ ]?name|name)\]','',str(c["front"]),flags=re.I).strip()
        back=re.sub(r'\[(?:person[_ ]?name|your[_ ]?name|name)\]','',str(c["back"]),flags=re.I).strip()
        cid="c"+hashlib.md5(front.lower().encode()).hexdigest()[:10]
        if cid in seen: continue
        seen.add(cid)
        clean.append({"id":cid,"front":front,"back":back,"dim":c.get("dim","fact")})
    if not clean: return dict(ok=False,msg="No cards generated — try again.")
    (CO/"interview-prep"/"flashcards.json").write_text(json.dumps(clean,indent=1))
    untraced=_untraced_metrics(" ".join(c["back"] for c in clean if c["dim"]=="fact"), read(CO/"cv.md"))
    msg=f"Flashcards built ✓ — {len(clean)} cards. Open 🃏 Flashcards to study (spaced repetition)."
    msg+= (" · fact metrics all trace ✓" if not untraced else " · ⚠️ verify fact cards: "+", ".join(untraced[:6]))
    return dict(ok=True,msg=msg)


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
    <button onclick="storyBank()" title="Build/open your behavioral STAR story bank from your CV">📖 Story Bank</button>
    <button onclick="window.open('/flashcards','_blank')" title="Spaced-repetition flashcards from your CV">🃏 Flashcards</button>
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
      <div class="panel"><h2>🎯 Interview readiness</h2><div id="readiness"></div></div>
      <div class="panel"><h2>🔥 Next best actions</h2><ul class="na" id="na"></ul></div>
      <div class="panel"><h2>📌 Standing gaps · study focus</h2><ul class="glist" id="gaps"></ul></div>
    </div>
  </div>
</div>

<!-- DISCOVER VIEW -->
<div id="v-discover" style="display:none">
  <div class="disc">
    <div class="panel" style="margin-bottom:16px"><h2>🛰 Scan coverage</h2><div id="scancov" class="sm muted">loading…</div></div>
    <div class="panel" style="margin-bottom:16px"><h2>🆕 New matches · last 7 days · best fit first</h2>
      <table><thead><tr><th>Fit</th><th>Company</th><th>Role</th><th>Posted</th><th></th></tr></thead><tbody id="nm"></tbody></table>
      <div class="sm muted" style="margin-top:8px">Fit = a quick title/location heuristic vs your profile — the full score comes when you Select &amp; evaluate.</div>
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
  renderReadiness();
  document.getElementById('gaps').innerHTML=(DATA.standing_gaps.length?DATA.standing_gaps:['Run a gap analysis to populate this.']).map(function(g){return '<li>'+esc(g)+'</li>'}).join('');
  // discover
  document.getElementById('nm').innerHTML=(DATA.new_matches.length?DATA.new_matches:[{company:'None new 🎉',title:'',posted:'',url:''}]).map(function(m){
    var args="(this,'"+encodeURIComponent(m.url||'')+"','"+encodeURIComponent(m.company||'')+"','"+encodeURIComponent(m.title||'')+"','"+esc(m.posted||'')+"')";
    var sel=(m.company&&m.title)?'<button class="sm p" title="Add to your board (To apply)" onclick="selectMatch'+args+'">➕ Select</button> ':'';
    var ig=m.url?'<button class="sm" title="Hide from Discover" onclick="ignoreMatch'+args+'">🚫 Ignore</button>':'';
    var sc=m.score||0, scc=(sc>=4?'var(--accent)':sc>=3.2?'var(--amber)':'var(--muted)');
    var sccell=m.company?'<td><b style="color:'+scc+'">'+sc.toFixed(1)+'</b><span class="sm muted">/5</span></td>':'<td></td>';
    return '<tr>'+sccell+'<td><b>'+esc(m.company)+'</b></td><td class="sm">'+esc(m.title)+'</td><td class="sm muted">'+esc(m.posted)+'</td><td style="white-space:nowrap">'+(m.url?'<a href="'+esc(m.url)+'" target="_blank">open ↗</a> ':'')+sel+ig+'</td></tr>'}).join('');
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
  if(st==='evaluated') qa='<button class="sm p" onclick="event.stopPropagation();act(\\'apply\\',{num:\\''+p.num+'\\'})">Mark applied</button> <button class="sm" title="Remove from board" onclick="event.stopPropagation();if(confirm(\\'Unselect '+esc(p.company)+' — remove from your board?\\'))act(\\'unselect\\',{num:\\''+p.num+'\\'})">↩ Unselect</button>';
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

function renderReadiness(){
  var r=(DATA.readiness)||{dims:[],weakest:null,total_reps:0};
  var el=document.getElementById('readiness'); if(!el)return;
  if(!r.total_reps){el.innerHTML='<div class="sm muted">No reps yet. Rehearse a role (🎤 Rehearse here → Finish → scorecard) and your per-dimension readiness charts here — then drill your weakest.</div>';return}
  var rows=r.dims.map(function(d){
    if(!d.reps)return '<div style="display:flex;align-items:center;gap:8px;margin:5px 0"><span style="min-width:96px;font-size:13px">'+d.dim+'</span><span class="sm muted">no reps</span></div>';
    var pct=Math.round((d.score/5)*100), col=d.score>=4?'var(--accent)':d.score>=3?'var(--amber)':'#ff6b6b';
    var tr=d.trend>0?'▲ +'+d.trend:(d.trend<0?'▼ '+d.trend:'–');
    var wk=(d.dim===r.weakest)?' <span style="color:#ff6b6b;font-size:10px;text-transform:uppercase">weakest</span>':'';
    return '<div style="display:flex;align-items:center;gap:8px;margin:5px 0"><span style="min-width:96px;font-size:13px">'+d.dim+wk+'</span>'+
      '<span style="flex:1;height:7px;background:var(--line);border-radius:4px;overflow:hidden"><i style="display:block;height:100%;width:'+pct+'%;background:'+col+'"></i></span>'+
      '<span style="font-size:12px;min-width:26px">'+d.score+'/5</span><span class="sm muted" style="min-width:56px;text-align:right">'+tr+' · '+d.reps+'r</span></div>';
  }).join('');
  var btn=r.weakest?'<button class="sm p" style="margin-top:9px" onclick="if(confirm(\\'Generate an escalated '+r.weakest+' drill (your weakest area)? ~40s\\'))act(\\'drill\\',{dim:\\''+r.weakest+'\\'})">🎯 Drill weakest — '+r.weakest+'</button>':'';
  el.innerHTML=rows+btn;
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
    '<div class="sec"><div class="h">📚 Interview prep</div><div class="actions">'+
      '<button class="sm p" onclick="if(confirm(\\'Generate the full prep pack (Leadership + Technical + Behavioral + Articles, JD-tailored) for '+esc(p.company)+'? ~90s\\'))act(\\'preppack\\',{num:\\''+p.num+'\\'})">📚 Generate prep pack</button>'+
      '<button class="sm" onclick="act(\\'open_prep_pdf\\',{num:\\''+p.num+'\\'})">📄 Full pack</button>'+
      (p.haspack?'<button class="sm" onclick="act(\\'open\\',{co:byNum(\\''+p.num+'\\').company})">📂 .md files</button>':'')+
    '</div>'+
    '<div class="sm muted" style="margin:8px 0 4px">Attack each round on its own:</div><div class="actions">'+
      '<button class="sm" onclick="dim(\\''+p.num+'\\',\\'leadership\\')">🎯 Leadership</button>'+
      '<button class="sm" onclick="dim(\\''+p.num+'\\',\\'technical\\')">🛠 Technical</button>'+
      '<button class="sm" onclick="dim(\\''+p.num+'\\',\\'behavioral\\')">💬 Behavioral</button>'+
      '<button class="sm" onclick="dim(\\''+p.num+'\\',\\'articles\\')">📰 Articles</button>'+
    '</div><div class="actions" style="margin-top:8px">'+
      '<button class="sm p" onclick="openMock(\\''+p.num+'\\')">🎤 Rehearse here</button>'+
      '<button class="sm" onclick="act(\\'mock\\',{num:\\''+p.num+'\\'})">🎙 Voice (Terminal)</button>'+
      '<button class="sm" title="Comp benchmark + leverage + scripts" onclick="if(confirm(\\'Generate salary-negotiation prep for '+esc(p.company)+'? Comp figures are estimates to verify. ~40s\\'))act(\\'negotiation\\',{num:\\''+p.num+'\\'})">💰 Negotiation prep</button>'+
    '</div><div class="sm muted" style="margin-top:6px">Each dimension is JD-tailored + CV-grounded — leadership brief, technical Q&amp;A, behavioral STAR, and a stack/articles reading guide.</div></div>'+
    '<div class="sec"><div class="h">Update stage</div><div class="actions">'+
      (stageOf(p.status)==='evaluated'?'<button class="sm p" onclick="act(\\'apply\\',{num:\\''+p.num+'\\'})">✓ Mark applied</button>':'')+
      '<button class="sm" title="Posting closed / cancelled / no longer available" onclick="if(confirm(\\'Discard '+esc(p.company)+'? (posting closed / not available) — it drops off the active board.\\'))act(\\'discard\\',{num:\\''+p.num+'\\'})">🗑 Discard (not available)</button>'+
      '<button class="sm" title="Rejected by the company" onclick="if(confirm(\\'Mark '+esc(p.company)+' Rejected? It drops off the active board.\\'))act(\\'reject\\',{num:\\''+p.num+'\\'})">✗ Rejected</button>'+
    '</div></div>'+
    '<div class="sec"><div class="h">✍️ Draft a reply (review before sending)</div>'+
      '<textarea id="dmsg" style="width:100%;min-height:64px" placeholder="Paste recruiter/HM message for '+esc(p.company)+'…"></textarea>'+
      '<div style="margin-top:7px"><button class="sm p" onclick="draft(\\'dmsg\\',\\'ddraft\\')">Draft reply</button> <span class="sm muted">flags spam · adds your mobile</span></div>'+
      '<div id="ddraft" style="display:none" class="draftbox"></div></div>'+
    '<div class="sec"><div class="h">📝 Post-interview debrief</div>'+
      '<textarea id="dbnotes" style="width:100%;min-height:70px" placeholder="After an interview, paste your notes — what they asked, how it went. I structure it and capture the real questions into your bank to sharpen future prep."></textarea>'+
      '<div style="margin-top:7px"><button class="sm p" onclick="act(\\'debrief\\',{num:\\''+p.num+'\\',notes:(document.getElementById(\\'dbnotes\\')||{}).value||\\''+'\\'})">Save debrief</button> <span class="sm muted">captures real questions -&gt; question bank</span></div></div>'+
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

function storyBank(){
  if(confirm('Build (or rebuild) your behavioral Story Bank from your CV? ~40s.\\n\\nClick Cancel to just OPEN your existing one.'))
    act('story_bank',{});
  else act('open_story_bank',{});
}
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
function dim(num,d){ act('open_dimension',{num:num,dim:d}); }
var _LONG={questions:'Generating questions + answers',preppack:'Building prep pack (questions + leadership + articles)',open_prep_pdf:'Opening prep pack',open_dimension:'Opening dimension',questions_all:'Generating question sets',preppack_selected:'Building prep packs',story_bank:'Building your behavioral story bank',drill:'Building your escalated drill',flashcards:'Building flashcards',negotiation:'Building negotiation prep',debrief:'Saving debrief + extracting questions'};
var _longActive=null;   // only ONE long generation at a time — no overlapping tickers, no server overload
async function act(kind,args){
  if(_busy[kind]){toast('Still working on that — one moment…');return}
  if(_LONG[kind]&&_longActive){toast('⏳ '+_LONG[_longActive]+' is still running — let it finish first.');return}
  _busy[kind]=true;
  var tick=null;
  if(_LONG[kind]){
    _longActive=kind;
    var t0=Date.now();
    var paint=function(){var s=Math.round((Date.now()-t0)/1000);toast('<span class="spin"></span>'+_LONG[kind]+'… '+s+'s'+(kind==='open_prep_pdf'?'':' (usually 30–60s)'),true);};
    paint(); tick=setInterval(paint,1000);
  } else { toast('<span class="spin"></span>Working…',true); }
  try{
    var r=await (await fetch('/api/'+kind,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(args||{})})).json();
    if(tick)clearInterval(tick);
    toast(r.msg||'Done.');
  }catch(e){ if(tick)clearInterval(tick); toast('Action failed — try again.'); }
  finally{ _busy[kind]=false; if(_longActive===kind)_longActive=null; }
  setTimeout(load,500);}  // lock held for the whole call so a second click can't fire a duplicate LLM request
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

FLASHCARDS_PAGE = """<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>JobHelm Flashcards</title>
<style>
:root{--bg:#0f1115;--card:#1a1d24;--line:#2a2f3a;--fg:#e6e9ef;--muted:#8a92a3;--accent:#3ecf8e;--amber:#e0a63e}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--fg);font-family:-apple-system,Segoe UI,Roboto,Arial,sans-serif;min-height:100vh;display:flex;flex-direction:column;align-items:center;padding:24px}
.top{width:100%;max-width:640px;display:flex;justify-content:space-between;align-items:center;margin-bottom:18px;gap:12px}
.top a{color:var(--muted);text-decoration:none;font-size:13px}
h1{font-size:18px;margin:0}
.stats{display:flex;gap:14px;font-size:13px;color:var(--muted)}.stats b{color:var(--fg)}
.card{width:100%;max-width:640px;background:var(--card);border:1px solid var(--line);border-radius:14px;min-height:260px;display:flex;flex-direction:column;justify-content:center;padding:34px;cursor:pointer;font-size:18px;line-height:1.55;box-shadow:0 8px 30px rgba(0,0,0,.25)}
.dim{font-size:11px;text-transform:uppercase;letter-spacing:.08em;color:var(--muted);margin-bottom:14px}
.back{border-top:1px dashed var(--line);margin-top:18px;padding-top:16px}
.hint{color:var(--muted);font-size:13px;text-align:center;margin-top:14px}
.grades{display:flex;gap:10px;width:100%;max-width:640px;margin-top:16px}
.grades button{flex:1;padding:12px;border-radius:10px;border:1px solid var(--line);background:#20242c;color:var(--fg);font-size:14px;cursor:pointer}
.grades button:hover{border-color:var(--accent)}
.g0{color:#ff6b6b}.g3{color:var(--amber)}.g4{color:var(--accent)}.g5{color:#5ec8ff}
.done{text-align:center;margin-top:56px;color:var(--muted);max-width:520px}.done b{color:var(--accent);font-size:20px}
button.b{background:var(--accent);color:#06231a;border:none;padding:10px 18px;border-radius:9px;cursor:pointer;font-weight:600;margin-top:10px}
</style></head><body>
<div class="top"><h1>🃏 Flashcards</h1><div class="stats" id="stats"></div><div><a href="#" onclick="build();return false">rebuild</a> &nbsp; <a href="/">← board</a></div></div>
<div id="app"></div>
<script>
var CARDS=[],queue=[],cur=null,shown=false,reviewed=0,LS='jobhelm_srs_v1';
function state(){try{return JSON.parse(localStorage.getItem(LS)||'{}')}catch(e){return {}}}
function save(s){try{localStorage.setItem(LS,JSON.stringify(s))}catch(e){}}
function today(){return Math.floor(Date.now()/864e5)}
function due(c){var s=state()[c.id];return !s||s.due<=today()}
function sm2(id,q){var s=state(),c=s[id]||{ease:2.5,int:0,reps:0};
  if(q<3){c.reps=0;c.int=1;}else{c.reps++;c.int=c.reps==1?1:c.reps==2?6:Math.round(c.int*c.ease);c.ease=Math.max(1.3,c.ease+(0.1-(5-q)*(0.08+(5-q)*0.02)));}
  c.due=today()+c.int;s[id]=c;save(s);}
function stats(){var s=state(),d=0,nw=0,l=0;CARDS.forEach(function(c){var x=s[c.id];if(!x)nw++;else if(x.due<=today())d++;if(x&&x.reps>=2)l++;});
  document.getElementById('stats').innerHTML='<span>due <b>'+d+'</b></span><span>new <b>'+nw+'</b></span><span>learned <b>'+l+'/'+CARDS.length+'</b></span>';}
function esc(s){var d=document.createElement('div');d.textContent=s;return d.innerHTML;}
function shuffle(a){for(var i=a.length-1;i>0;i--){var j=Math.floor(Math.random()*(i+1)),t=a[i];a[i]=a[j];a[j]=t;}return a;}
function next(){shown=false;cur=queue.shift();if(!cur){doneScreen();return}render();}
function render(){var dl={fact:'Recall your fact',behavioral:'Behavioral trigger',technical:'Technical concept'}[cur.dim]||cur.dim;
  document.getElementById('app').innerHTML='<div class="card" onclick="flip()"><div class="dim">'+dl+'</div><div>'+esc(cur.front)+'</div>'+(shown?'<div class="back">'+esc(cur.back)+'</div>':'')+'</div>'+(shown?'<div class="grades"><button class="g0" onclick="grade(0)">Again</button><button class="g3" onclick="grade(3)">Hard</button><button class="g4" onclick="grade(4)">Good</button><button class="g5" onclick="grade(5)">Easy</button></div>':'<div class="hint">click card to reveal · space to flip · 1-4 to grade</div>');stats();}
function flip(){shown=true;render();}
function grade(q){sm2(cur.id,q);reviewed++;next();}
function doneScreen(){document.getElementById('app').innerHTML='<div class="done"><b>✓ Session complete</b><p>You reviewed '+reviewed+' cards. Spaced-repetition will resurface the hard ones sooner.</p><button class="b" onclick="start(true)">Study all again</button></div>';stats();}
function start(all){reviewed=0;queue=shuffle(CARDS.filter(all?function(){return true}:due));
  if(!queue.length){document.getElementById('app').innerHTML='<div class="done"><b>All caught up 🎉</b><p>No cards due today — spaced repetition is working.</p><button class="b" onclick="start(true)">Review all anyway</button></div>';stats();return}next();}
function build(){document.getElementById('app').innerHTML='<div class="done">Building your deck from your CV… ~40s</div>';
  fetch('/api/flashcards',{method:'POST',headers:{'Content-Type':'application/json'},body:'{}'}).then(function(r){return r.json()}).then(function(d){
    if(d.ok){location.reload();}else{document.getElementById('app').innerHTML='<div class="done">'+esc(d.msg||'Failed')+'<br><button class="b" onclick="build()">Retry</button></div>';}});}
document.addEventListener('keydown',function(e){if(e.code==='Space'){e.preventDefault();if(!shown&&cur)flip();}else if(shown&&cur){if(e.key==='1')grade(0);else if(e.key==='2')grade(3);else if(e.key==='3')grade(4);else if(e.key==='4')grade(5);}});
fetch('/api/flashcards_data').then(function(r){return r.json()}).then(function(d){CARDS=d.cards||[];
  if(!CARDS.length){document.getElementById('app').innerHTML='<div class="done">No flashcards yet.<p>Build a deck from your CV — 24 active-recall cards (your metrics, behavioral triggers, technical concepts).</p><button class="b" onclick="build()">Build my deck (~40s)</button></div>';stats();return}start(false);});
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
        elif p=="/flashcards": self._send(200,FLASHCARDS_PAGE,"text/html")
        elif p=="/api/data": self._send(200,json.dumps(build_state()))
        elif p=="/api/flashcards_data": self._send(200,json.dumps(flashcards_data()))
        else: self._send(404,"{}")
    def do_POST(self):
        p=urlparse(self.path).path
        ln=int(self.headers.get("Content-Length") or 0)
        args=json.loads(self.rfile.read(ln) or "{}") if ln else {}
        if   p=="/api/scan":  self._send(200,json.dumps(do_scan()))
        elif p=="/api/apply": self._send(200,json.dumps(do_apply(args.get("num"))))
        elif p=="/api/select": self._send(200,json.dumps(do_select(args.get("url",""),args.get("company",""),args.get("title",""),args.get("posted",""))))
        elif p=="/api/unselect": self._send(200,json.dumps(do_unselect(args.get("num"))))
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
        elif p=="/api/preppack": self._send(200,json.dumps(do_preppack(args.get("num"))))
        elif p=="/api/preppack_selected": self._send(200,json.dumps(do_preppack_selected()))
        elif p=="/api/story_bank": self._send(200,json.dumps(do_story_bank()))
        elif p=="/api/open_story_bank": self._send(200,json.dumps(do_open_story_bank()))
        elif p=="/api/negotiation": self._send(200,json.dumps(do_negotiation(args.get("num"))))
        elif p=="/api/debrief": self._send(200,json.dumps(do_debrief(args.get("num"),args.get("notes",""))))
        elif p=="/api/flashcards": self._send(200,json.dumps(do_flashcards()))
        elif p=="/api/drill": self._send(200,json.dumps(do_drill(args.get("dim",""))))
        elif p=="/api/open_prep_pdf": self._send(200,json.dumps(do_open_prep_pdf(args.get("num"))))
        elif p=="/api/open_dimension": self._send(200,json.dumps(do_open_dimension(args.get("num"),args.get("dim",""))))
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
