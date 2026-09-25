#!/usr/bin/env python3
"""Status dashboard for an AltServer-Linux deployment.

    python3 web/server.py [--port 8099] [--host 127.0.0.1]

Stdlib only. Serves a single auto-refreshing page plus /api/status returning the same data as
JSON, so it doubles as the watchdog endpoint something else can poll.

WHY THIS EXISTS. AltServer cannot report its own health, and neither can the phone:

  * avahi can report a successful registration while publishing nothing, so the only trustworthy
    advertisement test is an external browse.
  * AltStore suppresses the one error it would otherwise raise during an unattended refresh
    (BackgroundRefreshAppsOperation sets ignoresServerNotFoundError = true).
  * Almost everything AltServer logs goes to stdout at info level, so `journalctl -p err` stays
    empty no matter what breaks.

Net effect without something like this: a deployment stops refreshing and the first symptom is an
app that will not open, seven days later, with no signal anywhere in between.

SCOPE. Status and pairing are read-only. /install is not: it signs in and takes the 2FA code in
the browser, via installer.py, which supervises an AltServer child and owns its stdin. That is
how a 2FA code reaches a `std::cin` read in a container with no terminal.

Bind to 127.0.0.1 unless you understand the consequences: this reports device identifiers and
should not be exposed to the LAN, and never to the internet.
"""

import argparse
import ipaddress
import json
import os
import re
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import status_checks  # noqa: E402
import pairing  # noqa: E402
import installer  # noqa: E402

# The install form accepts an Apple ID password, and the status/pairing pages report device
# identifiers. Two browser-borne attacks matter even on a home LAN:
#   * DNS rebinding -- a page on evil.example rebinds its name to this host's LAN IP, then the
#     browser sends requests here with Host: evil.example. Refusing unknown Host values breaks it.
#   * CSRF -- a page cross-origin POSTs to /api/install/start. text/plain skips the CORS preflight,
#     so the do_POST guards below require application/json (which forces a preflight this server
#     never answers) and reject a mismatched Origin.
# The allowlist is built so a normal home user never trips it: IP literals, localhost, single-label
# hostnames (raspberrypi, rasai), and names under the suffixes home networks actually use -- .local
# (mDNS), .home.arpa (RFC 8375), .internal (ICANN-reserved), and the undelegated .lan / .home /
# .localdomain that consumer routers hand out. An attacker cannot register any of those in public
# DNS, which is what a rebinding page needs. Extra names go in ALTSERVER_WEB_ALLOWED_HOSTS.
_ALLOWED_HOSTS_ENV = {
    h.strip().lower().rstrip(".")
    for h in re.split(r"[,\s]+", os.environ.get("ALTSERVER_WEB_ALLOWED_HOSTS", ""))
    if h.strip()
}
_PRIVATE_SUFFIXES = (".local", ".home.arpa", ".internal", ".lan", ".home", ".localdomain")
MAX_BODY_BYTES = 64 * 1024


def _hostname_only(host):
    """Return just the hostname from a Host header value, dropping any :port and [] brackets."""
    host = (host or "").strip()
    if host.startswith("["):                 # [::1] or [::1]:8099
        end = host.find("]")
        return host[1:end] if end != -1 else host[1:]
    if host.count(":") == 1:                  # name:port or 1.2.3.4:port
        return host.rsplit(":", 1)[0]
    return host                               # bare name, or bare IPv6 (no brackets)


def _host_allowed(host_header):
    # A missing Host (HTTP/1.0, raw local tooling) is not a browser-rebinding vector, which is the
    # only thing this guard defends; direct network access is a firewall's job, not this check's.
    if not host_header:
        return True
    name = _hostname_only(host_header).strip().lower().rstrip(".")
    if not name:
        return False
    try:
        ipaddress.ip_address(name)            # any IPv4/IPv6 literal
        return True
    except ValueError:
        pass
    if name == "localhost" or name.endswith(_PRIVATE_SUFFIXES):
        return True
    if "." not in name:                       # single-label host: raspberrypi, rasai, ...
        return True
    return name in _ALLOWED_HOSTS_ENV

# The three pages share a <head> but each has its own <body>, so the tab bar is inserted into each
# rather than living in one template. aria-current is what actually marks the active tab -- the
# styling hangs off it, so a screen reader and the stylesheet cannot disagree about which is which.
_TABS = (("/", "Status"), ("/pairing", "Pairing"), ("/install", "Install AltStore"))


