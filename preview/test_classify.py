from classify import ClassifyResult, classify_attachment


def _attach(name, mime=None, size=1000, scope="task", task_id="T1"):
    return {
        "name": name,
        "mimeType": mime,
        "size": size,
        "scope": scope,
        "taskId": task_id,
    }


def test_office_docx_is_convert():
    r = classify_attachment(_attach("report.docx"))
    assert r == ClassifyResult(action="convert", reason=None)


def test_pdf_is_skip_already_previewable():
    r = classify_attachment(_attach("doc.pdf"))
    assert r == ClassifyResult(action="skip", reason="already_previewable")


def test_image_is_skip_already_previewable():
    r = classify_attachment(_attach("photo.jpg"))
    assert r == ClassifyResult(action="skip", reason="already_previewable")


def test_preview_filename_v1_legacy_is_skip_self_output():
    # Legacy format: preview_<id>.pdf
    r = classify_attachment(_attach("preview_IEAAA12345.pdf"))
    assert r == ClassifyResult(action="skip", reason="filename_is_preview")


def test_preview_filename_v2_is_skip_self_output():
    # Current format: <originalStem>_<id>_preview.pdf
    r = classify_attachment(_attach("report_IEAAA12345_preview.pdf"))
    assert r == ClassifyResult(action="skip", reason="filename_is_preview")


def test_preview_filename_v2_with_spaces_is_skip_self_output():
    # Real Wrike filenames have spaces; v2 regex must still match.
    r = classify_attachment(_attach("STONE MT MANAGEMENT 2026_IEAENETVIYVQEIXO_preview.pdf"))
    assert r == ClassifyResult(action="skip", reason="filename_is_preview")


def test_oversize_is_skip_too_large():
    r = classify_attachment(_attach("big.docx", size=60 * 1024 * 1024))
    assert r == ClassifyResult(action="skip", reason="too_large")


def test_text_file_is_skip_wrong_type():
    r = classify_attachment(_attach("notes.txt"))
    assert r == ClassifyResult(action="skip", reason="wrong_type")


def test_comment_scope_is_skip_wrong_scope():
    r = classify_attachment(_attach("report.docx", scope="comment"))
    assert r == ClassifyResult(action="skip", reason="wrong_scope")


def test_folder_scope_is_skip_wrong_scope():
    r = classify_attachment(_attach("report.docx", scope="folder"))
    assert r == ClassifyResult(action="skip", reason="wrong_scope")


def test_preview_guard_fires_before_extension():
    # preview_<id>.pdf regex won't match preview_foo.docx, so this falls
    # through to the docx-convert branch — that's the intended behavior.
    r = classify_attachment(_attach("preview_foo.docx"))
    assert r == ClassifyResult(action="convert", reason=None)
