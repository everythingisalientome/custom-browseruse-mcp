from mcp.server.fastmcp import FastMCP
import json
from cdp_client import ChromeCDP
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
async def send_keys(keys: str):
    """
    Send special or combo keys.
    Examples: 'Enter', 'Tab', 'Ctrl+A', 'Ctrl+Shift+Tab'
    """
    try:
        cdp.send_keys(keys)
        return {"status": "OK"}
    except Exception as e:
        return {
            "status": "ERROR",
            "error_code": "KEY_ERROR",
            "message": str(e)
        }

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
# ---------------- Discovery tool ----------------

@app.tool()
async def find_element(fieldName: str):
    """
    Smart Search: Finds visible elements (buttons, inputs, links) where text/id/name 
    matches the search query (fieldName).
    Returns a valid XPath if 1 match is found, or a list of candidates if ambiguous.
    """
    # JS script to find matches and check visibility in one go
    js_script = f"""
    (function() {{
        const query = {json.dumps(fieldName)}.toLowerCase();
        const candidates = [];
        
        // Tags to search
        const selectors = 'input, button, a, textarea, select, [role="button"]';
        document.querySelectorAll(selectors).forEach(el => {{
            // 1. Check Visibility
            const rect = el.getBoundingClientRect();
            const style = window.getComputedStyle(el);
            if (rect.width === 0 || style.visibility === 'hidden' || style.display === 'none') return;
            
            // 2. Check Match (Text, ID, Name, Placeholder, Aria)
            const text = (el.innerText || '').toLowerCase();
            const val = (el.value || '').toLowerCase();
            const ph = (el.getAttribute('placeholder') || '').toLowerCase();
            const name = (el.getAttribute('name') || '').toLowerCase();
            const id = (el.id || '').toLowerCase();
            const aria = (el.getAttribute('aria-label') || '').toLowerCase();
            
            if (text.includes(query) || val.includes(query) || ph.includes(query) || 
                name.includes(query) || id.includes(query) || aria.includes(query)) {{
                
                // 3. Generate Simple XPath
                let xpath = '';
                if (el.id) {{
                    xpath = `//*[@id='${{el.id}}']`;
                }} else {{
                    // Fallback to a robust text/attribute matcher
                    const tag = el.tagName.toLowerCase();
                    if (el.innerText) {{
                        // Clean text for XPath
                        const cleanText = el.innerText.trim().substring(0, 30).replace(/'/g, "");
                        xpath = `//${{tag}}[contains(normalize-space(.), '${{cleanText}}')]`;
                    }} else if (el.getAttribute('name')) {{
                        xpath = `//${{tag}}[@name='${{el.getAttribute('name')}}']`;
                    }} else if (el.getAttribute('placeholder')) {{
                        xpath = `//${{tag}}[@placeholder='${{el.getAttribute('placeholder')}}']`;
                    }} else {{
                        // Last resort: absolute-ish path (handled by client logic usually)
                        xpath = `//${{tag}}[contains(@class, '${{el.className}}')]`;
                    }}
                }}
                
                candidates.push({{
                    tag: el.tagName,
                    text: el.innerText || el.value || el.getAttribute('aria-label') || '',
                    xpath: xpath,
                    attributes: {{ id: el.id, type: el.type, name: el.name }}
                }});
            }}
        }});
        return candidates;
    }})()
    """
    try:
        msg_id = cdp._send("Runtime.evaluate", {"expression": js_script, "returnByValue": True})
        matches = cdp._recv(msg_id)["result"]["result"]["value"]
        
        # 1. Perfect Match
        if len(matches) == 1:
            return ok(xpath=matches[0]["xpath"])
            
        # 2. No Matches
        if not matches:
            return err("NOT_FOUND", f"No visible element found matching '{fieldName}'")
            
        # 3. Ambiguous Matches (Let LLM decide)
        return {
            "status": "NEEDS_LLM",
            "message": f"Found {len(matches)} candidates for '{fieldName}'. Please select one.",
            "candidates": matches[:10] # Limit to 10 to save tokens
        }
        
    except Exception as e:
        return err("SEARCH_FAILED", str(e))

@app.tool()
async def get_interactive_elements(tag_name: str = "button"):
    """
    Discovery Tool: Returns a list of ALL visible elements of a specific type (button, input, a).
    Useful when you don't know the exact name of an element.
    tag_name options: 'button', 'input', 'a', 'select', 'textarea'
    """
    js_script = f"""
    (function() {{
        const results = [];
        // Handle "button" broadly to include input[type=submit] and role=button
        let selector = '{tag_name}';
        if ('{tag_name}' === 'button') selector = 'button, input[type="button"], input[type="submit"], [role="button"]';
        if ('{tag_name}' === 'input') selector = 'input:not([type="hidden"])';
        
        document.querySelectorAll(selector).forEach(el => {{
            // 1. Visibility Check
            const rect = el.getBoundingClientRect();
            const style = window.getComputedStyle(el);
            if (rect.width === 0 || style.visibility === 'hidden' || style.display === 'none') return;
            
            // 2. Generate XPath
            let xpath = '';
            if (el.id) {{
                xpath = `//*[@id='${{el.id}}']`;
            }} else {{
                const tag = el.tagName.toLowerCase();
                if (el.innerText && el.innerText.trim().length > 0) {{
                    const cleanText = el.innerText.trim().substring(0, 30).replace(/'/g, "");
                    xpath = `//${{tag}}[contains(normalize-space(.), '${{cleanText}}')]`;
                }} else if (el.getAttribute('name')) {{
                    xpath = `//${{tag}}[@name='${{el.getAttribute('name')}}']`;
                }} else if (el.getAttribute('aria-label')) {{
                    xpath = `//${{tag}}[@aria-label='${{el.getAttribute('aria-label')}}']`;
                }}
            }}
            
            if (xpath) {{
                results.push({{
                    tag: el.tagName,
                    text: el.innerText || el.value || el.getAttribute('aria-label') || 'N/A',
                    xpath: xpath,
                    visible: true
                }});
            }}
        }});
        return results;
    }})()
    """
    
    try:
        msg_id = cdp._send("Runtime.evaluate", {"expression": js_script, "returnByValue": True})
        result = cdp._recv(msg_id)
        items = result["result"]["result"]["value"]
        return ok(count=len(items), elements=items[:50]) # Limit to 50 to prevent context overflow
    except Exception as e:
        return err("DISCOVERY_FAILED", str(e))

# ---------------- Wait tools ----------------
@app.tool()
async def wait_for_element(xpath: str, timeout_ms: int = 10000):
    try:
        cdp.wait_for_element(xpath, timeout_ms)
        return ok()
    except TimeoutError as e:
        return err("TIMEOUT", str(e))

@app.tool()
async def wait_for_network_idle(timeout_ms: int = 10000):
    try:
        cdp.wait_for_network_idle(timeout_ms)
        return ok()
    except TimeoutError as e:
        return err("TIMEOUT", str(e))
    
@app.tool()
async def wait_for_text(text: str, timeout_ms: int = 10000):
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
        if cdp._scroll_into_view(xpath):
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
