// Copyright (c) 2026, Aegisub MCP contributors
// SPDX-License-Identifier: BSD-3-Clause

#include "mcp_bridge.h"

#include "ass_dialogue.h"
#include "ass_file.h"
#include "ass_style.h"
#include "async_video_provider.h"
#include "audio_controller.h"
#include "frame_main.h"
#include "include/aegisub/context.h"
#include "selection_controller.h"
#include "subs_controller.h"
#include "time_range.h"
#include "project.h"
#include "video_controller.h"
#include "video_frame.h"

#include <libaegisub/audio/provider.h>
#include <libaegisub/cajun/writer.h>

#include <algorithm>
#include <chrono>
#include <cmath>
#include <fstream>
#include <map>
#include <memory>
#include <random>
#include <set>
#include <sstream>
#include <stdexcept>
#include <vector>
#include <wx/utils.h>
#include <wx/base64.h>
#include <wx/image.h>
#include <wx/mstream.h>
#ifdef _WIN32
#include <windows.h>
#else
#include <fcntl.h>
#include <sys/file.h>
#include <unistd.h>
#endif

namespace {
constexpr int64_t max_time = 35999990; // ASS: 9:59:59.99
constexpr size_t max_message = 4 * 1024 * 1024;

int64_t Now() {
	return std::chrono::duration_cast<std::chrono::milliseconds>(
		std::chrono::system_clock::now().time_since_epoch()).count();
}

std::string Serialize(json::Object const& value) {
	std::ostringstream stream;
	agi::JsonWriter::Write(value, stream);
	return stream.str();
}

std::string OperationId(json::UnknownElement const& value) {
	std::string id = value;
	if (id.size() != 32 || id.find_first_not_of("0123456789abcdef") != std::string::npos)
		throw std::runtime_error("operation_id must be 32 lowercase hexadecimal characters");
	return id;
}

// Probe the same OS lock as Python. A leftover metadata file after a crash is
// not proof of ownership. Return -1 when ownership cannot be verified.
int LeaseHeld(agi::fs::path const& path) {
#ifdef _WIN32
	auto file = CreateFileW(path.c_str(), GENERIC_READ | GENERIC_WRITE,
		FILE_SHARE_READ | FILE_SHARE_WRITE | FILE_SHARE_DELETE, nullptr, OPEN_EXISTING, 0, nullptr);
	if (file == INVALID_HANDLE_VALUE) return GetLastError() == ERROR_FILE_NOT_FOUND ? 0 : -1;
	OVERLAPPED overlapped{};
	bool acquired = LockFileEx(file, LOCKFILE_EXCLUSIVE_LOCK | LOCKFILE_FAIL_IMMEDIATELY, 0, 1, 0, &overlapped);
	auto error = GetLastError();
	if (acquired) UnlockFileEx(file, 0, 1, 0, &overlapped);
	CloseHandle(file);
	return acquired ? 0 : error == ERROR_LOCK_VIOLATION ? 1 : -1;
#else
	auto file = open(path.c_str(), O_RDWR);
	if (file < 0) return errno == ENOENT ? 0 : -1;
	bool acquired = flock(file, LOCK_EX | LOCK_NB) == 0;
	auto error = errno;
	if (acquired) flock(file, LOCK_UN);
	close(file);
	return acquired ? 0 : (error == EWOULDBLOCK || error == EAGAIN) ? 1 : -1;
#endif
}

agi::fs::path Registry() {
	wxString root;
#ifdef _WIN32
	if (!wxGetEnv("LOCALAPPDATA", &root)) throw std::runtime_error("LOCALAPPDATA is unavailable");
	return agi::fs::path(root.ToStdWstring()) / "Aegisub" / "mcp-sessions";
#elif defined(__WXMAC__)
	return agi::fs::path(wxGetHomeDir().ToStdWstring()) / "Library/Application Support/Aegisub/mcp-sessions";
#else
	return agi::fs::path(wxGetHomeDir().ToStdWstring()) / ".local/share/aegisub/mcp-sessions";
#endif
}

std::string Secret(size_t bytes) {
	std::random_device random;
	std::string out;
	for (size_t i = 0; i < bytes; ++i) {
		auto value = random() & 255;
		out += "0123456789abcdef"[value >> 4];
		out += "0123456789abcdef"[value & 15];
	}
	return out;
}

void WriteJson(agi::fs::path const& path, json::Object const& value) {
	auto temp = path;
	temp += ".tmp";
	std::ofstream output(temp, std::ios::binary | std::ios::trunc);
	agi::JsonWriter::Write(value, output);
	output.close();
	if (!output) throw std::runtime_error("Could not write agent session state");
	// agi::fs::Rename replaces the destination atomically on Windows as well.
	agi::fs::Rename(temp, path);
}

int64_t Integer(json::UnknownElement const& value, int64_t low, int64_t high) {
	int64_t n = value;
	if (n < low || n > high) throw std::runtime_error("Integer outside allowed range");
	return n;
}

std::string String(json::UnknownElement const& value, bool metadata = false) {
	std::string s = value;
	if (s.size() > 65536 || s.find_first_of(std::string("\r\n\0", 3)) != std::string::npos)
		throw std::runtime_error("Invalid string: use ASS \\N for line breaks; maximum 65536 bytes");
	if (metadata && s.find(',') != std::string::npos)
		throw std::runtime_error("ASS metadata must not contain commas");
	return s;
}

void Keys(json::Object const& args, std::initializer_list<std::string> allowed) {
	for (auto const& [key, value] : args)
		if (std::find(allowed.begin(), allowed.end(), key) == allowed.end())
			throw std::runtime_error("Unknown argument: " + key);
}
double Number(json::UnknownElement const& value, double low, double high) {
	double n;
	try { n = static_cast<json::Double const&>(value); }
	catch (json::Exception const&) { n = static_cast<json::Integer const&>(value); }
	if (!std::isfinite(n) || n < low || n > high) throw std::runtime_error("Number outside allowed range");
	return n;
}

std::string StyleName(json::UnknownElement const& value) {
	auto name = String(value, true);
	if (name.empty() || name.size() > 256 ||
		wxString::FromUTF8(name).Trim().Trim(false) != wxString::FromUTF8(name))
		throw std::runtime_error("Style/font names must be nonempty, trimmed, and at most 256 UTF-8 bytes");
	return name;
}

agi::Color Color(json::UnknownElement const& value) {
	auto color = String(value);
	if (color.size() != 10 || color.substr(0, 2) != "&H" ||
		color.find_first_not_of("0123456789abcdefABCDEF", 2) != std::string::npos)
		throw std::runtime_error("Colors must use ASS &HAABBGGRR (00 alpha is opaque)");
	return agi::Color(color);
}

json::Object Style(AssStyle const& style) {
	json::Object out;
	out["name"] = style.name;
	out["ass"] = style.GetEntryData();
	out["font"] = style.font;
	out["font_size"] = style.fontsize;
	out["scale_x"] = style.scalex;
	out["scale_y"] = style.scaley;
	out["spacing"] = style.spacing;
	out["angle"] = style.angle;
	out["outline_width"] = style.outline_w;
	out["shadow_distance"] = style.shadow_w;
	out["border_style"] = style.borderstyle;
	out["alignment"] = style.alignment;
	out["margin_left"] = style.Margin[0];
	out["margin_right"] = style.Margin[1];
	out["margin_vertical"] = style.Margin[2];
	out["encoding"] = style.encoding;
	out["bold"] = style.bold;
	out["italic"] = style.italic;
	out["underline"] = style.underline;
	out["strikeout"] = style.strikeout;
	out["primary_color"] = style.primary.GetAssStyleFormatted();
	out["secondary_color"] = style.secondary.GetAssStyleFormatted();
	out["outline_color"] = style.outline.GetAssStyleFormatted();
	out["shadow_color"] = style.shadow.GetAssStyleFormatted();
	return out;
}

void PatchStyle(AssStyle& style, json::Object const& patch) {
	Keys(patch, {"font", "font_size", "scale_x", "scale_y", "spacing", "angle", "outline_width", "shadow_distance",
		"bold", "italic", "underline", "strikeout", "border_style", "alignment", "margin_left", "margin_right",
		"margin_vertical", "encoding", "primary_color", "secondary_color", "outline_color", "shadow_color"});
	for (auto const& [key, value] : patch) {
		if (key == "font") style.font = StyleName(value);
		else if (key == "font_size") style.fontsize = Number(value, 1, 1000);
		else if (key == "scale_x") style.scalex = Number(value, 0, 1000);
		else if (key == "scale_y") style.scaley = Number(value, 0, 1000);
		else if (key == "spacing") style.spacing = Number(value, -1000, 1000);
		else if (key == "angle") style.angle = Number(value, -3600, 3600);
		else if (key == "outline_width") style.outline_w = Number(value, 0, 1000);
		else if (key == "shadow_distance") style.shadow_w = Number(value, 0, 1000);
		else if (key == "bold") style.bold = static_cast<json::Boolean const&>(value);
		else if (key == "italic") style.italic = static_cast<json::Boolean const&>(value);
		else if (key == "underline") style.underline = static_cast<json::Boolean const&>(value);
		else if (key == "strikeout") style.strikeout = static_cast<json::Boolean const&>(value);
		else if (key == "border_style") style.borderstyle = int(Integer(value, 1, 4));
		else if (key == "alignment") style.alignment = int(Integer(value, 1, 9));
		else if (key == "margin_left") style.Margin[0] = int(Integer(value, 0, 9999));
		else if (key == "margin_right") style.Margin[1] = int(Integer(value, 0, 9999));
		else if (key == "margin_vertical") style.Margin[2] = int(Integer(value, 0, 9999));
		else if (key == "encoding") style.encoding = int(Integer(value, 0, 255));
		else if (key == "primary_color") style.primary = Color(value);
		else if (key == "secondary_color") style.secondary = Color(value);
		else if (key == "outline_color") style.outline = Color(value);
		else if (key == "shadow_color") style.shadow = Color(value);
	}
	if (style.borderstyle != 1 && style.borderstyle != 3 && style.borderstyle != 4)
		throw std::runtime_error("border_style must be 1, 3, or 4");
	style.UpdateData();
}

json::Object Line(AssDialogue const& line, int row, Selection const& selection) {
	json::Object out;
	out["id"] = line.Id;
	out["row"] = row;
	out["text"] = line.Text.get();
	out["start_ms"] = int(line.Start);
	out["end_ms"] = int(line.End);
	out["style"] = line.Style.get();
	out["actor"] = line.Actor.get();
	out["effect"] = line.Effect.get();
	out["layer"] = line.Layer;
	out["comment"] = line.Comment;
	out["selected"] = selection.count(const_cast<AssDialogue *>(&line)) != 0;
	return out;
}

void Patch(AssDialogueBase& line, json::Object const& patch, AssFile *ass) {
	Keys(patch, {"id", "text", "start_ms", "end_ms", "style", "actor", "effect", "layer", "comment"});
	for (auto const& [key, value] : patch) {
		if (key == "text") line.Text = String(value);
		else if (key == "start_ms" || key == "end_ms") {
			auto ms = Integer(value, 0, max_time);
			if (ms % 10) throw std::runtime_error("Timestamps must be multiples of 10 (ASS precision)");
			if (key == "start_ms") line.Start = int(ms);
			else line.End = int(ms);
		}
		else if (key == "style") {
			auto name = String(value, true);
			if (!ass->GetStyle(name)) throw std::runtime_error("Unknown style: " + name);
			line.Style = name;
		}
		else if (key == "actor") line.Actor = String(value, true);
		else if (key == "effect") line.Effect = String(value, true);
		else if (key == "layer") line.Layer = int(Integer(value, 0, 2147483647));
		else if (key == "comment") line.Comment = static_cast<json::Boolean const&>(value);
	}
	if (int(line.End) < int(line.Start)) throw std::runtime_error("end_ms must be >= start_ms");
}
}

