"""Bundled child process fixture. Never opens a game or accepts executable paths."""
import argparse
import json
from pathlib import Path
import time


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--behavior", choices=["success", "failure", "timeout", "ambiguous"], required=True)
    parser.add_argument("--log-file")
    args = parser.parse_args()
    def emit(event):
        line = json.dumps(event)
        if args.log_file:
            with Path(args.log_file).open("a", encoding="utf-8") as stream:
                stream.write(line + "\n")
        else:
            print(line, flush=True)
    if args.behavior == "timeout":
        time.sleep(60)
    if args.behavior == "success":
        emit({"event": "task_completed", "success": True})
    if args.behavior == "failure":
        emit({"event": "task_completed", "success": False})
        return 7
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
