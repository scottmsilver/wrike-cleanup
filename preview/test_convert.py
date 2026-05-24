from convert import classify_soffice_error


def test_classify_password_protected():
    stderr = "Error: Password to open required for /tmp/foo.docx"
    assert classify_soffice_error(returncode=1, stderr=stderr) == "password_protected"


def test_classify_timeout():
    assert classify_soffice_error(returncode=124, stderr="") == "soffice_timeout"


def test_classify_crash():
    assert classify_soffice_error(returncode=139, stderr="Segmentation fault") == "soffice_crash"


def test_classify_no_output():
    # returncode 0 but no PDF produced - caller passes a sentinel
    assert classify_soffice_error(returncode=0, stderr="") == "soffice_no_output"


def test_classify_unknown_nonzero():
    assert classify_soffice_error(returncode=1, stderr="some other error") == "soffice_crash"


def test_convert_to_pdf_importable():
    from convert import convert_to_pdf  # noqa: F401

import subprocess
from unittest.mock import patch

from convert import ConvertError, convert_to_pdf


def test_timeout_decodes_bytes_stderr(tmp_path):
    fake = subprocess.TimeoutExpired(cmd="soffice", timeout=1, output=None, stderr=b"some bytes \xff")
    with patch("subprocess.run", side_effect=fake):
        input_path = tmp_path / "x.docx"
        input_path.write_bytes(b"fake")
        try:
            convert_to_pdf(input_path, tmp_path)
            assert False, "should have raised"
        except ConvertError as e:
            assert e.error_code == "soffice_timeout"
            # The stderr should be decoded — no bytes prefix.
            assert "b'" not in str(e)
            assert "some bytes" in e.stderr