McpBridge::McpBridge(agi::Context *context) : context(context) {
	window_id = Secret(16);
	BindConnection(context->ass->AddCommitListener(&McpBridge::Changed, this));
	BindConnection(context->subsController->AddFileOpenListener(&McpBridge::Changed, this));
	BindConnection(context->subsController->AddFileSaveListener(&McpBridge::Changed, this));
	BindConnection(context->selectionController->AddSelectionListener(&McpBridge::ContextChanged, this));
	BindConnection(context->selectionController->AddActiveLineListener([this](AssDialogue *) { ContextChanged(); }));
	BindConnection(context->videoController->AddSeekListener([this](int) { ContextChanged(); }));
	BindConnection(context->project->AddVideoProviderListener([this](AsyncVideoProvider *) { ContextChanged(); }));
	BindConnection(context->project->AddAudioProviderListener([this](agi::AudioProvider *) { ContextChanged(); }));
	BindConnection(context->project->AddKeyframesListener([this](std::vector<int> const&) { ContextChanged(); }));
	BindConnection(context->project->AddTimecodesListener([this](agi::vfr::Framerate const&) { ContextChanged(); }));
	BindConnection(context->audioController->AddPlaybackPositionListener([this](int) { ContextChanged(); }));
	BindConnection(context->audioController->AddPlaybackStopListener(&McpBridge::ContextChanged, this));
	// Additional windows in the same process must never consume this mailbox.
	static bool claimed = false;
	if (claimed) return;
	wxString dir, secret, writable;
	if (!wxGetEnv("AEGISUB_MCP_DIR", &dir) || !wxGetEnv("AEGISUB_MCP_TOKEN", &secret)) return;
	if (secret.length() < 32) return;
	directory = agi::fs::path(dir.ToStdWstring());
	std::error_code ec;
	if (!directory.is_absolute() || !std::filesystem::is_directory(directory, ec)) { directory.clear(); return; }
	claimed = true;
	token = secret.ToStdString();
	allow_write = wxGetEnv("AEGISUB_MCP_WRITE", &writable) && writable == "1";
	wxUnsetEnv("AEGISUB_MCP_DIR");
	wxUnsetEnv("AEGISUB_MCP_TOKEN");
	wxUnsetEnv("AEGISUB_MCP_WRITE");
	StartSession();
}

