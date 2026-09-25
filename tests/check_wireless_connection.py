#!/usr/bin/env python3
"""Prove the AltServer TCP transport survives a phone that goes away mid-request.

WHY THIS EXISTS. Every AltStore request -- anisette, app upload, install -- goes through
upstream_repo/AltServer/WirelessConnection.cpp. As vendored, a phone that closes (FIN) or resets
(RST) the connection while AltServer is receiving leaves the worker spinning at 100% of a core,
logging ~700,000 lines a second, forever; a phone that vanishes without either (left Wi-Fi, VPN
dropped) blocks a worker forever. cpprestsdk has 40 workers, so enough of these and the server
accepts connections it never serves. makefiles/rewrite_altserver_source.py fixes it at build time.

This guards that fix in the same two ways tests/check_conn_data_layout.py guards its patch:

  1. It runs the rewriter against the real submodule sources. The rewriter exits non-zero if any
     of its guarded patterns stops matching, so a submodule bump fails here, not silently.
  2. It compiles the rewriter's OUTPUT -- the shipped WirelessConnection.cpp, not a copy -- against
     a few stub headers (no cpprestsdk needed) and drives it over real loopback sockets: FIN and
     RST mid-receive must raise within seconds, a large send must arrive intact across partial
     send()s, a send to a reset peer must raise, and keepalive must be configured.

Usage: python3 tests/check_wireless_connection.py [path/to/rewrite_altserver_source.py]
"""

import os
import re
import shutil
import subprocess
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
REWRITER = os.path.join(ROOT, "makefiles", "rewrite_altserver_source.py")
SRC = os.path.join(ROOT, "upstream_repo", "AltServer")
ERROR_HPP = os.path.join(ROOT, "upstream_repo", "AltSign", "Error.hpp")

# Just enough of ClientConnection.h for WirelessConnection to compile: the two virtuals it
# overrides, and a pplx::task that runs its function when .get() is called.
STUB_CLIENT_CONNECTION = r"""
#pragma once
#include <functional>
#include <vector>
namespace pplx {
template <class T> struct task { std::function<T()> fn; T get() { return fn(); } };
template <class F> task<decltype(std::declval<F>()())> create_task(F f)
{ task<decltype(std::declval<F>()())> t; t.fn = f; return t; }
}
class ClientConnection
{
public:
    virtual ~ClientConnection() {}
    virtual void Disconnect() {}
    virtual pplx::task<void> SendData(std::vector<unsigned char>& data) = 0;
    virtual pplx::task<std::vector<unsigned char>> ReceiveData(int size) = 0;
};
"""

