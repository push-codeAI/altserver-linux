#!/usr/bin/python3
"""Rewrite AltServer-Windows sources into something that compiles on Linux.

WHY THIS FILE HAS GUARDS. `upstream_repo` is a submodule of rileytestut/AltServer-Windows, and
every source in it is passed through here on the way to the compiler. The substitutions below are
matched against upstream text: if upstream renames a symbol, reflows a function signature, or
changes an include, a pattern silently stops matching and the build still succeeds -- producing a
binary that is quietly missing a transformation. This is the rewriter that strips the Win32 GUI
and splices in the console implementations of Authenticate, ShowAlert and Start, so "quietly
missing" here means the parts that talk to Apple.

The other three rewriters (AltSign, ldid, idevice) already fail loudly. This one had no `raise`,
`assert` or `sys.exit` anywhere.

TWO KINDS OF CHECK, because the two kinds of substitution fail differently:

  * The AltServerApp.cpp block runs for exactly one file and every substitution in it is
    mandatory, so each one asserts a match count. The counts are measured from the current
    submodule, not guessed -- note strsafe.h appears TWICE.

  * The global substitutions run over all 35 files in the directory, including binaries like
    MenuBarIcon.ico and Resource.aps. Most legitimately match zero times in any given file, so a
    per-file count would be meaningless. They are checked as POST-CONDITIONS on the output
    instead: no L"..." literal, no boost::filesystem, no bare std::wstring may survive. That is
    strictly stronger than counting, because it also catches an occurrence arriving in a NEW form
    the pattern was never written to handle.
"""

import os
import re
import sys

F = sys.argv[1]
NAME = os.path.basename(F)

with open(F, 'rb') as f:
    content = f.read()


def _fail(what, detail):
    sys.stderr.write(
        "rewrite_altserver_source.py: %s\n"
        "  file:   %s\n"
        "  detail: %s\n"
        "The vendored AltServer-Windows source has moved and this rewrite no longer applies as\n"
        "written. Re-derive it before shipping: the build would otherwise succeed and produce a\n"
        "binary silently missing this transformation.\n" % (what, F, detail))
    sys.exit(1)


def sub_literal(text, token, expect):
    """Delete/replace a literal, asserting how many times it was found."""
    found = text.count(token)
    if found < expect:
        _fail("expected at least %d occurrence(s) of %r, found %d"
              % (expect, token.decode('utf-8', 'replace'), found), "literal substitution")
    return text.replace(token, b'')


def replace_exact(text, old, new, expect=1):
    """Swap a literal for another, asserting how many times it was found."""
    found = text.count(old)
    if found != expect:
        _fail("expected %d occurrence(s) of %r, found %d"
              % (expect, old.decode('utf-8', 'replace')[:70], found), "literal replacement")
    return text.replace(old, new)


content = re.sub(br'L("([^"\\]|\\.)*")', br'U(\1)', content)
content = re.sub(br'\n(std::string StringFromWideString.*?\n\{[\s\S]+?\})', br'/*\1*/', content)
content = re.sub(br'\n(std::wstring WideStringFromString.*?\n\{[\s\S]+?\})', br'/*\1*/', content)
content = content.replace(b'std::wstring', b'std::string')
content = content.replace(b'std::string_convert', b'std::wstring_convert')

content = content.replace(b'boost/filesystem.hpp', b'filesystem')
content = content.replace(b'boost::filesystem', b'std::filesystem')

