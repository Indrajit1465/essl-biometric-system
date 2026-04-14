# app/schemas.py
"""
H7: Pydantic input validation schemas for API endpoints.
Replaces raw `dict` payloads with properly validated models.
"""
from __future__ import annotations

import re
from typing import Optional

from pydantic import BaseModel, Field, field_validator


_TIME_RE = re.compile(r"^\d{2}:\d{2}$")


class EmployeeCreate(BaseModel):
    """Schema for POST /api/employees"""
    device_user_id: str = Field(..., min_length=1, max_length=64)
    name: str = Field(default="", max_length=128)
    employee_code: Optional[str] = Field(default=None, max_length=64)
    department: Optional[str] = Field(default=None, max_length=128)
    shift_start: str = Field(default="09:00", max_length=8)
    shift_end: str = Field(default="18:00", max_length=8)
    grace_minutes: int = Field(default=15, ge=0, le=180)

    @field_validator("shift_start", "shift_end")
    @classmethod
    def validate_time_format(cls, v: str) -> str:
        if not _TIME_RE.match(v):
            raise ValueError(f"Time must be HH:MM format, got {v!r}")
        h, m = map(int, v.split(":"))
        if h > 23 or m > 59:
            raise ValueError(f"Invalid time {v!r}")
        return v

    @field_validator("name", mode="before")
    @classmethod
    def default_name(cls, v: str, info) -> str:
        if not v:
            did = info.data.get("device_user_id", "?")
            return f"Employee {did}"
        return v


class DeviceRegister(BaseModel):
    """Schema for POST /api/devices"""
    serial_number: str = Field(..., min_length=1, max_length=64)
    name: Optional[str] = Field(default=None, max_length=128)
    location: Optional[str] = Field(default=None, max_length=128)
    ip_address: Optional[str] = Field(default=None, max_length=48)
    port: int = Field(default=4370, ge=1, le=65535)

    @field_validator("serial_number")
    @classmethod
    def strip_serial(cls, v: str) -> str:
        return v.strip()

    @field_validator("ip_address")
    @classmethod
    def validate_ip(cls, v: Optional[str]) -> Optional[str]:
        if v is not None:
            v = v.strip()
            if v and not re.match(r"^\d{1,3}(\.\d{1,3}){3}$", v):
                raise ValueError(f"Invalid IP address: {v!r}")
        return v
