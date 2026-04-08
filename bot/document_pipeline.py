from __future__ import annotations

import io

TEXT_EXTENSIONS = frozenset({
    "txt", "md", "csv", "json", "yaml", "yml",
    "py", "js", "ts", "jsx", "tsx", "html", "htm", "xml",
    "toml", "ini", "cfg", "log", "sh", "bash", "sql",
    "rs", "go", "java", "c", "cpp", "h", "cs", "rb", "php",
    "swift", "kt", "r", "tex",
})


def _ext(file_name: str) -> str:
    if "." in file_name:
        return file_name.rsplit(".", 1)[1].lower()
    return ""


def _is_pdf(file_name: str, mime_type: str | None) -> bool:
    if _ext(file_name) == "pdf":
        return True
    if mime_type and "pdf" in mime_type.lower():
        return True
    return False


def _extract_pdf_text(data: bytes) -> str:
    from pypdf import PdfReader  # noqa: PLC0415

    reader = PdfReader(io.BytesIO(data))
    pages: list[str] = []
    for page in reader.pages:
        text = page.extract_text()
        if text and text.strip():
            pages.append(text.strip())
    return "\n\n".join(pages)


def extract_document_text(
    data: bytes,
    file_name: str,
    mime_type: str | None,
    max_chars: int,
) -> str:
    """Extract plain text from a document. Raises ValueError for unsupported formats."""
    if _is_pdf(file_name, mime_type):
        text = _extract_pdf_text(data)
        source = "pdf"
    elif _ext(file_name) in TEXT_EXTENSIONS:
        text = data.decode("utf-8", errors="replace")
        source = "text"
    else:
        ext = _ext(file_name) or "неизвестный"
        raise ValueError(
            f"Формат .{ext} не поддерживается.\n"
            "Поддерживаются: PDF и текстовые файлы (txt, md, py, js, csv, json, yaml и др.)."
        )

    if not text.strip():
        raise ValueError("Файл пустой или текст не удалось извлечь.")

    if len(text) > max_chars:
        text = text[:max_chars] + f"\n\n…[текст обрезан: показаны первые {max_chars} символов]"

    _ = source  # used for future metadata; suppresses unused-variable lint
    return text
