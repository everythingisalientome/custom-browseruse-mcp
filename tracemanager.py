import time
import os
from datetime import datetime
import json


class TraceManager:
    def __init__(self, enabled=True, out_dir="traces"):
        self.enabled = enabled
        self.out_dir = out_dir
        self.trace = []
        self.step = 0

        if enabled:
            os.makedirs(out_dir, exist_ok=True)

    def start_step(self, action, target=None, params=None):
        self.step += 1
        entry = {
            "step": self.step,
            "action": action,
            "target": target,
            "params": params or {},
            "start_time": datetime.utcnow().isoformat(),
            "retries": 0,
            "result": None,
            "error": None,
            "artifacts": {}
        }
        self.trace.append(entry)
        return entry

    def record_retry(self, entry):
        entry["retries"] += 1

    def success(self, entry):
        entry["end_time"] = datetime.utcnow().isoformat()
        entry["result"] = "SUCCESS"

    def failure(self, entry, error):
        entry["end_time"] = datetime.utcnow().isoformat()
        entry["result"] = "FAILURE"
        entry["error"] = str(error)

    def attach_artifact(self, entry, name, filename):
        entry["artifacts"][name] = filename

    def dump(self):
        path = os.path.join(self.out_dir, "trace.json")
        with open(path, "w", encoding="utf-8") as f:
            json.dump(self.trace, f, indent=2)
        return path
