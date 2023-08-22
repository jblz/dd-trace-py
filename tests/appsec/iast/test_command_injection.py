from copy import copy
import os
import subprocess
import sys

import pytest

from ddtrace import Pin
from ddtrace.appsec._constants import IAST
from ddtrace.appsec.iast import oce
from ddtrace.appsec.iast.constants import VULN_CMDI
from ddtrace.contrib.subprocess.patch import SubprocessCmdLine
from ddtrace.contrib.subprocess.patch import patch
from ddtrace.contrib.subprocess.patch import unpatch
from ddtrace.internal import core
from tests.appsec.iast.iast_utils import get_line_and_hash
from tests.utils import override_config
from tests.utils import override_global_config


try:
    from ddtrace.appsec.iast._taint_tracking import OriginType  # noqa: F401
    from ddtrace.appsec.iast._taint_tracking import is_pyobject_tainted
    from ddtrace.appsec.iast._taint_tracking import setup as taint_tracking_setup
    from ddtrace.appsec.iast._taint_tracking import taint_pyobject
    from ddtrace.appsec.iast._taint_tracking.aspects import add_aspect
except (ImportError, AttributeError):
    pytest.skip("IAST not supported for this Python version", allow_module_level=True)

FIXTURES_PATH = "test_command_injection.py"
_PARAMS = ["/bin/ls", "-l"]


@pytest.fixture(autouse=True)
def auto_unpatch():
    SubprocessCmdLine._clear_cache()
    yield
    SubprocessCmdLine._clear_cache()
    try:
        unpatch()
    except AttributeError:
        # Tests with appsec disabled or that didn't patch
        pass


def setup():
    oce._enabled = True
    taint_tracking_setup(bytes.join, bytearray.join)


def test_ossystem(tracer, iast_span_defaults):
    with override_global_config(dict(_appsec_enabled=True, _iast_enabled=True)):
        patch()
        Pin.get_from(os).clone(tracer=tracer).onto(os)
        _BAD_DIR = "forbidden_dir/"
        _BAD_DIR = taint_pyobject(
            pyobject=_BAD_DIR,
            source_name="test_ossystem",
            source_value=_BAD_DIR,
            source_origin=OriginType.PARAMETER,
        )
        assert is_pyobject_tainted(_BAD_DIR)
        with tracer.trace("ossystem_test"):
            # label test_ossystem
            os.system(add_aspect("dir -l ", _BAD_DIR))

        span_report = core.get_item(IAST.CONTEXT_KEY, span=iast_span_defaults)
        assert span_report

        vulnerability = list(span_report.vulnerabilities)[0]
        source = list(span_report.sources)[0]
        assert vulnerability.type == VULN_CMDI
        assert vulnerability.evidence.valueParts == [{"value": "dir -l "}, {"source": 0, "value": _BAD_DIR}]
        assert vulnerability.evidence.value is None
        assert vulnerability.evidence.pattern is None
        assert vulnerability.evidence.redacted is None
        assert source.name == "test_ossystem"
        assert source.origin == OriginType.PARAMETER
        assert source.value == _BAD_DIR

        line, hash_value = get_line_and_hash("test_ossystem", VULN_CMDI, filename=FIXTURES_PATH)
        assert vulnerability.location.path == FIXTURES_PATH
        assert vulnerability.location.line == line
        assert vulnerability.hash == hash_value


def test_ossystem_redacted(tracer, iast_span_defaults):
    with override_global_config(dict(_appsec_enabled=True, _iast_enabled=True)), override_config(
        "subprocess", dict(sensitive_wildcards=["*custom_scrub*", "*myscrub*"])
    ):
        _BAD_DIR = "forbidden_dir/"
        _BAD_DIR = taint_pyobject(
            pyobject=_BAD_DIR,
            source_name="test_ossystem",
            source_value=_BAD_DIR,
            source_origin=OriginType.PARAMETER,
        )
        assert is_pyobject_tainted(_BAD_DIR)
        with tracer.trace("ossystem_test"):
            # label test_ossystem_redacted
            SubprocessCmdLine(
                [
                    "binary",
                    "-passwd",
                    "SCRUB",
                    "-d bar",
                    _BAD_DIR,
                    "--custom_scrubed",
                    "SCRUB",
                    "--foomyscrub",
                    "SCRUB",
                ],
                shell=False,
            )

        span_report = core.get_item(IAST.CONTEXT_KEY, span=iast_span_defaults)
        assert span_report

        vulnerability = list(span_report.vulnerabilities)[0]
        source = list(span_report.sources)[0]
        assert vulnerability.type == VULN_CMDI
        assert vulnerability.evidence.valueParts == [
            {"value": "binary -passwd SCRUB -d bar "},
            {"source": 0, "value": _BAD_DIR},
            {"value": " --custom_scrubed SCRUB --foomyscrub SCRUB"},
        ]
        assert vulnerability.evidence.value is None
        assert vulnerability.evidence.pattern is None
        assert vulnerability.evidence.redacted is None
        assert source.name == "test_ossystem"
        assert source.origin == OriginType.PARAMETER
        assert source.value == _BAD_DIR

        line, hash_value = get_line_and_hash("test_ossystem_redacted", VULN_CMDI, filename=FIXTURES_PATH)
        assert vulnerability.location.path == FIXTURES_PATH
        assert vulnerability.location.line == line
        assert vulnerability.hash == hash_value


