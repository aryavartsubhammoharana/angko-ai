import io
import re
from reportlab.lib.pagesizes import letter
from reportlab.lib import colors
from reportlab.platypus import (
    SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle, HRFlowable, KeepTogether
)
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.pdfgen import canvas


class NumberedCanvas(canvas.Canvas):
    """Two-pass canvas to dynamically compute and print accurate 'Page X of Y' headers/footers."""
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._saved_page_states = []

    def showPage(self):
        self._saved_page_states.append(dict(self.__dict__))
        self._startPage()

    def save(self):
        num_pages = len(self._saved_page_states)
        for state in self._saved_page_states:
            self.__dict__.update(state)
            self.draw_page_decorations(num_pages)
            super().showPage()
        super().save()

    def draw_page_decorations(self, page_count):
        self.saveState()
        self.setFont("Helvetica", 8)
        self.setFillColor(colors.HexColor("#64748b"))

        # Footer
        footer_text = f"Page {self._pageNumber} of {page_count}"
        self.drawRightString(612 - 36, 22, footer_text)
        self.drawString(36, 22, "Angkor AI Executive Intelligence Report")

        # Top Running Rule on page 2+
        if self._pageNumber > 1:
            self.setStrokeColor(colors.HexColor("#e2e8f0"))
            self.setLineWidth(0.5)
            self.line(36, 792 - 28, 612 - 36, 792 - 28)
            self.drawString(36, 792 - 23, "Angkor AI Report")

        self.restoreState()


def sanitize_reportlab_xml(text: str) -> str:
    """Validates and enforces strict XML tag nesting for ReportLab Paragraphs.
    Fixes crossed tags (e.g. <b>...<i>...</b></i> -> <b>...<i>...</i></b>)
    and closes any dangling unclosed tags."""
    if not text:
        return ""
    
    tag_pattern = re.compile(r'<\s*(/)?\s*([a-zA-Z]+)([^>]*)>')
    stack = []
    output = []
    last_end = 0

    for match in tag_pattern.finditer(text):
        output.append(text[last_end:match.start()])
        last_end = match.end()

        is_closing = bool(match.group(1))
        tag_name = match.group(2).lower()
        tag_attrs = match.group(3)

        # Only allow ReportLab supported formatting tags
        if tag_name not in ('b', 'i', 'u', 'font', 'super', 'sub'):
            continue

        if not is_closing:
            stack.append(tag_name)
            output.append(f'<{tag_name}{tag_attrs}>')
        else:
            if tag_name in stack:
                # Pop and close any nested tags above it to preserve strict LIFO order
                while stack and stack[-1] != tag_name:
                    unwound = stack.pop()
                    output.append(f'</{unwound}>')
                if stack and stack[-1] == tag_name:
                    stack.pop()
                    output.append(f'</{tag_name}>')
            # If tag_name not in stack, silently discard the orphan closing tag

    output.append(text[last_end:])

    # Close any unclosed tags at the end of the line
    while stack:
        unwound = stack.pop()
        output.append(f'</{unwound}>')

    return ''.join(output)


def create_safe_paragraph(html_text: str, style: ParagraphStyle) -> Paragraph:
    """Creates a ReportLab Paragraph with 100% crash protection.
    Balances improperly nested/overlapping tags (e.g. <b>...<i>...</b></i>).
    If any XML parsing error still occurs, falls back gracefully to sanitized plain text."""
    if not html_text:
        return Paragraph("", style)
    
    sanitized = sanitize_reportlab_xml(html_text)
    try:
        return Paragraph(sanitized, style)
    except Exception:
        # Fallback: strip all XML tags, escape plain text
        plain_text = re.sub(r'<[^>]+>', '', html_text)
        clean_plain = plain_text.replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')
        try:
            return Paragraph(clean_plain, style)
        except Exception:
            return Paragraph("", style)


