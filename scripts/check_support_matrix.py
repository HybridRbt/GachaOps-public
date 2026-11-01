"""Validate committed compatibility evidence against the built-in catalog."""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from gachaops.compatibility import load_support_matrix

if __name__ == '__main__':
    try:
        data = load_support_matrix()
        print('Support matrix verified:', len(data['adapters']), 'adapters')
    except (ValueError, OSError, KeyError, TypeError):
        print('Support matrix invalid', file=sys.stderr)
        raise SystemExit(1)
