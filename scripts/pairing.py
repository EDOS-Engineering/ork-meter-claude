"""Connect, and finish a connection from a browser on another computer.

``/ork-meter:connect`` runs ``connect``: the runtime's own ``connect``
(scripts/runtime.sh), whose JSON it passes through. When the runtime could
open no browser on this machine (SSH, devcontainer, cloud) it answers
``setup_pending`` with the loopback setup URL it is waiting at (ork-meter
#571). This script then keeps that URL, owner-only, in the plugin data
directory (``remote-pairing.json``) and adds ``"terminal_approval":
"available"`` to the answer, so the person can finish in the terminal
instead of forwarding the port:

``prepare``
    Without ``--plan``: reads the setup page and prints the plans it offers
    and its permission statement, word for word, for the person to choose
    and confirm. With ``--plan`` and ``--permitted yes`` (the person's own
    answers): submits the setup page's form, as the browser would, and
    prints the approval link and fingerprint it returns. The approval link
    opens the Ork Meter /pair page on any computer. There the person signs
    in, compares the fingerprint, ticks "This browser is not on the computer
    running Claude Code" and approves; the page shows an approval code.

``claim --code XXXX-XXXX``
    (``/ork-meter:claim``) hands that code to the runtime through the same
    loopback claim form its browser return uses, and prints the runtime's
    answer: ``connected``, or why not.

Everything goes through the runtime's own loopback pages, the surface it
exposes for this today: the runtime has no ``claim`` command. The runtime
and its pages ship pinned inside this plugin (runtime/), so the markup read
here is the markup of the runtime this plugin runs; tests/pairing_regressions.py
mirrors it. The record holds the loopback setup and claim URLs and the
public proposal ID, never the code, a credential or an approval; it is
removed by a successful claim and by the next ``connect``. Requests go to
127.0.0.1 only, never through a proxy. Output is one JSON object; the code is
never printed or written.
"""
import argparse
from html.parser import HTMLParser
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

ROOT = Path(__file__).resolve().parents[1]
RECORD = "remote-pairing.json"
SETUP_URL = re.compile(r"http://127\.0\.0\.1:(\d{1,5})/setup/([a-f0-9]{64})")
CLAIM_PATH = re.compile(r"/plugin/claim/([a-f0-9]{64})")
# collectorpairing.CanonicalClaimCode: Crockford base32, one optional hyphen,
# O read as 0 and I or L as 1. Anything else is refused here, before a request.
CODE_ALPHABET = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"
PLANS = ("pro-max", "team-enterprise", "api")
TIMEOUT = 30
# The runtime's setup window (pluginruntime.setupWindow): a record older than
# this is from an expired setup.
SETUP_WINDOW = 30 * 60


class Failure(Exception):
    def __init__(self, state, message):
        super().__init__(message)
        self.state, self.message = state, message


def emit(result):
    sys.stdout.write(json.dumps(result, sort_keys=True) + "\n")


def data_dir(value):
    path = value or os.environ.get("CLAUDE_PLUGIN_DATA")
    if not path:
        raise Failure("unavailable", "The plugin data directory is unavailable; run this from Claude Code.")
    return Path(path)


def canonical_code(code):
    """The claim code as the platform reads it, or None (collectorpairing.CanonicalClaimCode)."""
    c = code.strip().upper()
    if len(c) == 9 and c[4] == "-":
        c = c[:4] + c[5:]
    if len(c) != 8:
        return None
    c = c.replace("O", "0").replace("I", "1").replace("L", "1")
    if any(ch not in CODE_ALPHABET for ch in c):
        return None
    return c[:4] + "-" + c[4:]


# --- the record -------------------------------------------------------------

def record_path(data):
    return data / RECORD


