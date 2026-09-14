#!/usr/bin/env python3
"""inbox-sync.py — turn employer email into proposed tracker updates.

Twenty applications on the board read as "silent". Most silence is not an employer
ignoring you; it is a reply that never reached the board. The rejection is in the
inbox, the board still says Applied, and you go on chasing a company that already
said no.

Deliberately source-agnostic: it reads a JSON array of messages and writes proposals.
Anything that can produce {from, subject, date, body} feeds it — a Gmail connector, an
IMAP fetch, an mbox export, a paste. The matching and the judgement live here, in one
testable place, rather than inside whichever mail client happens to be wired up.

    ./inbox-sync.py --self-test                     # fixtures, no mailbox needed
    ./inbox-sync.py --emails mail.json              # write proposals
    ./inbox-sync.py --emails mail.json --print      # ...and show them

It PROPOSES. It never writes to the tracker: career-ops' own doctrine is that nothing
auto-updates without confirmation, and a misread rejection would delete a live thread
from your pipeline. Approving happens in JobHelm, one row at a time, through the same
status path a button uses.
"""
import argparse, datetime, importlib.util, json, pathlib, re, sys

HERE = pathlib.Path(__file__).resolve().parent
_spec = importlib.util.spec_from_file_location("mc", HERE / "mission-control.py")
mc = importlib.util.module_from_spec(_spec); _spec.loader.exec_module(mc)

PROPOSALS = mc.CO / "data" / "inbox-proposals.json"

# Mail from these hosts tells you nothing about the employer — the ATS sends on their
# behalf — so the company has to come out of the subject or body instead.
ATS_HOSTS = ("greenhouse.io", "myworkday.com", "myworkdayjobs.com", "lever.co",
             "hire.lever.co", "ashbyhq.com", "smartrecruiters.com", "icims.com",
             "workable.com", "successfactors.com", "taleo.net", "jobvite.com",
             "bamboohr.com", "paylocity.com", "oraclecloud.com", "avature.net",
             "eightfold.ai", "phenompeople.com", "jazzhr.com", "breezy.hr")

# Ordered: the first pattern that matches wins, so a rejection that opens with
# "thank you for applying" is still read as a rejection.
CLASSIFIERS = [
    ("offer", re.compile(
        r"pleased to offer|we(?:'| a)re excited to offer|offer letter|extend(?:ing)? (?:you )?an offer"
        r"|formal offer", re.I)),
    ("rejected", re.compile(
        r"not (?:be )?(?:moving|proceeding|progressing)|will not be moving forward"
        r"|move forward with other|other candidates|decided to (?:move forward|proceed) with"
        r"|unfortunately[^.]{0,80}(?:not|other)|no longer (?:under )?consider"
        r"|position has been filled|we(?:'| ha)ve decided to pursue", re.I)),
    ("interview", re.compile(
        r"schedule (?:a|an|some)? ?(?:call|time|chat|interview|conversation)"
        r"|invite you to (?:an? )?(?:interview|conversation)|next (?:round|step)s? (?:is|will|would)"
        r"|panel interview|onsite interview|set up (?:a|some) time"
        r"|(?:are|would) you available|calendly\.com|book a time", re.I)),
    ("responded", re.compile(
        r"i (?:came across|reviewed|had a look at)|your (?:background|profile|experience) (?:stood out|caught)"
        r"|would love to (?:chat|connect|learn)|reaching out (?:about|regarding)"
        r"|wanted to (?:connect|reach out)|a few questions", re.I)),
    # An automated receipt is NOT a human being in touch. Recording it as "In touch"
    # would empty the column of meaning and stop you chasing a thread nobody has read.
    ("ack", re.compile(
        r"(?:we(?:'| ha)ve )?received your application|thank you for applying"
        r"|application (?:has been )?(?:received|submitted)|we are reviewing"
        r"|your application (?:for|to)", re.I)),
]

