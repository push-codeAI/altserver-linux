#include "AnisetteDataManager.h"
#include <sstream>
#include <filesystem>
#include "Error.hpp"
#include "ServerError.hpp"

#include <set>
#include <charconv>
#include <ctime>
#include <cstdlib>
#include <cstring>
#include <memory>

#include "AnisetteData.h"
#include "AltServerApp.h"

#define odslog(msg) { std::stringstream ss; ss << msg << std::endl; OutputDebugStringA(ss.str().c_str()); }

AnisetteDataManager* AnisetteDataManager::_instance = nullptr;

AnisetteDataManager* AnisetteDataManager::instance()
{
	if (_instance == 0)
	{
		_instance = new AnisetteDataManager();
	}

	return _instance;
}

AnisetteDataManager::AnisetteDataManager() : loadedDependencies(false)
{
}

AnisetteDataManager::~AnisetteDataManager()
{
}

bool AnisetteDataManager::LoadiCloudDependencies()
{
	return true;
}

bool AnisetteDataManager::LoadDependencies()
{
	return true;
}

#include <cpprest/json.h>

using namespace web;                        // Common features like URIs.
using namespace web::http;                  // Common HTTP functionality
using namespace web::http::client;          // HTTP client features

// The anisette server that used to be hardcoded here (armconverter.com) has been returning
// HTTP 502 with a text/plain body since at least 2026-09, and response.extract_json() on that
// reply throws a bare "Incorrect Content-Type: must be textual to extract_string, JSON to
// extract_json." naming neither the server nor the status code. That single unexplained line
// is the most-reported failure in this project (issues #99, #100, #128, #130).
//
// There is deliberately no default any more: pointing every user at one shared anisette
// identity also gets Apple IDs locked (issue #88). The server must be chosen explicitly.
std::string GetAnisetteURL() {
	const char *server = getenv("ALTSERVER_ANISETTE_SERVER");
	if (server == NULL || *server == '\0') {
		throw ServerError(ServerErrorCode::InvalidAnisetteData, {
			{ LocalizedFailureErrorKey,
			  "No anisette server is configured. Set the ALTSERVER_ANISETTE_SERVER environment "
			  "variable to the URL of an anisette server before running AltServer." }
		});
	}
	return server;
}

