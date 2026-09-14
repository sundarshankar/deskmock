#!/usr/bin/env python3
"""JobHelm — unit/logic tests against the bundled sample data. No side effects
(Terminal launch is stubbed; no LLM/network calls)."""
import os, sys, importlib.util, pathlib, datetime, subprocess
_REAL_POPEN = subprocess.Popen   # tests below stub Popen; keep the real one for node --check

HERE = pathlib.Path(__file__).resolve().parent
# Point the app at the bundled sample data BEFORE import (module reads env at import time).
os.environ["JOBHELM_CAREEROPS"] = str(HERE / "sample-data")
os.environ["JOBHELM_MOCK"]      = str(HERE / "sample-data" / "mock")
os.environ.setdefault("JOBHELM_NAME",   "Alex Rivera")
os.environ.setdefault("JOBHELM_MOBILE", "555-0100")
os.environ.setdefault("JOBHELM_EMAIL",  "alex.rivera@example.com")
# The sample postings carry fixed dates, but Discover's age window is anchored to
# TODAY — so the relevance tests below rot into failures purely by the calendar
# moving. Open the window wide here and test the window itself separately.
os.environ["JOBHELM_DISCOVER_DAYS"] = "36500"

spec = importlib.util.spec_from_file_location("mc", str(HERE / "mission-control.py"))
mc = importlib.util.module_from_spec(spec); spec.loader.exec_module(mc)

PASS = FAIL = 0
def check(name, cond, detail=""):
    global PASS, FAIL
    if cond: PASS += 1; print(f"  ok   {name}")
    else:    FAIL += 1; print(f"  FAIL {name}  {detail}")

print("== data layer (sample) ==")
A = mc.apps()
check("apps() parses 5 sample rows", len(A) == 5, f"got {len(A)}")
check("fields present", all(k in A[0] for k in ("num","company","role","score","status")))
st = mc.build_state()
check("build_state keys", all(k in st for k in ("stats","pipeline","new_matches","next_actions","standing_gaps","ts")))
check("each node has nmock + materials", all("nmock" in p and "materials" in p for p in st["pipeline"]))

print("== readiness = real practice only (materials don't count) ==")
check("Datawright readiness = 50 (one real mock)", mc.readiness("Datawright") == 50, mc.readiness("Datawright"))
check("real_mocks_for Datawright = 1", mc.real_mocks_for("Datawright") == 1)
check("Vertex Cloud readiness = 0 (materials/no real mock)", mc.readiness("Vertex Cloud") == 0)
check("readiness within 0..100", all(0 <= mc.readiness(p["company"]) <= 100 for p in st["pipeline"]))

print("== materials tracked separately ==")
check("Datawright materials = 65 (pack+gap+questions)", any(p["company"]=="Datawright" and p["materials"]==65 for p in st["pipeline"]))

print("== profile is configurable ==")
check("profile name from sample profile.yml", mc.profile()["name"] == "Alex Rivera", mc.profile()["name"])

print("== new-matches filter ==")
coms = [m["company"] for m in st["new_matches"]]
check("keeps senior platform/infra roles", ("Skyforge" in coms or "Meridian Labs" in coms), coms)
check("drops off-target (Sales Enablement)", "Fernwood Retail" not in coms, coms)

# ...and the window itself: with a 1-day window nothing from the fixed sample dates survives.
_wide = mc.DISCOVER_DAYS
mc.DISCOVER_DAYS = 1
check("the age window drops stale postings", not mc.build_state()["new_matches"])
mc.DISCOVER_DAYS = _wide
check("reopening the window brings them back", len(mc.build_state()["new_matches"]) > 0)

print("== follow-ups resolve company names ==")
fu = mc.followups_due()
check("no raw '- next' leaks", all(not c.startswith("- next") for _, c in fu))

print("== do_mock guard + command construction (Terminal stubbed) ==")
cap = {}
class FakePopen:
    def __init__(self, args, *a, **k): cap["args"] = args
mc.subprocess.Popen = FakePopen; mc._mock_running = lambda: False
mc.do_mock(num="5")  # Datawright / Director, Platform Engineering
cmd = cap.get("args", [""])[-1]
check("mock passes --company", "--company" in cmd, cmd)
check("mock tags the real company", "Datawright" in cmd, cmd)
launched = {"n": 0}
class CountPopen:
    def __init__(self, *a, **k): launched["n"] += 1
