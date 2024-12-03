"""
This custom pytest plugin implements tracing for pytest by using pytest hooks. The plugin registers tracing code
to be run at specific points during pytest execution. The most important hooks used are:

    * pytest_sessionstart: during pytest session startup, a custom trace filter is configured to the global tracer to
        only send test spans, which are generated by the plugin.
    * pytest_runtest_protocol: this wraps around the execution of a pytest test function, which we trace. Most span
        tags are generated and added in this function. We also store the span on the underlying pytest test item to
        retrieve later when we need to report test status/result.
    * pytest_runtest_makereport: this hook is used to set the test status/result tag, including skipped tests and
        expected failures.

"""
from doctest import DocTest
import json
import os
from pathlib import Path
import re
from typing import Dict  # noqa:F401

from _pytest.nodes import get_fslocation_from_item
import pytest

import ddtrace
from ddtrace import DDTraceDeprecationWarning
from ddtrace.constants import SPAN_KIND
from ddtrace.contrib.internal.coverage.data import _coverage_data
from ddtrace.contrib.internal.coverage.patch import patch as patch_coverage
from ddtrace.contrib.internal.coverage.patch import run_coverage_report
from ddtrace.contrib.internal.coverage.patch import unpatch as unpatch_coverage
from ddtrace.contrib.internal.coverage.utils import _is_coverage_invoked_by_coverage_run
from ddtrace.contrib.internal.coverage.utils import _is_coverage_patched
from ddtrace.contrib.pytest._utils import _extract_span
from ddtrace.contrib.pytest._utils import _is_enabled_early
from ddtrace.contrib.pytest._utils import _is_pytest_8_or_later
from ddtrace.contrib.pytest._utils import _is_test_unskippable
from ddtrace.contrib.pytest.constants import FRAMEWORK
from ddtrace.contrib.pytest.constants import KIND
from ddtrace.contrib.pytest.constants import XFAIL_REASON
from ddtrace.contrib.pytest.plugin import is_enabled
from ddtrace.contrib.unittest import unpatch as unpatch_unittest
from ddtrace.ext import SpanTypes
from ddtrace.ext import test
from ddtrace.internal.ci_visibility import CIVisibility as _CIVisibility
from ddtrace.internal.ci_visibility.constants import EVENT_TYPE as _EVENT_TYPE
from ddtrace.internal.ci_visibility.constants import ITR_CORRELATION_ID_TAG_NAME
from ddtrace.internal.ci_visibility.constants import MODULE_ID as _MODULE_ID
from ddtrace.internal.ci_visibility.constants import MODULE_TYPE as _MODULE_TYPE
from ddtrace.internal.ci_visibility.constants import SESSION_ID as _SESSION_ID
from ddtrace.internal.ci_visibility.constants import SESSION_TYPE as _SESSION_TYPE
from ddtrace.internal.ci_visibility.constants import SKIPPED_BY_ITR_REASON
from ddtrace.internal.ci_visibility.constants import SUITE
from ddtrace.internal.ci_visibility.constants import SUITE_ID as _SUITE_ID
from ddtrace.internal.ci_visibility.constants import SUITE_TYPE as _SUITE_TYPE
from ddtrace.internal.ci_visibility.constants import TEST
from ddtrace.internal.ci_visibility.coverage import USE_DD_COVERAGE
from ddtrace.internal.ci_visibility.coverage import _module_has_dd_coverage_enabled
from ddtrace.internal.ci_visibility.coverage import _report_coverage_to_span
from ddtrace.internal.ci_visibility.coverage import _start_coverage
from ddtrace.internal.ci_visibility.coverage import _stop_coverage
from ddtrace.internal.ci_visibility.coverage import _switch_coverage_context
from ddtrace.internal.ci_visibility.telemetry.constants import TEST_FRAMEWORKS
from ddtrace.internal.ci_visibility.utils import _add_pct_covered_to_span
from ddtrace.internal.ci_visibility.utils import _add_start_end_source_file_path_data_to_span
from ddtrace.internal.ci_visibility.utils import _generate_fully_qualified_module_name
from ddtrace.internal.ci_visibility.utils import _generate_fully_qualified_test_name
from ddtrace.internal.ci_visibility.utils import get_relative_or_absolute_path_for_path
from ddtrace.internal.ci_visibility.utils import take_over_logger_stream_handler
from ddtrace.internal.constants import COMPONENT
from ddtrace.internal.coverage.code import ModuleCodeCollector
from ddtrace.internal.logger import get_logger
from ddtrace.internal.utils.formats import asbool
from ddtrace.internal.utils.inspection import undecorated
from ddtrace.vendor.debtcollector import deprecate


log = get_logger(__name__)

_global_skipped_elements = 0

# COVER_SESSION is an experimental feature flag that provides full coverage (similar to coverage run), and is an
# experimental feature. It currently significantly increases test import time and should not be used.
COVER_SESSION = asbool(os.environ.get("_DD_COVER_SESSION", "false"))


