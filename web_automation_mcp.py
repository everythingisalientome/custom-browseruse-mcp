from mcp.server.fastmcp import FastMCP
import json
from cdp_client import ChromeCDP, DEFAULT_TIMEOUT
import base64

app = FastMCP("web-automation-mcp")
cdp = ChromeCDP()

def ok(**k): return {"status": "OK", **k}
def err(code, msg): return {"status": "ERROR", "error_code": code, "message": msg}

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
async def click(xpath: str):
    try:
        cdp.click(xpath)
        return ok()
    except TimeoutError:
        return err("ELEMENT_NOT_FOUND", xpath)
    except Exception as e:
        return err("CLICK_FAILED", str(e))

@app.tool()
async def type_into(xpath: str, value: str):
    try:
        cdp.fill(xpath, value)
        return ok()
    except TimeoutError:
        return err("ELEMENT_NOT_FOUND", xpath)

@app.tool()
async def hover(xpath: str):
    try:
        cdp.hover(xpath)
        return ok()
    except TimeoutError:
        return err("ELEMENT_NOT_FOUND", xpath)

@app.tool()
async def press_key(key: str):
    cdp.press_key(key)
    return ok()

@app.tool()
async def send_keys(keys: str, xpath: str = None):
    """
    Send special keys or shortcuts (e.g. 'Enter', 'Tab', 'Ctrl+A').
    If xpath is provided, focuses that element before sending.
    """
    try:
        cdp.send_keys(keys, xpath)
        return {"status": "OK"}
    except Exception as e:
        return err("KEY_ERROR", str(e))

@app.tool()
async def double_click(xpath: str):
    """
    Double-click an element. Useful for selecting text or special UI actions.
    """
    try:
        cdp.double_click(xpath)
        return ok()
    except Exception as e:
        return err("DOUBLE_CLICK_FAILED", str(e))

@app.tool()
async def drag_and_drop(source_xpath: str, target_xpath: str):
    """
    Drag an element from source_xpath and drop it at target_xpath.
    """
    try:
        cdp.drag_and_drop(source_xpath, target_xpath)
        return ok()
    except Exception as e:
        return err("DRAG_FAILED", str(e))

@app.tool()
async def type_like_human(xpath: str, value: str):
    """
    Types text character-by-character into the field.
    
    IMPORTANT:
    - This tool DOES NOT clear the field first. It appends to existing text.
    - Use this for "Type-ahead" fields, appending text, or when 'type_into' fails.
    - If you need to clear the field first, use 'send_keys' with Ctrl+A -> Backspace.
    """
    try:
        cdp.type_human(xpath, value)
        return ok()
    except Exception as e:
        return err("HUMAN_TYPE_FAILED", str(e))

# ---------------- Discovery tool ----------------

@app.tool()
async def find_element(fieldName: str):
    """
    Smart Search: Finds visible elements (buttons, inputs, links) where text/id/name 
    matches the search query (fieldName).
    """
    try:
        # Delegate the heavy lifting to the client
        matches = cdp.find_elements_by_text(fieldName)
        
        if len(matches) == 1:
            return ok(xpath=matches[0]["xpath"])
            
        if not matches:
            return err("NOT_FOUND", f"No visible element found matching '{fieldName}'")
            
        # Ambiguous Matches (Let LLM decide)
        return {
            "status": "NEEDS_LLM",
            "message": f"Found {len(matches)} candidates for '{fieldName}'. Please select one.",
            "candidates": matches[:10] 
        }
    except Exception as e:
        return err("SEARCH_FAILED", str(e))

@app.tool()
async def get_interactive_elements(tag_name: str = "button"):
    """
    Discovery Tool: Returns a list of ALL visible elements of a specific type.
    tag_name options: 'button', 'input', 'a', 'select', 'textarea'
    """
    try:
        # Delegate to client
        items = cdp.get_all_interactive_elements(tag_name)
        return ok(count=len(items), elements=items[:50])
    except Exception as e:
        return err("DISCOVERY_FAILED", str(e))

# ---------------- Wait tools ----------------
@app.tool()
async def wait_for_element(xpath: str, timeout_ms: int = DEFAULT_TIMEOUT):
    try:
        cdp.wait_for_element(xpath, timeout_ms)
        return ok()
    except TimeoutError as e:
        return err("TIMEOUT", str(e))