def _nav(active_href):
    links = []
    for href, label in _TABS:
        current = ' aria-current="page"' if href == active_href else ""
        links.append('    <a href="%s"%s>%s</a>' % (href, current, label))
    return '  <nav class="tabs">\n%s\n  </nav>\n' % "\n".join(links)


PAGE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>AltServer status</title>
<style>
  :root {
    --bg:#f6f7f9; --card:#fff; --fg:#14161a; --muted:#5b6370; --line:#e3e6ea;
    --ok:#177245; --okbg:#e8f5ee; --warn:#8a6100; --warnbg:#fdf3e0;
    --fail:#a01b2b; --failbg:#fdeaec; --unknown:#4a5160; --unknownbg:#eef0f3;
  }
  @media (prefers-color-scheme: dark) {
    :root {
      --bg:#14161a; --card:#1c1f25; --fg:#e8eaed; --muted:#9aa3b0; --line:#2a2f37;
      --ok:#5cd6a0; --okbg:#122a20; --warn:#e8b866; --warnbg:#2b2213;
      --fail:#ff8a94; --failbg:#2d1519; --unknown:#9aa3b0; --unknownbg:#22262c;
    }
  }
  * { box-sizing:border-box; }
  body { margin:0; padding:2rem 1rem; background:var(--bg); color:var(--fg);
         font:15px/1.55 system-ui,-apple-system,"Segoe UI",sans-serif; }
  .wrap { max-width:820px; margin:0 auto; }
  header { display:flex; align-items:baseline; justify-content:space-between;
           gap:1rem; flex-wrap:wrap; margin-bottom:1.25rem; }
  h1 { font-size:1.3rem; margin:0; letter-spacing:-0.01em; }
  .meta { color:var(--muted); font-size:.85rem; }
  .overall { display:inline-block; padding:.2rem .6rem; border-radius:999px;
             font-weight:600; font-size:.8rem; letter-spacing:.02em; }
  .card { background:var(--card); border:1px solid var(--line); border-radius:10px;
          padding:.9rem 1rem; margin-bottom:.6rem; }
  .row { display:flex; align-items:center; gap:.7rem; }
  .pill { flex:none; padding:.12rem .5rem; border-radius:6px; font-size:.72rem;
          font-weight:700; text-transform:uppercase; letter-spacing:.04em; }
  .name { font-weight:600; flex:none; min-width:11rem; }
  .summary { color:var(--fg); }
  .detail { color:var(--muted); font-size:.85rem; margin-top:.4rem;
            word-break:break-word; font-family:ui-monospace,SFMono-Regular,Menlo,monospace; }
  .fix { margin-top:.5rem; padding:.5rem .65rem; border-radius:7px;
         background:var(--unknownbg); font-size:.85rem; }
  .fix b { font-weight:600; }
  .ok .pill{background:var(--okbg);color:var(--ok)} .warn .pill{background:var(--warnbg);color:var(--warn)}
  .fail .pill{background:var(--failbg);color:var(--fail)} .unknown .pill{background:var(--unknownbg);color:var(--unknown)}
  .overall.ok{background:var(--okbg);color:var(--ok)} .overall.warn{background:var(--warnbg);color:var(--warn)}
  .overall.fail{background:var(--failbg);color:var(--fail)}
  footer { color:var(--muted); font-size:.8rem; margin-top:1.5rem; }
  button.act { font:inherit; font-size:.85rem; font-weight:600; padding:.35rem .8rem;
               border:1px solid var(--line); border-radius:7px; background:var(--card);
               color:var(--fg); cursor:pointer; }
  button.act:hover { border-color:var(--muted); }
  button.act[aria-pressed="true"] { background:var(--okbg); color:var(--ok); border-color:var(--ok); }
  pre.logout { margin:.7rem 0 0; padding:.6rem .7rem; max-height:24rem; overflow:auto;
               background:var(--unknownbg); border-radius:7px; font-size:.8rem; line-height:1.45;
               font-family:ui-monospace,SFMono-Regular,Menlo,monospace; white-space:pre-wrap;
               word-break:break-word; }
  nav.tabs { display:flex; flex-wrap:wrap; gap:.15rem; margin-bottom:1.4rem;
             border-bottom:1px solid var(--line); }
  nav.tabs a { padding:.5rem .8rem; margin-bottom:-1px; font-size:.9rem; font-weight:600;
               color:var(--muted); text-decoration:none; border-bottom:2px solid transparent; }
  nav.tabs a:hover { color:var(--fg); }
  nav.tabs a[aria-current="page"] { color:var(--fg); border-bottom-color:var(--fg); }
  @media (max-width:640px){ .row{flex-wrap:wrap} .name{min-width:0}
                            nav.tabs a{padding:.5rem .6rem} }