mc.subprocess.Popen = CountPopen; mc._mock_running = lambda: True
r = mc.do_mock(co="Datawright")
check("guard blocks a second Terminal", launched["n"] == 0 and "already open" in r["msg"].lower())

print("== in-app rehearsal ==")
mm, _ = mc._mock_messages("5", [])
check("mock messages start with system + name the company", mm[0]["role"]=="system" and "Datawright" in mm[0]["content"])
check("mock_reply requires an answer", mc.do_mock_reply("5", [])["ok"] is False)

print("== error handling (no LLM, no mutation) ==")
check("brief empty handled", mc.do_brief("")["ok"] is False)
check("draft empty handled", mc.do_draft("")["ok"] is False)
check("questions invalid num handled", mc.do_questions("9999")["ok"] is False)

print("== contact-placeholder scrub ==")
out = mc._scrub_contact_placeholders("Best,\nAlex\n[PHONE]")
check("scrubs [PHONE] to the configured mobile", out.strip().endswith(mc.MOBILE), out)
check("leaves normal text alone", mc._scrub_contact_placeholders("Talk soon.") == "Talk soon.")

print("== field-adaptive prep: works for non-tech AND tech (no LLM) ==")
# Representative CV text — a finance-leadership candidate (non-tech) and a platform-eng candidate (tech).
FINANCE = ("Director of FP&A and Corporate Finance. DCF and LBO modeling, valuation, US GAAP/IFRS, SOX compliance, "
           "budgeting and forecasting, variance analysis, month-end close, SAP/Oracle/Hyperion, CPA.")
TECH    = ("Director of Platform Engineering. Kubernetes, SRE, Terraform/IaC, cloud infrastructure, CI/CD, "
           "observability, service mesh, incident management.")
# 1) field detector
check("field detector: finance CV -> NON-tech", mc._looks_tech(FINANCE) is False, mc._looks_tech(FINANCE))
check("field detector: tech CV -> tech",        mc._looks_tech(TECH) is True,     mc._looks_tech(TECH))
# 2) tech-only curated GitHub resources are gated by field
check("curated tech resources shown for tech role", "github.com" in mc._res_section("technical", ("technical",), True))
check("curated tech resources hidden for non-tech role", mc._res_section("technical", ("technical",), False) == "")
# 3) the domain/technical question prompt is field-adaptive, not hardcoded to tech
tqp = mc._QSET["technical"][1].lower()
check("technical prompt infers the candidate's field", "infer" in tqp and "field" in tqp, tqp[:60])
check("technical prompt covers non-tech fields (e.g. finance)", "finance" in tqp)
check("technical prompt does not force software-only", "do not default to software" in tqp)

print("== cross-platform file open (stubbed — no real window) ==")
_origPopen = mc.subprocess.Popen
mc.subprocess.Popen = lambda *a, **k: None   # stub launcher so the test opens nothing
try:
    check("_open_file exists", callable(getattr(mc, "_open_file", None)))
    check("_open_file returns True when a launcher succeeds", mc._open_file("/tmp/nonexistent.pdf") is True)
finally:
    mc.subprocess.Popen = _origPopen

print("== Discover suppression: seen/applied roles must not resurface ==")
# role hash folds the label differences that made one posting look like several
check("Sr. == Senior in a role hash",
      mc.role_hash("Acme", "Sr. Director, Platform Engineering")
      == mc.role_hash("Acme", "Senior Director, Platform Engineering"))
check("VP == Vice President, and '(Remote)' is not identity",
      mc.role_hash("Acme", "VP of Engineering")
      == mc.role_hash("Acme", "Vice President of Engineering (Remote)"))
check("ATS slug and display name are one employer", mc._co_match("wex", "wexinc"))
check("long shared prefix collapses a tenant slug", mc._co_match("lightspeedhq", "lightspeedcommerce"))
check("distinct employers stay distinct", not mc._co_match("nvidia", "visa"))
check("a short slug cannot swallow a longer name", not mc._co_match("jj", "jjill"))

