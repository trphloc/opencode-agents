#!/usr/bin/env python3
"""Convert a .docx file to Markdown using mammoth + markdownify.

Extracts header/footer content via python-docx and prepends/appends them
to the converted Markdown output.
"""

import sys
from pathlib import Path

import mammoth
from docx import Document
from markdownify import markdownify as md


def _extract_header_footer(input_path: Path) -> tuple[str, str]:
    """Extract header and footer text from all sections of a DOCX file.

    Returns (header_text, footer_text) as plain strings.
    Deduplicates identical headers/footers across sections.
    """
    doc = Document(str(input_path))
    headers: list[str] = []
    footers: list[str] = []

    for section in doc.sections:
        # --- Headers (prefer first-page header if it exists, else default) ---
        for hdr in (section.first_page_header, section.header):
            if hdr and hdr.is_linked_to_previous is False or (hdr and hdr.paragraphs):
                text = "\n".join(
                    p.text.strip() for p in hdr.paragraphs if p.text.strip()
                )
                if text and text not in headers:
                    headers.append(text)
                break  # use the first non-empty header found

        # --- Footers (prefer first-page footer if it exists, else default) ---
        for ftr in (section.first_page_footer, section.footer):
            if ftr and ftr.is_linked_to_previous is False or (ftr and ftr.paragraphs):
                text = "\n".join(
                    p.text.strip() for p in ftr.paragraphs if p.text.strip()
                )
                if text and text not in footers:
                    footers.append(text)
                break  # use the first non-empty footer found

    return "\n\n".join(headers), "\n\n".join(footers)


def convert_docx_to_md(input_path: Path) -> Path:
    """Convert a single .docx file to .md in the same directory."""
    output_path = input_path.with_suffix(".md")

    # Step 1: Extract header/footer via python-docx
    header_text, footer_text = _extract_header_footer(input_path)

    # Step 2: docx -> HTML (mammoth preserves structure: headings, lists, tables, bold/italic)
    with open(input_path, "rb") as f:
        result = mammoth.convert_to_html(f)

    html = result.value

    # Show warnings if any (e.g. unsupported styles)
    if result.messages:
        for msg in result.messages:
            print(f"  ⚠ {msg}", file=sys.stderr)

    # Step 3: HTML -> Markdown (markdownify handles clean conversion)
    markdown = md(html, heading_style="ATX", strip=["img"])

    # Step 4: Assemble final output with header/footer
    parts: list[str] = []

    if header_text:
        parts.append(
            f"> **Header**\n>\n> {header_text.replace(chr(10), chr(10) + '> ')}"
        )
        parts.append("---")
        print(f"  ℹ Header extracted", file=sys.stderr)

    parts.append(markdown)

    if footer_text:
        parts.append("---")
        parts.append(
            f"> **Footer**\n>\n> {footer_text.replace(chr(10), chr(10) + '> ')}"
        )
        print(f"  ℹ Footer extracted", file=sys.stderr)

    # Write output
    with open(output_path, "w", encoding="utf-8") as f:
        f.write("\n\n".join(parts))

    return output_path


def main():
    if len(sys.argv) < 2:
        print("Usage: python docx2md.py input.docx [input2.docx ...]")
        sys.exit(1)

    for arg in sys.argv[1:]:
        input_file = Path(arg).expanduser().resolve()

        if not input_file.exists():
            print(f"✗ File not found: {input_file}", file=sys.stderr)
            sys.exit(1)

        if input_file.suffix.lower() != ".docx":
            print(f"✗ Not a .docx file: {input_file}", file=sys.stderr)
            sys.exit(1)

        output_file = convert_docx_to_md(input_file)
        print(f"✓ Converted {input_file} → {output_file}")


if __name__ == "__main__":
    main()