void McpBridge::StartSession() {
	json::Object session;
	session["protocol_version"] = 3;
	session["session_id"] = directory.filename().string();
	session["pid"] = int64_t(wxGetProcessId());
	session["token"] = token;
	session["write_enabled"] = allow_write;
	WriteJson(directory / "session.json", session);
	context_dirty = true;
	PublishContext();
	Start(50);
	context->frame->StatusTimeout(allow_write ? "Agent connection enabled (editing allowed)" : "Agent connection enabled (read only)");
}

void McpBridge::Enable(bool writable) {
	// Rotate credentials and directory when changing permissions. Old clients lose access.
	Disable();
	auto registry = Registry();
	std::filesystem::create_directories(registry);
#ifndef _WIN32
	std::filesystem::permissions(registry, std::filesystem::perms::owner_all);
#endif
	directory = registry / Secret(16);
	std::filesystem::create_directory(directory);
#ifndef _WIN32
	std::filesystem::permissions(directory, std::filesystem::perms::owner_all);
#endif
	owns_directory = true;
	token = Secret(32);
	allow_write = writable;
	StartSession();
}

void McpBridge::Disable() {
	Stop();
	if (!directory.empty()) {
		std::error_code ec;
		// Only remove bridge-owned files. Never recursively delete a user-supplied directory.
		for (auto name : {"session.json", "context.json", "request.json", "session.json.tmp", "context.json.tmp"})
			std::filesystem::remove(directory / name, ec);
		if (owns_directory) std::filesystem::remove(directory, ec);
	}
	directory.clear();
	token.clear();
}

void McpBridge::ReleaseClient() {
	if (!IsActive()) return;
	// A new mailbox revokes the old client's credentials and isolates any
	// outstanding requests/OS lease. The GUI and its document stay untouched.
	Enable(allow_write);
	ContextChanged();
	PublishContext();
	context->frame->StatusTimeout("Agent disconnected. Another agent can connect to this window.");
}

McpBridge::~McpBridge() { Disable(); }
void McpBridge::Changed() {
	++revision;
	for (auto& [id, batch] : batches) {
		if (batch.status == "prepared") {
			batch.status = "stale";
			batch.staged.reset();
		}
	}
	ContextChanged();
}
void McpBridge::ContextChanged() { ++context_version; context_dirty = true; }

json::Object McpBridge::Context() {
	auto result = Dispatch("get_document", {});
	result["context_version"] = context_version;
	result["session_id"] = directory.filename().string();
	result["video_file"] = context->project->VideoName().string();
	result["audio_file"] = context->project->AudioName().string();
	result["video_loaded"] = !!context->project->VideoProvider();
	result["audio_loaded"] = !!context->project->AudioProvider();
	result["video_playing"] = context->videoController->IsPlaying();
	result["audio_playing"] = context->audioController->IsPlaying();
	result["audio_position_ms"] = context->audioController->GetPlaybackPosition();
	if (auto active = context->selectionController->GetActiveLine()) {
		int row = 0;
		for (auto const& line : context->ass->Events) {
			++row;
			if (&line == active) {
				result["active_line"] = Line(line, row, context->selectionController->GetSelectedSet());
				break;
			}
		}
	}
	if (auto provider = context->project->VideoProvider()) {
		auto frame = context->videoController->GetFrameN();
		result["frame"] = frame;
		result["time_ms"] = context->videoController->TimeAtFrame(frame);
		result["video_width"] = provider->GetWidth();
		result["video_height"] = provider->GetHeight();
		result["frame_count"] = provider->GetFrameCount();
		auto const& keys = context->project->Keyframes();
		auto next = std::lower_bound(keys.begin(), keys.end(), frame);
		result["next_keyframe"] = next == keys.end() ? json::UnknownElement(json::Null()) : json::UnknownElement(*next);
		result["previous_keyframe"] = next == keys.begin() ? json::UnknownElement(json::Null()) : json::UnknownElement(*std::prev(next));
	}
	if (auto audio = context->project->AudioProvider()) {
		result["audio_duration_ms"] = audio->GetNumSamples() * 1000 / audio->GetSampleRate();
		result["audio_sample_rate"] = audio->GetSampleRate();
	}
	return result;
}

