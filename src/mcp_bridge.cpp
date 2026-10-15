// Copyright (c) 2026, Aegisub MCP contributors
// SPDX-License-Identifier: BSD-3-Clause

#include "mcp_bridge.h"

#include "ass_dialogue.h"
#include "ass_file.h"
#include "ass_style.h"
#include "frame_main.h"
#include "include/aegisub/context.h"
#include "selection_controller.h"
#include "subs_controller.h"

#include <libaegisub/cajun/writer.h>

#include <algorithm>
#include <chrono>
#include <fstream>
#include <map>
#include <set>
#include <stdexcept>
#include <wx/utils.h>

namespace {
constexpr int64_t max_time = 35999990; // ASS: 9:59:59.99
constexpr size_t max_message = 4 * 1024 * 1024;

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
	// Additional windows in the same process must never consume this mailbox.
	static bool claimed = false;
	if (claimed) return;
	wxString dir, secret, writable;
	if (!wxGetEnv("AEGISUB_MCP_DIR", &dir) || !wxGetEnv("AEGISUB_MCP_TOKEN", &secret)) return;
	if (secret.length() < 32) return;
	directory = agi::fs::path(dir.ToStdWstring());
	std::error_code ec;
	if (!directory.is_absolute() || !std::filesystem::is_directory(directory, ec)) return;
	claimed = true;
	token = secret.ToStdString();
	allow_write = wxGetEnv("AEGISUB_MCP_WRITE", &writable) && writable == "1";
	wxUnsetEnv("AEGISUB_MCP_DIR");
	wxUnsetEnv("AEGISUB_MCP_TOKEN");
	wxUnsetEnv("AEGISUB_MCP_WRITE");
	BindConnection(context->ass->AddCommitListener(&McpBridge::Changed, this));
	BindConnection(context->subsController->AddFileOpenListener(&McpBridge::Changed, this));
	BindConnection(context->subsController->AddFileSaveListener(&McpBridge::Changed, this));
	Start(50);
	context->frame->StatusTimeout(allow_write ? "MCP connected (editing enabled)" : "MCP connected (read only)");
}

McpBridge::~McpBridge() { Stop(); }
void McpBridge::Changed() { ++revision; }

void McpBridge::Notify() {
	// Modal tools can hold pointers into the document. Do not change it under them.
	if (busy || !context->frame->IsEnabled()) return;
	std::error_code ec;
	if (!std::filesystem::is_directory(directory, ec)) { Stop(); return; }
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
		response["result"] = Dispatch(envelope.at("method"), envelope.at("args"));
	}
	catch (std::exception const& e) { response["error"] = std::string(e.what()); }
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

json::Object McpBridge::Dispatch(std::string const& method, json::Object const& args) {
	json::Object result;
	auto const& events = context->ass->Events;
	std::map<int, AssDialogue *> by_id;
	for (auto& line : context->ass->Events) by_id.emplace(line.Id, &line);
	auto lookup = [&](json::UnknownElement const& value) {
		int id = int(Integer(value, 0, 2147483647));
		auto it = by_id.find(id);
		if (it == by_id.end()) throw std::runtime_error("Unknown subtitle ID; read the document again");
		return it->second;
	};
	if (method == "get_document") {
		Keys(args, {});
		result["filename"] = context->subsController->Filename().string();
		result["modified"] = context->subsController->IsModified();
		result["line_count"] = int64_t(by_id.size());
		result["write_enabled"] = allow_write;
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
		for (auto const& style : context->ass->Styles) {
			json::Object entry;
			entry["name"] = style.name;
			entry["ass"] = style.GetEntryData();
			styles.emplace_back(std::move(entry));
		}
		result["styles"] = std::move(styles);
	}
	else {
		if (!allow_write) throw std::runtime_error("Read-only session; launch with --allow-write to edit");
		if (Integer(args.at("expected_revision"), 1, INT64_MAX) != revision)
			throw std::runtime_error("Document revision conflict; read the document again before editing");
		if (method == "update_lines") {
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
