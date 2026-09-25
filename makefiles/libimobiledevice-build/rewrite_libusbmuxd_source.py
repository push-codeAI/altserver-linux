#!/usr/bin/python3
"""Make libusbmuxd's device-event monitor survive a mux that goes away or misbehaves.

device_monitor() reconnects in a tight loop with no delay, and never closes the socket of the
session that just ended. Against a mux that accepts and then drops the connection (a netmuxd whose
device manager has died answers Listen with Result 0 and closes), that is ~1000 reconnects per
second, each leaking an fd: within half a second the fd numbers pass FD_SETSIZE and select()'s
FD_SET overruns the stack ("*** stack smashing detected ***", SIGABRT); with a 1024 fd limit the
process instead runs out of fds, and the listener then spins on a failing accept().

Separately, when USBMUXD_SOCKET_ADDRESS names a UNIX socket that is not up yet, the inotify path
waits for "usbmuxd" to appear in /var/run -- never true for netmuxd's /run/muxd/usbmuxd -- so
after a netmuxd restart the subscription never comes back. Polling (one connect() per second) is
what the TCP form already does; do the same for the UNIX form.
"""
import sys

PATH = sys.argv[1]
content = open(PATH, encoding="utf-8").read()


def replace_once(text, old, new, what):
    n = text.count(old)
    if n != 1:
        sys.stderr.write("rewrite_libusbmuxd_source.py: expected 1 match for %s in %s, found %d\n"
                         % (what, PATH, n))
        sys.exit(1)
    return text.replace(old, new)


content = replace_once(content,
    "\t\t\tif (usbmuxd_socket_addr[5] != '\\0') {\n"
    "\t\t\t\tres = socket_connect_unix(usbmuxd_socket_addr+5);\n",
    "\t\t\tif (usbmuxd_socket_addr[5] != '\\0') {\n"
    "#ifdef HAVE_INOTIFY\n"
    "\t\t\t\tuse_inotify = 0; /* inotify only watches /var/run; poll a custom path */\n"
    "#endif\n"
    "\t\t\t\tres = socket_connect_unix(usbmuxd_socket_addr+5);\n",
    "the UNIX: socket branch of connect_usbmuxd_socket")

# A failed Listen (usbmuxd_listen() < 0: the mux answered with an error, or the connection broke
# before the reply) retried immediately -- ~2,100 reconnects per second at 20% CPU (measured).
content = replace_once(content,
    "\t\tlistenfd = usbmuxd_listen();\n"
    "\t\tif (listenfd < 0) {\n"
    "\t\t\tcontinue;\n"
    "\t\t}\n",
    "\t\tlistenfd = usbmuxd_listen();\n"
    "\t\tif (listenfd < 0) {\n"
    "\t\t\tif (running && !cancelling) {\n"
    "\t\t\t\tsleep(1);\n"
    "\t\t\t}\n"
    "\t\t\tcontinue;\n"
    "\t\t}\n",
    "the device_monitor listen-failure retry")

content = replace_once(content,
    "\t\twhile (running) {\n"
    "\t\t\tint res = get_next_event(listenfd);\n"
    "\t\t\tif (res < 0) {\n"
    "\t\t\t\tbreak;\n"
    "\t\t\t}\n"
    "\t\t}\n",
    "\t\twhile (running) {\n"
    "\t\t\tint res = get_next_event(listenfd);\n"
    "\t\t\tif (res < 0) {\n"
    "\t\t\t\tbreak;\n"
    "\t\t\t}\n"
    "\t\t}\n"
    "\t\t/* The session is over: close it (it used to leak) and back off before reconnecting. */\n"
    "\t\tsocket_close(listenfd);\n"
    "\t\tlistenfd = -1;\n"
    "\t\tif (running && !cancelling) {\n"
    "\t\t\tsleep(1);\n"
    "\t\t}\n",
    "the device_monitor event loop")

sys.stdout.write(content)