void McpBridge::PublishContext() {
	if (directory.empty() || !context_dirty) return;
	try { WriteJson(directory / "context.json", Context()); context_dirty = false; }
	catch (...) { /* A transient read lock must not disrupt the GUI. Retry next tick. */ }
}

json::Object McpBridge::AgentStatus() {
	json::Object out;
	out["window_id"] = window_id;
	out["session_id"] = directory.filename().string();
	out["access"] = !IsActive() ? "disabled" : allow_write ? "editing" : "read_only";
	out["filename"] = context->subsController->Filename().string();
	out["modified"] = context->subsController->IsModified();
	out["revision"] = revision;
	out["native_ready"] = IsActive() && context->frame->IsEnabled();
	out["last_command"] = last_command;
	out["last_error"] = last_error;
	out["last_success_ms"] = last_success_ms;
	out["tool_availability"] = "unknown (managed by the agent client)";
	auto held = directory.empty() ? 0 : LeaseHeld(directory / "client.lock");
	out["ownership"] = held < 0 ? "unknown" : held ? "connected" : "available";
	out["client_name"] = held ? "Unknown client" : "None";
	out["client_version"] = "";
	if (held == 1) {
		try {
			auto path = directory / "client.json";
			if (std::filesystem::is_symlink(path) || std::filesystem::file_size(path) > 4096)
				throw std::runtime_error("Invalid client metadata");
			std::ifstream input(path, std::ios::binary);
			auto data = agi::json_util::parse(input);
			json::Object const& client = data;
			out["client_name"] = String(client.at("name"));
			out["client_version"] = String(client.at("version"));
			out["client_pid"] = Integer(client.at("pid"), 1, INT64_MAX);
		} catch (...) { /* Identity is optional; never infer it from a stale file. */ }
	}
	json::Array pending;
	for (auto const& [id, batch] : batches) {
		if (batch.status == "prepared") {
			json::Object item;
			item["operation_id"] = id;
			item["label"] = batch.label;
			pending.emplace_back(std::move(item));
		}
	}
	out["pending_batches"] = std::move(pending);
	return out;
}

void McpBridge::Notify() {
	// Modal tools can hold pointers into the document. Do not change it under them.
	if (busy || !context->frame->IsEnabled()) return;
	std::error_code ec;
	if (!std::filesystem::is_directory(directory, ec)) { Stop(); return; }
	auto new_video_playing = context->videoController->IsPlaying();
	auto new_audio_playing = context->audioController->IsPlaying();
	if (new_video_playing != video_playing || new_audio_playing != audio_playing) {
		video_playing = new_video_playing;
		audio_playing = new_audio_playing;
		ContextChanged();
	}
	PublishContext();
	auto request = directory / "request.json";
	if (!std::filesystem::is_regular_file(request, ec)) return;
	busy = true;
	struct Reset { bool& flag; ~Reset() { flag = false; } } reset{busy};
	json::Object response;
	std::string id;
	try {
		if (std::filesystem::file_size(request) > max_message) throw std::runtime_error("Request too large");
		std::ifstream input(request, std::ios::binary);
		auto parsed = agi::json_util::parse(input);
		input.close();
		std::filesystem::remove(request);
		json::Object const& envelope = parsed;
		id = static_cast<json::String const&>(envelope.at("id"));
		if (id.size() != 32 || id.find_first_not_of("0123456789abcdef") != std::string::npos)
			throw std::runtime_error("Invalid request ID");
		if (static_cast<json::String const&>(envelope.at("token")) != token)
			throw std::runtime_error("Invalid session token");
		auto now = std::chrono::duration_cast<std::chrono::milliseconds>(
			std::chrono::system_clock::now().time_since_epoch()).count();
		if (Integer(envelope.at("expires_ms"), 0, INT64_MAX) < now)
			throw std::runtime_error("Request expired; no changes applied");
		std::string method = envelope.at("method");
		// Status reads must not hide the last meaningful command or its failure.
		if (method != "get_agent_status") last_command = method;
		response["result"] = Dispatch(method, envelope.at("args"));
		if (method != "get_agent_status") { last_success_ms = Now(); last_error.clear(); }
	}
	catch (std::exception const& e) { last_error = e.what(); response["error"] = last_error; }
	catch (...) { response["error"] = "Aegisub bridge failure"; }
	std::filesystem::remove(request, ec);
	// Never interpolate unvalidated input into a path, even on the error path.
	if (id.size() != 32 || id.find_first_not_of("0123456789abcdef") != std::string::npos) return;
	try {
		response["id"] = id;
		auto temp = directory / (id + ".tmp");
		std::ofstream output(temp, std::ios::binary | std::ios::trunc);
		agi::JsonWriter::Write(response, output);
		if (!output) throw std::runtime_error("Could not write bridge response");
		output.close();
		std::filesystem::rename(temp, directory / (id + ".json"));
	}
	catch (...) { Stop(); } // Fail closed after transport loss; never retry a mutation.
}

