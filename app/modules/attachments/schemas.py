from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class StrictModel(BaseModel):
    model_config = ConfigDict(extra='forbid', allow_inf_nan=False)


ColumnRef = Annotated[int, Field(ge=0, le=199)] | Annotated[str, Field(min_length=1, max_length=100)]


class ContextRequest(StrictModel):
    prompt: str = Field(min_length=1, max_length=12000)
    attachment_ids: list[str] = Field(min_length=1, max_length=10)


class AttachmentRole(StrictModel):
    attachment_id: str
    role: Literal['data', 'design_reference', 'instructions', 'data_and_design', 'unused']


class SourceDecision(StrictModel):
    source: Literal['attachment_only', 'api_only', 'combined', 'clarification_required']
    attachments: list[AttachmentRole] = Field(default_factory=list, max_length=10)
    use_selected_filters: bool = False
    reason: str = Field(min_length=1, max_length=2000)
    question: str | None = Field(default=None, max_length=2000)

    @model_validator(mode='after')
    def check_sources(self):
        data = any(a.role in {'data', 'data_and_design'} for a in self.attachments)
        ids = [a.attachment_id for a in self.attachments]
        if len(ids) != len(set(ids)):
            raise ValueError('Attachment IDs must be unique')
        if self.source in {'attachment_only', 'combined'} and not data:
            raise ValueError('Selected source requires at least one data attachment')
        if self.source == 'api_only' and data:
            raise ValueError('API-only reports cannot declare attachment data')
        if self.source == 'clarification_required' and not self.question:
            raise ValueError('A clarification question is required')
        return self


class PlanValidationRequest(ContextRequest):
    decision: SourceDecision


class CellUpdate(StrictModel):
    sheet: str = Field(min_length=1, max_length=100)
    cell: str = Field(pattern=r'^[A-Z]{1,3}[1-9][0-9]{0,6}$')
    value: str | int | float | bool | None


class TemplateEdit(StrictModel):
    replacements: dict[str, str] = Field(default_factory=dict, max_length=200)
    cells: list[CellUpdate] = Field(default_factory=list, max_length=10000)

    @model_validator(mode='after')
    def check_operations(self):
        if not self.replacements and not self.cells:
            raise ValueError('At least one edit is required')
        if any(not key or len(key) > 1000 or len(value) > 20000 for key, value in self.replacements.items()):
            raise ValueError('Invalid replacement length')
        return self


class Measure(StrictModel):
    label: str = Field(min_length=1, max_length=100)
    op: Literal['count', 'sum', 'avg', 'min', 'max', 'unique_count', 'share']
    column: ColumnRef | None = None


class RowFilter(StrictModel):
    column: ColumnRef
    op: Literal['eq', 'ne', 'in', 'gt', 'gte', 'lt', 'lte']
    value: str | int | float | list[str | int | float]


class TableAnalysis(StrictModel):
    id: str = Field(pattern=r'^[a-z][a-z0-9_]{0,49}$')
    title: str = Field(min_length=1, max_length=160)
    attachment_id: str
    unit_index: int = Field(ge=0, le=10000)
    header_row: int = Field(default=1, ge=1, le=100000)
    last_data_row: int | None = Field(default=None, ge=1, le=100000,
                                      description='One-based final data row; excludes footer/subtotal rows when present')
    group_by: list[ColumnRef] = Field(default_factory=list, max_length=3)
    measures: list[Measure] = Field(min_length=1, max_length=8)
    filters: list[RowFilter] = Field(default_factory=list, max_length=10)
    chart: Literal['bar', 'line', 'pie', 'doughnut'] | None = None
    sort_measure: int | None = Field(default=None, ge=0, le=7)
    descending: bool = True
    limit: int = Field(default=50, ge=1, le=200)


class SourceExcerpt(StrictModel):
    title: str = Field(min_length=1, max_length=160)
    attachment_id: str
    unit_index: int = Field(ge=0, le=10000)
    quote: str | None = Field(default=None, min_length=1, max_length=4000)
    quote_index: int = Field(ge=0, le=3,
                                  description='Prefer the source quote candidate index; original text is resolved locally')

    @model_validator(mode='after')
    def quote_reference(self):
        if self.quote is None and self.quote_index is None:
            raise ValueError('Select an exact quote or a quote candidate index')
        return self


class TemplateBinding(StrictModel):
    attachment_id: str
    placeholder: str | None = Field(default=None, min_length=1, max_length=1000)
    sheet: str | None = None
    cell: str | None = None
    analysis_id: str
    row: int = Field(default=0, ge=0, le=199)
    column: int = Field(ge=0, le=11)


class AttachmentReportPlan(StrictModel):
    title: str = Field(min_length=1, max_length=160)
    decision: SourceDecision
    analyses: list[TableAnalysis] = Field(default_factory=list, max_length=10)
    excerpts: list[SourceExcerpt] = Field(default_factory=list, max_length=15)
    external_prompt: str | None = Field(default=None, max_length=8000,
        description='For API-only/combined, describe the shipment/channel analysis needed without referring to unavailable attached files')
    template_bindings: list[TemplateBinding] = Field(default_factory=list, max_length=200)


class ExecuteRequest(StrictModel):
    prompt: str = Field(default='', max_length=12000)
    attachment_ids: list[str] = Field(min_length=1, max_length=10)
    plan: AttachmentReportPlan
