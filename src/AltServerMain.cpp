// HelloWindowsDesktop.cpp
// compile with: /D_UNICODE /DUNICODE /DWIN32 /D_WINDOWS /c

#include "common.h"
#include <stdlib.h>
#include <string.h>
#include <getopt.h>

#include <fstream>
#include <iterator>
#include <iostream>
#include <sstream>
#include <iomanip>
#include <codecvt>
#include <random>

#define _T(x) x

// AltSign
#include "DeviceManager.hpp"
#include "Error.hpp"

#include "AltServerApp.h"
#include "ServerError.hpp"

#include <sys/file.h>
#include <sys/stat.h>
#include <fcntl.h>
#include <errno.h>

#define odslog(msg) { std::stringstream ss; ss << msg << std::endl; OutputDebugStringA(ss.str().c_str()); }

// Exit status of an install. It used to be 0 for every outcome -- main() caught the error, logged
// it and fell off the end -- so a scripted re-sign that failed was indistinguishable from one that
// worked. The codes separate "retry later" (3, 4, 7) from "needs a human" (2, 5, 6).
enum InstallExitCode
{
	InstallExitFailed = 1,           // anything not listed below (and usage errors, as before)
	InstallExitNeedsSignIn = 2,      // 2FA required / wrong code / wrong password
	InstallExitAnisette = 3,         // anisette server unreachable or returned unusable data
	InstallExitDevice = 4,           // device not found / connection failed or lost
	InstallExitFreeLimit = 5,        // 3 active sideloaded apps, or 10 App IDs per 7 days
	InstallExitWouldRevoke = 6,      // refused to revoke the signing certificate unattended
	InstallExitBusy = 7,             // another install holds ./AltServerData/.install.lock
};

static int InstallExitStatus(Error& error)
{
	if (auto apiError = dynamic_cast<APIError*>(&error))
	{
		switch ((APIErrorCode)apiError->code())
		{
		case APIErrorCode::IncorrectCredentials:
		case APIErrorCode::AppSpecificPasswordRequired:
		case APIErrorCode::RequiresTwoFactorAuthentication:
		case APIErrorCode::IncorrectVerificationCode:
		case APIErrorCode::AuthenticationHandshakeFailed:
			return InstallExitNeedsSignIn;
		case APIErrorCode::InvalidAnisetteData:
			return InstallExitAnisette;
		default:
			return InstallExitFailed;
		}
	}

	if (auto serverError = dynamic_cast<ServerError*>(&error))
	{
		switch ((ServerErrorCode)serverError->code())
		{
		case ServerErrorCode::InvalidAnisetteData:
			return InstallExitAnisette;
		case ServerErrorCode::DeviceNotFound:
		case ServerErrorCode::ConnectionFailed:
		case ServerErrorCode::LostConnection:
			return InstallExitDevice;
		case ServerErrorCode::MaximumFreeAppLimitReached:
			return InstallExitFreeLimit;
		default:
			return InstallExitFailed;
		}
	}

	// The C++ AltSign has no case for Apple's App ID limit, so it arrives as a LocalizedError
	// carrying Apple's own result code. 9120 is the code upstream AltSign (ALTAppleAPI.m) maps to
	// ALTAppleAPIErrorMaximumAppIDLimitReached.
	if (dynamic_cast<LocalizedError*>(&error) && error.code() == 9120)
	{
		return InstallExitFreeLimit;
	}

	// Raised by the certificate guard spliced in by rewrite_altserver_source.py.
	if (error.domain() == "com.rileytestut.AltServer.Unattended")
	{
		return InstallExitWouldRevoke;
	}

	return InstallExitFailed;
}

#include <pplx/pplxtasks.h>
#include <pplx/threadpool.h>

#include <uuid/uuid.h>
std::string make_uuid() {
    uuid_t b;
	char out[UUID_STR_LEN] = {0};
	uuid_generate(b);
  	uuid_unparse_lower(b, out);
	return out;
}

std::string temporary_directory()
{
	return fs::temp_directory_path().string();
}

