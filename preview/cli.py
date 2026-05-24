"""One-off Wrike attachment → PDF preview tool.

Usage:
    python preview/cli.py --attachment-id IEAGS6BFI4G7...
    python preview/cli.py --from-permalink 721560302

--from-permalink resolves a numeric Wrike URL ID (the kind you get from copying
a link in the Wrike UI) into the alphanumeric API ID the rest of the pipeline needs.

Requires:
    - config.json in the working directory with the Wrike API token (same as main.py).
    - LibreOffice on PATH (or run inside the preview Docker image).
"""

import argparse
import sys
import tempfile
from pathlib import Path

import requests
from convert import ConvertError, convert_to_pdf
from wrike import WrikeApi


def resolve_permalink(wrike: WrikeApi, permalink_id: str) -> str:
    """Convert a numeric Wrike URL ID to the alphanumeric API ID for an attachment."""
    url = f"{wrike.WRIKE_BASE_URL}/ids"
    r = requests.get(
        url,
        headers=wrike.WRIKE_DEFAULT_HEADERS,
        params={"ids": f"[{permalink_id}]", "type": "ApiV2Attachment"},
    )
    r.raise_for_status()
    data = r.json().get("data", [])
    if not data:
        raise SystemExit(
            f"Permalink {permalink_id} did not resolve to an attachment. "
            "It may be a task or folder permalink — pass the attachment's permalink instead."
        )
    return data[0]["id"]


def main():
    parser = argparse.ArgumentParser()
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--attachment-id", help="Wrike API attachment id (alphanumeric).")
    group.add_argument(
        "--from-permalink",
        help="Numeric Wrike URL id; will be resolved to the API id automatically.",
    )
    parser.add_argument("--config", default="config.json")
    args = parser.parse_args()

    wrike = WrikeApi(args.config)

    if args.from_permalink:
        attachment_id = resolve_permalink(wrike, args.from_permalink)
        print(f"Resolved permalink {args.from_permalink} -> {attachment_id}")
    else:
        attachment_id = args.attachment_id

    meta = wrike.get_attachment(attachment_id)
    if meta is None:
        print(f"Attachment {attachment_id} not found (404).", file=sys.stderr)
        sys.exit(2)

    task_id = meta.get("taskId")
    if not task_id:
        print(f"Attachment is not task-scoped (scope: {meta.get('scope')}).", file=sys.stderr)
        sys.exit(3)

    name = meta["name"]
    print(f"Converting attachment {attachment_id} (name={name}, task={task_id})...")

    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        input_path = tmp_path / name

        # Download
        response = wrike.download_attachment_by_id(attachment_id)
        response.raise_for_status()
        with open(input_path, "wb") as fh:
            for chunk in response.iter_content(chunk_size=65536):
                fh.write(chunk)
        print(f"  downloaded {input_path.stat().st_size} bytes")

        # Convert
        try:
            pdf_path = convert_to_pdf(input_path, tmp_path)
        except ConvertError as e:
            print(f"  conversion failed: {e.error_code} -- {e.stderr[:200]}", file=sys.stderr)
            sys.exit(4)
        print(f"  converted to {pdf_path.name} ({pdf_path.stat().st_size} bytes)")

        # Upload
        upload_name = f"preview_{attachment_id}.pdf"
        new_id = wrike.add_file_to_task(task_id, pdf_path, upload_name=upload_name)
        print(f"  uploaded as {upload_name} (new attachment id: {new_id})")


if __name__ == "__main__":
    main()