if F.endswith('AltServerApp.cpp'):

    # MessageBox
    # IDCANCEL
    # fs::path AltServerApp::appDataDirectoryPath
    content = content.replace(b'\r', b'')

    # Counts measured against the current submodule. strsafe.h really is included twice.
    for token, expect in (
        (b'#include <windows.h>\n', 1),
        (b'#include <windowsx.h>\n', 1),
        (b'#include <strsafe.h>\n', 2),
        (b'#include <ShlObj_core.h>\n', 1),
        (b'#include <winsparkle.h>\n', 1),
        (b'#pragma comment( lib, "gdiplus.lib" ) \n', 1),
        (b'#include <gdiplus.h> \n', 1),
        (b'#include "resource.h"\n', 1),
    ):
        content = sub_literal(content, token, expect)

    def removePart(content, start, end):
        pattern = br'\n' + start + br'[\S\s]+?(' + end + br')'
        if not re.search(pattern, content):
            _fail("block removal matched nothing", "from %r to %r"
                  % (start.decode('utf-8', 'replace'), end.decode('utf-8', 'replace')))
        content = re.sub(pattern, br'\1', content)
        return content
    content = removePart(content, br'const char\* REGISTRY_ROOT_KEY', br'\nAltServerApp\* AltServerApp::_instance')
    content = removePart(content, br'static int CALLBACK BrowseFolderCallback', br'\npplx::task<std::shared_ptr<Application>> AltServerApp::InstallApplication')
    content = removePart(content, br'\n.*? AltServerApp::Authenticate', br'\npplx::task<std::shared_ptr<Team>> AltServerApp::FetchTeam')
    content = removePart(content, br'void AltServerApp::ShowNotification', br'\nvoid AltServerApp::ShowErrorAlert')
    content = removePart(content, br'bool AltServerApp::CheckDependencies', br'\nfs::path AltServerApp::certificatesDirectoryPath')

    def insertBefore(content, marker, newcontent):
        if content.count(marker) != 1:
            _fail("insertion marker occurs %d times, expected exactly 1" % content.count(marker),
                  marker.decode('utf-8', 'replace'))
        content = content.replace(marker, newcontent + b'\n' + marker)
        return content
    
    content = insertBefore(content, b'AltServerApp* AltServerApp::_instance = nullptr;', br'''
#define IDCANCEL 0
#define MessageBox(x, content, title, xx) (this->ShowAlert(title, content " (Ctrl-C to avoid)"), 1)

// Observes all exceptions that occurred in all tasks in the given range.
template<class T, class InIt>
void observe_all_exceptions(InIt first, InIt last)
{
	// TODO: FIX THIS
}

#include <sys/stat.h>
#include <cstring>

// ALTSERVER_NONINTERACTIVE=1 marks a scripted install (cron, systemd, a re-sign pipeline). It must
// never wait on stdin, and must not take the destructive decision upstream leaves to a person.
static bool AltServerNonInteractive()
{
	const char *value = getenv("ALTSERVER_NONINTERACTIVE");
	return value != NULL && *value != '\0' && strcmp(value, "0") != 0;
}

// Whether a two-factor code can arrive on stdin at all. Not when unattended, and not when stdin
// is /dev/null or closed (systemd, cron, `docker exec` without -i): there `std::cin >>` returns ""
// at once and AltSign submits that empty code to Apple -- after asking Apple to push a sign-in
// prompt to every trusted device. A pipe or a terminal may still deliver one (the web UI does).
static bool AltServerCanReadVerificationCode()
{
	if (AltServerNonInteractive())
	{
		return false;
	}

	struct stat input, devNull;
	if (fstat(STDIN_FILENO, &input) != 0)
	{
		return false;
	}

	return !(S_ISCHR(input.st_mode) && stat("/dev/null", &devNull) == 0 && input.st_rdev == devNull.st_rdev);
}

class UnattendedError : public Error
{
public:
	UnattendedError(std::string message) : Error(1, { { NSLocalizedDescriptionKey, message } })
	{
	}

	virtual std::string domain() const
	{
		return "com.rileytestut.AltServer.Unattended";
	}
};

// Revoking the development certificate stops every app signed with it from launching -- AltStore
// included -- until each is re-signed. Upstream asks first; MessageBox above answers yes by itself,
// and certificates not named "AltStore..." are revoked without even that.
static void AltServerCheckRevokeAllowed(std::shared_ptr<Certificate> certificate)
{
	const char *allow = getenv("ALTSERVER_ALLOW_REVOKE");
	if (!AltServerNonInteractive() || (allow != NULL && strcmp(allow, "1") == 0))
	{
		return;
	}

	throw UnattendedError("Refusing to revoke development certificate \"" + certificate->machineName().value_or("?") +
		"\" (serial " + certificate->serialNumber() + ") in an unattended run: every app signed with it would stop "
		"launching. ./AltServerData/Certificates/ in the working directory holds no usable key for it -- the run "
		"started somewhere else than before, or another tool (AltStore on the phone, another AltServer) replaced "
		"the certificate. Run from the directory that holds the key, or set ALTSERVER_ALLOW_REVOKE=1 once, deliberately.");
}
''')

    # Every revocation goes through this line: the "AltStore..." certificate whose key is not cached
    # here, or certificates[0] when none is named "AltStore...".
    content = replace_exact(
        content,
        b'auto certificate = (preferredCertificate != nullptr) ? preferredCertificate : certificates[0];\n',
        b'auto certificate = (preferredCertificate != nullptr) ? preferredCertificate : certificates[0];\n'
        b'                  AltServerCheckRevokeAllowed(certificate);\n')

    content = insertBefore(content, b'fs::path AltServerApp::certificatesDirectoryPath', br'''
HWND AltServerApp::windowHandle() const
{
	return _windowHandle;
}

HINSTANCE AltServerApp::instanceHandle() const
{
	return _instanceHandle;
}


bool AltServerApp::boolValueForRegistryKey(std::string key) const
{
	return false;
}

void AltServerApp::setBoolValueForRegistryKey(bool value, std::string key)
{
	return;
}

std::string AltServerApp::serverID() const
{
	//auto serverID = GetRegistryStringValue(SERVER_ID_KEY);
	//return serverID;
	return "1234567";
}

pplx::task<std::pair<std::shared_ptr<Account>, std::shared_ptr<AppleAPISession>>> AltServerApp::Authenticate(std::string appleID, std::string password, std::shared_ptr<AnisetteData> anisetteData)
{
	auto verificationHandler = [=](void)->pplx::task<std::optional<std::string>> {
		return pplx::create_task([=]() -> std::optional<std::string> {
			std::cout << "Enter two factor code" << std::endl;
			std::string _verificationCode = "";
			// A pipe at EOF leaves the string empty. Returning no code makes AltSign throw
			// RequiresTwoFactorAuthentication rather than submit "" to Apple as the code.
			if (!(std::cin >> _verificationCode) || _verificationCode.empty())
			{
				return std::nullopt;
			}
			auto verificationCode = std::make_optional<std::string>(_verificationCode);
			_verificationCode = "";

			return verificationCode;
		});
	};

	// Without a handler AltSign throws RequiresTwoFactorAuthentication as soon as Apple asks for a
	// code, before requesting the push to trusted devices (AppleAPI+Authentication.cpp).
	std::optional<std::function<pplx::task<std::optional<std::string>>(void)>> handler = std::nullopt;
	if (AltServerCanReadVerificationCode())
	{
		handler = verificationHandler;
	}

	return pplx::create_task([=]() {
		if (anisetteData == NULL)
		{
			throw ServerError(ServerErrorCode::InvalidAnisetteData);
		}

		return AppleAPI::getInstance()->Authenticate(appleID, password, anisetteData, handler);
	});
}

void AltServerApp::HandleAnisetteError(AnisetteError& error)
{
    this->ShowAlert("AnisetteData error: ", error.localizedDescription());
}

void AltServerApp::ShowNotification(std::string title, std::string message)
{
	std::cout << "Notify: " << title << std::endl << "    " << message << std::endl;
}


extern "C" int getchar();
void AltServerApp::ShowAlert(std::string title, std::string message)
{
	std::cout << "Alert: " << title << std::endl << "    " << message << std::endl;
	if (AltServerNonInteractive())
	{
		// A supervisor holding stdin open as a pipe would otherwise block here forever.
		return;
	}
	std::cout << "Press any key to continue..." << std::endl;
	//char a;
	//std::cin >> a;
	getchar();
}

fs::path AltServerApp::appDataDirectoryPath() const
{
	fs::path altserverDirectoryPath("./AltServerData");

	if (!fs::exists(altserverDirectoryPath))
	{
		fs::create_directory(altserverDirectoryPath);
	}

	return altserverDirectoryPath;
}

void AltServerApp::Start(HWND windowHandle, HINSTANCE instanceHandle)
{
	ConnectionManager::instance()->Start();

	// DeviceManager only needs 
	const char *isNoUSB = getenv("ALTSERVER_NO_SUBSCRIBE");
	if (!isNoUSB) {
		DeviceManager::instance()->Start();
	}
}

void AltServerApp::Stop()
{
}
''')


