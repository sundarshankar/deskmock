#!/usr/bin/env python3
"""mail-fetch.py — pull job-related mail over IMAP, locally, and hand it to inbox-sync.

The mail never leaves this machine. Your laptop connects to the mail server, the
matching runs here (inbox-sync.py is pure pattern matching — no model, no network),
and proposals land on the board. Nothing is uploaded to anyone, which is the whole
reason this exists rather than a cloud mail connector: the classification needs no
model, so routing a mailbox through one would be a privacy cost with nothing bought.

Read-only, always. The mailbox is SELECTed with readonly=True, so nothing is marked
read, moved, flagged or deleted. Running this cannot change what your inbox looks like.

CREDENTIALS — an app password, never your account password:
  Google account -> Security -> 2-Step Verification -> App passwords. A 16-character
  credential scoped to one app, revocable on its own, useless for signing in.
  Put it in ~/src/findingnemo/mail.env, chmod 600:

      IMAP_HOST=imap.gmail.com
      IMAP_USER=you@gmail.com
      IMAP_APP_PASSWORD=abcd efgh ijkl mnop

USAGE
  ./mail-fetch.py --self-test            # parsing + filtering, no network, no creds
  ./mail-fetch.py --days 30 --out mail.json
  ./mail-fetch.py --days 30 | ./inbox-sync.py --emails /dev/stdin --print
"""
import argparse, datetime, email, email.utils, html, imaplib, importlib.util, json, os, pathlib, re, sys
from email.header import decode_header, make_header

HERE = pathlib.Path(__file__).resolve().parent
CREDS = pathlib.Path(os.environ.get("JOBHELM_MAIL_ENV", pathlib.Path.home() / "src/findingnemo/mail.env"))

# The ATS hosts that mail on an employer's behalf, plus the phrases that mark a message
# as being about an application at all. Kept in step with inbox-sync.ATS_HOSTS.
ATS_HOSTS = ("greenhouse.io", "myworkday.com", "myworkdayjobs.com", "lever.co", "ashbyhq.com",
             "smartrecruiters.com", "icims.com", "workable.com", "successfactors.com",
             "taleo.net", "jobvite.com", "bamboohr.com", "oraclecloud.com", "avature.net",
             "eightfold.ai", "phenompeople.com", "jazzhr.com", "breezy.hr", "myworkday.com")
SUBJECT_HINTS = re.compile(
    r"applicat|your candidacy|interview|recruit|talent|hiring|position|role at|opportunity"
    r"|thank you for (?:applying|your interest)|we received|next steps|offer", re.I)


def load_creds():
    if not CREDS.exists():
        sys.exit(f"No credentials at {CREDS}.\n"
                 f"Create it with IMAP_HOST / IMAP_USER / IMAP_APP_PASSWORD (see --help), then chmod 600 it.")
    mode = CREDS.stat().st_mode & 0o777
    if mode & 0o077:
        print(f"  ! {CREDS} is mode {oct(mode)[2:]} — readable by others. chmod 600 it.", file=sys.stderr)
    out = {}
    for line in CREDS.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line: continue
        k, v = line.split("=", 1)
        out[k.strip().upper()] = v.strip()
    missing = [k for k in ("IMAP_USER", "IMAP_APP_PASSWORD") if not out.get(k)]
    if missing: sys.exit(f"{CREDS} is missing {', '.join(missing)}.")
    out.setdefault("IMAP_HOST", "imap.gmail.com")
    return out


def board_domains():
    """Company names on the board, as the domain roots their mail would come from."""
    spec = importlib.util.spec_from_file_location("mc", HERE / "mission-control.py")
    mc = importlib.util.module_from_spec(spec); spec.loader.exec_module(mc)
    out = set()
    for a in mc.apps():
        if a["status"].lower() in ("skip", "discarded"): continue
        s = mc.slug(a["company"])
        if len(s) >= 4: out.add(s)
    return sorted(out)


# Gmail's IMAP parser rejects a long X-GM-RAW string — a single query naming every ATS
# host AND every company on the board came to 971 characters and failed with "Could not
# parse command". The failure was invisible: the code fell back to a plain SINCE, which
# on All Mail returns EVERY message in the window (2,485 of them here), got truncated to
# the most recent few hundred, and buried the job mail in everything else. So: several
# short queries, unioned. Each one stays well inside what the server will parse.
MAX_QUERY = 250


def gmail_queries(days, domains):
    qs = []
    def add(terms):
        q = f'newer_than:{days}d ({" OR ".join(terms)})'
        if len(q) <= MAX_QUERY: qs.append(q)
        elif len(terms) > 1:                      # too long — split and retry both halves
            mid = len(terms) // 2
            add(terms[:mid]); add(terms[mid:])
    for i in range(0, len(ATS_HOSTS), 6):
        add([f"from:{h}" for h in ATS_HOSTS[i:i + 6]])
    for i in range(0, len(domains), 5):
        add([f"from:{d}" for d in domains[i:i + 5]])
    qs.append(f"newer_than:{days}d subject:(application OR interview OR recruiter OR candidacy OR offer)")
    return qs


