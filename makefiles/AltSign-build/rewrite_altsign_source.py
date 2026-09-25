#!/usr/bin/python3

import re
import sys

F = sys.argv[1]

with open(F, 'rb') as f:
    content = f.read()

content = re.sub(br'L("([^"\\]|\\.)*")', br'U(\1)', content)
content = content.replace(b'std::wstring', b'std::string')
content = content.replace(b'boost/filesystem.hpp', b'filesystem')
content = content.replace(b'boost::filesystem', b'std::filesystem')

content = content.replace(b'"%FT%T%z"', b'"%Y-%m-%dT%H:%M:%SZ"')
content = content.replace(b'localtime(', b'gmtime(')


# --- Give every GrandSlam request its own TCP connection -------------------------------
#
# AppleAPI is a process-wide singleton holding ONE _gsaClient, built in the constructor.
# gsaClient() hands back a copy sharing the same cpprestsdk impl and therefore the same asio
# connection pool, and the second GSA request is issued from a .then() continuation the instant
# the first completes -- textbook keep-alive reuse. No Connection header is ever set.
#
# Since ~2026-09 Apple's GrandSlam edge refuses the SECOND request on a reused connection.
# Observed here: request 1 -> 200, request 2 -> 429, identically on the first-ever attempt and
# again 28 minutes later. Positional and non-cumulative, which is not how volume throttling
# behaves. Every header and all ten anisette values are byte-identical between the two requests,
# so the only things that differ are the plist body and the connection position.
#
# This is the same fix as rileytestut/AltSign PR #52 ("Use a separate connection for each
# GrandSlam request", shipped in AltServer 1.7.6) and nab138/iloader 2.3.3 ("Disabled reqwest
# pooling to alleviate http 429 from grandslam"). Note iloader already carried the com.apple.akd
# client-info fix when it hit this, which is why that fix REVEALS the 429 rather than causing it.
#
# Cost: one extra TLS handshake per GrandSlam request, a handful of times per sign-in.
_gsa_old = (
    b'web::http::client::http_client AppleAPI::gsaClient()\n'
    b'{\n'
    b'\treturn this->_gsaClient;\n'
    b'}\n'
)
_gsa_new = (
    b'web::http::client::http_client AppleAPI::gsaClient()\n'
    b'{\n'
    b'\t// Patched by rewrite_altsign_source.py: a FRESH client per call, so each GrandSlam\n'
    b'\t// request opens its own connection instead of reusing the singleton\'s pooled one.\n'
    b'\t// Apple 429s the second request on a reused connection. See the note in the rewriter.\n'
    b'\thttp_client_config gsaConfig;\n'
    b'\tgsaConfig.set_validate_certificates(false);\n'
    b'\treturn web::http::client::http_client(U("https://gsa.apple.com"), gsaConfig);\n'
    b'}\n'
)

if F.endswith('AppleAPI.cpp'):
    if content.count(_gsa_old) != 1:
        sys.stderr.write(
            "rewrite_altsign_source.py: gsaClient() connection patch matched %d times, expected 1.\n"
            "  upstream AppleAPI.cpp changed; re-check before removing this guard.\n"
            % content.count(_gsa_old))
        sys.exit(1)
    content = content.replace(_gsa_old, _gsa_new)

# --- Make a non-200 from Apple's auth endpoint legible -------------------------------
# AppleAPI+Authentication.cpp logs the HTTP status and then DISCARDS it, feeding the body
# straight to plist_from_xml. A 429 body is not plist XML, so it fails to parse and the user
# is told "Server returned invalid response" (APIErrorCode::InvalidResponse, 17) -- which
# sends them looking for a protocol bug when Apple is simply rate limiting them.
# Observed for real: auth request 1 -> 200, request 2 -> 429, reported as "invalid response".
# Same defect class as the unchecked extract_json() this project already fixed in
# src/AnisetteDataManager.cpp: log the status, ignore it, mis-report the consequence.
_auth_old = (
    b'\t\t\t\todslog("Received auth response status code: " << response.status_code());\r\n'
)
_auth_new = (
    b'\t\t\t\todslog("Received auth response status code: " << response.status_code());\r\n'
    b'\t\t\t\tif (response.status_code() != 200)\r\n'
    b'\t\t\t\t{\r\n'
    b'\t\t\t\t\todslog("WARNING: Apple\'s auth endpoint returned HTTP " << response.status_code()\r\n'
    b'\t\t\t\t\t\t<< ". If this is 429, Apple is RATE LIMITING this machine or Apple ID: wait "\r\n'
    b'\t\t\t\t\t\t   "30-60 minutes and try ONCE more. Do NOT retry in a loop -- repeated failed "\r\n'
    b'\t\t\t\t\t\t   "attempts are how Apple IDs get locked. Any \'invalid response\' error below is "\r\n'
    b'\t\t\t\t\t\t   "misleading: the body is simply not a plist.");\r\n'
    b'\t\t\t\t}\r\n'
)

