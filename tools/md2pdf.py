#!/usr/bin/env python3
"""Convert a Markdown file to PDF using Markdown + WeasyPrint.

Pipeline: .md → HTML (python-markdown) → PDF (WeasyPrint with CSS @page rules)

Header/Footer strategy (priority order):
  1. CLI flags --header / --footer (text)
  2. Markdown frontmatter keys: pdf-header / pdf-footer (text)
  3. Auto-detect image pattern at top/bottom of body (docx2md output):
       > ![header](image.jpg)   ← first non-blank line
       > ![footer](image.jpg)   ← last non-blank line
     The image is stripped from the body and placed into @page margin via
     CSS  content: url("data:...")  — embedded as base64, on EVERY page.
  4. Default: no header; footer = page numbers only ("Trang X / Y")

All header/footer content is injected as pure CSS @page margin-box rules,
ensuring it appears on EVERY page regardless of document flow.
"""

import argparse
import base64
import html as html_mod
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
# Markdown extensions
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
    "codehilite": {"css_class": "highlight", "linenums": False, "guess_lang": True},
    "toc": {"permalink": False},
}

# Matches:  > ![header](path)  > ![footer](path)  (docx2md output pattern)
_BLOCKQUOTE_IMG_RE = re.compile(
    r"^>\s*!\[(?:header|footer|[^\]]*)\]\(([^)]+)\)\s*$",
    re.IGNORECASE,
)
# Also matches bare:  ![header](path)  ![footer](path)
_BARE_IMG_RE = re.compile(
    r"^!\[(?:header|footer)\]\(([^)]+)\)\s*$",
    re.IGNORECASE,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _parse_frontmatter(text: str) -> tuple[dict, str]:
    """Extract YAML frontmatter (---...---) and return (meta_dict, body)."""
    meta: dict = {}
    m = re.match(r"^---\s*\n(.*?)\n---\s*\n", text, re.DOTALL)
    if m:
        for line in m.group(1).strip().splitlines():
            if ":" in line:
                k, _, v = line.partition(":")
                meta[k.strip().lower()] = v.strip().strip('"').strip("'")
        text = text[m.end() :]
    return meta, text


def _match_img_line(line: str) -> str | None:
    """Return image path if line is a header/footer image pattern, else None."""
    for pat in (_BLOCKQUOTE_IMG_RE, _BARE_IMG_RE):
        m = pat.match(line.strip())
        if m:
            return m.group(1)
    return None


def _detect_body_images(md_text: str) -> tuple[str | None, str | None, str]:
    """Auto-detect and strip header/footer image lines from the Markdown body.

    Scans the FIRST non-blank line for a header image and the LAST non-blank
    line for a footer image.  Both lines (plus surrounding blank lines and a
    lone horizontal rule) are removed from the body so they don't appear in
    the main content area.

    Returns (header_img_path, footer_img_path, cleaned_md_text).
    """
    lines = md_text.splitlines()
    header_img: str | None = None
    footer_img: str | None = None
    header_idx: int | None = None
    footer_idx: int | None = None

    # First non-blank line → potential header
    for i, line in enumerate(lines):
        if line.strip():
            p = _match_img_line(line)
            if p:
                header_img = p
                header_idx = i
            break

    # Last non-blank line → potential footer
    for i in range(len(lines) - 1, -1, -1):
        if lines[i].strip():
            p = _match_img_line(lines[i])
            if p:
                footer_img = p
                footer_idx = i
            break

    remove: set[int] = set()

    if header_idx is not None:
        remove.add(header_idx)
        # Strip trailing blank lines + optional lone "---"
        j = header_idx + 1
        while j < len(lines) and not lines[j].strip():
            remove.add(j)
            j += 1
        if j < len(lines) and lines[j].strip() in ("---", "***", "___"):
            remove.add(j)
            j += 1
        while j < len(lines) and not lines[j].strip():
            remove.add(j)
            j += 1

    if footer_idx is not None and footer_idx not in remove:
        remove.add(footer_idx)
        # Strip preceding blank lines + optional lone "---"
        j = footer_idx - 1
        while j >= 0 and not lines[j].strip():
            remove.add(j)
            j -= 1
        if j >= 0 and lines[j].strip() in ("---", "***", "___"):
            remove.add(j)
            j -= 1
        while j >= 0 and not lines[j].strip():
            remove.add(j)
            j -= 1

    cleaned = "\n".join(ln for i, ln in enumerate(lines) if i not in remove)
    return header_img, footer_img, cleaned


def _img_to_data_uri(img_path: Path) -> str:
    """Encode image as a base64 data URI so WeasyPrint embeds it inline."""
    ext = img_path.suffix.lower().lstrip(".")
    mime = f"image/{'jpeg' if ext in ('jpg', 'jpeg') else ext}"
    data = base64.b64encode(img_path.read_bytes()).decode("ascii")
    return f"data:{mime};base64,{data}"


def _img_height_cm(img_path: Path, page_width_cm: float = 17.0) -> float:
    """Calculate the scaled height (cm) of an image fitted to page_width_cm.

    Reads the actual pixel dimensions via Pillow and preserves aspect ratio.
    Falls back to 2.5cm if Pillow is unavailable or the file can't be read.
    """
    try:
        from PIL import Image  # type: ignore

        with Image.open(img_path) as im:
            w_px, h_px = im.size
        return round(page_width_cm * h_px / w_px, 3)
    except Exception:
        return 2.5


def _build_css(
    header_text: str | None,
    header_img: str | None,
    footer_text: str | None,
    footer_img: str | None,
    show_page_number: bool,
    base_dir: Path,
) -> str:
    """Build the full CSS with header/footer guaranteed on every page.

    Strategy:
      - TEXT  → injected as CSS content: "..." in @page margin boxes.
                Pure CSS, always on every page.
      - IMAGE → injected as position:running() HTML element containing
                <img style="width:100%">.  WeasyPrint scales the image
                correctly when width:100% is set on the <img> tag inside
                a running element (unlike content:url() which ignores sizing).
    """

    def _esc(t: str) -> str:
        return t.replace("\\", "\\\\").replace('"', '\\"').replace("\n", " ")

    use_header_running = False
    use_footer_running = False

    # ---- @top-center ----
    if header_text:
        top_content = f'content: "{_esc(header_text)}";'
        top_style = "font-size: 9pt; color: #555; border-bottom: 0.5pt solid #ccc; padding-bottom: 3pt;"
        margin_top = "2.8cm"
    elif header_img and (base_dir / header_img).resolve().exists():
        h_cm = _img_height_cm((base_dir / header_img).resolve())
        margin_top = f"{h_cm + 0.3:.2f}cm"
        top_content = "content: element(page-header);"
        top_style = f"height: {h_cm:.3f}cm; vertical-align: bottom;"
        use_header_running = True
    else:
        top_content = "content: none;"
        top_style = ""
        margin_top = "2.5cm"

    # Page number format: "X / Y" (no "Trang" prefix), always @bottom-right
    _page_num_css = 'counter(page) " / " counter(pages)'

    # ---- @bottom-center ----
    # Priority: footer_text > footer_img > page numbers only > none
    bottom_right_content = ""  # page-number always in @bottom-right
    if footer_text:
        parts: list[str] = [f'"{_esc(footer_text)}"']
        if show_page_number:
            parts += ['" \u2014 "', _page_num_css]
        bottom_content = "content: " + " ".join(parts) + ";"
        bottom_style = "font-size: 9pt; color: #555; border-top: 0.5pt solid #ccc; padding-top: 3pt;"
        margin_bottom = "2.8cm"
    elif footer_img and (base_dir / footer_img).resolve().exists():
        f_cm = _img_height_cm((base_dir / footer_img).resolve())
        margin_bottom = f"{f_cm + 0.3:.2f}cm"
        bottom_content = "content: element(page-footer);"
        bottom_style = f"height: {f_cm:.3f}cm; vertical-align: top;"
        use_footer_running = True
        if show_page_number:
            bottom_right_content = f"content: {_page_num_css};"
    elif show_page_number:
        bottom_content = "content: none;"
        bottom_style = ""
        margin_bottom = "2.5cm"
        bottom_right_content = f"content: {_page_num_css};"
    else:
        bottom_content = "content: none;"
        bottom_style = ""
        margin_bottom = "2.5cm"

    # Running element CSS (only emitted when an image is used)
    running_css = ""
    if use_header_running:
        running_css += """
#page-header {
    position: running(page-header);
    width: 100%;
    margin: 0; padding: 0;
}
#page-header img {
    display: block;
    width: 100%;
    height: auto;
}
"""
    if use_footer_running:
        running_css += """
#page-footer {
    position: running(page-footer);
    width: 100%;
    margin: 0; padding: 0;
}
#page-footer img {
    display: block;
    width: 100%;
    height: auto;
}
"""

    bottom_right_block = (
        f"\n    @bottom-right {{\n        {bottom_right_content}\n        font-size: 9pt; color: #555; white-space: nowrap;\n    }}"
        if bottom_right_content
        else ""
    )

    return rf"""
/* ---------- Page layout ---------- */
@page {{
    size: A4;
    margin: {margin_top} 2cm {margin_bottom} 2cm;

    @top-center {{
        {top_content}
        {top_style}
        width: 100%;
    }}

    @bottom-center {{
        {bottom_content}
        {bottom_style}
        width: 100%;
    }}{bottom_right_block}
}}
{running_css}

/* ---------- Body typography ---------- */
body {{
    font-family: "Noto Sans", "Helvetica Neue", Arial, sans-serif;
    font-size: 11pt;
    line-height: 1.6;
    color: #222;
}}

h1 {{ font-size: 22pt; margin-top: 24pt; margin-bottom: 8pt;  color: #111; }}
h2 {{ font-size: 18pt; margin-top: 20pt; margin-bottom: 6pt;  color: #222; }}
h3 {{ font-size: 14pt; margin-top: 16pt; margin-bottom: 4pt;  color: #333; }}
h4, h5, h6 {{ font-size: 12pt; margin-top: 12pt; margin-bottom: 4pt; color: #444; }}

p {{ margin-top: 6pt; margin-bottom: 6pt; }}
a {{ color: #0366d6; text-decoration: none; }}

blockquote {{
    border-left: 3pt solid #ddd;
    padding-left: 12pt;
    margin-left: 0;
    color: #555;
    font-style: italic;
}}

/* ---------- Code ---------- */
code {{
    font-family: "Fira Code", "Consolas", "Monaco", monospace;
    font-size: 9.5pt;
    background: #f5f5f5;
    padding: 1pt 4pt;
    border-radius: 3pt;
}}
pre {{
    background: #f8f8f8;
    border: 0.5pt solid #e1e1e1;
    border-radius: 4pt;
    padding: 10pt;
    overflow-x: auto;
    font-size: 9pt;
    line-height: 1.4;
}}
pre code  {{ background: none; padding: 0; }}
.highlight pre {{ background: #f8f8f8; }}

/* ---------- Tables ---------- */
table {{
    border-collapse: collapse;
    width: 100%;
    margin: 12pt 0;
    font-size: 10pt;
}}
th, td {{
    border: 0.5pt solid #ccc;
    padding: 6pt 10pt;
    text-align: left;
}}
th {{ background: #f0f0f0; font-weight: bold; }}
tr:nth-child(even) {{ background: #fafafa; }}

/* ---------- Lists ---------- */
ul, ol {{ padding-left: 24pt; margin: 6pt 0; }}
li {{ margin-bottom: 3pt; }}

/* ---------- Misc ---------- */
hr  {{ border: none; border-top: 1pt solid #ddd; margin: 16pt 0; }}
img {{ max-width: 100%; height: auto; }}
"""


# ---------------------------------------------------------------------------
# Main conversion
# ---------------------------------------------------------------------------


def convert_md_to_pdf(
    input_path: Path,
    output_path: Path | None = None,
    header: str | None = None,
    footer: str | None = None,
    css: str | None = None,
    no_page_number: bool = False,
) -> tuple[Path, dict]:
    """Convert a single .md file to .pdf.

    Returns (output_path, info_dict) for status reporting.
    """
    if output_path is None:
        output_path = input_path.with_suffix(".pdf")
    base_dir = input_path.parent

    # 1. Read source
    md_text = input_path.read_text(encoding="utf-8")

    # 2. Strip YAML frontmatter
    frontmatter, md_text = _parse_frontmatter(md_text)

    # 3. Auto-detect + strip header/footer images from body
    body_header_img, body_footer_img, md_text = _detect_body_images(md_text)

    # 4. Resolve text: CLI > frontmatter  (text beats auto-detected image)
    resolved_header_text = (
        header or frontmatter.get("pdf-header") or frontmatter.get("header")
    )
    resolved_footer_text = (
        footer or frontmatter.get("pdf-footer") or frontmatter.get("footer")
    )

    # 5. Resolve image: only used when no text provided
    resolved_header_img = None if resolved_header_text else body_header_img
    resolved_footer_img = None if resolved_footer_text else body_footer_img

    # 6. Markdown → HTML
    md_conv = markdown.Markdown(
        extensions=MD_EXTENSIONS, extension_configs=MD_EXTENSION_CONFIGS
    )
    body_html = md_conv.convert(md_text)

    # 7. Title: frontmatter > first H1 > filename
    title = frontmatter.get("title") or ""
    if not title:
        m = re.search(r"<h1[^>]*>(.*?)</h1>", body_html)
        title = re.sub(r"<[^>]+>", "", m.group(1)) if m else input_path.stem

    # 8. CSS
    final_css = css or _build_css(
        header_text=resolved_header_text,
        header_img=resolved_header_img,
        footer_text=resolved_footer_text,
        footer_img=resolved_footer_img,
        show_page_number=not no_page_number,
        base_dir=base_dir,
    )

    # 9. Build running-element HTML divs for image header/footer
    #    (needed when _build_css emits content: element(page-header/footer))
    running_divs = ""
    if resolved_header_img:
        header_img_path = (base_dir / resolved_header_img).resolve()
        if header_img_path.exists():
            data_uri = _img_to_data_uri(header_img_path)
            running_divs += (
                f'<div id="page-header"><img src="{data_uri}" alt="header"></div>\n'
            )
    if resolved_footer_img:
        footer_img_path = (base_dir / resolved_footer_img).resolve()
        if footer_img_path.exists():
            data_uri = _img_to_data_uri(footer_img_path)
            running_divs += (
                f'<div id="page-footer"><img src="{data_uri}" alt="footer"></div>\n'
            )

    # 10. Assemble + render
    html_doc = f"""<!DOCTYPE html>
<html lang="vi">
<head>
    <meta charset="utf-8">
    <title>{html_mod.escape(title)}</title>
    <style>
{final_css}
    </style>
</head>
<body>
{running_divs}{body_html}
</body>
</html>"""

    HTML(string=html_doc, base_url=str(base_dir)).write_pdf(str(output_path))

    info = {
        "header_text": resolved_header_text,
        "header_img": resolved_header_img,
        "footer_text": resolved_footer_text,
        "footer_img": resolved_footer_img,
        "page_numbers": not no_page_number,
    }
    return output_path, info


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main():
    parser = argparse.ArgumentParser(
        description="Convert Markdown to PDF with header/footer on every page.",
        epilog="""
Header/footer priority:
  1. --header / --footer CLI flags (text)
  2. pdf-header / pdf-footer in YAML frontmatter (text)
  3. Auto-detected image: first line  > ![header](img.jpg)
                          last line   > ![footer](img.jpg)
  4. Default: no header; footer = page numbers only

Examples:
  python md2pdf.py report.md
  python md2pdf.py report.md --header "Company Inc." --footer "Confidential"
  python md2pdf.py report.md --no-page-number
  python md2pdf.py doc1.md doc2.md --header "Draft v2"
""",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "input", nargs="+", help="One or more Markdown (.md) file paths"
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
        "--no-page-number", action="store_true", help="Disable automatic page numbering"
    )
    parser.add_argument(
        "-o", "--output", default=None, help="Output PDF path (single file only)"
    )

    args = parser.parse_args()

    if args.output and len(args.input) > 1:
        print("✗ --output can only be used with a single input file", file=sys.stderr)
        sys.exit(1)

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
        output_file, info = convert_md_to_pdf(
            input_path=input_file,
            output_path=out,
            header=args.header,
            footer=args.footer,
            css=custom_css,
            no_page_number=args.no_page_number,
        )

        if info["header_text"]:
            print(f"  ℹ Header (text):  {info['header_text']}", file=sys.stderr)
        elif info["header_img"]:
            print(f"  ℹ Header (image): {info['header_img']}", file=sys.stderr)
        if info["footer_text"]:
            print(f"  ℹ Footer (text):  {info['footer_text']}", file=sys.stderr)
        elif info["footer_img"]:
            print(f"  ℹ Footer (image): {info['footer_img']}", file=sys.stderr)
        if info["page_numbers"]:
            print("  ℹ Page numbers: enabled", file=sys.stderr)

        print(f"✓ Converted {input_file} → {output_file}")


if __name__ == "__main__":
    main()