def encode_test_parameter(parameter):
    param_repr = repr(parameter)
    # if the representation includes an id() we'll remove it
    # because it isn't constant across executions
    return re.sub(r" at 0[xX][0-9a-fA-F]+", "", param_repr)


def _is_pytest_cov_enabled(config) -> bool:
    if not config.pluginmanager.get_plugin("pytest_cov"):
        return False
    cov_option = config.getoption("--cov", default=False)
    nocov_option = config.getoption("--no-cov", default=False)
    if nocov_option is True:
        return False
    if isinstance(cov_option, list) and cov_option == [True] and not nocov_option:
        return True
    return cov_option


def _store_span(item, span):
    """Store span at `pytest.Item` instance."""
    item._datadog_span = span


def _extract_module_span(item):
    """Extract span from `pytest.Item` instance."""
    return getattr(item, "_datadog_span_module", None)


def _extract_ancestor_module_span(item):
    """Return the first ancestor module span found"""
    while item:
        module_span = _extract_module_span(item) or _extract_span(item)
        if module_span is not None and module_span.name == "pytest.test_module":
            return module_span
        item = _get_parent(item)


def _extract_ancestor_suite_span(item):
    """Return the first ancestor suite span found"""
    while item:
        suite_span = _extract_span(item)
        if suite_span is not None and suite_span.name == "pytest.test_suite":
            return suite_span
        item = _get_parent(item)


def _store_module_span(item, span):
    """Store span at `pytest.Item` instance."""
    item._datadog_span_module = span


def _mark_failed(item):
    """Store test failed status at `pytest.Item` instance."""
    item_parent = _get_parent(item)
    if item_parent:
        _mark_failed(item_parent)
    item._failed = True


def _check_failed(item):
    """Extract test failed status from `pytest.Item` instance."""
    return getattr(item, "_failed", False)


def _mark_not_skipped(item):
    """Mark test suite/module/session `pytest.Item` as not skipped."""
    item_parent = _get_parent(item)
    if item_parent:
        _mark_not_skipped(item_parent)
    item._fully_skipped = False


def _mark_not_skipped(item):
    """Mark test suite/module/session `pytest.Item` as not skipped."""

    item_parent = _get_parent(item)

    if item_parent:
        _mark_not_skipped(item_parent)
    item._fully_skipped = False


def _get_parent(item):
    """Fetches the nearest parent that is not a directory.

    This is introduced as a workaround for pytest 8.0's introduction pytest.Dir objects.
    """
    if item is None or item.parent is None:
        return None

    if _is_pytest_8_or_later():
        # In pytest 8.0, the parent of a Package can be another Package. In previous versions, the parent was always
        # a session.
        if isinstance(item, pytest.Package):
            while item.parent is not None and not isinstance(item.parent, pytest.Session):
                item = item.parent
            return item.parent

        while item.parent is not None and isinstance(item.parent, pytest.Dir):
            item = item.parent

    return item.parent


def _mark_test_forced(test_item):
    # type: (pytest.Test) -> None
    test_span = _extract_span(test_item)
    test_span.set_tag_str(test.ITR_FORCED_RUN, "true")

    suite_span = _extract_ancestor_suite_span(test_item)
    suite_span.set_tag_str(test.ITR_FORCED_RUN, "true")

    module_span = _extract_ancestor_module_span(test_item)
    module_span.set_tag_str(test.ITR_FORCED_RUN, "true")

    session_span = _extract_span(test_item.session)
    session_span.set_tag_str(test.ITR_FORCED_RUN, "true")


def _mark_test_unskippable(test_item):
    # type: (pytest.Test) -> None
    test_span = _extract_span(test_item)
    test_span.set_tag_str(test.ITR_UNSKIPPABLE, "true")

    suite_span = _extract_ancestor_suite_span(test_item)
    suite_span.set_tag_str(test.ITR_UNSKIPPABLE, "true")

    module_span = _extract_ancestor_module_span(test_item)
    module_span.set_tag_str(test.ITR_UNSKIPPABLE, "true")

    session_span = _extract_span(test_item.session)
    session_span.set_tag_str(test.ITR_UNSKIPPABLE, "true")


def _check_fully_skipped(item):
    """Check if test suite/module/session `pytest.Item` has `_fully_skipped` marker."""
    return getattr(item, "_fully_skipped", True)


def _mark_test_status(item, span):
    """
    Given a `pytest.Item`, determine and set the test status of the corresponding span.
    """
    item_parent = _get_parent(item)

    # If any child has failed, mark span as failed.
    if _check_failed(item):
        status = test.Status.FAIL.value
        if item_parent:
            _mark_failed(item_parent)
            _mark_not_skipped(item_parent)
    # If all children have been skipped, mark span as skipped.
    elif _check_fully_skipped(item):
        status = test.Status.SKIP.value
    else:
        status = test.Status.PASS.value
        if item_parent:
            _mark_not_skipped(item_parent)
    span.set_tag_str(test.STATUS, status)


