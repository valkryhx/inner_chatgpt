from pathlib import Path
from zipfile import ZipFile
from xml.etree import ElementTree as ET
import re
import sys

from docx import Document
from docx.oxml import OxmlElement
from docx.oxml.ns import qn
from docx.shared import Pt


FONT = "\u4eff\u5b8b"  # 仿宋. Keep Unicode escape to avoid shell encoding issues.
STYLE_SIZES = {
    "Normal": 11,
    "normal": 11,
    "Heading 1": 24,
    "Heading 2": 18,
    "Heading 3": 14,
    "Heading 4": 12,
}
STYLE_BOLD = {
    "Heading 1": True,
    "Heading 2": True,
    "Heading 3": True,
    "Heading 4": True,
}
BODY_INDENT = Pt(22)
CITE_SIZE_PT = 8


def ensure_rfonts(rpr):
    rfonts = rpr.rFonts
    if rfonts is None:
        rfonts = OxmlElement("w:rFonts")
        rpr.insert(0, rfonts)
    return rfonts


def set_rfonts_on_element(rpr):
    rfonts = ensure_rfonts(rpr)
    for key in ("w:eastAsia", "w:ascii", "w:hAnsi", "w:cs"):
        rfonts.set(qn(key), FONT)


def set_font_obj(font, size=None, bold=None):
    font.name = FONT
    set_rfonts_on_element(font.element.rPr)
    if size is not None:
        font.size = Pt(size)
    if bold is not None:
        font.bold = bold


def set_run_font(run, size_pt=None, bold=None, superscript=None):
    run.font.name = FONT
    rpr = run._element.get_or_add_rPr()
    set_rfonts_on_element(rpr)
    if size_pt is not None:
        run.font.size = Pt(size_pt)
    if bold is not None:
        run.font.bold = bold
    if superscript is not None:
        run.font.superscript = superscript


def force_all_rfonts(root):
    for el in root.iter(qn("w:rFonts")):
        for key in ("w:eastAsia", "w:ascii", "w:hAnsi", "w:cs"):
            el.set(qn(key), FONT)
        for key in ("w:asciiTheme", "w:hAnsiTheme", "w:eastAsiaTheme", "w:csTheme", "w:cstheme"):
            attr = qn(key)
            if attr in el.attrib:
                del el.attrib[attr]


def format_docx(src: Path, out: Path):
    doc = Document(src)

    for style_name, size in STYLE_SIZES.items():
        if style_name in doc.styles:
            set_font_obj(doc.styles[style_name].font, size=size, bold=STYLE_BOLD.get(style_name))

    source_section = False
    for p in doc.paragraphs:
        text = p.text.strip()
        style_name = p.style.name if p.style is not None else ""

        if text in ("信源", "参考资料", "Works cited"):
            source_section = True

        is_heading = style_name.startswith("Heading")
        is_source_entry = source_section and text and text not in ("信源", "参考资料", "Works cited")
        is_table_caption = bool(re.match(r"^表\s*\d+[：:]", text))

        if text and not (is_heading or source_section or is_table_caption):
            p.paragraph_format.first_line_indent = BODY_INDENT
        elif text:
            p.paragraph_format.first_line_indent = None

        para_size = STYLE_SIZES.get(style_name, STYLE_SIZES.get(style_name.lower(), 11))
        para_bold = STYLE_BOLD.get(style_name)

        for run in p.runs:
            if not run.text:
                continue
            if re.fullmatch(r"\[\d+\]", run.text) and not is_source_entry:
                set_run_font(run, size_pt=CITE_SIZE_PT, bold=False, superscript=True)
            else:
                set_run_font(run, size_pt=para_size, bold=para_bold)
                if is_source_entry:
                    run.font.superscript = False

    for table in doc.tables:
        for row_index, row in enumerate(table.rows):
            for cell in row.cells:
                for p in cell.paragraphs:
                    p.paragraph_format.first_line_indent = None
                    for run in p.runs:
                        if not run.text:
                            continue
                        if re.fullmatch(r"\[\d+\]", run.text):
                            set_run_font(run, size_pt=CITE_SIZE_PT, bold=False, superscript=True)
                        else:
                            set_run_font(run, size_pt=11, bold=(row_index == 0))
                            run.font.superscript = False

    force_all_rfonts(doc.element)
    force_all_rfonts(doc.styles.element)
    doc.save(out)


def validate_docx(path: Path):
    ns = {"w": "http://schemas.openxmlformats.org/wordprocessingml/2006/main"}
    with ZipFile(path) as z:
        bad = {}
        for name in ("word/document.xml", "word/styles.xml"):
            root = ET.fromstring(z.read(name))
            vals = set()
            for el in root.findall(".//w:rFonts", ns):
                for key in ("eastAsia", "ascii", "hAnsi", "cs", "asciiTheme", "hAnsiTheme", "eastAsiaTheme", "csTheme", "cstheme"):
                    v = el.attrib.get("{http://schemas.openxmlformats.org/wordprocessingml/2006/main}" + key)
                    if v:
                        vals.add(v)
            if vals != {FONT}:
                bad[name] = vals
        return bad


def main(argv):
    if len(argv) != 3:
        print("Usage: python format_docx.py input.docx output.docx", file=sys.stderr)
        return 2
    src = Path(argv[1])
    out = Path(argv[2])
    if not src.exists():
        print(f"Input not found: {src}", file=sys.stderr)
        return 1
    format_docx(src, out)
    bad = validate_docx(out)
    if bad:
        print(f"Formatted, but font validation found unexpected rFonts: {bad}", file=sys.stderr)
        return 1
    print(f"Formatted: {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
