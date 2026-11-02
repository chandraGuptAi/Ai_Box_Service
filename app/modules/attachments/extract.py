"""Local extraction only. Uploaded content never triggers network or executes code."""
import io
import shutil
import subprocess
import tempfile
import warnings
import zipfile
from datetime import date, datetime
from pathlib import Path

from fastapi import HTTPException

MAX_UNPACKED = 80 * 1024 * 1024
MAX_UNITS = 100
MAX_CELLS = 100000
MAX_TEXT = 500000
MAX_PIXELS = 25000000
IMAGE_FORMATS = {'PNG', 'JPEG', 'WEBP', 'TIFF', 'GIF', 'BMP'}


def limit(condition, message):
    if condition:
        raise HTTPException(413, message)


def archive_type(blob):
    with zipfile.ZipFile(io.BytesIO(blob)) as z:
        info = z.infolist()
        limit(len(info) > 5000 or sum(i.file_size for i in info) > MAX_UNPACKED,
              'Office archive exceeds extraction limits')
        names = set(z.namelist())
        if len(names) != len(info) or any(name.startswith(('/', '\\')) or '..' in name.replace('\\', '/').split('/') for name in names):
            raise HTTPException(415, 'Invalid Office archive paths or duplicate parts')
        if '[Content_Types].xml' in names:
            from defusedxml import ElementTree
            types = ElementTree.fromstring(z.read('[Content_Types].xml'))
            if any(any(term in part.get('ContentType', '').lower() for term in
                       ('macroenabled', 'vbaproject', 'activex', 'oleobject')) for part in types):
                raise HTTPException(415, 'Macro-enabled or executable Office content is not supported')
        if any('vbaproject' in part.filename.lower() or '/activex/' in part.filename.lower() or '/embeddings/' in part.filename.lower()
               or (part.filename.lower().endswith('.bin') and '/printersettings/' not in part.filename.lower())
               for part in info if not part.is_dir()):
            raise HTTPException(415, 'Macro-enabled or embedded binary Office files are not supported')
        if 'word/document.xml' in names:
            return 'docx'
        if 'xl/workbook.xml' in names:
            return 'xlsx'
        if 'ppt/presentation.xml' in names:
            return 'pptx'
    raise HTTPException(415, 'Unsupported Office archive')


def inspect(blob, ocr_language='eng'):
    try:
        if blob.startswith(b'%PDF-'):
            result = pdf(blob, ocr_language)
        elif blob.startswith(b'PK\x03\x04'):
            kind = archive_type(blob)
            result = {'docx': word, 'xlsx': excel, 'pptx': slides}[kind](blob)
        elif blob.startswith(b'\xd0\xcf\x11\xe0'):
            raise HTTPException(415, 'Legacy DOC/XLS/PPT requires conversion to DOCX/XLSX/PPTX first')
        else:
            result = image(blob, ocr_language)
        limit(len(str(result)) > 8 * 1024 * 1024, 'Extracted attachment exceeds context limits')
        text_size = sum(len(u.get('text', '')) for u in result['units'])
        limit(text_size > MAX_TEXT, 'Attachment text exceeds extraction limits')
        result['complete'] = True
        return result
    except HTTPException:
        raise
    except Exception:
        # Never echo parser exceptions containing document content, paths or XML.
        raise HTTPException(422, 'The file is damaged, encrypted or cannot be read safely') from None