STAGE_FOR = {"offer": "offer", "interview": "interview", "rejected": "rejected",
             "responded": "responded", "ack": None}     # ack proposes no stage change

NOREPLY = re.compile(r"no[-_.]?reply|do[-_.]?not[-_.]?reply|notification|donotreply|automated|mailer", re.I)


def addr_of(frm):
    m = re.search(r"<([^>]+)>", frm or "")
    return (m.group(1) if m else (frm or "")).strip().lower()


def name_of(frm):
    n = re.sub(r"<[^>]*>", "", frm or "").strip().strip('"').strip()
    return "" if "@" in n else n


def host_of(frm):
    a = addr_of(frm)
    return a.split("@")[-1] if "@" in a else ""


def root_of(host):
    """foo.mail.acme.co.uk -> acme. Good enough to compare against a company name."""
    parts = [p for p in (host or "").split(".") if p]
    if len(parts) < 2: return host or ""
    tail = {"com", "co", "io", "ai", "net", "org", "us", "uk", "inc"}
    while len(parts) > 1 and parts[-1] in tail:
        parts.pop()
    return parts[-1] if parts else ""


def ids_in(text):
    return set(m.group(0).lower() for m in re.finditer(
        r"\b(?:gh_jid[ =]?\d{4,}|r-?\d{5,}|req-?\d{4,}|job id[ :]?\d{4,}|\d{7,})\b", text or "", re.I))


def match_row(email, apps):
    """Which application is this about? Returns (row, confidence 0-1, why)."""
    text = f"{email.get('subject','')} {email.get('body','')}"
    low = text.lower()
    frm = email.get("from", "")
    host, root = host_of(frm), root_of(host_of(frm))
    is_ats = any(h in host for h in ATS_HOSTS)
    mail_ids = ids_in(text)

    scored = []
    for a in apps:
        conf, why = 0.0, ""
        cslug = mc.slug(a["company"])
        # 1. a requisition id in BOTH the mail and the row's notes is unambiguous
        if mail_ids and (mail_ids & ids_in(a.get("notes", ""))):
            conf, why = 0.95, "req id matches the tracker note"
        # 2. the sender is the employer
        elif root and not is_ats and len(root) >= 3 and (root in cslug or cslug.startswith(root[:5])):
            conf, why = 0.90, f"sender domain {host}"
        # 3. an ATS sent it and the employer is named in the text
        elif is_ats and cslug and mc.slug(a["company"]) in mc.slug(low):
            conf, why = 0.80, f"{host} naming {a['company']}"
        # 4. the employer is named and nothing better matched
        elif cslug and len(cslug) >= 4 and cslug in mc.slug(low):
            conf, why = 0.65, "company named in the message"
        if conf:
            # two roles at one employer: let the title break the tie
            toks = [t for t in re.split(r"[^a-z]+", (a["role"] or "").lower()) if len(t) > 3]
            if toks and sum(1 for t in toks if t in low) >= 2:
                conf, why = min(0.99, conf + 0.06), why + " + role title"
            scored.append((conf, a, why))
    if not scored: return None, 0.0, "no tracked application matched"
    scored.sort(key=lambda x: (-x[0], x[1]["num"]))
    best = scored[0]
    # A tie between two rows is exactly the case to ask about rather than guess.
    if len(scored) > 1 and abs(scored[1][0] - best[0]) < 0.02:
        return best[1], round(best[0] * 0.7, 2), f"{best[2]} (ambiguous with {scored[1][1]['company']})"
    return best[1], round(best[0], 2), best[2]


def classify(email):
    """What happened? Returns (kind, the sentence that decided it)."""
    text = f"{email.get('subject','')}\n{email.get('body','')}"
    for kind, pat in CLASSIFIERS:
        m = pat.search(text)
        if m:
            s = max(0, m.start() - 70); e = min(len(text), m.end() + 70)
            return kind, " ".join(text[s:e].split())
    return "other", ""


