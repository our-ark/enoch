"""Bounded PDF preview worker; invoked directly with the runtime interpreter."""
import json
import sys

MAX_PAGES = 100
MAX_CHARS = 12_000


def extract(path: str) -> dict:
    from pypdf import PdfReader

    reader = PdfReader(path)
    if reader.is_encrypted and not reader.decrypt(""):
        return {"status": "PDF stored; password required to extract text"}
    pages = len(reader.pages)
    chunks = []
    remaining = MAX_CHARS
    visited = 0
    truncated = False
    for page in reader.pages[:MAX_PAGES]:
        text = page.extract_text() or ""
        visited += 1
        if len(text) > remaining:
            truncated = True
        chunks.append(f"[Page {visited}]\n" + text[:remaining])
        remaining -= min(len(text), remaining)
        if remaining <= 0:
            break
    has_text = any(chunk.split("\n", 1)[1].strip() for chunk in chunks)
    return {"status": "text preview" if has_text else "PDF stored; no extractable text (scanned pages may need OCR)",
            "pages": pages, "preview_pages": visited,
            "truncated": truncated or visited < pages, "text": "\n\n".join(chunks)}


if __name__ == "__main__":
    try:
        import resource
        resource.setrlimit(resource.RLIMIT_CPU, (15, 15))
        if sys.platform == "linux":
            resource.setrlimit(resource.RLIMIT_AS, (768 * 1024 * 1024,) * 2)
        print(json.dumps(extract(sys.argv[1]), ensure_ascii=False))
    except Exception:
        # Parser errors can contain document contents; report only a fixed status.
        print(json.dumps({"status": "PDF stored; text extraction unavailable. Inspect the local file with PDF tools."}))
