// Copyright (c) 2026, Aegisub MCP contributors
// SPDX-License-Identifier: BSD-3-Clause
#pragma once
#include <wx/dialog.h>
#include <wx/timer.h>

namespace agi { struct Context; }
class wxTextCtrl;
class wxButton;

class DialogAgent final : public wxDialog {
	agi::Context *context;
	wxTimer timer;
	wxTextCtrl *status;
	wxButton *release;
	void RefreshStatus();
public:
	explicit DialogAgent(agi::Context *context);
};