# tracker rows suppress, whatever their status — Discover kept re-listing applied roles
_apps = mc.apps()
if _apps:
    _a = _apps[0]
    _idx = mc.suppressed_index()
    check("a tracked role is suppressed by company+role, not by URL",
          bool(mc.suppression_reason(_a["company"], _a["role"], "https://example.invalid/never-seen", _idx)),
          f'{_a["company"]} / {_a["role"]}')
    check("an unrelated role is not suppressed",
          not mc.suppression_reason("Nonesuch Industries", "Chief Zamboni Officer", "https://example.invalid/x", _idx))

# agencies are demoted, never hidden — and never at the cost of a real employer
check("a staffing firm is recognised", mc.is_agency("BizTech Staffing"))
check("an aggregator is recognised", mc.is_agency("Ladders") and mc.is_agency("jobgether"))
check("de-spaced slugs are recognised too", mc.is_agency("talentmanagementsolution"))
check("a real employer is not flagged as an agency",
      not any(mc.is_agency(c) for c in ("Visa", "Experian", "Partners Healthcare", "Antares Capital LP")))

_rows, _hidden, _meta = mc.pipeline_recent()
check("pipeline_recent returns rows, hidden counts and meta", isinstance(_rows, list) and isinstance(_meta, dict))
check("the age window is anchored to today, not to the newest row",
      _meta["end"] == datetime.date.today().isoformat(), _meta.get("end"))
check("the true match total is reported, not the truncated count",
      _meta["total"] >= len(_rows[:_meta["shown"]]))
check("every row carries a stable role key", all(r.get("key") for r in _rows))
check("agencies never outrank a real employer",
      [r["agency"] for r in _rows] == sorted((r["agency"] for r in _rows), key=lambda a: (a,))
      or all(r["is_new"] for r in _rows if r["agency"]))

print("== Apply queue: prepare in bulk, submit one at a time ==")
check("Greenhouse's current host is recognised", mc.ats_of("https://job-boards.greenhouse.io/x/jobs/1") == "greenhouse")
check("the older Greenhouse host still works", mc.ats_of("https://boards.greenhouse.io/x/jobs/1") == "greenhouse")
for _u, _want in (("https://jobs.lever.co/a/b","lever"), ("https://jobs.ashbyhq.com/a/b","ashby"),
                  ("https://x.wd5.myworkdayjobs.com/y","workday"), ("https://x.icims.com/j/1","icims")):
    check(f"ATS detected: {_want}", mc.ats_of(_u) == _want)
check("an unknown host reports no ATS rather than guessing", mc.ats_of("https://example.com/job") == "")

_ev = next((a for a in mc.apps() if a["status"].lower() == "evaluated"), None)
if _ev:
    _name, _warn = mc._apply_pack(_ev, "")           # no key: answers fall back, pack still written
    _pack = mc.APPLY_DIR / _name
    _body = mc.read(_pack)
    check("an application pack is written", _pack.exists(), _name)
    check("the pack carries the submit checklist", "Before you click Submit" in _body)
    check("the pack flags a missing résumé rather than pretending", any("résumé" in w for w in _warn), str(_warn))
    check("the role is queued for review", any(q["num"] == _ev["num"] for q in mc.apply_queue()))
    _pack.unlink()
check("an applied role is not re-queued",
      not any(q["num"] == a["num"] for q in mc.apply_queue() for a in mc.apps() if a["status"].lower() == "applied"))
check("prepare_batch refuses an empty selection", mc.do_prepare_batch([])["ok"] is False)
check("prepare_batch caps a runaway batch", mc.do_prepare_batch([{"company":"C","title":"T"}]*51)["ok"] is False)

print("== debrief questions reach the prep that should carry them ==")
# A debrief tags each captured question, and prep used to take only an exact-prefix
# match — so every [Other] question was stranded. A recruiter screen is nearly all
# [Other] ("walk me through your background", "comp expectations"), which made the
# questions most certain to be asked again the ones prep never saw.
_qb = mc.CO / "data" / "question-bank.tsv"
_had = _qb.exists()
_prior = _qb.read_text() if _had else ""
_qb.write_text(_prior +
  "datawright\tLeadership\thow do you scale a platform team\n"
  "datawright\tTechnical\twalk me through your rollback strategy\n"
  "datawright\tBehavioral\ttell me about a failure\n"
  "datawright\tOther\twhat are your compensation expectations\n")
