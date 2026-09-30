"""The one common shape every source adapter produces.

Report: source, source_id, kind (breakdown | accident | lane_closure),
        lat, lon, road, direction, position (shoulder | in_lane | unknown),
        description, from_place, to_place, reported_at, n_reports

Rules from the brief:
- Never invent a road, junction, direction or cause. Use None and the
  formatter shows "unknown".
- Store no personal data. Reports carry none (no reporter names or ids).
- Times are stored as timezone-aware UTC datetimes.
"""
from __future__ import annotations

from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from typing import Literal, Optional

Kind = Literal["breakdown", "accident", "lane_closure"]
Position = Literal["shoulder", "in_lane", "unknown"]
Source = Literal["tomtom", "nh", "waze"]


@dataclass(slots=True)
class Report:
    source: str                     # "tomtom" | "nh" | "waze"
    source_id: str                  # the source's own id for this incident
    kind: str                       # "breakdown" | "accident" | "lane_closure"
    lat: float
    lon: float
    road: Optional[str] = None      # "M25", "A316" … or None if the source did not say
    direction: Optional[str] = None # free text from the source, e.g. "clockwise", "westbound"
    position: str = "unknown"       # "shoulder" | "in_lane" | "unknown"
    description: Optional[str] = None
    from_place: Optional[str] = None
    to_place: Optional[str] = None
    reported_at: Optional[datetime] = None  # UTC, tz-aware; when the source first reported it
    n_reports: Optional[int] = None         # community report count, if the source has one
    # Extra, per-source facts that do not fit above (no personal data). Kept
    # small; used for corroboration text such as NH lane counts.
    extra: dict = field(default_factory=dict)
    # For stretch reports (NH): the geometry as [(lon, lat), ...]. Points leave it empty.
    line: list = field(default_factory=list)

    def __post_init__(self) -> None:
        if self.kind not in ("breakdown", "accident", "lane_closure"):
            raise ValueError(f"bad kind {self.kind!r}")
        if self.position not in ("shoulder", "in_lane", "unknown"):
            raise ValueError(f"bad position {self.position!r}")
        if self.reported_at is not None:
            if self.reported_at.tzinfo is None:
                raise ValueError("reported_at must be timezone-aware (UTC)")
            self.reported_at = self.reported_at.astimezone(timezone.utc)

    def to_dict(self) -> dict:
        d = asdict(self)
        if self.reported_at is not None:
            d["reported_at"] = self.reported_at.isoformat()
        return d
