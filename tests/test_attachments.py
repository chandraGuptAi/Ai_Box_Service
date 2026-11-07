import io
import zipfile

import pytest
from fastapi.testclient import TestClient

from app.core.config import get_settings
from app.main import create_app

USER = {'X-User-Id': 'seller-one'}


@pytest.fixture
def client(tmp_path, monkeypatch):
    settings = get_settings().model_copy(update={'ATTACHMENT_STORAGE_DIR': tmp_path, 'SERVICE_TOKEN': ''})
    import app.modules.attachments.module as module
    monkeypatch.setattr(module, 'get_settings', lambda: settings)
    with TestClient(create_app()) as client:
        yield client


def docx():
    from docx import Document
    from docx.shared import RGBColor
    doc = Document()
    p = doc.add_paragraph()
    run = p.add_run('{{to')
    run.bold = True
    run.font.color.rgb = RGBColor.from_string('2366AA')
    p.add_run('tal}} orders')
    doc.sections[0].header.paragraphs[0].text = 'Brand header'
    output = io.BytesIO()
    doc.save(output)
    return output.getvalue()


def xlsx():
    from openpyxl import Workbook
    from openpyxl.styles import PatternFill
    book = Workbook()
    sheet = book.active
    sheet.title = 'Orders'
    sheet.append(['Status', 'Amount'])
    sheet.append(['Shipped', 1200])
    sheet.append(['Processing', 2400])
    sheet['B4'] = '=SUM(B2:B3)'
    sheet['B2'].fill = PatternFill('solid', fgColor='3366AA')
    sheet['B2'].number_format = '\u20b9#,##0.00'
    sheet.column_dimensions['B'].width = 24
    output = io.BytesIO()
    book.save(output)
    return output.getvalue()


def upload(client, blob=None, name='report.docx'):
    response = client.post('/api/attachments', headers=USER,
                           files={'file': (name, docx() if blob is None else blob, 'application/octet-stream')})
    assert response.status_code == 201, response.text
    return response.json()['attachment_id']


def test_upload_inspect_owner_isolation_and_delete(client):
    identifier = upload(client, name='../../fake.pdf')
    path = '/api/attachments/' + identifier
    result = client.get(path, headers=USER)
    assert result.json()['format'] == 'docx'  # Actual content, not extension.
    assert result.json()['filename'] == 'fake.pdf'
    assert result.headers['cache-control'] == 'no-store'
    assert 'Brand header' in str(result.json())
    assert client.get(path, headers={'X-User-Id': 'seller-two'}).status_code == 404
    assert client.post('/api/attachments', files={'file': ('x.docx', docx())}).status_code == 422
    assert client.delete(path, headers=USER).status_code == 204
    assert client.get(path, headers=USER).status_code == 404


def test_prompt_is_forwarded_before_source_choice_and_no_upstream_calls(client, monkeypatch):
    identifier = upload(client, xlsx(), 'orders.xlsx')
    body = {'prompt': 'Excel ka data use karke report banao, selected Shopify channel ignore karo',
            'attachment_ids': [identifier]}
    # Any attempt to call an upstream Bigship service in attachment routing fails this test.
    import httpx
    async def forbidden(*args, **kwargs):
        raise AssertionError('Attachment-only processing must not fetch external data')
    monkeypatch.setattr(httpx.AsyncClient, 'send', forbidden)
    context = client.post('/api/attachments/planning-context', headers=USER, json=body)
    assert context.status_code == 200
    assert context.json()['prompt'] == body['prompt']
    assert context.json()['needs_ai_decision']
    assert context.json()['privacy_status'] == 'not_sanitized'
    assert context.json()['attachments'][0]['units'][0]['sample_rows'][1] == ['Shipped', 1200]
    body['decision'] = {'source': 'attachment_only', 'reason': 'User explicitly requests Excel data',
                        'attachments': [{'attachment_id': identifier, 'role': 'data'}]}
    result = client.post('/api/attachments/validate-plan', headers=USER, json=body)
    assert result.status_code == 200, result.text
    assert result.json()['can_generate']
    assert not result.json()['fetch_external_data']
    assert not result.json()['fetch_preview_count']
    assert not result.json()['apply_selected_filters']
    assert result.json()['use_attachment_data']


@pytest.mark.parametrize('source,role,external', [
    ('api_only', 'design_reference', True), ('combined', 'data_and_design', True),
    ('clarification_required', 'unused', False)])
def test_validates_other_source_decisions(client, source, role, external):
    identifier = upload(client)
    result = client.post('/api/attachments/validate-plan', headers=USER, json={
        'prompt': 'Use this report', 'attachment_ids': [identifier], 'decision': {
            'source': source, 'attachments': [{'attachment_id': identifier, 'role': role}],
            'reason': 'Test source decision', 'question': 'Data or design reference?'}})
    assert result.status_code == 200, result.text
    assert result.json()['fetch_external_data'] is external
    assert result.json()['can_generate'] is (source != 'clarification_required')


