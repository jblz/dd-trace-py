import os
from typing import Any
from typing import Callable
from typing import Optional
from typing import Text

from ddtrace import tracer
from ddtrace.internal.logger import get_logger
from ddtrace.internal.utils.cache import LFUCache

from ..._deduplications import deduplication
from .._iast_request_context import get_iast_reporter
from .._iast_request_context import is_iast_request_enabled
from .._iast_request_context import set_iast_reporter
from .._overhead_control_engine import Operation
from .._stacktrace import get_info_frame
from .._utils import _is_iast_debug_enabled
from ..reporter import Evidence
from ..reporter import IastSpanReporter
from ..reporter import Location
from ..reporter import Vulnerability


log = get_logger(__name__)

CWD = os.path.abspath(os.getcwd())


class taint_sink_deduplication(deduplication):
    def _extract(self, args):
        # We skip positions 0 and 1 because they represent the 'cls' and 'span' respectively
        return args[2:]


def _check_positions_contained(needle, container):
    needle_start, needle_end = needle
    container_start, container_end = container

    return (
        (container_start <= needle_start < container_end)
        or (container_start < needle_end <= container_end)
        or (needle_start <= container_start < needle_end)
        or (needle_start < container_end <= needle_end)
    )


class VulnerabilityBase(Operation):
    vulnerability_type = ""
    _redacted_report_cache = LFUCache()

    @classmethod
    def _reset_cache_for_testing(cls):
        """Reset the redacted reports and deduplication cache. For testing purposes only."""
        cls._redacted_report_cache.clear()

    @classmethod
    def wrap(cls, func: Callable) -> Callable:
        def wrapper(wrapped: Callable, instance: Any, args: Any, kwargs: Any) -> Any:
            """Get the current root Span and attach it to the wrapped function. We need the span to report the
            vulnerability and update the context with the report information.
            """
            if not is_iast_request_enabled():
                log.debug(
                    "[IAST] VulnerabilityBase.wrapper. No request quota or this vulnerability is outside the context"
                )
                return wrapped(*args, **kwargs)
            elif cls.has_quota():
                return func(wrapped, instance, args, kwargs)
            else:
                return wrapped(*args, **kwargs)

        return wrapper

    @classmethod
    @taint_sink_deduplication
    def _prepare_report(cls, vulnerability_type, evidence, file_name, line_number):
        if not is_iast_request_enabled():
            if _is_iast_debug_enabled():
                log.debug(
                    "[IAST] VulnerabilityBase._prepare_report. "
                    "No request quota or this vulnerability is outside the context"
                )
            return False
        if line_number is not None and (line_number == 0 or line_number < -1):
            line_number = -1

        report = get_iast_reporter()
        span_id = 0
        if tracer and hasattr(tracer, "current_root_span"):
            span = tracer.current_root_span()
            if span:
                span_id = span.span_id

        vulnerability = Vulnerability(
            type=vulnerability_type,
            evidence=evidence,
            location=Location(path=file_name, line=line_number, spanId=span_id),
        )
        if report:
            report.vulnerabilities.add(vulnerability)
        else:
            report = IastSpanReporter(vulnerabilities={vulnerability})
        report.add_ranges_to_evidence_and_extract_sources(vulnerability)

        set_iast_reporter(report)

        return True

    @classmethod
    def report(cls, evidence_value: Text = "", dialect: Optional[Text] = None) -> None:
        """Build a IastSpanReporter instance to report it in the `AppSecIastSpanProcessor` as a string JSON"""
        # TODO: type of evidence_value will be Text. We wait to finish the redaction refactor.
        if cls.acquire_quota():
            file_name = None
            line_number = None

            skip_location = getattr(cls, "skip_location", False)
            if not skip_location:
                frame_info = get_info_frame(CWD)
                if not frame_info or frame_info[0] == "" or frame_info[0] == -1:
                    return None

                file_name, line_number = frame_info

                # Remove CWD prefix
                if file_name.startswith(CWD):
                    file_name = os.path.relpath(file_name, start=CWD)

                if not cls.is_not_reported(file_name, line_number):
                    return
            # Evidence is a string in weak cipher, weak hash and weak randomness
            if isinstance(evidence_value, (str, bytes, bytearray)):
                evidence = Evidence(value=evidence_value, dialect=dialect)
            else:
                log.debug("Unexpected evidence_value type: %s", type(evidence_value))
                evidence = Evidence(value="", dialect=dialect)

            result = cls._prepare_report(cls.vulnerability_type, evidence, file_name, line_number)
            # If result is None that's mean deduplication raises and no vulnerability wasn't reported, with that,
            # we need to restore the quota
            if not result:
                cls.increment_quota()
