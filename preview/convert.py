import subprocess
from pathlib import Path


def classify_soffice_error(returncode: int, stderr: str) -> str:
    """Classify a LibreOffice failure into a permanent or transient errorCode.

    Permanent codes (caller should not retry): password_protected.
    Transient codes (caller may retry per backoff): soffice_timeout, soffice_crash, soffice_no_output.
    """
    if returncode == 124:
        return "soffice_timeout"
    if "password" in stderr.lower() and "open" in stderr.lower():
        return "password_protected"
    if returncode == 0:
        return "soffice_no_output"
    return "soffice_crash"


class ConvertError(Exception):
    def __init__(self, error_code: str, stderr: str = ""):
        self.error_code = error_code
        self.stderr = stderr
        super().__init__(f"{error_code}: {stderr[:200]}")


def convert_to_pdf(input_path: Path, work_dir: Path, timeout_s: int = 120) -> Path:
    """Convert an Office file to PDF in work_dir. Returns the PDF path on success.

    Raises ConvertError with .error_code in {soffice_timeout, password_protected,
    soffice_no_output, soffice_crash} on failure.

    Uses a per-job LibreOffice user profile under work_dir/lo-profile, so concurrent
    invocations in the same container do not collide on the default profile.
    """
    input_path = Path(input_path)
    work_dir = Path(work_dir)
    profile_dir = work_dir / "lo-profile"
    profile_dir.mkdir(parents=True, exist_ok=True)
    profile_url = f"file://{profile_dir.resolve()}"

    cmd = [
        "soffice",
        "--headless",
        f"-env:UserInstallation={profile_url}",
        "--norestore",
        "--nofirststartwizard",
        "--convert-to",
        "pdf",
        "--outdir",
        str(work_dir),
        str(input_path),
    ]

    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout_s)
    except subprocess.TimeoutExpired as e:
        raw = e.stderr or b""
        if isinstance(raw, bytes):
            raw = raw.decode(errors="replace")
        raise ConvertError("soffice_timeout", raw) from e

    expected_pdf = work_dir / (input_path.stem + ".pdf")

    if result.returncode != 0:
        raise ConvertError(classify_soffice_error(result.returncode, result.stderr), result.stderr)

    if not expected_pdf.exists():
        raise ConvertError("soffice_no_output", result.stderr)

    return expected_pdf