json::Object McpBridge::BatchCommand(std::string const& method, json::Object const& args) {
	auto id = OperationId(args.at("operation_id"));
	if (method == "propose_batch") Keys(args, {"operation_id", "expected_revision", "label", "operations"});
	else if (method == "apply_batch") Keys(args, {"operation_id", "expected_revision"});
	else Keys(args, {"operation_id"});
	auto found = batches.find(id);
	if (method != "get_operation" && !allow_write) throw std::runtime_error("Read-only session");
	if (method == "propose_batch" && found != batches.end()) {
		if (found->second.request != Serialize(args)) throw std::runtime_error("operation_id already used for a different proposal");
	}
	else if (method == "propose_batch") {
		if (Integer(args.at("expected_revision"), 1, INT64_MAX) != revision)
			throw std::runtime_error("Document revision conflict; rebuild the proposal");
		if (batches.size() >= 128) throw std::runtime_error("Window operation history is full (128); no receipts are evicted. Use a new window.");
		size_t pending = 0;
		for (auto const& [key, value] : batches) if (value.staged) ++pending;
		if (pending >= 4) throw std::runtime_error("Cancel or apply an existing proposal first (maximum 4 pending)");
		Batch batch;
		batch.base_revision = revision;
		batch.label = String(args.at("label"));
		if (batch.label.empty() || batch.label.size() > 128) throw std::runtime_error("Label must be 1 to 128 UTF-8 bytes");
		batch.request = Serialize(args);
		if (batch.request.size() > 1024 * 1024) throw std::runtime_error("Proposal exceeds 1 MiB; use a smaller batch");
		batch.staged = std::make_unique<AssFile>(*context->ass);
		auto& staged = *batch.staged;
		// AssFile's normal copy assigns new IDs. A proposal must retain live IDs.
		auto original = context->ass->Events.begin();
		for (auto& line : staged.Events) { line.Id = original->Id; ++original; }
		staged.Properties = context->ass->Properties;
		json::Array const& operations = args.at("operations");
		if (operations.empty() || operations.size() > 50) throw std::runtime_error("Supply 1 to 50 operations");
		size_t total = 0;
		std::set<int> touched;
		std::set<std::string> styles;
		json::Array created_ids;
		for (auto const& operation : operations) {
			json::Object const& op = operation;
			std::string kind = op.at("method");
			auto lookup = [&](json::UnknownElement const& value) -> AssDialogue * {
				int key = int(Integer(value, 0, 2147483647));
				for (auto& line : staged.Events) if (line.Id == key) return &line;
				throw std::runtime_error("Unknown subtitle ID in proposal");
			};
			auto count = [&](size_t size) {
				if (!size || (total += size) > 500) throw std::runtime_error("Batch must contain 1 to 500 total edits");
			};
			if (kind == "create_style" || kind == "update_style") {
				if (kind == "create_style") Keys(op, {"method", "name", "properties", "base_style"});
				else Keys(op, {"method", "name", "properties"});
				count(1);
				auto name = StyleName(op.at("name"));
				json::Object const& properties = op.at("properties");
				auto current = staged.GetStyle(name);
				if (kind == "create_style") {
					for (auto const& style : staged.Styles)
						if (wxString::FromUTF8(style.name).CmpNoCase(wxString::FromUTF8(name)) == 0)
							throw std::runtime_error("Style name already exists (case-insensitive)");
					std::unique_ptr<AssStyle> copy;
					if (op.count("base_style")) {
						auto base = staged.GetStyle(StyleName(op.at("base_style")));
						if (!base) throw std::runtime_error("Unknown base style");
						copy = std::make_unique<AssStyle>(*base);
					} else copy = std::make_unique<AssStyle>();
					copy->name = name;
					PatchStyle(*copy, properties);
					staged.Styles.push_back(*copy.release());
				} else {
					if (!current || properties.empty()) throw std::runtime_error("Unknown style or empty style update");
					PatchStyle(*current, properties);
				}
				styles.insert(name);
			}
			else if (kind == "create_lines") {
				Keys(op, {"method", "lines", "after_id"});
				json::Array const& lines = op.at("lines");
				count(lines.size());
				auto position = staged.Events.end();
				if (op.count("after_id")) position = ++staged.iterator_to(*lookup(op.at("after_id")));
				for (auto const& value : lines) {
					json::Object const& patch = value;
					Keys(patch, {"text", "start_ms", "end_ms", "style", "actor", "effect", "layer", "comment"});
					String(patch.at("text"));
					Integer(patch.at("start_ms"), 0, max_time);
					Integer(patch.at("end_ms"), 0, max_time);
					auto line = std::make_unique<AssDialogue>();
					Patch(*line, patch, &staged);
					if (!staged.GetStyle(line->Style.get())) throw std::runtime_error("Unknown style");
					touched.insert(line->Id);
					created_ids.emplace_back(line->Id);
					staged.Events.insert(position, *line.release());
				}
			}
			else if (kind == "update_lines") {
				Keys(op, {"method", "updates"});
				json::Array const& updates = op.at("updates");
				count(updates.size());
				std::set<int> seen;
				for (auto const& update : updates) {
					json::Object const& patch = update;
					auto line = lookup(patch.at("id"));
					if (patch.size() < 2 || !seen.insert(line->Id).second) throw std::runtime_error("Empty update or duplicate subtitle ID");
					Patch(*line, patch, &staged);
					touched.insert(line->Id);
				}
			}
			else if (kind == "delete_lines" || kind == "shift_times") {
				if (kind == "delete_lines") Keys(op, {"method", "ids"});
				else Keys(op, {"method", "ids", "delta_ms"});
				json::Array const& ids = op.at("ids");
				count(ids.size());
				std::set<int> seen;
				for (auto const& value : ids) {
					auto line = lookup(value);
					if (!seen.insert(line->Id).second) throw std::runtime_error("Duplicate subtitle ID");
					touched.insert(line->Id);
					if (kind == "delete_lines") {
						staged.Events.erase(staged.iterator_to(*line));
						delete line;
					} else {
						auto delta = Integer(op.at("delta_ms"), -max_time, max_time);
						if (delta % 10 || int(line->Start) + delta < 0 || int(line->End) + delta < 0 ||
							int(line->Start) + delta > max_time || int(line->End) + delta > max_time)
							throw std::runtime_error("Invalid shift or timestamp outside ASS range");
						line->Start = int(int(line->Start) + delta);
						line->End = int(int(line->End) + delta);
					}
				}
			}
			else throw std::runtime_error("Unsupported batch operation; use subtitle/style edits only");
		}
		if (staged.Events.empty()) {
			auto blank = new AssDialogue;
			staged.Events.push_back(*blank);
			touched.insert(blank->Id);
			created_ids.emplace_back(blank->Id);
		}
		json::Array changes;
		for (auto key : touched) {
			json::Object change;
			change["id"] = key;
			change["before"] = json::Null();
			change["after"] = json::Null();
			int row = 0;
			for (auto const& line : context->ass->Events) {
				++row;
				if (line.Id == key) { change["before"] = Line(line, row, {}); break; }
			}
			row = 0;
			for (auto const& line : staged.Events) {
				++row;
				if (line.Id == key) { change["after"] = Line(line, row, {}); break; }
			}
			changes.emplace_back(std::move(change));
		}
		json::Array style_changes;
		for (auto const& name : styles) {
			json::Object change;
			change["name"] = name;
			auto before = context->ass->GetStyle(name);
			change["before"] = before ? json::UnknownElement(Style(*before)) : json::UnknownElement(json::Null());
			change["after"] = Style(*staged.GetStyle(name));
			style_changes.emplace_back(std::move(change));
		}
		batch.receipt["changes"] = std::move(changes);
		batch.receipt["style_changes"] = std::move(style_changes);
		batch.receipt["created_ids"] = std::move(created_ids);
		batch.receipt["base_revision"] = revision;
		batch.receipt["operation_id"] = id;
		batch.receipt["window_id"] = window_id;
		batch.receipt["label"] = batch.label;
		// Keep recovery history bounded without evicting IDs and risking replay.
		size_t retained = Serialize(batch.receipt).size() + batch.request.size();
		for (auto const& [key, previous] : batches) retained += Serialize(previous.receipt).size() + previous.request.size();
		if (retained > 16 * 1024 * 1024) throw std::runtime_error("Window operation history exceeds 16 MiB; use a new window");
		found = batches.emplace(id, std::move(batch)).first;
	}
	if (found == batches.end()) {
		if (method != "get_operation") throw std::runtime_error("Unknown operation; never recreate an uncertain applied operation automatically");
		json::Object unknown;
		unknown["operation_id"] = id;
		unknown["window_id"] = window_id;
		unknown["status"] = "unknown";
		return unknown;
	}
	auto& batch = found->second;
	if (method == "cancel_batch" && batch.status == "prepared") {
		batch.status = "cancelled";
		batch.staged.reset();
	}
	if (method == "apply_batch" && batch.status != "applied") {
		if (batch.status != "prepared" || !batch.staged || batch.base_revision != revision ||
			Integer(args.at("expected_revision"), 1, INT64_MAX) != revision)
			throw std::runtime_error("Proposal is cancelled or stale; document revision conflict; rebuild and review it");
		// Allocate the receipt and remapped selection before the no-throw list swaps.
		batch.receipt["applied_revision"] = revision + 1;
		Selection selection;
		AssDialogue *active = nullptr;
		std::set<int> selected;
		for (auto line : context->selectionController->GetSelectedSet()) selected.insert(line->Id);
		auto old_active = context->selectionController->GetActiveLine();
		for (auto& line : batch.staged->Events) {
			if (selected.count(line.Id)) selection.insert(&line);
			if (old_active && old_active->Id == line.Id) active = &line;
		}
		if (!active) active = &batch.staged->Events.front();
		selection.insert(active);
		// Mark first so commit listeners cannot invalidate or destroy this batch.
		batch.status = "applied";
		context->ass->Events.swap(batch.staged->Events);
		context->ass->Styles.swap(batch.staged->Styles);
		context->ass->Commit(wxString::FromUTF8("MCP: " + batch.label), AssFile::COMMIT_NEW);
		context->selectionController->SetSelectionAndActive(std::move(selection), active);
		batch.staged.reset(); // old pointers stay alive through all commit listeners
		batch.receipt["applied_revision"] = revision;
	}
	auto result = batch.receipt;
	result["status"] = batch.status;
	result["revision"] = revision;
	result["receipt_scope"] = "this native window lifetime; survives client handoff, not application exit";
	return result;
}