HARNESS = r"""
#include "WirelessConnection.h"
#include "ServerError.hpp"
#include <arpa/inet.h>
#include <netinet/in.h>
#include <netinet/tcp.h>
#include <signal.h>
#include <sys/socket.h>
#include <unistd.h>
#include <chrono>
#include <cstdio>
#include <future>
#include <thread>

std::string NSLocalizedDescriptionKey = "NSLocalizedDescription";
std::string NSLocalizedFailureErrorKey = "NSLocalizedFailure";
std::string NSLocalizedFailureReasonErrorKey = "NSLocalizedFailureReason";
std::string NSLocalizedRecoverySuggestionErrorKey = "NSLocalizedRecoverySuggestion";

static int failures = 0;
#define CHECK(cond, what) do { if (cond) printf("ok   %s\n", what); \
    else { printf("FAIL %s\n", what); failures++; } } while (0)

// rcvbuf > 0 sizes the client's receive buffer BEFORE connect(), so the window it advertises is
// negotiated for that size. Shrinking it on a connected socket leaves an advertised window the
// buffer cannot hold: some kernels then drop segments and the sender stalls for a retransmission
// timeout (>= 200 ms) -- which is what a short send timeout on the other end then reports.
static void make_pair(int& server, int& client, int rcvbuf = 0)
{
    int l = socket(AF_INET, SOCK_STREAM, 0);
    sockaddr_in a = {}; a.sin_family = AF_INET; a.sin_addr.s_addr = htonl(INADDR_LOOPBACK);
    socklen_t n = sizeof(a);
    bind(l, (sockaddr*)&a, n); listen(l, 4); getsockname(l, (sockaddr*)&a, &n);
    client = socket(AF_INET, SOCK_STREAM, 0);
    if (rcvbuf > 0) setsockopt(client, SOL_SOCKET, SO_RCVBUF, &rcvbuf, sizeof(rcvbuf));
    connect(client, (sockaddr*)&a, n);
    server = accept(l, NULL, NULL);
    close(l);
}

static void reset(int fd) { linger lg = { 1, 0 }; setsockopt(fd, SOL_SOCKET, SO_LINGER, &lg, sizeof(lg)); close(fd); }

// The unpatched code spins or blocks forever instead of returning. A stuck thread cannot be
// cancelled, so it is detached and everything it touches is leaked; the checks carry on.
enum Outcome { RETURNED, THREW_SERVER_ERROR, THREW_OTHER, STUCK };
template <class F> static Outcome run(const char* what, F f, int seconds = 10)
{
    auto result = std::make_shared<std::promise<Outcome>>();
    std::future<Outcome> fut = result->get_future();
    std::thread([result, f]() {
        Outcome o;
        try { f(); o = RETURNED; }
        catch (ServerError&) { o = THREW_SERVER_ERROR; }
        catch (...) { o = THREW_OTHER; }
        result->set_value(o);
    }).detach();
    if (fut.wait_for(std::chrono::seconds(seconds)) != std::future_status::ready)
    {
        printf("     %s: still running after %d s (spinning or blocked)\n", what, seconds);
        return STUCK;
    }
    return fut.get();
}

int main()
{
    signal(SIGPIPE, SIG_IGN);
    int s, c;

    // Heap-allocated and never freed: a check that gets stuck leaves a thread using them.
    make_pair(s, c);
    WirelessConnection* w = new WirelessConnection(s);
    {
        int on = 0, idle = 0, intvl = 0, cnt = 0; socklen_t n = sizeof(int);
        getsockopt(s, SOL_SOCKET, SO_KEEPALIVE, &on, &n);
        getsockopt(s, IPPROTO_TCP, TCP_KEEPIDLE, &idle, &n);
        getsockopt(s, IPPROTO_TCP, TCP_KEEPINTVL, &intvl, &n);
        getsockopt(s, IPPROTO_TCP, TCP_KEEPCNT, &cnt, &n);
        int dead = idle + intvl * cnt;
        printf("     keepalive: on=%d idle=%d intvl=%d cnt=%d -> dead peer noticed after %d s\n", on, idle, intvl, cnt, dead);
        CHECK(on == 1 && dead >= 60 && dead <= 900, "keepalive frees a vanished peer within 1-15 minutes");

        write(c, "\x01\x02\x03\x04", 4);
        auto got = new std::vector<unsigned char>();
        Outcome o = run("receive 4 bytes", [w, got]() { *got = w->ReceiveData(4).get(); });
        CHECK(o == RETURNED && got->size() == 4 && (*got)[3] == 4, "a complete message is received");
    }

    make_pair(s, c);
    w = new WirelessConnection(s);
    write(c, "\x01\x02", 2); close(c);
    CHECK(run("FIN mid-receive", [w]() { w->ReceiveData(4).get(); }) == THREW_SERVER_ERROR,
          "peer closing (FIN) mid-receive raises ServerError");

    make_pair(s, c);
    w = new WirelessConnection(s);
    write(c, "\x01\x02", 2); usleep(100000); reset(c);
    CHECK(run("RST mid-receive", [w]() { w->ReceiveData(4).get(); }) == THREW_SERVER_ERROR,
          "peer resetting (RST) mid-receive raises ServerError");

    make_pair(s, c, 16384);
    w = new WirelessConnection(s);
    {
        // A send timeout on the server socket makes send() return short while the reader lags,
        // so the loop must resume from the right offset. The original sent once and stopped.
        // The reader drains ~32 KiB per 50 ms, so 2 MiB takes ~3 s: several sends return short
        // after 1 s, and a 1 s stall with NO progress (which send() reports as EAGAIN, and the
        // loop rightly treats as a dead peer) would need the reader starved for a whole second.
        timeval tv = { 1, 0 }; setsockopt(s, SOL_SOCKET, SO_SNDTIMEO, &tv, sizeof(tv));
        int small = 16384; setsockopt(s, SOL_SOCKET, SO_SNDBUF, &small, sizeof(small));
        const size_t total = 2 * 1024 * 1024;
        auto payload = new std::vector<unsigned char>(total);
        for (size_t i = 0; i < total; i++) (*payload)[i] = (unsigned char)(i * 7 + (i >> 16));
        auto received = new std::vector<unsigned char>();
        auto drained = std::make_shared<std::promise<void>>();
        auto drainedFuture = drained->get_future();
        int reader_fd = c;
        std::thread reader([reader_fd, received, total, drained]() {
            unsigned char buf[65536];
            while (received->size() < total) {
                ssize_t r = read(reader_fd, buf, sizeof(buf));
                if (r <= 0) break;
                received->insert(received->end(), buf, buf + r);
                usleep(50000);
            }
            drained->set_value();
        });
        printf("     large send: begin\n"); fflush(stdout);
        Outcome o = run("large send", [w, payload]() { w->SendData(*payload).get(); }, 60);
        std::cout.flush(); printf("     large send: end\n"); fflush(stdout);
        // SendData returns once the last byte is in the kernel, with up to both socket buffers
        // still in flight: let the reader drain them. Shutting its socket down straight away
        // (as this once did) truncated the tail whenever the reader was a little behind -- a
        // false failure on a busy CI runner. Only a reader still waiting after a failed or
        // stalled send is cut off.
        if (o != RETURNED || drainedFuture.wait_for(std::chrono::seconds(30)) != std::future_status::ready)
            shutdown(c, SHUT_RDWR);
        reader.join();
        if (o != RETURNED || *received != *payload)
            printf("     large send: outcome %d, %zu of %zu bytes received\n", (int)o, received->size(), total);
        CHECK(o == RETURNED && *received == *payload, "a large payload arrives complete and in order across partial sends");
    }

    make_pair(s, c);
    w = new WirelessConnection(s);
    reset(c); usleep(100000);
    {
        auto payload = new std::vector<unsigned char>(65536, 0x42);
        CHECK(run("send to reset peer", [w, payload]() { w->SendData(*payload).get(); }) == THREW_SERVER_ERROR,
              "sending to a peer that reset raises ServerError instead of reporting -1 bytes as sent");
    }

    make_pair(s, c);
    w = new WirelessConnection(s);
    {
        auto none = new std::vector<unsigned char>(1);
        Outcome o = run("receive 0 bytes", [w, none]() { *none = w->ReceiveData(0).get(); }, 3);
        CHECK(o == RETURNED && none->empty(), "a zero-length receive returns at once");
    }

    printf(failures ? "%d check(s) failed\n" : "all checks passed\n", failures);
    fflush(stdout);
    _exit(failures ? 1 : 0);   // do not wait for, or destroy anything used by, a stuck thread
}
"""