std::vector<unsigned char> readFile(const char* filename)
{
	// One bulk read. This used to insert through std::istream_iterator<unsigned char> -- a
	// formatted extraction per byte -- and every byte of every file uploaded to the phone goes
	// through here (DeviceManager::WriteFile): 3.8 s per 100 MB at -O0 on x86, 0.07 s now.
	std::ifstream file(filename, std::ios::binary | std::ios::ate);
	std::streamoff fileSize = file.tellg();
	if (fileSize < 0)
	{
		// The old code reached vec.reserve(-1) here and threw std::length_error.
		throw std::runtime_error(std::string("Could not read ") + filename);
	}

	std::vector<unsigned char> vec((size_t)fileSize);
	file.seekg(0, std::ios::beg);
	if (fileSize > 0 && !file.read((char *)vec.data(), fileSize))
	{
		throw std::runtime_error(std::string("Could not read ") + filename);
	}

	return vec;
}

#define BOOST_STACKTRACE_GNU_SOURCE_NOT_REQUIRED
#include <boost/stacktrace.hpp>

#include <usbmuxd.h>

void print_help() {
	printf("Usage:  AltServer-Linux options [ ipa-file ]\n");
	printf(
			"  -h  --help             Display this usage information.\n"
			"  -u  --udid UDID        Device's UDID, only needed when installing IPA.\n"
			"  -a  --appleID AppleID  Apple ID to sign the ipa, only needed when installing IPA.\n"
			"  -p  --password passwd  Password of Apple ID, only needed when installing IPA.\n"
			"  -d  --debug            Print debug output, can be used several times to increase debug level.\n"
			"\n"
			"The following environment var can be set for some special situation:\n"
			"  - ALTSERVER_ANISETTE_SERVER: (REQUIRED) URL of an anisette server, including\n"
			"          the scheme, e.g. http://127.0.0.1:6969\n"
			"          There is no default. The server that used to be hardcoded here has been\n"
			"          returning HTTP 502 since 2026-09, and pointing every user at one shared\n"
			"          anisette identity can get Apple IDs locked. See the README.\n"
			"  - ALTSERVER_UDID / ALTSERVER_APPLE_ID / ALTSERVER_APPLE_PASSWORD:\n"
			"          Alternatives to -u / -a / -p. A command-line flag wins if both are given.\n"
			"          Prefer these when running unattended or in a container: a password passed\n"
			"          as -p is visible in `ps` to every user on the host, and lands in shell history.\n"
			"  - ALTSERVER_NO_CLIENTINFO_SANITIZE: set to 1 to stop rewriting com.apple.dt.Xcode\n"
			"          to com.apple.akd in X-MMe-Client-Info. Only useful for diagnosing sign-in\n"
			"          failures; leave unset normally.\n"
			"  - ALTSERVER_NO_SUBSCRIBE: set to skip usbmuxd_subscribe and poll the device list instead.\n"
			"          For mux implementations that do not report attach events correctly.\n"
			"  - ALTSERVER_NONINTERACTIVE: set to 1 for scripted installs (cron, systemd, a pipeline).\n"
			"          Never waits on stdin; fails at once if Apple asks for a two-factor code; and\n"
			"          refuses to revoke the signing certificate unless ALTSERVER_ALLOW_REVOKE=1.\n"
			"\n"
			"Install exit status: 0 installed, 1 other failure, 2 Apple sign-in needs a human (2FA,\n"
			"password), 3 anisette server, 4 device unreachable, 5 free-account limit (3 apps /\n"
			"10 App IDs per 7 days), 6 refused to revoke the certificate, 7 another install running.\n"
			);
}

