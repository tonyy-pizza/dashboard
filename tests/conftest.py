"""Point the stores at a scratch directory before anything imports `paths`.

paths.py resolves its Windows defaults at import time, so these have to be set
during collection — before the first `import dashboard_widget`.
"""

import os
import tempfile
from pathlib import Path

_ROOT = Path(tempfile.mkdtemp(prefix="dashboard-tests-"))
os.environ.setdefault("DASHBOARD_DATA_DIR", str(_ROOT / "data"))
os.environ.setdefault("DASHBOARD_NOTES_DIR", str(_ROOT / "notes"))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