try:
    _lead = mc.real_questions_for("Datawright", "leadership")
    _tech = mc.real_questions_for("Datawright", "technical")
    _beh  = mc.real_questions_for("Datawright", "behavioral")
    check("a leadership question goes to the leadership brief", _lead == ["how do you scale a platform team"], _lead)
    check("a technical question goes to the technical set", _tech == ["walk me through your rollback strategy"], _tech)
    check("an [Other] question is no longer stranded", "what are your compensation expectations" in _beh, _beh)
    check("it lands in exactly one set, not all of them",
          "what are your compensation expectations" not in _lead + _tech, (_lead, _tech))
    check("the behavioral set still carries its own", "tell me about a failure" in _beh, _beh)
    check("another company's bank is not borrowed", mc.real_questions_for("Vertex Cloud", "behavioral") == [],
          mc.real_questions_for("Vertex Cloud", "behavioral"))
finally:
    if _had: _qb.write_text(_prior)
    else: _qb.unlink()

print("== a truncated reasoning model reports a budget problem, not gibberish ==")
# A reasoning model spends max_tokens on thinking BEFORE it writes the answer. Too small a
# budget returns finish_reason=length with content empty and the reasoning stream in its
# place; handing that prose back as the answer produced "Expecting ',' delimiter" three
# functions away, which is how a token-budget bug spent a week looking like a JSON bug.
import io as _io, json as _json
class _FakeResp:
    def __init__(self, payload): self._p = _json.dumps(payload).encode()
    def read(self, *a): return self._p
    def __enter__(self): return self
    def __exit__(self, *a): return False
def _reply(payload):
    _real = mc.urllib.request.urlopen
    mc.urllib.request.urlopen = lambda *a, **k: _FakeResp(payload)
    try: return mc._llm([{"role": "user", "content": "x"}], "k", 1600), None
    except Exception as e: return None, e
    finally: mc.urllib.request.urlopen = _real

_out, _err = _reply({"choices": [{"finish_reason": "length",
                                  "message": {"content": None, "reasoning": "I am thinking about " * 40}}]})
check("a length-truncated reply raises instead of returning the reasoning",
      _out is None and _err is not None, f"out={str(_out)[:60]}")
check("the error names the budget, so the fix is obvious",
      _err is not None and "max_tokens=1600" in str(_err), str(_err)[:120])
check("the error does not masquerade as the answer",
      _err is not None and "I am thinking about" not in str(_err)[:60], str(_err)[:80])

_out, _err = _reply({"choices": [{"finish_reason": "stop",
                                  "message": {"content": None, "reasoning": "the actual answer"}}]})
check("a model that genuinely answers in `reasoning` still works",
      _out == "the actual answer", f"{_out!r} {_err}")

_out, _err = _reply({"choices": [{"finish_reason": "stop", "message": {"content": "  hi  "}}]})
check("an ordinary reply is returned stripped", _out == "hi", f"{_out!r} {_err}")

_out, _err = _reply({"choices": [{"finish_reason": "stop", "message": {"content": None}}]})
check("a null content with nothing behind it is an error, not an AttributeError",
      _out is None and isinstance(_err, RuntimeError), f"{_out!r} {type(_err).__name__}")

