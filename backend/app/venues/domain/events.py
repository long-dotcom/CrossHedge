"""统一交易场所事件。"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Any

from app.core.time_utils import utc_now
from app.venues.domain.models import AccountSnapshot, Fill, OrderSnapshot, Position


class VenueEventType(StrEnum):
    ORDER_SUBMITTED = "ORDER_SUBMITTED"
    ORDER_ACCEPTED = "ORDER_ACCEPTED"
    ORDER_PARTIALLY_FILLED = "ORDER_PARTIALLY_FILLED"
    ORDER_FILLED = "ORDER_FILLED"
    ORDER_PENDING_CANCEL = "ORDER_PENDING_CANCEL"
    ORDER_CANCELED = "ORDER_CANCELED"
    ORDER_EXPIRED = "ORDER_EXPIRED"
    ORDER_REJECTED = "ORDER_REJECTED"
    ORDER_UNKNOWN = "ORDER_UNKNOWN"
    FILL = "FILL"
    ACCOUNT = "ACCOUNT"
    POSITION = "POSITION"
    STREAM_CONNECTED = "STREAM_CONNECTED"
    STREAM_DISCONNECTED = "STREAM_DISCONNECTED"
    RECONCILIATION_REQUIRED = "RECONCILIATION_REQUIRED"


@dataclass(frozen=True)
class VenueEvent:
    event_id: str
    venue: str
    event_type: VenueEventType
    occurred_at: datetime
    received_at: datetime = field(default_factory=utc_now)
    order: OrderSnapshot | None = None
    fill: Fill | None = None
    account: AccountSnapshot | None = None
    position: Position | None = None
    reconciliation: bool = False
    raw: dict[str, Any] = field(default_factory=dict, compare=False, repr=False)
