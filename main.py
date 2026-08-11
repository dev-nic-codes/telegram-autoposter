import os
import sys

ROOT = os.path.dirname(os.path.abspath(__file__))
SRC = os.path.join(ROOT, "src")

# Ensure imports resolve to modules inside src/.
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)
if SRC not in sys.path:
    sys.path.insert(0, SRC)

# Import the real entry module so PyInstaller bundles it reliably.
from src import main as real_main  # noqa: E402

if hasattr(real_main, "main"):
    raise SystemExit(real_main.main())

# Fallback (should not normally be needed).
raise SystemExit("Could not locate main() in src/main.py")
