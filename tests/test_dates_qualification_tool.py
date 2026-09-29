"""A local qualification must prove it can save evidence before generating."""
from pathlib import Path
import urllib.request

import pytest

from tools.check_dates_open_door import main


@pytest.mark.parametrize("failure", ["existing", "permission", "missing_consent"])
def test_evidence_preflight_sends_no_model_request(tmp_path, monkeypatch, failure):
    output = tmp_path / "evidence.json"

    def unexpected_request(*args, **kwargs):
        pytest.fail("model transport reached before evidence preflight")

    monkeypatch.setattr(urllib.request.OpenerDirector, "open", unexpected_request)
    if failure == "existing":
        output.write_text("preserve this evidence", encoding="utf-8")
    elif failure == "permission":
        original_open = Path.open

        def refuse_evidence(path, *args, **kwargs):
            if path == output:
                raise PermissionError("synthetic evidence destination denied")
            return original_open(path, *args, **kwargs)

        monkeypatch.setattr(Path, "open", refuse_evidence)
    arguments = ["--output", str(output)]
    if failure != "missing_consent":
        arguments.insert(0, "--run-local-synthetic")
    expected = PermissionError if failure == "permission" else SystemExit
    with pytest.raises(expected):
        main(arguments)
    if failure == "existing":
        assert output.read_text(encoding="utf-8") == "preserve this evidence"
    else:
        assert not output.exists()
