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
import shutil
import glob
from pathlib import Path
import re

from tracemanager import TraceManager

# Load environment variables from the .env file (if present)
load_dotenv(override=True)
os.environ["NO_PROXY"] = "127.0.0.1,localhost" #Disable proxy for local connections
os.environ["no_proxy"] = "127.0.0.1,localhost" #Disable proxy for local connections


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


TRACE_ENABLED = os.getenv("WEB_MCP_TRACE", "0") == "1"
SCREENSHOT_ON_FAIL = os.getenv("WEB_MCP_SCREENSHOT_ON_FAIL", "0") == "1"


# --- CONFIGURATION CONSTANTS ---

# Connection
CHROME_PATH = find_chrome_executable()
DEBUG_PORT = int(os.getenv("CHROME_DEBUG_PORT", "9222"))
USER_DATA_DIR = os.getenv("USER_DATA_DIR")

# Global Timeouts (ms)
DEFAULT_TIMEOUT = int(os.getenv("DEFAULT_TIMEOUT", "10000"))
PAGE_LOAD_TIMEOUT = int(os.getenv("PAGE_LOAD_TIMEOUT", "15000"))
NETWORK_TIMEOUT = int(os.getenv("NETWORK_IDLE_TIMEOUT", "2000"))
DOM_TIMEOUT = int(os.getenv("DOM_STABLE_TIMEOUT", "2000"))
APP_CLOSE_TIMEOUT = int(os.getenv("APP_CLOSE_TIMEOUT", "2000"))

# Stability Criteria (ms)
NETWORK_IDLE_MS = int(os.getenv("NETWORK_IDLE_DURATION", "500"))
DOM_IDLE_MS = int(os.getenv("DOM_STABLE_DURATION", "500"))

# Delays (Seconds - converted from ms)
HUMAN_DELAY = int(os.getenv("HUMAN_KEY_DELAY", "100")) / 1000.0
AUTO_DELAY = int(os.getenv("AUTOCOMPLETE_TYPE_DELAY", "100")) / 1000.0
UI_DELAY = int(os.getenv("UI_ANIMATION_DELAY", "500")) / 1000.0
STEP_DELAY = int(os.getenv("ACTION_STEP_DELAY", "200")) / 1000.0

# Viewport
VIEWPORT_WIDTH = int(os.getenv("VIEWPORT_WIDTH", "1920"))
VIEWPORT_HEIGHT = int(os.getenv("VIEWPORT_HEIGHT", "1080"))


## Removing global entry for user data dir to create a fresh one each time
#USER_DATA_DIR = tempfile.mkdtemp(prefix="cdp-profile-", dir="C:\\Users\\PreetPragyan\\temp")

#PROXIES
HTTP_PROXY = os.getenv("HTTP_PROXY")
HTTPS_PROXY = os.getenv("HTTPS_PROXY")
PROXIES = {
    "http": HTTP_PROXY,
    "https": HTTPS_PROXY
}

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
    "Home": ("Home", "Home"),
    "End": ("End", "End"),
    "PageUp": ("PageUp", "PageUp"),
    "PageDown": ("PageDown", "PageDown"),
}