</style>
</head>
<body>
<div class="wrap">
  <header>
    <h1>AltServer status</h1>
    <div class="meta">
      <span id="overall" class="overall">checking…</span>
      &nbsp;<span id="host"></span> &middot; <span id="when"></span>
    </div>
  </header>
  <div id="checks"></div>

  <div class="card" style="margin-top:1rem">
    <div class="row">
      <button class="act" id="logtoggle" aria-pressed="false">Watch refresh log</button>
      <span class="summary" id="logstate">Not watching. Start this, then trigger a refresh from AltStore.</span>
    </div>
    <pre class="logout" id="logout" hidden></pre>
  </div>

  <footer>
    Refreshes every 30s. Read-only &mdash; this page does not sign in or change anything.
    Raw JSON at <code>/api/status</code>.
  </footer>
</div>
<script>
async function load() {
  try {
    const r = await fetch('/api/status', {cache:'no-store'});
    const d = await r.json();
    const o = document.getElementById('overall');
    o.textContent = d.overall; o.className = 'overall ' + d.overall;
    document.getElementById('host').textContent = d.host || '';
    document.getElementById('when').textContent = new Date().toLocaleTimeString();
    document.getElementById('checks').innerHTML = d.checks.map(c => `
      <div class="card ${c.state}">
        <div class="row">
          <span class="pill">${c.state}</span>
          <span class="name">${esc(c.name)}</span>
          <span class="summary">${esc(c.summary)}</span>
        </div>
        ${c.detail ? `<div class="detail">${esc(c.detail)}</div>` : ''}
        ${c.fix ? `<div class="fix"><b>Try:</b> ${esc(c.fix)}</div>` : ''}
      </div>`).join('');
  } catch (e) {
    document.getElementById('checks').innerHTML =
      '<div class="card fail"><div class="row"><span class="pill">fail</span>' +
      '<span class="summary">Status service unreachable</span></div></div>';
  }
}
function esc(s){ return String(s).replace(/[&<>"']/g, c =>
  ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c])); }
let logTimer = null;
const logBtn = document.getElementById('logtoggle');
const logOut = document.getElementById('logout');
const logState = document.getElementById('logstate');

async function pollLog(){
  try {
    const d = await (await fetch('/api/logs', {cache:'no-store'})).json();
    if (!d.available) {
      logState.textContent = d.why || 'No log available.';
      logOut.hidden = true;
      return;
    }
    const atBottom = logOut.scrollTop + logOut.clientHeight >= logOut.scrollHeight - 30;
    logOut.hidden = false;
    logOut.textContent = d.lines.length ? d.lines.join(String.fromCharCode(10)) : '(log is empty)';
    logState.textContent = d.lines.length + ' line(s) \u2014 watching';
    if (atBottom) { logOut.scrollTop = logOut.scrollHeight; }
  } catch (e) {
    logState.textContent = 'Could not read the log.';
  }
}

// Watching stops itself after this long. A refresh takes seconds, so anything beyond a few
// minutes means the tab was left open -- and an abandoned tab polling every 2s forever is a
// self-inflicted load on a box whose whole job is to sit quietly and refresh apps.
const LOG_WATCH_MS = 5 * 60 * 1000;
let logStopTimer = null;

function setWatching(on, reason){
  logBtn.setAttribute('aria-pressed', on ? 'true' : 'false');
  logBtn.textContent = on ? 'Stop watching' : 'Watch refresh log';
  clearTimeout(logStopTimer); logStopTimer = null;
  if (on) {
    pollLog();
    logTimer = setInterval(pollLog, 2000);
    logStopTimer = setTimeout(function(){
      setWatching(false, 'Stopped automatically after 5 minutes. Click to watch again.');
    }, LOG_WATCH_MS);
  } else {
    clearInterval(logTimer); logTimer = null;
    logState.textContent = reason ||
      'Stopped. The log keeps being written; nothing is being polled.';
  }
}

logBtn.addEventListener('click', () => setWatching(logTimer === null));