@app.tool()
async def wait_for_network_idle(timeout_ms: int = DEFAULT_TIMEOUT):
    try:
        cdp.wait_for_network_idle(timeout_ms)
        return ok()
    except TimeoutError as e:
        return err("TIMEOUT", str(e))
    
@app.tool()
async def wait_for_text(text: str, timeout_ms: int = DEFAULT_TIMEOUT):
    try:
        cdp.wait_for_text(text, timeout_ms)
        return {"status": "OK"}
    except TimeoutError as e:
        return {
            "status": "ERROR",
            "error_code": "TIMEOUT",
            "message": str(e)
        }

@app.tool()
async def scroll_to_element(xpath: str):
    try:
        if cdp.scroll_into_view(xpath):
            return {"status": "OK"}
        return {"status": "ERROR", "error_code": "NOT_FOUND"}
    except Exception as e:
        return {"status": "ERROR", "message": str(e)}


@app.tool()
async def screenshot(full_page: bool = True):
    """
    Take a screenshot of the current page.
    Returns base64 PNG.
    """
    try:
        img = cdp.screenshot(full_page=full_page)
        return {
            "status": "OK",
            "image_base64": base64.b64encode(img).decode("utf-8")
        }
    except Exception as e:
        return {
            "status": "ERROR",
            "error_code": "SCREENSHOT_FAILED",
            "message": str(e)
        }


@app.tool()
async def is_checked(xpath: str):
    return {
        "status": "OK",
        "checked": cdp.is_checked(xpath)
    }

@app.tool()
async def is_selected(xpath: str):
    return {
        "status": "OK",
        "selected": cdp.is_selected(xpath)
    }


@app.tool()
async def select_dropdown(
    xpath: str,
    value: str | None = None,
    label: str | None = None,
    index: int | None = None
):
    try:
        cdp.select_option(xpath, value=value, label=label, index=index)
        return {"status": "OK"}
    except Exception as e:
        return {
            "status": "ERROR",
            "error_code": "SELECT_FAILED",
            "message": str(e)
        }

@app.tool()
async def multi_select_dropdown(xpath: str, values: list[str]):
    try:
        cdp.multi_select(xpath, values)
        return {"status": "OK"}
    except Exception as e:
        return {
            "status": "ERROR",
            "error_code": "MULTI_SELECT_FAILED",
            "message": str(e)
        }

@app.tool()
async def select_custom_dropdown(trigger_xpath: str, option_text: str):
    """
    Selects an item from a modern UI dropdown (React/Vue/Angular/MUI).
    Use this when standard 'select_dropdown' fails.
    
    Args:
        trigger_xpath: The XPath of the input/div you click to open the list.
        option_text: The visible text of the option you want to choose.
    """
    try:
        cdp.select_custom_option(trigger_xpath, option_text)
        return ok()
    except Exception as e:
        return err("CUSTOM_SELECT_FAILED", str(e))
    
@app.tool()
async def select_autocomplete(input_xpath: str, select_text: str):
    """
    Selects from a 'Type-to-Filter' dropdown.
    1. Focuses the input (input_xpath).
    2. Types 'select_text' character by character.
    3. Clicks 'select_text' as soon as it appears in the list.
    """
    try:
        cdp.select_autocomplete_option(input_xpath, select_text)
        return ok()
    except Exception as e:
        return err("AUTOCOMPLETE_FAILED", str(e))
    

# ---------------- Extraction tools ----------------
@app.tool()
async def get_text(xpath: str):
    """
    Get the visible text or value from any element (label, input, div, span, etc).
    Use this to read data from the screen.
    """
    try:
        text = cdp.get_text(xpath)
        return ok(text=text)
    except Exception as e:
        return err("GET_TEXT_FAILED", str(e))

@app.tool()
async def get_table_data(
    table_xpath: str, 
    next_page_xpath: str = None, 
    max_pages: int = 0,
    total_pages_xpath: str = None
):
    """
    Extract data from a table (with optional pagination).
    
    Args:
        table_xpath: XPath to the <table> (or container).
        next_page_xpath: (Optional) XPath to the 'Next' button.
        max_pages: (Optional) Exact number of pages to scrape (e.g., 5).
        total_pages_xpath: (Optional) XPath to a label like "Page 1 of 10". 
                           Use this to automatically determine how many pages to scrape.
    """
    try:
        data = cdp.scrape_table(table_xpath, next_page_xpath, max_pages, total_pages_xpath)
        return ok(count=len(data), data=data)
    except Exception as e:
        return err("TABLE_SCRAPE_FAILED", str(e))