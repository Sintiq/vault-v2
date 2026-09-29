"""Real named OS objects, unique names: never the live Vault's admission marker."""
import os
import uuid

import pytest

from vault_v2.installation import InstallationSession, InstallationBusy, RuntimeState, runtime_state


pytestmark = pytest.mark.skipif(os.name != "nt", reason="Windows named mutex contract")


def test_open_windows_allow_read_only_peers_but_refuse_install_until_last_close():
    namespace = "Local\\VaultV2-test-" + uuid.uuid4().hex
    assert runtime_state(namespace) == RuntimeState.IDLE
    with InstallationSession("runtime", namespace=namespace):
        assert runtime_state(namespace) == RuntimeState.ACTIVE
        with InstallationSession("runtime", namespace=namespace):
            with pytest.raises(InstallationBusy):
                with InstallationSession("installer", namespace=namespace):
                    pytest.fail("installer must not enter")
        assert runtime_state(namespace) == RuntimeState.ACTIVE
    assert runtime_state(namespace) == RuntimeState.IDLE


def test_installer_blocks_another_installer_and_new_windows_then_releases_on_error():
    namespace = "Local\\VaultV2-test-" + uuid.uuid4().hex
    with pytest.raises(RuntimeError, match="synthetic failure"):
        with InstallationSession("installer", namespace=namespace):
            for kind in ("runtime", "installer"):
                with pytest.raises(InstallationBusy):
                    with InstallationSession(kind, namespace=namespace):
                        pytest.fail("installation must exclude entry")
            raise RuntimeError("synthetic failure")
    with InstallationSession("runtime", namespace=namespace):
        assert runtime_state(namespace) == RuntimeState.ACTIVE


def test_invalid_admission_namespace_is_unknown_not_idle():
    assert runtime_state("arbitrary\\object") == RuntimeState.UNKNOWN
    with pytest.raises(InstallationBusy):
        with InstallationSession("installer", namespace="arbitrary\\object"):
            pytest.fail("unknown must not enter")


def test_os_reclaims_runtime_marker_after_child_exits():
    import subprocess
    import sys
    namespace = "Local\\VaultV2-child-test-" + uuid.uuid4().hex
    code = (
        "import time; from vault_v2.installation import InstallationSession; "
        f"session=InstallationSession('runtime',namespace={namespace!r}); session.__enter__(); "
        "print('READY',flush=True); time.sleep(0.5)"
    )
    child = subprocess.Popen([sys.executable, "-B", "-c", code], stdout=subprocess.PIPE,
                             stderr=subprocess.PIPE, text=True, creationflags=subprocess.CREATE_NO_WINDOW)
    try:
        assert child.stdout.readline().strip() == "READY"
        assert runtime_state(namespace) == RuntimeState.ACTIVE
        with pytest.raises(InstallationBusy):
            with InstallationSession("installer", namespace=namespace):
                pytest.fail("a different process is running")
        _, stderr = child.communicate(timeout=5)
        assert child.returncode == 0, stderr
        assert runtime_state(namespace) == RuntimeState.IDLE
    finally:
        if child.poll() is None:
            child.terminate()
            child.wait(timeout=5)


def test_global_namespace_is_usable_without_admin_and_released():
    namespace = "Global\\VaultV2-test-" + uuid.uuid4().hex
    with InstallationSession("runtime", namespace=namespace):
        assert runtime_state(namespace) == RuntimeState.ACTIVE
    assert runtime_state(namespace) == RuntimeState.IDLE