def test_rejects_inconsistent_and_inaccessible_plans(client):
    identifier = upload(client)
    body = {'prompt': 'Use attachment', 'attachment_ids': [identifier], 'decision': {
        'source': 'attachment_only', 'reason': 'Only attachment',
        'attachments': [{'attachment_id': identifier, 'role': 'design_reference'}]}}
    assert client.post('/api/attachments/validate-plan', headers=USER, json=body).status_code == 422
    body['decision']['attachments'][0] = {'attachment_id': '0' * 32, 'role': 'data'}
    assert client.post('/api/attachments/validate-plan', headers=USER, json=body).status_code == 422


def test_word_template_preserves_run_style_header_and_original(client):
    from docx import Document
    original = docx()
    identifier = upload(client, original)
    result = client.post(f'/api/attachments/{identifier}/render', headers=USER,
                         json={'replacements': {'{{total}}': '3'}})
    assert result.status_code == 200, result.text
    edited = Document(io.BytesIO(result.content))
    assert edited.paragraphs[0].text == '3 orders'
    assert edited.paragraphs[0].runs[0].bold
    assert str(edited.paragraphs[0].runs[0].font.color.rgb) == '2366AA'
    assert edited.sections[0].header.paragraphs[0].text == 'Brand header'
    assert '{{total}} orders' == Document(io.BytesIO(original)).paragraphs[0].text
    before = zipfile.ZipFile(io.BytesIO(original))
    after = zipfile.ZipFile(io.BytesIO(result.content))
    assert before.read('word/styles.xml') == after.read('word/styles.xml')


def test_excel_template_keeps_formatting_and_formulas_but_never_injects_formulas(client):
    from openpyxl import load_workbook
    identifier = upload(client, xlsx(), 'orders.xlsx')
    result = client.post(f'/api/attachments/{identifier}/render', headers=USER, json={'cells': [
        {'sheet': 'Orders', 'cell': 'B2', 'value': 3600},
        {'sheet': 'Orders', 'cell': 'A2', 'value': '=HYPERLINK("https://example.invalid")'}]})
    assert result.status_code == 200, result.text
    book = load_workbook(io.BytesIO(result.content))
    sheet = book['Orders']
    assert sheet['B2'].value == 3600
    assert sheet['B2'].number_format == '\u20b9#,##0.00'
    assert sheet['B2'].fill.fgColor.rgb == '003366AA'
    assert sheet.column_dimensions['B'].width == 24
    assert sheet['B4'].value == '=SUM(B2:B3)'
    assert sheet['A2'].data_type == 's'
    assert book.calculation.fullCalcOnLoad


def test_pptx_text_extraction_and_template_edit_keep_design(client):
    from pptx import Presentation
    from pptx.util import Inches
    deck = Presentation()
    slide = deck.slides.add_slide(deck.slide_layouts[6])
    shape = slide.shapes.add_textbox(Inches(1), Inches(1), Inches(5), Inches(1))
    run = shape.text_frame.paragraphs[0].add_run()
    run.text = '{{title}}'
    run.font.bold = True
    output = io.BytesIO()
    deck.save(output)
    identifier = upload(client, output.getvalue(), 'report.pptx')
    info = client.get('/api/attachments/' + identifier, headers=USER).json()
    assert info['inspection']['units'][0]['text'] == '{{title}}'
    result = client.post(f'/api/attachments/{identifier}/render', headers=USER,
                         json={'replacements': {'{{title}}': 'Order report'}})
    assert result.status_code == 200, result.text
    changed = Presentation(io.BytesIO(result.content))
    assert changed.slides[0].shapes[0].text == 'Order report'
    assert changed.slides[0].shapes[0].left == Inches(1)
    assert changed.slides[0].shapes[0].text_frame.paragraphs[0].runs[0].font.bold


def test_pdf_extraction_and_preview(client):
    import pymupdf
    with pymupdf.open() as doc:
        page = doc.new_page()
        page.insert_text((72, 72), 'Total orders 3', color=(0, 0, 1))
        blob = doc.tobytes()
    identifier = upload(client, blob, 'report.pdf')
    result = client.get('/api/attachments/' + identifier, headers=USER).json()
    unit = result['inspection']['units'][0]
    assert 'Total orders 3' in unit['text']
    assert unit['spans'][0]['color'] == '#0000ff'
    preview = client.get(f'/api/attachments/{identifier}/preview/0', headers=USER)
    assert preview.status_code == 200
    assert preview.content.startswith(b'\x89PNG')
    assert client.get(f'/api/attachments/{identifier}/preview/100', headers=USER).status_code == 404


