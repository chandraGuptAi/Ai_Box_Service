from datetime import date

from fastapi import APIRouter, Depends, HTTPException, Path, Query, Request

from app.core.security import bigship_authorization

from .schemas import ChannelListResponse, ChannelOrdersResponse, ChannelOrderStatus, PreviewCountResponse

router = APIRouter()


def check_period(from_date: date | None, to_date: date | None) -> None:
    if from_date and to_date and from_date > to_date:
        raise HTTPException(422, "from_date must be on or before to_date")


@router.get("/channels", response_model=ChannelListResponse, summary="List the caller's connected sales channels")
async def list_channels(request: Request, authorization: str = Depends(bigship_authorization)) -> ChannelListResponse:
    return await request.app.state.channel_wise_report_service.list_channels(authorization)


@router.get(
    "/channels/{channel_id}/orders",
    response_model=ChannelOrdersResponse,
    summary="All store orders of one channel, with a summary",
)
async def list_channel_orders(
    request: Request,
    channel_id: int = Path(..., ge=1, description="`channel_id` from GET /channels"),
    status: ChannelOrderStatus = Query(ChannelOrderStatus.all, description="Order status filter"),
    from_date: date | None = Query(None, description="First IST day (by order date), inclusive"),
    to_date: date | None = Query(None, description="Last IST day (by order date), inclusive"),
    authorization: str = Depends(bigship_authorization),
) -> ChannelOrdersResponse:
    check_period(from_date, to_date)
    return await request.app.state.channel_wise_report_service.list_channel_orders(
        authorization, channel_id, status, from_date, to_date)


@router.get(
    "/preview-count",
    response_model=PreviewCountResponse,
    summary="Records an AI Box report will read for these filters ('AI will read about N ...')",
)
async def preview_count(
    request: Request,
    from_date: date = Query(..., description="First IST day, inclusive"),
    to_date: date = Query(..., description="Last IST day, inclusive"),
    status_type: int = Query(0, ge=0, description="Shipment status code (0 = all); ignored with channel_id"),
    channel_id: int | None = Query(None, ge=1, description="Count this channel's orders instead of shipments"),
    authorization: str = Depends(bigship_authorization),
) -> PreviewCountResponse:
    check_period(from_date, to_date)
    return await request.app.state.channel_wise_report_service.preview_count(
        authorization, from_date, to_date, status_type, channel_id)
