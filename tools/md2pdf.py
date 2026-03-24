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


def _img_bytes_to_data_uri(img_bytes: bytes, fmt: str = "PNG") -> str:
    """Encode raw image bytes as a base64 PNG data URI."""
    data = base64.b64encode(img_bytes).decode("ascii")
    return f"data:image/png;base64,{data}"


def _footer_with_pagenum(
    img_path: Path, page: int, total: int, page_width_cm: float = 17.0
) -> bytes:
    """Return PNG bytes of the footer image with 'page / total' drawn on it.

    Text is placed at the right side, vertically centred, in white with a
    subtle dark shadow for legibility on any background colour.
    Falls back to raw image bytes if Pillow is unavailable.
    """
    try:
        from PIL import Image, ImageDraw, ImageFont  # type: ignore

        with Image.open(img_path) as im:
            img = im.convert("RGBA")

        w, h = img.size
        draw = ImageDraw.Draw(img)

        label = f"{page} / {total}"

        # Font size: target ~9pt at 96 dpi scaled to image width
        # A4 content width = 17cm = ~643px at 96dpi; image fills that width
        scale = w / (page_width_cm * 96 / 2.54)
        font_px = max(18, int(18 * scale))

        font: ImageFont.FreeTypeFont | ImageFont.ImageFont
        for font_path in [
            "/Library/Fonts/Arial Unicode.ttf",
            "/System/Library/Fonts/Helvetica.ttc",
            "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        ]:
            try:
                font = ImageFont.truetype(font_path, size=font_px)
                break
            except Exception:
                continue
        else:
            font = ImageFont.load_default()

        # Measure text
        bbox = draw.textbbox((0, 0), label, font=font)
        tw, th = bbox[2] - bbox[0], bbox[3] - bbox[1]

        # Position: right margin 1.5% of width, vertically centred
        right_margin = int(w * 0.015)
        x = w - tw - right_margin
        y = (h - th) // 2

        # Shadow
        draw.text((x + 1, y + 1), label, font=font, fill=(0, 0, 0, 160))
        # White text
        draw.text((x, y), label, font=font, fill=(255, 255, 255, 255))

        import io

        buf = io.BytesIO()
        img.save(buf, format="PNG")
        return buf.getvalue()

    except Exception:
        # Pillow unavailable or error — return raw bytes of original image
        return img_path.read_bytes()


def _count_pdf_pages(pdf_bytes: bytes) -> int:
    """Return the number of pages in a PDF given its raw bytes."""
    try:
        import pypdfium2 as pdfium  # type: ignore

        doc = pdfium.PdfDocument(pdf_bytes)
        pages = list(doc)
        n = len(pages)
        for p in pages:
            p.close()
        doc.close()
        return n
    except Exception:
        return 1


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
    footer_nth_css: str = "",
) -> str:
    """Build the full CSS with header/footer guaranteed on every page.

    Strategy:
      - TEXT  → injected as CSS content: "..." in @page margin boxes.
      - IMAGE header → position:running() HTML element, <img width="100%">
      - IMAGE footer → @page:nth(n) rules, one running element per page,
                       each with the footer image + page number baked in via Pillow.
                       footer_nth_css is pre-built by convert_md_to_pdf().
    """

    def _esc(t: str) -> str:
        return t.replace("\\", "\\\\").replace('"', '\\"').replace("\n", " ")

    use_header_running = False

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

    # Page number format: "X / Y" (no "Trang" prefix)
    _page_num_css = 'counter(page) " / " counter(pages)'

    # ---- @bottom-center ----
    # Priority: footer_text > footer_img (handled via nth_css) > page numbers only > none
    bottom_right_content = ""
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
        # footer content handled by @page:nth rules in footer_nth_css
        bottom_content = "content: none;"
        bottom_style = ""
    elif show_page_number:
        bottom_content = "content: none;"
        bottom_style = ""
        margin_bottom = "2.5cm"
        bottom_right_content = f"content: {_page_num_css};"
    else:
        bottom_content = "content: none;"
        bottom_style = ""
        margin_bottom = "2.5cm"

    # Running element CSS for header image
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
{footer_nth_css}
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


def _build_html(
    title: str,
    css: str,
    running_divs: str,
    body_html: str,
) -> str:
    """Assemble a full HTML document ready for WeasyPrint."""
    return f"""<!DOCTYPE html>
<html lang="vi">
<head>
    <meta charset="utf-8">
    <title>{html_mod.escape(title)}</title>
    <style>
{css}
    </style>
</head>
<body>
{running_divs}{body_html}
</body>
</html>"""