int main(int argc, char *argv[]) {
	static struct option long_options[] =
        {
          // "help" was documented in the usage text and handled in the switch, but was never
          // listed here -- so --help fell through to the error branch, printing
          // "?? getopt returned character code 077 ??" above the usage and exiting 1.
          {"help",		no_argument,			0, 'h'},
          {"udid",		required_argument,   	0, 'u'},
          {"appleID",	required_argument,      0, 'a'},
          {"password",	required_argument,      0, 'p'},
          //{"ipaddr",	required_argument,		0, 'i'},
		  //{"pairData",	required_argument,      0, 'P'},
		  {"debug",		no_argument,      		0, 'd'},
          {0, 0, 0, 0}
        };
	
	// Initialised: these are read unconditionally at the install call below, so leaving them
	// indeterminate made a missing flag undefined behaviour rather than an error.
	char *udid = NULL;
	char *ipaddr = NULL;
	char *appleID = NULL;
	char *password = NULL;
	char *pairDataFile = NULL;
	
	char *ipaPath = NULL;
	int debugLogLevel = 0;

	while (1) {
		int this_option_optind = optind ? optind : 1;
		int option_index = 0;

		// 'h' was handled below but missing from this string, so -h fell through to the error
		// branch and printed "?? getopt returned character code 077 ??" before the usage text.
		int c = getopt_long (argc, argv, "hu:i:a:p:P:d",
						long_options, &option_index);
		if (c == -1) break;

		switch (c) {
        case 'u':
			udid = optarg;
            break;
       	case 'i':
			ipaddr = optarg;
			break;
        case 'a':
			appleID = optarg;
			break;   // was missing: -a fell through into -p, so `-a ID` set the password to ID too
        case 'p':
            password = optarg;
			break;
		case 'P':
            pairDataFile = optarg;
			break;
		case 'd':
			//debugLog = true;
			debugLogLevel++;
			break;
		case 'h':
			print_help();
			exit(0);
		default:
            printf("?? getopt returned character code 0%o ??\n", c);
			print_help();
			exit(1);
    	}
	}

	if (argc == 1) {
		printf("No argument supplied, if you want for help, please use -h or --help\n");
	}

	bool installApp = true;
	if (optind == argc) {
		printf("Not supplying ipa, running in server mode!\n");
        installApp = false;
    } else if (optind + 1 == argc) {
		ipaPath = argv[optind];
	} else {
		printf("Unknown options: ");
 		while (optind < argc)
            printf("%s ", argv[optind++]);
        printf("\n");
		return 1;
	}

	// Fall back to the environment when a flag is absent. This is what makes unattended and
	// containerised operation possible at all: a detached container has no argv to type into,
	// and -p places the Apple ID password in `ps` output for every user on the host and in shell
	// history. An env var (or a Docker secret sourced into one) is strictly better on both counts.
	// Precedence is flag > environment, so existing command lines keep working unchanged.
	{
		const char *envUdid = getenv("ALTSERVER_UDID");
		const char *envAppleID = getenv("ALTSERVER_APPLE_ID");
		const char *envPassword = getenv("ALTSERVER_APPLE_PASSWORD");

		if (udid == NULL && envUdid != NULL && *envUdid != '\0') { udid = (char *)envUdid; }
		if (appleID == NULL && envAppleID != NULL && *envAppleID != '\0') { appleID = (char *)envAppleID; }
		if (password == NULL && envPassword != NULL && *envPassword != '\0') { password = (char *)envPassword; }
	}

	if (installApp && (udid == NULL || appleID == NULL || password == NULL))
	{
		fprintf(stderr,
			"ERROR: installing an IPA requires a UDID, an Apple ID and a password.\n"
			"       Missing:%s%s%s\n"
			"       Supply them as -u/--udid, -a/--appleID, -p/--password, or as the environment\n"
			"       variables ALTSERVER_UDID, ALTSERVER_APPLE_ID and ALTSERVER_APPLE_PASSWORD.\n"
			"       Run with no IPA argument to start in server (daemon) mode instead.\n",
			udid == NULL ? " UDID" : "",
			appleID == NULL ? " AppleID" : "",
			password == NULL ? " password" : "");
		return 1;
	}

	setvbuf(stdin, NULL, _IONBF, 0); 
    setvbuf(stdout, NULL, _IONBF, 0); 
    setvbuf(stderr, NULL, _IONBF, 0); 
	
	srand(time(NULL));

	if (debugLogLevel) {
		idevice_set_debug_level(debugLogLevel);
		libusbmuxd_set_debug_level(debugLogLevel - 2);
	}
    
	signal(SIGPIPE, SIG_IGN);

	// ALTSERVER_ANISETTE_SERVER is read per-request, deep inside FetchAnisetteData(). Without a
	// check here, a daemon started without it comes up cleanly, advertises itself over Bonjour and
	// is discovered by the phone -- then fails only when someone first tries to refresh, which is
	// a long way from the actual mistake (a systemd unit missing Environment=, or `sudo` without
	// -E dropping it from the environment).
	//
	// This deliberately WARNS rather than exiting. Of the six request types the daemon serves, only
	// AnisetteDataRequest needs an anisette server; PrepareApp, InstallProvisioningProfiles,
	// RemoveProvisioningProfiles, RemoveApp and EnableUnsignedCodeExecution (AltJIT) all work
	// without one, and refusing to start would break those.
	{
		const char *anisetteServer = getenv("ALTSERVER_ANISETTE_SERVER");

		if (anisetteServer == NULL || *anisetteServer == '\0')
		{
			fprintf(stderr,
				"WARNING: ALTSERVER_ANISETTE_SERVER is not set.\n"
				"         Signing in with an Apple ID will fail, so installing and refreshing apps\n"
				"         will not work. In server mode, AltJIT and provisioning profile requests\n"
				"         still work. Set it to the URL of an anisette server, including the scheme,\n"
				"         e.g. http://127.0.0.1:6969 -- see --help.\n");
		}
		else if (strncmp(anisetteServer, "http://", 7) != 0 && strncmp(anisetteServer, "https://", 8) != 0)
		{
			fprintf(stderr,
				"WARNING: ALTSERVER_ANISETTE_SERVER (\"%s\") has no http:// or https:// scheme.\n"
				"         It will be rejected when anisette data is first requested. Use a full URL,\n"
				"         e.g. http://127.0.0.1:6969\n", anisetteServer);
		}
		else
		{
			printf("Using anisette server: %s\n", anisetteServer);
		}
	}

	if (installApp) {
		// One install at a time per AltServerData. Two installs against one Apple ID race on the
		// certificate (each can revoke the other's) and on the phone's provisioning profiles (each
		// removes them all while it installs). The lock sits next to the cached certificate it
		// protects, so it is shared by exactly the processes that share that certificate.
		// Held until exit; the kernel releases it however the process ends.
		int lockFD = -1;
		if (mkdir("AltServerData", 0700) == 0 || errno == EEXIST)
		{
			lockFD = open("AltServerData/.install.lock", O_RDWR | O_CREAT | O_CLOEXEC, 0600);
		}
		if (lockFD < 0 || flock(lockFD, LOCK_EX | LOCK_NB) != 0)
		{
			bool busy = (lockFD >= 0 && errno == EWOULDBLOCK);
			fprintf(stderr, "ERROR: %s ./AltServerData/.install.lock: %s\n",
				busy ? "another AltServer install is running; it holds" : "could not create", strerror(errno));
			return busy ? InstallExitBusy : InstallExitFailed;
		}

		odslog("Installing app...");
		std::shared_ptr<Device> _selectedDevice = std::make_shared<Device>("unknown", udid, Device::Type::All);;
		std::optional<std::string> _ipaFilepath = std::make_optional<std::string>(ipaPath);
		auto task = AltServerApp::instance()->InstallApplication(_ipaFilepath, _selectedDevice, (appleID), (password));
		int status = 0;
		try
		{
			task.get();
		}
		catch (Error& error)
		{
			odslog("Error: " << error.domain() << " (" << error.code() << ").")
			status = InstallExitStatus(error);

			if (dynamic_cast<APIError*>(&error) && (APIErrorCode)error.code() == APIErrorCode::RequiresTwoFactorAuthentication)
			{
				odslog("Apple asked for a two-factor code. Sign in once interactively (terminal or web UI) with the same anisette server, then re-run.");
			}
		}
		catch (std::exception& exception)
		{
			odslog("Exception: " << exception.what());
			odslog(boost::stacktrace::stacktrace());
			status = InstallExitFailed;
		}

		odslog("Finished!");
		return status;
	} else {
		AltServerApp::instance()->Start(0, 0);
		while (1) {
			sleep(100);
		}
	}
}