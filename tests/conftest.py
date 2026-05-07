"""pytest configuration for chatwire-telegram tests.

When running tests against an installed chatwire (e.g. in CI or after
`pip install chatwire`), no path manipulation is needed — chatwire's
modules are already on sys.path.

For local development against a chatwire source checkout, set the
CHATWIRE_SRC environment variable to the chatwire repo root and this
conftest will add it to sys.path automatically:

    CHATWIRE_SRC=/path/to/chatwire pytest
"""
import os
import sys
from pathlib import Path

_src = os.environ.get("CHATWIRE_SRC")
if _src:
    sys.path.insert(0, str(Path(_src).resolve()))