def ocr_png(blob, language):
    executable = shutil.which('tesseract')
    if not executable:
        return '', 'OCR unavailable: install Tesseract and required language packs; image content has not been interpreted.'
    with tempfile.TemporaryDirectory(prefix='aibox-ocr-') as folder:
        source = Path(folder) / 'page.png'
        source.write_bytes(blob)
        try:
            process = subprocess.run([executable, str(source), 'stdout', '-l', language],
                capture_output=True, timeout=30, creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
            if process.returncode:
                return '', 'OCR failed; no text was inferred.'
            return process.stdout.decode('utf-8', errors='replace').strip(), None
        except subprocess.TimeoutExpired:
            return '', 'OCR timed out; no text was inferred.'


def image(blob, language):
    from PIL import Image, ImageOps
    with warnings.catch_warnings():
        warnings.simplefilter('error', Image.DecompressionBombWarning)
        with Image.open(io.BytesIO(blob)) as im:
            if im.format not in IMAGE_FORMATS:
                raise HTTPException(415, 'Supported image formats: PNG, JPEG, WebP, TIFF, GIF, BMP')
            limit(im.width * im.height > MAX_PIXELS or getattr(im, 'n_frames', 1) > MAX_UNITS,
                  'Image size or frame count exceeds extraction limits')
            units, notes = [], []
            for i in range(getattr(im, 'n_frames', 1)):
                im.seek(i)
                limit(im.width * im.height > MAX_PIXELS, 'Image dimensions exceed extraction limits')
                frame = ImageOps.exif_transpose(im.copy()).convert('RGB')
                output = io.BytesIO()
                frame.save(output, format='PNG')
                text, warning = ocr_png(output.getvalue(), language)
                units.append({'index': i, 'text': text, 'width': frame.width, 'height': frame.height,
                              'ocr_used': not warning, 'visual_interpretation': 'not_performed'})
                if warning:
                    notes.append(warning)
            return {'format': 'image', 'image_format': im.format, 'units': units,
                    'warnings': list(dict.fromkeys(notes)), 'requires_vision': True}


def pdf(blob, language):
    import pymupdf
    units, notes, tables = [], [], []
    with pymupdf.open(stream=blob, filetype='pdf') as doc:
        if doc.needs_pass:
            raise HTTPException(422, 'Password-protected PDFs are not supported')
        limit(len(doc) > MAX_UNITS, 'PDF exceeds page limit')
        for i, page in enumerate(doc):
            spans = [span for block in page.get_text('dict')['blocks'] for line in block.get('lines', [])
                     for span in line['spans']]
            text = page.get_text(sort=True)
            warning = None
            ocr_used = False
            if not text.strip():
                scale = min(1.5, (MAX_PIXELS / max(page.rect.width * page.rect.height, 1)) ** .5)
                text, warning = ocr_png(page.get_pixmap(matrix=pymupdf.Matrix(scale, scale)).tobytes('png'), language)
                ocr_used = not warning
            if warning:
                notes.append(f'Page {i + 1}: {warning}')
            units.append({'index': i, 'text': text, 'width': page.rect.width, 'height': page.rect.height,
                'ocr_used': ocr_used, 'spans': [{'text': s['text'], 'font': s['font'], 'size': s['size'],
                    'color': f"#{s['color']:06x}", 'bbox': list(s['bbox'])} for s in spans]})
            try:
                for table in page.find_tables().tables:
                    rows = table.extract()
                    if rows:
                        tables.append({'kind': 'table', 'page': i, 'rows': rows, 'row_count': len(rows),
                                       'column_count': max(len(row) for row in rows)})
            except Exception:
                notes.append(f'Page {i + 1}: table structure could not be extracted; text is available.')
    needs_vision = any(u['ocr_used'] or not u['text'].strip() for u in units)
    design = {'page_sizes': [[u['width'], u['height']] for u in units],
              'fonts': sorted({s['font'] for u in units for s in u['spans']}),
              'colors': sorted({s['color'] for u in units for s in u['spans']})}
    for table in tables:
        units.append({**table, 'index': len(units)})
    return {'format': 'pdf', 'units': units, 'warnings': notes, 'design': design,
            'requires_vision': needs_vision, 'design_fidelity': 'layout_metadata_only'}


def word(blob):
    from docx import Document
    doc = Document(io.BytesIO(blob))
    from docx.text.paragraph import Paragraph
    from docx.oxml.ns import qn
    paragraphs = [Paragraph(element, doc) for element in doc.element.body.iter(qn('w:p'))]
    for section in doc.sections:
        paragraphs.extend(section.header.paragraphs)
        paragraphs.extend(section.footer.paragraphs)
    units = [{'index': i, 'text': p.text, 'style': p.style.name if p.style else None,
              'runs': [{'text': r.text, 'font': r.font.name, 'size': r.font.size.pt if r.font.size else None,
                        'color': str(r.font.color.rgb) if r.font.color.rgb else None,
                        'bold': r.bold, 'italic': r.italic} for r in p.runs]}
             for i, p in enumerate(paragraphs)]
    for table in doc.tables:
        rows = [[cell.text for cell in row.cells] for row in table.rows]
        units.append({'index': len(units), 'kind': 'table', 'rows': rows,
                      'row_count': len(rows), 'column_count': max((len(row) for row in rows), default=0)})
    sections = [{'page_width': s.page_width, 'page_height': s.page_height,
                 'top_margin': s.top_margin, 'bottom_margin': s.bottom_margin,
                 'left_margin': s.left_margin, 'right_margin': s.right_margin} for s in doc.sections]
    return {'format': 'docx', 'units': units, 'design': {'sections_emu': sections},
            'embedded_image_count': len(embedded_images(blob)),
            'warnings': ['Embedded images and complex objects need visual review.'],
            'requires_vision': bool(doc.inline_shapes)}


def scalar(value):
    return value.isoformat() if isinstance(value, (date, datetime)) else value


def color(value):
    if value is None:
        return None
    return {'type': value.type, 'value': str(value.value)}


def excel(blob):
    from openpyxl import load_workbook
    book = load_workbook(io.BytesIO(blob), data_only=False, keep_links=False)
    cached = load_workbook(io.BytesIO(blob), data_only=True, keep_links=False)
    units, total, formulas = [], 0, False
    try:
        limit(len(book.worksheets) > MAX_UNITS, 'Workbook exceeds sheet limit')
        for i, sheet in enumerate(book.worksheets):
            total += sheet.max_row * sheet.max_column
            limit(total > MAX_CELLS or sheet.max_column > 200, 'Workbook exceeds cell/column limits')
            rows, cells = [], []
            for row in sheet:
                rows.append([scalar(cached[sheet.title][c.coordinate].value) for c in row])
                for c in row:
                    if c.value is None and not c.has_style:
                        continue
                    formula = c.value if c.data_type == 'f' else None
                    formulas = formulas or bool(formula)
                    cells.append({'cell': c.coordinate, 'value': scalar(cached[sheet.title][c.coordinate].value),
                        'formula': formula, 'number_format': c.number_format,
                        'font': {'name': c.font.name, 'size': c.font.sz, 'bold': c.font.bold, 'color': color(c.font.color)},
                        'fill': {'type': c.fill.patternType, 'color': color(c.fill.fgColor)},
                        'alignment': {'horizontal': c.alignment.horizontal, 'vertical': c.alignment.vertical}})
            units.append({'index': i, 'sheet': sheet.title, 'rows': rows, 'cells': cells,
                'row_count': sheet.max_row, 'column_count': sheet.max_column,
                'merged_ranges': [str(r) for r in sheet.merged_cells.ranges],
                'column_widths': {k: v.width for k, v in sheet.column_dimensions.items()},
                'freeze_panes': str(sheet.freeze_panes) if sheet.freeze_panes else None})
    finally:
        book.close()
        cached.close()
    return {'format': 'xlsx', 'units': units, 'requires_vision': False,
            'warnings': ['Formulas are not evaluated; cached values can be missing or stale.'] if formulas else []}


def walk_shapes(shapes):
    for shape in shapes:
        yield shape
        if shape.shape_type == 6:  # GROUP
            yield from walk_shapes(shape.shapes)


def slides(blob):
    from pptx import Presentation
    deck = Presentation(io.BytesIO(blob))
    limit(len(deck.slides) > MAX_UNITS, 'Presentation exceeds slide limit')
    units, images = [], False
    for i, slide in enumerate(deck.slides):
        shapes, texts = [], []
        for shape in walk_shapes(slide.shapes):
            text = shape.text if shape.has_text_frame else ''
            if shape.has_table:
                text = '\n'.join('\t'.join(c.text for c in r.cells) for r in shape.table.rows)
            runs = []
            if shape.has_text_frame:
                for p in shape.text_frame.paragraphs:
                    for run in p.runs:
                        font = run.font
                        runs.append({'text': run.text, 'font': font.name, 'size': font.size.pt if font.size else None,
                                     'bold': font.bold, 'color': str(font.color.rgb) if font.color.type == 1 else None})
            images = images or shape.shape_type == 13
            shapes.append({'id': shape.shape_id, 'name': shape.name, 'text': text, 'runs': runs,
                           'left': shape.left, 'top': shape.top, 'width': shape.width, 'height': shape.height})
            if text:
                texts.append(text)
        units.append({'index': i, 'text': '\n'.join(texts), 'shapes': shapes})
        if slide.has_notes_slide:
            note = slide.notes_slide.notes_text_frame
            if note:
                units[-1]['speaker_notes'] = note.text
                units[-1]['text'] += '\n' + note.text
    return {'format': 'pptx', 'units': units, 'embedded_image_count': len(embedded_images(blob)),
            'design': {'slide_width_emu': deck.slide_width,
            'slide_height_emu': deck.slide_height}, 'requires_vision': images,
            'warnings': ['Slide previews require LibreOffice; charts, images and inherited theme styling need visual review.']}


def preview(blob, kind, index):
    if kind == 'pdf':
        import pymupdf
        with pymupdf.open(stream=blob, filetype='pdf') as doc:
            if index >= len(doc):
                raise HTTPException(404, 'Page not found')
            page = doc[index]
            scale = min(1.5, (MAX_PIXELS / max(page.rect.width * page.rect.height, 1)) ** .5)
            return page.get_pixmap(matrix=pymupdf.Matrix(scale, scale)).tobytes('png')
    if kind == 'image':
        from PIL import Image, ImageOps
        with Image.open(io.BytesIO(blob)) as im:
            if index >= getattr(im, 'n_frames', 1):
                raise HTTPException(404, 'Frame not found')
            im.seek(index)
            frame = ImageOps.exif_transpose(im.copy()).convert('RGB')
            frame.thumbnail((2000, 2000))
            output = io.BytesIO()
            frame.save(output, format='PNG')
            return output.getvalue()
    raise HTTPException(501, 'Office slide/page previews are not configured; original file and layout metadata are available')


def embedded_images(blob):
    with zipfile.ZipFile(io.BytesIO(blob)) as archive:
        return sorted(name for name in archive.namelist()
                      if name.startswith(('word/media/', 'ppt/media/', 'xl/media/'))
                      and name.lower().endswith(('.png', '.jpg', '.jpeg', '.webp', '.tiff', '.tif', '.gif', '.bmp')))


def embedded_preview(blob, index):
    names = embedded_images(blob)
    if index >= len(names):
        raise HTTPException(404, 'Embedded image not found')
    with zipfile.ZipFile(io.BytesIO(blob)) as archive:
        data = archive.read(names[index])
    # Reuse signature/pixel checks before normalizing untrusted embedded images.
    from PIL import Image
    with Image.open(io.BytesIO(data)) as image:
        limit(image.width * image.height > MAX_PIXELS, 'Embedded image exceeds size limit')
    return preview(data, 'image', 0)
