from fastapi import HTTPException
from pathlib import Path
import json

from . import extract
from .schemas import SourceDecision, AttachmentReportPlan
from .store import Store

class Service:
    def __init__(self, settings):
        self.settings = settings
        self.store = Store(settings.ATTACHMENT_STORAGE_DIR, settings.ATTACHMENT_TTL_SECONDS)
        from .vision import Vision
        self.vision = Vision(settings)

    async def prepare(self, owner, request):
        from fastapi.concurrency import run_in_threadpool
        prepared, vision_calls = [], 0
        for identifier in request.attachment_ids:
            meta, blob = await run_in_threadpool(self.store.get, owner, identifier)
            data = meta['inspection']
            if not data.get('requires_vision') or data.get('vision_prepared') or not self.vision.configured:
                prepared.append(self.summary(meta))
                continue
            resources = []
            if data['format'] == 'image':
                resources = [('page', i) for i in range(len(data['units']))]
            elif data['format'] == 'pdf':
                resources = [('page', u['index']) for u in data['units']
                             if 'rows' not in u and (u.get('ocr_used') or not u.get('text', '').strip())]
            else:
                resources = [('embedded', i) for i in range(data.get('embedded_image_count', 0))]
            if len(resources) > self.settings.ATTACHMENT_VISION_MAX_IMAGES:
                raise HTTPException(413, 'Too many visual pages/images; split the attachment into smaller files')
            # Mutate only the private loaded copy; commit after every image has succeeded.
            for resource, index in resources:
                png = await run_in_threadpool(extract.embedded_preview, blob, index) if resource == 'embedded' else await run_in_threadpool(extract.preview, blob, data['format'], index)
                reading = await self.vision.read(png)
                vision_calls += 1
                if resource == 'page':
                    data['units'][index]['text'] = reading['text']
                    data['units'][index]['visual_interpretation'] = 'vision'
                else:
                    data['units'].append({'index': len(data['units']), 'text': reading['text'],
                                          'embedded_image': index, 'visual_interpretation': 'vision'})
                for table in reading['tables']:
                    rows = table['rows']
                    data['units'].append({'index': len(data['units']), 'kind': 'table', 'title': table['title'],
                        'rows': rows, 'row_count': len(rows), 'column_count': max((len(row) for row in rows), default=0),
                        'source_image': index, 'extraction': 'vision'})
            if resources:
                data['warnings'].append('Visual content was extracted by an AI model; verify source figures against the original image.')
            data['vision_prepared'] = True
            await run_in_threadpool(self.store.update_inspection, owner, identifier, data)
            prepared.append(self.summary(meta))
        return {'attachments': prepared, 'vision_calls': vision_calls, 'external_data_calls_performed': False}

    def upload(self, owner, name, blob):
        if not blob:
            raise HTTPException(422, 'Attachment is empty')
        if 0 < self.settings.ATTACHMENT_MAX_BYTES < len(blob):
            raise HTTPException(413, 'Attachment exceeds upload size limit')
        meta = self.store.create(owner, name, blob, lambda b: extract.inspect(b, self.settings.ATTACHMENT_OCR_LANGUAGE))
        return self.summary(meta)

    def summary(self, meta):
        data = meta['inspection']
        return {'attachment_id': meta['id'], 'filename': meta['filename'], 'format': data['format'],
                'unit_count': len(data['units']), 'warnings': data.get('warnings', []),
                'requires_vision': data.get('requires_vision', False), 'complete': data['complete'],
                'vision_prepared': data.get('vision_prepared', False),
                'embedded_image_count': data.get('embedded_image_count', 0),
                'expires_at': meta['created_at'] + self.settings.ATTACHMENT_TTL_SECONDS}

    def context(self, owner, request):
        if len(set(request.attachment_ids)) != len(request.attachment_ids):
            raise HTTPException(422, 'Attachment IDs must be unique')
        attachments = []
        budget = 180000
        for identifier in request.attachment_ids:
            meta, _ = self.store.get(owner, identifier)
            units = []
            for unit in meta['inspection']['units']:
                if 'rows' in unit:
                    units.append({'index': unit['index'], 'sheet': unit.get('sheet', 'Table'),
                                  'row_count': unit['row_count'], 'column_count': unit['column_count'],
                                  'sample_rows': unit['rows'][:10], 'sample_only': len(unit['rows']) > 10})
                    units[-1]['tail_rows'] = unit['rows'][-5:]
                    units[-1]['tail_start_row'] = max(1, len(unit['rows']) - 4)
                else:
                    text = unit.get('text', '')
                    units.append({'index': unit['index'], 'text': text[:12000], 'truncated': len(text) > 12000,
                                  'quote_candidates': [{'index': i, 'text': text[start:start + 3000]}
                                      for i, start in enumerate(range(0, min(len(text), 12000), 3000))]})
            item = {**self.summary(meta), 'units': units, 'design': meta['inspection'].get('design')}
            budget -= len(json.dumps(item, ensure_ascii=True, default=str))
            if budget < 0:
                raise HTTPException(413, 'Planning context too large; select fewer attachments')
            attachments.append(item)
        skill_path = Path(__file__).resolve().parents[3] / '.agents' / 'skills' / 'attachment-report' / 'SKILL.md'
        if not skill_path.is_file():
            raise HTTPException(503, 'Attachment planning skill is not installed')
        return {'prompt': request.prompt, 'attachments': attachments, 'planning_instructions': skill_path.read_text(encoding='utf-8'),
                'decision_schema': SourceDecision.model_json_schema(), 'needs_ai_decision': True,
                'report_plan_schema': AttachmentReportPlan.model_json_schema(),
                'privacy_status': 'not_sanitized', 'send_through_privacy_guard': True,
                'external_data_calls_performed': False}

    def execute(self, owner, request):
        from .analysis import execute
        expected = set(request.attachment_ids)
        roles = {a.attachment_id: a.role for a in request.plan.decision.attachments}
        if set(roles) != expected:
            raise HTTPException(422, 'Plan must classify every supplied attachment')
        data_ids = {identifier for identifier, role in roles.items() if role in {'data', 'data_and_design'}}
        inspected = {identifier: self.store.get(owner, identifier)[0]['inspection'] for identifier in expected}
        tables, excerpts, ids = [], [], set()
        for spec in request.plan.analyses:
            if spec.attachment_id not in data_ids or spec.id in ids:
                raise HTTPException(422, 'Invalid or duplicate analysis reference')
            ids.add(spec.id)
            tables.append(execute(spec, inspected[spec.attachment_id]))
        for excerpt in request.plan.excerpts:
            if excerpt.attachment_id not in data_ids:
                raise HTTPException(422, 'Excerpt is outside the selected data attachments')
            units = inspected[excerpt.attachment_id]['units']
            if excerpt.unit_index >= len(units):
                raise HTTPException(422, 'Source unit does not exist')
            source_text = units[excerpt.unit_index].get('text', '')
            quote = (source_text[excerpt.quote_index * 3000:(excerpt.quote_index + 1) * 3000]
                     if excerpt.quote_index is not None else excerpt.quote)
            if not quote or quote not in source_text:
                raise HTTPException(422, 'The quoted text does not match its source')
            excerpts.append({**excerpt.model_dump(), 'quote': quote})
        return {'tables': tables, 'excerpts': excerpts, 'verified': True,
                'limitations': list(dict.fromkeys(note for data in inspected.values() for note in data.get('warnings', []))),
                'external_data_calls_performed': False}

    def validate_plan(self, owner, request):
        expected = set(request.attachment_ids)
        supplied = {a.attachment_id for a in request.decision.attachments}
        if supplied != expected:
            raise HTTPException(422, 'Decision must classify every supplied attachment exactly once')
        self.context(owner, request)
        source = request.decision.source
        unreadable = []
        for attachment in request.decision.attachments:
            if attachment.role not in {'data', 'data_and_design'}:
                continue
            meta, _ = self.store.get(owner, attachment.attachment_id)
            if not any(unit.get('text', '').strip() or any(any(value is not None for value in row)
                        for row in unit.get('rows', [])) for unit in meta['inspection']['units']):
                unreadable.append(attachment.attachment_id)
        return {'decision': request.decision.model_dump(),
                'can_generate': source != 'clarification_required' and not unreadable,
                'unreadable_data_attachments': unreadable,
                'fetch_external_data': source in {'api_only', 'combined'},
                'fetch_preview_count': source in {'api_only', 'combined'},
                'use_attachment_data': source in {'attachment_only', 'combined'},
                'apply_selected_filters': request.decision.use_selected_filters,
                'external_data_calls_performed': False}
