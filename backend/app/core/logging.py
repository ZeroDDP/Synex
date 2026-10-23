from __future__ import annotations

import contextlib
import logging
import sys

import structlog


def setup_logging(debug: bool = False) -> None:
    """DEBUG=true: цветной вывод для разработки, уровень debug.

    DEBUG=false: JSON-строки, уровень info.
    """
    # На Windows stdout, перенаправленный в файл или пайп (например, когда backend
    # запускает UI), пишет в кодировке локали (cp1251), а эмодзи и часть символов
    # из вывода воркера в ней нет: print() упал бы с UnicodeEncodeError.
    for stream in (sys.stdout, sys.stderr):
        with contextlib.suppress(AttributeError, ValueError, OSError):
            stream.reconfigure(encoding="utf-8", errors="backslashreplace")

    processors: list = [
        structlog.contextvars.merge_contextvars,
        structlog.processors.add_log_level,
        structlog.processors.StackInfoRenderer(),
        structlog.processors.TimeStamper(fmt="iso"),
    ]
    if debug:
        processors.append(structlog.dev.ConsoleRenderer(colors=sys.stdout.isatty()))
    else:
        processors += [
            structlog.processors.format_exc_info,
            structlog.processors.JSONRenderer(ensure_ascii=False),
        ]

    structlog.configure(
        processors=processors,
        wrapper_class=structlog.make_filtering_bound_logger(
            logging.DEBUG if debug else logging.INFO
        ),
        context_class=dict,
        logger_factory=structlog.PrintLoggerFactory(),
        cache_logger_on_first_use=True,
    )
