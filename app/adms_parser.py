# app/adms_parser.py
"""
ADMS (Attendance Data Management System) Protocol Parser
---------------------------------------------------------
eSSL / ZKTeco devices POST attendance data in a proprietary text/plain format.

Typical ADMS POST body
~~~~~~~~~~~~~~~~~~~~~~
    SN=DESX12345678&table=ATTLOG&Stamp=9999
    1\t101\t2024-06-01 09:02:11\t0\t1\t0
    2\t205\t2024-06-01 09:07:45\t0\t1\t0

Fields in each tab-delimited record:
    UID        — internal device slot number (not the employee ID)
    user_id    — employee ID as enrolled on device (use this for matching)
    timestamp  — YYYY-MM-DD HH:MM:SS
    status     — 0=Check-In, 1=Check-Out, 2=Break-Out, 3=Break-In, 4=OT-In, 5=OT-Out
    verify     — 1=FP, 3=Password, 11=Face, 15=Palm, 200=RFID Card, 255=Card/Other
    workcode   — configurable (0 = no code)

The device also sends:
    OPERLOG    — device operation log (user enrolled, deleted, etc.)
    ATTPHOTO   — photo snapshots on punch (face recognition devices)

Handshake (GET /iclock/cdata)
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
Device sends a GET before POSTing. Server must reply with a config block
or the device will not push any data.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone, timedelta
from typing import Optional

logger = logging.getLogger(__name__)

# Status code → human label mapping
PUNCH_STATUS = {
    0: "CHECK_IN",
    1: "CHECK_OUT",
    2: "BREAK_OUT",
    3: "BREAK_IN",
    4: "OT_IN",
    5: "OT_OUT",
}

# M11: Added 255 mapping — common on eSSL F18 devices
VERIFY_TYPE = {
    1:   "FINGERPRINT",
    3:   "PASSWORD",
    11:  "FACE",
    15:  "PALM",
    200: "RFID_CARD",
    255: "CARD_OTHER",
}


@dataclass
class ParsedPunch:
    uid:             str          # Raw device UID (slot)
    employee_id:     str          # Enrolled employee user_id (use for DB lookup)
    punch_time:      datetime     # Timezone-aware UTC datetime
    status:          int          # See PUNCH_STATUS
    verify_type:     int          # See VERIFY_TYPE
    workcode:        int = 0
    raw_line:        str = ""     # Original line (for audit)

    @property
    def status_label(self) -> str:
        return PUNCH_STATUS.get(self.status, f"UNKNOWN({self.status})")

    @property
    def verify_label(self) -> str:
        return VERIFY_TYPE.get(self.verify_type, f"UNKNOWN({self.verify_type})")


@dataclass
class ADMSPayload:
    """Result of parsing a full ADMS POST body."""
    device_serial: str
    table:         str              # ATTLOG | OPERLOG | ATTPHOTO
    stamp:         str              # Watermark value (last-sent pointer)
    punches:       list[ParsedPunch] = field(default_factory=list)
    raw_body:      str = ""
    parse_errors:  list[str] = field(default_factory=list)


def _parse_timestamp(ts_str: str, device_tz_offset: float = 5.5) -> datetime:
    """
    Convert device-local timestamp to UTC.

    The device stores timestamps in its configured local time. We record
    everything as UTC and apply the offset here.
    """
    naive_dt = datetime.strptime(ts_str.strip(), "%Y-%m-%d %H:%M:%S")
    device_tz = timezone(timedelta(hours=device_tz_offset))
    local_dt = naive_dt.replace(tzinfo=device_tz)
    return local_dt.astimezone(timezone.utc)


def parse_adms_body(
    raw_body: str,
    query_params: dict,
    device_tz_offset: float = 5.5,
) -> ADMSPayload:
    """
    Parse a full ADMS POST request body.

    Parameters
    ----------
    raw_body        : The raw bytes decoded as UTF-8 from the request body.
    query_params    : Dict of URL query parameters (SN, table, Stamp, etc.).
    device_tz_offset: Device clock UTC offset in hours. IST = 5.5.

    Returns
    -------
    ADMSPayload with parsed punches (possibly empty on non-ATTLOG tables).
    """
    serial  = query_params.get("SN", "UNKNOWN").strip()
    table   = query_params.get("table", "").strip()
    stamp   = query_params.get("Stamp", "0").strip()

    payload = ADMSPayload(
        device_serial=serial,
        table=table,
        stamp=stamp,
        raw_body=raw_body,
    )

    if table != "ATTLOG":
        # OPERLOG / ATTPHOTO / heartbeat — nothing to parse for attendance
        logger.debug("Device %s sent table=%s (not ATTLOG), skipping punch parse", serial, table)
        return payload

    # Each line after the first metadata line is a punch record
    lines = raw_body.strip().splitlines()
    for line_no, line in enumerate(lines, start=1):
        line = line.strip()
        if not line:
            continue

        # L7 FIX: Only skip lines that start with key=value pairs (metadata header)
        # Check if the first field (before any tab) contains '='
        first_field = line.split("\t")[0] if "\t" in line else line
        if "=" in first_field and not first_field[0].isdigit():
            continue

        parts = line.split("\t")
        if len(parts) < 4:
            payload.parse_errors.append(
                f"Line {line_no}: expected >=4 tab-separated fields, got {len(parts)}: {line!r}"
            )
            continue

        try:
            punch = ParsedPunch(
                uid=parts[0].strip(),
                employee_id=parts[1].strip(),
                punch_time=_parse_timestamp(parts[2], device_tz_offset),
                status=int(parts[3].strip()),
                verify_type=int(parts[4].strip()) if len(parts) > 4 else 1,
                workcode=int(parts[5].strip()) if len(parts) > 5 else 0,
                raw_line=line,
            )
            payload.punches.append(punch)
        except (ValueError, IndexError) as exc:
            payload.parse_errors.append(f"Line {line_no}: {exc} -> {line!r}")
            logger.warning("ADMS parse error on device %s line %d: %s", serial, line_no, exc)

    if payload.parse_errors:
        logger.warning(
            "Device %s ATTLOG had %d parse errors (of %d lines)",
            serial, len(payload.parse_errors), len(lines)
        )

    return payload


def build_handshake_response(
    device_serial: str,
    trans_interval: int = 1,
    device_tz: float = 5.5,
) -> str:
    """
    Build the plain-text config response the device expects on GET /iclock/cdata.

    Critical fields
    ---------------
    ATTLOGStamp=9999  -> Send ALL buffered attendance logs on connect.
                        After you've done initial sync, set this to the last
                        Stamp value returned by the device so it only sends new records.
    Delay=N           -> Device pushes new punches within N seconds of the event.
    TransInterval=N   -> Bulk sync every N minutes.
    """
    tz_sign = "+" if device_tz >= 0 else "-"
    tz_hours = int(abs(device_tz))
    tz_mins  = int((abs(device_tz) % 1) * 60)
    tz_str   = f"{tz_sign}{tz_hours:02d}:{tz_mins:02d}"

    return (
        f"GET OPTION FROM: {device_serial}\r\n"
        f"ATTLOGStamp=9999\r\n"        # Fetch all logs; change to last Stamp after first sync
        f"OPERLOGStamp=9999\r\n"
        f"ATTPHOTOStamp=9999\r\n"
        f"ErrorDelay=30\r\n"           # Retry after 30s on error
        f"Delay=10\r\n"                # Push realtime events within 10 seconds
        f"TransTimes=00:00;12:00\r\n"  # Bulk sync at midnight + noon
        f"TransInterval={trans_interval}\r\n"
        f"TransFlag=TransData AttLog OpLog\r\n"
        f"TimeZone={tz_str}\r\n"
        f"Realtime=1\r\n"              # Enable real-time push mode
        f"Encrypt=None\r\n"
    )
