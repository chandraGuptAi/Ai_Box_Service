from typing import Annotated
import asyncio

from fastapi import APIRouter, Depends, File, Header, HTTPException, Path, Request, Response, UploadFile
from fastapi.concurrency import run_in_threadpool

from .extract import embedded_preview, preview
from .schemas import ContextRequest, ExecuteRequest, PlanValidationRequest, TemplateEdit
from .template import office

def no_store(response: Response):
    response.headers['Cache-Control'] = 'no-store'


router = APIRouter(dependencies=[Depends(no_store)])


def owner(x_user_id: Annotated[str, Header(min_length=1, max_length=200)]):
    """Trusted gateway supplies the authenticated seller ID; no Bigship credential is needed."""
    if not x_user_id.strip():
        raise HTTPException(401, 'Authenticated user ID is required')
    return x_user_id


def service(request: Request):
    return request.app.state.attachments_service


@router.get('/capabilities')
def capabilities(request: Request):
    import shutil
    return {'upload_formats': ['pdf', 'docx', 'xlsx', 'pptx', 'png', 'jpeg', 'webp', 'tiff', 'gif', 'bmp'],
            'template_edit_formats': ['docx', 'xlsx', 'pptx'], 'ocr_available': bool(shutil.which('tesseract')),
            'vision_model_configured': service(request).vision.configured, 'ai_planning_location': 'main_backend',
            'office_preview_available': False, 'legacy_office_conversion_available': False}


@router.post('', status_code=201)
async def upload(request: Request, user: str = Depends(owner), file: UploadFile = File(...)):
    backend = service(request)
    try:
        chunks, size = [], 0
        while chunk := await file.read(1024 * 1024):
            size += len(chunk)
            if 0 < backend.settings.ATTACHMENT_MAX_BYTES < size:
                raise HTTPException(413, 'Attachment exceeds upload size limit')
            chunks.append(chunk)
        return await run_in_threadpool(backend.upload, user, file.filename or 'attachment', b''.join(chunks))
    finally:
        await file.close()


@router.post('/planning-context')
def planning_context(body: ContextRequest, request: Request, user: str = Depends(owner)):
    return service(request).context(user, body)


@router.post('/prepare')
async def prepare(body: ContextRequest, request: Request, user: str = Depends(owner)):
    try:
        async with asyncio.timeout(150):
            return await service(request).prepare(user, body)
    except TimeoutError:
        raise HTTPException(504, 'Visual preparation timed out. Use fewer pages/images in one report') from None


@router.post('/validate-plan')
def validate_plan(body: PlanValidationRequest, request: Request, user: str = Depends(owner)):
    return service(request).validate_plan(user, body)


@router.post('/execute')
def execute(body: ExecuteRequest, request: Request, user: str = Depends(owner)):
    return service(request).execute(user, body)


@router.post('/validate-report-plan')
def validate_report_plan(body: ExecuteRequest, request: Request, user: str = Depends(owner)):
    # Reference text belongs to template operations, not the report's factual sections.
    roles = {attachment.attachment_id: attachment.role for attachment in body.plan.decision.attachments}
    if any(excerpt.attachment_id not in roles for excerpt in body.plan.excerpts):
        raise HTTPException(422, 'A source excerpt references an unknown attachment')
    body.plan = body.plan.model_copy(update={'excerpts': [excerpt for excerpt in body.plan.excerpts
        if roles[excerpt.attachment_id] in {'data', 'data_and_design'}]})
    if body.plan.decision.source in {'attachment_only', 'combined'} and not (body.plan.analyses or body.plan.excerpts):
        raise HTTPException(422, 'Attachment report needs a table analysis or a verifiable source excerpt')
    validation = service(request).validate_plan(user, PlanValidationRequest(
        prompt=body.prompt or 'Attachment report', attachment_ids=body.attachment_ids, decision=body.plan.decision))
    for excerpt in body.plan.excerpts:
        meta, _ = service(request).store.get(user, excerpt.attachment_id)
        units = meta['inspection']['units']
        if excerpt.unit_index >= len(units) or len(units[excerpt.unit_index].get('text', '')) <= excerpt.quote_index * 3000:
            raise HTTPException(422, 'Select quote_index from the quote_candidates of an actual text unit; table-only units cannot be quoted')
    return {**validation, 'plan': body.plan.model_dump()}


@router.get('/{attachment_id}')
def inspect_attachment(attachment_id: str, request: Request, user: str = Depends(owner)):
    meta, _ = service(request).store.get(user, attachment_id)
    return {**service(request).summary(meta), 'inspection': meta['inspection'], 'privacy_status': 'not_sanitized'}


@router.get('/{attachment_id}/preview/{index}')
def attachment_preview(attachment_id: str, request: Request, index: int = Path(ge=0), user: str = Depends(owner)):
    meta, blob = service(request).store.get(user, attachment_id)
    return Response(preview(blob, meta['inspection']['format'], index), media_type='image/png',
                    headers={'Cache-Control': 'no-store'})


@router.post('/{attachment_id}/render')
def render_template(attachment_id: str, body: TemplateEdit, request: Request, user: str = Depends(owner)):
    meta, blob = service(request).store.get(user, attachment_id)
    kind = meta['inspection']['format']
    result = office(blob, kind, body)
    mime = {'docx': 'application/vnd.openxmlformats-officedocument.wordprocessingml.document',
            'xlsx': 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
            'pptx': 'application/vnd.openxmlformats-officedocument.presentationml.presentation'}[kind]
    return Response(result, media_type=mime, headers={'Content-Disposition': f'attachment; filename="report.{kind}"',
                                                     'X-Template-Review-Required': 'true',
                                                     'X-Formula-Recalculation-Required': 'true' if kind == 'xlsx' else 'false',
                                                     'Cache-Control': 'no-store'})


@router.get('/{attachment_id}/images/{index}')
def embedded_image(attachment_id: str, request: Request, index: int = Path(ge=0), user: str = Depends(owner)):
    meta, blob = service(request).store.get(user, attachment_id)
    if meta['inspection']['format'] not in {'docx', 'xlsx', 'pptx'}:
        raise HTTPException(415, 'This file has no Office image parts')
    return Response(embedded_preview(blob, index), media_type='image/png', headers={'Cache-Control': 'no-store'})


@router.delete('/{attachment_id}', status_code=204)
def delete_attachment(attachment_id: str, request: Request, user: str = Depends(owner)):
    service(request).store.delete(user, attachment_id)
    return Response(status_code=204)
