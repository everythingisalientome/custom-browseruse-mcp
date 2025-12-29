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
os.environ["NO_PROXY"] = "127.0.0.1,localhost"
os.environ["no_proxy"] = "127.0.0.1,localhost"


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

def find_free_port():
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("localhost", 0))
        return s.getsockname()[1]


TRACE_ENABLED = os.getenv("WEB_MCP_TRACE", "0") == "1"
SCREENSHOT_ON_FAIL = os.getenv("WEB_MCP_SCREENSHOT_ON_FAIL", "0") == "1" # Ensure this is '1' or 'true' in .env

# --- CONFIGURATION CONSTANTS ---
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

# Delays (Seconds)
HUMAN_DELAY = int(os.getenv("HUMAN_KEY_DELAY", "100")) / 1000.0
AUTO_DELAY = int(os.getenv("AUTOCOMPLETE_TYPE_DELAY", "100")) / 1000.0
UI_DELAY = int(os.getenv("UI_ANIMATION_DELAY", "500")) / 1000.0
STEP_DELAY = int(os.getenv("ACTION_STEP_DELAY", "200")) / 1000.0

# Viewport
VIEWPORT_WIDTH = int(os.getenv("VIEWPORT_WIDTH", "1920"))
VIEWPORT_HEIGHT = int(os.getenv("VIEWPORT_HEIGHT", "1080"))

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
        self._inflight_requests = 0
        self.tracer = TraceManager(enabled=TRACE_ENABLED)
        self.input_ready = False
        self._clean_old_profiles()
        self.user_data_dir = tempfile.mkdtemp(prefix="cdp-profile-", dir=USER_DATA_DIR)

    def _save_debug_screenshot(self, prefix="error"):
        """
        Universal helper to save a screenshot on any failure.
        Saves to traces/error_{prefix}_{timestamp}.png
        """
        if not SCREENSHOT_ON_FAIL:
            return

        try:
            timestamp = int(time.time())
            filename = f"traces/error_{prefix}_{timestamp}.png"
            os.makedirs("traces", exist_ok=True)
            
            img_data = self.screenshot(full_page=True)
            with open(filename, "wb") as f:
                f.write(img_data)
            print(f"📸 Captured error screenshot: {filename}")
        except Exception as e:
            print(f"Failed to capture error screenshot: {e}")

    def _capture_failure_artifacts(self, entry):
        # Existing tracer logic for fill/click
        if not SCREENSHOT_ON_FAIL:
            return

        ts = entry["step"]
        try:
            img = self.screenshot(full_page=True)
            shot_path = f"traces/step_{ts}.png"
            with open(shot_path, "wb") as f:
                f.write(img)
            self.tracer.attach_artifact(entry, "screenshot", shot_path)
        except Exception:
            pass
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
        if self.process: return

        args = [
            CHROME_PATH,
            f"--remote-debugging-port={DEBUG_PORT}",
            "--remote-debugging-address=127.0.0.1",
            f"--user-data-dir={self.user_data_dir}",
            "--remote-allow-origins=*",
            "--disable-extensions",
            "--disable-infobars",
            "--disable-features=TranslateUI,PasswordCheck,PasswordLeakDetection,PasswordManagerOnboarding,AutofillServerCommunication",
            "--no-first-run",
            "--no-default-browser-check",
            "--disable-save-password-bubble",
            "--password-store=basic",
            "--use-mock-keychain",
            "--disable-notifications",
            "--disable-popup-blocking",
        ]
        
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
            creationflags=subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0
        )

        self.http = requests.Session()
        self.http.trust_env = False
        self.http.proxies = {"http": None, "https": None}

        self._wait_for_cdp()
        r = self.http.get(f"http://localhost:{DEBUG_PORT}/json/new", timeout=1)
        print(f"New Tab Response: {r.status_code}")

        self._connect_ws()
        self._enable_domains()
        self.force_viewport(VIEWPORT_WIDTH, VIEWPORT_HEIGHT)

    def close(self):
        if not self.process: return
        try:
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
        time.sleep(UI_DELAY)
        try:
            shutil.rmtree(self.user_data_dir, ignore_errors=True)
            print(f"Cleaned up profile: {self.user_data_dir}")
        except Exception as e:
            print(f"Warning: Could not delete profile {self.user_data_dir}: {e}")

    def _wait_for_cdp(self, timeout=10):
        start = time.time()
        while time.time() - start < timeout:
            if self.process and self.process.poll() is not None:
                raise RuntimeError("Chrome process exited unexpectedly")
            try:
                r = self.http.get(f"http://localhost:{DEBUG_PORT}/json/version", timeout=0.5)
                if r.status_code == 200: return
            except Exception:
                pass
            time.sleep(STEP_DELAY)
        raise RuntimeError("CDP endpoint not available")

    def _connect_ws(self, attempts=5, delay=0.2):
        last_error = None
        for i in range(attempts):
            try:
                resp = self.http.get(f"http://localhost:{DEBUG_PORT}/json", timeout=2)
                targets = resp.json() if resp.ok else []
                pages = [t for t in targets if isinstance(t, dict) and t.get("type") == "page"]
                
                if pages and pages[0].get("webSocketDebuggerUrl"):
                    ws_url = pages[0]["webSocketDebuggerUrl"]
                    self.ws = websocket.WebSocket()
                    try:
                        self.ws.connect(ws_url, timeout=5)
                        self.ws.settimeout(1)
                        print(f"Connected to target: {pages[0]['id']}")
                        return
                    except Exception as e:
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

    def _recv(self, msg_id, timeout=None):
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
                # Handle async events (network tracking)
                if msg.get("method") in ("Network.requestWillBeSent", "Network.responseReceived", "Network.loadingFinished", "Network.loadingFailed"):
                    with self._lock:
                        if msg["method"] == "Network.requestWillBeSent":
                            self._inflight_requests += 1
                        elif msg["method"] in ("Network.loadingFinished", "Network.loadingFailed"):
                            self._inflight_requests = max(0, self._inflight_requests - 1)
                continue
            if msg.get("id") == msg_id:
                return msg

    def _enable_domains(self):
        for domain in ["Page", "DOM", "CSS", "Runtime", "Input", "Network"]:
            self._send(f"{domain}.enable")
        self._send("Page.setLifecycleEventsEnabled", {"enabled": True})
        self._send("Page.bringToFront")
        self._send("Network.setCacheDisabled", {"cacheDisabled": True})

    def _parse_key_combo(self, combo: str):
        parts = combo.split("+")
        modifiers = {"Control": False, "Shift": False, "Alt": False, "Meta": False}
        key_part = None
        for p in parts:
            p = p.strip()
            if p in ["Ctrl", "Control"]: modifiers["Control"] = True
            elif p == "Shift": modifiers["Shift"] = True
            elif p == "Alt": modifiers["Alt"] = True
            elif p in ["Meta", "Cmd"]: modifiers["Meta"] = True
            else: key_part = p
        return modifiers, key_part

    def force_viewport(self, width=1920, height=1080):
        try:
            msg_id = self._send("Browser.getWindowForTarget")
            result = self._recv(msg_id)["result"]
            self._send("Browser.setWindowBounds", {
                "windowId": result["windowId"],
                "bounds": {"width": width, "height": height, "windowState": "normal"}
            })
            print(f"Viewport forced to {width}x{height}")
        except Exception as e:
            print(f"Failed to force viewport: {e}")

    # ---------------- Page operations ----------------
    def navigate(self, url: str):
        self._send("Page.navigate", {"url": url})

    def get_html(self) -> str:
        msg_id = self._send("Runtime.evaluate", {"expression": "document.documentElement.outerHTML"})
        return self._recv(msg_id)["result"]["result"]["value"]

    # ---------------- Wait helpers ----------------
    def wait_for_element(self, xpath, timeout_ms=DEFAULT_TIMEOUT):
        self.wait_for_dom_stable(timeout_ms)
        deadline = time.monotonic() + timeout_ms / 1000
        expr = f"""
        (function () {{
            const snapshot = document.evaluate("{xpath}", document, null, XPathResult.ORDERED_NODE_SNAPSHOT_TYPE, null);
            for (let i = 0; i < snapshot.snapshotLength; i++) {{
                const el = snapshot.snapshotItem(i);
                const r = el.getBoundingClientRect();
                const s = window.getComputedStyle(el);
                if (r.width > 0 && r.height > 0 && s.visibility !== 'hidden' && s.display !== 'none') return true;
            }}
            return false;
        }})()
        """
        while time.monotonic() < deadline:
            msg_id = self._send("Runtime.evaluate", {"expression": expr})
            if self._recv(msg_id)["result"]["result"]["value"]: return True
            time.sleep(STEP_DELAY)
        raise TimeoutError(f"Element not visible: {xpath}")

    def wait_for_dom_stable(self, timeout_ms=DOM_TIMEOUT, idle_ms=DOM_IDLE_MS):
        deadline = time.monotonic() + timeout_ms / 1000
        expr = """
        (function () {
        if (!window.__domStableTracker) {
            window.__domStableTracker = { last: Date.now() };
            new MutationObserver(() => { window.__domStableTracker.last = Date.now(); }).observe(document, { subtree: true, childList: true, attributes: true });
        }
        return Date.now() - window.__domStableTracker.last;
        })()
        """
        while time.monotonic() < deadline:
            try:
                msg_id = self._send("Runtime.evaluate", {"expression": expr})
                result = self._recv(msg_id)
                if "error" in result.get("result", {}):
                    time.sleep(STEP_DELAY)
                    continue
                if result["result"]["result"]["value"] >= idle_ms:
                    return True
            except Exception:
                pass
            time.sleep(STEP_DELAY)
        raise TimeoutError("DOM did not stabilize")

    def wait_for_network_idle(self, timeout_ms=NETWORK_TIMEOUT, idle_ms=NETWORK_IDLE_MS):
        deadline = time.monotonic() + timeout_ms / 1000
        stable_since = None
        while time.monotonic() < deadline:
            with self._lock: pending = self._inflight_requests
            now = time.monotonic()
            if pending == 0:
                stable_since = stable_since or now
                if (now - stable_since) * 1000 >= idle_ms: return True
            else:
                stable_since = None
            time.sleep(STEP_DELAY)
        raise TimeoutError("Network did not become idle")

    def wait_for_text(self, text: str, timeout_ms: int = DEFAULT_TIMEOUT):
        deadline = time.monotonic() + timeout_ms / 1000
        expr = f"""
        (function () {{
        const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT, null, false);
        while (walker.nextNode()) {{
            const node = walker.currentNode;
            if (node.nodeValue && node.nodeValue.includes({json.dumps(text)})) {{
                const parent = node.parentElement;
                if (parent) {{
                    const style = window.getComputedStyle(parent);
                    if (style && style.visibility !== 'hidden' && style.display !== 'none') return true;
                }}
            }}
        }}
        return false;
        }})()
        """
        while time.monotonic() < deadline:
            msg_id = self._send("Runtime.evaluate", {"expression": expr})
            result = self._recv(msg_id)["result"]["result"]
            if result.get("value") is True: return
            time.sleep(STEP_DELAY)
        raise TimeoutError(f"Text not found within {timeout_ms}ms: '{text}'")

    # --------------- mouse handlers ----------------
    def mouse_move(self, x, y):
        self._send("Input.dispatchMouseEvent", {"type": "mouseMoved", "x": x, "y": y, "buttons": 0})

    def mouse_down(self, x, y, button="left"):
        self._send("Input.dispatchMouseEvent", {"type": "mousePressed", "x": x, "y": y, "button": button, "clickCount": 1})

    def mouse_up(self, x, y, button="left"):
        self._send("Input.dispatchMouseEvent", {"type": "mouseReleased", "x": x, "y": y, "button": button, "clickCount": 1})

    def hover(self, xpath, timeout_ms=DEFAULT_TIMEOUT):
        self._ensure_page_actionable(timeout_ms=timeout_ms)
        self.wait_for_element(xpath, timeout_ms=timeout_ms)
        obj_id = self._get_object_id(xpath)
        if not obj_id: raise RuntimeError(f"Hover failed; ID retrieval failed: {xpath}")

        self._send("DOM.scrollIntoViewIfNeeded", {"objectId": obj_id})
        point = self._get_center_by_id(obj_id)
        if not point: raise RuntimeError(f"Hover failed; geometry failed: {xpath}")

        self.mouse_move(point["x"] - 5, point["y"] - 5)
        time.sleep(STEP_DELAY)
        self.mouse_move(point["x"], point["y"])
        
        # Synthetic fallback
        expr = """
        function(el) {
            ['mouseover', 'mouseenter'].forEach(type => {
                el.dispatchEvent(new MouseEvent(type, { view: window, bubbles: true, cancelable: true }));
            });
        }
        """
        self._send("Runtime.callFunctionOn", {"functionDeclaration": expr, "objectId": obj_id})
        time.sleep(UI_DELAY)

    def double_click(self, xpath, timeout_ms=DEFAULT_TIMEOUT):
        self._ensure_page_actionable(timeout_ms=timeout_ms)
        self.wait_for_element(xpath, timeout_ms=timeout_ms)
        obj_id = self._get_object_id(xpath)
        if not obj_id: raise RuntimeError(f"Double click failed; no ID for {xpath}")

        self._send("DOM.scrollIntoViewIfNeeded", {"objectId": obj_id})
        point = self._get_center_by_id(obj_id)
        if point:
            for _ in range(2):
                self.mouse_down(point["x"], point["y"])
                self.mouse_up(point["x"], point["y"])
            return
        
        self._send("Runtime.callFunctionOn", {"functionDeclaration": "function() { this.click(); this.click(); }", "objectId": obj_id})

    def drag_and_drop(self, source_xpath, target_xpath, timeout_ms=DEFAULT_TIMEOUT):
        self._ensure_page_actionable(timeout_ms=timeout_ms)
        self.wait_for_element(source_xpath, timeout_ms=timeout_ms)
        self.wait_for_element(target_xpath, timeout_ms=timeout_ms)
        
        src_id = self._get_object_id(source_xpath)
        tgt_id = self._get_object_id(target_xpath)
        if not src_id or not tgt_id: raise RuntimeError("Drag failed: could not resolve source or target ID")

        self._send("DOM.scrollIntoViewIfNeeded", {"objectId": src_id})
        self._send("DOM.scrollIntoViewIfNeeded", {"objectId": tgt_id})
        
        src = self._get_center_by_id(src_id)
        tgt = self._get_center_by_id(tgt_id)
        if src and tgt:
            self.mouse_move(src["x"], src["y"])
            self.mouse_down(src["x"], src["y"])
            time.sleep(STEP_DELAY)
            self.mouse_move(tgt["x"], tgt["y"])
            time.sleep(STEP_DELAY)
            self.mouse_up(tgt["x"], tgt["y"])
            return
        raise RuntimeError("Drag failed: could not calculate geometry")

    def press_key(self, key):
        self._send("Input.dispatchKeyEvent", {"type": "keyDown", "key": key})
        self._send("Input.dispatchKeyEvent", {"type": "keyUp", "key": key})

    def fill(self, xpath: str, value: str, timeout_ms: int = DEFAULT_TIMEOUT):
        entry = self.tracer.start_step(action="fill", target=xpath, params={"value": value}) if self.tracer.enabled else None
        deadline = time.monotonic() + timeout_ms / 1000
        
        try:            
            while time.monotonic() < deadline:
                try:
                    self._ensure_page_actionable(timeout_ms=PAGE_LOAD_TIMEOUT)
                    self.wait_for_element(xpath, timeout_ms=DEFAULT_TIMEOUT)
                    obj_id = self._get_object_id(xpath)
                    if not obj_id: raise RuntimeError("Object ID lookup failed")

                    self._send("DOM.scrollIntoViewIfNeeded", {"objectId": obj_id})
                    
                    # Physical Click for Focus
                    point = self._get_center_by_id(obj_id)
                    if point:
                        self.mouse_move(point["x"], point["y"])
                        self.mouse_down(point["x"], point["y"])
                        self.mouse_up(point["x"], point["y"])
                    else:
                        self._send("Runtime.callFunctionOn", {"functionDeclaration": "function() { this.focus(); }", "objectId": obj_id})
                    
                    time.sleep(STEP_DELAY)

                    # Physical Clear (Ctrl+A -> Backspace)
                    self._send("Input.dispatchKeyEvent", {"type": "keyDown", "key": "Control", "code": "ControlLeft", "modifiers": 2})
                    self._send("Input.dispatchKeyEvent", {"type": "keyDown", "key": "a", "code": "KeyA", "modifiers": 2, "text": "", "unmodifiedText": ""})
                    self._send("Input.dispatchKeyEvent", {"type": "keyUp", "key": "a", "code": "KeyA", "modifiers": 2})
                    self._send("Input.dispatchKeyEvent", {"type": "keyUp", "key": "Control", "code": "ControlLeft", "modifiers": 0})
                    
                    time.sleep(0.05)
                    self._send("Input.dispatchKeyEvent", {"type": "keyDown", "key": "Backspace", "code": "Backspace"})
                    self._send("Input.dispatchKeyEvent", {"type": "keyUp", "key": "Backspace", "code": "Backspace"})
                    
                    time.sleep(STEP_DELAY)

                    for ch in value:
                        self._send("Input.dispatchKeyEvent", { "type": "char", "text": ch })

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
            # If no tracer, ensure we still take a generic screenshot
            else:
                self._save_debug_screenshot("fill_failed")
            raise

    def click(self, xpath, timeout_ms=DEFAULT_TIMEOUT):
        entry = self.tracer.start_step(action="click", target=xpath) if self.tracer.enabled else None
        deadline = time.monotonic() + timeout_ms / 1000
        try:
            while time.monotonic() < deadline:
                try:
                    self._ensure_page_actionable(timeout_ms=PAGE_LOAD_TIMEOUT)
                    self.wait_for_element(xpath, timeout_ms=DEFAULT_TIMEOUT)
                    obj_id = self._get_object_id(xpath)
                    if not obj_id: raise RuntimeError("Object ID lookup failed")

                    self._send("DOM.scrollIntoViewIfNeeded", {"objectId": obj_id})
                    point = self._get_center_by_id(obj_id)
                    
                    if point:
                        self._send("Input.dispatchMouseEvent", {"type": "mousePressed", "x": point["x"], "y": point["y"], "button": "left", "clickCount": 1})
                        self._send("Input.dispatchMouseEvent", {"type": "mouseReleased", "x": point["x"], "y": point["y"], "button": "left", "clickCount": 1})
                        if entry: self.tracer.success(entry)
                        return

                    self._send("Runtime.callFunctionOn", {"functionDeclaration": "function() { this.click(); }", "objectId": obj_id})
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
            else:
                self._save_debug_screenshot("click_failed")
            raise

    def _ensure_page_actionable(self, timeout_ms=PAGE_LOAD_TIMEOUT):
        deadline = time.monotonic() + timeout_ms / 1000
        while time.monotonic() < deadline:
            try:
                ready_id = self._send("Runtime.evaluate", {"expression": "document.readyState"})
                state = self._recv(ready_id)["result"]["result"].get("value")
                if state != "complete":
                    time.sleep(STEP_DELAY)
                    continue
                try: self.wait_for_network_idle(timeout_ms=NETWORK_TIMEOUT, idle_ms=500)
                except TimeoutError: pass 
                try: self.wait_for_dom_stable(timeout_ms=DOM_TIMEOUT, idle_ms=500)
                except TimeoutError:
                    if time.monotonic() > deadline: raise
                    continue
                return
            except Exception:
                time.sleep(STEP_DELAY)
        raise TimeoutError(f"Page failed to stabilize within {timeout_ms}ms")

    def send_keys(self, keys: str, xpath: str = None):
        self._ensure_page_actionable()
        if xpath:
            obj_id = self._get_object_id(xpath)
            if not obj_id: raise RuntimeError(f"Cannot send keys; element not found: {xpath}")
            self._send("DOM.scrollIntoViewIfNeeded", {"objectId": obj_id})
            self._send("Runtime.callFunctionOn", {"functionDeclaration": "function() { this.focus(); }", "objectId": obj_id})
            time.sleep(STEP_DELAY)

        modifiers, key = self._parse_key_combo(keys)
        mod_mask = ((2 if modifiers["Control"] else 0) | (8 if modifiers["Shift"] else 0) | (1 if modifiers["Alt"] else 0) | (4 if modifiers["Meta"] else 0))

        if key in KEY_MAP:
            key_val, code_val = KEY_MAP[key]
            type_down = "rawKeyDown" if key == "Enter" else "keyDown"
        elif len(key) == 1:
            if modifiers["Shift"]: key_val = key.upper()
            else: key_val = key
            if key.isalpha(): code_val = f"Key{key.upper()}"
            elif key.isdigit(): code_val = f"Digit{key}"
            else: code_val = "Unidentified"
            type_down = "keyDown"
        else:
            key_val = key; code_val = "Unidentified"; type_down = "keyDown"

        self._send("Input.dispatchKeyEvent", {"type": type_down, "key": key_val, "code": code_val, "modifiers": mod_mask, "windowsVirtualKeyCode": 0, "nativeVirtualKeyCode": 0})
        self._send("Input.dispatchKeyEvent", {"type": "keyUp", "key": key_val, "code": code_val, "modifiers": mod_mask})

    def scroll_into_view(self, xpath):
        expr = f"""
        (function () {{
            const snapshot = document.evaluate("{xpath}", document, null, XPathResult.ORDERED_NODE_SNAPSHOT_TYPE, null);
            for (let i = 0; i < snapshot.snapshotLength; i++) {{
                const el = snapshot.snapshotItem(i);
                const style = window.getComputedStyle(el);
                if (style.display !== 'none' && style.visibility !== 'hidden') {{
                    el.scrollIntoView({{ block: 'center', inline: 'center', behavior: 'instant' }});
                    return true;
                }}
            }}
            return false;
        }})()
        """
        msg_id = self._send("Runtime.evaluate", {"expression": expr})
        result = self._recv(msg_id)["result"]["result"]
        return result.get("value") is True

    def type_human(self, xpath: str, text: str):
        try:
            self._ensure_page_actionable()
            obj_id = self._get_object_id(xpath)
            if not obj_id: raise RuntimeError(f"Element not found: {xpath}")

            self._send("DOM.scrollIntoViewIfNeeded", {"objectId": obj_id})
            point = self._get_center_by_id(obj_id)
            if point:
                self.mouse_move(point["x"], point["y"])
                self.mouse_down(point["x"], point["y"])
                self.mouse_up(point["x"], point["y"])
            else:
                self._send("Runtime.callFunctionOn", {"functionDeclaration": "function() { this.focus(); }", "objectId": obj_id})
            
            time.sleep(STEP_DELAY)
            print(f"Human typing into {xpath}...")
            for char in text:
                self._send("Input.dispatchKeyEvent", {"type": "keyDown", "key": char})
                self._send("Input.dispatchKeyEvent", {"type": "char", "text": char})
                self._send("Input.dispatchKeyEvent", {"type": "keyUp", "key": char})
                jitter = (ord(char) % 3) * 0.02 
                time.sleep(HUMAN_DELAY + jitter)
        except Exception as e:
            self._save_debug_screenshot("type_human_failed")
            raise e

    def get_text(self, xpath: str) -> str:
        self._ensure_page_actionable()
        obj_id = self._get_object_id(xpath)
        if not obj_id: raise RuntimeError(f"Element not found for text retrieval: {xpath}")

        expr = """
        function() {
            const el = this;
            const tag = el.tagName.toLowerCase();
            const inputTypes = ['text', 'password', 'email', 'number', 'search', 'url', 'tel', 'date'];
            if (tag === 'textarea' || (tag === 'input' && inputTypes.includes(el.type))) return el.value || el.getAttribute('placeholder') || '';
            if (tag === 'input' && ['button', 'submit', 'reset'].includes(el.type)) return el.value || '';
            if (tag === 'select') return el.options[el.selectedIndex].text || '';
            const childInput = el.querySelector('input, textarea, select');
            if (childInput) {
                const directText = el.innerText.replace(childInput.value || '', '').trim();
                if (directText.length === 0) {
                     if (childInput.tagName.toLowerCase() === 'select') return childInput.options[childInput.selectedIndex].text || '';
                     return childInput.value || childInput.getAttribute('placeholder') || '';
                }
            }
            return el.innerText || el.textContent || '';
        }
        """
        msg_id = self._send("Runtime.callFunctionOn", {"objectId": obj_id, "functionDeclaration": expr, "returnByValue": True})
        result = self._recv(msg_id)
        res_root = result.get("result", {})
        inner_res = res_root.get("result", {})
        val = inner_res.get("value", "")
        return str(val).strip()

    def scrape_table(self, table_xpath: str, next_page_xpath: str = None, max_pages: int = 0, total_pages_xpath: str = None):
        SAFETY_LIMIT = 50 
        if max_pages > 0:
            limit = max_pages
            print(f"Scraping limit set by user: {limit} pages")
        elif total_pages_xpath:
            try:
                text = self.get_text(total_pages_xpath)
                match = re.search(r"(?:of|/)\s*(\d+)", text, re.IGNORECASE)
                if match: limit = int(match.group(1))
                else:
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
        for page in range(limit):
            if page > 0:
                self._ensure_page_actionable()
                time.sleep(DOM_IDLE_MS / 1000)

            table_id = self._get_object_id(table_xpath)
            if not table_id:
                print(f"Table not found on page {page + 1}. Stopping.")
                self._save_debug_screenshot(f"scrape_table_missing_{page}")
                break
                
            scraper_js = """
            function() {
                const table = this;
                const data = [];
                const headers = [];
                let headerCells = table.querySelectorAll('thead th');
                if (headerCells.length === 0) headerCells = table.querySelectorAll('tr:first-child th');
                headerCells.forEach(th => headers.push(th.innerText.trim()));
                let rows = table.querySelectorAll('tbody tr');
                if (rows.length === 0) rows = table.querySelectorAll('tr');
                for (const row of rows) {
                    if (row.querySelector('th')) continue;
                    const cells = row.querySelectorAll('td');
                    if (cells.length === 0) continue;
                    const rowObj = {};
                    cells.forEach((cell, i) => {
                        const txt = cell.innerText.trim().replace(/\\n/g, ' ');
                        if (headers[i]) { rowObj[headers[i]] = txt; } 
                        else { rowObj[`column_${i}`] = txt; }
                    });
                    data.push(rowObj);
                }
                return data;
            }
            """
            msg_id = self._send("Runtime.callFunctionOn", {"objectId": table_id, "functionDeclaration": scraper_js, "returnByValue": True})
            response = self._recv(msg_id)

            if "exceptionDetails" in response.get("result", {}):
                error_msg = response["result"]["exceptionDetails"]["exception"]["description"]
                print(f"JS Error in scrape_table: {error_msg}")
                self._save_debug_screenshot("scrape_table_js_error")
                break

            page_data = response.get("result", {}).get("result", {}).get("value", [])
            all_data.extend(page_data)
            print(f"Scraped {len(page_data)} rows from page {page + 1}")
            
            if not next_page_xpath: break
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
                if "exceptionDetails" in response.get("result", {}):
                    print(f"Pagination JS Error: {response['result']['exceptionDetails']}")
                    self._save_debug_screenshot("pagination_check_failed")
                    break
                
                result_obj = response.get("result", {}).get("result", {})
                if result_obj.get("value", False):
                    print("Pagination 'Next' button is disabled. Stopping.")
                    break
                    
                print(f"Navigating to table page {page + 2}...")
                self.click(next_page_xpath)
            except Exception as e:
                print(f"Pagination failed: {e}")
                self._save_debug_screenshot("pagination_click_failed")
                break
        return all_data

    def is_checked(self, xpath: str) -> bool:
        obj_id = self._get_object_id(xpath)
        if not obj_id: return False
        result = self._send("Runtime.callFunctionOn", {"objectId": obj_id, "functionDeclaration": "function() { return this.checked; }", "returnByValue": True})
        return self._recv(result)["result"]["result"]["value"] is True
    
    def is_selected(self, xpath: str) -> bool:
        obj_id = self._get_object_id(xpath)
        if not obj_id: return False
        result = self._send("Runtime.callFunctionOn", {"objectId": obj_id, "functionDeclaration": "function() { return this.selected; }", "returnByValue": True})
        return self._recv(result)["result"]["result"]["value"] is True

    def select_option(self, select_xpath: str, *, value: str | None = None, label: str | None = None, index: int | None = None):
        try:
            self._ensure_page_actionable()
            obj_id = self._get_object_id(select_xpath)
            if not obj_id: raise RuntimeError(f"Select element not found or hidden: {select_xpath}")
            self._send("DOM.scrollIntoViewIfNeeded", {"objectId": obj_id})
            expr = f"""
            function() {{
                const select = this;
                let option = null;
                if ({json.dumps(value)} !== null) option = [...select.options].find(o => o.value === {json.dumps(value)});
                else if ({json.dumps(label)} !== null) option = [...select.options].find(o => o.text.trim() === {json.dumps(label)});
                else if ({index} !== null) option = select.options[{index}];
                if (!option) return false;
                select.value = option.value;
                option.selected = true;
                select.dispatchEvent(new Event('input', {{ bubbles: true }}));
                select.dispatchEvent(new Event('change', {{ bubbles: true }}));
                return true;
            }}
            """
            msg_id = self._send("Runtime.callFunctionOn", {"objectId": obj_id, "functionDeclaration": expr, "returnByValue": True})
            result = self._recv(msg_id)["result"]["result"]
            if result.get("value") is not True: raise RuntimeError(f"Option not found (Value: {value}, Label: {label}, Index: {index})")
        except Exception as e:
            self._save_debug_screenshot("select_option_failed")
            raise e

    def select_custom_option(self, trigger_xpath: str, option_text: str):
        try:
            self._ensure_page_actionable()
            print(f"Clicking dropdown trigger: {trigger_xpath}")
            self.click(trigger_xpath)
            time.sleep(UI_DELAY) 

            expr = f"""
            (function() {{
                const query = {json.dumps(option_text)}.toLowerCase();
                const candidates = document.querySelectorAll('li, [role="option"], div, span, a, .item, .option');
                let bestEl = null; let bestScore = -1;
                for (const el of candidates) {{
                    const rect = el.getBoundingClientRect();
                    const style = window.getComputedStyle(el);
                    if (rect.width < 5 || rect.height < 5 || style.visibility === 'hidden' || style.display === 'none' || style.opacity === '0') continue;
                    const text = el.innerText.toLowerCase().trim();
                    if (!text.includes(query)) continue;
                    let score = 0;
                    if (text === query) score += 100;
                    if (el.tagName === 'LI' || el.getAttribute('role') === 'option') score += 50;
                    if (text.length > query.length + 50) score -= 1000;
                    if (score > bestScore) {{ bestScore = score; bestEl = el; }}
                }}
                return bestEl;
            }})()
            """
            msg_id = self._send("Runtime.evaluate", {"expression": expr, "returnByValue": False})
            result = self._recv(msg_id)
            remote_obj = result["result"]["result"]
            if remote_obj.get("subtype") == "null" or "objectId" not in remote_obj:
                raise RuntimeError(f"Option '{option_text}' not found (or visible) after clicking trigger.")

            option_id = remote_obj["objectId"]
            self._send("DOM.scrollIntoViewIfNeeded", {"objectId": option_id})
            point = self._get_center_by_id(option_id)
            if point:
                self.mouse_move(point["x"], point["y"])
                self.mouse_down(point["x"], point["y"])
                self.mouse_up(point["x"], point["y"])
            else:
                self._send("Runtime.callFunctionOn", {"functionDeclaration": "function() { this.click(); }", "objectId": option_id})
            time.sleep(STEP_DELAY)
        except Exception as e:
            self._save_debug_screenshot("custom_select_failed")
            raise e

    def select_autocomplete_option(self, input_xpath: str, select_text: str):
        try:
            self._ensure_page_actionable()
            obj_id = self._get_object_id(input_xpath)
            if not obj_id: raise RuntimeError(f"Autocomplete input not found: {input_xpath}")

            print(f"Focusing input: {input_xpath}")
            self._send("DOM.scrollIntoViewIfNeeded", {"objectId": obj_id})
            self._send("Runtime.callFunctionOn", {"functionDeclaration": "function() { this.focus(); this.value = ''; }", "objectId": obj_id})

            check_js = f"""
            (function() {{
                const query = {json.dumps(select_text)}.toLowerCase().trim();
                const candidates = document.querySelectorAll('li, [role="option"], div, span, a, .item, .option');
                for (const el of candidates) {{
                    const style = window.getComputedStyle(el);
                    if (style.display === 'none' || style.visibility === 'hidden' || style.opacity === '0') continue;
                    const rect = el.getBoundingClientRect();
                    if (rect.width < 5 || rect.height < 5) continue;
                    const text = el.innerText.toLowerCase().trim();
                    if (text === query || (text.includes(query) && text.length < query.length + 30)) return true;
                }}
                return false;
            }})()
            """
            found = False
            print(f"Typing '{select_text}'...")
            for i, char in enumerate(select_text):
                self._send("Input.dispatchKeyEvent", {"type": "keyDown", "key": char})
                self._send("Input.dispatchKeyEvent", {"type": "char", "text": char})
                self._send("Input.dispatchKeyEvent", {"type": "keyUp", "key": char})
                time.sleep(AUTO_DELAY) 
                if i >= 1: 
                    msg_id = self._send("Runtime.evaluate", {"expression": check_js})
                    if self._recv(msg_id)["result"]["result"]["value"]:
                        print(f"Target '{select_text}' appeared! Stopping input.")
                        found = True
                        break
            if not found: time.sleep(1.0)
            self._select_visible_option(select_text)
        except Exception as e:
            self._save_debug_screenshot("autocomplete_failed")
            raise e

    def _select_visible_option(self, option_text):
        expr = f"""
        (function() {{
            const query = {json.dumps(option_text)}.toLowerCase().trim();
            const candidates = document.querySelectorAll('li, [role="option"], div, span, a, .item, .option');
            let bestEl = null; let bestScore = -1;
            for (const el of candidates) {{
                const rect = el.getBoundingClientRect();
                const style = window.getComputedStyle(el);
                if (rect.width < 5 || rect.height < 5 || style.visibility === 'hidden' || style.display === 'none' || style.opacity === '0') continue;
                const text = el.innerText.toLowerCase().trim();
                if (!text.includes(query)) continue;
                let score = 0;
                if (text === query) score += 100;
                if (el.tagName === 'LI' || el.getAttribute('role') === 'option') score += 50;
                if (text.length > query.length + 50) score -= 1000;
                if (el.querySelector('.highlight') || el.classList.contains('highlight')) score += 20;
                if (score > bestScore) {{ bestScore = score; bestEl = el; }}
            }}
            return bestEl;
        }})()
        """
        msg_id = self._send("Runtime.evaluate", {"expression": expr, "returnByValue": False})
        result = self._recv(msg_id)
        remote_obj = result["result"]["result"]
        if remote_obj.get("subtype") == "null" or "objectId" not in remote_obj: raise RuntimeError(f"Option '{option_text}' not found.")

        option_id = remote_obj["objectId"]
        self._send("DOM.scrollIntoViewIfNeeded", {"objectId": option_id})
        point = self._get_center_by_id(option_id)
        if point:
            self.mouse_move(point["x"], point["y"])
            self.mouse_down(point["x"], point["y"])
            self.mouse_up(point["x"], point["y"])
        else:
            self._send("Runtime.callFunctionOn", {"functionDeclaration": "function() { this.click(); }", "objectId": option_id})

    def multi_select(self, select_xpath: str, values: list[str]):
        try:
            self._ensure_page_actionable()
            obj_id = self._get_object_id(select_xpath)
            if not obj_id: raise RuntimeError(f"Multi-select element not found or hidden: {select_xpath}")

            self._send("DOM.scrollIntoViewIfNeeded", {"objectId": obj_id})
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
                    }} else {{ option.selected = false; }}
                }}
                if (foundAny) {{
                    select.dispatchEvent(new Event('input', {{ bubbles: true }}));
                    select.dispatchEvent(new Event('change', {{ bubbles: true }}));
                }}
                return true;
            }}
            """
            msg_id = self._send("Runtime.callFunctionOn", {"objectId": obj_id, "functionDeclaration": expr, "returnByValue": True})
            result = self._recv(msg_id)["result"]["result"]
            if result.get("value") is not True: raise RuntimeError("Multi-select failed or element was not multiple")
        except Exception as e:
            self._save_debug_screenshot("multi_select_failed")
            raise e

    def _get_center_via_box_model(self, xpath):
        expr = f"""
        (function () {{
            const snapshot = document.evaluate("{xpath}", document, null, XPathResult.ORDERED_NODE_SNAPSHOT_TYPE, null);
            for (let i = 0; i < snapshot.snapshotLength; i++) {{
                const el = snapshot.snapshotItem(i);
                const style = window.getComputedStyle(el);
                const rect = el.getBoundingClientRect();
                if (rect.width > 0 && style.display !== 'none' && style.visibility !== 'hidden') return el;
            }}
            return null;
        }})()
        """
        msg_id = self._send("Runtime.evaluate", {"expression": expr, "returnByValue": False})
        result = self._recv(msg_id)
        remote_obj = result["result"]["result"]
        if remote_obj.get("subtype") == "null" or "objectId" not in remote_obj: return None
        object_id = remote_obj["objectId"]
        try:
            box_id = self._send("DOM.getBoxModel", {"objectId": object_id})
            box_result = self._recv(box_id)
            if "error" in box_result: return None
            quad = box_result["result"]["model"]["content"]
            x = (quad[0] + quad[2] + quad[4] + quad[6]) / 4
            y = (quad[1] + quad[3] + quad[5] + quad[7]) / 4
            return {"x": x, "y": y}
        except Exception: return None
        
    def _get_object_id(self, xpath):
        expr = f"""
        (function () {{
            const snapshot = document.evaluate("{xpath}", document, null, XPathResult.ORDERED_NODE_SNAPSHOT_TYPE, null);
            for (let i = 0; i < snapshot.snapshotLength; i++) {{
                const el = snapshot.snapshotItem(i);
                const style = window.getComputedStyle(el);
                const rect = el.getBoundingClientRect();
                if (rect.width > 0 && style.display !== 'none' && style.visibility !== 'hidden') return el;
            }}
            return null;
        }})()
        """
        msg_id = self._send("Runtime.evaluate", {"expression": expr, "returnByValue": False})
        result = self._recv(msg_id)
        remote_obj = result["result"]["result"]
        if remote_obj.get("subtype") == "null" or "objectId" not in remote_obj: return None
        return remote_obj["objectId"]
    
    def _get_center_by_id(self, object_id):
        try:
            box_data = self._send("DOM.getBoxModel", {"objectId": object_id})
            box_result = self._recv(box_data)
            if "error" in box_result: return None
            quad = box_result["result"]["model"]["content"]
            x = (quad[0] + quad[2] + quad[4] + quad[6]) / 4
            y = (quad[1] + quad[3] + quad[5] + quad[7]) / 4
            return {"x": x, "y": y}
        except Exception: return None

    def find_elements_by_text(self, query: str):
        js_script = f"""
        (function() {{
            const query = {json.dumps(query)}.toLowerCase().trim();
            const candidates = [];
            const selectors = `input, button, a, textarea, select, [role="button"], [role="link"], [role="menuitem"], [role="tab"], [onclick], [class*="btn"], [class*="button"], [class*="icon"], [class*="arrow"], [class*="pager"], [class*="pagination"]`;
            document.querySelectorAll(selectors).forEach(el => {{
                const rect = el.getBoundingClientRect();
                const style = window.getComputedStyle(el);
                if (rect.width < 1 || rect.height < 1 || style.visibility === 'hidden' || style.display === 'none') return;
                const text = (el.innerText || '').toLowerCase();
                const val = (el.value || '').toLowerCase();
                const ph = (el.getAttribute('placeholder') || '').toLowerCase();
                const name = (el.getAttribute('name') || '').toLowerCase();
                const id = (el.id || '').toLowerCase();
                const aria = (el.getAttribute('aria-label') || '').toLowerCase();
                const title = (el.getAttribute('title') || '').toLowerCase();
                const className = (el.className || '').toLowerCase(); 
                const role = (el.getAttribute('role') || '').toLowerCase();
                if (text.includes(query) || val.includes(query) || ph.includes(query) || name.includes(query) || id.includes(query) || aria.includes(query) || title.includes(query) || className.includes(query) || role.includes(query)) {{
                    let xpath = '';
                    if (el.id) {{ xpath = `//*[@id='${{el.id}}']`; }} 
                    else {{
                        const tag = el.tagName.toLowerCase();
                        if (el.innerText && el.innerText.trim().length > 0 && el.innerText.trim().length < 50) {{
                            const cleanText = el.innerText.trim().replace(/'/g, "");
                            xpath = `//${{tag}}[contains(normalize-space(.), '${{cleanText}}')]`;
                        }} else if (el.getAttribute('name')) {{
                            xpath = `//${{tag}}[@name='${{el.getAttribute('name')}}']`;
                        }} else if (el.className) {{
                            const cleanClass = el.className.trim().split(' ')[0]; // Take first class
                            if (cleanClass) xpath = `//${{tag}}[contains(@class, '${{cleanClass}}')]`;
                        }}
                        if (!xpath) xpath = `//${{tag}}`;
                    }}
                    candidates.push({{
                        tag: el.tagName.toLowerCase(),
                        text: (el.innerText || el.value || '').trim().substring(0, 50),
                        xpath: xpath,
                        attributes: {{
                            id: el.id, class: el.className, title: el.getAttribute('title'), role: el.getAttribute('role'), type: el.getAttribute('type'), 'aria-label': el.getAttribute('aria-label'), onclick: el.hasAttribute('onclick') ? 'true' : 'false'
                        }}
                    }});
                }}
            }});
            return candidates;
        }})()
        """
        msg_id = self._send("Runtime.evaluate", {"expression": js_script, "returnByValue": True})
        response = self._recv(msg_id)
        
        if "exceptionDetails" in response.get("result", {}):
            error_msg = response["result"]["exceptionDetails"]["exception"]["description"]
            print(f"CRITICAL JS ERROR in find_elements_by_text: {error_msg}")
            self._save_debug_screenshot("find_elements_js_error")
            return []

        result_val = response.get("result", {}).get("result", {}).get("value")
        return result_val if isinstance(result_val, list) else []

    def get_all_interactive_elements(self, tag_name: str = "button"):
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
                if (el.id) {{ xpath = `//*[@id='${{el.id}}']`; }} 
                else {{
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
                    results.push({{ tag: el.tagName, text: el.innerText || el.value || el.getAttribute('aria-label') || 'N/A', xpath: xpath, visible: true }});
                }}
            }});
            return results;
        }})()
        """
        msg_id = self._send("Runtime.evaluate", {"expression": js_script, "returnByValue": True})
        response = self._recv(msg_id)

        if "exceptionDetails" in response.get("result", {}):
            print(f"JS ERROR in get_all_interactive_elements: {response['result']['exceptionDetails']}")
            self._save_debug_screenshot("interactive_discovery_error")
            return []

        result_val = response.get("result", {}).get("result", {}).get("value")
        return result_val if isinstance(result_val, list) else []

    def _clean_old_profiles(self, max_age_seconds=300):        
        pattern = os.path.join(USER_DATA_DIR, "cdp-profile-*")
        now = time.time()
        try:
            for profile_path in glob.glob(pattern):
                try:
                    mtime = os.path.getmtime(profile_path)
                    if now - mtime > max_age_seconds:
                        shutil.rmtree(profile_path, ignore_errors=True)
                except Exception: pass
        except Exception: pass