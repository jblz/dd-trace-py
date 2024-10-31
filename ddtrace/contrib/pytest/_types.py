from ddtrace.contrib.pytest._utils import _get_pytest_version_tuple


if _get_pytest_version_tuple() >= (7, 0, 0):
    from pytest import CallInfo as pytest_CallInfo  # noqa: F401
    from pytest import Config as pytest_Config  # noqa: F401
    from pytest import TestReport as pytest_TestReport  # noqa: F401
else:
    from _pytest.config import Config as pytest_Config  # noqa: F401
    from _pytest.reports import TestReport as pytest_TestReport  # noqa: F401
    from _pytest.runner import CallInfo as pytest_CallInfo  # noqa: F401

if _get_pytest_version_tuple() >= (7, 4, 0):
    from pytest import TestShortLogReport as pytest_TestShortLogReport  # noqa: F401
else:
    from _pytest.reports import TestReport as pytest_TestShortLogReport  # noqa: F401