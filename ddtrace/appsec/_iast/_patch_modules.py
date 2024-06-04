from ddtrace.internal import core


IAST_PATCH = {
    "command_injection": True,
    "header_injection": True,
    "weak_cipher": True,
    "weak_hash": True,
}


def patch_iast(patch_modules=IAST_PATCH):
    """Load IAST vulnerabilities sink points.

    IAST_PATCH: list of implemented vulnerabilities
    """
    # TODO: Devise the correct patching strategy for IAST
    from ddtrace._monkey import _on_import_factory

    for module in (m for m, e in patch_modules.items() if e):
        core.on_import("hashlib")(
            _on_import_factory(module, prefix="ddtrace.appsec._iast.taint_sinks", raise_errors=False)
        )

    core.on_import("json")(
        _on_import_factory("json_tainting", prefix="ddtrace.appsec._iast._patches", raise_errors=False)
    )
