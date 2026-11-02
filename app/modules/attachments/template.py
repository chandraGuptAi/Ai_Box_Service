"""Edit OOXML in-place in a copy, retaining unrelated template parts and styles."""
import io
import posixpath
import zipfile

from defusedxml import ElementTree as SafeET
from lxml import etree
from fastapi import HTTPException

from .schemas import TemplateEdit

NS = {'w': 'http://schemas.openxmlformats.org/wordprocessingml/2006/main',
      'a': 'http://schemas.openxmlformats.org/drawingml/2006/main',
      's': 'http://schemas.openxmlformats.org/spreadsheetml/2006/main',
      'r': 'http://schemas.openxmlformats.org/officeDocument/2006/relationships'}


def parse(blob):
    SafeET.fromstring(blob)  # Reject DTD entities before preserving namespaces with lxml.
    return etree.fromstring(blob, parser=etree.XMLParser(resolve_entities=False, no_network=True))


def replace_nodes(nodes, replacements):
    """Match across text runs; insert into the first matching run without rebuilding paragraphs."""
    text = ''.join(node.text or '' for node in nodes)
    matches = []
    import re
    pattern = re.compile('|'.join(re.escape(k) for k in sorted(replacements, key=len, reverse=True)))
    matches = list(pattern.finditer(text))
    starts, offset = [], 0
    for node in nodes:
        starts.append(offset)
        offset += len(node.text or '')
    for match in reversed(matches):
        touched = [i for i, node in enumerate(nodes) if starts[i] < match.end()
                   and starts[i] + len(node.text or '') > match.start()]
        if not touched:
            continue
        first, last = touched[0], touched[-1]
        prefix = (nodes[first].text or '')[:match.start() - starts[first]]
        suffix = (nodes[last].text or '')[match.end() - starts[last]:]
        nodes[first].text = prefix + replacements[match[0]] + (suffix if first == last else '')
        nodes[first].set('{http://www.w3.org/XML/1998/namespace}space', 'preserve')
        for i in touched[1:]:
            nodes[i].text = suffix if i == last else ''
    return {m[0] for m in matches}


def office(blob, kind, edits: TemplateEdit):
    if kind not in {'docx', 'xlsx', 'pptx'}:
        raise HTTPException(415, 'Template editing supports DOCX, XLSX and PPTX; PDF recreation is a separate export step')
    if edits.cells and kind != 'xlsx':
        raise HTTPException(422, 'Cell edits require an XLSX template')
    if edits.replacements and kind == 'xlsx':
        raise HTTPException(422, 'Use explicit sheet/cell edits for Excel templates')
    modified, found = {}, set()
    with zipfile.ZipFile(io.BytesIO(blob)) as source:
        if kind in {'docx', 'pptx'}:
            ns = NS['w' if kind == 'docx' else 'a']
            for name in source.namelist():
                if not name.endswith('.xml') or not name.startswith('word/' if kind == 'docx' else 'ppt/'):
                    continue
                root = parse(source.read(name))
                part_found = set()
                for p in root.iter(f'{{{ns}}}p'):
                    part_found.update(replace_nodes(list(p.iter(f'{{{ns}}}t')), edits.replacements))
                found.update(part_found)
                if part_found:
                    modified[name] = etree.tostring(root, xml_declaration=True, encoding='UTF-8', standalone=True)
            if set(edits.replacements) - found:
                raise HTTPException(422, 'One or more replacement placeholders were not found in the template')
        else:
            root = parse(source.read('xl/workbook.xml'))
            relationships = parse(source.read('xl/_rels/workbook.xml.rels'))
            targets = {r.get('Id'): r.get('Target') for r in relationships}
            sheets = {}
            for s in root.findall('s:sheets/s:sheet', NS):
                target = targets.get(s.get(f"{{{NS['r']}}}id"), '')
                path = posixpath.normpath(target.lstrip('/') if target.startswith('/') else 'xl/' + target)
                if path not in source.namelist() or not path.startswith('xl/'):
                    raise HTTPException(422, 'Invalid workbook sheet relationship')
                sheets[s.get('name')] = path
            roots = {}
            from openpyxl.utils.cell import coordinate_to_tuple
            for edit in edits.cells:
                if edit.sheet not in sheets:
                    raise HTTPException(422, 'Requested worksheet does not exist')
                row_no, col_no = coordinate_to_tuple(edit.cell)
                if row_no > 1048576 or col_no > 16384:
                    raise HTTPException(422, 'Cell is outside Excel bounds')
                path = sheets[edit.sheet]
                sheet = roots.setdefault(path, parse(source.read(path)))
                data = sheet.find('s:sheetData', NS)
                if data is None:
                    raise HTTPException(422, 'Worksheet has no cell data')
                row = next((r for r in data if r.get('r') == str(row_no)), None)
                if row is None:
                    raise HTTPException(422, 'Edit existing template rows; row expansion requires an explicit layout plan')
                cell = next((c for c in row if c.get('r') == edit.cell), None)
                if cell is None:
                    raise HTTPException(422, 'Edit existing template cells so styles remain explicit')
                for child in list(cell):
                    if etree.QName(child).localname in {'v', 'f', 'is'}:
                        cell.remove(child)
                cell.attrib.pop('t', None)
                value = edit.value
                if isinstance(value, str):
                    cell.set('t', 'inlineStr')
                    inline = etree.SubElement(cell, f"{{{NS['s']}}}is")
                    node = etree.SubElement(inline, f"{{{NS['s']}}}t")
                    node.text = value
                    node.set('{http://www.w3.org/XML/1998/namespace}space', 'preserve')
                elif value is not None:
                    if isinstance(value, bool):
                        cell.set('t', 'b')
                    etree.SubElement(cell, f"{{{NS['s']}}}v").text = str(int(value) if isinstance(value, bool) else value)
            for path, sheet in roots.items():
                modified[path] = etree.tostring(sheet, xml_declaration=True, encoding='UTF-8')
            calc = root.find('s:calcPr', NS)
            if calc is None:
                calc = etree.SubElement(root, f"{{{NS['s']}}}calcPr")
            calc.set('fullCalcOnLoad', '1')
            calc.set('forceFullCalc', '1')
            calc.set('calcMode', 'auto')
            modified['xl/workbook.xml'] = etree.tostring(root, xml_declaration=True, encoding='UTF-8')
            # Old cached formula results must not be presented as recalculated figures.
            for path in set(sheets.values()):
                sheet = roots.get(path)
                if sheet is None:
                    sheet = parse(source.read(path))
                for cell in sheet.findall('.//s:c', NS):
                    if cell.find('s:f', NS) is not None:
                        cached = cell.find('s:v', NS)
                        if cached is not None:
                            cell.remove(cached)
                modified[path] = etree.tostring(sheet, xml_declaration=True, encoding='UTF-8')
        output = io.BytesIO()
        with zipfile.ZipFile(output, 'w', compression=zipfile.ZIP_DEFLATED) as target:
            for info in source.infolist():
                target.writestr(info, modified.get(info.filename, source.read(info.filename)))
        return output.getvalue()
