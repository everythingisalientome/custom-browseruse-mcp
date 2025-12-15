"""
Web Automation MCP – Playwright-complete tool surface
NO Playwright dependency
LLM-driven orchestration
"""

from typing import Optional, List, Dict, Any
from bs4 import BeautifulSoup
from mcp.server.fastmcp import FastMCP

# ======================================================
# Browser Adapter (from scratch – replace later)
# ======================================================

class BrowserAdapter:
    """
    Abstract layer for browser adapter.
    Replace implementation with CDP / WebDriver / embedded browser later.
    """

    async def launch(self, browser_type: str): ...
    async def close(self): ...
    async def navigate(self, url: str): ...
    async def reload(self): ...
    async def go_back(self): ...
    async def go_forward(self): ...

    async def get_page_html(self) -> str: ...
    async def get_page_title(self) -> str: ...
    async def get_current_url(self) -> str: ...

    async def find_by_xpath(self, xpath: str) -> bool: ...
    async def click(self, xpath: str): ...
    async def double_click(self, xpath: str): ...
    async def right_click(self, xpath: str): ...
    async def hover(self, xpath: str): ...
    async def type(self, xpath: str, value: str): ...
    async def press_key(self, key: str): ...
    async def scroll(self, x: int, y: int): ...
    async def drag_and_drop(self, source_xpath: str, target_xpath: str): ...

    async def is_visible(self, xpath: str) -> bool: ...
    async def is_enabled(self, xpath: str) -> bool: ...
    async def get_text(self, xpath: str) -> str: ...
    async def get_value(self, xpath: str) -> str: ...
    async def get_attribute(self, xpath: str, attr: str) -> str: ...

    async def wait_for_timeout(self, ms: int): ...
    async def wait_for_element(self, xpath: str, timeout_ms: int): ...
    async def wait_for_navigation(self): ...
    async def wait_for_network_idle(self): ...

    async def screenshot(self) -> bytes: ...
    async def highlight(self, xpath: str): ...


browser = BrowserAdapter()

# ======================================================
# MCP Server
# ======================================================

app = FastMCP("web-automation-mcp")

# ======================================================
# Helpers
# ======================================================

def error(code: str, message: str):
    return {
        "status": "ERROR",
        "error_code": code,
        "message": message
    }

def ok(**kwargs):
    return {
        "status": "OK",
        **kwargs
    }

def build_xpath(tag):
    path = []
    while tag and tag.name != "[document]":
        siblings = tag.find_previous_siblings(tag.name)
        index = len(siblings) + 1
        path.insert(0, f"{tag.name}[{index}]")
        tag = tag.parent
    return "/" + "/".join(path)

# ======================================================
# 🔍 Element Discovery Tool
# ======================================================

@app.tool()
async def find_element(fieldName: str, action: str):
    """
    Finds element xpath using:
    1. Full page HTML
    2. Basic DOM search
    3. Signals LLM if ambiguous
    """

    html = await browser.get_page_html()
    soup = BeautifulSoup(html, "html.parser")

    candidates = []

    for tag in soup.find_all(["input", "button", "textarea", "select", "a"]):
        text = " ".join([
            tag.get("id", ""),
            tag.get("name", ""),
            tag.get("placeholder", ""),
            tag.get("aria-label", ""),
            tag.get_text(strip=True)
        ]).lower()

        if fieldName.lower() in text:
            candidates.append(tag)

    if len(candidates) == 1:
        return ok(
            xpath=build_xpath(candidates[0]),
            source="basic_dom_search"
        )

    return {
        "status": "NEEDS_LLM",
        "fieldName": fieldName,
        "action": action,
        "candidates": [
            {
                "tag": c.name,
                "attributes": dict(c.attrs),
                "text": c.get_text(strip=True)
            } for c in candidates
        ]
    }

# ======================================================
# 🧭 Browser / Page Tools
# ======================================================

@app.tool()
async def launch_application(browser_type: str):
    await browser.launch(browser_type)
    return ok()