std::shared_ptr<AnisetteData> AnisetteDataManager::FetchAnisetteData()
{
	std::string anisetteURL = GetAnisetteURL();
	odslog("Fetching anisette data from: " << anisetteURL);

	// http_client's constructor validates the URI and throws before any request is made: a
	// missing scheme or hostname ("localhost:6969", which is exactly what someone running a
	// containerised anisette server is likely to type) raises std::invalid_argument, and a
	// malformed URI raises uri_exception. Neither derives from Error, so uncaught they reach the
	// device as errorCode 0 (Unknown) rather than InvalidAnisetteData, and the CLI prints raw
	// cpprest text under a generic title -- the exact failure mode this function exists to end.
	std::unique_ptr<web::http::client::http_client> client;
	try
	{
		client.reset(new web::http::client::http_client(anisetteURL));
	}
	catch (const std::exception& exception)
	{
		throw ServerError(ServerErrorCode::InvalidAnisetteData, {
			{ LocalizedFailureErrorKey,
			  "ALTSERVER_ANISETTE_SERVER is not a usable URL (\"" + anisetteURL + "\"): " +
			  exception.what() + ". It must include a scheme, for example http://127.0.0.1:6969." }
		});
	}

	http_request request(methods::GET);

	std::map<utility::string_t, utility::string_t> headers = {
		{"User-Agent", "Xcode"},
	};

	for (auto& pair : headers)
	{
		if (request.headers().has(pair.first))
		{
			request.headers().remove(pair.first);
		}

		request.headers().add(pair.first, pair.second);
	}

	// This function was already synchronous -- the original chained pplx continuations and then
	// immediately called task.wait(). Doing it in a straight line makes it possible to attach the
	// URL and status code to every failure, which is the whole point of the exercise.
	http_response response;
	try
	{
		response = client->request(request).get();
		response.content_ready().wait();
	}
	catch (const std::exception& exception)
	{
		// DNS failure, connection refused, TLS error, malformed URL, ...
		throw ServerError(ServerErrorCode::InvalidAnisetteData, {
			{ LocalizedFailureErrorKey,
			  "Could not reach the anisette server at " + anisetteURL + ": " + exception.what() }
		});
	}

	auto statusCode = response.status_code();
	odslog("Received response status code: " << statusCode);

	std::string body;
	try
	{
		body = response.extract_utf8string(true).get();
	}
	catch (const std::exception&)
	{
		// A body we cannot even read as text is reported below via the status code alone.
		body = "";
	}

	// Clamp the preview to printable ASCII on one line. Two reasons beyond readability:
	// truncating at a byte boundary can split a multi-byte UTF-8 sequence, and this string is
	// copied verbatim into the JSON ErrorResponse sent to the device -- invalid UTF-8 there makes
	// the phone reject the whole response, losing the message this function worked to build. It
	// also neutralises terminal escapes, since the CLI path prints this straight to stdout.
	auto printable = [](std::string text)
	{
		for (auto& character : text)
		{
			unsigned char byte = static_cast<unsigned char>(character);
			if (byte < 0x20 || byte > 0x7E)
			{
				character = (byte == '\r' || byte == '\n' || byte == '\t') ? ' ' : '.';
			}
		}
		return text;
	};
	std::string bodyPreview = printable(body.substr(0, 256));

	if (body.empty())
	{
		bodyPreview = "(empty response body)";
	}
	else if (body.size() > 256)
	{
		bodyPreview += "...";
	}

	if (statusCode != status_codes::OK)
	{
		throw ServerError(ServerErrorCode::InvalidAnisetteData, {
			{ LocalizedFailureErrorKey,
			  "The anisette server at " + anisetteURL + " returned HTTP " +
			  std::to_string(statusCode) + ". Response body: " + bodyPreview }
		});
	}

	std::error_code parseError;
	json::value jsonVal = json::value::parse(body, parseError);
	if (parseError || !jsonVal.is_object())
	{
		throw ServerError(ServerErrorCode::InvalidAnisetteData, {
			{ LocalizedFailureErrorKey,
			  "The anisette server at " + anisetteURL + " did not return a JSON object. "
			  "Response body: " + bodyPreview }
		});
	}

	odslog("Got anisetteData json: " << jsonVal);

	auto requireString = [&](const std::string& key) -> std::string
	{
		if (!jsonVal.has_field(key))
		{
			throw ServerError(ServerErrorCode::InvalidAnisetteData, {
				{ LocalizedFailureErrorKey,
				  "The anisette server at " + anisetteURL + " returned a response with no \"" +
				  key + "\" field. Response body: " + bodyPreview }
			});
		}

		const json::value& field = jsonVal.at(key);
		if (!field.is_string())
		{
			throw ServerError(ServerErrorCode::InvalidAnisetteData, {
				{ LocalizedFailureErrorKey,
				  "The anisette server at " + anisetteURL + " returned a non-string value for \"" +
				  key + "\": " + field.serialize() }
			});
		}

		return field.as_string();
	};

	std::string clientTime = requireString("X-Apple-I-Client-Time");

	// The canonical form is YYYY-MM-DDTHH:MM:SSZ -- that is what upstream AltStore emits, via
	// NSISO8601DateFormatter with default options (AltSign/Apple API/ALTAppleAPI.m). But the value
	// parsed HERE comes from a third-party anisette server, not from AltStore, and those are
	// independent implementations. Matching the trailing "Z" as a literal would reject tails like
	// ".123456Z" or a bare "2026-09-14T12:34:56" that resolve to exactly the same instant, turning
	// a working server into a hard failure on every refresh. So parse only through the seconds.
	struct tm tm = { 0 };
	const char* tail = strptime(clientTime.c_str(), "%Y-%m-%dT%H:%M:%S", &tm);
	if (tail == NULL)
	{
		// The original ignored strptime()'s return value entirely, so an unparseable timestamp
		// silently became whatever the zero-initialised struct produced.
		throw ServerError(ServerErrorCode::InvalidAnisetteData, {
			{ LocalizedFailureErrorKey,
			  "The anisette server at " + anisetteURL + " returned an X-Apple-I-Client-Time that "
			  "does not begin with YYYY-MM-DDTHH:MM:SS: " + printable(clientTime.substr(0, 64)) }
		});
	}

	// An explicit numeric UTC offset IS rejected, because timegm() below ignores the tail entirely
	// and would otherwise silently produce an instant wrong by that offset.
	if (strchr(tail, '+') != NULL || strchr(tail, '-') != NULL)
	{
		throw ServerError(ServerErrorCode::InvalidAnisetteData, {
			{ LocalizedFailureErrorKey,
			  "The anisette server at " + anisetteURL + " returned an X-Apple-I-Client-Time with a "
			  "non-UTC offset, which cannot be interpreted reliably: " + printable(clientTime.substr(0, 64)) }
		});
	}

	// The timestamp carries a trailing "Z", so it is UTC. mktime() interprets the fields as
	// LOCAL time, which skewed the instant by the host's UTC offset on any machine not running
	// in UTC. timegm() is the UTC counterpart.
	struct timeval tv = { 0 };
	tv.tv_sec = timegm(&tm);
	tv.tv_usec = 0;

	odslog("Building anisetteData obj...");
	// Read the fields into locals first. As arguments to make_shared these would be evaluated in
	// an unspecified order, so which missing field got reported would depend on the compiler --
	// unhelpful in a function whose entire purpose is a deterministic diagnosis.
	std::string machineID = requireString("X-Apple-I-MD-M");
	std::string oneTimePassword = requireString("X-Apple-I-MD");
	std::string localUserID = requireString("X-Apple-I-MD-LU");
	std::string routingInfo = requireString("X-Apple-I-MD-RINFO");

	// This used to be std::atoi, the one unguarded field: garbage became 0 and overflow is
	// undefined behaviour -- measured, "abc" was forwarded as routingInfo 0, "-5" as
	// 18446744073709551611 and "99999999999999999999" as 18446744073709551615, each surfacing only
	// later as an opaque Apple error. Servers send a plain decimal such as "17106176"; accept only
	// that. from_chars for an unsigned type rejects a sign, whitespace and out-of-range values.
	unsigned long long routingInfoValue = 0;
	const char* routingInfoEnd = routingInfo.data() + routingInfo.size();
	auto routingInfoParse = std::from_chars(routingInfo.data(), routingInfoEnd, routingInfoValue);
	if (routingInfoParse.ec != std::errc() || routingInfoParse.ptr != routingInfoEnd)
	{
		throw ServerError(ServerErrorCode::InvalidAnisetteData, {
			{ LocalizedFailureErrorKey,
			  "The anisette server at " + anisetteURL + " returned an X-Apple-I-MD-RINFO that is not "
			  "an unsigned decimal number: \"" + printable(routingInfo.substr(0, 64)) + "\"" }
		});
	}
	std::string deviceUniqueIdentifier = requireString("X-Mme-Device-Id");
	std::string deviceSerialNumber = requireString("X-Apple-I-SRL-NO");
	std::string deviceDescription = requireString("X-MMe-Client-Info");

	// Since ~2026-09 Apple's GSA edge (gsa.apple.com/grandslam/GsService2) rejects with an
	// immediate HTTP 503 any request whose X-MMe-Client-Info contains "com.apple.dt.Xcode",
	// independent of version or User-Agent. Anisette servers commonly return exactly that
	// substring -- upstream AltStore still builds one containing com.apple.dt.Xcode/3594.4.19
	// as of v2.3.3. Sanitize it here, at the single point where anisette data enters this
	// program, before it is ever used to build a request header. See upstream PR #135.
	//
	// This is an UNVERIFIED third-party claim that we cannot test without a real Apple ID, and
	// it alters a header Apple sees -- so it is defeatable without a rebuild. If sign-in fails
	// with the rewrite in place, try ALTSERVER_NO_CLIENTINFO_SANITIZE=1 before assuming the
	// anisette server is at fault.
	const char *noSanitize = getenv("ALTSERVER_NO_CLIENTINFO_SANITIZE");
	if (noSanitize == NULL || *noSanitize == '\0')
	{
		const std::string needle = "com.apple.dt.Xcode";
		const std::string replacement = "com.apple.akd";

		size_t position = 0;
		bool rewrote = false;
		while ((position = deviceDescription.find(needle, position)) != std::string::npos)
		{
			deviceDescription.replace(position, needle.length(), replacement);
			position += replacement.length();
			rewrote = true;
		}

		if (rewrote)
		{
			odslog("Rewrote " << needle << " -> " << replacement << " in X-MMe-Client-Info "
				"(Apple 503s requests carrying it). Set ALTSERVER_NO_CLIENTINFO_SANITIZE=1 to disable.");
		}
	}
	std::string locale = requireString("X-Apple-Locale");
	std::string timeZone = requireString("X-Apple-I-TimeZone");

	auto anisetteData = std::make_shared<AnisetteData>(
		machineID,
		oneTimePassword,
		localUserID,
		routingInfoValue,
		deviceUniqueIdentifier,
		deviceSerialNumber,
		deviceDescription,
		tv,
		locale,
		timeZone);

	odslog(*anisetteData);

	return anisetteData;
}