def _extract_reason(call):
    if call.excinfo is not None:
        return call.excinfo.value


def _get_pytest_command(config):
    """Extract and re-create pytest session command from pytest config."""
    command = "pytest"
    if getattr(config, "invocation_params", None):
        command += " {}".format(" ".join(config.invocation_params.args))
    return command


def _get_module_path(item):
    """Extract module path from a `pytest.Item` instance."""
    # type (pytest.Item) -> str
    if not isinstance(item, (pytest.Package, pytest.Module)):
        return None

    if _is_pytest_8_or_later() and isinstance(item, pytest.Package):
        module_path = item.nodeid

    else:
        module_path = item.nodeid.rpartition("/")[0]

    return module_path


def _module_is_package(pytest_package_item=None, pytest_module_item=None):
    # Pytest 8+ module items have a pytest.Dir object as their parent instead of the session object
    if _is_pytest_8_or_later():
        return isinstance(pytest_module_item.parent, pytest.Package)

    if pytest_package_item is None and pytest_module_item is not None:
        return False
    return True


def _start_test_module_span(item):
    """
    Starts a test module span at the start of a new pytest test package.
    Note that ``item`` is a ``pytest.Item`` object referencing the test being run.
    """
    pytest_module_item = _find_pytest_item(item, pytest.Module)
    pytest_package_item = _find_pytest_item(pytest_module_item, pytest.Package)

    is_package = _module_is_package(pytest_package_item, pytest_module_item)

    if is_package:
        span_target_item = pytest_package_item
    else:
        span_target_item = pytest_module_item

    test_session_span = _extract_span(item.session)
    test_module_span = _CIVisibility._instance.tracer._start_span(
        "pytest.test_module",
        service=_CIVisibility._instance._service,
        span_type=SpanTypes.TEST,
        activate=True,
        child_of=test_session_span,
    )
    test_module_span.set_tag_str(COMPONENT, "pytest")
    test_module_span.set_tag_str(SPAN_KIND, KIND)
    test_module_span.set_tag_str(test.FRAMEWORK, FRAMEWORK)
    test_module_span.set_tag_str(test.FRAMEWORK_VERSION, pytest.__version__)
    test_module_span.set_tag_str(test.COMMAND, _get_pytest_command(item.config))
    test_module_span.set_tag_str(_EVENT_TYPE, _MODULE_TYPE)
    if test_session_span:
        test_module_span.set_tag_str(_SESSION_ID, str(test_session_span.span_id))
    test_module_span.set_tag_str(_MODULE_ID, str(test_module_span.span_id))
    test_module_span.set_tag_str(test.MODULE, item.config.hook.pytest_ddtrace_get_item_module_name(item=item))
    test_module_span.set_tag_str(test.MODULE_PATH, _get_module_path(span_target_item))
    if is_package:
        _store_span(span_target_item, test_module_span)
    else:
        _store_module_span(span_target_item, test_module_span)

    test_module_span.set_tag_str(
        test.ITR_TEST_CODE_COVERAGE_ENABLED,
        "true" if _CIVisibility._instance._collect_coverage_enabled else "false",
    )

    if _CIVisibility.test_skipping_enabled():
        test_module_span.set_tag_str(test.ITR_TEST_SKIPPING_ENABLED, "true")
        test_module_span.set_tag(
            test.ITR_TEST_SKIPPING_TYPE, SUITE if _CIVisibility._instance._suite_skipping_mode else TEST
        )
        test_module_span.set_tag_str(test.ITR_TEST_SKIPPING_TESTS_SKIPPED, "false")
        test_module_span.set_tag_str(test.ITR_DD_CI_ITR_TESTS_SKIPPED, "false")
        test_module_span.set_tag_str(test.ITR_FORCED_RUN, "false")
        test_module_span.set_tag_str(test.ITR_UNSKIPPABLE, "false")
    else:
        test_module_span.set_tag(test.ITR_TEST_SKIPPING_ENABLED, "false")

    return test_module_span, is_package