@app.tool()
async def close_application():
    await browser.close()
    return ok()

@app.tool()
async def navigate(url: str):
    await browser.navigate(url)
    return ok()

@app.tool()
async def reload():
    await browser.reload()
    return ok()

@app.tool()
async def go_back():
    await browser.go_back()
    return ok()

@app.tool()
async def go_forward():
    await browser.go_forward()
    return ok()

@app.tool()
async def get_page_title():
    return ok(title=await browser.get_page_title())

@app.tool()
async def get_current_url():
    return ok(url=await browser.get_current_url())

@app.tool()
async def get_page_html():
    return ok(html=await browser.get_page_html())

# ======================================================
# 🖱️ Mouse Tools
# ======================================================

@app.tool()
async def click(xpath: str):
    if not await browser.find_by_xpath(xpath):
        return error("ELEMENT_NOT_FOUND", "Click target not found")
    await browser.click(xpath)
    return ok()

@app.tool()
async def double_click(xpath: str):
    if not await browser.find_by_xpath(xpath):
        return error("ELEMENT_NOT_FOUND", "Double click target not found")
    await browser.double_click(xpath)
    return ok()

@app.tool()
async def right_click(xpath: str):
    if not await browser.find_by_xpath(xpath):
        return error("ELEMENT_NOT_FOUND", "Right click target not found")
    await browser.right_click(xpath)
    return ok()

@app.tool()
async def hover(xpath: str):
    if not await browser.find_by_xpath(xpath):
        return error("ELEMENT_NOT_FOUND", "Hover target not found")
    await browser.hover(xpath)
    return ok()

@app.tool()
async def scroll(x: int, y: int):
    await browser.scroll(x, y)
    return ok()

@app.tool()
async def drag_and_drop(source_xpath: str, target_xpath: str):
    if not await browser.find_by_xpath(source_xpath):
        return error("ELEMENT_NOT_FOUND", "Drag source not found")
    if not await browser.find_by_xpath(target_xpath):
        return error("ELEMENT_NOT_FOUND", "Drop target not found")
    await browser.drag_and_drop(source_xpath, target_xpath)
    return ok()

# ======================================================
# ⌨️ Keyboard Tools
# ======================================================

@app.tool()
async def type_into(xpath: str, value: str):
    if not await browser.find_by_xpath(xpath):
        return error("ELEMENT_NOT_FOUND", "Type target not found")
    await browser.type(xpath, value)
    return ok()

@app.tool()
async def press_key(key: str):
    await browser.press_key(key)
    return ok()

# ======================================================
# ⏳ Wait / Sync Tools
# ======================================================

@app.tool()
async def wait_for_element(xpath: str, timeout_ms: int = 5000):
    await browser.wait_for_element(xpath, timeout_ms)
    return ok()

@app.tool()
async def wait_for_timeout(ms: int):
    await browser.wait_for_timeout(ms)
    return ok()

@app.tool()
async def wait_for_navigation():
    await browser.wait_for_navigation()
    return ok()

@app.tool()
async def wait_for_network_idle():
    await browser.wait_for_network_idle()
    return ok()

# ======================================================
# 🧱 Element State Tools
# ======================================================

@app.tool()
async def is_visible(xpath: str):
    return ok(visible=await browser.is_visible(xpath))

@app.tool()
async def is_enabled(xpath: str):
    return ok(enabled=await browser.is_enabled(xpath))

@app.tool()
async def get_text(xpath: str):
    return ok(text=await browser.get_text(xpath))

@app.tool()
async def get_value(xpath: str):
    return ok(value=await browser.get_value(xpath))

@app.tool()
async def get_attribute(xpath: str, attribute: str):
    return ok(value=await browser.get_attribute(xpath, attribute))

# ======================================================
# 🖼️ Debug / Evidence
# ======================================================

@app.tool()
async def screenshot():
    return ok(image=await browser.screenshot())

@app.tool()
async def highlight_element(xpath: str):
    await browser.highlight(xpath)
    return ok()