def clean_markdown_inline(text: str) -> str:
    """Escape XML special characters and convert markdown bold/italic/code to ReportLab tags.
    Cleans up redundant leading spaces and dashes."""
    if not text:
        return ""
    
    text = text.strip()
    # Normalize multiple whitespaces inside line
    text = re.sub(r'[ \t]{2,}', ' ', text)

    # XML Escape
    text = text.replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')

    # Bold: **text** or __text__
    text = re.sub(r'\*\*(.+?)\*\*', r'<b>\1</b>', text)
    text = re.sub(r'__(.+?)__', r'<b>\1</b>', text)

    # Italic: *text* or _text_
    text = re.sub(r'(?<!\*)\*(?!\*)(.+?)(?<!\*)\*(?!\*)', r'<i>\1</i>', text)
    text = re.sub(r'(?<!_)_(?!_)(.+?)(?<!_)_(?!_)', r'<i>\1</i>', text)

    # Inline Code: `text`
    text = re.sub(r'`(.+?)`', r'<font name="Courier" color="#0f172a" size="8"><b>\1</b></font>', text)

    return sanitize_reportlab_xml(text)


def build_bullet_table(bullet_symbol: str, content_html: str, content_style: ParagraphStyle, symbol_style: ParagraphStyle, col_width_symbol=18, total_width=540) -> Table:
    """Renders a bullet or numbered point using an aligned 2-column table to guarantee ZERO unwanted space after the dot/symbol."""
    sym_para = create_safe_paragraph(bullet_symbol, symbol_style)
    content_para = create_safe_paragraph(content_html, content_style)
    
    t = Table([[sym_para, content_para]], colWidths=[col_width_symbol, total_width - col_width_symbol])
    t.setStyle(TableStyle([
        ('VALIGN', (0, 0), (-1, -1), 'TOP'),
        ('LEFTPADDING', (0, 0), (-1, -1), 0),
        ('RIGHTPADDING', (0, 0), (-1, -1), 0),
        ('TOPPADDING', (0, 0), (-1, -1), 1),
        ('BOTTOMPADDING', (0, 0), (-1, -1), 2),
    ]))
    return t