if NAME == 'ServerError.hpp':
    # InvalidAnisetteData's recovery suggestion is Windows-only advice: it tells the user to
    # install iTunes and iCloud from Apple rather than the Microsoft Store. On Linux there is no
    # iTunes to install, and the actual causes are an unreachable or unhealthy anisette server and
    # clock skew on the anisette host, whose timestamp is forwarded to Apple verbatim.
    #
    # It has to be rewritten HERE rather than supplied at the throw site: ServerError::
    # localizedRecoverySuggestion() returns from this case directly, so stuffing a
    # NSLocalizedRecoverySuggestionErrorKey into userInfo never reaches the default branch.
    # AltServerApp.cpp:1614 appends whatever this returns to the alert the operator sees.
    content = replace_exact(
        content,
        b'return "Please download the latest versions of iTunes and iCloud directly from Apple, '
        b'and not from the Microsoft Store.";',
        b'return "Check the anisette server: that ALTSERVER_ANISETTE_SERVER points at one that is '
        b'reachable and returns all ten X-Apple-* fields, and that the clock on the anisette host '
        b'is NTP-synchronised - its timestamp is sent to Apple verbatim.";')


if NAME == 'WirelessConnection.cpp':
    # The TCP transport under every AltStore request. As vendored (the pinned submodule predates
    # upstream's partial fix 7a4cd5d, "Fixes potential infinite loop after unexpected wireless
    # disconnection", 2022-05-26):
    #
    #   * ReceiveData never checks recv() for 0 (FIN) or -1 (RST): a phone that closes mid-request
    #     leaves the worker spinning at 100% of a core, printing two log lines per iteration,
    #     forever. Upstream's fix checks only == 0.
    #   * select() has the socket in the WRITE set too, which a connected socket always satisfies,
    #     so it never waits and recv() blocks with no timeout: a phone that vanishes without FIN
    #     (left Wi-Fi, VPN dropped, battery died) holds the worker forever. cpprestsdk's pool is 40
    #     threads, so enough of these and every later request is accepted but never served.
    #   * SendData ignores send() failures (-1 on Linux; upstream's fix tests for 0) and treats one
    #     partial send as complete, so "Sent Data: -1 Bytes" is followed by "Finished handling
    #     request!".
    #
    # poll(), not select(): FD_SET() with an fd >= FD_SETSIZE (1024) or -1 is undefined behaviour.
    # A failed transfer throws LostConnection, which ProcessAppRequest already turns into an error
    # response and ConnectionManager logs as "Failed to handle request".
    content = content.replace(b'\r', b'')

    content = replace_exact(content, b'#include <WinSock2.h>\n', br'''#include <WinSock2.h>
#include <netinet/tcp.h>
#include <poll.h>
#include <errno.h>
#include <string.h>
''')

    # TCP keepalive on every accepted connection: a peer that vanishes without FIN or RST is
    # declared dead after 60 s of silence plus 8 unanswered probes 15 s apart (~3 minutes).
    # Probes are answered by the phone's kernel whatever the app is doing, so unlike a receive
    # timeout this never cuts a transfer that is slow but alive (Wi-Fi, or WireGuard over
    # cellular with a 125 MB IPA); it only fires when nothing at all answers.
    content = replace_exact(content,
        b'WirelessConnection::WirelessConnection(int socket) : _socket(socket)\n'
        b'{\n'
        b'}\n',
        br'''WirelessConnection::WirelessConnection(int socket) : _socket(socket)
{
	int on = 1, idle = 60, interval = 15, count = 8;
	setsockopt(socket, SOL_SOCKET, SO_KEEPALIVE, &on, sizeof(on));
	setsockopt(socket, IPPROTO_TCP, TCP_KEEPIDLE, &idle, sizeof(idle));
	setsockopt(socket, IPPROTO_TCP, TCP_KEEPINTVL, &interval, sizeof(interval));
	setsockopt(socket, IPPROTO_TCP, TCP_KEEPCNT, &count, sizeof(count));
}
''')

    content = replace_exact(content,
        b'\t\tfd_set input_set;\n'
        b'\t\tfd_set copy_set;\n'
        b'\n'
        b'\t\tint64_t totalSentBytes = 0;\n'
        b'\n'
        b'\t\twhile (true)\n'
        b'\t\t{\n'
        b'\t\t\tstruct timeval tv;\n'
        b'\t\t\ttv.tv_sec = 1; /* 1 second timeout */\n'
        b'\t\t\ttv.tv_usec = 0; /* no microseconds. */\n'
        b'\n'
        b'\t\t\t/* Selection */\n'
        b'\t\t\tFD_ZERO(&input_set);   /* Empty the FD Set */\n'
        b'\t\t\tFD_SET(this->socket(), &input_set);  /* Listen to the input descriptor */\n'
        b'\n'
        b'\t\t\tFD_ZERO(&copy_set);   /* Empty the FD Set */\n'
        b'\t\t\tFD_SET(this->socket(), &copy_set);  /* Listen to the input descriptor */\n'
        b'\n'
        b'\t\t\tssize_t sentBytes = send(this->socket(), (const char*)data.data(), (size_t)(data.size() - totalSentBytes), 0);\n'
        b'\t\t\ttotalSentBytes += sentBytes;\n'
        b'\n'
        b'\t\t\tstd::cout << "Sent Bytes Count: " << sentBytes << " (" << totalSentBytes << ")" << std::endl;\n'
        b'\n'
        b'\t\t\tif (totalSentBytes >= sentBytes)\n'
        b'\t\t\t{\n'
        b'\t\t\t\tbreak;\n'
        b'\t\t\t}\n'
        b'\t\t}\n',
        br'''		int64_t totalSentBytes = 0;

		while (totalSentBytes < (int64_t)data.size())
		{
			ssize_t sentBytes = send(this->socket(), (const char*)data.data() + totalSentBytes, (size_t)(data.size() - totalSentBytes), MSG_NOSIGNAL);
			int sendError = errno;
			if (sentBytes < 0 && sendError == EINTR)
			{
				continue;
			}

			if (sentBytes <= 0)
			{
				std::cout << "Failed to send data: " << (sentBytes == 0 ? "no progress" : strerror(sendError)) << " (" << totalSentBytes << " of " << data.size() << " bytes sent)" << std::endl;
				throw ServerError(ServerErrorCode::LostConnection);
			}

			totalSentBytes += sentBytes;

			std::cout << "Sent Bytes Count: " << sentBytes << " (" << totalSentBytes << ")" << std::endl;
		}
''')

    content = replace_exact(content,
        b'\t\tfd_set          input_set;\n'
        b'\t\tfd_set          copy_set;\n'
        b'\n'
        b'\t\twhile (true)\n'
        b'\t\t{\n'
        b'\t\t\tstruct timeval tv;\n'
        b'\t\t\ttv.tv_sec = 1; /* 1 second timeout */\n'
        b'\t\t\ttv.tv_usec = 0; /* no microseconds. */\n'
        b'\n'
        b'\t\t\tint socket = this->socket();\n'
        b'\t\t\tstd::cout << "Checking socket: " << socket << std::endl;\n'
        b'\n'
        b'\t\t\t/* Selection */\n'
        b'\t\t\tFD_ZERO(&input_set);   /* Empty the FD Set */\n'
        b'\t\t\tFD_SET(socket, &input_set);  /* Listen to the input descriptor */\n'
        b'\n'
        b'\t\t\tFD_ZERO(&copy_set);   /* Empty the FD Set */\n'
        b'\t\t\tFD_SET(socket, &copy_set);  /* Listen to the input descriptor */\n'
        b'\n'
        b'\t\t\tint result = select(this->socket() + 1, &input_set, &copy_set, NULL, &tv);\n'
        b'\n'
        b'\t\t\tif (result == 0)\n'
        b'\t\t\t{\n'
        b'\t\t\t\tcontinue;\n'
        b'\t\t\t}\n'
        b'\t\t\telse if (result == -1)\n'
        b'\t\t\t{\n'
        b'\t\t\t\tstd::cout << "Error!" << std::endl;\n'
        b'\t\t\t}\n'
        b'\t\t\telse\n'
        b'\t\t\t{\n'
        b'\t\t\t\tssize_t readBytes = recv(this->socket(), buffer, min((ssize_t)4096, (ssize_t)(size - data.size())), 0);\n',
        # A peer that is connected (its kernel answers keepalive) but sends nothing for 10 minutes
        # is treated as gone: far above any stall a live transfer survives, and it bounds workers
        # held by an app that was suspended mid-request or by a stray client holding the port.
        # "Checking socket" is printed once per wait, as before, not once per idle second.
        br'''		const int idleLimitSeconds = 600;
		int idleSeconds = 0;

		while (data.size() < (size_t)size)
		{
			int socket = this->socket();
			if (idleSeconds == 0 && data.empty())
			{
				std::cout << "Checking socket: " << socket << std::endl;
			}

			struct pollfd pfd = { socket, POLLIN, 0 };
			int result = poll(&pfd, 1, 1000);

			if (result == 0)
			{
				if (++idleSeconds >= idleLimitSeconds)
				{
					std::cout << "No data from peer for " << idleSeconds << " s (" << data.size() << " of " << size << " bytes received)" << std::endl;
					throw ServerError(ServerErrorCode::LostConnection);
				}
				continue;
			}
			else if (result == -1)
			{
				int pollError = errno;
				if (pollError == EINTR)
				{
					continue;
				}
				std::cout << "Error! " << strerror(pollError) << std::endl;
				throw ServerError(ServerErrorCode::LostConnection);
			}
			else
			{
				ssize_t readBytes = recv(socket, buffer.data(), min((ssize_t)buffer.size(), (ssize_t)(size - data.size())), 0);
				int recvError = errno;
				if (readBytes < 0 && recvError == EINTR)
				{
					continue;
				}
				if (readBytes <= 0)
				{
					std::cout << "Connection lost: " << (readBytes == 0 ? "peer closed it" : strerror(recvError)) << " (" << data.size() << " of " << size << " bytes received)" << std::endl;
					throw ServerError(ServerErrorCode::LostConnection);
				}
				idleSeconds = 0;
''')

    # Per-byte push_back and two log lines per <=4 KiB recv() were the CPU and log cost of every
    # app upload: 1.7 s of CPU per 100 MB at -O0 on x86, and ~52,000 log lines for a 100 MB IPA.
    # Append whole recv() results from a 64 KiB heap buffer (pool threads on musl have small
    # stacks), and report progress once per 8 MiB of a large transfer instead of per chunk.
    content = replace_exact(content, b'\t\tchar buffer[4096];\n',
        b'\t\tstd::vector<char> buffer(64 * 1024);\n'
        b'\t\tsize_t lastLogged = 0;\n')
    content = replace_exact(content,
        b'\t\t\t\tfor (int i = 0; i < readBytes; i++)\n'
        b'\t\t\t\t{\n'
        b'\t\t\t\t\tdata.push_back(buffer[i]);\n'
        b'\t\t\t\t}\n'
        b'\n'
        b'\t\t\t\todslog("Received bytes: " << data.size() << "(of " << size << ")");\n',
        b'\t\t\t\tdata.insert(data.end(), buffer.begin(), buffer.begin() + readBytes);\n'
        b'\n'
        b'\t\t\t\tif (size >= 1024 * 1024 && (data.size() >= (size_t)size || data.size() - lastLogged >= 8 * 1024 * 1024))\n'
        b'\t\t\t\t{\n'
        b'\t\t\t\t\tlastLogged = data.size();\n'
        b'\t\t\t\t\todslog("Received bytes: " << data.size() << "(of " << size << ")");\n'
        b'\t\t\t\t}\n')


