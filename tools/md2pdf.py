#!/usr/bin/env python3
"""Convert a Markdown file to PDF using Markdown + WeasyPrint.

Pipeline: .md → HTML (python-markdown) → PDF (WeasyPrint with CSS @page rules)

Supports:
- Custom header/footer text rendered on every PDF page via CSS @page margins
- Syntax highlighting for code blocks (Pygments)
- Tables, task lists, footnotes, table of contents
- Auto-detection of header/footer from Markdown frontmatter (YAML)
- Command-line override for header/footer text
- Page numbering (auto-inserted in footer if no custom footer provided)
"""

import argparse
import os
import platform
import re
import sys
from pathlib import Path

# macOS + Homebrew: WeasyPrint needs DYLD_FALLBACK_LIBRARY_PATH to find
# libgobject, libpango, etc. Set it early, before importing weasyprint.
if platform.system() == "Darwin":
    _brew_lib = "/opt/homebrew/lib"
    if os.path.isdir(_brew_lib):
        _current = os.environ.get("DYLD_FALLBACK_LIBRARY_PATH", "")
        if _brew_lib not in _current:
            os.environ["DYLD_FALLBACK_LIBRARY_PATH"] = (
                f"{_brew_lib}:{_current}" if _current else _brew_lib
            )

import markdown  # noqa: E402
from weasyprint import HTML  # noqa: E402

# ---------------------------------------------------------------------------
# Markdown extensions to enable
# ---------------------------------------------------------------------------
MD_EXTENSIONS = [
    "extra",  # tables, fenced_code, footnotes, attr_list, def_list, abbr
    "codehilite",  # syntax highlighting via Pygments
    "toc",  # table of contents [TOC]
    "meta",  # YAML-style metadata at top of file
    "sane_lists",  # better list handling
    "smarty",  # smart quotes
]

MD_EXTENSION_CONFIGS = {
    "codehilite": {
        "css_class": "highlight",
        "linenums": False,
        "guess_lang": True,
    },
    "toc": {
        "permalink": False,
    },
}

# ---------------------------------------------------------------------------
# Default CSS for the PDF (A4 page, clean typography, header/footer)
# ---------------------------------------------------------------------------
DEFAULT_CSS = r"""
/* ---------- Page layout ---------- */
@page {
    size: A4;
    margin: 2.5cm 2cm 2.5cm 2cm;

    @top-center {
        content: element(page-header);
        width: 100%;
    }

    @bottom-center {
        content: element(page-footer);
        width: 100%;
    }
}

/* First page: optionally different header */
@page :first {
    @top-center {
        content: element(page-header);
    }
}

/* Running elements — these are pulled out of flow and placed in margins */
#page-header {
    position: running(page-header);
    text-align: center;
    font-size: 9pt;
    color: #666;
    border-bottom: 0.5pt solid #ccc;
    padding-bottom: 4pt;
    margin-bottom: 8pt;
}

#page-footer {
    position: running(page-footer);
    text-align: center;
    font-size: 9pt;
    color: #666;
    border-top: 0.5pt solid #ccc;
    padding-top: 4pt;
    margin-top: 8pt;
}

/* Page counter in footer */
#page-footer .page-number::after {
    content: counter(page);
}
#page-footer .page-total::after {
    content: counter(pages);
}

/* ---------- Body typography ---------- */
body {
    font-family: "Noto Sans", "Helvetica Neue", Arial, sans-serif;
    font-size: 11pt;
    line-height: 1.6;
    color: #222;
}

h1 { font-size: 22pt; margin-top: 24pt; margin-bottom: 8pt; color: #111; }
h2 { font-size: 18pt; margin-top: 20pt; margin-bottom: 6pt; color: #222; }
h3 { font-size: 14pt; margin-top: 16pt; margin-bottom: 4pt; color: #333; }
h4, h5, h6 { font-size: 12pt; margin-top: 12pt; margin-bottom: 4pt; color: #444; }

p { margin-top: 6pt; margin-bottom: 6pt; }

a { color: #0366d6; text-decoration: none; }

blockquote {
    border-left: 3pt solid #ddd;
    padding-left: 12pt;
    margin-left: 0;
    color: #555;
    font-style: italic;
}

/* ---------- Code ---------- */
code {
    font-family: "Fira Code", "Consolas", "Monaco", monospace;
    font-size: 9.5pt;
    background: #f5f5f5;
    padding: 1pt 4pt;
    border-radius: 3pt;
}

pre {
    background: #f8f8f8;
    border: 0.5pt solid #e1e1e1;
    border-radius: 4pt;
    padding: 10pt;
    overflow-x: auto;
    font-size: 9pt;
    line-height: 1.4;
}

pre code {
    background: none;
    padding: 0;
}

/* Pygments highlight */
.highlight pre {
    background: #f8f8f8;
}

/* ---------- Tables ---------- */
table {
    border-collapse: collapse;
    width: 100%;
    margin: 12pt 0;
    font-size: 10pt;
}

th, td {
    border: 0.5pt solid #ccc;
    padding: 6pt 10pt;
    text-align: left;
}

th {
    background: #f0f0f0;
    font-weight: bold;
}

tr:nth-child(even) {
    background: #fafafa;
}

/* ---------- Lists ---------- */
ul, ol {
    padding-left: 24pt;
    margin: 6pt 0;
}

li {
    margin-bottom: 3pt;
}

/* ---------- Horizontal rule ---------- */
hr {
    border: none;
    border-top: 1pt solid #ddd;
    margin: 16pt 0;
}

/* ---------- Images ---------- */
img {
    max-width: 100%;
    height: auto;
}
"""


