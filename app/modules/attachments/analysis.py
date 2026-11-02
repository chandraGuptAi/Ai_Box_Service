"""Bounded typed aggregations over complete attachment tables; no generated code."""
from collections import defaultdict
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from datetime import date
import re

from fastapi import HTTPException


def numeric(value):
    if value is None or value == '':
        return None
    if isinstance(value, bool):
        raise HTTPException(422, 'Boolean cells cannot be used as numeric measures')
    text = re.sub(r'(?i)\bINR\b|\bUSD\b|\bEUR\b|[\u20b9$\u20ac,\s]', '', str(value))
    if text.startswith('(') and text.endswith(')'):
        text = '-' + text[1:-1]
    try:
        number = Decimal(text)
        if not number.is_finite():
            raise InvalidOperation
        return number
    except InvalidOperation:
        raise HTTPException(422, 'A selected numeric column contains unreadable values; clarify the column or clean the source') from None


def matches(value, condition):
    expected = condition.value
    if condition.op in {'gt', 'gte', 'lt', 'lte'}:
        if isinstance(value, str) and isinstance(expected, str) and re.match(r'^\d{4}-\d{2}-\d{2}(?:T|$)', value) and re.match(r'^\d{4}-\d{2}-\d{2}(?:T|$)', expected):
            try:
                first, second = date.fromisoformat(value[:10]), date.fromisoformat(expected[:10])
            except ValueError:
                raise HTTPException(422, 'A selected date column contains an invalid date') from None
        else:
            first, second = numeric(value), numeric(expected)
        if first is None or second is None:
            return False
        return {'gt': first > second, 'gte': first >= second, 'lt': first < second, 'lte': first <= second}[condition.op]
    if condition.op == 'in':
        if not isinstance(expected, list):
            raise HTTPException(422, 'The in filter requires a list')
        return str(value).casefold() in {str(v).casefold() for v in expected}
    equal = str(value).casefold() == str(expected).casefold()
    return equal if condition.op == 'eq' else not equal


def aggregate(rows, measure, denominator=None):
    if measure.op == 'share':
        return round(100 * len(rows) / denominator, 2) if denominator else 0
    if measure.op == 'count':
        return len(rows)
    if measure.column is None:
        raise HTTPException(422, 'This metric requires a source column')
    values = [row[measure.column] for row in rows if row[measure.column] not in (None, '')]
    if measure.op == 'unique_count':
        return len({str(value) for value in values})
    numbers = [numeric(value) for value in values]
    if not numbers:
        return None
    value = {'sum': lambda: sum(numbers), 'avg': lambda: sum(numbers) / len(numbers),
             'min': lambda: min(numbers), 'max': lambda: max(numbers)}[measure.op]()
    return float(value.quantize(Decimal('.01'), rounding=ROUND_HALF_UP))


def execute(spec, inspection):
    try:
        unit = inspection['units'][spec.unit_index]
        source = unit['rows']
        headers = source[spec.header_row - 1]
    except (IndexError, KeyError):
        raise HTTPException(422, 'The selected attachment unit has no usable table/header') from None
    def column_index(value):
        if isinstance(value, int):
            return value
        found = [i for i, header in enumerate(headers) if str(header).strip().casefold() == value.strip().casefold()]
        if len(found) != 1:
            raise HTTPException(422, 'The selected column name is missing or ambiguous; use its numeric index')
        return found[0]
    spec = spec.model_copy(update={
        'group_by': [column_index(value) for value in spec.group_by],
        'measures': [m.model_copy(update={'column': column_index(m.column) if m.column is not None else None}) for m in spec.measures],
        'filters': [f.model_copy(update={'column': column_index(f.column)}) for f in spec.filters]})
    required = set(spec.group_by) | {m.column for m in spec.measures if m.column is not None} | {f.column for f in spec.filters}
    end = spec.last_data_row if spec.last_data_row is not None else len(source)
    if end < spec.header_row or end > len(source):
        raise HTTPException(422, 'Selected data range is outside the source table')
    if any(column < 0 or column >= len(headers) for column in required):
        raise HTTPException(422, 'Analysis selected a column outside the source table')
    if len(set(spec.group_by)) != len(spec.group_by):
        raise HTTPException(422, 'Grouping columns must be unique')
    if spec.sort_measure is not None and spec.sort_measure >= len(spec.measures):
        raise HTTPException(422, 'Sort metric does not exist')
    # Missing cached results must never disappear from numeric totals as if they were blank.
    used = {m.column for m in spec.measures if m.op not in {'count', 'unique_count', 'share'} and m.column is not None}
    from openpyxl.utils.cell import coordinate_to_tuple
    for cell in unit.get('cells', []):
        row, col = coordinate_to_tuple(cell['cell'])
        if spec.header_row < row <= end and col - 1 in used and cell.get('formula') and cell.get('value') is None:
            raise HTTPException(422, 'Selected formulas have no cached values. Recalculate and save the workbook first')
    rows = [list(row) + [None] * max(0, len(headers) - len(row)) for row in source[spec.header_row:end]
            if any(value not in (None, '') for value in row)]
    selected = [row for row in rows if all(matches(row[f.column], f) for f in spec.filters)]
    groups = defaultdict(list)
    for row in selected:
        groups[tuple(str(row[c]) if row[c] is not None else 'Not available' for c in spec.group_by)].append(row)
    if not spec.group_by:
        groups[()] = selected
    records = [[*key, *[aggregate(group, measure, len(selected)) for measure in spec.measures]] for key, group in groups.items()]
    if spec.sort_measure is not None:
        index = len(spec.group_by) + spec.sort_measure
        records.sort(key=lambda row: (row[index] is None, -(row[index] or 0) if spec.descending else row[index] or 0,
                                      tuple(str(x) for x in row[:len(spec.group_by)])))
    else:
        records.sort(key=lambda row: tuple(str(x) for x in row[:len(spec.group_by)]))
    currency = {}
    for column in used:
        formats = [cell.get('number_format', '') for cell in unit.get('cells', [])
                   if coordinate_to_tuple(cell['cell'])[1] - 1 == column]
        symbols = {symbol for fmt in formats for symbol in ('\u20b9', '$', '\u20ac') if symbol in fmt}
        symbols.update(symbol for row in selected for symbol in ('\u20b9', '$', '\u20ac') if symbol in str(row[column]))
        for code, symbol in [('INR', '\u20b9'), ('USD', '$'), ('EUR', '\u20ac')]:
            if re.search(r'\b' + code + r'\b', str(headers[column]), re.I):
                symbols.add(symbol)
        if len(symbols) > 1:
            raise HTTPException(422, 'Selected monetary data contains mixed currencies; separate currencies before aggregation')
        if len(symbols) == 1:
            currency[column] = symbols.pop()
    return {'id': spec.id, 'title': spec.title,
            'columns': [str(headers[c] or f'Column {c + 1}') for c in spec.group_by] + [m.label for m in spec.measures],
            'values': records[:spec.limit], 'total_groups': len(records), 'truncated': len(records) > spec.limit,
            'source_rows': len(rows), 'matched_rows': len(selected),
            'totals': [aggregate(selected, measure, len(selected)) for measure in spec.measures],
            'group_columns': len(spec.group_by),
            'currency': [currency.get(m.column) for m in spec.measures],
            'formats': ['percent' if m.op == 'share' else 'number' for m in spec.measures], 'chart': spec.chart,
            'source': {'attachment_id': spec.attachment_id, 'unit_index': spec.unit_index,
                       'sheet': unit.get('sheet'), 'header_row': spec.header_row}}