def contact_from(email):
    """A recruiter who writes to you is a contact you did not have."""
    frm = email.get("from", "")
    addr, nm = addr_of(frm), name_of(frm)
    if not addr or NOREPLY.search(frm) or any(h in host_of(frm) for h in ATS_HOSTS):
        return None
    return {"name": nm or addr.split("@")[0], "email": addr}


def propose(emails, apps=None):
    apps = apps if apps is not None else mc.apps()
    live = [a for a in apps if a["status"].lower() not in ("skip", "discarded")]
    out = []
    for i, e in enumerate(emails):
        kind, quote = classify(e)
        if kind == "other":
            continue
        row, conf, why = match_row(e, live)
        if not row:
            continue
        stage = STAGE_FOR.get(kind)
        cur = (row["status"] or "").lower()
        if stage and stage == cur:
            continue                      # already where the mail says it should be
        out.append({
            "id": f"{row['num']}-{i}-{kind}",
            "num": row["num"], "company": row["company"], "role": row["role"],
            "from": e.get("from", ""), "subject": e.get("subject", ""), "date": e.get("date", ""),
            "kind": kind, "stage": stage, "current": row["status"],
            "confidence": conf, "why": why, "quote": quote[:240],
            "contact": contact_from(e),
        })
    # highest confidence first, and a rejection before an ack for the same role
    order = {"offer": 0, "interview": 1, "rejected": 2, "responded": 3, "ack": 4}
    out.sort(key=lambda p: (order.get(p["kind"], 9), -p["confidence"]))
    return out


def write(proposals):
    PROPOSALS.parent.mkdir(parents=True, exist_ok=True)
    PROPOSALS.write_text(json.dumps(
        {"generated": datetime.datetime.now().isoformat(timespec="seconds"), "proposals": proposals},
        indent=1))
    return PROPOSALS


# --------------------------------------------------------------------------- tests
FIXTURES = [
  dict(**{"from": "no-reply@greenhouse.io", "subject": "Your application to FanDuel",
          "date": "2026-08-20", "body": "Thank you for applying to FanDuel for Infrastructure Engineering Director (gh_jid 8048893). We have received your application."}),
  dict(**{"from": "Talent Team <careers@pfizer.com>", "subject": "Update on your application",
          "date": "2026-08-29", "body": "After careful review we have decided to move forward with other candidates for the Sr. Director, Cloud and Infrastructure Transformation role."}),
  dict(**{"from": "Marlin Plank <mplank@alteryx.com>", "subject": "Director, Developer Platform Engineering — next steps",
          "date": "2026-09-11", "body": "Great speaking today. Are you available Thursday to set up a time with the hiring manager?"}),
  dict(**{"from": "noreply@myworkdayjobs.com", "subject": "Alight Solutions — application received",
          "date": "2026-08-18", "body": "Thank you for applying to Alight for R-37852."}),
  dict(**{"from": "deals@some-newsletter.com", "subject": "50% off this week only",
          "date": "2026-09-01", "body": "Shop the sale."}),
  dict(**{"from": "recruiting@northerntrust.com", "subject": "Global Head of Platform Engineering",
          "date": "2026-09-02", "body": "We are pleased to offer you the position."}),
]


# The matcher is tested against a fixed board, not whatever happens to be in the real
# tracker: a test that passes only on one person's data is not a test of the matcher.
FIXTURE_APPS = [
  {"num": "18", "company": "FanDuel", "role": "Infrastructure Engineering Director",
   "status": "Applied", "notes": "Applied via Greenhouse gh_jid 8048893."},
  {"num": "19", "company": "pfizer", "role": "Sr. Director, Cloud and Infrastructure Transformation",
   "status": "Applied", "notes": "Applied 2026-08-26."},
  {"num": "13", "company": "Alteryx", "role": "Director, Developer Platform Engineering",
   "status": "Responded", "notes": "Applied via Workday."},
  {"num": "15", "company": "Alight", "role": "SVP, Technology, Platforms & Solutions",
   "status": "Applied", "notes": "Applied via Alight Workday (R-37852)."},
  {"num": "10", "company": "Northern Trust", "role": "Global Head of Platform Engineering",
   "status": "Applied", "notes": "Applied direct."},
]


