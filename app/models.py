# app/models.py
from __future__ import annotations
from datetime import datetime, date
from typing import Optional
from sqlalchemy import (
    BigInteger, Boolean, Date, DateTime, Float,
    ForeignKey, Integer, SmallInteger, String, Text,
    UniqueConstraint, func,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    pass


class Device(Base):
    __tablename__ = "devices"
    id:            Mapped[int]           = mapped_column(Integer, primary_key=True)
    serial_number: Mapped[str]           = mapped_column(String(64), unique=True, nullable=False)
    name:          Mapped[Optional[str]] = mapped_column(String(128))
    location:      Mapped[Optional[str]] = mapped_column(String(128))
    ip_address:    Mapped[Optional[str]] = mapped_column(String(48))
    port:          Mapped[int]           = mapped_column(Integer, default=4370)
    protocol:      Mapped[str]           = mapped_column(String(16), default="ADMS")
    is_active:     Mapped[bool]          = mapped_column(Boolean, default=True)
    last_seen_at:  Mapped[Optional[datetime]] = mapped_column(DateTime)
    created_at:    Mapped[datetime]      = mapped_column(DateTime, server_default=func.now())
    punch_logs: Mapped[list[RawPunchLog]] = relationship(back_populates="device")


class RawPunchLog(Base):
    __tablename__ = "raw_punch_logs"
    __table_args__ = (
        UniqueConstraint("device_serial", "employee_device_id", "punch_time", name="uq_punch"),
    )
    id:                 Mapped[int]  = mapped_column(BigInteger, primary_key=True)
    device_serial:      Mapped[str]  = mapped_column(String(64), ForeignKey("devices.serial_number"))
    employee_device_id: Mapped[str]  = mapped_column(String(64), nullable=False)
    punch_time:         Mapped[datetime] = mapped_column(DateTime, nullable=False)
    status:             Mapped[int]  = mapped_column(SmallInteger, default=0)
    verify_type:        Mapped[int]  = mapped_column(SmallInteger, default=1)
    is_processed:       Mapped[bool] = mapped_column(Boolean, default=False)
    raw_payload:        Mapped[Optional[str]] = mapped_column(Text)
    source:             Mapped[str]  = mapped_column(String(16), default="ADMS")
    received_at:        Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    device: Mapped[Device] = relationship(back_populates="punch_logs")


class Employee(Base):
    __tablename__ = "employees"
    id:              Mapped[int]           = mapped_column(Integer, primary_key=True)
    device_user_id:  Mapped[str]           = mapped_column(String(64), unique=True, nullable=False)
    name:            Mapped[str]           = mapped_column(String(128), nullable=False)
    employee_code:   Mapped[Optional[str]] = mapped_column(String(64))
    department:      Mapped[Optional[str]] = mapped_column(String(128))
    shift_start:     Mapped[Optional[str]] = mapped_column(String(8))
    shift_end:       Mapped[Optional[str]] = mapped_column(String(8))
    grace_minutes:   Mapped[int]           = mapped_column(Integer, default=15)
    is_active:       Mapped[bool]          = mapped_column(Boolean, default=True)
    created_at:      Mapped[datetime]      = mapped_column(DateTime, server_default=func.now())
    daily_records:   Mapped[list[DailyAttendance]] = relationship(back_populates="employee")
    summary_records: Mapped[list[AttendanceSummary]] = relationship(back_populates="employee")


class DailyAttendance(Base):
    """Internal computed table — stores raw UTC datetimes."""
    __tablename__ = "daily_attendance"
    __table_args__ = (UniqueConstraint("employee_id", "work_date", name="uq_daily"),)
    id:               Mapped[int]           = mapped_column(Integer, primary_key=True)
    employee_id:      Mapped[int]           = mapped_column(Integer, ForeignKey("employees.id"))
    work_date:        Mapped[date]          = mapped_column(Date, nullable=False)
    first_in:         Mapped[Optional[datetime]] = mapped_column(DateTime)
    last_out:         Mapped[Optional[datetime]] = mapped_column(DateTime)
    total_minutes:    Mapped[Optional[int]]  = mapped_column(Integer)
    status:           Mapped[str]           = mapped_column(String(20), default="ABSENT")
    is_late:          Mapped[bool]          = mapped_column(Boolean, default=False)
    late_minutes:     Mapped[int]           = mapped_column(Integer, default=0)
    overtime_minutes: Mapped[int]           = mapped_column(Integer, default=0)
    punch_count:      Mapped[int]           = mapped_column(Integer, default=0)
    computed_at:      Mapped[datetime]      = mapped_column(DateTime, server_default=func.now())
    employee: Mapped[Employee] = relationship(back_populates="daily_records")


class AttendanceSummary(Base):
    """
    Human-readable summary table — times stored as IST strings 'HH:MM'.
    This is what the dashboard and reports read from.
    Populated/refreshed by recompute_daily().
    """
    __tablename__ = "attendance_summary"
    __table_args__ = (UniqueConstraint("emp_id", "work_date", name="uq_summary"),)

    id:           Mapped[int]           = mapped_column(Integer, primary_key=True)
    # FK to employees table
    employee_id:  Mapped[int]           = mapped_column(Integer, ForeignKey("employees.id"))
    # Denormalised for easy querying — no join needed for reports
    emp_id:       Mapped[str]           = mapped_column(String(64), nullable=False,
                                              doc="device_user_id — e.g. '1', '2', '91'")
    emp_name:     Mapped[str]           = mapped_column(String(128), nullable=False)
    work_date:    Mapped[date]          = mapped_column(Date, nullable=False)
    punch_in:     Mapped[Optional[str]] = mapped_column(String(8),
                                              doc="First punch-in in IST, format HH:MM")
    punch_out:    Mapped[Optional[str]] = mapped_column(String(8),
                                              doc="Last punch-out in IST, format HH:MM")
    is_late:      Mapped[bool]          = mapped_column(Boolean, default=False)
    late_minutes: Mapped[int]           = mapped_column(Integer, default=0)
    total_hours:  Mapped[Optional[float]] = mapped_column(Float)
    status:       Mapped[str]           = mapped_column(String(20), default="ABSENT")
    updated_at:   Mapped[datetime]      = mapped_column(DateTime, server_default=func.now())

    employee: Mapped[Employee] = relationship(back_populates="summary_records")