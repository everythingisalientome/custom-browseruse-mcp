from mcp.server.fastmcp import FastMCP
from bs4 import BeautifulSoup
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


# ---------------- Mouse and keyboard tools ----------------

@app.tool()
async def click(xpath: str):
    try:
        cdp.wait_for_element(xpath)
        cdp.click(xpath)
        return ok()
    except TimeoutError:
        return err("ELEMENT_NOT_FOUND", xpath)

@app.tool()
async def type_into(xpath: str, value: str):
    try:
        cdp.wait_for_element(xpath)
        cdp.fill(xpath, value)
        return ok()
    except TimeoutError:
        return err("ELEMENT_NOT_FOUND", xpath)

@app.tool()
async def hover(xpath: str):
    try:
        cdp.wait_for_element(xpath)
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


# ---------------- Discovery tool ----------------

@app.tool()
async def find_element(fieldName: str):
    """
    Basic DOM search. LLM retries original action if xpath is returned.
    """
    html = cdp.get_html()
    soup = BeautifulSoup(html, "html.parser")

    matches = []

    for tag in soup.find_all(["input", "button", "textarea"]):
        text = " ".join([
            tag.get("id", ""),
            tag.get("name", ""),
            tag.get("placeholder", ""),
            tag.get_text(strip=True)
        ]).lower()

        if fieldName.lower() in text:
            matches.append(tag)

    if len(matches) == 1:
        return ok(xpath=_build_xpath(matches[0]))

    return {
        "status": "NEEDS_LLM",
        "fieldName": fieldName,
        "candidates": [dict(tag.attrs) for tag in matches]
    }

def _build_xpath(tag):
    path = []
    while tag and tag.name != "[document]":
        siblings = tag.find_previous_siblings(tag.name)
        path.insert(0, f"{tag.name}[{len(siblings)+1}]")
        tag = tag.parent
    return "/" + "/".join(path)



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
