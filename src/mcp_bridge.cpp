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
#include <cmath>
#include <fstream>
#include <map>
#include <memory>
#include <set>
#include <stdexcept>
#include <vector>
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
		if (method == "create_lines") {
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