def test_image_without_ocr_is_not_silently_used_as_data(client, monkeypatch):
    from PIL import Image
    import app.modules.attachments.extract as extract
    monkeypatch.setattr(extract.shutil, 'which', lambda _: None)
    output = io.BytesIO()
    Image.new('RGB', (100, 100), 'red').save(output, 'PNG')
    identifier = upload(client, output.getvalue(), 'picture.png')
    info = client.get('/api/attachments/' + identifier, headers=USER).json()
    assert info['requires_vision']
    assert info['warnings']
    result = client.post('/api/attachments/validate-plan', headers=USER, json={
        'prompt': 'Image ka data use karo', 'attachment_ids': [identifier], 'decision': {
            'source': 'attachment_only', 'reason': 'Read image data',
            'attachments': [{'attachment_id': identifier, 'role': 'data'}]}})
    assert result.status_code == 200
    assert not result.json()['can_generate']
    assert result.json()['unreadable_data_attachments'] == [identifier]


def test_rejects_bad_archives_empty_uploads_and_legacy_files(client):
    for blob in [b'', b'not a document', b'\xd0\xcf\x11\xe0old']:
        response = client.post('/api/attachments', headers=USER, files={'file': ('file.doc', blob)})
        assert response.status_code in {415, 422}
    output = io.BytesIO()
    with zipfile.ZipFile(output, 'w') as archive:
        archive.writestr('word/document.xml', '<xml/>')
        archive.writestr('word/vbaProject.bin', 'macro')
    response = client.post('/api/attachments', headers=USER, files={'file': ('macro.docx', output.getvalue())})
    assert response.status_code == 415


@pytest.mark.parametrize('active_macro_type', [False, True])
def test_office_content_types_only_reject_actual_macro_parts(client, active_macro_type):
    from xml.etree import ElementTree as ET
    ns = '{http://schemas.openxmlformats.org/package/2006/content-types}'
    output = io.BytesIO()
    with zipfile.ZipFile(io.BytesIO(xlsx())) as source, zipfile.ZipFile(output, 'w') as target:
        for entry in source.infolist():
            content = source.read(entry.filename)
            if entry.filename == '[Content_Types].xml':
                types = ET.fromstring(content)
                ET.SubElement(types, ns + 'Default', Extension='bin',
                              ContentType='application/vnd.ms-excel.sheet.binary.macroEnabled.main')
                if active_macro_type:
                    for part in types:
                        if part.get('PartName') == '/xl/workbook.xml':
                            part.set('ContentType', 'application/vnd.ms-excel.sheet.macroEnabled.main+xml')
                content = ET.tostring(types)
            target.writestr(entry, content)
    response = client.post('/api/attachments', headers=USER,
                           files={'file': ('export.xlsx', output.getvalue())})
    assert response.status_code == (415 if active_macro_type else 201), response.text


def test_empty_office_embedding_directories_are_not_embedded_files(client):
    from pptx import Presentation
    deck = Presentation()
    deck.slides.add_slide(deck.slide_layouts[6])
    original = io.BytesIO()
    deck.save(original)
    output = io.BytesIO()
    with zipfile.ZipFile(original) as source, zipfile.ZipFile(output, 'w') as target:
        for entry in source.infolist():
            target.writestr(entry, source.read(entry.filename))
        target.writestr('ppt/embeddings/', b'')
        target.writestr('ppt/activeX/', b'')
    identifier = upload(client, output.getvalue(), 'sample.pptx')
    assert client.get('/api/attachments/' + identifier, headers=USER).json()['unit_count'] == 1
    with zipfile.ZipFile(output, 'a') as target:
        target.writestr('ppt/embeddings/object.bin', b'embedded-object')
    response = client.post('/api/attachments', headers=USER, files={'file': ('embedded.pptx', output.getvalue())})
    assert response.status_code == 415


def test_expiration_size_limits_and_missing_placeholders(client, monkeypatch):
    backend = client.app.state.attachments_service
    monkeypatch.setattr(backend.settings, 'ATTACHMENT_MAX_BYTES', 8)
    response = client.post('/api/attachments', headers=USER, files={'file': ('file.docx', docx())})
    assert response.status_code == 413
    monkeypatch.setattr(backend.settings, 'ATTACHMENT_MAX_BYTES', 20 * 1024 * 1024)
    identifier = upload(client)
    result = client.post(f'/api/attachments/{identifier}/render', headers=USER,
                         json={'replacements': {'{{missing}}': 'x'}})
    assert result.status_code == 422
    backend.store.ttl = -1
    assert client.get('/api/attachments/' + identifier, headers=USER).status_code == 404


def test_shared_service_token_is_required_when_configured(client, monkeypatch):
    monkeypatch.setattr(get_settings(), 'SERVICE_TOKEN', 'test-internal-secret')
    result = client.post('/api/attachments', headers=USER, files={'file': ('x.docx', docx())})
    assert result.status_code == 403
    result = client.post('/api/attachments', headers={**USER, 'X-Ai-Box-Service-Token': 'test-internal-secret'},
                         files={'file': ('x.docx', docx())})
    assert result.status_code == 201, result.text
