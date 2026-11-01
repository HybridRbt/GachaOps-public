"""Bundled stdlib-only probe; never imports an installed application."""
import argparse
import json
import os
from pathlib import Path
import sys


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", required=True)
    parser.add_argument("--sentinel", required=True)
    args = parser.parse_args()
    report = {"executable": sys.executable, "version": list(sys.version_info[:3]), "pid": os.getpid(),
              "cwd": os.getcwd(), "sentinel": args.sentinel, "environment_sentinel": os.getenv("GACHAOPS_PROBE"),
              "pythonpath_present": "PYTHONPATH" in os.environ, "pythonhome_present": "PYTHONHOME" in os.environ,
              "no_site": bool(sys.flags.no_site), "isolated": bool(sys.flags.isolated),
              "application_imported": any(name == "ok" or name.startswith("ok.") for name in sys.modules)}
    Path(args.output).write_text(json.dumps(report, ensure_ascii=False), encoding="utf-8")


if __name__ == "__main__":
    main()
