// Copyright (c) 2026, Aegisub MCP contributors
// SPDX-License-Identifier: BSD-3-Clause
#pragma once

#include <libaegisub/json.h>
#include <libaegisub/signal.h>
#include <wx/timer.h>

namespace agi { struct Context; }

// The launcher supplies an ephemeral, private directory and a session token.
// All dispatch runs on the GUI thread; no network listener is created.
class McpBridge final : public wxTimer, private agi::signal::ConnectionScope {
	agi::Context *context;
	agi::fs::path directory;
	std::string token;
	int64_t revision = 1;
	bool allow_write = false;
	bool busy = false;
	bool owns_directory = false;
	int64_t context_version = 1;
	bool context_dirty = true;
	bool video_playing = false;
	bool audio_playing = false;
	void ContextChanged();
	void PublishContext();
	void StartSession();
	json::Object Context();
	void Changed();
	void Notify() override;
	json::Object Dispatch(std::string const& method, json::Object const& args);
public:
	explicit McpBridge(agi::Context *context);
	~McpBridge() override;
	void Enable(bool writable);
	void Disable();
	bool IsActive() const { return IsRunning(); }
	bool IsWritable() const { return allow_write; }
};