json::Object McpBridge::Dispatch(std::string const& method, json::Object const& args) {
	json::Object result;
	auto const& events = context->ass->Events;
	if (method == "get_agent_status") {
		Keys(args, {});
		return AgentStatus();
	}
	if (method == "propose_batch" || method == "apply_batch" || method == "cancel_batch" || method == "get_operation")
		return BatchCommand(method, args);
	if (method == "get_context") {
		Keys(args, {});
		return Context();
	}
	else if (method == "render_preview") {
		Keys(args, {"time_ms", "max_width", "max_height", "raw", "operation_id"});
		AssFile *subtitles = context->ass.get();
		if (args.count("operation_id")) {
			auto found = batches.find(OperationId(args.at("operation_id")));
			if (found == batches.end() || found->second.status != "prepared" || !found->second.staged)
				throw std::runtime_error("Proposal is unavailable or stale");
			subtitles = found->second.staged.get();
			result["operation_id"] = OperationId(args.at("operation_id"));
		}
		auto provider = context->project->VideoProvider();
		if (!provider) throw std::runtime_error("Open a video or dummy video in this window before rendering a preview");
		auto frame = context->videoController->GetFrameN();
		if (args.count("time_ms")) {
			auto time = int(Integer(args.at("time_ms"), 0, max_time));
			if (time > context->videoController->TimeAtFrame(provider->GetFrameCount() - 1, agi::vfr::END))
				throw std::runtime_error("Preview time is beyond the video duration");
			frame = std::clamp(context->videoController->FrameAtTime(time), 0, provider->GetFrameCount() - 1);
		}
		auto time = context->videoController->TimeAtFrame(frame);
		auto width = Integer(args.at("max_width"), 64, 1920);
		auto height = Integer(args.at("max_height"), 64, 1080);
		bool raw = args.at("raw");
		// Restore the live renderer even if proposed-frame rendering fails.
		struct Restore { AsyncVideoProvider *provider; AssFile *live; ~Restore() { provider->LoadSubtitles(live); } } restore{provider, context->ass.get()};
		provider->LoadSubtitles(subtitles);
		auto image = GetImage(*provider->GetFrame(frame, time, raw));
		double scale = std::min({1.0, double(width) / image.GetWidth(), double(height) / image.GetHeight()});
		if (scale < 1.0) image.Rescale(std::max(1, int(image.GetWidth() * scale)), std::max(1, int(image.GetHeight() * scale)), wxIMAGE_QUALITY_HIGH);
		wxMemoryOutputStream stream;
		if (!image.SaveFile(stream, wxBITMAP_TYPE_PNG)) throw std::runtime_error("PNG preview encoding failed");
		std::vector<unsigned char> png(stream.GetSize());
		stream.CopyTo(png.data(), png.size());
		result["png_base64"] = wxBase64Encode(png.data(), png.size()).ToStdString();
		result["width"] = image.GetWidth();
		result["height"] = image.GetHeight();
		result["frame"] = frame;
		result["time_ms"] = time;
	}
	else if (method == "get_document") {
		Keys(args, {});
		result["filename"] = context->subsController->Filename().string();
		result["window_id"] = window_id;
		result["modified"] = context->subsController->IsModified();
		result["line_count"] = int64_t(std::distance(events.begin(), events.end()));
		result["write_enabled"] = allow_write;
		int width, height;
		context->ass->GetResolution(width, height);
		result["script_width"] = width;
		result["script_height"] = height;
		result["can_undo"] = !context->subsController->IsUndoStackEmpty();
		result["can_redo"] = !context->subsController->IsRedoStackEmpty();
		auto active = context->selectionController->GetActiveLine();
		if (active) result["active_id"] = active->Id;
		else result["active_id"] = json::Null();
		json::Array selected;
		for (auto line : context->selectionController->GetSortedSelection()) selected.emplace_back(line->Id);
		result["selected_ids"] = std::move(selected);
	}
	else if (method == "list_lines") {
		Keys(args, {"offset", "limit", "selected_only"});
		auto offset = Integer(args.at("offset"), 0, 2147483647);
		auto limit = Integer(args.at("limit"), 1, 500);
		bool selected_only = args.at("selected_only");
		auto const& selection = context->selectionController->GetSelectedSet();
		json::Array lines;
		int row = 0;
		int64_t count = 0;
		for (auto const& line : events) {
			++row;
			if (selected_only && !selection.count(const_cast<AssDialogue *>(&line))) continue;
			if (count >= offset && int64_t(lines.size()) < limit) lines.emplace_back(Line(line, row, selection));
			++count;
		}
		result["lines"] = std::move(lines);
		result["total"] = count;
		if (offset + limit < count) result["next_offset"] = offset + limit;
		else result["next_offset"] = json::Null();
	}
	else if (method == "list_styles") {
		Keys(args, {});
		json::Array styles;
		for (auto const& style : context->ass->Styles) styles.emplace_back(Style(style));
		result["styles"] = std::move(styles);
	}
	else {
		if (!allow_write) throw std::runtime_error("Read-only session; launch with --allow-write to edit");
		if (Integer(args.at("expected_revision"), 1, INT64_MAX) != revision)
			throw std::runtime_error("Document revision conflict; read the document again before editing");
		// Live context is published during playback. Build the ID lookup only for
		// mutation requests, rather than allocating a document-sized map each tick.
		std::map<int, AssDialogue *> by_id;
		for (auto& line : context->ass->Events) by_id.emplace(line.Id, &line);
		auto lookup = [&](json::UnknownElement const& value) {
			int id = int(Integer(value, 0, 2147483647));
			auto it = by_id.find(id);
			if (it == by_id.end()) throw std::runtime_error("Unknown subtitle ID; read the document again");
			return it->second;
		};
		if (method == "seek_video") {
			Keys(args, {"expected_revision", "time_ms"});
			auto time = int(Integer(args.at("time_ms"), 0, max_time));
			auto provider = context->project->VideoProvider();
			if (!provider) throw std::runtime_error("No video is loaded");
			if (time > context->videoController->TimeAtFrame(provider->GetFrameCount() - 1, agi::vfr::END))
				throw std::runtime_error("Seek time is beyond the video duration");
			context->videoController->Stop();
			context->videoController->JumpToTime(time);
			ContextChanged();
			return Context();
		}
		else if (method == "play_audio") {
			Keys(args, {"expected_revision", "start_ms", "end_ms"});
			auto start = int(Integer(args.at("start_ms"), 0, max_time));
			auto end = int(Integer(args.at("end_ms"), 0, max_time));
			auto audio = context->project->AudioProvider();
			if (!audio) throw std::runtime_error("No audio is loaded");
			if (end <= start || end - start > 30000 || end > audio->GetNumSamples() * 1000 / audio->GetSampleRate())
				throw std::runtime_error("Audio range must be within the loaded audio and at most 30 seconds");
			context->audioController->PlayRange(TimeRange(start, end));
			ContextChanged();
			return Context();
		}
		else if (method == "stop_playback") {
			Keys(args, {"expected_revision"});
			context->videoController->Stop();
			context->audioController->Stop();
			ContextChanged();
			return Context();
		}
		else if (method == "create_lines") {
			Keys(args, {"expected_revision", "lines", "after_id"});
			json::Array const& lines = args.at("lines");
			if (lines.empty() || lines.size() > 500) throw std::runtime_error("Supply 1 to 500 lines");
			auto position = context->ass->Events.end();
			if (args.count("after_id")) position = ++context->ass->iterator_to(*lookup(args.at("after_id")));
			std::vector<std::unique_ptr<AssDialogue>> staged;
			json::Array ids;
			Selection selection;
			for (auto const& item : lines) {
				json::Object const& patch = item;
				Keys(patch, {"text", "start_ms", "end_ms", "style", "actor", "effect", "layer", "comment"});
				// Require explicit content and timing even if a caller bypasses Python validation.
				String(patch.at("text"));
				Integer(patch.at("start_ms"), 0, max_time);
				Integer(patch.at("end_ms"), 0, max_time);
				auto line = std::make_unique<AssDialogue>();
				Patch(*line, patch, context->ass.get());
				if (!context->ass->GetStyle(line->Style.get())) throw std::runtime_error("Unknown style");
				ids.emplace_back(line->Id);
				selection.insert(line.get());
				staged.emplace_back(std::move(line));
			}
			auto active = staged.front().get();
			for (auto& line : staged) context->ass->Events.insert(position, *line.release());
			context->ass->Commit("MCP: create subtitles", AssFile::COMMIT_DIAG_ADDREM);
			context->selectionController->SetSelectionAndActive(std::move(selection), active);
			result["created_ids"] = std::move(ids);
		}
		else if (method == "delete_lines") {
			Keys(args, {"expected_revision", "ids"});
			json::Array const& ids = args.at("ids");
			if (ids.empty() || ids.size() > 500) throw std::runtime_error("Supply 1 to 500 subtitle IDs");
			Selection removed;
			for (auto const& id : ids)
				if (!removed.insert(lookup(id)).second) throw std::runtime_error("Duplicate subtitle ID");
			// Aegisub requires a nonempty document. Allocate its replacement before changing anything.
			std::unique_ptr<AssDialogue> blank;
			if (removed.size() == by_id.size()) blank = std::make_unique<AssDialogue>();
			std::vector<std::unique_ptr<AssDialogue>> retired;
			retired.reserve(removed.size());
			// Keep removed objects alive until the grid and active-line listeners finish updating.
			context->ass->Events.remove_and_dispose_if([&](AssDialogue const& line) {
				return removed.count(const_cast<AssDialogue *>(&line));
			}, [&](AssDialogue *line) { retired.emplace_back(line); });
			if (blank) {
				result["replacement_id"] = blank->Id;
				context->ass->Events.push_back(*blank.release());
			}
			else result["replacement_id"] = json::Null();
			auto active = context->selectionController->GetActiveLine();
			if (!active || removed.count(active)) active = &context->ass->Events.front();
			auto selection = context->selectionController->GetSelectedSet();
			for (auto line : removed) selection.erase(line);
			selection.insert(active);
			context->ass->Commit("MCP: delete subtitles", AssFile::COMMIT_DIAG_ADDREM);
			context->selectionController->SetSelectionAndActive(std::move(selection), active);
			result["deleted"] = int64_t(removed.size());
		}
		else if (method == "create_style" || method == "update_style") {
			bool create = method == "create_style";
			if (create) Keys(args, {"expected_revision", "name", "properties", "base_style"});
			else Keys(args, {"expected_revision", "name", "properties"});
			auto name = StyleName(args.at("name"));
			json::Object const& properties = args.at("properties");
			auto current = context->ass->GetStyle(name);
			std::unique_ptr<AssStyle> staged;
			if (create) {
				for (auto const& existing : context->ass->Styles)
					if (wxString::FromUTF8(existing.name).CmpNoCase(wxString::FromUTF8(name)) == 0)
						throw std::runtime_error("Style name already exists (case-insensitive)");
				if (args.count("base_style")) {
					auto base = context->ass->GetStyle(StyleName(args.at("base_style")));
					if (!base) throw std::runtime_error("Unknown base style");
					staged = std::make_unique<AssStyle>(*base);
				}
				else staged = std::make_unique<AssStyle>();
				staged->name = name;
			}
			else {
				if (!current) throw std::runtime_error("Unknown style");
				if (properties.empty()) throw std::runtime_error("Supply at least one style property");
				staged = std::make_unique<AssStyle>(*current);
			}
			PatchStyle(*staged, properties);
			result["style"] = Style(*staged);
			if (create) context->ass->Styles.push_back(*staged.release());
			else *current = *staged;
			context->ass->Commit(create ? "MCP: create style" : "MCP: update style", AssFile::COMMIT_STYLES);
		}
		else if (method == "update_lines") {
			Keys(args, {"expected_revision", "updates"});
			json::Array const& updates = args.at("updates");
			if (updates.empty() || updates.size() > 500) throw std::runtime_error("Supply 1 to 500 updates");
			std::map<AssDialogue *, AssDialogueBase> staged;
			for (auto const& update : updates) {
				json::Object const& patch = update;
				auto line = lookup(patch.at("id"));
				if (!staged.emplace(line, *line).second) throw std::runtime_error("Duplicate subtitle ID");
				Patch(staged.at(line), patch, context->ass.get());
			}
			// Validation and flyweight allocation finish before any document mutation.
			for (auto const& [line, replacement] : staged) static_cast<AssDialogueBase&>(*line) = replacement;
			context->ass->Commit("MCP: update subtitles", AssFile::COMMIT_DIAG_FULL);
			result["updated"] = int64_t(staged.size());
		}
		else if (method == "shift_times" || method == "select_lines") {
			if (method == "shift_times") Keys(args, {"expected_revision", "ids", "delta_ms"});
			else Keys(args, {"expected_revision", "ids", "active_id"});
			json::Array const& ids = args.at("ids");
			if (ids.empty() || ids.size() > 500) throw std::runtime_error("Supply 1 to 500 subtitle IDs");
			Selection selection;
			for (auto const& id : ids)
				if (!selection.insert(lookup(id)).second) throw std::runtime_error("Duplicate subtitle ID");
			if (method == "select_lines") {
				auto active = lookup(args.at("active_id"));
				if (!selection.count(active)) throw std::runtime_error("active_id must be selected");
				context->selectionController->SetSelectionAndActive(std::move(selection), active);
			}
			else {
				auto delta = Integer(args.at("delta_ms"), -max_time, max_time);
				if (delta % 10) throw std::runtime_error("delta_ms must be a multiple of 10 (ASS precision)");
				for (auto line : selection)
					if (int(line->Start) + delta < 0 || int(line->End) + delta > max_time ||
						int(line->End) + delta < 0 || int(line->Start) + delta > max_time)
						throw std::runtime_error("Shift would move a timestamp outside ASS range");
				for (auto line : selection) {
					line->Start = int(int(line->Start) + delta);
					line->End = int(int(line->End) + delta);
				}
				context->ass->Commit("MCP: shift subtitle timing", AssFile::COMMIT_DIAG_TIME);
				result["updated"] = int64_t(selection.size());
			}
		}
		else if (method == "undo" || method == "redo") {
			Keys(args, {"expected_revision"});
			if (method == "undo") {
				if (context->subsController->IsUndoStackEmpty()) throw std::runtime_error("Nothing to undo");
				context->subsController->Undo();
			}
			else {
				if (context->subsController->IsRedoStackEmpty()) throw std::runtime_error("Nothing to redo");
				context->subsController->Redo();
			}
		}
		else throw std::runtime_error("Unknown bridge method");
	}
	result["revision"] = revision;
	return result;
}
