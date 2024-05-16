import os

from ..internal.logger import get_logger


log = get_logger(__name__)

ENV_VAR_MAPPINGS = {
    "OTEL_SERVICE_NAME": "DD_SERVICE",
    "OTEL_LOG_LEVEL": "DD_TRACE_DEBUG",
    "OTEL_PROPAGATORS": "DD_TRACE_PROPAGATION_STYLE",
    "OTEL_TRACES_SAMPLER": "DD_TRACE_SAMPLE_RATE",
    "OTEL_TRACES_EXPORTER": "DD_TRACE_ENABLED",
    "OTEL_METRICS_EXPORTER": "DD_RUNTIME_METRICS_ENABLED",
    "OTEL_LOGS_EXPORTER": "none",  # This should be ignored
    "OTEL_RESOURCE_ATTRIBUTES": "DD_TAGS",
    "OTEL_SDK_DISABLED": "DD_TRACE_OTEL_ENABLED",
}

OTEL_UNIFIED_TAG_MAPPINGS = {
    "deployment.environment": "DD_ENV",
    "service.name": "DD_SERVICE",
    "service.version": "DD_VERSION",
}


def otel_remapping():
    """Checks for the existence of both OTEL and Datadog tracer environment variables and remaps them accordingly.
    Datadog Environment variables take precedence over OTEL, but if there isn't a Datadog value present,
    then OTEL values take their place.
    """

    def _remap_otel_log_level(otel_value):
        if otel_value == "debug":
            new_value = "True"
        elif otel_value == "info":
            new_value = "False"
        else:
            log.warning(
                "ddtrace does not support otel log level '%s'. setting ddtrace to log level info.",
                otel_value,
            )
            new_value = "False"
        return new_value

    def _remap_otel_propagators(otel_value):
        accepted_styles = []
        for style in otel_value.split(","):
            style = style.strip().lower()
            if style in ["b3", "b3multi", "b3single", "datadog", "tracecontext", "none"]:
                if style == "b3single":
                    style = "b3"
                if style not in accepted_styles:
                    accepted_styles.append(style)
            else:
                log.warning("Following style not supported by ddtrace: %s", style)
        new_value = ",".join(accepted_styles)
        return new_value

    def _remap_traces_sampler(otel_value):
        if otel_value == "always_on" or otel_value == "parentbased_always_on":
            new_value = "1.0"
        elif otel_value == "always_off" or otel_value == "parentbased_always_off":
            new_value = "0.0"
        elif otel_value == "traceidratio" or otel_value == "parentbased_traceidratio":
            new_value = os.environ.get("OTEL_TRACES_SAMPLER_ARG", "1")
        return new_value

    def _remap_traces_exporter(otel_value):
        if otel_value != "none":
            log.warning("An unrecognized exporter '%s' is being used; setting dd_trace_enabled to false.", otel_value)
        new_value = "False"
        return new_value

    def _remap_metrics_exporter(otel_value):
        if otel_value != "none":
            log.warning(
                "An unrecognized exporter '%s' is being used; setting dd_runtime_metrics_enabled to false.", otel_value
            )
        new_value = "False"
        return new_value

    def _remap_logs_exporter(otel_value):
        if otel_value != "none":
            log.warning("Unsupported logs exporter detected.")
            new_dd_value = ""
            return new_dd_value
        return otel_value

    def _remap_otel_tags(otel_value):
        dd_tags = []
        otel_tags = otel_value.split(",")
        otel_user_tag_dict = dict()

        for tag in otel_tags:
            tag_pair = tag.split("=")
            otel_user_tag_dict[tag_pair[0]] = tag_pair[1]

        for otel_key, dd_key in OTEL_UNIFIED_TAG_MAPPINGS.items():
            if otel_key in otel_user_tag_dict.keys():
                dd_tags.append("{}:{}".format(dd_key, otel_user_tag_dict[otel_key]))

        for key, value in otel_user_tag_dict.items():
            if len(dd_tags) < 11 and key not in [
                "deployment.environment",
                "service.name",
                "service.version",
            ]:
                dd_tags.append("{}:{}".format(key, value))

        if len(otel_user_tag_dict.items()) > 10:
            ten_tags = dd_tags[:10]
            log.warning(
                "To preserve metrics cardinality, only the following first 10 tags have been processed %s",
                ten_tags,
            )
        new_value = ",".join(dd_tags)
        return new_value

    def _remap_otel_sdk_config(otel_value):
        if otel_value == "false":
            new_value = "True"
        elif otel_value == "true":
            new_value = "False"
        else:
            log.warning("Unexpected value for OTEL_SDK_DISABLED.")
            return otel_value
        return new_value

    def _remapper(otel_env, otel_value):
        if otel_env == "OTEL_LOG_LEVEL":
            new_dd_value = _remap_otel_log_level(otel_value)
        elif otel_env == "OTEL_PROPAGATORS":
            new_dd_value = _remap_otel_propagators(otel_value)
        elif otel_env == "OTEL_TRACES_SAMPLER":
            new_dd_value = _remap_traces_sampler(otel_value)
        elif otel_env == "OTEL_TRACES_EXPORTER":
            new_dd_value = _remap_traces_exporter(otel_value)
        elif otel_env == "OTEL_METRICS_EXPORTER":
            new_dd_value = _remap_metrics_exporter(otel_value)
        elif otel_env == "OTEL_LOGS_EXPORTER":
            new_dd_value = _remap_logs_exporter(otel_value)
        elif otel_env == "OTEL_RESOURCE_ATTRIBUTES":
            new_dd_value = _remap_otel_tags(otel_value)
        elif otel_env == "OTEL_SDK_DISABLED":
            new_dd_value = _remap_otel_sdk_config(otel_value)
        else:
            new_dd_value = otel_value
        return new_dd_value

    def _set_otel_env(otel_env, dd_env, otel_config_remapper):
        otel_value = os.environ.get(otel_env, "").strip()
        dd_value = os.environ.get(dd_env, "").strip()
        if dd_value == "":
            if otel_env not in ["OTEL_RESOURCE_ATTRIBUTES", "OTEL_SERVICE_NAME"]:
                otel_value = otel_value.lower()
                dd_value = dd_value.lower()
            dd_remap_value = otel_config_remapper(otel_env, otel_value)
            if dd_remap_value != "":
                os.environ[dd_env] = dd_remap_value

    user_envs = [key.upper() for key in os.environ.keys()]

    for env in ENV_VAR_MAPPINGS.keys():
        if env in user_envs:
            _set_otel_env(env, ENV_VAR_MAPPINGS[env], _remapper)
