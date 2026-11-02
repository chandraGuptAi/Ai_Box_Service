import asyncio
import io
import json

import httpx
import pytest
from fastapi import HTTPException

from app.core.config import get_settings
from app.modules.attachments.analysis import execute
from app.modules.attachments.schemas import TableAnalysis
from app.modules.attachments.vision import Vision


def spec(**updates):
    return TableAnalysis.model_validate({'id': 'status', 'title': 'Status Totals', 'attachment_id': 'a' * 32,
        'unit_index': 0, 'group_by': [0], 'measures': [{'op': 'count', 'label': 'Orders'},
        {'op': 'sum', 'column': 1, 'label': 'Amount'}], **updates})


def data(rows=None):
    return {'units': [{'rows': rows or [['Status', 'Amount'], ['Shipped', '1,200.10'], ['Processing', 2400.2],
                                      ['Shipped', 0.1]], 'cells': []}]}


def test_full_data_aggregation_exact_decimals_and_group_totals():
    result = execute(spec(limit=1), data())
    assert result['totals'] == [3, 3600.4]
    assert result['total_groups'] == 2
    assert result['truncated']
    assert result['source_rows'] == 3
    assert result['values'] == [['Processing', 1, 2400.2]]


def test_filters_sort_and_explicit_source_footer_exclusion():
    result = execute(spec(last_data_row=3, sort_measure=1, filters=[{'column': 1, 'op': 'gte', 'value': 1000}]),
                     data([['Status', 'Amount'], ['Shipped', 1200], ['Processing', 2400], ['Total', 3600]]))
    assert result['totals'] == [2, 3600]
    assert result['values'][0] == ['Processing', 1, 2400]


def test_exact_column_names_are_resolved_to_bounded_indexes():
    result = execute(spec(group_by=['Status'], measures=[{'op': 'sum', 'column': 'Amount', 'label': 'Amount'}]), data())
    assert result['values'] == [['Processing', 2400.2], ['Shipped', 1200.2]]
    with pytest.raises(HTTPException, match='ambiguous'):
        execute(spec(group_by=['Status']), data([['Status', 'Status'], ['Shipped', 1200]]))


def test_shares_use_the_complete_filtered_cohort_and_date_filters_are_calendar_based():
    source = data([['Status', 'Date'], ['Shipped', '2026-10-29T12:00:00Z'],
                   ['Processing', '2026-10-30'], ['Shipped', '2026-09-01']])
    result = execute(spec(measures=[{'op': 'count', 'label': 'Orders'}, {'op': 'share', 'label': 'Share'}],
        filters=[{'column': 'Date', 'op': 'gte', 'value': '2026-10-01'}]), source)
    assert result['values'] == [['Processing', 1, 50], ['Shipped', 1, 50]]
    assert result['totals'] == [2, 100]
    assert result['formats'] == ['number', 'percent']


@pytest.mark.parametrize('value', ['not a number', '=SUM(A1:A2)', float('inf'), True])
def test_unreadable_numeric_values_are_not_silently_zero(value):
    with pytest.raises(HTTPException):
        execute(spec(), data([['Status', 'Amount'], ['Shipped', value]]))


def test_missing_formula_cache_blocks_calculation():
    source = data([['Status', 'Amount'], ['Shipped', None]])
    source['units'][0]['cells'] = [{'cell': 'B2', 'formula': '=SUM(B3:B4)', 'value': None}]
    with pytest.raises(HTTPException, match='cached'):
        execute(spec(), source)


def test_uncertain_vision_never_becomes_report_data():
    settings = get_settings().model_copy(update={'ATTACHMENT_VISION_BASE_URL': 'http://127.0.0.1:11434/v1',
                                                'ATTACHMENT_VISION_MODEL': 'test-vision'})
    def respond(request):
        body = json.loads(request.content)
        assert body['messages'][1]['content'][1]['image_url']['url'].startswith('data:image/png;base64,')
        return httpx.Response(200, json={'choices': [{'message': {'content': json.dumps({
            'text': '', 'uncertain': True, 'uncertainty_reason': 'Unreadable amount', 'tables': []})}}]})
    with pytest.raises(HTTPException, match='unreadable'):
        asyncio.run(Vision(settings, transport=httpx.MockTransport(respond)).read(b'png-fixture'))


def test_deepseek_vision_has_separate_auth_and_structured_extraction():
    settings = get_settings().model_copy(update={'ATTACHMENT_VISION_BASE_URL': 'https://api.deepseek.com',
        'ATTACHMENT_VISION_MODEL': 'deepseek-flash', 'ATTACHMENT_VISION_API_KEY': 'test-vision-key'})
    def respond(request):
        assert request.headers['Authorization'] == 'Bearer test-vision-key'
        assert 'X-Ai-Box-Service-Token' not in request.headers
        return httpx.Response(200, json={'choices': [{'message': {'content': json.dumps({
            'text': 'Orders 3', 'uncertain': False, 'tables': [{'title': 'Orders', 'rows': [['Orders'], [3]]}]})}}]})
    result = asyncio.run(Vision(settings, transport=httpx.MockTransport(respond)).read(b'png-fixture'))
    assert result['tables'][0]['rows'] == [['Orders'], [3]]


def test_vision_rejects_unsupported_provider_before_credentials_leave():
    settings = get_settings().model_copy(update={'ATTACHMENT_VISION_BASE_URL': 'https://example.invalid',
                                                'ATTACHMENT_VISION_MODEL': 'test'})
    with pytest.raises(HTTPException):
        asyncio.run(Vision(settings).read(b'png-fixture'))
