from mcp.server.fastmcp import FastMCP
import json
from cdp_client import ChromeCDP, DEFAULT_TIMEOUT
import base64

app = FastMCP("web-automation-mcp")
cdp = ChromeCDP()

def ok(**k): return {"status": "OK", **k}
def err(code, msg): return {"status": "ERROR", "error_code": code, "message": msg}

# ---------------- Helper Logic ----------------

async def _attempt_fallback(label: str, role: str):
    """
    Python-side logic to find an element by Label/Role.
    Useful for retrieving the 'Healed' XPath to update your CSV.
    """
    if not label: return None
    
    # 1. Use the "Smart Scanner" from CDP
    candidates = cdp.find_elements_by_text(label)
    if not candidates: return None

    # 2. Filter by Role (Python Logic)
    role = role.lower() if role else ""
    best_match = None
    
    for c in candidates:
        tag = c["tag"]
        attrs = c.get("attributes", {})
        c_role = (attrs.get("role") or "").lower()
        c_type = (attrs.get("type") or "").lower()
        c_class = (attrs.get("class") or "").lower()
        
        is_match = False
        
        # Heuristic Matching
        if "button" in role:
            if tag == "button" or "btn" in c_class or c_type in ["button", "submit"] or c_role == "button":
                is_match = True
        elif "text" in role or "edit" in role:
            if tag in ["textarea", "input"] and c_type not in ["checkbox", "radio", "button", "submit"]:
                is_match = True
        elif "link" in role:
            if tag == "a" or c_role == "link":
                is_match = True
        elif "combo" in role or "select" in role:
            if tag == "select" or c_role == "combobox":
                is_match = True
        elif "check" in role:
            if c_type == "checkbox" or c_role == "checkbox":
                is_match = True
        elif "radio" in role:
             if c_type == "radio" or c_role == "radio":
                is_match = True
        else:
            # If no specific role requested, accept the text match
            is_match = True
            
        if is_match:
            best_match = c
            break # Take the first good match
            
    if best_match:
        return best_match["xpath"]
    return None

# ---------------- Browser tools ----------------

@app.tool()
async def launch_application(url: str):
    cdp.launch()
    cdp.navigate(url)
    return ok()

@app.tool()
async def close_application():
    cdp.close()
    return ok()

@app.tool()
async def get_page_html():
    return ok(html=cdp.get_html())

@app.tool()
async def navigate(url: str):
    """
    Navigate to a URL without closing the browser.
    """
    try:
        cdp.navigate(url)
        return ok()
    except Exception as e:
        return err("NAVIGATION_FAILED", str(e))


# ---------------- Mouse and keyboard tools ----------------

@app.tool()
async def click(xpath: str = None, label: str = None, role: str = None):
    """
    Clicks an element.
    Args:
        xpath: The strict XPath (Preferred).
        label: Visual text of the button (Fallback).
        role: Type of element e.g. 'button', 'link' (Fallback).
    """
    try:
        # We pass all 3 to CDP. The 'Smart Driver' handles the fallback internally.
        cdp.click(xpath, label, role)
        return ok(message=f"Clicked {xpath or label}")
    except Exception as e:
        return err("CLICK_FAILED", str(e))

@app.tool()
async def type_into(value: str, xpath: str = None, label: str = None, role: str = None):
    """
    Types text into an input field.
    Args:
        value: The text to type.
        xpath: The strict XPath (Preferred).
        label: Visual label of the input (Fallback).
        role: Type of element e.g. 'editable text' (Fallback).
    """
    try:
        cdp.fill(xpath, value, label, role)
        return ok(message=f"Typed '{value}' into {xpath or label}")
    except Exception as e:
        return err("TYPE_FAILED", str(e))

@app.tool()
async def send_keys(keys: str, xpath: str = None):
    """
    Sends special keys (Enter, Tab, etc.).
    """
    try:
        cdp.send_keys(keys, xpath)
        return ok(message=f"Sent keys: {keys}")
    except Exception as e:
        return err("KEY_FAILED", str(e))

@app.tool()
async def get_text(xpath: str):
    """Gets the visible text of an element."""
    try:
        text = cdp.get_text(xpath)
        return ok(data={"text": text})
    except Exception as e:
        return err("GET_TEXT_FAILED", str(e))