print("== Discover: the posting-age window is a filter, not a restart ==")
# A posting that has been up three weeks already has a queue in front of it, so the
# window is a per-look question. It used to be JOBHELM_DISCOVER_DAYS only — an env var
# on a LaunchAgent, changeable only by editing a plist and restarting.
_wide = mc.pipeline_recent(3650)[0]
_ages = sorted(r["age"] for r in _wide if r.get("age") is not None)
check("every match carries its own age in days", len(_ages) == len(_wide) and all(a >= 0 for a in _ages), _ages[:5])
if _ages:
    _cut = _ages[len(_ages)//2] or 1
    _rows, _hidden, _meta = mc.pipeline_recent(_cut)
    check("a narrower window drops everything older than it",
          all(r["age"] <= _cut for r in _rows), [r["age"] for r in _rows])
    check("the window the caller asked for is what the panel reports",
          _meta["days"] == _cut, _meta["days"])
    check("the window is wider or equal when asked for more",
          len(mc.pipeline_recent(3650)[0]) >= len(_rows))
check("an absent window falls back to the configured default",
      mc.pipeline_recent()[1] is not None and mc.pipeline_recent()[2]["days"] == mc.DISCOVER_DAYS,
      mc.pipeline_recent()[2]["days"])
check("the default is still reported alongside the active window",
      mc.pipeline_recent(7)[2]["default_days"] == mc.DISCOVER_DAYS)
# a hostile or fat-fingered value must not become an unbounded scan
check("a nonsense window is clamped, not honoured",
      mc.pipeline_recent(0)[2]["days"] == 1 and mc.pipeline_recent(99999)[2]["days"] == 365,
      (mc.pipeline_recent(0)[2]["days"], mc.pipeline_recent(99999)[2]["days"]))
check("build_state threads the window through to the panel",
      mc.build_state(7)["new_meta"]["days"] == 7, mc.build_state(7)["new_meta"]["days"])
check("the Discover controls are on the page",
      'id="nmctl"' in mc.PAGE and "setNmDays(" in mc.PAGE and "setNmSort(" in mc.PAGE)
check("the age column is rendered", "nmAgeCell" in mc.PAGE and "<th>Age</th>" in mc.PAGE)

print("== stage moves: the board can walk a role forward (and back) ==")
# The board used to be able to say "Applied" and nothing after it, so a company
# that replied stayed parked in Applied forever. These cover the write path the
# stage buttons and the drag-and-drop both call.
_stage_calls = []
_real_run = mc.run
mc.run = lambda cmd, cwd: (_stage_calls.append(cmd), (True, ""))[1]

for _key, _label in [("responded", "Responded"), ("interview", "Interview"),
                     ("offer", "Offer"), ("hired", "Hired"), ("evaluated", "Evaluated")]:
    _stage_calls.clear()
    _r = mc.do_stage("5", _key)
    _cmd = _stage_calls[0] if _stage_calls else []
    check(f"'{_key}' writes the canonical label '{_label}'",
          _r["ok"] and _cmd[:2] == ["node", "set-status.mjs"] and _cmd[3] == _label, f"{_r} {_cmd}")

_stage_calls.clear()
_r = mc.do_stage("5", "MADE-UP")
check("an unknown stage is refused before shelling out", _r["ok"] is False and not _stage_calls, f"{_r} {_stage_calls}")
_stage_calls.clear()
_r = mc.do_stage("5", "")
check("an empty stage is refused before shelling out", _r["ok"] is False and not _stage_calls, f"{_r} {_stage_calls}")

_stage_calls.clear()
mc.run = lambda cmd, cwd: (_stage_calls.append(cmd), (False, "boom"))[1]
_r = mc.do_stage("5", "responded")
check("a failed write is reported, not swallowed", _r["ok"] is False and "boom" in _r["msg"], _r)
mc.run = _real_run

# set-status.mjs validates the label against templates/states.yml and rejects anything
# else, so a typo here would only surface at click time. Check it against the real file
# when one is reachable (dev checkout); the sample data ships without it.
_states = mc.CO / "templates" / "states.yml"
if _states.exists():
    import re as _re2
    _labels = set(_re2.findall(r"^\s*label:\s*(.+?)\s*$", mc.read(_states), _re2.M))
    _bad = [v for v in mc.STAGE_LABELS.values() if v not in _labels]
    check("every STAGE_LABELS value is a canonical state", not _bad, f"unknown: {_bad}")
else:
    print("  skip canonical-state cross-check (no templates/states.yml in sample data)")

check("/api/stage is routed", '/api/stage' in mc.read(HERE / "mission-control.py"))

print("== the page's JavaScript actually parses (escaping guard) ==")
# PAGE is a non-raw Python string, so a JS escape written with one backslash is
# eaten before the browser sees it. That has broken this page three separate ways
# (lone surrogates, quote escapes, \n inside the bookmarklet), and each time
# Python imported happily and served a blank screen. Parse it for real.
import shutil, tempfile, re as _re
subprocess.Popen = _REAL_POPEN          # undo the launcher stub so node can actually run
if shutil.which("node"):
    _js = max(_re.findall(r"<script>(.*?)</script>", mc.PAGE, _re.S), key=len)
    with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False) as _f:
        _f.write(_js); _pathjs = _f.name
    _r = subprocess.run(["node", "--check", _pathjs], capture_output=True, text=True)
    check("the dashboard's inline JS parses", _r.returncode == 0, _r.stderr[:200])
    os.unlink(_pathjs)
else:
    print("  skip node --check (node not installed)")