def rewrite(rewriter, path):
    proc = subprocess.run([sys.executable, rewriter, path], capture_output=True)
    if proc.returncode != 0:
        print("FAIL: the rewriter no longer applies to %s:\n" % os.path.relpath(path, ROOT))
        print(proc.stderr.decode("utf-8", "replace").rstrip())
        return None
    return proc.stdout.decode("utf-8", "replace")


def structural(wireless, manager):
    """The shape of the fix, independent of whether it happens to compile."""
    problems = []
    if "FD_SET(" in wireless or "select(" in wireless:
        problems.append("WirelessConnection.cpp still uses select()/FD_SET (write-set wakeups, "
                        "undefined behaviour for fd >= FD_SETSIZE)")
    if not re.search(r"readBytes\s*<=\s*0\s*\)\s*\{[^}]*throw ServerError", wireless):
        problems.append("recv() returning 0 (FIN) or -1 (RST, keepalive timeout) does not throw")
    if not re.search(r"sentBytes\s*<=\s*0\s*\)\s*\{[^}]*throw ServerError", wireless):
        problems.append("send() returning -1 does not throw")
    if "data.data() + totalSentBytes" not in wireless:
        problems.append("SendData does not resume from the unsent offset after a partial send")
    for opt in ("SO_KEEPALIVE", "TCP_KEEPIDLE", "TCP_KEEPINTVL", "TCP_KEEPCNT"):
        if opt not in wireless:
            problems.append("accepted sockets do not set %s" % opt)

    lines = manager.splitlines()
    for i, line in enumerate(lines):
        if re.search(r"_connections\.(insert|erase)\(|return _connections;", line):
            if not any("lock_guard" in l and "connectionsLock" in l for l in lines[max(0, i - 2):i]):
                problems.append("ConnectionManager.cpp:%d touches _connections without the lock: %s"
                                % (i + 1, line.strip()))
    if not re.search(r"other_socket\s*<\s*0", manager):
        problems.append("accept() failure (-1) is still handed to a WirelessConnection")
    if re.search(r"listen\(\s*socket4\s*,\s*0\s*\)", manager):
        problems.append("listen() backlog is still 0")
    return problems


