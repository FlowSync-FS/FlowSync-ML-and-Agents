"""Pydantic schemas for temperature APIs."""

from pydantic import BaseModel


class TemperatureReadingResponse(BaseModel):
    """Output payload for a temperature reading."""
    device_id: str
    recorded_at: str
    temperature_c: float