def self_test():
    ok = fail = 0
    def check(n, c, d=""):
        nonlocal ok, fail
        if c: ok += 1; print(f"  ok   {n}")
        else: fail += 1; print(f"  FAIL {n}  {d}")

    print("== classify ==")
    for e, want in zip(FIXTURES, ["ack", "rejected", "interview", "ack", "other", "offer"]):
        got, _ = classify(e)
        check(f"{e['subject'][:44]:<44} -> {want}", got == want, f"got {got}")
    check("a rejection that opens politely is still a rejection",
          classify({"subject": "Thank you for applying", "body": "Thank you for applying. Unfortunately we are moving forward with other candidates."})[0] == "rejected")
    check("an automated receipt is not a human being in touch", STAGE_FOR["ack"] is None)

    print("\n== match ==")
    apps = FIXTURE_APPS
    for e, want in [(FIXTURES[0], "FanDuel"), (FIXTURES[1], "pfizer"), (FIXTURES[2], "Alteryx"),
                    (FIXTURES[3], "Alight"), (FIXTURES[5], "Northern Trust")]:
        row, conf, why = match_row(e, apps)
        check(f"{e['subject'][:40]:<40} -> {want} ({conf})",
              row is not None and mc.slug(row["company"]) == mc.slug(want), f"got {row and row['company']} ({why})")
    row, conf, _ = match_row(FIXTURES[4], apps)
    check("an unrelated newsletter matches nothing", row is None, row and row["company"])

    print("\n== propose ==")
    ps = propose(FIXTURES, FIXTURE_APPS)
    kinds = {p["kind"] for p in ps}
    check("the newsletter produces no proposal", all(p["kind"] != "other" for p in ps))
    check("a rejection is proposed", "rejected" in kinds, kinds)
    check("an interview is proposed", "interview" in kinds, kinds)
    check("every proposal carries the sentence it judged on", all(p["quote"] for p in ps if p["kind"] != "ack"))
    check("every proposal carries a confidence", all(0 < p["confidence"] <= 1 for p in ps))
    check("an ack proposes no stage change", all(p["stage"] is None for p in ps if p["kind"] == "ack"))
    check("offers and interviews sort above acks",
          [p["kind"] for p in ps].index("offer") < [p["kind"] for p in ps].index("ack") if "ack" in kinds else True)

    print("\n== contacts come OUT of the sync ==")
    c = contact_from(FIXTURES[2])
    check("a named human becomes a contact", c and c["email"] == "mplank@alteryx.com" and c["name"] == "Marlin Plank", c)
    check("a no-reply address does not", contact_from(FIXTURES[0]) is None)
    check("an ATS sender does not", contact_from(FIXTURES[3]) is None)

    print(f"\n==== {ok} passed, {fail} failed ====")
    return 1 if fail else 0


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--emails", type=pathlib.Path, help="JSON array of {from, subject, date, body}")
    ap.add_argument("--print", action="store_true", dest="show")
    ap.add_argument("--self-test", action="store_true")
    a = ap.parse_args()
    if a.self_test:
        return self_test()
    if not a.emails:
        ap.error("pass --emails <file.json> or --self-test")
    emails = json.loads(a.emails.read_text())
    ps = propose(emails)
    write(ps)
    print(f"{len(ps)} proposal(s) from {len(emails)} message(s) -> {PROPOSALS}")
    if a.show:
        for p in ps:
            st = p["stage"] or "no change"
            print(f"  [{p['confidence']:.2f}] #{p['num']} {p['company']}: {p['current']} -> {st}   ({p['why']})")
            if p["quote"]: print(f"         \"{p['quote'][:110]}\"")
    return 0


if __name__ == "__main__":
    sys.exit(main())