if NAME == 'ConnectionManager.cpp':
    # _connections (a std::set) is inserted into by HandleRequest -- on the listener thread, and
    # on a pplx thread for wired connections -- and erased by Disconnect on whichever pplx thread
    # finishes a request, with no lock. Concurrent rebalancing of one red-black tree is undefined
    # behaviour: a lost node at best, a corrupted tree (crash, or a lookup that never ends) at
    # worst. A file-static mutex suffices: the member is private and only touched here.
    content = content.replace(b'\r', b'')

    content = replace_exact(content, b'#include <chrono>\n', br'''#include <chrono>
#include <mutex>
#include <errno.h>
#include <string.h>

static std::mutex connectionsLock;
''')

    content = replace_exact(content,
        b'\t_connections.erase(connection);\n',
        b'\t{\n'
        b'\t\tstd::lock_guard<std::mutex> lock(connectionsLock);\n'
        b'\t\t_connections.erase(connection);\n'
        b'\t}\n')

    content = replace_exact(content,
        b'\tthis->_connections.insert(clientConnection);\n',
        b'\t{\n'
        b'\t\tstd::lock_guard<std::mutex> lock(connectionsLock);\n'
        b'\t\tthis->_connections.insert(clientConnection);\n'
        b'\t}\n')

    content = replace_exact(content,
        b'    return _connections;\n',
        b'    std::lock_guard<std::mutex> lock(connectionsLock);\n'
        b'    return _connections;\n')

    # accept() returning -1 was wrapped in a WirelessConnection and sent down the request path
    # (FD_SET(-1) is undefined behaviour). EMFILE/ENFILE/ENOBUFS leave the connection queued, so
    # the listening socket stays readable and select() returns at once: back off, don't spin.
    content = replace_exact(content,
        b'            int other_socket = accept(socket4, (SOCKADDR*)&clientAddress, &addrlen);\n',
        br'''            int other_socket = accept(socket4, (SOCKADDR*)&clientAddress, &addrlen);
            if (other_socket < 0)
            {
                int acceptError = errno;
                std::cout << "Failed to accept connection: " << strerror(acceptError) << std::endl;
                if (acceptError == EMFILE || acceptError == ENFILE)
                {
                    _exit(1); // descriptors exhausted: a restart frees leaked ones, waiting may not
                }
                std::this_thread::sleep_for(std::chrono::seconds(1));
                continue;
            }
''')

    # A backlog of 0 queues one pending connection: simultaneous connects have their SYN or final
    # ACK dropped and wait for a retransmit (1 s, then 2, 4 ...).
    content = replace_exact(content, b'if (listen(socket4, 0) != 0)', b'if (listen(socket4, SOMAXCONN) != 0)')

    # LIVENESS. Listen() runs on its own thread while main() only sleeps, so a listener that never
    # starts leaves a process that systemd and Docker report as running while nothing listens or
    # is advertised -- Restart= and restart: never fire. Exit so the supervisor retries instead.
    # socket() returns -1 on failure, never 0, so the upstream check could not fire at all.
    content = replace_exact(content, b'    if (socket4 == 0)\n', b'    if (socket4 < 0)\n')
    for message in (b'Failed to create socket.', b'Failed to bind socket.'):
        content = replace_exact(content,
            b'        std::cout << "' + message + b'" << std::endl;\n        return;\n',
            b'        std::cout << "' + message + b'" << std::endl;\n        _exit(1);\n')
    content = replace_exact(content,
        b'        std::cout << "Failed to prepare listening socket." << std::endl;\n',
        b'        std::cout << "Failed to prepare listening socket." << std::endl;\n        _exit(1);\n')