def convert_md_to_pdf(
    input_path: Path,
    output_path: Path | None = None,
    header: str | None = None,
    footer: str | None = None,
    css: str | None = None,
    no_page_number: bool = False,
) -> tuple[Path, dict]:
    """Convert a single .md file to .pdf.

    When a footer IMAGE is used, a 2-pass strategy is applied:
      Pass 1: render a no-footer-image PDF → count total pages.
      Pass 2: bake per-page page numbers into footer PNGs (Pillow),
              inject one @page:nth(n) rule + one running <div> per page,
              render the final PDF.

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

    # 6. Markdown → HTML body
    md_conv = markdown.Markdown(
        extensions=MD_EXTENSIONS, extension_configs=MD_EXTENSION_CONFIGS
    )
    body_html = md_conv.convert(md_text)

    # 7. Title: frontmatter > first H1 > filename
    title = frontmatter.get("title") or ""
    if not title:
        m = re.search(r"<h1[^>]*>(.*?)</h1>", body_html)
        title = re.sub(r"<[^>]+>", "", m.group(1)) if m else input_path.stem

    # 8. Resolve the actual footer image path (if any)
    footer_img_path: Path | None = None
    if resolved_footer_img and not resolved_footer_text and not css:
        candidate = (base_dir / resolved_footer_img).resolve()
        if candidate.exists():
            footer_img_path = candidate

    # 9. Build header running-div (used in both passes)
    header_running_div = ""
    if resolved_header_img and not css:
        header_img_path = (base_dir / resolved_header_img).resolve()
        if header_img_path.exists():
            data_uri = _img_to_data_uri(header_img_path)
            header_running_div = (
                f'<div id="page-header"><img src="{data_uri}" alt="header"></div>\n'
            )

    # -----------------------------------------------------------------------
    # 10. TWO-PASS when footer image is present; single-pass otherwise
    # -----------------------------------------------------------------------
    if footer_img_path and not css:
        # ---- PASS 1: render without footer image to count pages ----
        css_pass1 = _build_css(
            header_text=resolved_header_text,
            header_img=resolved_header_img,
            footer_text=resolved_footer_text,
            footer_img=None,  # no footer in pass 1
            show_page_number=False,  # we don't need page nums in pass 1
            base_dir=base_dir,
            footer_nth_css="",
        )
        html_pass1 = _build_html(title, css_pass1, header_running_div, body_html)
        pdf_bytes_pass1 = (
            HTML(string=html_pass1, base_url=str(base_dir)).write_pdf() or b""
        )
        total_pages = _count_pdf_pages(pdf_bytes_pass1)

        # ---- Build per-page footer PNGs + CSS + divs ----
        footer_nth_parts: list[str] = []
        footer_running_css_parts: list[str] = []
        footer_div_parts: list[str] = []

        f_cm = _img_height_cm(footer_img_path)

        for n in range(1, total_pages + 1):
            png_bytes = _footer_with_pagenum(
                footer_img_path,
                page=n,
                total=total_pages,
            )
            data_uri = _img_bytes_to_data_uri(png_bytes, fmt="PNG")
            elem_name = f"page-footer-{n}"
            div_id = f"page-footer-{n}"

            # @page:nth(n) rule
            footer_nth_parts.append(
                f"@page:nth({n}) {{\n"
                f"    @bottom-center {{\n"
                f"        content: element({elem_name});\n"
                f"        height: {f_cm:.3f}cm;\n"
                f"        vertical-align: bottom;\n"
                f"        width: 100%;\n"
                f"    }}\n"
                f"}}"
            )

            # running element CSS
            footer_running_css_parts.append(
                f"#{div_id} {{\n"
                f"    position: running({elem_name});\n"
                f"    width: 100%;\n"
                f"    margin: 0; padding: 0;\n"
                f"}}\n"
                f"#{div_id} img {{\n"
                f"    display: block;\n"
                f"    width: 100%;\n"
                f"    height: auto;\n"
                f"}}"
            )

            # HTML div
            footer_div_parts.append(
                f'<div id="{div_id}"><img src="{data_uri}" alt="footer page {n}"></div>'
            )

        footer_nth_css = "\n".join(footer_nth_parts)
        footer_running_css = "\n".join(footer_running_css_parts)
        footer_divs = "\n".join(footer_div_parts)

        # ---- PASS 2: render with per-page footer images ----
        css_pass2 = _build_css(
            header_text=resolved_header_text,
            header_img=resolved_header_img,
            footer_text=resolved_footer_text,
            footer_img=str(footer_img_path),  # only used for margin calc
            show_page_number=not no_page_number,
            base_dir=base_dir,
            footer_nth_css=footer_nth_css + "\n" + footer_running_css,
        )
        running_divs = header_running_div + footer_divs + "\n"
        html_pass2 = _build_html(title, css_pass2, running_divs, body_html)
        HTML(string=html_pass2, base_url=str(base_dir)).write_pdf(str(output_path))

    else:
        # ---- SINGLE PASS (no footer image) ----
        final_css = css or _build_css(
            header_text=resolved_header_text,
            header_img=resolved_header_img,
            footer_text=resolved_footer_text,
            footer_img=resolved_footer_img,
            show_page_number=not no_page_number,
            base_dir=base_dir,
        )
        running_divs = header_running_div
        html_doc = _build_html(title, final_css, running_divs, body_html)
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