if F.endswith('AppleAPI+Authentication.cpp'):
    if content.count(_auth_old) != 1:
        sys.stderr.write(
            "rewrite_altsign_source.py: auth status-code patch matched %d times, expected 1.\n"
            "  upstream AppleAPI+Authentication.cpp changed; re-check before removing this guard.\n"
            % content.count(_auth_old))
        sys.exit(1)
    content = content.replace(_auth_old, _auth_new)

# --- Initialise PKCS12_parse()'s out-parameters -------------------------------------------
# Certificate(p12Data, password) declares `EVP_PKEY* key; X509* certificate;` uninitialised and
# relies on PKCS12_parse() to null them. LibreSSL 3.4 (the Alpine 3.15 build) returns early for a
# NULL PKCS12 -- an empty or truncated ./AltServerData/Certificates/<team>.p12, e.g. after power is
# lost just after the cache was written -- WITHOUT nulling them, so the nullptr check that follows
# reads stack garbage. (OpenSSL 3 nulls them first; the dynamic test build does not show this.)
_p12_old = b'\tEVP_PKEY* key;\r\n\tX509* certificate;\r\n'
_p12_new = b'\tEVP_PKEY* key = nullptr;\r\n\tX509* certificate = nullptr;\r\n'

if F.endswith('/Certificate.cpp') or F == 'Certificate.cpp':
    if content.count(_p12_old) != 1:
        sys.stderr.write(
            "rewrite_altsign_source.py: PKCS12 out-parameter patch matched %d times, expected 1.\n"
            "  upstream Certificate.cpp changed; re-check before removing this guard.\n"
            % content.count(_p12_old))
        sys.exit(1)
    content = content.replace(_p12_old, _p12_new)

# --- Let the operator answer an Apple client-identity change without a rebuild --------------
# GrandSlam requests still say "akd/1.0 CFNetwork/978.0.7 Darwin/18.7.0" (macOS 10.14) and the
# 2FA requests "Xcode" + "X-Xcode-Version: 11.2". Upstream AltSign replaced both on 2026-09-03 and
# 2026-09-16 ("Apple's servers reject outdated client identities", ec2968c / 468313b) with
# "AuthKit/1 (Macintosh; OS X 26.5.2) (com.apple.dt.Xcode/26.0)". The defaults are left alone -- they
# are the ones proven here -- but ALTSERVER_GSA_USER_AGENT overrides every one of them at runtime.
_ua_sites = (
    (b'{U("User-Agent"), U("Xcode")},', b'{U("User-Agent"), U(getenv("ALTSERVER_GSA_USER_AGENT") ? getenv("ALTSERVER_GSA_USER_AGENT") : "Xcode")},'),
    (b'{U("User-Agent"), U("akd/1.0 CFNetwork/978.0.7 Darwin/18.7.0")}', b'{U("User-Agent"), U(getenv("ALTSERVER_GSA_USER_AGENT") ? getenv("ALTSERVER_GSA_USER_AGENT") : "akd/1.0 CFNetwork/978.0.7 Darwin/18.7.0")}'),
)
if F.endswith('AppleAPI+Authentication.cpp'):
    for _old, _new in _ua_sites:
        if content.count(_old) != 1:
            sys.stderr.write("rewrite_altsign_source.py: User-Agent patch matched %d times, expected 1: %r\n"
                             % (content.count(_old), _old))
            sys.exit(1)
        content = content.replace(_old, _new)

content = content.replace(b'winsock2.h', b'WinSock2.h')

sys.stdout.buffer.write(content)