from __future__ import annotations

import logging


def test_logger_redacts_known_key_shapes(caplog):
    from app.utils.logger import get_logger

    log = get_logger("researchos.test.redact")
    # caplog's propagation handler does not include our filter; so instead
    # we verify that the formatted output from our configured root handler
    # redacts. Use the root handler's filters directly.
    root = logging.getLogger()
    assert root.handlers, "logger_module should have configured a handler"
    handler = root.handlers[0]

    class _Collector(logging.Handler):
        def __init__(self) -> None:
            super().__init__()
            for f in handler.filters:
                self.addFilter(f)
            self.records: list[str] = []

        def emit(self, record):
            self.records.append(self.format(record))

    collector = _Collector()
    collector.setFormatter(handler.formatter)
    log.addHandler(collector)
    log.info("leaking key sk-abcdefghijklmnopqrstuvwxyz1234567890")
    log.info("leaking ant sk-ant-ABCDEFGHIJKLMNOPQRSTUVWXYZ1234567890")
    log.info("leaking bearer Authorization: Bearer tok_abcdefghijklmnopqrstuv")
    log.removeHandler(collector)

    text = "\n".join(collector.records)
    assert "sk-abcdefghijklmnopqrstuvwxyz1234567890" not in text
    assert "sk-ant-ABCDEFGHIJKLMNOPQRSTUVWXYZ1234567890" not in text
    assert "tok_abcdefghijklmnopqrstuv" not in text
    assert "REDACTED" in text