def decode(v):
    try: return str(make_header(decode_header(v or "")))
    except Exception: return v or ""


def body_of(msg, limit=4000):
    """Prefer text/plain; fall back to de-tagged HTML. Never the raw markup."""
    def part_text(part):
        try:
            payload = part.get_payload(decode=True) or b""
            return payload.decode(part.get_content_charset() or "utf-8", "replace")
        except Exception:
            return ""
    text = ""
    if msg.is_multipart():
        for part in msg.walk():
            if part.get_content_type() == "text/plain" and "attachment" not in str(part.get("Content-Disposition", "")):
                text = part_text(part)
                if text.strip(): break
        if not text.strip():
            for part in msg.walk():
                if part.get_content_type() == "text/html":
                    text = part_text(part); break
    else:
        text = part_text(msg)
    if "<" in text and ">" in text:
        text = re.sub(r"(?is)<(script|style).*?</\1>", " ", text)
        text = re.sub(r"(?s)<[^>]+>", " ", text)
        text = html.unescape(text)
    return " ".join(text.split())[:limit]


def keep(frm, subject):
    """Second gate, applied locally: the server query is broad-ish, this is the sieve."""
    host = (frm or "").lower()
    if any(h in host for h in ATS_HOSTS): return True
    return bool(SUBJECT_HINTS.search(subject or ""))


def _search_all(M, queries, days, verbose):
    """Union of several short searches, with a safe fallback if the server refuses them."""
    ids, ok = [], False
    for q in queries:
        try:
            typ, data = M.search(None, "X-GM-RAW", f'"{q}"')
            if typ != "OK": continue
            ok = True
            ids.extend(data[0].split() if data and data[0] else [])
        except imaplib.IMAP4.error as e:
            if verbose: print(f"  ! query refused ({str(e)[:50]}): {q[:60]}", file=sys.stderr)
    if not ok:
        # Not Gmail, or Gmail said no to everything. SINCE returns the whole window, which
        # is why the header pass below exists: nothing downloads a body it will discard.
        since = (datetime.date.today() - datetime.timedelta(days=days)).strftime("%d-%b-%Y")
        typ, data = M.search(None, "SINCE", since)
        ids = data[0].split() if data and data[0] else []
        if verbose: print(f"  (server-side filtering unavailable — sieving {len(ids)} locally)", file=sys.stderr)
    seen, uniq = set(), []
    for i in ids:
        if i not in seen: seen.add(i); uniq.append(i)
    return uniq


def _headers(M, ids, chunk=200):
    """One batched round trip per chunk for FROM/SUBJECT/DATE — not one per message."""
    out = {}
    for k in range(0, len(ids), chunk):
        batch = b",".join(ids[k:k + chunk])
        typ, data = M.fetch(batch, "(BODY.PEEK[HEADER.FIELDS (FROM SUBJECT DATE)])")
        if typ != "OK": continue
        for item in data:
            if not isinstance(item, tuple) or len(item) < 2: continue
            m = re.match(rb"\s*(\d+)\s+\(", item[0] or b"")
            if not m: continue
            msg = email.message_from_bytes(item[1])
            out[m.group(1)] = (decode(msg.get("From")), decode(msg.get("Subject")), msg.get("Date"))
    return out


def fetch(days, limit=400, verbose=True):
    creds = load_creds()
    domains = board_domains()
    queries = gmail_queries(days, domains)
    if verbose:
        print(f"  connecting to {creds['IMAP_HOST']} as {creds['IMAP_USER']} (read-only)", file=sys.stderr)
    M = imaplib.IMAP4_SSL(creds["IMAP_HOST"])
    try:
        M.login(creds["IMAP_USER"], creds["IMAP_APP_PASSWORD"].replace(" ", ""))
        # All Mail, so nothing hiding in Promotions/Updates or already archived is missed.
        for box in ('"[Gmail]/All Mail"', "INBOX"):
            typ, _ = M.select(box, readonly=True)          # readonly: cannot alter the mailbox
            if typ == "OK": break
        ids = _search_all(M, queries, days, verbose)
        if verbose: print(f"  {len(ids)} candidate message(s) from {len(queries)} queries", file=sys.stderr)

        # Headers first, bodies only for what survives. Downloading every body and THEN
        # deciding meant a fallback search could pull hundreds of full messages — minutes
        # of round trips — to keep a dozen.
        heads = _headers(M, ids)
        keepers = [i for i in ids if i in heads and keep(heads[i][0], heads[i][1])][-limit:]
        if verbose: print(f"  {len(keepers)} look job-related — fetching those bodies", file=sys.stderr)

        out = []
        for i in keepers:
            typ, raw = M.fetch(i, "(BODY.PEEK[])")          # PEEK: does not set the \\Seen flag
            if typ != "OK" or not raw or not raw[0]: continue
            msg = email.message_from_bytes(raw[0][1])
            frm, subject, datehdr = heads[i]
            try:
                date = email.utils.parsedate_to_datetime(datehdr).date().isoformat()
            except Exception:
                date = ""
            out.append({"from": frm, "subject": subject, "date": date, "body": body_of(msg)})
        return out
    finally:
        try: M.logout()
        except Exception: pass


