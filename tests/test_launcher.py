import os
import uuid
import pytest

from vault_v2.installation import InstallationSession, RuntimeState, runtime_state
from vault_v2.launcher import launch_application


pytestmark = pytest.mark.skipif(os.name != "nt", reason="Windows launcher")


def test_launch_holds_runtime_admission_for_the_whole_application_lifetime():
    namespace = "Local\\VaultV2-launch-test-" + uuid.uuid4().hex
    def application(argv):
        assert argv == ["Vault"]
        assert runtime_state(namespace) == RuntimeState.ACTIVE
        return 7
    assert launch_application(application, ["Vault"], namespace=namespace) == 7
    assert runtime_state(namespace) == RuntimeState.IDLE


def test_launch_is_refused_before_application_callback_during_install():
    from vault_v2.installation import InstallationBusy
    namespace = "Local\\VaultV2-launch-test-" + uuid.uuid4().hex
    def forbidden(_):
        pytest.fail("application must not start")
    with InstallationSession("installer", namespace=namespace):
        with pytest.raises(InstallationBusy):
            launch_application(forbidden, ["Vault"], namespace=namespace)
