"""
Centralised logging for the API Maintenance Agent.

Usage:
    from app.logger import get_logger
    log = get_logger(__name__)

    log.info("Processing started")
    log.info("Processing started", extra={"job_id": job_id})
    log.error("Something failed", extra={"job_id": job_id, "error": str(e)})

Output format:
    2026-05-19 10:00:00,123 | INFO     | [app.services.parser][parse_extent_report] Processing started
    2026-05-19 10:00:00,124 | ERROR    | [app.tasks.huey_tasks][task_parse_report] Something failed | job_id=abc error=oops
"""
import logging
import sys
from typing import Optional


class AgentFormatter(logging.Formatter):
    """
    Custom formatter that produces:
        TIMESTAMP | LEVEL    | [module][func] message | key=val key=val
    """

    LEVEL_COLORS = {
        "DEBUG":    "\033[36m",   # cyan
        "INFO":     "\033[32m",   # green
        "WARNING":  "\033[33m",   # yellow
        "ERROR":    "\033[31m",   # red
        "CRITICAL": "\033[35m",   # magenta
    }
    RESET = "\033[0m"

    def __init__(self, colorize: bool = True):
        super().__init__()
        self.colorize = colorize

    def format(self, record: logging.LogRecord) -> str:
        timestamp = self.formatTime(record, "%Y-%m-%d %H:%M:%S")
        level = f"{record.levelname:<8}"
        location = f"[{record.module}][{record.funcName}]"

        message = record.getMessage()

        # Collect any extra fields that aren't standard LogRecord attributes
        standard_keys = {
            "name", "msg", "args", "levelname", "levelno", "pathname",
            "filename", "module", "exc_info", "exc_text", "stack_info",
            "lineno", "funcName", "created", "msecs", "relativeCreated",
            "thread", "threadName", "processName", "process", "message",
            "taskName",
        }
        extras = {
            k: v for k, v in record.__dict__.items()
            if k not in standard_keys and not k.startswith("_")
        }

        extra_str = ""
        if extras:
            extra_str = " | " + "  ".join(f"{k}={v}" for k, v in extras.items())

        exc_str = ""
        if record.exc_info:
            exc_str = "\n" + self.formatException(record.exc_info)

        line = f"{timestamp} | {level} | {location} {message}{extra_str}{exc_str}"

        if self.colorize and sys.stderr.isatty():
            color = self.LEVEL_COLORS.get(record.levelname, "")
            line = f"{color}{line}{self.RESET}"

        return line


def _build_handler(colorize: bool = True) -> logging.StreamHandler:
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(AgentFormatter(colorize=colorize))
    return handler


# Root logger for the project — all child loggers inherit this handler
_root = logging.getLogger("app")
if not _root.handlers:
    _root.addHandler(_build_handler())
    _root.setLevel(logging.DEBUG)
    _root.propagate = False  # don't double-print via the root Python logger


def get_logger(name: str) -> logging.Logger:
    """
    Return a child logger under the 'app' namespace.

    Pass __name__ as the argument so the module path is captured automatically:
        log = get_logger(__name__)
    """
    return logging.getLogger(name)
