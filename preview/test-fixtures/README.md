# Test fixtures

This directory is gitignored. Place small test files here before running Phase 1
smoke tests:

- `sample.docx` — a small (<100KB) Word document with mixed text + a table.
- `sample.xlsx` — a 2-sheet workbook with formulas.
- `sample.pptx` — a 3-slide deck with text + one image.
- `password.docx` — a Word doc protected with a password.
- `corrupt.docx` — a deliberately-mangled .docx (e.g., truncate the file).
- `huge.docx` — a 50MB+ DOCX (for Phase 3's size-preflight test).
- `not-office.zip` — any random zip file (for Phase 3's wrong-type test).
