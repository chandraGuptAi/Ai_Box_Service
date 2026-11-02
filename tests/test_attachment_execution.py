import asyncio
import io

from tests.test_attachments import client, upload, xlsx, docx  # noqa: F401


def plan(identifier, **changes):
    return {'title': 'Order Analysis', 'decision': {'source': 'attachment_only', 'reason': 'Use uploaded data',
        'attachments': [{'attachment_id': identifier, 'role': 'data'}]}, 'analyses': [{
        'id': 'orders', 'title': 'Orders by Status', 'attachment_id': identifier, 'unit_index': 0,
        'last_data_row': 3, 'group_by': [0], 'measures': [{'op': 'count', 'label': 'Orders'},
        {'op': 'sum', 'column': 1, 'label': 'Amount'}]}], **changes}


def test_skill_is_loaded_and_full_workbook_analysis_uses_python(client):
    identifier = upload(client, xlsx(), 'orders.xlsx')
    body = {'prompt': 'Excel se report banao', 'attachment_ids': [identifier]}
    context = client.post('/api/attachments/planning-context', headers={'X-User-Id': 'seller-one'}, json=body).json()
    assert 'name: attachment-report' in context['planning_instructions']
    assert 'report_plan_schema' in context
    body['plan'] = plan(identifier)
    response = client.post('/api/attachments/validate-report-plan', headers={'X-User-Id': 'seller-one'}, json=body)
    assert response.status_code == 200, response.text
    result = client.post('/api/attachments/execute', headers={'X-User-Id': 'seller-one'}, json=body)
    assert result.status_code == 200, result.text
    table = result.json()['tables'][0]
    assert table['totals'] == [2, 3600]
    assert table['values'] == [['Processing', 1, 2400], ['Shipped', 1, 1200]]
    assert table['currency'] == [None, '\u20b9']
    assert not result.json()['external_data_calls_performed']


def test_document_quote_references_resolve_original_text_after_privacy_masking(client):
    identifier = upload(client, docx())
    body = {'attachment_ids': [identifier], 'plan': plan(identifier, analyses=[], excerpts=[{
        'title': 'Source Summary', 'attachment_id': identifier, 'unit_index': 0, 'quote_index': 0}])}
    response = client.post('/api/attachments/execute', headers={'X-User-Id': 'seller-one'}, json=body)
    assert response.status_code == 200, response.text
    assert response.json()['excerpts'][0]['quote'] == '{{total}} orders'
    body['plan']['excerpts'][0] = {'title': 'Summary', 'attachment_id': identifier, 'unit_index': 0,
                                  'quote': 'Invented source fact'}
    assert client.post('/api/attachments/execute', headers={'X-User-Id': 'seller-one'}, json=body).status_code == 422


def test_image_preparation_commits_local_model_tables_and_allows_typed_analysis(client):
    from PIL import Image
    image = io.BytesIO()
    Image.new('RGB', (100, 100), 'white').save(image, 'PNG')
    identifier = upload(client, image.getvalue(), 'orders.png')
    service = client.app.state.attachments_service
    class Visual:
        configured = True
        async def read(self, png):
            assert png.startswith(b'\x89PNG')
            return {'text': 'Status Amount\nShipped 1200', 'tables': [{'title': 'Orders',
                    'rows': [['Status', 'Amount'], ['Shipped', '1200']]}]}
    service.vision = Visual()
    body = {'prompt': 'Use image data', 'attachment_ids': [identifier]}
    assert client.post('/api/attachments/prepare', headers={'X-User-Id': 'seller-one'}, json=body).status_code == 200
    body['plan'] = plan(identifier)
    body['plan']['analyses'][0].update(unit_index=1, last_data_row=2)
    response = client.post('/api/attachments/execute', headers={'X-User-Id': 'seller-one'}, json=body)
    assert response.status_code == 200, response.text
    assert response.json()['tables'][0]['values'] == [['Shipped', 1, 1200]]
