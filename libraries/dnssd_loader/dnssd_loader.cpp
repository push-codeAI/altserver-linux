#include "dns_sd.h"
#include <iostream>
#include <string>
#include <vector>
#include <unistd.h>
#include <sys/prctl.h> // prctl(), PR_SET_PDEATHSIG
#include <signal.h> // signals
#include <sys/wait.h> // waitpid()
#include <arpa/inet.h> // ntohs()

// The advertiser run as `python3 -c <this> flags ifindex name regtype domain host port txthex`.
// It is a constant: per-call values arrive in argv ("" = NULL), nothing is spliced into source.
//
// It must keep servicing the avahi-compat connection. avahi-compat only dispatches events
// inside DNSServiceProcessResult(), and its client is created without AVAHI_CLIENT_NO_FAIL,
// so when avahi-daemon restarts (package upgrade, crash + D-Bus re-activation, `systemctl
// restart`) the registration is gone for good unless we notice the failure callback and
// register again. A process that just sleeps after DNSServiceRegister keeps running while
// advertising nothing, which the parent below cannot detect.
//
// It also has to wait for an avahi-daemon that is not there YET. Docker starts containers without
// waiting for avahi, and an apt upgrade restarts it; exiting then left the server unadvertised
// until something restarted it. So a failing DNSServiceRegister is retried with backoff (1 s,
// doubling to 60 s), logged once per outage, and a registration made while avahi is still in its
// first second (REGISTERING) -- which returns 0 but used to publish nothing, ever -- is completed
// by the event loop. The backoff only resets once avahi confirms a registration, so one that keeps
// failing cannot become a busy loop. The child still exits at once if python3 or libdns_sd.so is
// missing, which never gets better and which the parent's liveness check below reports.
static const char kAdvertiseScript[] = R"PY(
import ctypes as C, select, socket, sys, time
def log(msg):
    sys.stdout.write('mDNS: %s\n' % msg)
    sys.stdout.flush()
flags, ifindex = int(sys.argv[1]), int(sys.argv[2])
name, regtype, domain, host = [a.encode() if a else None for a in sys.argv[3:7]]
port, txt = socket.htons(int(sys.argv[7])), bytes.fromhex(sys.argv[8])
dll = C.CDLL('libdns_sd.so')
Reply = C.CFUNCTYPE(None, C.c_void_p, C.c_uint32, C.c_int32, C.c_char_p, C.c_char_p, C.c_char_p, C.c_void_p)
dll.DNSServiceRegister.argtypes = [C.POINTER(C.c_void_p), C.c_uint32, C.c_uint32, C.c_char_p, C.c_char_p,
                                   C.c_char_p, C.c_char_p, C.c_uint16, C.c_uint16, C.c_char_p, Reply, C.c_void_p]
dll.DNSServiceRegister.restype = C.c_int32
for fn in (dll.DNSServiceRefSockFD, dll.DNSServiceProcessResult, dll.DNSServiceRefDeallocate):
    fn.argtypes = [C.c_void_p]
dll.DNSServiceRefSockFD.restype = C.c_int
dll.DNSServiceProcessResult.restype = C.c_int32
dll.DNSServiceRefDeallocate.restype = None
state = {'err': 0, 'up': False}
def on_reply(ref, fl, err, nm, rt, dom, ctx):
    state['err'] = err
    if err == 0:
        state['up'] = True
        log('published "%s" as %s%s port %d' % ((nm or b'?').decode('utf-8', 'replace'),
            (rt or b'').decode(), (dom or b'').decode(), socket.ntohs(port)))
on_reply = Reply(on_reply)
first, delay, waiting = True, 1, False
while True:
    ref = C.c_void_p()
    state['err'], state['up'] = 0, False
    ret = dll.DNSServiceRegister(C.byref(ref), flags, ifindex, name, regtype, domain, host, port,
                                 len(txt), txt or None, on_reply, None)
    if first:
        print('DNSServiceRegister result: %d' % ret)
        sys.stdout.flush()
        first = False
    if ret != 0:
        if not waiting:
            log('avahi-daemon not reachable (error %d); retrying with backoff up to 60s until it is'
                % ret)
            waiting = True
        time.sleep(delay)
        delay = min(delay * 2, 60)
        continue
    if waiting:
        log('avahi-daemon reachable again')
        waiting = False
    try:
        fd = dll.DNSServiceRefSockFD(ref)
        while state['err'] == 0:
            select.select([fd], [], [])
            if dll.DNSServiceProcessResult(ref) != 0:
                state['err'] = -1
    except Exception as e:
        state['err'] = repr(e)
    dll.DNSServiceRefDeallocate(ref)
    delay = 1 if state['up'] else min(delay * 2, 60)
    log('registration lost (error %s): avahi-daemon restarted or went away; re-registering in %ds'
        % (state['err'], delay))
    time.sleep(delay)
)PY";