// Only while the tab is visible: every poll makes anisette log the machine identity and
// netmuxd print its device list, so a forgotten tab fills both logs around the clock.
load(); setInterval(() => { if (!document.hidden) load(); }, 30000);
document.addEventListener('visibilitychange', () => { if (!document.hidden) load(); });
</script>
</body>
</html>
"""


PAIRING_PAGE = PAGE.replace("<title>AltServer status</title>", "<title>Pair your iPhone</title>")

PAIRING_PAGE = PAIRING_PAGE[:PAIRING_PAGE.index("<body>")] + """<body>
<div class="wrap">
  <header>
    <h1>Pair your iPhone</h1>
    <div class="meta"><span id="when"></span></div>
  </header>
  <p class="meta" style="margin-top:-.5rem">
    A USB cable is needed for this once, and only once. Wireless pairing is not supported, but
    after this step refreshing happens over Wi-Fi and the cable is never needed again.
  </p>
  <div id="steps"></div>
  <footer>Re-checks every 5s while you work. Run the commands shown on the server itself.</footer>
</div>
<script>
function esc(s){ return String(s).replace(/[&<>\"']/g, c =>
  ({'&':'&amp;','<':'&lt;','>':'&gt;','\"':'&quot;',"'":'&#39;'}[c])); }
const PILL = {ok:'ok', todo:'warn', blocked:'fail'};
async function load(){
  try{
    const d = await (await fetch('/api/pairing',{cache:'no-store'})).json();
    document.getElementById('when').textContent =
      d.paired ? 'Paired \u2713' : ('Next: ' + (d.next||''));
    document.getElementById('steps').innerHTML = d.steps.map((s,i) => `
      <div class="card ${PILL[s.state]||'unknown'}">
        <div class="row">
          <span class="pill">${s.state==='ok'?'done':s.state}</span>
          <span class="name">${i+1}. ${esc(s.title)}</span>
        </div>
        ${s.detail ? `<div class="detail">${esc(s.detail)}</div>` : ''}
        ${s.action ? `<div class="fix"><b>Run on the server:</b><br>
           <code style="user-select:all">${esc(s.action)}</code></div>` : ''}
        ${s.note ? `<div class="fix" style="white-space:pre-line">${esc(s.note)}</div>` : ''}
      </div>`).join('');
  }catch(e){
    document.getElementById('steps').innerHTML =
      '<div class="card fail"><div class="row"><span class="pill">fail</span>' +
      '<span class="summary">Status service unreachable</span></div></div>';
  }
}
// Only while the tab is visible: every poll makes anisette log the machine identity and
// netmuxd print its device list, so a forgotten tab fills both logs around the clock.
load(); setInterval(() => { if (!document.hidden) load(); }, 5000);
document.addEventListener('visibilitychange', () => { if (!document.hidden) load(); });
</script>
</body>
</html>
"""


INSTALL_PAGE = PAGE[:PAGE.index("<body>")].replace(
    "<title>AltServer status</title>", "<title>Install AltStore</title>") + """<body>
<div class="wrap">
  <header>
    <h1>Install AltStore</h1>
    <div class="meta"><span id="state">\u2026</span></div>
  </header>

  <div class="card" id="warnbox">
    <div class="row"><span class="pill" style="background:var(--warnbg);color:var(--warn)">note</span>
    <span class="summary">Your Apple ID password is sent to this page over plain HTTP.</span></div>
    <div class="fix">Run this on loopback and reach it over an SSH tunnel unless you trust every
    device on your network. The password is passed to AltServer through the environment, never on
    a command line, and is not logged or echoed back.</div>
  </div>

  <form id="f" class="card" onsubmit="return start(event)">
    <label>Device UDID<br><input name="udid" id="udid" style="width:100%;padding:.45rem;margin:.3rem 0 .25rem"
      placeholder="detecting\u2026" required></label>
    <div id="udidnote" class="meta" style="display:none;margin-bottom:.7rem"></div>
    <label>Apple ID<br><input name="apple_id" type="email" style="width:100%;padding:.45rem;margin:.3rem 0 .7rem" required></label>
    <label>Password<br><input name="password" type="password" style="width:100%;padding:.45rem;margin:.3rem 0 .7rem" required></label>
    <button type="submit" style="padding:.5rem 1rem;font-weight:600">Install AltStore</button>
    <div id="msg" style="display:none;margin-top:.7rem;padding:.55rem .7rem;border-radius:7px;
         background:var(--failbg);color:var(--fail);font-weight:600"></div>
  </form>

  <form id="tfa" class="card" style="display:none" onsubmit="return sendCode(event)">
    <div class="row"><span class="pill" style="background:var(--warnbg);color:var(--warn)">2FA</span>
    <span class="summary">Apple sent a six-digit code to your devices.</span></div>
    <input id="code" inputmode="numeric" pattern="[0-9]{6}" maxlength="6"
      style="width:9rem;padding:.45rem;margin:.6rem .5rem 0 0;font-size:1.1rem;letter-spacing:.2em" required>
    <button type="submit" style="padding:.5rem 1rem;font-weight:600">Submit code</button>
    <span id="tfamsg" class="meta"></span>
  </form>

  <div id="cancelbox" style="display:none;margin:-.2rem 0 .6rem">
    <button type="button" class="act" onclick="cancelInstall()">Cancel install</button>
    <span class="meta">&nbsp;Stops the running sign-in. An unanswered 2FA prompt also gives up by itself after 10 minutes.</span>
  </div>

  <div class="card" id="logbox" style="display:none">
    <div class="row"><span class="name">Progress</span></div>
    <pre id="log" class="detail" style="max-height:22rem;overflow:auto;white-space:pre-wrap"></pre>
  </div>

  <footer>Credentials and account data are filtered out of the log above before it is shown.</footer>
</div>
<script>
function esc(s){ return String(s).replace(/[&<>\"']/g, c =>
  ({'&':'&amp;','<':'&lt;','>':'&gt;','\"':'&quot;',"'":'&#39;'}[c])); }
function showMsg(t){
  const el = document.getElementById('msg');
  el.textContent = t || '';
  el.style.display = t ? '' : 'none';
}
async function post(url, body){
  try {
    const r = await fetch(url, {method:'POST', headers:{'Content-Type':'application/json'},
                               body: JSON.stringify(body)});
    return await r.json();
  } catch (e) {
    return {ok:false, error:'Could not reach the server: ' + e};
  }
}
async function start(e){
  e.preventDefault();
  showMsg('');
  const f = e.target;
  const d = await post('/api/install/start', {
    udid: f.udid.value, apple_id: f.apple_id.value, password: f.password.value});
  showMsg(d.ok ? '' : (d.error || 'Could not start the install.'));
  f.password.value = '';
  return false;
}
async function sendCode(e){
  e.preventDefault();
  const d = await post('/api/install/code', {code: document.getElementById('code').value});
  document.getElementById('tfamsg').textContent = d.ok ? '' : d.error;
  if (d.ok) document.getElementById('code').value = '';
  return false;
}
async function cancelInstall(){
  const d = await post('/api/install/cancel', {});
  showMsg(d.ok ? '' : (d.error || 'Could not cancel the install.'));
  return false;
}
async function poll(){
  try{
    const d = await (await fetch('/api/install/status',{cache:'no-store'})).json();
    document.getElementById('state').textContent =
      d.state + (d.elapsed ? ' \u00b7 ' + d.elapsed + 's' : '');
    document.getElementById('tfa').style.display = d.state === 'awaiting_2fa' ? '' : 'none';
    document.getElementById('cancelbox').style.display =
      (d.state === 'running' || d.state === 'awaiting_2fa') ? '' : 'none';
    document.getElementById('logbox').style.display = d.lines.length ? '' : 'none';
    const log = document.getElementById('log');
    const atBottom = log.scrollTop + log.clientHeight >= log.scrollHeight - 20;
    log.textContent = d.lines.join(String.fromCharCode(10));
    if (atBottom) log.scrollTop = log.scrollHeight;
    if (d.error) showMsg(d.error);
  }catch(e){}
}
// The pairing page already knows the UDID. Making someone copy it across by hand is a step that
// can only go wrong -- and an empty field produced the least helpful failure available: a
// validation error that used to render as barely-visible grey text.
async function fillUdid(){
  const el = document.getElementById('udid');
  const note = document.getElementById('udidnote');
  try{
    const d = await (await fetch('/api/pairing',{cache:'no-store'})).json();
    if (d.udids && d.udids.length){
      if (!el.value) el.value = d.udids[0];        // never clobber something typed by hand
      note.textContent = d.paired
        ? 'Detected and paired.'
        : 'Detected, but the pairing is not valid \u2014 the install will fail until it is.';
      note.style.display = '';
      if (!d.paired) note.innerHTML += ' <a href="/pairing">Fix pairing \u2192</a>';
    } else {
      el.placeholder = 'no device detected';
      note.innerHTML = 'No device is connected. <a href="/pairing">Pair your iPhone first \u2192</a>';
      note.style.display = '';
    }
  }catch(e){
    el.placeholder = 'enter the device UDID';
  }
}
fillUdid();
poll(); setInterval(poll, 1500);
</script>
</body>
</html>
"""


# Insert the tab bar now that all three pages exist. Doing it here rather than inside each literal
# keeps one definition of the tabs: PAIRING_PAGE and INSTALL_PAGE are built from PAGE's <head>, so
# a nav placed in PAGE's <body> would not reach them, and three hand-written copies would drift.
def _with_nav(page, active_href):
    marker = '<div class="wrap">'
    if page.count(marker) != 1:
        raise AssertionError(
            "expected exactly one %r in the page for %s, found %d -- the tab bar would be "
            "inserted in the wrong place or not at all" % (marker, active_href, page.count(marker)))
    return page.replace(marker, marker + "\n" + _nav(active_href), 1)


PAGE = _with_nav(PAGE, "/")
PAIRING_PAGE = _with_nav(PAIRING_PAGE, "/pairing")
INSTALL_PAGE = _with_nav(INSTALL_PAGE, "/install")


class Handler(BaseHTTPRequestHandler):
    server_version = "AltServerStatus/0.1"
    # Socket read/write timeout. Without it a client that opens a connection and never finishes
    # its request holds a server thread forever, and ThreadingHTTPServer starts one per connection.
    # It bounds socket I/O only -- a slow status check does not count against it.
    timeout = 60

    def _send(self, code, body, content_type):
        payload = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(payload)

    def _host_ok(self):
        """Reject a disallowed Host with 403. Applies to GET too: rebinding reads /api/status,
        /api/pairing and /api/logs, which expose device identifiers."""
        if _host_allowed(self.headers.get("Host")):
            return True
        self._send(403,
                   "Refused: the Host header %r is not allowed. Reach this service by IP address, "
                   "as localhost, by a single-label name or one under .local/.lan/.home.arpa/"
                   ".internal, or add the name to ALTSERVER_WEB_ALLOWED_HOSTS. This blocks "
                   "DNS-rebinding from a browser.\n"
                   % self.headers.get("Host", ""),
                   "text/plain; charset=utf-8")
        return False

    def _read_json_body(self):
        """Return (ok, obj_or_None). Enforces JSON content type, an Origin that matches Host, and
        a sane, capped Content-Length -- the last of which also closes the unbounded-read hang."""
        ctype = (self.headers.get("Content-Type") or "").split(";", 1)[0].strip().lower()
        if ctype != "application/json":
            # A cross-origin form/fetch can send text/plain WITHOUT a CORS preflight; requiring
            # JSON forces a preflight this server never answers, so the browser blocks the CSRF.
            self._send(415, json.dumps({"ok": False,
                       "error": "Content-Type must be application/json."}), "application/json")
            return False, None

        origin = self.headers.get("Origin")
        if origin:
            if urlsplit(origin).netloc != (self.headers.get("Host") or ""):
                self._send(403, json.dumps({"ok": False,
                           "error": "Cross-origin request refused."}), "application/json")
                return False, None

        raw = self.headers.get("Content-Length")
        try:
            length = int(raw) if raw is not None else 0
        except (TypeError, ValueError):
            self._send(400, json.dumps({"ok": False, "error": "bad request"}), "application/json")
            return False, None
        if length < 0 or length > MAX_BODY_BYTES:
            self._send(413, json.dumps({"ok": False,
                       "error": "Request body missing, negative or too large."}), "application/json")
            return False, None

        try:
            body = json.loads(self.rfile.read(length) or b"{}")
        except Exception:
            body = None
        if not isinstance(body, dict):
            # A JSON array or scalar would reach body.get() below and kill the handler thread.
            self._send(400, json.dumps({"ok": False, "error": "bad request"}), "application/json")
            return False, None
        return True, body

    def do_GET(self):
        if not self._host_ok():
            return
        path = self.path.split("?", 1)[0]
        if path in ("/", "/index.html"):
            self._send(200, PAGE, "text/html; charset=utf-8")
        elif path == "/install":
            self._send(200, INSTALL_PAGE, "text/html; charset=utf-8")
        elif path == "/api/install/status":
            self._send(200, json.dumps(installer.INSTALLER.snapshot()), "application/json")
        elif path == "/pairing":
            self._send(200, PAIRING_PAGE, "text/html; charset=utf-8")
        elif path == "/api/pairing":
            try:
                data = pairing.diagnose()
            except Exception as exc:
                data = {"steps": [{"title": "Pairing check failed", "state": "blocked",
                                   "detail": str(exc), "action": "", "note": ""}],
                        "udids": [], "paired": False, "next": "Pairing check failed"}
            self._send(200, json.dumps(data), "application/json")
        elif path == "/api/logs":
            # AltServer's own output, as redacted by docker/redact-log.py --tee. Read from the
            # volume both containers share, so this needs no Docker socket -- which would be
            # root-on-host for a service that already takes an Apple ID password over plain HTTP.
            #
            # Read-only and tail-bounded: a refresh is a few dozen lines, and the file itself is
            # capped by the filter.
            path_log = os.environ.get("ALTSERVER_LOG", "/data/altserver.log")
            try:
                size = os.path.getsize(path_log)
                with open(path_log, "r", encoding="utf-8", errors="replace") as f:
                    if size > 200_000:
                        f.seek(size - 200_000)
                        f.readline()
                    lines = f.read().splitlines()[-400:]
                data = {"lines": lines, "available": True, "path": path_log}
            except FileNotFoundError:
                data = {"lines": [], "available": False, "path": path_log,
                        "why": "No log yet at %s. It appears once AltServer has written a line; "
                               "an image built before the log view was added will not create it."
                               % path_log}
            except Exception as exc:
                data = {"lines": [], "available": False, "path": path_log,
                        "why": "Could not read %s: %s" % (path_log, exc)}
            self._send(200, json.dumps(data), "application/json")
        elif path == "/api/status":
            try:
                data = status_checks.run_all()
            except Exception as exc:  # never let a check crash the dashboard
                data = {"overall": "fail", "host": "", "checks": [{
                    "name": "Status service", "state": "fail",
                    "summary": "A check raised an exception", "detail": str(exc), "fix": ""}]}
            self._send(200, json.dumps(data), "application/json")
        else:
            self._send(404, "not found\n", "text/plain; charset=utf-8")

    def do_POST(self):
        if not self._host_ok():
            return
        path = self.path.split("?", 1)[0]
        ok, body = self._read_json_body()
        if not ok:
            return

        if path == "/api/install/start":
            ok, err = installer.INSTALLER.start(
                body.get("udid", ""), body.get("apple_id", ""), body.get("password", ""),
                os.environ.get("ALTSERVER_ANISETTE_SERVER"))
            self._send(200, json.dumps({"ok": ok, "error": err}), "application/json")
        elif path == "/api/install/code":
            ok, err = installer.INSTALLER.submit_code(body.get("code", ""))
            self._send(200, json.dumps({"ok": ok, "error": err}), "application/json")
        elif path == "/api/install/cancel":
            # Escape hatch for an abandoned or wrong sign-in: terminate the child and return to a
            # terminal state without waiting for the 2FA timeout or a container restart.
            ok, err = installer.INSTALLER.cancel()
            self._send(200, json.dumps({"ok": ok, "error": err}), "application/json")
        else:
            self._send(404, json.dumps({"ok": False, "error": "not found"}), "application/json")

    def log_message(self, fmt, *args):
        pass  # the dashboard polls every 30s; logging that is pure noise


def main():
    ap = argparse.ArgumentParser(description="AltServer-Linux status dashboard")
    ap.add_argument("--host", default="127.0.0.1",
                    help="bind address (default 127.0.0.1; this reports device identifiers, so "
                         "do not expose it)")
    ap.add_argument("--port", type=int, default=8099)
    args = ap.parse_args()

    print("AltServer status dashboard on http://%s:%d" % (args.host, args.port), flush=True)
    # THREADING IS REQUIRED, not an optimisation. The status checks shell out to avahi-browse,
    # idevicepair and curl, which take seconds; a single-threaded server would block every other
    # request behind them. Three pages polling at 30s, 5s and 1.5s would then queue against each
    # other, which looks like the UI freezing when you switch pages.
    ThreadingHTTPServer((args.host, args.port), Handler).serve_forever()


if __name__ == "__main__":
    main()
