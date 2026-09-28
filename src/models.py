from dataclasses import dataclass
from enum import Enum
from typing import Optional
from datetime import datetime


class SetupStatus(str, Enum):
    SUCCESS = "SUCCESS"
    SAVED_ONLY = "SUCCESS_SAVED_ONLY"
    FAILED = "FAILED"


@dataclass
class DVR:
    atmid: str
    ip: str
    port: int = 81
    username: str = "admin"
    password: str = "trans@123"
    name: Optional[str] = None


@dataclass
class SetupResult:
    atmid: str
    ip: str
    port: int
    model: str
    status: SetupStatus
    remark: str
    duration_ms: int
    timestamp: datetime