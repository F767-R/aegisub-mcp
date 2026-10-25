// Copyright (c) 2026, Aegisub MCP contributors
// SPDX-License-Identifier: BSD-3-Clause
#include "dialog_agent.h"
#include "frame_main.h"
#include "mcp_bridge.h"
#include "include/aegisub/context.h"
#include <wx/button.h>
#include <wx/datetime.h>
#include <wx/sizer.h>
#include <wx/textctrl.h>

DialogAgent::DialogAgent(agi::Context *c)
: wxDialog(c->parent, -1, _("Agent connection"), wxDefaultPosition, wxSize(620, 440), wxDEFAULT_DIALOG_STYLE | wxRESIZE_BORDER)
, context(c), timer(this) {
	auto root = new wxBoxSizer(wxVERTICAL);
	status = new wxTextCtrl(this, -1, "", wxDefaultPosition, wxDefaultSize, wxTE_MULTILINE | wxTE_READONLY);
	root->Add(status, 1, wxEXPAND | wxALL, 12);
	auto buttons = new wxBoxSizer(wxHORIZONTAL);
	auto read = new wxButton(this, -1, _("Allow read only"));
	auto write = new wxButton(this, -1, _("Allow editing"));
	release = new wxButton(this, -1, _("Disconnect agent"));
	auto disable = new wxButton(this, -1, _("Disable access"));
	for (auto button : {read, write, release, disable}) buttons->Add(button, 0, wxRIGHT, 6);
	root->Add(buttons, 0, wxLEFT | wxRIGHT | wxBOTTOM, 12);
	root->Add(CreateButtonSizer(wxCLOSE), 0, wxEXPAND | wxALL, 12);
	SetSizer(root);
	SetMinSize(wxSize(620, 360));
	read->Bind(wxEVT_BUTTON, [this](wxCommandEvent&) { context->frame->GetMcpBridge()->Enable(false); RefreshStatus(); });
	write->Bind(wxEVT_BUTTON, [this](wxCommandEvent&) { context->frame->GetMcpBridge()->Enable(true); RefreshStatus(); });
	release->Bind(wxEVT_BUTTON, [this](wxCommandEvent&) { context->frame->GetMcpBridge()->ReleaseClient(); RefreshStatus(); });
	disable->Bind(wxEVT_BUTTON, [this](wxCommandEvent&) { context->frame->GetMcpBridge()->Disable(); RefreshStatus(); });
	Bind(wxEVT_BUTTON, [this](wxCommandEvent&) { Close(); }, wxID_CLOSE);
	Bind(wxEVT_TIMER, [this](wxTimerEvent&) { RefreshStatus(); });
	RefreshStatus();
	timer.Start(500);
}

void DialogAgent::RefreshStatus() {
	auto bridge = context->frame->GetMcpBridge();
	if (!bridge) return; // The parent may already be shutting down.
	auto info = bridge->AgentStatus();
	auto text = [&](char const *key) { return wxString::FromUTF8(static_cast<json::String const&>(info.at(key))); };
	auto access = text("access");
	wxString value = "Document: " + text("filename") + (static_cast<json::Boolean const&>(info.at("modified")) ? " (unsaved changes)" : "");
	value += "\nAccess: " + (access == "editing" ? wxString("Editing allowed") : access == "read_only" ? wxString("Read only") : wxString("Disabled"));
	value += "\nOwner: " + text("client_name") + " " + text("client_version");
	value += "\nConnection: " + text("ownership");
	value += "\nNative window: " + wxString(static_cast<json::Boolean const&>(info.at("native_ready")) ? "Ready" : "Access disabled or waiting for a dialog");
	auto last = static_cast<int64_t>(info.at("last_success_ms"));
	value += "\nLast successful command: " + (last ? wxDateTime(static_cast<time_t>(last / 1000)).FormatISOCombined(' ') : wxString("None yet"));
	value += "\nLast command: " + text("last_command");
	if (!text("last_error").empty()) value += "\nLast error: " + text("last_error");
	json::Array const& pending = info.at("pending_batches");
	value += wxString::Format("\nProposals awaiting application: %zu", pending.size());
	for (auto const& item : pending) {
		json::Object const& batch = item;
		value += "\n  " + wxString::FromUTF8(static_cast<json::String const&>(batch.at("label")));
	}
	value += "\n\nDisconnect releases this window for another agent. Your document and Undo history stay open.";
	value += "\nTool availability inside Codex or Claude must be checked in that client.";
	if (status->GetValue() != value) status->ChangeValue(value);
	release->Enable(bridge->IsActive());
}
