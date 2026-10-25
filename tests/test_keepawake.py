import sys

import pytest

if sys.platform != "win32":
    pytest.skip("SetThreadExecutionState is Windows only", allow_module_level=True)

from airmirror import keepawake


def test_hold_and_release_succeed():
    keepawake.hold()
    keepawake.release()
