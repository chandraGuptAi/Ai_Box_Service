import csv
import io
import json

from fastapi import HTTPException
from typing import Any

from .client import VoiceAIAdminClient
from .schemas import CallFilters


def query(filters: CallFilters, *names: str) -> dict[str, Any]:
    """Voice AI query parameters; unset filters are left out."""
    values = filters.model_dump()
    return {name: (values[name].isoformat() if hasattr(values[name], "isoformat") else values[name])
            for name in names if values[name] is not None}


LIST_PARAMS = ("client_id", "type", "campaign_id", "phone", "date_from", "date_to", "limit", "offset")


class Service:
    def __init__(self, client: VoiceAIAdminClient, recording_path: str) -> None:
        self.client = client
        #: e.g. "/api/audit_log/call-logs/{call_id}/recording"
        self.recording_path = recording_path

    def own_links(self, call: dict[str, Any]) -> dict[str, Any]:
        """Point a call's recording links at this service, which adds the admin key.

        Voice AI's own links need the admin key or expire; these work for anyone allowed to call this API.
        """
        recording = call.get("recording")
        if isinstance(recording, dict) and call.get("call_id"):
            path = self.recording_path.format(call_id=call["call_id"])
            recording = {k: v for k, v in recording.items() if k != "links_expire_at"}
            if recording.get("available"):
                recording.update(url=path, play_url=path, download_url=path + "?download=true")
            call = {**call, "recording": recording}
        return call

    async def call_logs(self, filters: CallFilters) -> dict[str, Any]:
        body = await self.client.get_json("/v1/admin/call-logs", query(filters, *LIST_PARAMS, "status"))
        return {**body, "calls": [self.own_links(c) for c in body.get("calls") or [] if isinstance(c, dict)]}

    async def call_log(self, call_id: str) -> dict[str, Any]:
        return self.own_links(await self.client.get_json(f"/v1/admin/call-logs/{call_id}"))

    async def call_analyses(self, filters: CallFilters) -> dict[str, Any]:
        return await self.client.get_json("/v1/admin/call-analyses", query(filters, *LIST_PARAMS, "outcome"))


    async def export_call_logs_csv(self, filters: CallFilters) -> bytes:
        """Export all matching calls, independently of the table's limit/offset."""
        calls = []
        offset = 0
        total = None
        while total is None or offset < total:
            page = await self.call_logs(filters.model_copy(update={"limit": 500, "offset": offset}))
            rows = page["calls"]
            if total is None:
                total = int(page["total"])
            if not rows:
                if offset < total:
                    raise HTTPException(502, "Call logs changed during export; please retry")
                break
            calls.extend(rows)
            offset += len(rows)

        # Keep every returned field, including fields present only on later pages.
        fields = list(dict.fromkeys(key for call in calls for key in call))
        if not fields:
            fields = ["call_id", "type", "client_id", "customer_name", "phone_number",
                      "status", "answered", "duration_s", "cost_rupees", "analysis", "recording"]
        output = io.StringIO(newline="")
        writer = csv.DictWriter(output, fieldnames=fields)
        writer.writeheader()
        for call in calls:
            row = {}
            for key, value in call.items():
                if isinstance(value, (dict, list)):
                    value = json.dumps(value, ensure_ascii=False)
                # Prevent spreadsheet software from executing untrusted cell formulas.
                if isinstance(value, str) and value.lstrip().startswith(("=", "+", "-", "@")):
                    value = "'" + value
                row[key] = value
            writer.writerow(row)
        return output.getvalue().encode("utf-8-sig")


    async def export_call_logs_excel(self, filters: CallFilters) -> bytes:
        from openpyxl import Workbook
        from openpyxl.styles import Alignment, Font, PatternFill
        from openpyxl.utils import get_column_letter
        from datetime import datetime, timezone, timedelta
    
        data = await self.export_call_logs_csv(filters)
        rows = list(csv.reader(io.StringIO(data.decode("utf-8-sig"))))
        workbook = Workbook()
        sheet = workbook.active
        sheet.title = "Call Logs"
        width = len(rows[0])
        sheet.merge_cells(start_row=1, start_column=1, end_row=1, end_column=width)
        sheet.cell(1, 1, "BIGSHIP | Voice AI Call Report")
        sheet.cell(1, 1).font = Font(name="Calibri", size=18, bold=True, color="FFFFFF")
        for cell in sheet[1]:
            cell.fill = PatternFill("solid", fgColor="111B53")
        sheet.row_dimensions[1].height = 38
        sheet.cell(2, 1, "Exported: " + datetime.now(timezone(timedelta(hours=5, minutes=30))).strftime("%d %b %Y, %I:%M %p IST"))
        sheet.cell(2, 1).font = Font(size=11, color="64748B")
        sheet.merge_cells(start_row=2, start_column=1, end_row=2, end_column=width)
        sheet.append([])
        sheet.append([name.replace("_", " ").title() for name in rows[0]])
        for row in rows[1:]:
            sheet.append(row)
        for cell in sheet[4]:
            cell.fill = PatternFill("solid", fgColor="3346A8")
            cell.font = Font(bold=True, color="FFFFFF")
            cell.alignment = Alignment(vertical="center")
        sheet.row_dimensions[4].height = 28
        for row in sheet.iter_rows(min_row=5):
            for cell in row:
                cell.font = Font(name="Calibri", size=11, color="1E293B")
                cell.alignment = Alignment(vertical="top", wrap_text=True)
                if cell.row % 2:
                    cell.fill = PatternFill("solid", fgColor="F1F5FB")
            sheet.row_dimensions[row[0].row].height = 32
        for index, key in enumerate(rows[0], 1):
            sheet.column_dimensions[get_column_letter(index)].width = 32 if key in ("analysis", "recording") else 24
            if key in ("duration_s", "cost_rupees"):
                for row in sheet.iter_rows(min_row=5, min_col=index, max_col=index):
                    cell = row[0]
                    try:
                        cell.value = float(cell.value)
                        cell.number_format = '"?" #,##0.00' if key == "cost_rupees" else '0" s"'
                    except (TypeError, ValueError):
                        pass
        sheet.freeze_panes = "D5"
        sheet.sheet_view.showGridLines = False
        sheet.auto_filter.ref = f"A4:{get_column_letter(width)}{max(sheet.max_row, 4)}"
        sheet.sheet_properties.pageSetUpPr.fitToPage = True
        sheet.page_setup.orientation = "landscape"
        sheet.page_setup.paperSize = sheet.PAPERSIZE_A3
        sheet.page_setup.fitToWidth = 1
        sheet.page_setup.fitToHeight = 0
        sheet.print_title_rows = "1:4"
        output = io.BytesIO()
        workbook.save(output)
        return output.getvalue()