if NAME == 'ClientConnection.cpp':
    content = content.replace(b'\r', b'')

    # ReceiveApp held the whole IPA in memory, and pplx hands a continuation a COPY of the result,
    # so an upload peaked at twice the app size (256 MB for a 125 MB IPA, measured) and was then
    # written out byte by byte through ostreambuf_iterator. Stream it to disk in 256 KiB chunks
    # instead: peak RSS ~30 MB whatever the app size. A transfer that dies leaves no partial .ipa
    # behind in /tmp, which is the container's writable layer on the SSD.
    content = replace_exact(content,
        b'\treturn this->ReceiveData(appSize).then([this](std::vector<unsigned char> data) {\n'
        b'\t\tfs::path filepath = fs::path(temporary_directory()).append(make_uuid() + ".ipa");\n'
        b'\n'
        b'\t\tstd::ofstream file(filepath.string(), std::ios::out | std::ios::binary);\n'
        b'\t\tcopy(data.cbegin(), data.cend(), std::ostreambuf_iterator<char>(file));\n'
        b'\n'
        b'\t\treturn filepath.string();\n',
        br"""	return pplx::create_task([this, appSize]() {
		if (appSize < 0)
		{
			throw ServerError(ServerErrorCode::InvalidRequest);
		}

		fs::path filepath = fs::path(temporary_directory()).append(make_uuid() + ".ipa");
		std::ofstream file(filepath.string(), std::ios::out | std::ios::binary);
		try
		{
			for (int received = 0; received < appSize; )
			{
				int chunk = std::min(appSize - received, 256 * 1024);
				auto data = this->ReceiveData(chunk).get();
				if (!file.write((const char *)data.data(), data.size()))
				{
					std::cout << "Could not write the received app to " << filepath.string() << " (disk full?)" << std::endl;
					throw ServerError(ServerErrorCode::Unknown);
				}

				received += chunk;
				if (received == appSize || received / (8 << 20) != (received - chunk) / (8 << 20))
				{
					std::cout << "Received " << received << " of " << appSize << " bytes" << std::endl;
				}
			}
		}
		catch (...)
		{
			file.close();
			std::error_code removeError;
			fs::remove(filepath, removeError);
			throw;
		}

		return filepath.string();
""")

    # The length prefix comes straight off the wire. ReceiveData reserve()s it, so 0x7fffffff
    # claimed 2 GiB of address space and a client that then sent that much would take a 4 GB Pi
    # with it. Requests are small JSON documents -- the largest, InstallProvisioningProfilesRequest,
    # carries a few base64 profiles -- so 64 MiB is generous.
    content = replace_exact(content,
        b'\t\tstd::cout << "Receiving " << expectedBytes << " bytes..." << std::endl;\n',
        b'\t\tstd::cout << "Receiving " << expectedBytes << " bytes..." << std::endl;\n'
        b'\t\tif (expectedBytes < 0 || expectedBytes > 64 * 1024 * 1024)\n'
        b'\t\t{\n'
        b'\t\t\tthrow ServerError(ServerErrorCode::InvalidRequest);\n'
        b'\t\t}\n')

    # The async task captured the by-value parameter `request` BY REFERENCE and outlived it. The
    # body never reads it, so nothing broke -- drop the capture rather than leave a dangling one.
    content = replace_exact(content,
        b'\treturn pplx::create_task([this, &request]() {\n',
        b'\treturn pplx::create_task([this]() {\n')

    # `new utility::string_t` was deleted only in the last continuation, so a PrepareAppRequest
    # that failed before the chain was built (missing udid or contentSize) leaked it.
    content = replace_exact(content,
        b'\tutility::string_t* filepath = new utility::string_t;\n',
        b'\tauto filepath = std::make_shared<utility::string_t>();\n')
    content = replace_exact(content, b'\t\tdelete filepath;\t\t\n', b'')