def _start_test_suite_span(item, test_module_span, should_enable_coverage=False):
    """
    Starts a test suite span at the start of a new pytest test module.
    """
    pytest_module_item = _find_pytest_item(item, pytest.Module)
    test_session_span = _extract_span(pytest_module_item.session)
    if test_module_span is None and isinstance(pytest_module_item.parent, pytest.Package):
        test_module_span = _extract_span(pytest_module_item.parent)
    parent_span = test_module_span
    if parent_span is None:
        parent_span = test_session_span

    test_suite_span = _CIVisibility._instance.tracer._start_span(
        "pytest.test_suite",
        service=_CIVisibility._instance._service,
        span_type=SpanTypes.TEST,
        activate=True,
        child_of=parent_span,
    )
    test_suite_span.set_tag_str(COMPONENT, "pytest")
    test_suite_span.set_tag_str(SPAN_KIND, KIND)
    test_suite_span.set_tag_str(test.FRAMEWORK, FRAMEWORK)
    test_suite_span.set_tag_str(test.FRAMEWORK_VERSION, pytest.__version__)
    test_suite_span.set_tag_str(test.COMMAND, _get_pytest_command(pytest_module_item.config))
    test_suite_span.set_tag_str(_EVENT_TYPE, _SUITE_TYPE)
    if test_session_span:
        test_suite_span.set_tag_str(_SESSION_ID, str(test_session_span.span_id))
    test_suite_span.set_tag_str(_SUITE_ID, str(test_suite_span.span_id))
    test_module_path = ""
    if test_module_span is not None:
        test_suite_span.set_tag_str(_MODULE_ID, str(test_module_span.span_id))
        test_suite_span.set_tag_str(test.MODULE, test_module_span.get_tag(test.MODULE))
        test_module_path = test_module_span.get_tag(test.MODULE_PATH)
        test_suite_span.set_tag_str(test.MODULE_PATH, test_module_path)
    test_suite_name = item.config.hook.pytest_ddtrace_get_item_suite_name(item=item)
    test_suite_span.set_tag_str(test.SUITE, test_suite_name)
    _store_span(pytest_module_item, test_suite_span)

    if should_enable_coverage and _module_has_dd_coverage_enabled(pytest):
        fqn_module = _generate_fully_qualified_module_name(test_module_path, test_suite_name)
        _switch_coverage_context(pytest._dd_coverage, fqn_module, TEST_FRAMEWORKS.PYTEST)
    return test_suite_span


def _find_pytest_item(item, pytest_item_type):
    """
    Given a `pytest.Item`, traverse upwards until we find a specified `pytest.Package` or `pytest.Module` item,
    or return None.
    """
    if item is None:
        return None
    if pytest_item_type not in [pytest.Package, pytest.Module]:
        return None
    parent = _get_parent(item)
    while not isinstance(parent, pytest_item_type) and parent is not None:
        parent = parent.parent
    return parent


def _get_test_class_hierarchy(item):
    """
    Given a `pytest.Item` function item, traverse upwards to collect and return a string listing the
    test class hierarchy, or an empty string if there are no test classes.
    """
    parent = _get_parent(item)
    test_class_hierarchy = []
    while parent is not None:
        if isinstance(parent, pytest.Class):
            test_class_hierarchy.insert(0, parent.name)
        parent = parent.parent
    return ".".join(test_class_hierarchy)


def pytest_load_initial_conftests(early_config, parser, args):
    if _is_enabled_early(early_config):
        # Enables experimental use of ModuleCodeCollector for coverage collection.
        from ddtrace.internal.ci_visibility.coverage import USE_DD_COVERAGE
        from ddtrace.internal.logger import get_logger

        log = get_logger(__name__)

        COVER_SESSION = asbool(os.environ.get("_DD_COVER_SESSION", "false"))

        if USE_DD_COVERAGE:
            from ddtrace.ext.git import extract_workspace_path
            from ddtrace.internal.coverage.code import ModuleCodeCollector
            from ddtrace.internal.coverage.installer import install

            try:
                workspace_path = Path(extract_workspace_path())
            except (ValueError, FileNotFoundError):
                workspace_path = Path(os.getcwd())

            log.warning("Installing ModuleCodeCollector with include_paths=%s", [workspace_path])

            install(include_paths=[workspace_path], collect_import_time_coverage=True)
            if COVER_SESSION:
                ModuleCodeCollector.start_coverage()
        else:
            if COVER_SESSION:
                log.warning(
                    "_DD_COVER_SESSION must be used with _DD_USE_INTERNAL_COVERAGE but not DD_CIVISIBILITY_ITR_ENABLED"
                )


def pytest_configure(config):
    deprecate(
        "this version of the pytest ddtrace plugin is slated for deprecation",
        message="set DD_PYTEST_USE_NEW_PLUGIN_BETA=true in your environment to preview the next version of the plugin.",
        removal_version="3.0.0",
        category=DDTraceDeprecationWarning,
    )
    unpatch_unittest()
    if is_enabled(config):
        ddtrace.config.test_visibility._itr_skipping_ignore_parameters = True
        take_over_logger_stream_handler()
        _CIVisibility.enable(config=ddtrace.config.pytest)
    if _is_pytest_cov_enabled(config):
        patch_coverage()