@app.tool()
async def find_element(query: str):
    """
    Finds elements by visible text or attribute.
    Returns a list of potential matches with XPaths.
    """
    try:
        elements = cdp.find_elements_by_text(query)
        return ok(data=elements)
    except Exception as e:
        return err("FIND_FAILED", str(e))

@app.tool()
async def find_smart_locator(label: str, role: str):
    """
    Diagnostic Tool: Returns the active XPath that matches the Label and Role.
    Use this to 'Heal' your CSV by finding the new XPath for a broken field.
    """
    try:
        xpath = await _attempt_fallback(label, role)
        if xpath:
            return ok(data=xpath, message=f"Found match: {xpath}")
        return err("NOT_FOUND", f"No match found for Label='{label}', Role='{role}'")
    except Exception as e:
        return err("SMART_FIND_FAILED", str(e))

@app.tool()
async def discover_interactive_elements(tag_name: str = "button"):
    """
    Returns a list of all interactive elements of a specific type.
    Useful for exploring a new page.
    """
    try:
        elements = cdp.get_all_interactive_elements(tag_name)
        return ok(data=elements)
    except Exception as e:
        return err("DISCOVERY_FAILED", str(e))

@app.tool()
async def scrape_table(table_xpath: str, next_page_xpath: str = None, max_pages: int = 0, total_pages_xpath: str = None):
    """
    Scrapes a table across multiple pages.
    """
    try:
        data = cdp.scrape_table(table_xpath, next_page_xpath, max_pages, total_pages_xpath)
        return ok(data=data, message=f"Scraped {len(data)} rows")
    except Exception as e:
        return err("SCRAPE_FAILED", str(e))

@app.tool()
async def hover(xpath: str = None, label: str = None, role: str = None):
    """Hovers over an element."""
    try:
        cdp.hover(xpath, label, role)
        return ok(message="Hovered successfully")
    except Exception as e:
        return err("HOVER_FAILED", str(e))

@app.tool()
async def double_click(xpath: str = None, label: str = None, role: str = None):
    """Double clicks an element."""
    try:
        cdp.double_click(xpath, label, role)
        return ok(message="Double-clicked successfully")
    except Exception as e:
        return err("DOUBLE_CLICK_FAILED", str(e))

@app.tool()
async def select_option(xpath: str, value: str = None, label: str = None):
    """Selects an option from a dropdown."""
    try:
        cdp.select_option(xpath, value=value, label=label)
        return ok(message="Option selected")
    except Exception as e:
        return err("SELECT_FAILED", str(e))

# --- NEW TOOLS FOR TABS & FRAMES ---

@app.tool()
async def switch_tab(keyword: str = None, new_tab: bool = False):
    """
    Switches the browser focus to a different tab.
    Args:
        keyword: (Optional) A word to look for in the tab's Title or URL.
        new_tab: (Optional) If True, switches to the NEWEST tab (index -1).
    """
    try:
        if new_tab:
            cdp.switch_to_tab(index=-1)
        elif keyword:
            cdp.switch_to_tab(keyword=keyword)
        else:
            return err("INVALID_ARGS", "Must provide either 'keyword' or 'new_tab=True'")
        return ok(message="Switched tab successfully")
    except Exception as e:
        return err("SWITCH_FAILED", str(e))

@app.tool()
async def get_frames():
    """
    Returns a list of all named iframes on the current page.
    Useful when you cannot find an element that should be there.
    """
    try:
        frames = cdp.get_frames()
        return ok(data=frames)
    except Exception as e:
        return err("GET_FRAMES_FAILED", str(e))

@app.tool()
async def switch_frame(frame_name: str = None):
    """
    Switches the automation context to a specific iframe.
    Pass None to return to the main (top-level) page.
    """
    try:
        cdp.switch_frame(frame_name)
        return ok(message=f"Switched focus to frame: {frame_name if frame_name else 'Top Level'}")
    except Exception as e:
        return err("SWITCH_FRAME_FAILED", str(e))

# Run Server
if __name__ == "__main__":
    app.run()