# --------------------------------------------------------------------------- tests
def self_test():
    ok = fail = 0
    def check(n, c, d=""):
        nonlocal ok, fail
        if c: ok += 1; print(f"  ok   {n}")
        else: fail += 1; print(f"  FAIL {n}  {d}")

    print("== the query is scoped, not a mailbox dump ==")
    qs = gmail_queries(30, ["alteryx", "fanduel"])
    joined = " ".join(qs)
    check("every query is time-bounded", all("newer_than:30d" in q for q in qs), qs)
    check("the ATS hosts are covered", "from:greenhouse.io" in joined and "from:lever.co" in joined)
    check("the board's companies are covered", "from:alteryx" in joined and "from:fanduel" in joined)
    # A 971-char query is what Gmail rejected, silently falling back to "every message".
    check("no query exceeds what the server will parse", all(len(q) <= MAX_QUERY for q in qs),
          max((len(q) for q in qs), default=0))
    check("a company list too long to fit is split, not dropped",
          all("from:" + d in " ".join(gmail_queries(30, [f"company{i}withaverylongname" for i in range(40)]))
              for d in ("company0withaverylongname", "company39withaverylongname")))
    check("it never asks for everything", "in:anywhere" not in joined)

    print("\n== the local sieve ==")
    check("an ATS sender is kept", keep("no-reply@greenhouse.io", "hello"))
    check("application language is kept", keep("someone@acme.com", "Update on your application"))
    check("an interview subject is kept", keep("x@y.com", "Interview scheduling"))
    check("a newsletter is dropped", not keep("deals@shop.com", "50% off this week"))
    check("a bank alert is dropped", not keep("alerts@bank.com", "Your statement is ready"))

    print("\n== body extraction ==")
    raw = ("From: A <a@b.com>\r\nSubject: Test\r\nContent-Type: text/html\r\n\r\n"
           "<html><style>p{color:red}</style><body><p>Thank you for &amp; applying.</p>"
           "<script>evil()</script></body></html>")
    b = body_of(email.message_from_string(raw))
    check("html tags are stripped", "<" not in b and ">" not in b, b)
    check("entities are decoded", "&" in b and "&amp;" not in b, b)
    check("script and style contents are dropped", "evil" not in b and "color:red" not in b, b)
    multi = ("From: A <a@b.com>\r\nSubject: T\r\nMIME-Version: 1.0\r\n"
             'Content-Type: multipart/alternative; boundary="X"\r\n\r\n'
             "--X\r\nContent-Type: text/plain\r\n\r\nplain wins\r\n"
             "--X\r\nContent-Type: text/html\r\n\r\n<p>html loses</p>\r\n--X--\r\n")
    check("text/plain is preferred over html", body_of(email.message_from_string(multi)) == "plain wins",
          body_of(email.message_from_string(multi)))

    print("\n== headers ==")
    check("an encoded subject is decoded",
          decode("=?utf-8?q?Caf=C3=A9_role?=") == "Café role", decode("=?utf-8?q?Caf=C3=A9_role?="))
    check("a plain subject survives", decode("Plain subject") == "Plain subject")

    print("\n== it cannot modify the mailbox ==")
    src = pathlib.Path(__file__).read_text()
    check("the mailbox is selected read-only", "readonly=True" in src)
    check("messages are fetched with PEEK, so nothing is marked read", "BODY.PEEK" in src)
    check("no store/copy/delete anywhere", not re.search(r"\.(store|copy|expunge)\(", src))

    print(f"\n==== {ok} passed, {fail} failed ====")
    return 1 if fail else 0


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--days", type=int, default=30, help="how far back to look")
    ap.add_argument("--limit", type=int, default=400, help="cap on messages inspected")
    ap.add_argument("--out", type=pathlib.Path, help="write JSON here instead of stdout")
    ap.add_argument("--self-test", action="store_true")
    a = ap.parse_args()
    if a.self_test:
        return self_test()
    msgs = fetch(a.days, a.limit)
    blob = json.dumps(msgs, indent=1)
    if a.out:
        a.out.write_text(blob); os.chmod(a.out, 0o600)      # it is your mail; do not leave it world-readable
        print(f"{len(msgs)} job-related message(s) -> {a.out}", file=sys.stderr)
    else:
        print(blob)
    return 0


if __name__ == "__main__":
    sys.exit(main())