print("== drag-and-drop: the gesture maps to the right write ==")
# The card is moved optimistically and the server reconciles, so a wrong key here
# would put the card in a column the reload then yanks it out of. Run the real
# source of stageOf()/dropCard() in node against stubs rather than re-describing it.
if shutil.which("node"):
    _page = mc.PAGE
    _stages_src = _re.search(r"var STAGES=\[.*?\];", _page, _re.S).group(0)
    _stageof_src = _re.search(r"function stageOf\(status\)\{.*?return 'applied'\}", _page, _re.S).group(0)
    _drop_src = _re.search(r"function dropCard\(e,stage\)\{.*?\n\}", _page, _re.S).group(0)
    _harness = _stages_src + "\n" + _stageof_src + "\n" + _drop_src + r"""
var CALLS=[], CONFIRMS=[], ANSWER=true, ROW=null;
function act(kind,args){CALLS.push({kind:kind,args:args})}
function byNum(n){return ROW}
function renderBoard(){}
function dragEnd(){}
function confirm(m){CONFIRMS.push(m);return ANSWER}
function drop(fromStatus,toStage,answer){
  CALLS=[];CONFIRMS=[];ANSWER=(answer===undefined?true:answer);
  ROW={num:'7',company:'Acme',status:fromStatus};
  _drag='7';
  dropCard({preventDefault:function(){},dataTransfer:null},toStage);
  return {calls:CALLS,confirms:CONFIRMS,landedOn:ROW.status};
}
var out={};
out.sameColumn      = drop('Applied','applied');
out.appliedToInTouch= drop('Applied','responded');
out.evaluatedToApplied = drop('Evaluated','applied');
out.appliedToInterview = drop('Applied','interview');
out.backwardsAccepted  = drop('Interview','applied',true);
out.backwardsDeclined  = drop('Interview','applied',false);
out.stageOfKeys = STAGES.map(function(s){return s[0]+'->'+stageOf(s[0])});
console.log(JSON.stringify(out));
"""
    with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False) as _f:
        _f.write(_harness); _hp = _f.name
    _out = subprocess.run(["node", _hp], capture_output=True, text=True)
    os.unlink(_hp)
    if _out.returncode != 0:
        check("the drag-and-drop harness runs", False, _out.stderr[:300])
    else:
        import json as _json
        _o = _json.loads(_out.stdout.strip().splitlines()[-1])
        check("a drop on the card's own column writes nothing",
              not _o["sameColumn"]["calls"] and not _o["sameColumn"]["confirms"], _o["sameColumn"])
        check("Applied -> In touch writes state 'responded'",
              _o["appliedToInTouch"]["calls"] == [{"kind":"stage","args":{"num":"7","state":"responded"}}],
              _o["appliedToInTouch"]["calls"])
        check("Applied -> Interview skips a column cleanly",
              _o["appliedToInterview"]["calls"] == [{"kind":"stage","args":{"num":"7","state":"interview"}}],
              _o["appliedToInterview"]["calls"])
        check("To apply -> Applied goes through /api/apply (it archives the résumé you sent)",
              _o["evaluatedToApplied"]["calls"] == [{"kind":"apply","args":{"num":"7"}}],
              _o["evaluatedToApplied"]["calls"])
        check("a backwards drag asks before writing",
              len(_o["backwardsAccepted"]["confirms"]) == 1 and _o["backwardsAccepted"]["calls"],
              _o["backwardsAccepted"])
        check("declining a backwards drag writes nothing",
              not _o["backwardsDeclined"]["calls"], _o["backwardsDeclined"])
        check("the optimistic card carries the stage KEY, not the column label",
              _o["appliedToInTouch"]["landedOn"] == "responded", _o["appliedToInTouch"]["landedOn"])
        check("every stage key resolves to its own column",
              all(k.split("->")[0] == k.split("->")[1] for k in _o["stageOfKeys"]), _o["stageOfKeys"])
    check("cards are draggable", 'draggable="true"' in _page and "dragStart(event" in _page)
    check("columns are drop targets", 'data-stage="' in _page and "ondrop=" in _page and "dragOver(event)" in _page)
else:
    print("  skip drag-and-drop behaviour (node not installed)")

print(f"\n==== {PASS} passed, {FAIL} failed ====")
sys.exit(1 if FAIL else 0)
