"""JSON logs with request correlation and PII redaction."""

import contextvars
import json
import logging
import re
import sys
import time
from logging.handlers import RotatingFileHandler
from pathlib import Path

request_id_var: contextvars.ContextVar[str] = contextvars.ContextVar("request_id", default="-")

_EMAIL = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")
# Phone shapes only: optional country code, then 10 digits (98765 43210 / 9876543210). A loose "any 10+ digits"
# pattern also swallowed season lists like "2008 2009 2010" and broke cricket questions.
# Word boundaries on both sides: 10 digits inside a hex request id ("5b8765432109edd5") are not a phone number.
_PHONE = re.compile(r"(?<![\w.])(?:\+\d{1,3}[ -]?)?\d{5}[ -]?\d{5}(?![\w.])")
_BEARER = re.compile(r"(?i)(bearer\s+)[\w.\-]+")


# Credentials: key=value pairs, well-known key shapes, and the app's own secrets registered at startup.
_KV_SECRET = re.compile(r"(?i)\b(api[_-]?key|access[_-]?key|secret|password|passwd|pwd|token|authorization|x-admin-token|"
                        r"session[_-]?secret|admin[_-]?token|client[_-]?secret)(\s*[:=]\s*)([\"']?)(?!bearer\b)[^\s\"',;}]{4,}")
_KEY_SHAPES = re.compile(r"\b(sk-[A-Za-z0-9_-]{16,}|AKIA[0-9A-Z]{16}|ghp_[A-Za-z0-9]{20,}|xox[abp]-[A-Za-z0-9-]{10,}|"
                         r"eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{5,})\b")
_registered: set[str] = set()


def register_secret(value: str | None) -> None:
    """Exact values (admin token, session secret) that must never appear in any log, CSV or dashboard."""
    if value and len(value) >= 8:
        _registered.add(value)


def redact_secrets(text: str) -> str:
    text = _BEARER.sub(r"\1[token]", text)
    for v in _registered:
        if v in text:
            text = text.replace(v, "[secret]")
    text = _KV_SECRET.sub(lambda m: f"{m.group(1)}{m.group(2)}{m.group(3)}[secret]", text)
    return _KEY_SHAPES.sub("[secret]", text)


def redact(text: str) -> str:
    text = redact_secrets(text)
    text = _EMAIL.sub("[email]", text)
    text = _PHONE.sub("[phone]", text)
    return _BEARER.sub(r"\1[token]", text)


class JsonFormatter(logging.Formatter):
    _skip = set(logging.LogRecord("", 0, "", 0, "", (), None).__dict__) | {"message", "asctime"}

    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(record.created)) + f".{int(record.msecs):03d}Z",
            "level": record.levelname,
            "logger": record.name,
            "msg": redact(record.getMessage()),
            "request_id": request_id_var.get(),
        }
        for key, value in record.__dict__.items():
            if key not in self._skip:
                payload[key] = redact(value) if isinstance(value, str) else value
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        return json.dumps(payload, default=str)


def setup_logging(level: str = "INFO", log_file: Path | None = None) -> None:
    """JSON lines to stdout and, if given, to a rotating file (10 MB x 5), however the app was started."""
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter())
    handlers: list[logging.Handler] = [handler]
    if log_file:
        log_file.parent.mkdir(parents=True, exist_ok=True)
        fh = RotatingFileHandler(log_file, maxBytes=10_000_000, backupCount=5, encoding="utf-8")
        fh.setFormatter(JsonFormatter())
        handlers.append(fh)
    root = logging.getLogger()
    root.handlers[:] = handlers
    root.setLevel(level)
    for noisy in ("httpx", "httpcore", "chromadb", "uvicorn.access"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
