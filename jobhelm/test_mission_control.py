#!/usr/bin/env python3
"""JobHelm — unit/logic tests against the bundled sample data. No side effects
(Terminal launch is stubbed; no LLM/network calls)."""
import os, sys, importlib.util, pathlib

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

print(f"\n==== {PASS} passed, {FAIL} failed ====")
sys.exit(1 if FAIL else 0)