def pytest_sessionstart(session):
    if _CIVisibility.enabled:
        log.debug("CI Visibility enabled - starting test session")
        global _global_skipped_elements
        _global_skipped_elements = 0

        workspace_path = _CIVisibility.get_workspace_path()
        if workspace_path is None:
            workspace_path = session.config.rootdir

        session.config._dd_workspace_path = workspace_path

        test_session_span = _CIVisibility._instance.tracer.trace(
            "pytest.test_session",
            service=_CIVisibility._instance._service,
            span_type=SpanTypes.TEST,
        )
        test_command = _get_pytest_command(session.config)
        test_session_span.set_tag_str(COMPONENT, "pytest")
        test_session_span.set_tag_str(SPAN_KIND, KIND)
        test_session_span.set_tag_str(test.FRAMEWORK, FRAMEWORK)
        test_session_span.set_tag_str(test.FRAMEWORK_VERSION, pytest.__version__)
        test_session_span.set_tag_str(_EVENT_TYPE, _SESSION_TYPE)
        test_session_span.set_tag_str(test.COMMAND, test_command)
        test_session_span.set_tag_str(_SESSION_ID, str(test_session_span.span_id))

        _CIVisibility.set_test_session_name(test_command=test_command)

        if _CIVisibility.test_skipping_enabled():
            test_session_span.set_tag_str(test.ITR_TEST_SKIPPING_ENABLED, "true")
            test_session_span.set_tag(
                test.ITR_TEST_SKIPPING_TYPE, SUITE if _CIVisibility._instance._suite_skipping_mode else TEST
            )
            test_session_span.set_tag(test.ITR_TEST_SKIPPING_TESTS_SKIPPED, "false")
            test_session_span.set_tag(test.ITR_DD_CI_ITR_TESTS_SKIPPED, "false")
            test_session_span.set_tag_str(test.ITR_FORCED_RUN, "false")
            test_session_span.set_tag_str(test.ITR_UNSKIPPABLE, "false")
        else:
            test_session_span.set_tag_str(test.ITR_TEST_SKIPPING_ENABLED, "false")
        test_session_span.set_tag_str(
            test.ITR_TEST_CODE_COVERAGE_ENABLED,
            "true" if _CIVisibility._instance._collect_coverage_enabled else "false",
        )
        if _is_coverage_invoked_by_coverage_run():
            patch_coverage()
        if _CIVisibility._instance._collect_coverage_enabled and not _module_has_dd_coverage_enabled(
            pytest, silent_mode=True
        ):
            pytest._dd_coverage = _start_coverage(session.config.rootdir)

        _store_span(session, test_session_span)


def pytest_sessionfinish(session, exitstatus):
    if _CIVisibility.enabled:
        log.debug("CI Visibility enabled - finishing test session")
        test_session_span = _extract_span(session)
        if test_session_span is not None:
            if _CIVisibility.test_skipping_enabled():
                test_session_span.set_metric(test.ITR_TEST_SKIPPING_COUNT, _global_skipped_elements)
            _mark_test_status(session, test_session_span)
            pytest_cov_status = _is_pytest_cov_enabled(session.config)
            invoked_by_coverage_run_status = _is_coverage_invoked_by_coverage_run()
            if _is_coverage_patched() and (pytest_cov_status or invoked_by_coverage_run_status):
                if invoked_by_coverage_run_status and not pytest_cov_status:
                    run_coverage_report()
                _add_pct_covered_to_span(_coverage_data, test_session_span)
                unpatch_coverage()
            test_session_span.finish()
        _CIVisibility.disable()


def pytest_collection_modifyitems(session, config, items):
    if _CIVisibility.test_skipping_enabled():
        skip = pytest.mark.skip(reason=SKIPPED_BY_ITR_REASON)

        items_to_skip_by_module = {}
        current_suite_has_unskippable_test = False

        for item in items:
            test_is_unskippable = _is_test_unskippable(item)

            item_name = item.config.hook.pytest_ddtrace_get_item_test_name(item=item)

            if test_is_unskippable:
                log.debug(
                    "Test %s in module %s (file: %s ) is marked as unskippable",
                    item_name,
                    item.module.__name__,
                    item.module.__file__,
                )
                item._dd_itr_test_unskippable = True

            # Due to suite skipping mode, defer adding ITR skip marker until unskippable status of the suite has
            # been fully resolved because Pytest markers cannot be dynamically removed
            if _CIVisibility._instance._suite_skipping_mode:
                if item.module not in items_to_skip_by_module:
                    items_to_skip_by_module[item.module] = []
                    current_suite_has_unskippable_test = False

                if test_is_unskippable and not current_suite_has_unskippable_test:
                    current_suite_has_unskippable_test = True
                    # Retroactively mark collected tests as forced:
                    for item_to_skip in items_to_skip_by_module[item.module]:
                        item_to_skip._dd_itr_forced = True
                    items_to_skip_by_module[item.module] = []

            if _CIVisibility._instance._should_skip_path(str(get_fslocation_from_item(item)[0]), item_name):
                if test_is_unskippable or (
                    _CIVisibility._instance._suite_skipping_mode and current_suite_has_unskippable_test
                ):
                    item._dd_itr_forced = True
                else:
                    items_to_skip_by_module.setdefault(item.module, []).append(item)

        # Mark remaining tests that should be skipped
        for items_to_skip in items_to_skip_by_module.values():
            for item_to_skip in items_to_skip:
                item_to_skip.add_marker(skip)