class ChromeCDP:
    def __init__(self):
        self.process = None
        self.ws = None
        self._ids = itertools.count(1)
        self._lock = threading.Lock()
        self._inflight_requests = 0 #rack in-flight requests
        self.tracer = TraceManager(enabled=TRACE_ENABLED)
        self.input_ready = False # To track if Input domain is enabled
        #Cleanup stale profiles
        self._clean_old_profiles()
        #Create a fresh user data dir for this session
        self.user_data_dir = tempfile.mkdtemp(prefix="cdp-profile-", dir=USER_DATA_DIR)

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
            "--remote-debugging-address=127.0.0.1",
            #f"--user-data-dir={USER_DATA_DIR}",
            f"--user-data-dir={self.user_data_dir}",
            "--remote-allow-origins=*",
            "--disable-extensions",
            "--disable-infobars",
            "--disable-features=TranslateUI,PasswordCheck,PasswordLeakDetection,PasswordManagerOnboarding,AutofillServerCommunication",
            "--no-first-run",
            "--no-default-browser-check",
            #increae window size
            #"--start-maximized",
            "--disable-save-password-bubble",
            "--password-store=basic",
            "--use-mock-keychain",
            "--disable-notifications",
            "--disable-popup-blocking",
        ]
        
        #set preference in temp profile to disable password saving prompts
        prefs = {
            "credentials_enable_service": False,
            "profile": {
                "password_manager_enabled": False,
                "password_manager_leak_detection": False,
            },
            "autofill": {"enabled": False},
        }
        prefs_path = os.path.join(self.user_data_dir, "Default")
        os.makedirs(prefs_path, exist_ok=True)
        with open(os.path.join(prefs_path, "Preferences"), "w", encoding="utf-8") as f:
            json.dump(prefs, f)


        self.process = subprocess.Popen(
            args,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=subprocess.CREATE_NEW_PROCESS_GROUP
            if os.name == "nt" else 0
        )

        self.http = requests.Session()
        self.http.trust_env = False  # Ignore system proxies
        self.http.proxies = {
            "http": None,
            "https": None
        }

        self._wait_for_cdp()

        r = self.http.get(f"http://localhost:{DEBUG_PORT}/json/new", timeout=1)
        print(f"New Tab Response: {r.status_code}")

        self._connect_ws()
        self._enable_domains()
        self.force_viewport(VIEWPORT_WIDTH, VIEWPORT_HEIGHT)

    def _ensure_input_ready(self):
        if self.input_ready:
            return

        self._enable_domains()
        self.input_ready = True

    def close(self):
        if not self.process:
            return
        try:
            # ... existing kill logic ...
            if os.name == "nt":
                os.kill(self.process.pid, signal.CTRL_BREAK_EVENT)
            else:
                self.process.terminate()

            try:
                self.process.wait(timeout=APP_CLOSE_TIMEOUT / 1000)
            except subprocess.TimeoutExpired:
                self.process.kill()
                
        except Exception:
            pass
        
        self.process = None
        
        # Wait a little for file locks to release
        time.sleep(UI_DELAY)
        try:
            shutil.rmtree(self.user_data_dir, ignore_errors=True)
            print(f"Cleaned up profile: {self.user_data_dir}")
        except Exception as e:
            print(f"Warning: Could not delete profile {self.user_data_dir}: {e}")

    def _wait_for_cdp(self, timeout=10):
        start = time.time()
        last_error = None
        while time.time() - start < timeout:
            #if chrome dies fail immediately
            if self.process and self.process.poll() is not None:
                raise RuntimeError("Chrome process exited unexpectedly")

            try:
                r = self.http.get(f"http://localhost:{DEBUG_PORT}/json/version", timeout=0.5)
                print(f"CDP Version Check Status Code: {r.status_code}")
                if r.status_code == 200:
                    return
            except Exception as e:
                #pass
                last_error = e
            time.sleep(STEP_DELAY)
        raise RuntimeError("CDP endpoint not available")

    def _connect_ws(self, attempts=5, delay=0.2):
        last_error = None
        for i in range(attempts):
            try:
                # 1. Get list of targets
                resp = self.http.get(f"http://localhost:{DEBUG_PORT}/json", timeout=2)
                targets = resp.json() if resp.ok else []
                
                # 2. Filter for valid pages
                pages = [t for t in targets if isinstance(t, dict) and t.get("type") == "page"]
                
                if pages and pages[0].get("webSocketDebuggerUrl"):
                    ws_url = pages[0]["webSocketDebuggerUrl"]
                    
                    # 3. Try to connect (Wrapped in Try/Except)
                    self.ws = websocket.WebSocket()
                    try:
                        self.ws.connect(ws_url, timeout=5)
                        self.ws.settimeout(1)
                        print(f"Connected to target: {pages[0]['id']}")
                        return
                    except Exception as e:
                        # If target vanished (500 Error), ignore and retry loop
                        print(f"Target {pages[0]['id']} vanished, retrying... ({e})")
                        last_error = e
                        self.ws = None
                        time.sleep(delay)
                        continue

            except Exception as e:
                last_error = e
            
            time.sleep(delay)
            
        raise RuntimeError(f"Could not connect to Chrome WS after {attempts} attempts. Last error: {last_error}")

    def _send(self, method, params=None):
        with self._lock:
            msg_id = next(self._ids)
            payload = {"id": msg_id, "method": method, "params": params or {}}
            self.ws.send(json.dumps(payload))
            return msg_id

    def _handle_event(self, msg):
        if msg.get("method") in (
            "Network.requestWillBeSent",
            "Network.responseReceived",
            "Network.loadingFinished",
            "Network.loadingFailed",
        ):
            with self._lock:
                if msg["method"] == "Network.requestWillBeSent":
                    self._inflight_requests += 1
                elif msg["method"] in ("Network.loadingFinished", "Network.loadingFailed"):
                    self._inflight_requests = max(0, self._inflight_requests - 1)

    def _recv(self, msg_id, timeout=None):
        # while True:
        #     msg = json.loads(self.ws.recv())
        #     if msg.get("id") == msg_id:
        #         return msg
        deadline = None if timeout is None else time.monotonic() + timeout
        while True:
            if deadline and time.monotonic() > deadline:
                raise TimeoutError(f"CDP response timeout for {msg_id}")
            try:
                raw = self.ws.recv()
            except websocket.WebSocketTimeoutException:
                continue
            except Exception as e:
                raise RuntimeError(f"WebSocket receive failed: {e}")
            try:
                msg = json.loads(raw)
            except Exception:
                continue
            if "method" in msg:
                self._handle_event(msg)  # process events, then keep waiting
                continue
            if msg.get("id") == msg_id:
                return msg

    def _enable_domains(self):
        self._send("Page.enable")
        self._send("DOM.enable")
        self._send("CSS.enable")
        self._send("Runtime.enable")
        self._send("Input.enable")
        self._send("Network.enable")
        self._send("Page.setLifecycleEventsEnabled", {"enabled": True})
        self._send("Page.bringToFront")
        self._send("Network.enable")
        self._send("Network.setCacheDisabled", {"cacheDisabled": True})

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

    #Force a viewport
    def force_viewport(self, width=1920, height=1080):
            """
            Force the browser window to a specific size using CDP.
            This overrides any user profile settings or previous session states.
            """
            try:
                # 1. Get the Window ID of the current target
                msg_id = self._send("Browser.getWindowForTarget")
                result = self._recv(msg_id)["result"]
                window_id = result["windowId"]

                # 2. Force the bounds
                self._send("Browser.setWindowBounds", {
                    "windowId": window_id,
                    "bounds": {
                        "width": width,
                        "height": height,
                        "windowState": "normal" # Ensure it's not minimized/maximized
                    }
                })
                print(f"Viewport forced to {width}x{height}")
            except Exception as e:
                print(f"Failed to force viewport: {e}")

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


    # --------------- Wait helpers ----------------
    def wait_for_element(self, xpath, timeout_ms=DEFAULT_TIMEOUT):
        self.wait_for_dom_stable(timeout_ms)

        deadline = time.monotonic() + timeout_ms / 1000

        expr = f"""
        (function () {{
            const snapshot = document.evaluate("{xpath}", document, null,
                XPathResult.ORDERED_NODE_SNAPSHOT_TYPE, null);
            
            for (let i = 0; i < snapshot.snapshotLength; i++) {{
                const el = snapshot.snapshotItem(i);
                const r = el.getBoundingClientRect();
                const s = window.getComputedStyle(el);
                
                if (r.width > 0 && r.height > 0 && 
                    s.visibility !== 'hidden' && s.display !== 'none') {{
                    return true;
                }}
            }}
            return false;
        }})()
        """

        while time.monotonic() < deadline:
            msg_id = self._send("Runtime.evaluate", {"expression": expr})
            if self._recv(msg_id)["result"]["result"]["value"]:
                return True
            time.sleep(STEP_DELAY)

        raise TimeoutError(f"Element not visible: {xpath}")
    
    def wait_for_visible_element(self, xpath: str, timeout_ms: int = DEFAULT_TIMEOUT):
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

            time.sleep(STEP_DELAY)

        raise TimeoutError(f"Element not visible within {timeout_ms}ms: {xpath}")

    def wait_for_dom_stable(self, timeout_ms=DOM_TIMEOUT, idle_ms=DOM_IDLE_MS):
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
            # msg_id = self._send("Runtime.evaluate", {"expression": expr})
            # idle_time = self._recv(msg_id)["result"]["result"]["value"]

            # if idle_time >= idle_ms:
            #     return True

            # Added try-except to handle transient errors during navigation
            try:
                msg_id = self._send("Runtime.evaluate", {"expression": expr})
                result = self._recv(msg_id)
                
                # Check for error in evaluation (e.g. context destroyed)
                if "error" in result.get("result", {}):
                    time.sleep(STEP_DELAY)
                    continue
                    
                idle_time = result["result"]["result"]["value"]
                if idle_time >= idle_ms:
                    return True
                    
            except Exception:
                # Ignore transient errors during page loads/navs
                pass

            time.sleep(STEP_DELAY)

        raise TimeoutError("DOM did not stabilize")

    def wait_for_network_idle(self, timeout_ms=NETWORK_TIMEOUT, idle_ms=NETWORK_IDLE_MS):
        """
        Wait until there are no pending network requests.
        """
        deadline = time.monotonic() + timeout_ms / 1000
        stable_since = None
        while time.monotonic() < deadline:
            with self._lock:
                pending = self._inflight_requests
            now = time.monotonic()
            if pending == 0:
                stable_since = stable_since or now
                if (now - stable_since) * 1000 >= idle_ms:
                    return True
            else:
                stable_since = None
            time.sleep(STEP_DELAY)
        raise TimeoutError("Network did not become idle")

    def wait_for_text(self, text: str, timeout_ms: int = DEFAULT_TIMEOUT):
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

            time.sleep(STEP_DELAY)

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

    #epxerimental method to dispatch hover events
    def _dispatch_synthetic_hover(self, xpath):
        """
        Manually dispatches hover events to the FIRST VISIBLE element.
        """
        expr = f"""
        (function() {{
            const snapshot = document.evaluate("{xpath}", document, null,
                XPathResult.ORDERED_NODE_SNAPSHOT_TYPE, null);

            let el = null;
            for (let i = 0; i < snapshot.snapshotLength; i++) {{
                const item = snapshot.snapshotItem(i);
                const style = window.getComputedStyle(item);
                if (style.display !== 'none' && style.visibility !== 'hidden') {{
                    el = item;
                    break;
                }}
            }}

            if (!el) return;
            
            const eventTypes = ['mouseover', 'mouseenter', 'pointerover', 'pointerenter'];
            eventTypes.forEach(type => {{
                const e = new MouseEvent(type, {{
                    view: window,
                    bubbles: true,
                    cancelable: true,
                    buttons: 0,
                    clientX: el.getBoundingClientRect().left,
                    clientY: el.getBoundingClientRect().top
                }});
                el.dispatchEvent(e);
            }});
        }})()
        """
        self._send("Runtime.evaluate", {"expression": expr})

    def _dispatch_synthetic_hover_on_id(self, object_id):
        """
        Dispatches hover events directly to the Object ID.
        """
        expr = """
        function(el) {
            const eventTypes = ['mouseover', 'mouseenter', 'pointerover', 'pointerenter'];
            eventTypes.forEach(type => {
                const e = new MouseEvent(type, {
                    view: window,
                    bubbles: true,
                    cancelable: true,
                    buttons: 0,
                    clientX: el.getBoundingClientRect().left,
                    clientY: el.getBoundingClientRect().top
                });
                el.dispatchEvent(e);
            });
        }
        """
        self._send("Runtime.callFunctionOn", {
            "functionDeclaration": expr,
            "objectId": object_id
        })

    def hover(self, xpath, timeout_ms=DEFAULT_TIMEOUT):
        self._ensure_page_actionable(timeout_ms=timeout_ms)
        self.wait_for_element(xpath, timeout_ms=timeout_ms)
        
        # 1. Resolve XPath to a permanent Object ID ONE TIME.
        object_id = self._get_object_id(xpath)
        if not object_id:
             raise RuntimeError(f"Element found but ID retrieval failed: {xpath}")

        # 2. Scroll using the ID (Robust against DOM moves)
        self._send("DOM.scrollIntoViewIfNeeded", {"objectId": object_id})
        
        # 3. Get Center using the Box Model of the ID (No XPath re-eval)
        point = self._get_center_by_id(object_id)
        if not point:
             raise RuntimeError(f"Could not calculate geometry for: {xpath}")

        # 4. Perform the Physical Hover
        # "Jitter" to wake up event listeners
        self.mouse_move(point["x"] - 5, point["y"] - 5)
        time.sleep(STEP_DELAY)
        self.mouse_move(point["x"], point["y"])
        
        # 5. Synthetic Fallback (using the ID directly)
        self._dispatch_synthetic_hover_on_id(object_id)
        
        time.sleep(UI_DELAY) # Allow hover effects to take hold

    def mouse_move(self, x, y):
        self._send("Input.dispatchMouseEvent", {
            "type": "mouseMoved",
            "x": x,
            "y": y,
            "buttons": 0
        })

    def double_click(self, xpath, timeout_ms=DEFAULT_TIMEOUT):
        self._ensure_page_actionable(timeout_ms=timeout_ms)
        self.wait_for_element(xpath, timeout_ms=timeout_ms)
        
        obj_id = self._get_object_id(xpath)
        if not obj_id: raise RuntimeError(f"Double click failed; no ID for {xpath}")

        self._send("DOM.scrollIntoViewIfNeeded", {"objectId": obj_id})
        point = self._get_center_by_id(obj_id)

        if point:
            for i in range(2):
                self.mouse_down(point["x"], point["y"])
                self.mouse_up(point["x"], point["y"])
            return
            
        # JS Fallback
        self._send("Runtime.callFunctionOn", {
            "functionDeclaration": "function() { this.click(); this.click(); }",
            "objectId": obj_id
        })

    def drag_and_drop(self, source_xpath, target_xpath, timeout_ms=DEFAULT_TIMEOUT):
        self._ensure_page_actionable(timeout_ms=timeout_ms)
        self.wait_for_element(source_xpath, timeout_ms=timeout_ms)
        self.wait_for_element(target_xpath, timeout_ms=timeout_ms)
        
        # 1. Resolve IDs immediately
        src_id = self._get_object_id(source_xpath)
        tgt_id = self._get_object_id(target_xpath)
        
        if not src_id or not tgt_id:
            raise RuntimeError("Drag failed: could not resolve source or target ID")

        # 2. Scroll both into view (Sequence matters less now)
        self._send("DOM.scrollIntoViewIfNeeded", {"objectId": src_id})
        self._send("DOM.scrollIntoViewIfNeeded", {"objectId": tgt_id})

        # 3. Get Coordinates from IDs
        src = self._get_center_by_id(src_id)
        tgt = self._get_center_by_id(tgt_id)

        if src and tgt:
            self.mouse_move(src["x"], src["y"])
            self.mouse_down(src["x"], src["y"])
            time.sleep(STEP_DELAY) # Small drag delay
            self.mouse_move(tgt["x"], tgt["y"])
            time.sleep(STEP_DELAY)
            self.mouse_up(tgt["x"], tgt["y"])
            return

        raise RuntimeError("Drag failed: could not calculate geometry from IDs")

    def _get_element_center(self, xpath):
        # STRATEGY 1: JavaScript getBoundingClientRect (Fastest)
        expr = f"""
        (function () {{
            try {{
                const snapshot = document.evaluate("{xpath}", document, null,
                    XPathResult.ORDERED_NODE_SNAPSHOT_TYPE, null);
                
                let el = null;
                for (let i = 0; i < snapshot.snapshotLength; i++) {{
                    const item = snapshot.snapshotItem(i);
                    const rect = item.getBoundingClientRect();
                    const style = window.getComputedStyle(item);
                    
                    if (rect.width > 0 && rect.height > 0 && 
                        style.visibility !== 'hidden' && style.display !== 'none') {{
                        el = item;
                        break;
                    }}
                }}

                if (!el) return null;

                const r = el.getBoundingClientRect();
                return {{ x: r.left + r.width / 2, y: r.top + r.height / 2 }};
            }} catch (e) {{
                return null;
            }}
        }})()
        """
        msg_id = self._send("Runtime.evaluate", {"expression": expr})
        result = self._recv(msg_id)["result"]["result"]
        point = result.get("value")
        
        if point and "x" in point and "y" in point:
            return point

        # STRATEGY 2: CDP Box Model (Fallback)
        # Use this if JS fails or returns null (e.g., complex overlays)
        print(f"DEBUG: JS center failed for {xpath}, trying Box Model...")
        return self._get_center_via_box_model(xpath)

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

    def fill(self, xpath: str, value: str, timeout_ms: int = DEFAULT_TIMEOUT):

        entry = None
        if self.tracer.enabled:
            entry = self.tracer.start_step(action="fill", target=xpath, params={"value": value})

        deadline = time.monotonic() + timeout_ms / 1000
        
        try:            
            while time.monotonic() < deadline:
                try:
                    self._ensure_page_actionable(timeout_ms=PAGE_LOAD_TIMEOUT)
                    self.wait_for_element(xpath, timeout_ms=DEFAULT_TIMEOUT)
                    
                    # 1. Get Stable Reference
                    obj_id = self._get_object_id(xpath)
                    if not obj_id: raise RuntimeError("Object ID lookup failed")

                    # 2. Scroll
                    self._send("DOM.scrollIntoViewIfNeeded", {"objectId": obj_id})

                    # 3. Focus (Using Runtime.callFunctionOn)
                    # self._send("Runtime.callFunctionOn", {
                    #     "functionDeclaration": "function() { this.focus(); }",
                    #     "objectId": obj_id
                    # })

                    # 3. Focus (Physical click ensures events fire)
                    point = self._get_center_by_id(obj_id)
                    if point:
                        self.mouse_move(point["x"], point["y"])
                        self.mouse_down(point["x"], point["y"])
                        self.mouse_up(point["x"], point["y"])
                    else:
                        self._send("Runtime.callFunctionOn", {
                            "functionDeclaration": "function() { this.focus(); }",
                            "objectId": obj_id
                        })

                    # WAIT for focus to settle
                    time.sleep(STEP_DELAY)

                    # 4. Clear (Using Runtime.callFunctionOn)
                    # self._send("Runtime.callFunctionOn", {
                    #     "functionDeclaration": "function() { this.value = ''; this.dispatchEvent(new Event('input', {bubbles:true})); }",
                    #     "objectId": obj_id
                    # })

                    # 4. Physical Clear (Ctrl+A -> Backspace)
                    # We use this instead of JS to ensure React/Angular state updates
                    
                    # Press Ctrl
                    self._send("Input.dispatchKeyEvent", {
                        "type": "keyDown", 
                        "key": "Control", 
                        "code": "ControlLeft", 
                        "modifiers": 2 
                    })
                    
                    # Press A
                    self._send("Input.dispatchKeyEvent", {
                        "type": "keyDown", 
                        "key": "a", 
                        "code": "KeyA", 
                        "modifiers": 2, # 2 = Ctrl
                        "text": "",     # Explicitly say no text
                        "unmodifiedText": ""
                    })
                    
                    self._send("Input.dispatchKeyEvent", {
                        "type": "keyUp", 
                        "key": "a", 
                        "code": "KeyA", 
                        "modifiers": 2
                    })
                    
                    # Release Ctrl
                    self._send("Input.dispatchKeyEvent", {"type": "keyUp", "key": "Control", "code": "ControlLeft", "modifiers": 0})
                    
                    time.sleep(0.05) # Pause between Select and Delete

                    # Press Backspace
                    self._send("Input.dispatchKeyEvent", {"type": "keyDown", "key": "Backspace", "code": "Backspace"})
                    self._send("Input.dispatchKeyEvent", {"type": "keyUp", "key": "Backspace", "code": "Backspace"})

                    # --- CRITICAL FIX: PAUSE HERE ---
                    # Wait for the "Field Required" validation to fire and settle
                    time.sleep(STEP_DELAY)

                    # 5. Type (Keystrokes go to focused element)
                    for ch in value:
                        self._send("Input.dispatchKeyEvent", { "type": "char", "text": ch })
                        # Optional: Tiny delay for stability
                        # time.sleep(0.01)

                    if entry: self.tracer.success(entry)
                    return

                except Exception:
                    if entry: self.tracer.record_retry(entry)
                    time.sleep(STEP_DELAY)

            raise TimeoutError(f"Fill timed out for xpath: {xpath}")

        except Exception as e:
            if entry:
                self.tracer.failure(entry, e)
                self._capture_failure_artifacts(entry)
                self.tracer.dump()
            raise

    def click(self, xpath, timeout_ms=DEFAULT_TIMEOUT):
        entry = self.tracer.start_step(action="click", target=xpath) if self.tracer.enabled else None
        deadline = time.monotonic() + timeout_ms / 1000
        try:
            while time.monotonic() < deadline:
                try:
                    self._ensure_page_actionable(timeout_ms=PAGE_LOAD_TIMEOUT)
                    self.wait_for_element(xpath, timeout_ms=DEFAULT_TIMEOUT)
                    
                    # 1. Get Stable Reference (OBJECT ID)
                    # We do this BEFORE scrolling to avoid losing the element if the DOM shifts
                    obj_id = self._get_object_id(xpath)
                    if not obj_id: raise RuntimeError("Object ID lookup failed")

                    # 2. Scroll & Calculate using ID
                    self._send("DOM.scrollIntoViewIfNeeded", {"objectId": obj_id})
                    point = self._get_center_by_id(obj_id)
                    
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
                        if entry: self.tracer.success(entry)
                        return

                    # Fallback: JS click on the specific ID
                    self._send("Runtime.callFunctionOn", {
                        "functionDeclaration": "function() { this.click(); }",
                        "objectId": obj_id
                    })
                    if entry: self.tracer.success(entry)
                    return

                except Exception:
                    if entry: self.tracer.record_retry(entry)
                    time.sleep(STEP_DELAY)

            raise TimeoutError(f"Click failed: {xpath}")
        except Exception as e:
            if entry:
                self.tracer.failure(entry, e)
                self._capture_failure_artifacts(entry)
                self.tracer.dump()
            raise

    def _focus_element(self, xpath):
        expr = f"""
        (function () {{
            const snapshot = document.evaluate("{xpath}", document, null,
                XPathResult.ORDERED_NODE_SNAPSHOT_TYPE, null);
                
            let el = null;
            for (let i = 0; i < snapshot.snapshotLength; i++) {{
                const item = snapshot.snapshotItem(i);
                const rect = item.getBoundingClientRect();
                const style = window.getComputedStyle(item);
                
                if (rect.width > 0 && style.display !== 'none' && style.visibility !== 'hidden') {{
                    el = item;
                    break;
                }}
            }}
            
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
            const snapshot = document.evaluate("{xpath}", document, null,
                XPathResult.ORDERED_NODE_SNAPSHOT_TYPE, null);
            
            for (let i = 0; i < snapshot.snapshotLength; i++) {{
                const el = snapshot.snapshotItem(i);
                const style = window.getComputedStyle(el);
                
                if (style.display !== 'none' && style.visibility !== 'hidden') {{
                    return !el.disabled && !el.readOnly;
                }}
            }}
            return false;
        }})()
        """
        msg_id = self._send("Runtime.evaluate", {"expression": expr})
        return self._recv(msg_id)["result"]["result"].get("value") is True

    def _ensure_page_actionable(self, timeout_ms=PAGE_LOAD_TIMEOUT):
        """
        Robustly waits for the page to be fully loaded and stable.
        Checks:
        1. document.readyState == 'complete'
        2. Network is idle (Soft Check - doesn't block if busy)
        3. DOM is stable (no mutations > 500ms)
        """
        deadline = time.monotonic() + timeout_ms / 1000

        while time.monotonic() < deadline:
            try:
                # 1. Browser Lifecycle Check
                ready_id = self._send("Runtime.evaluate", {
                    "expression": "document.readyState"
                })
                state = self._recv(ready_id)["result"]["result"].get("value")

                if state != "complete":
                    time.sleep(STEP_DELAY)
                    continue

                # 2. Network Idle Check (SOFT CHECK)
                # If network is busy (analytics/ads) try to wait, but if times out, proceed anyway
                try:
                    self.wait_for_network_idle(timeout_ms=NETWORK_TIMEOUT, idle_ms=500)
                except TimeoutError:
                    pass 

                # 3. DOM Stability Check (Crucial for Hydration/Animations)
                # Waits for the HTML to stop shifting/growing for 500ms
                try:
                    self.wait_for_dom_stable(timeout_ms=DOM_TIMEOUT, idle_ms=500)
                except TimeoutError:
                    if time.monotonic() > deadline: raise
                    continue

                # return if lifecycle and DOM stability pass
                return

            except Exception:
                # Ignore transient errors (e.g., context destroyed during nav)
                time.sleep(STEP_DELAY)

        raise TimeoutError(f"Page failed to stabilize within {timeout_ms}ms")

    def send_keys(self, keys: str, xpath: str = None):
        """
        Send keyboard shortcuts or keys.
        Supports combos like 'Ctrl+A', 'Shift+Enter'.
        If xpath is provided, focuses that element first.
        """
        self._ensure_page_actionable()

        # 1. Target Specific Element (if requested)
        if xpath:
            # Robust Object ID pattern
            obj_id = self._get_object_id(xpath)
            if not obj_id:
                raise RuntimeError(f"Cannot send keys; element not found: {xpath}")

            self._send("DOM.scrollIntoViewIfNeeded", {"objectId": obj_id})
            
            # Force focus via JS
            self._send("Runtime.callFunctionOn", {
                "functionDeclaration": "function() { this.focus(); }",
                "objectId": obj_id
            })
            time.sleep(STEP_DELAY) # Small delay for focus to register

        # 2. Parse Keys
        modifiers, key = self._parse_key_combo(keys)
        
        # Modifier bitmask (CDP spec: Alt=1, Ctrl=2, Meta/Cmd=4, Shift=8)
        mod_mask = (
            (2 if modifiers["Control"] else 0) |
            (8 if modifiers["Shift"] else 0) |
            (1 if modifiers["Alt"] else 0) |
            (4 if modifiers["Meta"] else 0)
        )

        # Resolve key/code
        if key in KEY_MAP:
            key_val, code_val = KEY_MAP[key]
            # Special handling for "Enter" which often needs 'rawKeyDown'
            type_down = "rawKeyDown" if key == "Enter" else "keyDown"

        elif len(key) == 1:
            # Handle regular characters (a-z, 0-9)
            # If Shift is held (e.g. Shift+a), we usually want 'A'
            if modifiers["Shift"]:
                key_val = key.upper()
            else:
                key_val = key
            
            # Simple heuristic for code
            if key.isalpha():
                code_val = f"Key{key.upper()}"
            elif key.isdigit():
                code_val = f"Digit{key}"
            else:
                code_val = "Unidentified"
            
            type_down = "keyDown"
        else:
             # Fallback for unknown keys
            key_val = key
            code_val = "Unidentified"
            type_down = "keyDown"

        # 3. Dispatch Events (KeyDown -> KeyUp)
        self._send("Input.dispatchKeyEvent", {
            "type": type_down,
            "key": key_val,
            "code": code_val,
            "modifiers": mod_mask,
            "windowsVirtualKeyCode": 0, 
            "nativeVirtualKeyCode": 0
        })
        
        self._send("Input.dispatchKeyEvent", {
            "type": "keyUp",
            "key": key_val,
            "code": code_val,
            "modifiers": mod_mask
        })

    def scroll_into_view(self, xpath):
        expr = f"""
        (function () {{
            const snapshot = document.evaluate("{xpath}", document, null,
                XPathResult.ORDERED_NODE_SNAPSHOT_TYPE, null);
            
            for (let i = 0; i < snapshot.snapshotLength; i++) {{
                const el = snapshot.snapshotItem(i);
                const style = window.getComputedStyle(el);
                
                // Find the first visible one
                if (style.display !== 'none' && style.visibility !== 'hidden') {{
                    el.scrollIntoView({{
                        block: 'center',
                        inline: 'center',
                        behavior: 'instant'
                    }});
                    return true;
                }}
            }}
            return false;
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

    def type_human(self, xpath: str, text: str):
        """
        Types text like a human (Appends to existing text).
        1. Focuses the element.
        2. Types one char at a time with delays.
        Does NOT clear the field first.
        """
        self._ensure_page_actionable()

        # 1. Get ID & Scroll
        obj_id = self._get_object_id(xpath)
        if not obj_id:
             raise RuntimeError(f"Element not found: {xpath}")

        self._send("DOM.scrollIntoViewIfNeeded", {"objectId": obj_id})
        
        # 2. Focus to field
        # We use a physical click to ensure the browser strictly focuses it
        point = self._get_center_by_id(obj_id)
        if point:
            self.mouse_move(point["x"], point["y"])
            self.mouse_down(point["x"], point["y"])
            self.mouse_up(point["x"], point["y"])
        else:
            self._send("Runtime.callFunctionOn", {
                "functionDeclaration": "function() { this.focus(); }",
                "objectId": obj_id
            })
        
        time.sleep(STEP_DELAY)

        # 3. Human like typing (Loop)
        # REMOVED: The Ctrl+A + Backspace block is gone.
        
        print(f"Human typing into {xpath}...")
        for char in text:
            # FIX: Send 'key' only for Up/Down, 'text' only for Char event
            self._send("Input.dispatchKeyEvent", {"type": "keyDown", "key": char})
            self._send("Input.dispatchKeyEvent", {"type": "char", "text": char})
            self._send("Input.dispatchKeyEvent", {"type": "keyUp", "key": char})
            
            # Jitter the delay to look natural
            jitter = (ord(char) % 3) * 0.02 
            time.sleep(HUMAN_DELAY + jitter)


    # ---------------- Data Extraction Tools ----------------
    def get_text(self, xpath: str) -> str:
        """
        Retrieves text from ANY element.
        - Inputs/Textareas: Returns 'value' (or 'placeholder' if empty).
        - Selects: Returns the visible text of the selected option.
        - Buttons: Handles both <button>Text</button> and <input type="button" value="Text">.
        - Standard Tags (div, span, td, p, h1, li, etc): Returns innerText.
        """
        self._ensure_page_actionable()

        obj_id = self._get_object_id(xpath)
        if not obj_id:
            raise RuntimeError(f"Element not found for text retrieval: {xpath}")

        expr = """
        function() {
            const el = this;
            const tag = el.tagName.toLowerCase();
            const inputTypes = ['text', 'password', 'email', 'number', 'search', 'url', 'tel', 'date'];
            
            // 1. Form Fields (Input, Textarea)
            if (tag === 'textarea' || (tag === 'input' && inputTypes.includes(el.type))) {
                return el.value || el.getAttribute('placeholder') || '';
            }
            
            // 2. Buttons (Submit, Reset, Button)
            // <input type="button" value="Save"> vs <button>Save</button>
            if (tag === 'input' && ['button', 'submit', 'reset'].includes(el.type)) {
                return el.value || '';
            }
            
            // 3. Dropdowns
            if (tag === 'select') {
                return el.options[el.selectedIndex].text || '';
            }

            // 4. Wrapper Logic (e.g., <td><input value="123"></td>)
            // If this element wraps a form field and has no text of its own, grab the child's value.
            const childInput = el.querySelector('input, textarea, select');
            if (childInput) {
                const directText = el.innerText.replace(childInput.value || '', '').trim();
                if (directText.length === 0) {
                     if (childInput.tagName.toLowerCase() === 'select') {
                        return childInput.options[childInput.selectedIndex].text || '';
                     }
                     return childInput.value || childInput.getAttribute('placeholder') || '';
                }
            }

            // 5. Universal Fallback (h1, p, div, span, li, a, label, th, td...)
            return el.innerText || el.textContent || '';
        }
        """
        msg_id = self._send("Runtime.callFunctionOn", {
            "objectId": obj_id,
            "functionDeclaration": expr,
            "returnByValue": True
        })

        result = self._recv(msg_id)
        
        # Safety for null/undefined results
        res_root = result.get("result", {})
        inner_res = res_root.get("result", {})
        val = inner_res.get("value", "")
        
        return str(val).strip()
    

    def scrape_table(
        self, 
        table_xpath: str, 
        next_page_xpath: str = None, 
        max_pages: int = 0,
        total_pages_xpath: str = None
    ):
        """
        Scrapes a table into a list of dictionaries.
        
        Args:
            table_xpath: XPath to the table/container.
            next_page_xpath: XPath to the 'Next' button.
            max_pages: Explicit limit (e.g., scrape 5 pages).
            total_pages_xpath: XPath to an element showing "Page 1 of N". 
                               We extract 'N' to determine the limit dynamically.
        """
        # Safety cap to prevent infinite loops
        SAFETY_LIMIT = 50 
        
        # 1. Determine the hard limit
        if max_pages > 0:
            limit = max_pages
            print(f"Scraping limit set by user: {limit} pages")
        elif total_pages_xpath:
            # Try to extract the limit from the UI (e.g., "Page 1 of 7")
            try:
                text = self.get_text(total_pages_xpath)
                # Look for number after 'of' or '/' (e.g. "of 7", "/ 7")
                match = re.search(r"(?:of|/)\s*(\d+)", text, re.IGNORECASE)
                
                if match:
                    limit = int(match.group(1))
                else:
                    # Fallback: find the last number in the string
                    numbers = re.findall(r"(\d+)", text)
                    limit = int(numbers[-1]) if numbers else SAFETY_LIMIT
                
                print(f"Detected total pages from UI: {limit}")
                
            except Exception as e:
                print(f"Could not extract page count from {total_pages_xpath}: {e}")
                limit = SAFETY_LIMIT
        else:
            limit = SAFETY_LIMIT
            print(f"No limit specified. Using safety cap: {limit} pages")
        
        all_data = []
        
        # 2. Scrape Loop
        for page in range(limit):
            # A. Ensure page is ready
            if page > 0:
                self._ensure_page_actionable()
                time.sleep(DOM_IDLE_MS / 1000)

            # B. Get Table ID (Re-fetch every loop)
            table_id = self._get_object_id(table_xpath)
            if not table_id:
                print(f"Table not found on page {page + 1}. Stopping.")
                break
                
            # C. Scrape Data (JS)
            scraper_js = """
            function() {
                const table = this;
                const data = [];
                const headers = [];
                
                // Headers
                let headerCells = table.querySelectorAll('thead th');
                if (headerCells.length === 0) headerCells = table.querySelectorAll('tr:first-child th');
                headerCells.forEach(th => headers.push(th.innerText.trim()));
                
                // Rows
                let rows = table.querySelectorAll('tbody tr');
                if (rows.length === 0) rows = table.querySelectorAll('tr');
                
                for (const row of rows) {
                    if (row.querySelector('th')) continue;
                    const cells = row.querySelectorAll('td');
                    if (cells.length === 0) continue;
                    
                    const rowObj = {};
                    
                    cells.forEach((cell, i) => {
                        // FIX: Use double backslash \\n so Python sends \n to JS
                        const txt = cell.innerText.trim().replace(/\\n/g, ' ');
                        
                        if (headers[i]) {
                            rowObj[headers[i]] = txt;
                        } else {
                            rowObj[`column_${i}`] = txt;
                        }
                    });
                    
                    data.push(rowObj);
                }
                return data;
            }
            """
            
            msg_id = self._send("Runtime.callFunctionOn", {
                "objectId": table_id,
                "functionDeclaration": scraper_js,
                "returnByValue": True
            })
            
            #Receive Response
            response = self._recv(msg_id)

            #Error Handling (had previous failures here)
            if "exceptionDetails" in response["result"]:
                # Print the JS error description
                error_msg = response["result"]["exceptionDetails"]["exception"]["description"]
                print(f"JS Error in scrape_table: {error_msg}")
                # Return empty list or break
                break

            # 4. Extract Data safely
            page_data = response["result"]["result"]["value"]
            
            all_data.extend(page_data)
            print(f"Scraped {len(page_data)} rows from page {page + 1}")
            
            # D. Handle Pagination
            if not next_page_xpath:
                break

            # Stop if we reached our calculated limit
            if page >= limit - 1:
                print("Reached calculated page limit. Stopping.")
                break

            try:
                next_id = self._get_object_id(next_page_xpath)
                if not next_id:
                    print("Pagination 'Next' button hidden. Stopping.")
                    break
                
                is_disabled_msgid = self._send("Runtime.callFunctionOn", {
                    "objectId": next_id,
                    "functionDeclaration": "function() { return this.disabled || this.classList.contains('disabled') || this.getAttribute('aria-disabled') === 'true'; }",
                    "returnByValue": True
                })
                
                response = self._recv(is_disabled_msgid)

                # Safety Check 1: Did JS execution fail?
                if "exceptionDetails" in response.get("result", {}):
                    print(f"Pagination JS Error: {response['result']['exceptionDetails']}")
                    break
                    
                # Safety Check 2: Safe Value Extraction (Fixes KeyError)
                # If 'value' is missing, default to False (assume enabled)
                result_obj = response.get("result", {}).get("result", {})
                is_disabled = result_obj.get("value", False)

                if is_disabled:
                    print("Pagination 'Next' button is disabled. Stopping.")
                    break
                    
                print(f"Navigating to table page {page + 2}...")
                self.click(next_page_xpath)
                
            except Exception as e:
                print(f"Pagination failed: {e}")
                break
                
        return all_data
    

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
        # 1. Get Stable Reference (first visible element)
        obj_id = self._get_object_id(xpath)
        if not obj_id:
            return False # Or raise Error if you prefer strictness

        # 2. Check state directly on the object
        result = self._send("Runtime.callFunctionOn", {
            "objectId": obj_id,
            "functionDeclaration": "function() { return this.checked; }",
            "returnByValue": True
        })
        return result["result"]["result"]["value"] is True
    
    def is_selected(self, xpath: str) -> bool:
        # 1. Get Stable Reference
        obj_id = self._get_object_id(xpath)
        if not obj_id:
            return False

        # 2. Check state directly on the object
        result = self._send("Runtime.callFunctionOn", {
            "objectId": obj_id,
            "functionDeclaration": "function() { return this.selected; }",
            "returnByValue": True
        })
        return result["result"]["result"]["value"] is True


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

        # 1. Get Stable Reference (Visible <select>)
        obj_id = self._get_object_id(select_xpath)
        if not obj_id:
            raise RuntimeError(f"Select element not found or hidden: {select_xpath}")

        # 2. Scroll into view (Ensures visibility for event bubbling)
        self._send("DOM.scrollIntoViewIfNeeded", {"objectId": obj_id})

        # 3. Execute Selection Logic on the ID
        expr = f"""
        function() {{
            const select = this;
            let option = null;
            
            if ({json.dumps(value)} !== null) {{
                option = [...select.options].find(o => o.value === {json.dumps(value)});
            }} else if ({json.dumps(label)} !== null) {{
                option = [...select.options].find(o => o.text.trim() === {json.dumps(label)});
            }} else if ({index} !== null) {{
                option = select.options[{index}];
            }}

            if (!option) return false;

            select.value = option.value;
            option.selected = true;

            select.dispatchEvent(new Event('input', {{ bubbles: true }}));
            select.dispatchEvent(new Event('change', {{ bubbles: true }}));
            return true;
        }}
        """

        msg_id = self._send("Runtime.callFunctionOn", {
            "objectId": obj_id,
            "functionDeclaration": expr,
            "returnByValue": True
        })
        
        result = self._recv(msg_id)["result"]["result"]
        if result.get("value") is not True:
            raise RuntimeError(f"Option not found (Value: {value}, Label: {label}, Index: {index})")

    def select_custom_option(self, trigger_xpath: str, option_text: str):
        """
        Selects an item from a modern dropdown using a 'Best Match' scoring system.
        Prioritizes Exact Matches and Semantic Tags (li, role=option) over generic text.
        """
        self._ensure_page_actionable()

        # Step 1: Open Dropdown
        print(f"Clicking dropdown trigger: {trigger_xpath}")
        self.click(trigger_xpath)
        time.sleep(UI_DELAY) 

        # Step 2: Find Best Option using Scoring Logic
        expr = f"""
        (function() {{
            const query = {json.dumps(option_text)}.toLowerCase();
            const candidates = document.querySelectorAll('li, [role="option"], div, span, a, .item, .option');
            
            let bestEl = null;
            let bestScore = -1;
            
            for (const el of candidates) {{
                // 1. Strict Visibility Check
                const rect = el.getBoundingClientRect();
                const style = window.getComputedStyle(el);
                if (rect.width < 5 || rect.height < 5 || 
                    style.visibility === 'hidden' || style.display === 'none' || 
                    style.opacity === '0') continue;
                
                const text = el.innerText.toLowerCase().trim();
                if (!text.includes(query)) continue;
                
                // --- SCORING SYSTEM ---
                let score = 0;
                
                // Rule A: Exact Match is King (Score: +100)
                if (text === query) score += 100;
                
                // Rule B: Semantic Tags are Queen (Score: +50)
                // Prefer actual list items over generic divs
                if (el.tagName === 'LI' || el.getAttribute('role') === 'option') score += 50;
                
                // Rule C: Penalize "Wrapper" Containers (Score: -1000)
                // If a div contains the text but also 50 other characters, it's likely a parent, not the button.
                if (text.length > query.length + 50) score -= 1000;
                
                // Update Best Candidate
                if (score > bestScore) {{
                    bestScore = score;
                    bestEl = el;
                }}
            }}
            return bestEl;
        }})()
        """

        # 3. Get ID
        msg_id = self._send("Runtime.evaluate", {
            "expression": expr, 
            "returnByValue": False 
        })
        result = self._recv(msg_id)
        remote_obj = result["result"]["result"]
        
        if remote_obj.get("subtype") == "null" or "objectId" not in remote_obj:
            raise RuntimeError(f"Option '{option_text}' not found (or visible) after clicking trigger.")

        option_id = remote_obj["objectId"]

        # 4. Click
        self._send("DOM.scrollIntoViewIfNeeded", {"objectId": option_id})
        point = self._get_center_by_id(option_id)
        
        if point:
            self.mouse_move(point["x"], point["y"])
            self.mouse_down(point["x"], point["y"])
            self.mouse_up(point["x"], point["y"])
        else:
            self._send("Runtime.callFunctionOn", {
                "functionDeclaration": "function() { this.click(); }",
                "objectId": option_id
            })
            
        time.sleep(STEP_DELAY)

    def select_autocomplete_option(self, input_xpath: str, select_text: str):
        """
        Simpler Autocomplete:
        1. Focuses input.
        2. Types 'select_text' one char at a time.
        3. After EACH char, checks if 'select_text' option is visible.
        4. If found, clicks immediately and stops typing.
        """
        self._ensure_page_actionable()

        #1: Get Stable Reference & Focus
        obj_id = self._get_object_id(input_xpath)
        if not obj_id:
            raise RuntimeError(f"Autocomplete input not found: {input_xpath}")

        print(f"Focusing input: {input_xpath}")
        self._send("DOM.scrollIntoViewIfNeeded", {"objectId": obj_id})
        self._send("Runtime.callFunctionOn", {
            "functionDeclaration": "function() { this.focus(); this.value = ''; }",
            "objectId": obj_id
        })

        #2: Type and Check Loop        
        check_js = f"""
        (function() {{
            const query = {json.dumps(select_text)}.toLowerCase().trim();
            // Search all potential list items
            const candidates = document.querySelectorAll('li, [role="option"], div, span, a, .item, .option');
            
            for (const el of candidates) {{
                // 1. Visibility Check
                const style = window.getComputedStyle(el);
                if (style.display === 'none' || style.visibility === 'hidden' || style.opacity === '0') continue;
                const rect = el.getBoundingClientRect();
                if (rect.width < 5 || rect.height < 5) continue;

                // 2. Text Match
                const text = el.innerText.toLowerCase().trim();
                
                // We want the element that IS the option, not just contains it
                // So we check if the text matches closely
                if (text === query || (text.includes(query) && text.length < query.length + 30)) {{
                    return true;
                }}
            }}
            return false;
        }})()
        """

        found = False
        print(f"Typing '{select_text}'...")

        for i, char in enumerate(select_text):
            # A. Type the character
            self._send("Input.dispatchKeyEvent", {"type": "keyDown", "key": char})
            self._send("Input.dispatchKeyEvent", {"type": "char", "text": char})
            self._send("Input.dispatchKeyEvent", {"type": "keyUp", "key": char})
            
            # B. Small delay for JS to react
            time.sleep(AUTO_DELAY) 

            # C. Check if target appeared (Start checking after 2nd char to save resources)
            if i >= 1: 
                msg_id = self._send("Runtime.evaluate", {"expression": check_js})
                if self._recv(msg_id)["result"]["result"]["value"]:
                    print(f"Target '{select_text}' appeared! Stopping input.")
                    found = True
                    break
        
        # 3: Click the result
        if not found:
            # wait one last second
            time.sleep(1.0)
            
        self._select_visible_option(select_text)


    def _select_visible_option(self, option_text):
        """
        Helper: Finds and clicks the best matching visible option.
        """
        # Find Best Option using Scoring Logic
        expr = f"""
        (function() {{
            const query = {json.dumps(option_text)}.toLowerCase().trim();
            const candidates = document.querySelectorAll('li, [role="option"], div, span, a, .item, .option');
            
            let bestEl = null;
            let bestScore = -1;
            
            for (const el of candidates) {{
                // Visibility
                const rect = el.getBoundingClientRect();
                const style = window.getComputedStyle(el);
                if (rect.width < 5 || rect.height < 5 || 
                    style.visibility === 'hidden' || style.display === 'none' || 
                    style.opacity === '0') continue;
                
                const text = el.innerText.toLowerCase().trim();
                if (!text.includes(query)) continue;
                
                // Scoring
                let score = 0;
                if (text === query) score += 100; // Exact match
                if (el.tagName === 'LI' || el.getAttribute('role') === 'option') score += 50; // Semantic
                if (text.length > query.length + 50) score -= 1000; // Penalty for wrappers
                if (el.querySelector('.highlight') || el.classList.contains('highlight')) score += 20;

                if (score > bestScore) {{
                    bestScore = score;
                    bestEl = el;
                }}
            }}
            return bestEl;
        }})()
        """

        msg_id = self._send("Runtime.evaluate", {"expression": expr, "returnByValue": False})
        result = self._recv(msg_id)
        remote_obj = result["result"]["result"]
        
        if remote_obj.get("subtype") == "null" or "objectId" not in remote_obj:
            raise RuntimeError(f"Option '{option_text}' not found.")

        option_id = remote_obj["objectId"]

        # Robust Click via ID
        self._send("DOM.scrollIntoViewIfNeeded", {"objectId": option_id})
        point = self._get_center_by_id(option_id)
        
        if point:
            self.mouse_move(point["x"], point["y"])
            self.mouse_down(point["x"], point["y"])
            self.mouse_up(point["x"], point["y"])
        else:
            self._send("Runtime.callFunctionOn", {
                "functionDeclaration": "function() { this.click(); }",
                "objectId": option_id
            })

    def multi_select(self, select_xpath: str, values: list[str]):
        self._ensure_page_actionable()

        # 1. Get Stable Reference
        obj_id = self._get_object_id(select_xpath)
        if not obj_id:
             raise RuntimeError(f"Multi-select element not found or hidden: {select_xpath}")

        # 2. Scroll
        self._send("DOM.scrollIntoViewIfNeeded", {"objectId": obj_id})

        # 3. Execute on ID
        expr = f"""
        function() {{
            const select = this;
            if (!select.multiple) return false;

            const values = {json.dumps(values)};
            let foundAny = false;
            
            for (const option of select.options) {{
                if (values.includes(option.value) || values.includes(option.text.trim())) {{
                    option.selected = true;
                    foundAny = true;
                }} else {{
                    option.selected = false;
                }}
            }}
            
            if (foundAny) {{
                select.dispatchEvent(new Event('input', {{ bubbles: true }}));
                select.dispatchEvent(new Event('change', {{ bubbles: true }}));
            }}
            return true;
        }}
        """

        msg_id = self._send("Runtime.callFunctionOn", {
            "objectId": obj_id,
            "functionDeclaration": expr,
            "returnByValue": True
        })
        
        result = self._recv(msg_id)["result"]["result"]
        if result.get("value") is not True:
            raise RuntimeError("Multi-select failed or element was not multiple")


    # --------------- Box Model Design ----------------
    def _get_center_via_box_model(self, xpath):
        """
        Backup Method: Uses native CDP DOM.getBoxModel to calculate geometry.
        This bypasses JavaScript coordinate calculations.
        """
        # 1. Get the ObjectId of the FIRST VISIBLE match
        # We cannot just use DOM.getDocument because that finds hidden nodes.
        # We use Runtime.evaluate to filter, but return the HANDLE (objectId), not the value.
        expr = f"""
        (function () {{
            const snapshot = document.evaluate("{xpath}", document, null,
                XPathResult.ORDERED_NODE_SNAPSHOT_TYPE, null);
            
            for (let i = 0; i < snapshot.snapshotLength; i++) {{
                const el = snapshot.snapshotItem(i);
                const style = window.getComputedStyle(el);
                const rect = el.getBoundingClientRect();
                
                if (rect.width > 0 && style.display !== 'none' && style.visibility !== 'hidden') {{
                    return el; // Return the actual DOM Node
                }}
            }}
            return null;
        }})()
        """
        
        # returnByValue=False gives us the objectId reference instead of JSON
        msg_id = self._send("Runtime.evaluate", {
            "expression": expr, 
            "returnByValue": False 
        })
        result = self._recv(msg_id)
        
        # Check if we got a valid object back
        remote_obj = result["result"]["result"]
        if remote_obj.get("subtype") == "null" or "objectId" not in remote_obj:
            return None

        object_id = remote_obj["objectId"]

        try:
            # 2. Ask Chrome for the Box Model of this specific object
            box_id = self._send("DOM.getBoxModel", {"objectId": object_id})
            box_result = self._recv(box_id)
            
            if "error" in box_result:
                print(f"DEBUG: BoxModel Error: {box_result['error']['message']}")
                return None
            
            # 3. Calculate Center from 'content' Quad
            # Quad is [x1, y1, x2, y2, x3, y3, x4, y4] (TopLeft, TopRight, BottomRight, BottomLeft)
            quad = box_result["result"]["model"]["content"]
            
            # Average the X and Y coordinates to find the center
            x = (quad[0] + quad[2] + quad[4] + quad[6]) / 4
            y = (quad[1] + quad[3] + quad[5] + quad[7]) / 4
            
            return {"x": x, "y": y}
            
        except Exception as e:
            print(f"DEBUG: BoxModel Exception: {e}")
            return None
        

    def _get_object_id(self, xpath):
        """
        Resolves an XPath to a specific Chrome Remote Object ID.
        This handle survives DOM movements (like sticky headers).
        """
        expr = f"""
        (function () {{
            const snapshot = document.evaluate("{xpath}", document, null,
                XPathResult.ORDERED_NODE_SNAPSHOT_TYPE, null);
            
            for (let i = 0; i < snapshot.snapshotLength; i++) {{
                const el = snapshot.snapshotItem(i);
                const style = window.getComputedStyle(el);
                const rect = el.getBoundingClientRect();
                
                // Return the first VISIBLE match
                if (rect.width > 0 && style.display !== 'none' && style.visibility !== 'hidden') {{
                    return el; 
                }}
            }}
            return null;
        }})()
        """
        msg_id = self._send("Runtime.evaluate", {
            "expression": expr, 
            "returnByValue": False  # CRITICAL: Returns pointer, not data
        })
        result = self._recv(msg_id)
        
        remote_obj = result["result"]["result"]
        if remote_obj.get("subtype") == "null" or "objectId" not in remote_obj:
            return None
            
        return remote_obj["objectId"]
    
    def _get_center_by_id(self, object_id):
        """
        Calculates center (x, y) using the stable Object ID.
        """
        try:
            box_data = self._send("DOM.getBoxModel", {"objectId": object_id})
            box_result = self._recv(box_data)
            
            if "error" in box_result:
                return None

            quad = box_result["result"]["model"]["content"]
            # Quad is [x1,y1, x2,y2, x3,y3, x4,y4]
            x = (quad[0] + quad[2] + quad[4] + quad[6]) / 4
            y = (quad[1] + quad[3] + quad[5] + quad[7]) / 4
            return {"x": x, "y": y}
        except Exception:
            return None

    # ---------------- Discovery Helpers ----------------    
    def find_elements_by_text(self, query: str):
        """
        Scans the DOM for visible elements matching the query.
        Matches against: Text, ID, Name, Class, Title, Aria-Label, and Role.
        Returns a 'Rich Fingerprint' of attributes for the LLM to analyze.
        """
        js_script = f"""
        (function() {{
            const query = {json.dumps(query)}.toLowerCase().trim();
            const candidates = [];
            
            // BROAD SELECTOR: Matches anything likely to be interactive
            // - Standard: input, button, a, select, textarea
            // - Accessibility: [role="button"], [role="link"], [role="menuitem"]
            // - JavaScript: [onclick] (catches your pagination li!)
            // - CSS Naming: Elements with 'btn', 'button', 'icon', 'arrow' in their class
            const selectors = `
                input, button, a, textarea, select, 
                [role="button"], [role="link"], [role="menuitem"], [role="tab"],
                [onclick], 
                [class*="btn"], [class*="button"], [class*="icon"], [class*="arrow"], [class*="pager"], [class*="pagination"]
            `;
            
            document.querySelectorAll(selectors).forEach(el => {{
                // 1. Strict Visibility Check
                const rect = el.getBoundingClientRect();
                const style = window.getComputedStyle(el);
                if (rect.width < 1 || rect.height < 1 || style.visibility === 'hidden' || style.display === 'none') return;
                
                // 2. Gather All Searchable Attributes
                const text = (el.innerText || '').toLowerCase();
                const val = (el.value || '').toLowerCase();
                const ph = (el.getAttribute('placeholder') || '').toLowerCase();
                const name = (el.getAttribute('name') || '').toLowerCase();
                const id = (el.id || '').toLowerCase();
                const aria = (el.getAttribute('aria-label') || '').toLowerCase();
                const title = (el.getAttribute('title') || '').toLowerCase();
                const className = (el.className || '').toLowerCase(); 
                const role = (el.getAttribute('role') || '').toLowerCase();
                
                // 3. The Match Logic (Does ANY field contain the query?)
                if (text.includes(query) || val.includes(query) || ph.includes(query) || 
                    name.includes(query) || id.includes(query) || aria.includes(query) ||
                    title.includes(query) || className.includes(query) || role.includes(query)) {{
                    
                    // 4. Generate Robust XPath
                    let xpath = '';
                    if (el.id) {{
                        xpath = `//*[@id='${{el.id}}']`;
                    }} else {{
                        // Generate a path based on hierarchy if no ID
                        // (Simplified logic for brevity, matches your existing pattern)
                        const tag = el.tagName.toLowerCase();
                        if (el.innerText && el.innerText.trim().length > 0 && el.innerText.trim().length < 50) {{
                            const cleanText = el.innerText.trim().replace(/'/g, "");
                            xpath = `//${{tag}}[contains(normalize-space(.), '${{cleanText}}')]`;
                        }} else if (el.getAttribute('name')) {{
                            xpath = `//${{tag}}[@name='${{el.getAttribute('name')}}']`;
                        }} else if (el.className) {{
                             // Fallback to class match if unique-ish
                            const cleanClass = el.className.trim().split(' ')[0]; # Take first class
                            if (cleanClass) xpath = `//${{tag}}[contains(@class, '${{cleanClass}}')]`;
                        }}
                        
                        // Absolute fallback if we couldn't make a nice relative path
                        if (!xpath) {{
                             xpath = `//${{tag}}`; // Warning: This is vague, but the loop usually finds better attributes
                        }}
                    }}
                    
                    // 5. Return The "Whole Shebang" (Rich Attributes)
                    candidates.push({{
                        tag: el.tagName.toLowerCase(),
                        text: (el.innerText || el.value || '').trim().substring(0, 50),
                        xpath: xpath,
                        attributes: {{
                            id: el.id,
                            class: el.className,
                            title: el.getAttribute('title'),
                            role: el.getAttribute('role'),
                            type: el.getAttribute('type'),
                            'aria-label': el.getAttribute('aria-label'),
                            onclick: el.hasAttribute('onclick') ? 'true' : 'false'
                        }}
                    }});
                }}
            }});
            return candidates;
        }})()
        """
        msg_id = self._send("Runtime.evaluate", {"expression": js_script, "returnByValue": True})
        return self._recv(msg_id)["result"]["result"]["value"]

    def get_all_interactive_elements(self, tag_name: str = "button"):
        """
        Returns a list of ALL visible elements of a specific type.
        """
        js_script = f"""
        (function() {{
            const results = [];
            let selector = '{tag_name}';
            if ('{tag_name}' === 'button') selector = 'button, input[type="button"], input[type="submit"], [role="button"]';
            if ('{tag_name}' === 'input') selector = 'input:not([type="hidden"])';
            
            document.querySelectorAll(selector).forEach(el => {{
                const rect = el.getBoundingClientRect();
                const style = window.getComputedStyle(el);
                if (rect.width === 0 || style.visibility === 'hidden' || style.display === 'none') return;
                
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
        msg_id = self._send("Runtime.evaluate", {"expression": js_script, "returnByValue": True})
        return self._recv(msg_id)["result"]["result"]["value"]

    # ---------------- Clean Up Tool ----------------
    def _clean_old_profiles(self, max_age_seconds=300):        
        pattern = os.path.join(USER_DATA_DIR, "cdp-profile-*")
        
        now = time.time()
        
        try:
            for profile_path in glob.glob(pattern):
                try:
                    # Check modification time
                    mtime = os.path.getmtime(profile_path)
                    
                    if now - mtime > max_age_seconds:
                        shutil.rmtree(profile_path, ignore_errors=True)
                        # print(f"Janitor: Cleaned old profile {profile_path}")
                except Exception:
                    # Ignore permission errors (file in use)
                    pass
        except Exception:
            pass