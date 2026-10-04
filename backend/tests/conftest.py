"""Set deterministic application configuration before any test imports app.main."""

import os
import tempfile


_TEST_DATA_DIR = tempfile.TemporaryDirectory(prefix="omt-pytest-")
os.environ.setdefault("OMT_DATA_DIR", _TEST_DATA_DIR.name)
os.environ.setdefault("OMT_ENVIRONMENT", "test")
os.environ.setdefault("OMT_ENABLE_DEV_LOGIN", "1")
os.environ.setdefault("OMT_DEFAULT_TIMEZONE", "Asia/Taipei")