@pytest.hookimpl(tryfirst=True, hookwrapper=True)
def pytest_runtest_protocol(item, nextitem):
    if not _CIVisibility.enabled:
        yield
        return

    is_skipped = bool(
        item.get_closest_marker("skip")
        or any([marker for marker in item.iter_markers(name="skipif") if marker.args[0] is True])
    )
    is_skipped_by_itr = bool(
        is_skipped
        and any(
            [
                marker
                for marker in item.iter_markers(name="skip")
                if "reason" in marker.kwargs and marker.kwargs["reason"] == SKIPPED_BY_ITR_REASON
            ]
        )
    )

    test_session_span = _extract_span(item.session)

    pytest_module_item = _find_pytest_item(item, pytest.Module)
    pytest_package_item = _find_pytest_item(pytest_module_item, pytest.Package)

    module_is_package = True

    test_module_span = _extract_span(pytest_package_item)
    if not test_module_span:
        test_module_span = _extract_module_span(pytest_module_item)
        if test_module_span:
            module_is_package = False

    if test_module_span is None:
        test_module_span, module_is_package = _start_test_module_span(item)

    if _CIVisibility.test_skipping_enabled() and test_module_span.get_metric(test.ITR_TEST_SKIPPING_COUNT) is None:
        test_module_span.set_tag(
            test.ITR_TEST_SKIPPING_TYPE, SUITE if _CIVisibility._instance._suite_skipping_mode else TEST
        )
        test_module_span.set_metric(test.ITR_TEST_SKIPPING_COUNT, 0)

    test_suite_span = _extract_ancestor_suite_span(item)
    if pytest_module_item is not None and test_suite_span is None:
        # Start coverage for the test suite if coverage is enabled
        # In ITR suite skipping mode, all tests in a skipped suite should be marked
        # as skipped
        test_suite_span = _start_test_suite_span(
            item,
            test_module_span,
            should_enable_coverage=(
                _CIVisibility._instance._suite_skipping_mode
                and _CIVisibility._instance._collect_coverage_enabled
                and not is_skipped_by_itr
            ),
        )

    if is_skipped_by_itr:
        test_module_span._metrics[test.ITR_TEST_SKIPPING_COUNT] += 1
        global _global_skipped_elements
        _global_skipped_elements += 1
        test_module_span.set_tag_str(test.ITR_TEST_SKIPPING_TESTS_SKIPPED, "true")
        test_module_span.set_tag_str(test.ITR_DD_CI_ITR_TESTS_SKIPPED, "true")

        test_session_span.set_tag_str(test.ITR_TEST_SKIPPING_TESTS_SKIPPED, "true")
        test_session_span.set_tag_str(test.ITR_DD_CI_ITR_TESTS_SKIPPED, "true")

    with _CIVisibility._instance.tracer._start_span(
        ddtrace.config.pytest.operation_name,
        service=_CIVisibility._instance._service,
        resource=item.nodeid,
        span_type=SpanTypes.TEST,
        activate=True,
    ) as span:
        span.set_tag_str(COMPONENT, "pytest")
        span.set_tag_str(SPAN_KIND, KIND)
        span.set_tag_str(test.FRAMEWORK, FRAMEWORK)
        span.set_tag_str(_EVENT_TYPE, SpanTypes.TEST)
        test_name = item.config.hook.pytest_ddtrace_get_item_test_name(item=item)
        test_module_path = test_module_span.get_tag(test.MODULE_PATH)
        span.set_tag_str(test.NAME, test_name)
        span.set_tag_str(test.COMMAND, _get_pytest_command(item.config))
        if test_session_span:
            span.set_tag_str(_SESSION_ID, str(test_session_span.span_id))

        span.set_tag_str(_MODULE_ID, str(test_module_span.span_id))
        span.set_tag_str(test.MODULE, test_module_span.get_tag(test.MODULE))
        span.set_tag_str(test.MODULE_PATH, test_module_path)

        span.set_tag_str(_SUITE_ID, str(test_suite_span.span_id))
        test_class_hierarchy = _get_test_class_hierarchy(item)
        if test_class_hierarchy:
            span.set_tag_str(test.CLASS_HIERARCHY, test_class_hierarchy)
        if hasattr(item, "dtest") and isinstance(item.dtest, DocTest):
            test_suite_name = "{}.py".format(item.dtest.globs["__name__"])
            span.set_tag_str(test.SUITE, test_suite_name)
        else:
            test_suite_name = test_suite_span.get_tag(test.SUITE)
            span.set_tag_str(test.SUITE, test_suite_name)

        span.set_tag_str(test.TYPE, SpanTypes.TEST)
        span.set_tag_str(test.FRAMEWORK_VERSION, pytest.__version__)

        if item.location and item.location[0]:
            _CIVisibility.set_codeowners_of(item.location[0], span=span)
        if hasattr(item, "_obj"):
            item_path = Path(item.path if hasattr(item, "path") else item.fspath)
            test_method_object = undecorated(item._obj, item.name, item_path)
            _add_start_end_source_file_path_data_to_span(
                span,
                test_method_object,
                test_name,
                getattr(item.session.config, "_dd_workspace_path", item.config.rootdir),
            )

        # We preemptively set FAIL as a status, because if pytest_runtest_makereport is not called
        # (where the actual test status is set), it means there was a pytest error
        span.set_tag_str(test.STATUS, test.Status.FAIL.value)

        # Parameterized test cases will have a `callspec` attribute attached to the pytest Item object.
        # Pytest docs: https://docs.pytest.org/en/6.2.x/reference.html#pytest.Function
        if getattr(item, "callspec", None):
            parameters = {"arguments": {}, "metadata": {}}  # type: Dict[str, Dict[str, str]]
            for param_name, param_val in item.callspec.params.items():
                try:
                    parameters["arguments"][param_name] = encode_test_parameter(param_val)
                except Exception:
                    parameters["arguments"][param_name] = "Could not encode"
                    log.warning("Failed to encode %r", param_name, exc_info=True)
            span.set_tag_str(test.PARAMETERS, json.dumps(parameters))

        if ITR_CORRELATION_ID_TAG_NAME in _CIVisibility._instance._itr_meta:
            if _CIVisibility._instance._suite_skipping_mode:
                test_suite_span.set_tag_str(
                    ITR_CORRELATION_ID_TAG_NAME, _CIVisibility._instance._itr_meta[ITR_CORRELATION_ID_TAG_NAME]
                )
            else:
                span.set_tag_str(
                    ITR_CORRELATION_ID_TAG_NAME, _CIVisibility._instance._itr_meta[ITR_CORRELATION_ID_TAG_NAME]
                )

        markers = [marker.kwargs for marker in item.iter_markers(name="dd_tags")]
        for tags in markers:
            span.set_tags(tags)
        _store_span(item, span)

        # Items are marked ITR-unskippable regardless of other unrelateed skipping status
        if getattr(item, "_dd_itr_test_unskippable", False) or getattr(item, "_dd_itr_suite_unskippable", False):
            _mark_test_unskippable(item)
        if not is_skipped:
            if getattr(item, "_dd_itr_forced", False):
                _mark_test_forced(item)

        coverage_per_test = (
            not _CIVisibility._instance._suite_skipping_mode
            and _CIVisibility._instance._collect_coverage_enabled
            and not is_skipped
        )
        root_directory = str(item.config.rootdir)
        if coverage_per_test and _module_has_dd_coverage_enabled(pytest):
            fqn_test = _generate_fully_qualified_test_name(test_module_path, test_suite_name, test_name)
            _switch_coverage_context(pytest._dd_coverage, fqn_test, TEST_FRAMEWORKS.PYTEST)
        # Run the actual test
        yield

        # Finish coverage for the test suite if coverage is enabled
        if coverage_per_test and _module_has_dd_coverage_enabled(pytest):
            _report_coverage_to_span(pytest._dd_coverage, span, root_directory, TEST_FRAMEWORKS.PYTEST)

        nextitem_pytest_module_item = _find_pytest_item(nextitem, pytest.Module)
        if nextitem is None or nextitem_pytest_module_item != pytest_module_item and not test_suite_span.finished:
            _mark_test_status(pytest_module_item, test_suite_span)
            # Finish coverage for the test suite if coverage is enabled
            # In ITR suite skipping mode, all tests in a skipped suite should be marked
            # as skipped
            if (
                _CIVisibility._instance._suite_skipping_mode
                and _CIVisibility._instance._collect_coverage_enabled
                and not is_skipped_by_itr
                and _module_has_dd_coverage_enabled(pytest)
            ):
                _report_coverage_to_span(pytest._dd_coverage, test_suite_span, root_directory)
            test_suite_span.finish()

            if not module_is_package:
                test_module_span.set_tag_str(test.STATUS, test_suite_span.get_tag(test.STATUS))
                test_module_span.finish()
            else:
                nextitem_pytest_package_item = _find_pytest_item(nextitem, pytest.Package)
                if (
                    nextitem is None
                    or nextitem_pytest_package_item != pytest_package_item
                    and not test_module_span.finished
                ):
                    _mark_test_status(pytest_package_item, test_module_span)
                    test_module_span.finish()

        if (
            nextitem is None
            and _CIVisibility._instance._collect_coverage_enabled
            and _module_has_dd_coverage_enabled(pytest)
        ):
            _stop_coverage(pytest)


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_makereport(item, call):
    """Store outcome for tracing."""
    outcome = yield

    if not _CIVisibility.enabled:
        return

    span = _extract_span(item)
    if span is None:
        return

    is_setup_or_teardown = call.when == "setup" or call.when == "teardown"
    has_exception = call.excinfo is not None

    if is_setup_or_teardown and not has_exception:
        return

    result = outcome.get_result()
    xfail = hasattr(result, "wasxfail") or "xfail" in result.keywords
    has_skip_keyword = any(x in result.keywords for x in ["skip", "skipif", "skipped"])

    # If run with --runxfail flag, tests behave as if they were not marked with xfail,
    # that's why no XFAIL_REASON or test.RESULT tags will be added.
    if result.skipped:
        if xfail and not has_skip_keyword:
            # XFail tests that fail are recorded skipped by pytest, should be passed instead
            span.set_tag_str(test.STATUS, test.Status.PASS.value)
            _mark_not_skipped(_get_parent(item))
            if not item.config.option.runxfail:
                span.set_tag_str(test.RESULT, test.Status.XFAIL.value)
                span.set_tag_str(XFAIL_REASON, getattr(result, "wasxfail", "XFail"))
        else:
            span.set_tag_str(test.STATUS, test.Status.SKIP.value)
        reason = _extract_reason(call)
        if reason is not None:
            span.set_tag_str(test.SKIP_REASON, str(reason))
            if str(reason) == SKIPPED_BY_ITR_REASON:
                if _CIVisibility._instance._suite_skipping_mode:
                    suite_span = _extract_ancestor_suite_span(item)
                    if suite_span is not None:
                        suite_span.set_tag_str(test.ITR_SKIPPED, "true")
                span.set_tag_str(test.ITR_SKIPPED, "true")
    elif result.passed:
        _mark_not_skipped(_get_parent(item))
        span.set_tag_str(test.STATUS, test.Status.PASS.value)
        if xfail and not has_skip_keyword and not item.config.option.runxfail:
            # XPass (strict=False) are recorded passed by pytest
            span.set_tag_str(XFAIL_REASON, getattr(result, "wasxfail", "XFail"))
            span.set_tag_str(test.RESULT, test.Status.XPASS.value)
    else:
        # Store failure in test suite `pytest.Item` to propagate to test suite spans
        _mark_failed(_get_parent(item))
        _mark_not_skipped(_get_parent(item))
        span.set_tag_str(test.STATUS, test.Status.FAIL.value)
        if xfail and not has_skip_keyword and not item.config.option.runxfail:
            # XPass (strict=True) are recorded failed by pytest, longrepr contains reason
            span.set_tag_str(XFAIL_REASON, getattr(result, "longrepr", "XFail"))
            span.set_tag_str(test.RESULT, test.Status.XPASS.value)
        if call.excinfo:
            span.set_exc_info(call.excinfo.type, call.excinfo.value, call.excinfo.tb)