if NAME == 'DeviceManager.cpp':
    content = content.replace(b'\r', b'')

    # InstallApp and RemoveApp wait for installation_proxy's final status with an unbounded
    # cv.wait(). libimobiledevice's status thread returns WITHOUT calling back on a connection
    # error and polls forever on silence, so a phone that leaves Wi-Fi, drops off WireGuard or
    # sleeps mid-operation parked the task forever -- and InstallApp still holds
    # DeviceManager::_mutex, which every later install and profile refresh needs: refreshes then
    # hang silently until AltServer is restarted (reproduced against a fake installation_proxy).
    # Bound the wait. On timeout, instproxy_client_free() first: it joins the status thread, so no
    # late callback can touch this stack frame; the handler is then erased and LostConnection
    # thrown, which the existing catch paths turn into cleanup and an error response. RemoveApp's
    # handler frees uuidString itself and could still run between the timeout and the join, so
    # the timeout path leaves it alone (a one-off leak of ~37 bytes beats a double free).
    wait = b'cv.wait(lock, [&didFinishInstalling] { return didFinishInstalling; });'
    if content.count(wait) != 2:
        _fail("expected 2 installation_proxy waits, found %d" % content.count(wait), "DeviceManager.cpp")
    content = replace_exact(content, b'#include <condition_variable>',
        b'#include <condition_variable>\n'
        b'#ifndef ALTSERVER_INSTPROXY_TIMEOUT_S\n'
        b'#define ALTSERVER_INSTPROXY_TIMEOUT_S 900\n'
        b'#endif\n')
    content = content.replace(wait,
        b'if (!cv.wait_for(lock, std::chrono::seconds(ALTSERVER_INSTPROXY_TIMEOUT_S), [&didFinishInstalling] { return didFinishInstalling; })) '
        b'{ lock.unlock(); instproxy_client_free(ipc); ipc = NULL; this->_installationProgressHandlers.erase(UUID); '
        b'std::cout << "No final status from the device after " << ALTSERVER_INSTPROXY_TIMEOUT_S << " s; giving up." << std::endl; '
        b'throw ServerError(ServerErrorCode::LostConnection); }', 1)
    content = content.replace(wait,
        b'if (!cv.wait_for(lock, std::chrono::seconds(ALTSERVER_INSTPROXY_TIMEOUT_S), [&didFinishInstalling] { return didFinishInstalling; })) '
        b'{ lock.unlock(); instproxy_client_free(ipc); ipc = NULL; this->_deletionCompletionHandlers.erase(UUID); '
        b'std::cout << "No final status from the device after " << ALTSERVER_INSTPROXY_TIMEOUT_S << " s; giving up." << std::endl; '
        b'throw ServerError(ServerErrorCode::LostConnection); }', 1)

    # A REMOVE event for a UDID that was never cached inserted a NULL entry via operator[], and
    # the ADD branch's count() check then ignored that UDID for the life of the process.
    content = replace_exact(content,
        b'std::shared_ptr<Device> device = DeviceManager::instance()->cachedDevices()[event->udid];',
        b'std::shared_ptr<Device> device = NULL; { auto& cachedDevices = DeviceManager::instance()->cachedDevices(); '
        b'auto cached = cachedDevices.find(event->udid); if (cached != cachedDevices.end()) { device = cached->second; } }')

    # A status dict without "Status" built std::string(NULL): std::logic_error on
    # libimobiledevice's thread, std::terminate, the daemon gone.
    content = replace_exact(content,
        b'if (std::string(statusName) == std::string("Complete") || errorCode != 0 || errorName != NULL)',
        b'if ((statusName != NULL && std::string(statusName) == std::string("Complete")) || errorCode != 0 || errorName != NULL)')


    # (1) Name the step that failed. Every device-side failure in this file is thrown as a bare
    # ServerError(ConnectionFailed) or (DeviceNotFound), and nothing is logged, so the journal
    # only ever says "There was an error connecting to the device." -- whether the phone
    # vanished from netmuxd, rejected the pairing record, refused TLS, or stopped offering
    # misagent/installation_proxy/afc over lockdown. Those are exactly the ways an iOS update
    # breaks this path, and each needs a different fix. The wrappers log the call and the
    # libimobiledevice error, then return it unchanged, so control flow is untouched.
    # Defined AFTER the libimobiledevice headers so the function-like macros cannot rewrite
    # the prototypes; a macro's own name is not re-expanded inside its replacement.
    content = replace_exact(content, b'#define DEVICE_LISTENING_SOCKET 28151\n', b'''#define DEVICE_LISTENING_SOCKET 28151

/* --- AltServer-Linux: log which device call failed (rewrite_altserver_source.py) --- */
template <typename E> static E altserver_trace(const char* call, E err)
{
	if ((int)err != 0) { std::cout << "[device] " << call << " failed: error " << (int)err << std::endl; }
	return err;
}
static idevice_error_t altserver_trace(const char* call, idevice_error_t err)
{
	if (err != IDEVICE_E_SUCCESS) { std::cout << "[device] " << call << " failed: idevice " << (int)err << (err == IDEVICE_E_NO_DEVICE ? " (not listed by usbmuxd/netmuxd)" : "") << std::endl; }
	return err;
}
static lockdownd_error_t altserver_trace(const char* call, lockdownd_error_t err)
{
	if (err != LOCKDOWN_E_SUCCESS) { std::cout << "[device] " << call << " failed: lockdownd " << (int)err << " (" << lockdownd_strerror(err) << ")" << std::endl; }
	return err;
}
#define idevice_new_with_options(...) altserver_trace("idevice_new_with_options", idevice_new_with_options(__VA_ARGS__))
#define lockdownd_client_new_with_handshake(...) altserver_trace("lockdownd_client_new_with_handshake", lockdownd_client_new_with_handshake(__VA_ARGS__))
#define lockdownd_start_service(c, name, svc) altserver_trace(name, lockdownd_start_service(c, name, svc))
#define misagent_client_new(...) altserver_trace("misagent_client_new", misagent_client_new(__VA_ARGS__))
#define instproxy_client_new(...) altserver_trace("instproxy_client_new", instproxy_client_new(__VA_ARGS__))
#define afc_client_new(...) altserver_trace("afc_client_new", afc_client_new(__VA_ARGS__))
/* --- end AltServer-Linux --- */
''')

    # (2) iOS 18+: do not strip every free provisioning profile during an install. Port of
    # upstream AltServer-Windows 5da5175 (1.7.2, 2024-09-04): "As of iOS 18, removing all
    # provisioning profiles causes apps to become unverified." The pinned 2022 code removes ALL
    # free profiles whenever the app being installed uses a free profile -- i.e. on every CLI
    # re-sign with a free Apple ID -- then reinstalls the cached ones afterwards.
    content = replace_exact(content, b'''			if (misagent_client_new(device, service, &mis) != MISAGENT_E_SUCCESS)
			{
				throw ServerError(ServerErrorCode::ConnectionFailed);
			}


			/* Connect to AFC service */''', b'''			if (misagent_client_new(device, service, &mis) != MISAGENT_E_SUCCESS)
			{
				throw ServerError(ServerErrorCode::ConnectionFailed);
			}

			/* Get iOS Version (AltServer-Linux: port of upstream 5da5175) */
			OperatingSystemVersion osVersion(18, 0, 0);
			{
				plist_t device_version_plist = NULL;
				char* device_version_string = NULL;
				if (lockdownd_get_value(client, NULL, "ProductVersion", &device_version_plist) == LOCKDOWN_E_SUCCESS && device_version_plist != NULL)
				{
					plist_get_string_val(device_version_plist, &device_version_string);
					if (device_version_string != NULL)
					{
						osVersion = OperatingSystemVersion(device_version_string);
						free(device_version_string);
					}
					plist_free(device_version_plist);
				}
			}


			/* Connect to AFC service */''')
    content = replace_exact(content,
        b'\t\t\tbool shouldManageProfiles = (activeProfiles.has_value() || (application->provisioningProfile() != NULL && application->provisioningProfile()->isFreeProvisioningProfile()));\n',
        b'\t\t\t// As of iOS 18, removing all provisioning profiles causes apps to become unverified (upstream 5da5175).\n'
        b'\t\t\tbool isAtLeastiOS18 = (osVersion.majorVersion >= 18);\n'
        b'\t\t\tbool shouldManageProfiles = !isAtLeastiOS18 && (activeProfiles.has_value() || (application->provisioningProfile() != NULL && application->provisioningProfile()->isFreeProvisioningProfile()));\n'
        b'\t\t\todslog("Device iOS " << osVersion.majorVersion << "; " << (shouldManageProfiles ? "removing" : "keeping") << " other free provisioning profiles during install");\n')

