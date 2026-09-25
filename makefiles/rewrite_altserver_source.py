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