def test_communicate(tracer, iast_span_defaults):
    with override_global_config(dict(_appsec_enabled=True, _iast_enabled=True)):
        patch()
        Pin.get_from(os).clone(tracer=tracer).onto(os)
        _BAD_DIR = "forbidden_dir/"
        _BAD_DIR = taint_pyobject(
            pyobject=_BAD_DIR,
            source_name="test_communicate",
            source_value=_BAD_DIR,
            source_origin=OriginType.PARAMETER,
        )
        with tracer.trace("communicate_test"):
            # label test_communicate
            subp = subprocess.Popen(args=["dir", "-l", _BAD_DIR])
            subp.communicate()
            subp.wait()

        span_report = core.get_item(IAST.CONTEXT_KEY, span=iast_span_defaults)
        assert span_report

        vulnerability = list(span_report.vulnerabilities)[0]
        source = list(span_report.sources)[0]
        assert vulnerability.type == VULN_CMDI
        assert vulnerability.evidence.valueParts == [{"value": "dir -l "}, {"source": 0, "value": _BAD_DIR}]
        assert vulnerability.evidence.value is None
        assert vulnerability.evidence.pattern is None
        assert vulnerability.evidence.redacted is None
        assert source.name == "test_communicate"
        assert source.origin == OriginType.PARAMETER
        assert source.value == _BAD_DIR

        line, hash_value = get_line_and_hash("test_communicate", VULN_CMDI, filename=FIXTURES_PATH)
        assert vulnerability.location.path == FIXTURES_PATH
        assert vulnerability.location.line == line
        assert vulnerability.hash == hash_value


def test_run(tracer, iast_span_defaults):
    with override_global_config(dict(_appsec_enabled=True, _iast_enabled=True)):
        patch()
        Pin.get_from(os).clone(tracer=tracer).onto(os)
        _BAD_DIR = "forbidden_dir/"
        _BAD_DIR = taint_pyobject(
            pyobject=_BAD_DIR,
            source_name="test_run",
            source_value=_BAD_DIR,
            source_origin=OriginType.PARAMETER,
        )
        with tracer.trace("communicate_test"):
            # label test_run
            subprocess.run(["dir", "-l", _BAD_DIR])

        span_report = core.get_item(IAST.CONTEXT_KEY, span=iast_span_defaults)
        assert span_report

        vulnerability = list(span_report.vulnerabilities)[0]
        source = list(span_report.sources)[0]
        assert vulnerability.type == VULN_CMDI
        assert vulnerability.evidence.valueParts == [{"value": "dir -l "}, {"source": 0, "value": _BAD_DIR}]
        assert vulnerability.evidence.value is None
        assert vulnerability.evidence.pattern is None
        assert vulnerability.evidence.redacted is None
        assert source.name == "test_run"
        assert source.origin == OriginType.PARAMETER
        assert source.value == _BAD_DIR

        line, hash_value = get_line_and_hash("test_run", VULN_CMDI, filename=FIXTURES_PATH)
        assert vulnerability.location.path == FIXTURES_PATH
        assert vulnerability.location.line == line
        assert vulnerability.hash == hash_value


def test_popen_wait(tracer, iast_span_defaults):
    with override_global_config(dict(_appsec_enabled=True, _iast_enabled=True)):
        patch()
        Pin.get_from(os).clone(tracer=tracer).onto(os)
        _BAD_DIR = "forbidden_dir/"
        _BAD_DIR = taint_pyobject(
            pyobject=_BAD_DIR,
            source_name="test_popen_wait",
            source_value=_BAD_DIR,
            source_origin=OriginType.PARAMETER,
        )
        with tracer.trace("communicate_test"):
            # label test_popen_wait
            subp = subprocess.Popen(args=["dir", "-l", _BAD_DIR])
            subp.wait()

        span_report = core.get_item(IAST.CONTEXT_KEY, span=iast_span_defaults)
        assert span_report

        vulnerability = list(span_report.vulnerabilities)[0]
        source = list(span_report.sources)[0]
        assert vulnerability.type == VULN_CMDI
        assert vulnerability.evidence.valueParts == [{"value": "dir -l "}, {"source": 0, "value": _BAD_DIR}]
        assert vulnerability.evidence.value is None
        assert vulnerability.evidence.pattern is None
        assert vulnerability.evidence.redacted is None
        assert source.name == "test_popen_wait"
        assert source.origin == OriginType.PARAMETER
        assert source.value == _BAD_DIR

        line, hash_value = get_line_and_hash("test_popen_wait", VULN_CMDI, filename=FIXTURES_PATH)
        assert vulnerability.location.path == FIXTURES_PATH
        assert vulnerability.location.line == line
        assert vulnerability.hash == hash_value


