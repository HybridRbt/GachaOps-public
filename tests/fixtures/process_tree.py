"""No game operations: create a sleeping child and publish its PID."""
import json
from pathlib import Path
import subprocess
import sys
import time

child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
Path(sys.argv[1]).write_text(json.dumps({"child_pid": child.pid}), encoding="utf-8")
if sys.argv[2] == "hold":
    time.sleep(60)