DNSServiceErrorType DNSSD_API DNSServiceRegister
    (
    DNSServiceRef                       *sdRef,
    DNSServiceFlags                     flags,
    uint32_t                            interfaceIndex,
    const char                          *name,         /* may be NULL */
    const char                          *regtype,
    const char                          *domain,       /* may be NULL */
    const char                          *host,         /* may be NULL */
    uint16_t                            port,
    uint16_t                            txtLen,
    const void                          *txtRecord,    /* may be NULL */
    DNSServiceRegisterReply             callBack,      /* may be NULL */
    void                                *context       /* may be NULL */
    ) {
        std::string txtRecordHex;
        for (int i = 0; i < txtLen; i++) {
            char buf[3];
            snprintf(buf, sizeof(buf), "%02x", ((const unsigned char *)txtRecord)[i]);
            txtRecordHex += buf;
        }

        std::vector<std::string> args = {
            "python3", "-c", kAdvertiseScript,
            std::to_string(flags), std::to_string(interfaceIndex),
            name ? name : "", regtype ? regtype : "", domain ? domain : "", host ? host : "",
            std::to_string(ntohs(port)), txtRecordHex,
        };
        // Built before fork(): the child of a multithreaded process must not allocate.
        std::vector<char *> argv;
        for (auto &arg : args) argv.push_back(&arg[0]);
        argv.push_back(nullptr);

        printf("Starting mDNS advertiser: python3 -c <advertiser> %s %s '%s' %s '%s' '%s' %s %s\n",
               args[3].c_str(), args[4].c_str(), args[5].c_str(), args[6].c_str(), args[7].c_str(),
               args[8].c_str(), args[9].c_str(), args[10].c_str());
        fflush(stdout);

        pid_t ppid_before_fork = getpid();
        int child,status;
        if ((child = fork()) < 0) {
            perror("fork");
            return EXIT_FAILURE;
        }
        if(child == 0){
            // SIGTERM when the forking thread (the listener, which never returns) dies.
            if (prctl(PR_SET_PDEATHSIG, SIGTERM) == -1) _exit(1);
            // test in case the original parent exited just
            // before the prctl() call
            if (getppid() != ppid_before_fork)
                _exit(1);
            execvp("python3", argv.data());
            _exit(1);
        } else {
            // The child is meant to run forever -- the Python advertiser never returns, it waits
            // for avahi and re-registers. So if it exits promptly, python3 or libdns_sd.so is
            // missing (avahi-daemon being down is logged by the child as "mDNS: ..." instead). Poll
            // rather than block: a healthy child never exits, and a plain waitpid() would hang
            // here for the life of the server.
            //
            // This check matters more than its size suggests. Without it this function returned
            // success unconditionally, so a missing python3 or an unloadable libdns_sd.so left
            // AltServer running, logging nothing wrong, and completely undiscoverable by the
            // device. On an unattended headless server that is the worst possible failure mode:
            // nobody finds out until a sideloaded app expires a week later.
            bool advertised = true;

            for (int attempt = 0; attempt < 20; attempt++) // ~1 second total
            {
                usleep(50 * 1000);

                int status = 0;
                pid_t result = waitpid(child, &status, WNOHANG);

                if (result == child)
                {
                    advertised = false;
                    break;
                }

                if (result < 0)
                {
                    // Cannot tell either way; assume it is running rather than cry wolf.
                    break;
                }
            }

            if (!advertised)
            {
                fprintf(stderr,
                    "ERROR: could not advertise this server over mDNS -- the python3 helper exited\n"
                    "       immediately. AltStore on your device will NOT be able to discover this\n"
                    "       server, and refreshing will never happen.\n"
                    "       Verify with the same call this program makes:\n"
                    "           python3 -c \"from ctypes import CDLL; CDLL('libdns_sd.so')\"\n"
                    "       On Debian/Ubuntu install libavahi-compat-libdnssd-dev -- the -dev package is\n"
                    "       the one providing the unversioned libdns_sd.so symlink, not libdnssd1.\n");

                return kDNSServiceErr_Unknown;
            }

            printf("Advertising this server over mDNS as _altserver._tcp on port %d\n", ntohs(port));
        }
        return 0;
    }

int DNSSD_API DNSServiceRefSockFD(DNSServiceRef sdRef) {
    return 0xDEADBEEF;
}