@pytest.hookimpl(trylast=True)
def pytest_ddtrace_get_item_module_name(item):
    pytest_module_item = _find_pytest_item(item, pytest.Module)
    pytest_package_item = _find_pytest_item(pytest_module_item, pytest.Package)

    if _module_is_package(pytest_package_item, pytest_module_item):
        if _is_pytest_8_or_later():
            # pytest 8.0.0 no longer treats Packages as Module/File, so we replicate legacy behavior by
            # concatenating parent package names in reverse until we hit a non-Package-type item
            # https://github.com/pytest-dev/pytest/issues/11137
            package_names = []
            current_package = pytest_package_item
            while isinstance(current_package, pytest.Package):
                package_names.append(str(current_package.name))
                current_package = current_package.parent

            return ".".join(package_names[::-1])

        return pytest_package_item.module.__name__

    return pytest_module_item.nodeid.rpartition("/")[0].replace("/", ".")


@pytest.hookimpl(trylast=True)
def pytest_ddtrace_get_item_suite_name(item):
    """
    Extract suite name from a `pytest.Item` instance.
    If the module path doesn't exist, the suite path will be reported in full.
    """
    pytest_module_item = _find_pytest_item(item, pytest.Module)
    test_module_path = _get_module_path(pytest_module_item)
    if test_module_path:
        if not pytest_module_item.nodeid.startswith(test_module_path):
            log.warning("Suite path is not under module path: '%s' '%s'", pytest_module_item.nodeid, test_module_path)
        return get_relative_or_absolute_path_for_path(pytest_module_item.nodeid, test_module_path)
    return pytest_module_item.nodeid


@pytest.hookimpl(trylast=True)
def pytest_ddtrace_get_item_test_name(item):
    """Extract name from item, prepending class if desired"""
    if hasattr(item, "cls") and item.cls:
        if item.config.getoption("ddtrace-include-class-name") or item.config.getini("ddtrace-include-class-name"):
            return "%s.%s" % (item.cls.__name__, item.name)
    return item.name


@pytest.hookimpl(trylast=True)
def pytest_terminal_summary(terminalreporter, exitstatus, config):
    # Reports coverage if experimental session-level coverage is enabled.
    if USE_DD_COVERAGE and COVER_SESSION:
        workspace_path = getattr(config, "_dd_workspace_path", None)
        if workspace_path is None:
            workspace_path = Path(os.getcwd())

        ModuleCodeCollector.report(workspace_path)
        try:
            ModuleCodeCollector.write_json_report_to_file("dd_coverage.json", workspace_path)
        except Exception:
            log.debug("Failed to write coverage report to file", exc_info=True)
