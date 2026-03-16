#!/usr/bin/env python3
import sys
from pathlib import Path
from docling.datamodel.base_models import InputFormat
from docling.datamodel.pipeline_options import PdfPipelineOptions
from docling.document_converter import DocumentConverter, PdfFormatOption

if len(sys.argv) != 2:
    print("Usage: python pdf2md.py input.pdf")
    sys.exit(1)

input_file = Path(sys.argv[1])
output_file = input_file.with_suffix('.md')

# Cấu hình pipeline options
pipeline_options = PdfPipelineOptions(
    do_ocr=True,           # OCR cho PDF scan
    do_table_structure=True # Extract bảng
)

# Tạo converter với format options MỚI
converter = DocumentConverter(
    format_options={
        InputFormat.PDF: PdfFormatOption(pipeline_options=pipeline_options)
    }
)

result = converter.convert(input_file)
markdown = result.document.export_to_markdown()

with open(output_file, 'w', encoding='utf-8') as f:
    f.write(markdown)

print(f"✓ Converted {input_file} → {output_file}")
