import os
import sys

ROOT = os.path.dirname(os.path.abspath(__file__))
SRC = os.path.join(ROOT, "src")

# Ensure imports resolve to modules inside src/.
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)
if SRC not in sys.path:
    sys.path.insert(0, SRC)

from src import setup_wizard as real_wizard  # noqa: E402

if hasattr(real_wizard, "main"):
    raise SystemExit(real_wizard.main())

raise SystemExit("Could not locate main() in src/setup_wizard.py")