def test_popen_wait_shell_true(tracer, iast_span_defaults):
    with override_global_config(dict(_appsec_enabled=True, _iast_enabled=True)):
        patch()
        Pin.get_from(os).clone(tracer=tracer).onto(os)
        _BAD_DIR = "forbidden_dir/"
        _BAD_DIR = taint_pyobject(
            pyobject=_BAD_DIR,
            source_name="test_popen_wait_shell_true",
            source_value=_BAD_DIR,
            source_origin=OriginType.PARAMETER,
        )
        with tracer.trace("communicate_test"):
            # label test_popen_wait_shell_true
            subp = subprocess.Popen(args=["dir", "-l", _BAD_DIR], shell=True)
            subp.wait()

        span_report = core.get_item(IAST.CONTEXT_KEY, span=iast_span_defaults)
        assert span_report

        vulnerability = list(span_report.vulnerabilities)[0]
        source = list(span_report.sources)[0]
        assert vulnerability.type == VULN_CMDI
        assert vulnerability.evidence.valueParts == [{"value": "dir -l "}, {"source": 0, "value": _BAD_DIR}]
        assert vulnerability.evidence.value is None
        assert vulnerability.evidence.pattern is None
        assert vulnerability.evidence.redacted is None
        assert source.name == "test_popen_wait_shell_true"
        assert source.origin == OriginType.PARAMETER
        assert source.value == _BAD_DIR

        line, hash_value = get_line_and_hash("test_popen_wait_shell_true", VULN_CMDI, filename=FIXTURES_PATH)
        assert vulnerability.location.path == FIXTURES_PATH
        assert vulnerability.location.line == line
        assert vulnerability.hash == hash_value


@pytest.mark.skipif(sys.platform != "linux", reason="Only for Linux")
@pytest.mark.parametrize(
    "function,mode,arguments, tag",
    [
        (os.spawnl, os.P_WAIT, _PARAMS, "test_osspawn_variants1"),
        (os.spawnl, os.P_NOWAIT, _PARAMS, "test_osspawn_variants1"),
        (os.spawnlp, os.P_WAIT, _PARAMS, "test_osspawn_variants1"),
        (os.spawnlp, os.P_NOWAIT, _PARAMS, "test_osspawn_variants1"),
        (os.spawnv, os.P_WAIT, _PARAMS, "test_osspawn_variants2"),
        (os.spawnv, os.P_NOWAIT, _PARAMS, "test_osspawn_variants2"),
        (os.spawnvp, os.P_WAIT, _PARAMS, "test_osspawn_variants2"),
        (os.spawnvp, os.P_NOWAIT, _PARAMS, "test_osspawn_variants2"),
    ],
)
def test_osspawn_variants(tracer, iast_span_defaults, function, mode, arguments, tag):
    with override_global_config(dict(_appsec_enabled=True, _iast_enabled=True)):
        patch()
        Pin.get_from(os).clone(tracer=tracer).onto(os)
        _BAD_DIR = "forbidden_dir/"
        _BAD_DIR = taint_pyobject(
            pyobject=_BAD_DIR,
            source_name="test_osspawn_variants",
            source_value=_BAD_DIR,
            source_origin=OriginType.PARAMETER,
        )
        copied_args = copy(arguments)
        copied_args.append(_BAD_DIR)

        if "_" in function.__name__:
            # wrapt changes function names when debugging
            cleaned_name = function.__name__.split("_")[-1]
        else:
            cleaned_name = function.__name__

        with tracer.trace("osspawn_test"):
            if "spawnv" in cleaned_name:
                # label test_osspawn_variants2
                function(mode, copied_args[0], copied_args)
            else:
                # label test_osspawn_variants1
                function(mode, copied_args[0], *copied_args)

        span_report = core.get_item(IAST.CONTEXT_KEY, span=iast_span_defaults)
        assert span_report

        vulnerability = list(span_report.vulnerabilities)[0]
        source = list(span_report.sources)[0]
        assert vulnerability.type == VULN_CMDI
        assert vulnerability.evidence.valueParts == [{"value": "/bin/ls -l "}, {"source": 0, "value": _BAD_DIR}]
        assert vulnerability.evidence.value is None
        assert vulnerability.evidence.pattern is None
        assert vulnerability.evidence.redacted is None
        assert source.name == "test_osspawn_variants"
        assert source.origin == OriginType.PARAMETER
        assert source.value == _BAD_DIR

        line, hash_value = get_line_and_hash(tag, VULN_CMDI, filename=FIXTURES_PATH)
        assert vulnerability.location.path == FIXTURES_PATH
        assert vulnerability.location.line == line
        assert vulnerability.hash == hash_value
