#!/usr/bin/env python3
"""JobHelm — unit/logic tests against the bundled sample data. No side effects
(Terminal launch is stubbed; no LLM/network calls)."""
import os, sys, importlib.util, pathlib, datetime

HERE = pathlib.Path(__file__).resolve().parent
# Point the app at the bundled sample data BEFORE import (module reads env at import time).
os.environ["JOBHELM_CAREEROPS"] = str(HERE / "sample-data")
os.environ["JOBHELM_MOCK"]      = str(HERE / "sample-data" / "mock")
os.environ.setdefault("JOBHELM_NAME",   "Alex Rivera")
os.environ.setdefault("JOBHELM_MOBILE", "555-0100")
os.environ.setdefault("JOBHELM_EMAIL",  "alex.rivera@example.com")

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

print(f"\n==== {PASS} passed, {FAIL} failed ====")
sys.exit(1 if FAIL else 0)