def main():
    strict = bool(os.environ.get("CI"))
    rewriter = sys.argv[1] if len(sys.argv) > 1 else REWRITER

    if not os.path.exists(os.path.join(SRC, "WirelessConnection.cpp")):
        msg = "upstream_repo is not checked out (git submodule update --init upstream_repo)"
        if strict:
            print("FAIL: " + msg + "\n      Refusing to skip under CI.")
            return 1
        print("SKIP: " + msg)
        return 0

    out = {}
    for name in ("WirelessConnection.cpp", "WirelessConnection.h", "ConnectionManager.cpp",
                 "ServerError.hpp", "ServerError.cpp"):
        text = rewrite(rewriter, os.path.join(SRC, name))
        if text is None:
            return 1
        out[name] = text
    print("rewriter applies cleanly to WirelessConnection.cpp and ConnectionManager.cpp")

    problems = structural(out["WirelessConnection.cpp"], out["ConnectionManager.cpp"])
    if problems:
        print("FAIL: the rewritten sources do not contain the fix:\n  - " + "\n  - ".join(problems))
        return 1
    print("rewritten sources are structurally correct")

    cxx = os.environ.get("CXX") or shutil.which("g++") or shutil.which("c++")
    if not cxx:
        if strict:
            print("FAIL: no C++ compiler available. Refusing to skip the socket cases under CI.")
            return 1
        print("SKIP: no C++ compiler available to run the socket cases")
        return 0

    tmp = tempfile.mkdtemp(prefix="wireless_")
    try:
        os.makedirs(os.path.join(tmp, "cpprest"))
        open(os.path.join(tmp, "cpprest", "json.h"), "w").close()
        with open(os.path.join(tmp, "ClientConnection.h"), "w") as f:
            f.write(STUB_CLIENT_CONNECTION)
        shutil.copy(ERROR_HPP, os.path.join(tmp, "Error.hpp"))
        for name, text in out.items():
            if name != "ConnectionManager.cpp":
                with open(os.path.join(tmp, name), "w") as f:
                    f.write(text)
        with open(os.path.join(tmp, "harness.cpp"), "w") as f:
            f.write(HARNESS)
        exe = os.path.join(tmp, "harness")
        # The same forced includes as the real build (Makefile), so the shims are the shipped ones.
        cmd = [cxx, "-std=c++17", "-O1", "-pthread", "-fpermissive", "-w",
               "-I" + tmp, "-I" + os.path.join(ROOT, "shims"), "-I" + os.path.join(ROOT, "src"),
               "-include", "windows_shim.h", "-include", "common.h",
               "-o", exe, os.path.join(tmp, "harness.cpp"),
               os.path.join(tmp, "WirelessConnection.cpp"), os.path.join(tmp, "ServerError.cpp")]
        build = subprocess.run(cmd, capture_output=True, text=True)
        if build.returncode != 0:
            print("FAIL: the rewritten WirelessConnection.cpp does not compile:\n" + build.stderr[-3000:])
            return 1
        run = subprocess.run([exe], capture_output=True, text=True, timeout=180)
        # WirelessConnection logs every chunk; show only the verdict lines, and why a send failed.
        lines = run.stdout.splitlines()
        sys.stdout.write("".join(l + "\n" for l in lines
                                 if l.startswith(("ok ", "FAIL", "     keepalive", "     large send: outcome",
                                                  "all checks", "check(s)", "Failed to send"))
                                 or "check(s) failed" in l or "still running" in l))
        # The large send must actually have come back short and been resumed, or the case above
        # proved nothing about the resume offset.
        if "     large send: begin" in lines and "     large send: end" in lines:
            section = lines[lines.index("     large send: begin"):lines.index("     large send: end")]
            sends = sum(1 for l in section if l.startswith("Sent Bytes Count: "))
            if sends < 2:
                print("FAIL the large send completed in %d send() call(s); the resume path was not exercised"
                      % sends)
                return 1
            print("ok   the large send took %d send() calls" % sends)
        if run.returncode != 0:
            print("\nFAIL: the shipped transport mishandles a peer that goes away.")
            return 1
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    print("\nFIN, RST and send failures raise; large sends arrive intact; keepalive is on.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
