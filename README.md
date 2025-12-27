Robust Web Automation MCP Server
An MCP (Model Context Protocol) server that enables LLMs (like Claude or Gemini) to interact with the web reliably.

Unlike standard Selenium/Playwright wrappers, this project uses raw Chrome DevTools Protocol (CDP) with a custom "Object ID" pattern. This makes it immune to common automation failures like sticky headers, animations, and hidden mobile menus.

🚀 Key Features
Anti-Flake Architecture: Resolves elements to stable Chrome Object IDs instead of fragile XPaths. Interactions survive DOM updates (e.g., animations or layout shifts).

Smart Visibility Logic: Automatically ignores hidden or "mobile-view" elements when running on desktop, preventing "Element not visible" errors.

Live Discovery: Tools like find_element run JavaScript in the browser to find elements by text, ID, or attributes in real-time.

Native Events: Uses synthetic events + physical mouse simulation to guarantee clicks and hovers register on complex React/Vue apps.

Trace Management: Built-in tracing to debug steps, screenshots, and errors.

🛠️ Project Structure
web_automation_mcp.py: The MCP server definition. Exposes tools (click, Maps, find_element) to the LLM.

cdp_client.py: The heavy-lifting driver. Manages the WebSocket connection to Chrome, handles geometry calculations, and executes CDP commands.

tracemanager.py: Utilities for logging execution steps and capturing artifacts (screenshots/DOM) on failure.

cleanup_profiles.py: A utility script to wipe old Chrome user profile folders from your temp directory.

📦 Prerequisites
Python 3.10+

Google Chrome or Microsoft Edge installed on the machine.

Requirements:

Plaintext

mcp
requests
websocket-client
python-dotenv
beautifulsoup4  # (Optional, depending on your final cleaning)
⚡ Quick Start
Install Dependencies:

Bash

pip install -r requirements.txt
Configuration (.env): Create a .env file (optional) to control behavior:

Ini, TOML

WEB_MCP_TRACE=1                 # Enable tracing
WEB_MCP_SCREENSHOT_ON_FAIL=1    # Save screenshots on error
Run the Server:

Bash

# Using the MCP CLI
mcp dev web_automation_mcp.py
🤖 Available Tools
The server exposes these tools to the LLM:

Navigation: launch_application(url), Maps(url), close_application

Interaction:

click(xpath): Robust click using stable IDs.

type_into(xpath, value): Focuses, clears, and types text.

hover(xpath), double_click(xpath), drag_and_drop(source, target).

Discovery (The "Eyes"):

find_element(fieldName): Finds a visible element by fuzzy matching text/ID/name.

get_interactive_elements(tag_name): Returns a list of all visible elements of a certain type (e.g., all buttons) to help the agent orient itself.

State: get_page_html, screenshot, is_checked, wait_for_text.

⚠️ Troubleshooting
"Handshake status 500": This usually means Chrome didn't start fast enough or a "Zombie" Chrome process is blocking port 9222. Kill all chrome.exe processes and try again.

"Element not visible": The tool automatically attempts to find the visible version of an element. If this persists, ensure force_viewport in cdp_client.py is set to 1920x1080.


⚠️ LICENSE NOTICE

This project is NOT open source.

- No usage rights are granted by default
- Personal use requires prior written permission
- Enterprise or commercial use is strictly prohibited
- Downloading or forking does NOT grant a license

See LICENSE, and RESPONSIBLE_USE.md for details.

Ideas Help: 
1. https://www.youtube.com/watch?v=ftUDZwlkbxg
2. https://chromedevtools.github.io/devtools-protocol/