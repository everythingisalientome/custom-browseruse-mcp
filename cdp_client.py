from dotenv import load_dotenv
import subprocess
import time
import requests
import json
import itertools
import threading
import os
import signal
import websocket
import tempfile
import socket
import time
import base64

from tracemanager import TraceManager

# Load environment variables from the .env file (if present)
load_dotenv(override=True)

TRACE_ENABLED = os.getenv("WEB_MCP_TRACE", "0") == "1"
SCREENSHOT_ON_FAIL = os.getenv("WEB_MCP_SCREENSHOT_ON_FAIL", "0") == "1"
FULLSCREEN = os.getenv("RUN_WEB_FULLSCREEN", "0") == "1"


def find_chrome_executable():
    candidates = [
        r"C:\Program Files\Google\Chrome\Application\chrome.exe",
        r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
        r"C:\Users\%USERNAME%\AppData\Local\Google\Chrome\Application\chrome.exe",
        r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
        r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
    ]

    for path in candidates:
        expanded = os.path.expandvars(path)
        if os.path.exists(expanded):
            return expanded

    raise FileNotFoundError(
        "Chrome / Edge executable not found. "
        "Install Chrome or update find_chrome_executable()."
    )

#will use this function to find a free port on localhost
def find_free_port():
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("localhost", 0))
        return s.getsockname()[1]

CHROME_PATH = find_chrome_executable()
DEBUG_PORT = 9222
USER_DATA_DIR = tempfile.mkdtemp(prefix="cdp-profile-")


KEY_MAP = {
    "Enter": ("Enter", "Enter"),
    "Tab": ("Tab", "Tab"),
    "Escape": ("Escape", "Escape"),
    "Backspace": ("Backspace", "Backspace"),
    "Delete": ("Delete", "Delete"),
    "Space": (" ", "Space"),
    "ArrowUp": ("ArrowUp", "ArrowUp"),
    "ArrowDown": ("ArrowDown", "ArrowDown"),
    "ArrowLeft": ("ArrowLeft", "ArrowLeft"),
    "ArrowRight": ("ArrowRight", "ArrowRight"),
}