def _parse_frontmatter(text: str) -> tuple[dict, str]:
    """Extract YAML-ish frontmatter from Markdown text.

    Supports the common pattern:
        ---
        key: value
        ---

    Returns (metadata_dict, remaining_markdown).
    """
    meta: dict = {}
    pattern = re.compile(r"^---\s*\n(.*?)\n---\s*\n", re.DOTALL)
    match = pattern.match(text)
    if match:
        raw = match.group(1)
        for line in raw.strip().splitlines():
            if ":" in line:
                key, _, value = line.partition(":")
                meta[key.strip().lower()] = value.strip().strip('"').strip("'")
        text = text[match.end() :]
    return meta, text


def _build_header_html(header_text: str | None) -> str:
    """Build the running header HTML element."""
    if not header_text:
        return '<div id="page-header"></div>'
    # Support multi-line headers via \\n or literal newlines
    lines = header_text.replace("\\n", "\n").splitlines()
    inner = "<br>".join(line.strip() for line in lines if line.strip())
    return f'<div id="page-header">{inner}</div>'


def _build_footer_html(footer_text: str | None, show_page_number: bool = True) -> str:
    """Build the running footer HTML element."""
    parts: list[str] = []
    if footer_text:
        lines = footer_text.replace("\\n", "\n").splitlines()
        parts.append("<br>".join(line.strip() for line in lines if line.strip()))
    if show_page_number:
        parts.append(
            'Trang <span class="page-number"></span> / <span class="page-total"></span>'
        )
    if not parts:
        return '<div id="page-footer"></div>'
    inner = (
        " &mdash; ".join(parts) if footer_text and show_page_number else "".join(parts)
    )
    return f'<div id="page-footer">{inner}</div>'


