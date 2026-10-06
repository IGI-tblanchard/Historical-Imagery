"""Timestamped diagnostics reports for scheduled historical imagery runs."""

from __future__ import annotations

import csv
import getpass
import os
import platform
import socket
import sys
import traceback
from datetime import datetime
from pathlib import Path

FIELDS = (
    "timestamp", "script", "stage", "status", "issue_category", "client",
    "input_path", "output_path", "message", "details",
)


def classify_issue(text: str) -> str:
    value = text.casefold()
    if any(term in value for term in ("schema lock", "schema-lock", "000464", "locked by another", "cannot acquire a lock", "lock")):
        return "schema_lock"
    if any(term in value for term in ("permission denied", "access is denied", "unauthorized", "not have permission", "read-only", "winerror 5")):
        return "permissions"
    if any(term in value for term in ("modulenotfounderror", "no module named", "importerror", "license", "licensing", "failed to initialize", "not initialized")):
        return "environment_or_license"
    if any(term in value for term in ("network path", "network name", "drive not found", "system cannot find", "no such file", "not found", "does not exist", "unavailable")):
        return "path_or_network"
    if any(term in value for term in ("000732", "000210", "000464", "executeerror", "arcpy", "geoprocessing")):
        return "geoprocessing"
    if any(term in value for term in ("raster", "tiff", "tif", "extent", "coordinate system", "crs")):
        return "raster_or_data"
    return "other"


class HistoricalRunReport:
    def __init__(self, script_file: str | Path, report_dir: str | Path):
        script = Path(script_file).resolve()
        self.path = Path(report_dir) / f"{script.stem}_{datetime.now():%Y%m%d_%H%M%S}_{os.getpid()}_diagnostics.csv"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._handle = self.path.open("w", newline="", encoding="utf-8-sig")
        self._writer = csv.DictWriter(self._handle, fieldnames=FIELDS)
        self._writer.writeheader()
        self._handle.flush()
        self.issue_count = 0
        share_visible = Path("\\\\IGG-QNAP12\\IGG_Archive\\IGG\\Z_Drive").exists()
        self.record(
            "startup", "info", message="Run started; execution environment snapshot",
            details=(f"python={sys.version.replace(os.linesep, ' ')}; executable={sys.executable}; "
                     f"platform={platform.platform()}; host={socket.gethostname()}; "
                     f"user={getpass.getuser()}; cwd={Path.cwd()}; "
                     f"historical_unc_share_visible={share_visible}"),
        )

    def record(self, stage: str, status: str, *, client: str = "", input_path: str | Path = "",
               output_path: str | Path = "", message: str = "", details: str = "",
               issue_category: str = "") -> None:
        self._writer.writerow({
            "timestamp": datetime.now().astimezone().isoformat(timespec="seconds"),
            "script": Path(sys.argv[0]).name,
            "stage": stage,
            "status": status,
            "issue_category": issue_category or (classify_issue(message + " " + details) if status in {"failed", "warning"} else ""),
            "client": client,
            "input_path": str(input_path) if input_path else "",
            "output_path": str(output_path) if output_path else "",
            "message": message,
            "details": details,
        })
        self._handle.flush()
        if status in {"failed", "warning"}:
            self.issue_count += 1

    def exception(self, stage: str, error: BaseException, **context) -> None:
        extra = context.pop("details", "")
        details = "".join(traceback.format_exception(type(error), error, error.__traceback__))
        if extra:
            details += f"\nAdditional details:\n{extra}"
        self.record(stage, "failed", message=f"{type(error).__name__}: {error}",
                    details=details, issue_category=classify_issue(f"{error}\n{details}"), **context)

    def close(self) -> None:
        if not self._handle.closed:
            self._handle.close()