bool AnisetteDataManager::ReprovisionDevice(std::function<void(void)> provisionCallback)
{
#if !SPOOF_MAC
	provisionCallback();
	return true;
#else
	std::string adiDirectoryPath = "C:\\ProgramData\\Apple Computer\\iTunes\\adi";

	/* Start Provisioning */

	// Move iCloud's ADI files (so we don't mess with them).
	for (const auto& entry : fs::directory_iterator(adiDirectoryPath))
	{
		if (entry.path().extension() == ".pb")
		{
			fs::path backupPath = entry.path();
			backupPath += ".icloud";

			fs::rename(entry.path(), backupPath);
		}
	}

	// Copy existing AltServer .pb files into original location to reuse the MID.
	for (const auto& entry : fs::directory_iterator(adiDirectoryPath))
	{
		if (entry.path().extension() == ".altserver")
		{
			fs::path path = entry.path();
			path.replace_extension();

			fs::rename(entry.path(), path);
		}
	}

	auto cleanUp = [adiDirectoryPath]() {
		/* Finish Provisioning */

		// Backup AltServer ADI files.
		for (const auto& entry : fs::directory_iterator(adiDirectoryPath))
		{
			// Backup AltStore file
			if (entry.path().extension() == ".pb")
			{
				fs::path backupPath = entry.path();
				backupPath += ".altserver";

				fs::rename(entry.path(), backupPath);
			}
		}

		// Copy iCloud ADI files back to original location.
		for (const auto& entry : fs::directory_iterator(adiDirectoryPath))
		{
			if (entry.path().extension() == ".icloud")
			{
				// Move backup file to original location
				fs::path path = entry.path();
				path.replace_extension();

				fs::rename(entry.path(), path);

				odslog("Copying iCloud file from: " << entry.path().string() << " to: " << path.string());
			}
		}
	};

	// Calling CopyAnisetteData implicitly generates new anisette data,
	// using the new client info string we injected.
	ObjcObject* error = NULL;
	ObjcObject* anisetteDictionary = (ObjcObject*)CopyAnisetteData(NULL, 0x1, &error);

	try
	{
		if (anisetteDictionary == NULL)
		{
			odslog("Reprovision Error:" << ((ObjcObject*)error)->description());

			ObjcObject* localizedDescription = (ObjcObject*)((id(*)(id, SEL))objc_msgSend)(error, sel_registerName("localizedDescription"));
			if (localizedDescription)
			{
				int errorCode = ((int(*)(id, SEL))objc_msgSend)(error, sel_registerName("code"));
				throw LocalizedError(errorCode, localizedDescription->description());
			}
			else
			{
				throw ServerError(ServerErrorCode::InvalidAnisetteData);
			}
		}

		odslog("Reprovisioned Anisette:" << anisetteDictionary->description());

		AltServerApp::instance()->setReprovisionedDevice(true);

		// Call callback while machine is provisioned for AltServer.
		provisionCallback();
	}
	catch (std::exception &exception)
	{
		cleanUp();

		throw;
	}

	cleanUp();

	return true;
#endif
}

bool AnisetteDataManager::ResetProvisioning()
{
	// On Windows this clears AltServer's cached ADI provisioning files so the next attempt
	// re-provisions the machine. There is no such directory on Linux -- anisette data comes
	// from an external anisette server, and nothing here is cached locally, so there is
	// nothing to reset.
	//
	// This used to iterate the literal Windows path below, which threw a
	// std::filesystem::filesystem_error about "C:\\ProgramData\\..." on every Linux run:
	//
	//     std::string adiDirectoryPath = "C:\\ProgramData\\Apple Computer\\iTunes\\adi";
	//
	// Both callers (AltServerApp.cpp, in `catch (APIError&)` when Apple returns
	// InvalidAnisetteData) invoke this while already handling an error, so that exception
	// escaped the handler and replaced Apple's real, actionable error with a confusing
	// Windows path -- and at the first call site it also aborted the 12-second retry that
	// was about to run. See issue #104.
	return true;
}