def convert_md_to_pdf(
    input_path: Path,
    output_path: Path | None = None,
    header: str | None = None,
    footer: str | None = None,
    css: str | None = None,
    no_page_number: bool = False,
) -> Path:
    """Convert a single .md file to .pdf.

    Priority for header/footer:
    1. CLI arguments (--header / --footer)
    2. Frontmatter keys: pdf-header, pdf-footer
    3. Default: no header, page numbers only in footer
    """
    if output_path is None:
        output_path = input_path.with_suffix(".pdf")

    # Read Markdown source
    md_text = input_path.read_text(encoding="utf-8")

    # Parse frontmatter for metadata
    frontmatter, md_text = _parse_frontmatter(md_text)

    # Resolve header/footer: CLI > frontmatter > default
    resolved_header = (
        header or frontmatter.get("pdf-header") or frontmatter.get("header")
    )
    resolved_footer = (
        footer or frontmatter.get("pdf-footer") or frontmatter.get("footer")
    )

    # Convert Markdown → HTML
    md_converter = markdown.Markdown(
        extensions=MD_EXTENSIONS,
        extension_configs=MD_EXTENSION_CONFIGS,
    )
    body_html = md_converter.convert(md_text)

    # Resolve title: frontmatter > first H1 > filename
    title = frontmatter.get("title", "")
    if not title:
        h1_match = re.search(r"<h1[^>]*>(.*?)</h1>", body_html)
        if h1_match:
            title = re.sub(r"<[^>]+>", "", h1_match.group(1))
        else:
            title = input_path.stem

    # Build running elements
    header_html = _build_header_html(resolved_header)
    footer_html = _build_footer_html(
        resolved_footer, show_page_number=not no_page_number
    )

    # Use custom CSS or default
    final_css = css if css else DEFAULT_CSS

    # Assemble full HTML document
    html_doc = f"""<!DOCTYPE html>
<html lang="vi">
<head>
    <meta charset="utf-8">
    <title>{title}</title>
    <style>
{final_css}
    </style>
</head>
<body>
    {header_html}
    {footer_html}
    <main>
{body_html}
    </main>
</body>
</html>"""

    # Render PDF via WeasyPrint
    html_obj = HTML(
        string=html_doc,
        base_url=str(input_path.parent),  # resolve relative images
    )
    html_obj.write_pdf(str(output_path))

    return output_path


def main():
    parser = argparse.ArgumentParser(
        description="Convert Markdown to PDF with header/footer support.",
        epilog="""
Examples:
  python md2pdf.py report.md
  python md2pdf.py report.md --header "Company Inc." --footer "Confidential"
  python md2pdf.py report.md --no-page-number
  python md2pdf.py doc1.md doc2.md --header "Draft v2"

Header/footer can also be set via Markdown frontmatter:
  ---
  title: My Report
  pdf-header: Company Inc.
  pdf-footer: Confidential
  ---
""",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "input",
        nargs="+",
        help="One or more Markdown (.md) file paths",
    )
    parser.add_argument(
        "--header",
        default=None,
        help="Header text for every page (overrides frontmatter)",
    )
    parser.add_argument(
        "--footer",
        default=None,
        help="Footer text for every page (overrides frontmatter)",
    )
    parser.add_argument(
        "--css",
        default=None,
        help="Path to a custom CSS file (replaces default styling)",
    )
    parser.add_argument(
        "--no-page-number",
        action="store_true",
        help="Disable automatic page numbering in footer",
    )
    parser.add_argument(
        "-o",
        "--output",
        default=None,
        help="Output PDF path (only valid when converting a single file)",
    )

    args = parser.parse_args()

    # Validate single-file output option
    if args.output and len(args.input) > 1:
        print("✗ --output can only be used with a single input file", file=sys.stderr)
        sys.exit(1)

    # Load custom CSS if provided
    custom_css = None
    if args.css:
        css_path = Path(args.css).expanduser().resolve()
        if not css_path.exists():
            print(f"✗ CSS file not found: {css_path}", file=sys.stderr)
            sys.exit(1)
        custom_css = css_path.read_text(encoding="utf-8")

    for arg in args.input:
        input_file = Path(arg).expanduser().resolve()

        if not input_file.exists():
            print(f"✗ File not found: {input_file}", file=sys.stderr)
            sys.exit(1)

        if input_file.suffix.lower() != ".md":
            print(f"✗ Not a .md file: {input_file}", file=sys.stderr)
            sys.exit(1)

        out = Path(args.output).expanduser().resolve() if args.output else None

        output_file = convert_md_to_pdf(
            input_path=input_file,
            output_path=out,
            header=args.header,
            footer=args.footer,
            css=custom_css,
            no_page_number=args.no_page_number,
        )

        # Report header/footer status
        md_text = input_file.read_text(encoding="utf-8")
        fm, _ = _parse_frontmatter(md_text)
        h = args.header or fm.get("pdf-header") or fm.get("header")
        f = args.footer or fm.get("pdf-footer") or fm.get("footer")
        if h:
            print(f"  ℹ Header: {h}", file=sys.stderr)
        if f:
            print(f"  ℹ Footer: {f}", file=sys.stderr)
        if not args.no_page_number:
            print(f"  ℹ Page numbers: enabled", file=sys.stderr)

        print(f"✓ Converted {input_file} → {output_file}")


if __name__ == "__main__":
    main()