class ChromeCDP:
    def __init__(self):
        self.process = None
        self.ws = None
        self._ids = itertools.count(1)
        self._lock = threading.Lock()
        self._inflight_requests = 0 #rack in-flight requests
        self.tracer = TraceManager(enabled=TRACE_ENABLED)

    def _capture_failure_artifacts(self, entry):
        if not SCREENSHOT_ON_FAIL:
            return  # hard stop

        ts = entry["step"]

        # Screenshot
        try:
            img = self.screenshot(full_page=True)
            shot_path = f"traces/step_{ts}.png"
            with open(shot_path, "wb") as f:
                f.write(img)
            self.tracer.attach_artifact(entry, "screenshot", shot_path)
        except Exception:
            pass

        # DOM snapshot
        try:
            html = self.get_html()
            dom_path = f"traces/step_{ts}.html"
            with open(dom_path, "w", encoding="utf-8") as f:
                f.write(html)
            self.tracer.attach_artifact(entry, "dom", dom_path)
        except Exception:
            pass

    # ---------------- Chrome lifecycle ----------------
    def launch(self):
        if self.process:
            return

        args = [
            CHROME_PATH,
            f"--remote-debugging-port={DEBUG_PORT}",
            f"--user-data-dir={USER_DATA_DIR}",
            "--remote-allow-origins=*",
            "--disable-extensions",
            "--disable-infobars",
            "--disable-features=TranslateUI",
            "--no-first-run",
            "--no-default-browser-check",

            "--disable-save-password-bubble",
            "--disable-features=PasswordManagerOnboarding,PasswordCheck",
            "--password-store=basic",
            "--use-mock-keychain",
            "--disable-notifications",
            "--disable-popup-blocking",
        ]

        self.process = subprocess.Popen(
            args,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=subprocess.CREATE_NEW_PROCESS_GROUP
            if os.name == "nt" else 0
        )

        self._wait_for_cdp()
        self._connect_ws()
        self._enable_domains()
        if FULLSCREEN:
            self._set_fullscreen()


    def close(self):
        if not self.process:
            return
        try:
            if os.name == "nt":
                os.kill(self.process.pid, signal.CTRL_BREAK_EVENT)
            else:
                self.process.terminate()
        except Exception:
            pass
        self.process = None

    def _wait_for_cdp(self, timeout=10):
        start = time.time()
        while time.time() - start < timeout:
            try:
                r = requests.get(f"http://localhost:{DEBUG_PORT}/json/version", timeout=0.5)
                if r.status_code == 200:
                    return
            except Exception:
                pass
            time.sleep(0.2)
        raise RuntimeError("CDP endpoint not available")

    def _connect_ws(self):
        targets = requests.get(f"http://localhost:{DEBUG_PORT}/json").json()
        ws_url = next(t["webSocketDebuggerUrl"] for t in targets if t["type"] == "page")
        self.ws = websocket.WebSocket()
        self.ws.connect(ws_url)

    def _send(self, method, params=None):
        with self._lock:
            msg_id = next(self._ids)
            payload = {"id": msg_id, "method": method, "params": params or {}}
            self.ws.send(json.dumps(payload))
            return msg_id

    def _recv(self, msg_id):
        while True:
            msg = json.loads(self.ws.recv())
            if msg.get("id") == msg_id:
                return msg

    def _enable_domains(self):
        self._send("Page.enable")
        self._send("DOM.enable")
        self._send("Runtime.enable")
        self._send("Input.enable")
        self._send("Network.enable")

    def _parse_key_combo(self, combo: str):
        parts = combo.split("+")
        modifiers = {
            "Control": False,
            "Shift": False,
            "Alt": False,
            "Meta": False
        }

        key_part = None

        for p in parts:
            p = p.strip()
            if p in ["Ctrl", "Control"]:
                modifiers["Control"] = True
            elif p == "Shift":
                modifiers["Shift"] = True
            elif p == "Alt":
                modifiers["Alt"] = True
            elif p in ["Meta", "Cmd"]:
                modifiers["Meta"] = True
            else:
                key_part = p

        return modifiers, key_part

    def _set_fullscreen(self):
        """
        Maximize browser window using CDP.
        """
        try:
            # Get window ID
            msg_id = self._send("Browser.getWindowForTarget")
            result = self._recv(msg_id)["result"]
            window_id = result["windowId"]

            # Set window bounds to fullscreen
            self._send("Browser.setWindowBounds", {
                "windowId": window_id,
                "bounds": {"windowState": "fullscreen"}
            })
        except Exception:
            # Do NOT fail automation if fullscreen fails
            pass



    # ---------------- Page operations ----------------

    def navigate(self, url: str):
        self._send("Page.navigate", {"url": url})

    def get_html(self) -> str:
        msg_id = self._send(
            "Runtime.evaluate",
            {"expression": "document.documentElement.outerHTML"}
        )
        return self._recv(msg_id)["result"]["result"]["value"]

    # ---------------- Element helpers ----------------

    def element_exists(self, xpath: str) -> bool:
        expr = f'''
        document.evaluate("{xpath}", document, null,
        XPathResult.FIRST_ORDERED_NODE_TYPE, null).singleNodeValue
        '''
        msg_id = self._send("Runtime.evaluate", {"expression": expr})
        return self._recv(msg_id)["result"]["result"]["value"] is not None



    def type(self, xpath: str, value: str):
        self.click(xpath)
        for ch in value:
            self._send("Input.dispatchKeyEvent", {
                "type": "char",
                "text": ch
            })

    ### Old wait for element implementation ###
    '''
    def wait_for_element(self, xpath: str, timeout_ms: int = 5000, poll_ms: int = 200):
        """
        Wait until an element matching xpath appears in the DOM.
        Raises TimeoutError on failure.
        """
        deadline = time.monotonic() + (timeout_ms / 1000)

        expr = f'
        document.evaluate("{xpath}", document, null,
        XPathResult.FIRST_ORDERED_NODE_TYPE, null).singleNodeValue
        '

        while time.monotonic() < deadline:
            msg_id = self._send("Runtime.evaluate", {"expression": expr})
            result = self._recv(msg_id)["result"]["result"]["value"]

            if result is not None:
                return True

            time.sleep(poll_ms / 1000)

        raise TimeoutError(f"Element not found within {timeout_ms}ms: {xpath}")
    '''

    def wait_for_element(self, xpath, timeout_ms=5000):
        self.wait_for_dom_stable(timeout_ms)

        deadline = time.monotonic() + timeout_ms / 1000

        expr = f"""
        (function () {{
        const el = document.evaluate("{xpath}", document, null,
            XPathResult.FIRST_ORDERED_NODE_TYPE, null).singleNodeValue;
        if (!el) return false;
        const r = el.getBoundingClientRect();
        const s = window.getComputedStyle(el);
        return (
            r.width > 0 &&
            r.height > 0 &&
            s.visibility !== 'hidden' &&
            s.display !== 'none'
        );
        }})()
        """

        while time.monotonic() < deadline:
            msg_id = self._send("Runtime.evaluate", {"expression": expr})
            if self._recv(msg_id)["result"]["result"]["value"]:
                return True
            time.sleep(0.1)

        raise TimeoutError(f"Element not visible: {xpath}")

    
    def wait_for_visible_element(self, xpath: str, timeout_ms: int = 5000):
        deadline = time.monotonic() + (timeout_ms / 1000)

        expr = f'''
        (function() {{
        const el = document.evaluate("{xpath}", document, null,
            XPathResult.FIRST_ORDERED_NODE_TYPE, null).singleNodeValue;
        if (!el) return false;
        const style = window.getComputedStyle(el);
        const r = el.getBoundingClientRect();
        return (
            style.visibility !== "hidden" &&
            style.display !== "none" &&
            r.width > 0 &&
            r.height > 0
        );
        }})()
        '''

        while time.monotonic() < deadline:
            msg_id = self._send("Runtime.evaluate", {"expression": expr})
            visible = self._recv(msg_id)["result"]["result"]["value"]

            if visible:
                return True

            time.sleep(0.2)

        raise TimeoutError(f"Element not visible within {timeout_ms}ms: {xpath}")


    def wait_for_dom_stable(self, timeout_ms=5000, idle_ms=300):
        """
        Wait until DOM mutations stop for idle_ms duration.
        """
        deadline = time.monotonic() + timeout_ms / 1000

        expr = """
        (function () {
        if (!window.__domStableTracker) {
            window.__domStableTracker = { last: Date.now() };
            new MutationObserver(() => {
            window.__domStableTracker.last = Date.now();
            }).observe(document, { subtree: true, childList: true, attributes: true });
        }
        return Date.now() - window.__domStableTracker.last;
        })()
        """

        while time.monotonic() < deadline:
            msg_id = self._send("Runtime.evaluate", {"expression": expr})
            idle_time = self._recv(msg_id)["result"]["result"]["value"]

            if idle_time >= idle_ms:
                return True

            time.sleep(0.1)

        raise TimeoutError("DOM did not stabilize")


    def wait_for_network_idle(self, timeout_ms=5000, idle_ms=500):
        """
        Wait until there are no pending network requests.
        """
        deadline = time.monotonic() + timeout_ms / 1000

        expr = """
        (function () {
        return performance.getEntriesByType('resource')
            .filter(e => !e.responseEnd || e.responseEnd === 0).length;
        })()
        """

        stable_since = None

        while time.monotonic() < deadline:
            msg_id = self._send("Runtime.evaluate", {"expression": expr})
            pending = self._recv(msg_id)["result"]["result"]["value"]

            now = time.monotonic()

            if pending == 0:
                stable_since = stable_since or now
                if (now - stable_since) * 1000 >= idle_ms:
                    return True
            else:
                stable_since = None

            time.sleep(0.1)

        raise TimeoutError("Network did not become idle")


    def wait_for_text(self, text: str, timeout_ms: int = 10000):
        """
        Wait until the given visible text appears anywhere in the document.
        """
        deadline = time.monotonic() + timeout_ms / 1000

        expr = f"""
        (function () {{
        const walker = document.createTreeWalker(
            document.body,
            NodeFilter.SHOW_TEXT,
            null,
            false
        );

        while (walker.nextNode()) {{
            const node = walker.currentNode;
            if (node.nodeValue && node.nodeValue.includes({json.dumps(text)})) {{
            const parent = node.parentElement;
            if (!parent) continue;
            const style = window.getComputedStyle(parent);
            if (style && style.visibility !== 'hidden' && style.display !== 'none') {{
                return true;
            }}
            }}
        }}
        return false;
        }})()
        """

        while time.monotonic() < deadline:
            msg_id = self._send("Runtime.evaluate", {"expression": expr})
            result = self._recv(msg_id)["result"]["result"]

            if result.get("value") is True:
                return

            time.sleep(0.1)

        raise TimeoutError(f"Text not found within {timeout_ms}ms: '{text}'")







    # --------------- mouse handlers ----------------
    def mouse_down(self, x, y, button="left"):
        self._send("Input.dispatchMouseEvent", {
            "type": "mousePressed",
            "x": x,
            "y": y,
            "button": button,
            "clickCount": 1
        })

    def mouse_up(self, x, y, button="left"):
        self._send("Input.dispatchMouseEvent", {
            "type": "mouseReleased",
            "x": x,
            "y": y,
            "button": button,
            "clickCount": 1
        })

    def hover(self, xpath):
        point = self._get_element_center(xpath)
        self.mouse_move(point["x"], point["y"])

    def mouse_move(self, x, y):
        self._send("Input.dispatchMouseEvent", {
            "type": "mouseMoved",
            "x": x,
            "y": y,
            "buttons": 0
        })

    def double_click(self, xpath):
        point = self._get_element_center(xpath)
        for _ in range(2):
            self.mouse_down(point["x"], point["y"])
            self.mouse_up(point["x"], point["y"])

    def drag_and_drop(self, source_xpath, target_xpath):
        src = self._get_element_center(source_xpath)
        tgt = self._get_element_center(target_xpath)

        self.mouse_move(src["x"], src["y"])
        self.mouse_down(src["x"], src["y"])
        time.sleep(0.1)
        self.mouse_move(tgt["x"], tgt["y"])
        self.mouse_up(tgt["x"], tgt["y"])


    def _get_element_center(self, xpath):
        expr = f"""
        (function () {{
        try {{
            const el = document.evaluate("{xpath}", document, null,
            XPathResult.FIRST_ORDERED_NODE_TYPE, null).singleNodeValue;
            if (!el) return null;

            const r = el.getBoundingClientRect();
            if (!r || r.width === 0 || r.height === 0) return null;

            return {{ x: r.left + r.width / 2, y: r.top + r.height / 2 }};
        }} catch (e) {{
            return null;
        }}
        }})()
        """

        msg_id = self._send("Runtime.evaluate", {"expression": expr})
        result = self._recv(msg_id)["result"]["result"]

        # CRITICAL: never assume "value" exists
        return result.get("value")
    

    def press_key(self, key):
        self._send("Input.dispatchKeyEvent", {
            "type": "keyDown",
            "key": key
        })
        self._send("Input.dispatchKeyEvent", {
            "type": "keyUp",
            "key": key
        })

    def _clear_input(self, xpath):
        expr = f"""
        (function () {{
        const el = document.evaluate("{xpath}", document, null,
            XPathResult.FIRST_ORDERED_NODE_TYPE, null).singleNodeValue;
        if (!el) return false;
        el.value = '';
        el.dispatchEvent(new Event('input', {{ bubbles: true }}));
        el.dispatchEvent(new Event('change', {{ bubbles: true }}));
        return true;
        }})()
        """
        msg_id = self._send("Runtime.evaluate", {"expression": expr})
        return self._recv(msg_id)["result"]["result"].get("value") is True


    def fill(self, xpath: str, value: str, timeout_ms: int = 10000):

        """
            Fill an input field reliably (Playwright-style):
            - waits for page readiness
            - waits for element
            - scrolls into view
            - focuses via JS
            - clears via JS
            - types characters
            - retries on transient failures
            - traces everything (optionally)
        """
        entry = None
        if self.tracer.enabled:
            entry = self.tracer.start_step(
                action="fill",
                target=xpath,
                params={"value": value}
            )

        deadline = time.monotonic() + timeout_ms / 1000
        
        try:            
            while time.monotonic() < deadline:
                try:
                    # Check if page is ready
                    self._ensure_page_actionable(timeout_ms=3000)

                    self.wait_for_element(xpath, timeout_ms=2000)
                    
                    # Scroll into view
                    self._scroll_into_view(xpath)
                    
                    # Focus
                    if not self._is_editable(xpath):
                        raise RuntimeError("Not editable")

                    if not self._focus_element(xpath):
                        raise RuntimeError("Focus failed")

                    # Clear
                    self._clear_input(xpath)

                    # Type
                    for ch in value:
                        self._send("Input.dispatchKeyEvent", {
                            "type": "char",
                            "text": ch
                        })

                    if entry:
                        self.tracer.success(entry)

                    return  #return true

                except Exception:
                    if entry:
                        self.tracer.record_retry(entry)
                    time.sleep(0.1)

            raise TimeoutError(f"Fill timed out for xpath: {xpath}")

        except Exception as e:
            if entry:
                self.tracer.failure(entry, e)
                self._capture_failure_artifacts(entry)  # screenshot + DOM (env-controlled)
                self.tracer.dump()
            raise

    def click(self, xpath, timeout_ms=10000):
        """
            Click an element reliably (Playwright-style):
            - waits for page readiness
            - waits for element
            - scrolls into view
            - tries mouse click
            - falls back to JS click
            - retries on transient failures
            - traces everything (optionally)
        """
        entry = None
        if self.tracer.enabled:
            entry = self.tracer.start_step(
                action="click",
                target=xpath
            )

        deadline = time.monotonic() + timeout_ms / 1000
        try:
            while time.monotonic() < deadline:
                try:
                    ## Check if page is ready
                    self._ensure_page_actionable(timeout_ms=3000)

                    #Chckk if element is ready
                    self.wait_for_element(xpath, timeout_ms=2000)

                    #scroll to view
                    self._scroll_into_view(xpath)

                    # click first to check if ready
                    point = self._get_element_center(xpath)
                    if point:
                        self._send("Input.dispatchMouseEvent", {
                            "type": "mousePressed",
                            "x": point["x"],
                            "y": point["y"],
                            "button": "left",
                            "clickCount": 1
                        })
                        self._send("Input.dispatchMouseEvent", {
                            "type": "mouseReleased",
                            "x": point["x"],
                            "y": point["y"],
                            "button": "left",
                            "clickCount": 1
                        })
                        
                        if entry:
                            self.tracer.success(entry)
                        return  #true

                    # Fallback to JS click
                    if self._js_click(xpath):
                        if entry:
                            self.tracer.success(entry)
                        return  #true

                    raise RuntimeError("CMouse and JS click both failed")

                except Exception:
                    if entry:
                        self.tracer.record_retry(entry)
                    time.sleep(0.1)

            raise TimeoutError(f"Click failed: {xpath}")
        except Exception as e:            
            if entry:
                self.tracer.failure(entry, e)
                self._capture_failure_artifacts(entry)  # screenshot + DOM (env-controlled)
                self.tracer.dump()
            raise

    def _focus_element(self, xpath):
        expr = f"""
        (function () {{
        const el = document.evaluate("{xpath}", document, null,
            XPathResult.FIRST_ORDERED_NODE_TYPE, null).singleNodeValue;
        if (!el) return false;
        el.scrollIntoView({{block: 'center', inline: 'center'}});
        el.focus();
        return document.activeElement === el;
        }})()
        """
        msg_id = self._send("Runtime.evaluate", {"expression": expr})
        result = self._recv(msg_id)["result"]["result"]
        return result.get("value") is True
    

    def _is_editable(self, xpath):
        expr = f"""
        (function () {{
        const el = document.evaluate("{xpath}", document, null,
            XPathResult.FIRST_ORDERED_NODE_TYPE, null).singleNodeValue;
        if (!el) return false;
        return !el.disabled && !el.readOnly;
        }})()
        """
        msg_id = self._send("Runtime.evaluate", {"expression": expr})
        return self._recv(msg_id)["result"]["result"].get("value") is True



    def _ensure_page_actionable(self, timeout_ms=15000):
        """
        Block until the page is ready to accept user input.
        This mirrors Playwright's internal actionability checks.
        """
        deadline = time.monotonic() + timeout_ms / 1000

        while time.monotonic() < deadline:
            try:
                # Navigation finished
                ready = self._send("Runtime.evaluate", {
                    "expression": "document.readyState"
                })
                state = self._recv(ready)["result"]["result"].get("value")

                if state != "complete":
                    time.sleep(0.1)
                    continue

                #Network idle check
                self.wait_for_network_idle(timeout_ms=2000)

                #DOM stable
                self.wait_for_dom_stable(timeout_ms=2000)

                return  #Page is ready for use

            except Exception:
                time.sleep(0.1)

        raise TimeoutError("Page never became actionable")


    def send_keys(self, keys: str):
        """
        Send keyboard shortcuts or special keys.
        Example: 'Enter', 'Ctrl+A', 'Ctrl+Shift+Tab'
        """
        # Global readiness gate (same as click/fill)
        self._ensure_page_actionable()

        modifiers, key = self._parse_key_combo(keys)

        # Modifier bitmask (CDP spec)
        mod_mask = (
            (2 if modifiers["Control"] else 0) |
            (8 if modifiers["Shift"] else 0) |
            (1 if modifiers["Alt"] else 0) |
            (4 if modifiers["Meta"] else 0)
        )

        # Resolve key/code
        if key in KEY_MAP:
            key_val, code_val = KEY_MAP[key]
        elif len(key) == 1 and key.isalpha():
            key_val = key.lower()
            code_val = f"Key{key.upper()}"
        else:
            raise ValueError(f"Unsupported key: {keys}")

        # KeyDown
        self._send("Input.dispatchKeyEvent", {
            "type": "keyDown",
            "key": key_val,
            "code": code_val,
            "modifiers": mod_mask
        })

        # KeyUp
        self._send("Input.dispatchKeyEvent", {
            "type": "keyUp",
            "key": key_val,
            "code": code_val,
            "modifiers": mod_mask
        })


    def _scroll_into_view(self, xpath):
        expr = f"""
        (function () {{
        const el = document.evaluate("{xpath}", document, null,
            XPathResult.FIRST_ORDERED_NODE_TYPE, null).singleNodeValue;
        if (!el) return false;
        el.scrollIntoView({{
            block: 'center',
            inline: 'center',
            behavior: 'instant'
        }});
        return true;
        }})()
        """
        msg_id = self._send("Runtime.evaluate", {"expression": expr})
        result = self._recv(msg_id)["result"]["result"]
        return result.get("value") is True



    def _js_click(self, xpath):
        expr = f"""
        (function () {{
        const el = document.evaluate("{xpath}", document, null,
            XPathResult.FIRST_ORDERED_NODE_TYPE, null).singleNodeValue;
        if (!el) return false;
        el.scrollIntoView({{block: 'center', inline: 'center'}});
        el.click();
        return true;
        }})()
        """
        msg_id = self._send("Runtime.evaluate", {"expression": expr})
        result = self._recv(msg_id)["result"]["result"]
        return result.get("value") is True


    # ------------ Screenshot tools ------------
    def screenshot(self, full_page: bool = True):
        """
        Take a screenshot of the current page.
        Returns raw PNG bytes.
        """
        # Ensure page is stable before screenshot
        self._ensure_page_actionable(timeout_ms=5000)

        params = {
            "format": "png"
        }

        if full_page:
            # Get layout metrics for full-page screenshot
            metrics_id = self._send("Page.getLayoutMetrics")
            metrics = self._recv(metrics_id)["result"]

            content_size = metrics["contentSize"]

            params["clip"] = {
                "x": 0,
                "y": 0,
                "width": content_size["width"],
                "height": content_size["height"],
                "scale": 1
            }

        shot_id = self._send("Page.captureScreenshot", params)
        result = self._recv(shot_id)["result"]

        return base64.b64decode(result["data"])
    

    # ------------ Element state checkers ------------
    def is_checked(self, xpath: str) -> bool:
        expr = f"""
        (function () {{
        const el = document.evaluate("{xpath}", document, null,
            XPathResult.FIRST_ORDERED_NODE_TYPE, null).singleNodeValue;
        if (!el) return false;
        return el.checked === true;
        }})()
        """
        msg_id = self._send("Runtime.evaluate", {"expression": expr})
        return self._recv(msg_id)["result"]["result"].get("value") is True
    

    def is_selected(self, xpath: str) -> bool:
        expr = f"""
        (function () {{
        const el = document.evaluate("{xpath}", document, null,
            XPathResult.FIRST_ORDERED_NODE_TYPE, null).singleNodeValue;
        if (!el) return false;
        return el.selected === true;
        }})()
        """
        msg_id = self._send("Runtime.evaluate", {"expression": expr})
        return self._recv(msg_id)["result"]["result"].get("value") is True


    # ---------------- Multi options functions ----------------
    # select option by value / label / index
    def select_option(
        self,
        select_xpath: str,
        *,
        value: str | None = None,
        label: str | None = None,
        index: int | None = None
    ):
        self._ensure_page_actionable()

        expr = f"""
        (function () {{
        const select = document.evaluate("{select_xpath}", document, null,
            XPathResult.FIRST_ORDERED_NODE_TYPE, null).singleNodeValue;
        if (!select) return false;

        let option = null;
        if ({json.dumps(value)} !== null) {{
            option = [...select.options].find(o => o.value === {json.dumps(value)});
        }} else if ({json.dumps(label)} !== null) {{
            option = [...select.options].find(o => o.text === {json.dumps(label)});
        }} else if ({index} !== null) {{
            option = select.options[{index}];
        }}

        if (!option) return false;

        select.value = option.value;
        option.selected = true;

        select.dispatchEvent(new Event('input', {{ bubbles: true }}));
        select.dispatchEvent(new Event('change', {{ bubbles: true }}));
        return true;
        }})()
        """

        msg_id = self._send("Runtime.evaluate", {"expression": expr})
        result = self._recv(msg_id)["result"]["result"]
        if result.get("value") is not True:
            raise RuntimeError("Select option failed")


    #multi select
    def multi_select(self, select_xpath: str, values: list[str]):
        self._ensure_page_actionable()

        expr = f"""
        (function () {{
        const select = document.evaluate("{select_xpath}", document, null,
            XPathResult.FIRST_ORDERED_NODE_TYPE, null).singleNodeValue;
        if (!select || !select.multiple) return false;

        const values = {json.dumps(values)};
        for (const option of select.options) {{
            option.selected = values.includes(option.value);
        }}

        select.dispatchEvent(new Event('input', {{ bubbles: true }}));
        select.dispatchEvent(new Event('change', {{ bubbles: true }}));
        return true;
        }})()
        """

        msg_id = self._send("Runtime.evaluate", {"expression": expr})
        result = self._recv(msg_id)["result"]["result"]
        if result.get("value") is not True:
            raise RuntimeError("Multi-select failed")
