"""One-off Wrike attachment → PDF preview tool.

Usage:
    python preview/cli.py --attachment-id IEAAAAAB

Requires:
    - config.json in the working directory with the Wrike API token (same as main.py).
    - LibreOffice on PATH (or run inside the preview Docker image).
"""

import argparse
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))  # so we can import wrike

from wrike import WrikeApi

from convert import ConvertError, convert_to_pdf


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--attachment-id", required=True)
    parser.add_argument("--config", default="config.json")
    args = parser.parse_args()

    wrike = WrikeApi(args.config)

    meta = wrike.get_attachment(args.attachment_id)
    if meta is None:
        print(f"Attachment {args.attachment_id} not found (404).", file=sys.stderr)
        sys.exit(2)

    task_id = meta.get("taskId")
    if not task_id:
        print(f"Attachment is not task-scoped (scope: {meta.get('scope')}).", file=sys.stderr)
        sys.exit(3)

    name = meta["name"]
    print(f"Converting attachment {args.attachment_id} (name={name}, task={task_id})...")

    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        input_path = tmp_path / name

        # Download
        response = wrike.download_attachment_by_id(args.attachment_id)
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
        upload_name = f"preview_{args.attachment_id}.pdf"
        new_id = wrike.add_file_to_task(task_id, pdf_path, upload_name=upload_name)
        print(f"  uploaded as {upload_name} (new attachment id: {new_id})")


if __name__ == "__main__":
    main()