if NAME == 'AltServerApp.cpp':
    # AltStore 2.3 (build 64+) schedules its background refresh as a BGAppRefreshTask named
    # "<bundle ID>.Refresh", and iOS only accepts identifiers listed in Info.plist's
    # BGTaskSchedulerPermittedIdentifiers. Re-signing changes the bundle ID, so the list has to be
    # rewritten too -- upstream does this in both AltServer and AltStore since 032c461 (2026-09-13).
    # Without it, submit() fails with notPermitted inside the app and background refresh never runs.
    content = replace_exact(content,
        b'\t\tplist_dict_set_item(plist, "ALTBundleIdentifier", plist_new_string(app->bundleIdentifier().c_str()));\n',
        b'''\t\tplist_dict_set_item(plist, "ALTBundleIdentifier", plist_new_string(app->bundleIdentifier().c_str()));

		/* AltServer-Linux: BGTaskSchedulerPermittedIdentifiers must use the resigned bundle ID (upstream 032c461) */
		plist_t bgTaskIDs = plist_dict_get_item(plist, "BGTaskSchedulerPermittedIdentifiers");
		if (bgTaskIDs != nullptr && plist_get_node_type(bgTaskIDs) == PLIST_ARRAY)
		{
			std::string originalID = app->bundleIdentifier();
			std::string resignedID = profile->bundleIdentifier();
			for (uint32_t i = 0; i < plist_array_get_size(bgTaskIDs); i++)
			{
				char* raw = nullptr;
				plist_get_string_val(plist_array_get_item(bgTaskIDs, i), &raw);
				if (raw == nullptr) { continue; }
				std::string value(raw);
				free(raw);
				for (size_t pos = 0; (pos = value.find(originalID, pos)) != std::string::npos; pos += resignedID.size())
				{
					value.replace(pos, originalID.size(), resignedID);
				}
				plist_set_string_val(plist_array_get_item(bgTaskIDs, i), value.c_str());
			}
		}
''')


# --- Post-conditions on the output -----------------------------------------------------------
# Only for C/C++ text. The directory also holds .ico, .aps, .png and .rc, where these byte
# sequences could occur by coincidence and mean nothing.
if NAME.endswith(('.cpp', '.c', '.h', '.hpp')):
    for pattern, why in (
        (br'(?<![A-Za-z0-9_])L"', 'a wide string literal survived; U("...") is what compiles here'),
        (br'std::wstring(?!_convert)', 'a bare std::wstring survived (std::wstring_convert is fine)'),
        (br'boost::filesystem', 'boost::filesystem survived; the build links std::filesystem'),
        (br'boost/filesystem\.hpp', 'the boost/filesystem.hpp include survived'),
    ):
        m = re.search(pattern, content)
        if m:
            line = content[:m.start()].count(b'\n') + 1
            _fail("post-condition failed at line %d: %s" % (line, why),
                  content[max(0, m.start() - 40):m.start() + 40].decode('utf-8', 'replace'))

sys.stdout.buffer.write(content)
