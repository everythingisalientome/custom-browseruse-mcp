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
FULLSCREEN = os.getenv("RUN_WEB_FULLSCREEN", "0") == "1"

CHROME_PATH = find_chrome_executable()
DEBUG_PORT = 9222
## Removing global entry for user data dir to create a fresh one each time
#USER_DATA_DIR = tempfile.mkdtemp(prefix="cdp-profile-", dir="C:\\Users\\PreetPragyan\\temp")
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
        self.user_data_dir = tempfile.mkdtemp(prefix="cdp-profile-", dir="C:\\Users\\PreetPragyan\\temp")

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
            #"--window-size=1920,1080"
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
        self.force_viewport(1920, 1080)
        if FULLSCREEN:
            self._set_fullscreen()

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
                self.process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                self.process.kill()
                
        except Exception:
            pass
        
        self.process = None
        
        # Wait a little for file locks to release
        time.sleep(0.5)
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
            time.sleep(0.2)
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

    def wait_for_element(self, xpath, timeout_ms=5000):
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
                    time.sleep(0.1)
                    continue
                    
                idle_time = result["result"]["result"]["value"]
                if idle_time >= idle_ms:
                    return True
                    
            except Exception:
                # Ignore transient errors during page loads/navs
                pass

            time.sleep(0.1)

        raise TimeoutError("DOM did not stabilize")

    def wait_for_network_idle(self, timeout_ms=5000, idle_ms=500):
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
            time.sleep(0.05)
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

    def hover(self, xpath, timeout_ms=10000):
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
        time.sleep(0.05)
        self.mouse_move(point["x"], point["y"])
        
        # 5. Synthetic Fallback (using the ID directly)
        self._dispatch_synthetic_hover_on_id(object_id)
        
        time.sleep(0.5) # Allow hover effects to take hold

    def mouse_move(self, x, y):
        self._send("Input.dispatchMouseEvent", {
            "type": "mouseMoved",
            "x": x,
            "y": y,
            "buttons": 0
        })

    def double_click(self, xpath, timeout_ms=10000):
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

    def drag_and_drop(self, source_xpath, target_xpath, timeout_ms=10000):
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
            time.sleep(0.2) # Small drag delay
            self.mouse_move(tgt["x"], tgt["y"])
            time.sleep(0.2)
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

    def fill(self, xpath: str, value: str, timeout_ms: int = 10000):

        entry = None
        if self.tracer.enabled:
            entry = self.tracer.start_step(action="fill", target=xpath, params={"value": value})

        deadline = time.monotonic() + timeout_ms / 1000
        
        try:            
            while time.monotonic() < deadline:
                try:
                    self._ensure_page_actionable(timeout_ms=3000)
                    self.wait_for_element(xpath, timeout_ms=2000)
                    
                    # 1. Get Stable Reference
                    obj_id = self._get_object_id(xpath)
                    if not obj_id: raise RuntimeError("Object ID lookup failed")

                    # 2. Scroll
                    self._send("DOM.scrollIntoViewIfNeeded", {"objectId": obj_id})

                    # 3. Focus (Using Runtime.callFunctionOn)
                    self._send("Runtime.callFunctionOn", {
                        "functionDeclaration": "function() { this.focus(); }",
                        "objectId": obj_id
                    })

                    # 4. Clear (Using Runtime.callFunctionOn)
                    self._send("Runtime.callFunctionOn", {
                        "functionDeclaration": "function() { this.value = ''; this.dispatchEvent(new Event('input', {bubbles:true})); }",
                        "objectId": obj_id
                    })

                    # 5. Type (Keystrokes go to focused element)
                    for ch in value:
                        self._send("Input.dispatchKeyEvent", { "type": "char", "text": ch })

                    if entry: self.tracer.success(entry)
                    return

                except Exception:
                    if entry: self.tracer.record_retry(entry)
                    time.sleep(0.1)

            raise TimeoutError(f"Fill timed out for xpath: {xpath}")

        except Exception as e:
            if entry:
                self.tracer.failure(entry, e)
                self._capture_failure_artifacts(entry)
                self.tracer.dump()
            raise



    def click(self, xpath, timeout_ms=10000):
        entry = self.tracer.start_step(action="click", target=xpath) if self.tracer.enabled else None
        deadline = time.monotonic() + timeout_ms / 1000
        try:
            while time.monotonic() < deadline:
                try:
                    self._ensure_page_actionable(timeout_ms=3000)
                    self.wait_for_element(xpath, timeout_ms=2000)
                    
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
                    time.sleep(0.1)

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

    def _ensure_page_actionable(self, timeout_ms=15000):
        """
        Robustly waits for the page to be fully loaded and stable.
        Checks:
        1. document.readyState == 'complete'
        2. Network is idle (no active requests > 500ms)
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
                    time.sleep(0.1)
                    continue

                # 2. Network Idle Check (Crucial for SPAs)
                # We use a short timeout (2s) for the check itself, but require 500ms of silence.
                try:
                    self.wait_for_network_idle(timeout_ms=2000, idle_ms=500)
                except TimeoutError:
                    # If network is busy, loop back and wait more (unless global deadline hit)
                    if time.monotonic() > deadline: raise
                    continue

                # 3. DOM Stability Check (Crucial for Hydration/Animations)
                # Waits for the HTML to stop shifting/growing for 500ms.
                try:
                    self.wait_for_dom_stable(timeout_ms=2000, idle_ms=500)
                except TimeoutError:
                    if time.monotonic() > deadline: raise
                    continue

                # If we passed all 3 gauntlets, the page is truly ready.
                return

            except Exception:
                # Ignore transient errors (e.g., context destroyed during nav)
                time.sleep(0.1)

        raise TimeoutError(f"Page failed to stabilize within {timeout_ms}ms")

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


    #multi select
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
        Scans the DOM for visible elements matching the query (text, id, name, etc).
        Returns a list of dictionaries with element details.
        """
        js_script = f"""
        (function() {{
            const query = {json.dumps(query)}.toLowerCase();
            const candidates = [];
            
            const selectors = 'input, button, a, textarea, select, [role="button"]';
            document.querySelectorAll(selectors).forEach(el => {{
                // 1. Check Visibility
                const rect = el.getBoundingClientRect();
                const style = window.getComputedStyle(el);
                if (rect.width === 0 || style.visibility === 'hidden' || style.display === 'none') return;
                
                // 2. Check Match
                const text = (el.innerText || '').toLowerCase();
                const val = (el.value || '').toLowerCase();
                const ph = (el.getAttribute('placeholder') || '').toLowerCase();
                const name = (el.getAttribute('name') || '').toLowerCase();
                const id = (el.id || '').toLowerCase();
                const aria = (el.getAttribute('aria-label') || '').toLowerCase();
                
                if (text.includes(query) || val.includes(query) || ph.includes(query) || 
                    name.includes(query) || id.includes(query) || aria.includes(query)) {{
                    
                    let xpath = '';
                    if (el.id) {{
                        xpath = `//*[@id='${{el.id}}']`;
                    }} else {{
                        const tag = el.tagName.toLowerCase();
                        if (el.innerText) {{
                            const cleanText = el.innerText.trim().substring(0, 30).replace(/'/g, "");
                            xpath = `//${{tag}}[contains(normalize-space(.), '${{cleanText}}')]`;
                        }} else if (el.getAttribute('name')) {{
                            xpath = `//${{tag}}[@name='${{el.getAttribute('name')}}']`;
                        }} else if (el.getAttribute('placeholder')) {{
                            xpath = `//${{tag}}[@placeholder='${{el.getAttribute('placeholder')}}']`;
                        }} else {{
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
        base_dir = r"C:\Users\PreetPragyan\temp" # Your specific temp path
        pattern = os.path.join(base_dir, "cdp-profile-*")
        
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