def generate_pdf_report(metadata: dict, markdown_text: str, engine_name: str = "Angkor Executive AI") -> bytes:
    """Generate a clean, high-density, professional executive PDF report from Markdown and metadata."""
    buffer = io.BytesIO()

    # Document Geometry (Letter size, 0.5 in / 36pt margins for maximum data density, 540pt printable width)
    doc = SimpleDocTemplate(
        buffer,
        pagesize=letter,
        leftMargin=36,
        rightMargin=36,
        topMargin=34,
        bottomMargin=34
    )

    styles = getSampleStyleSheet()

    # Custom Typography Hierarchy
    brand_style = ParagraphStyle(
        'ReportBrand',
        parent=styles['Normal'],
        fontName='Helvetica-Bold',
        fontSize=8.5,
        leading=10,
        textColor=colors.HexColor('#2563eb'),
        spaceAfter=2,
        textTransform='uppercase'
    )

    doc_title_style = ParagraphStyle(
        'DocTitle',
        parent=styles['Normal'],
        fontName='Helvetica-Bold',
        fontSize=14,
        leading=17,
        textColor=colors.HexColor('#0f172a'),
        spaceAfter=3
    )

    meta_style = ParagraphStyle(
        'MetaLine',
        parent=styles['Normal'],
        fontName='Helvetica',
        fontSize=8,
        leading=11,
        textColor=colors.HexColor('#475569'),
        spaceAfter=6
    )

    h1_style = ParagraphStyle(
        'Heading1_Custom',
        parent=styles['Normal'],
        fontName='Helvetica-Bold',
        fontSize=11.5,
        leading=14,
        textColor=colors.HexColor('#0f172a'),
        spaceBefore=8,
        spaceAfter=3,
        keepWithNext=True
    )

    h2_style = ParagraphStyle(
        'Heading2_Custom',
        parent=styles['Normal'],
        fontName='Helvetica-Bold',
        fontSize=10,
        leading=13,
        textColor=colors.HexColor('#1e293b'),
        spaceBefore=7,
        spaceAfter=3,
        keepWithNext=True
    )

    h3_style = ParagraphStyle(
        'Heading3_Custom',
        parent=styles['Normal'],
        fontName='Helvetica-Bold',
        fontSize=9,
        leading=12,
        textColor=colors.HexColor('#334155'),
        spaceBefore=5,
        spaceAfter=2,
        keepWithNext=True
    )

    body_style = ParagraphStyle(
        'Body_Custom',
        parent=styles['Normal'],
        fontName='Helvetica',
        fontSize=8.5,
        leading=11.5,
        textColor=colors.HexColor('#1e293b'),
        spaceAfter=2.5
    )

    list_text_style = ParagraphStyle(
        'ListText_Custom',
        parent=body_style,
        spaceAfter=0,
        leftIndent=0,
        firstLineIndent=0
    )

    symbol_style = ParagraphStyle(
        'Symbol_Custom',
        parent=styles['Normal'],
        fontName='Helvetica-Bold',
        fontSize=8.5,
        leading=11.5,
        textColor=colors.HexColor('#0f172a'),
        alignment=0
    )

    blockquote_style = ParagraphStyle(
        'Blockquote_Custom',
        parent=body_style,
        fontName='Helvetica-Oblique',
        textColor=colors.HexColor('#334155'),
        leftIndent=12,
        spaceBefore=3,
        spaceAfter=3
    )

    table_cell_style = ParagraphStyle(
        'TableCell',
        parent=styles['Normal'],
        fontName='Helvetica',
        fontSize=7.5,
        leading=9.5,
        textColor=colors.HexColor('#1e293b')
    )

    table_header_style = ParagraphStyle(
        'TableHeader',
        parent=table_cell_style,
        fontName='Helvetica-Bold',
        fontSize=7.5,
        leading=9.5,
        textColor=colors.HexColor('#0f172a')
    )

    story = []

    # 1. Header Banner Block (Kept together to avoid breaking across pages)
    title_text = metadata.get('title') or 'Video Intelligence Report'
    author_text = metadata.get('author') or 'Unknown'
    duration_val = metadata.get('duration') or 0
    try:
        duration_sec = int(float(duration_val))
        m, s = divmod(duration_sec, 60)
        h, m = divmod(m, 60)
        dur_str = f"{h}h {m}m {s}s" if h > 0 else f"{m}m {s}s"
    except Exception:
        dur_str = str(duration_val)

    header_elements = [
        create_safe_paragraph("ANGKOR AI &bull; EXECUTIVE INTELLIGENCE REPORT", brand_style),
        create_safe_paragraph(clean_markdown_inline(title_text), doc_title_style),
        create_safe_paragraph(
            f"<b>Speaker/Channel:</b> {clean_markdown_inline(author_text)} &nbsp;&bull;&nbsp; "
            f"<b>Duration:</b> {dur_str} &nbsp;&bull;&nbsp; "
            f"<b>Engine:</b> {engine_name}",
            meta_style
        ),
        HRFlowable(width="100%", thickness=1.2, color=colors.HexColor("#0f172a"), spaceBefore=1, spaceAfter=6)
    ]
    story.append(KeepTogether(header_elements))

    # Helper to flush a parsed markdown table
    def flush_table(rows):
        if not rows:
            return None
        # Clean cell text
        num_cols = max(len(r) for r in rows)
        if num_cols == 0:
            return None

        formatted_data = []
        for row_idx, r in enumerate(rows):
            # Pad row if incomplete
            padded_row = r + [''] * (num_cols - len(r))
            row_cells = []
            style_to_use = table_header_style if row_idx == 0 else table_cell_style
            for c in padded_row:
                row_cells.append(create_safe_paragraph(clean_markdown_inline(c), style_to_use))
            formatted_data.append(row_cells)

        # Total printable width = 540 pt
        # Dynamically allocate column widths based on content length
        col_lens = [0] * num_cols
        for r in rows:
            for idx, c in enumerate(r):
                if idx < num_cols:
                    col_lens[idx] = max(col_lens[idx], len(c.strip()))
        total_len = sum(col_lens) or 1
        
        # Calculate proportional width with min bounds
        computed_widths = []
        for l in col_lens:
            proportion = max(l, 8) / max(total_len, 8 * num_cols)
            computed_widths.append(round(proportion * 540.0, 1))
        
        # Normalize sum to exactly 540 pt
        diff = 540.0 - sum(computed_widths)
        computed_widths[-1] += diff

        t = Table(formatted_data, colWidths=computed_widths, repeatRows=1)
        t.setStyle(TableStyle([
            ('BACKGROUND', (0, 0), (-1, 0), colors.HexColor('#f1f5f9')),
            ('TEXTCOLOR', (0, 0), (-1, 0), colors.HexColor('#0f172a')),
            ('ALIGN', (0, 0), (-1, -1), 'LEFT'),
            ('VALIGN', (0, 0), (-1, -1), 'TOP'),
            # Complete grid with strong visible lines & boxes
            ('INNERGRID', (0, 0), (-1, -1), 0.5, colors.HexColor('#94a3b8')),
            ('BOX', (0, 0), (-1, -1), 1.0, colors.HexColor('#475569')),
            ('TOPPADDING', (0, 0), (-1, -1), 4),
            ('BOTTOMPADDING', (0, 0), (-1, -1), 4),
            ('LEFTPADDING', (0, 0), (-1, -1), 5),
            ('RIGHTPADDING', (0, 0), (-1, -1), 5),
        ]))
        # Keep whole table together to avoid messy pagination
        return KeepTogether(t)

    # 2. Parse Markdown Lines into Clean ReportLab Flowables
    lines = markdown_text.split('\n')
    i = 0
    num_lines = len(lines)

    in_table = False
    table_rows = []

    while i < num_lines:
        raw_line = lines[i]
        line = raw_line.strip()

        # Handle Markdown Tables: | col1 | col2 |
        if line.startswith('|') and line.endswith('|'):
            # Check if this is a separator row like |---|---|
            if re.match(r'^\|[\s\-:|]+\|$', line):
                i += 1
                continue
            cells = [c.strip() for c in line.strip('|').split('|')]
            table_rows.append(cells)
            in_table = True
            i += 1
            continue
        elif in_table:
            # End of table, render it
            tbl_flowable = flush_table(table_rows)
            if tbl_flowable:
                story.append(tbl_flowable)
                story.append(Spacer(1, 4))
            table_rows = []
            in_table = False

        if not line:
            i += 1
            continue

        # Horizontal Rule
        if re.match(r'^(\*{3,}|-{3,}|_{3,})$', line):
            story.append(HRFlowable(width="100%", thickness=0.5, color=colors.HexColor("#e2e8f0"), spaceBefore=3, spaceAfter=4))
            i += 1
            continue

        # Headings
        if line.startswith('# '):
            story.append(create_safe_paragraph(clean_markdown_inline(line[2:]), h1_style))
        elif line.startswith('## '):
            story.append(create_safe_paragraph(clean_markdown_inline(line[3:]), h2_style))
        elif line.startswith('### '):
            story.append(create_safe_paragraph(clean_markdown_inline(line[4:]), h3_style))
        elif line.startswith('#### '):
            story.append(create_safe_paragraph(clean_markdown_inline(line[5:]), h3_style))
        
        # Bullet list: - or *
        elif line.startswith('- ') or line.startswith('* '):
            cleaned_bullet = clean_markdown_inline(line[2:])
            # Use 2-column table with tight symbol column (14pt) to guarantee ZERO space after bullet
            story.append(build_bullet_table("&bull;", cleaned_bullet, list_text_style, symbol_style, col_width_symbol=12))
        
        # Numbered list: 1. or 2.
        elif re.match(r'^\d+\.\s*', line):
            num_match = re.match(r'^(\d+\.)\s*(.*)$', line)
            if num_match:
                prefix = num_match.group(1)
                content = clean_markdown_inline(num_match.group(2))
                # Fixed symbol column (16pt) so numbers don't push text far right
                story.append(build_bullet_table(prefix, content, list_text_style, symbol_style, col_width_symbol=16))
            else:
                story.append(create_safe_paragraph(clean_markdown_inline(line), body_style))
        
        # Blockquote: > text
        elif line.startswith('>'):
            cleaned_quote = clean_markdown_inline(line.lstrip('>').strip())
            story.append(create_safe_paragraph(cleaned_quote, blockquote_style))
        
        # Standard Paragraph
        else:
            story.append(create_safe_paragraph(clean_markdown_inline(line), body_style))

        i += 1

    # If document ended while parsing table
    if table_rows:
        tbl_flowable = flush_table(table_rows)
        if tbl_flowable:
            story.append(tbl_flowable)

    # Build PDF with dynamic two-pass canvas
    doc.build(story, canvasmaker=NumberedCanvas)
    buffer.seek(0)
    return buffer.getvalue()
