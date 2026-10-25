from airmirror import REPO_ROOT, SENDER_EXE


def test_sender_path_is_inside_repo():
    assert SENDER_EXE.parent == REPO_ROOT / "bin"
    assert SENDER_EXE.name == "open-airplay.exe"