def write_record(data, record):
    data.mkdir(parents=True, exist_ok=True)
    path = record_path(data)
    temporary = path.with_name(path.name + ".%d.tmp" % os.getpid())
    fd = os.open(str(temporary), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(record, stream, sort_keys=True)
        os.replace(str(temporary), str(path))
    except BaseException:
        try:
            os.unlink(str(temporary))
        except OSError:
            pass
        raise


def read_record(data):
    try:
        record = json.loads(record_path(data).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        record = None
    if not isinstance(record, dict) or record.get("v") != 1 or not SETUP_URL.fullmatch(str(record.get("setup_url", ""))):
        raise Failure("no_terminal_setup", "No setup is waiting for a terminal approval on this computer. Run "
                                           "/ork-meter:connect first; this path is offered when no browser could be "
                                           "opened here.")
    if not isinstance(record.get("created_at"), (int, float)) or time.time() - record["created_at"] > SETUP_WINDOW:
        forget(data)
        raise Failure("setup_expired", "This setup was started more than 30 minutes ago and has expired. Run "
                                       "/ork-meter:connect again.")
    return record


def forget(data):
    try:
        record_path(data).unlink()
    except OSError:
        pass


# --- the runtime's loopback pages -------------------------------------------

class Page(HTMLParser):
    """The parts of a runtime setup page (pluginruntime.page) this script reads."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.title, self.paragraphs, self.codes, self.links, self.options, self.permission = "", [], [], [], [], ""
        self._in, self._text, self._label_checkbox = None, [], False

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag in ("h1", "p", "code", "option", "label"):
            if tag == "label":
                self._label_checkbox = False
            self._in, self._text = tag, []
            if tag == "option" and attrs.get("value"):
                self.options.append(attrs["value"])
        elif tag == "a" and attrs.get("href"):
            self.links.append(attrs["href"])
        elif tag == "input" and attrs.get("type") == "checkbox" and attrs.get("name") == "permitted":
            self._label_checkbox = True

    def handle_endtag(self, tag):
        if tag != self._in and not (tag == "label" and self._in == "label"):
            return
        text = " ".join("".join(self._text).split())
        if tag == "h1":
            self.title = text
        elif tag == "p":
            self.paragraphs.append(text)
        elif tag == "code":
            self.codes.append(text)
        elif tag == "label" and self._label_checkbox:
            self.permission = text
        self._in = None

    def handle_data(self, data):
        if self._in is not None:
            self._text.append(data)


def parse(body):
    page = Page()
    page.feed(body)
    page.close()
    return page


def opener():
    # Loopback only: never through HTTPS_PROXY or HTTP_PROXY.
    return urllib.request.build_opener(urllib.request.ProxyHandler({}))


def request(url, form=None):
    """GET ``url``, or POST ``form`` to it as the runtime's own page would
    (its Origin, a urlencoded body). Returns the parsed page."""
    parts = urllib.parse.urlsplit(url)
    if parts.scheme != "http" or parts.hostname != "127.0.0.1":
        raise Failure("setup_unavailable", "Refused a setup address that is not this computer's loopback. Run "
                                           "/ork-meter:connect again.")
    origin = "%s://%s" % (parts.scheme, parts.netloc)
    if form is None:
        req = urllib.request.Request(url, method="GET")
    else:
        req = urllib.request.Request(url, data=urllib.parse.urlencode(form).encode("ascii"), method="POST",
                                     headers={"Origin": origin, "Content-Type": "application/x-www-form-urlencoded"})
    try:
        with opener().open(req, timeout=TIMEOUT) as response:
            return parse(response.read(65536).decode("utf-8", "replace"))
    except urllib.error.HTTPError as error:
        raise Failure("setup_unavailable", "The setup on this computer refused the request (HTTP %d). It may have "
                                           "been replaced or finished; run /ork-meter:status, or /ork-meter:connect "
                                           "to start again." % error.code)
    except (OSError, ValueError):
        raise Failure("setup_unavailable", "The setup on this computer is not answering. Run /ork-meter:connect "
                                           "again to restart it.")


def finished(page):
    """A terminal answer for a page that is no longer asking anything, or None."""
    message = " ".join(page.paragraphs[:1])
    if page.title == "Connected":
        return {"state": "connected", "message": message}
    if page.title == "Setup expired":
        return {"state": "setup_expired", "message": message}
    if page.title in ("Setup needs attention", "Connection not completed"):
        return {"state": "not_completed", "message": message}
    return None


# --- commands ---------------------------------------------------------------

def connect(args):
    data = data_dir(args.plugin_data)
    command = ["bash", str(ROOT / "scripts/runtime.sh"), "--plugin-data", str(data), "connect",
               "--environment", args.environment]
    completed = subprocess.run(command, stdin=subprocess.DEVNULL, capture_output=True, text=True)
    sys.stderr.write(completed.stderr)
    forget(data)   # any earlier terminal setup is replaced by this one
    try:
        answer = json.loads(completed.stdout)
    except ValueError:
        answer = None
    if completed.returncode != 0 or not isinstance(answer, dict):
        sys.stdout.write(completed.stdout)
        return completed.returncode
    found = SETUP_URL.search(str(answer.get("message", ""))) if answer.get("state") == "setup_pending" else None
    if found:
        write_record(data, {"v": 1, "setup_url": found.group(0), "created_at": int(time.time())})
        answer["terminal_approval"] = "available"
    emit(answer)
    return 0


def prepare(args):
    data = data_dir(args.plugin_data)
    record = read_record(data)
    if args.plan is None:
        page = request(record["setup_url"])
        done = finished(page)
        if done:
            if done["state"] != "not_completed":
                forget(data)
            emit(done)
            return 0
        plans = [plan for plan in page.options if plan in PLANS]
        if not plans or not page.permission:
            raise Failure("setup_unavailable", "The setup page did not offer a plan and permission to confirm. Run "
                                               "/ork-meter:connect again.")
        destination = next((p for p in page.paragraphs if p.startswith("Destination:")), "")
        notes = [p for p in page.paragraphs[1:] if p != destination]
        emit({"state": "consent_required", "plans": plans, "permission": page.permission,
              "destination": destination, "notes": notes})
        return 0
    if args.permitted != "yes":
        raise Failure("consent_required", "The person must confirm the permission statement (--permitted yes) "
                                          "themselves before setup continues.")
    setup = urllib.parse.urlsplit(record["setup_url"])
    nonce = SETUP_URL.fullmatch(record["setup_url"]).group(2)
    page = request(urllib.parse.urlunsplit((setup.scheme, setup.netloc, "/prepare/" + nonce, "", "")),
                   {"plan": args.plan, "permitted": "yes"})
    done = finished(page)
    if done:
        if done["state"] == "connected":
            forget(data)
        emit(done)
        return 0
    approval = next((link for link in page.links if "/pair#" in link), None)
    if approval is None or not page.codes:
        raise Failure("setup_unavailable", "The setup page did not return an approval link. Run /ork-meter:connect "
                                           "again.")
    parts = urllib.parse.urlsplit(approval)
    fragment = urllib.parse.parse_qs(parts.fragment)
    try:
        proposal = json.loads(fragment["proposal"][0])
        reference = proposal["id"]
        claim_url = fragment["return_url"][0]
    except (KeyError, IndexError, ValueError, TypeError):
        raise Failure("setup_unavailable", "The approval link had no plugin return to claim through. Run "
                                           "/ork-meter:connect again.")
    claim = urllib.parse.urlsplit(claim_url)
    if (parts.scheme != "https" or claim.scheme != "http" or claim.netloc != setup.netloc
            or CLAIM_PATH.fullmatch(claim.path) is None or CLAIM_PATH.fullmatch(claim.path).group(1) != nonce
            or not isinstance(reference, str) or not reference):
        raise Failure("setup_unavailable", "The approval link did not match this setup. Run /ork-meter:connect again.")
    record.update(claim_url=claim_url, reference=reference)
    write_record(data, record)
    emit({"state": "approval_pending", "approval_url": approval, "fingerprint": page.codes[0],
          "next_step": "Open the approval link in a browser on any computer and sign in to Ork Meter. Compare the "
                       "security code with this fingerprint, tick \"This browser is not on the computer running "
                       "Claude Code\", and approve. Then run /ork-meter:claim with the approval code the page shows. "
                       "This setup waits 30 minutes from /ork-meter:connect."})
    return 0


def claim(args):
    code = canonical_code(args.code)
    if code is None:
        raise Failure("invalid_code", "That is not an approval code: it is eight letters and digits, shown as "
                                      "XXXX-XXXX on the Ork Meter page after you approve. No request was sent.")
    data = data_dir(args.plugin_data)
    record = read_record(data)
    if not record.get("claim_url") or not record.get("reference"):
        raise Failure("approval_not_requested", "No approval link has been made for this setup yet. Run "
                                                "/ork-meter:connect and follow its terminal approval first.")
    page = request(record["claim_url"], {"reference": record["reference"], "claim_code": code})
    done = finished(page)
    if done is None:
        raise Failure("not_completed", "The setup did not confirm the connection. Run /ork-meter:status.")
    if done["state"] == "connected":
        forget(data)
    emit(done)
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument("--plugin-data")
    commands = parser.add_subparsers(dest="command", required=True)
    one = commands.add_parser("connect")
    one.add_argument("--environment", choices=("production", "dev"), default="production")
    two = commands.add_parser("prepare")
    two.add_argument("--plan", choices=PLANS)
    two.add_argument("--permitted", choices=("yes",))
    three = commands.add_parser("claim")
    three.add_argument("--code", required=True)
    args = parser.parse_args(argv)
    try:
        return {"connect": connect, "prepare": prepare, "claim": claim}[args.command](args)
    except Failure as failure:
        emit({"state": failure.state, "message": failure.message})
        return 1


if __name__ == "__main__":
    sys.exit(main())
