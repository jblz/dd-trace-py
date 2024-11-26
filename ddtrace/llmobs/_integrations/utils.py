from ddtrace.llmobs._constants import INPUT_TOKENS_METRIC_KEY
from ddtrace.llmobs._constants import OUTPUT_TOKENS_METRIC_KEY
from ddtrace.llmobs._constants import TOTAL_TOKENS_METRIC_KEY
from ddtrace.llmobs._utils import _get_attr


def extract_model_name_google(instance, model_name_attr):
    """Extract the model name from the instance.
    Model names are stored in the format `"models/{model_name}"`
    so we do our best to return the model name instead of the full string.
    """
    model_name = _get_attr(instance, model_name_attr, "")
    if not model_name or not isinstance(model_name, str):
        return ""
    if "/" in model_name:
        return model_name.split("/")[-1]
    return model_name


def get_generation_config_google(instance, kwargs):
    """
    The generation config can be defined on the model instance or
    as a kwarg of the request. Therefore, try to extract this information
    from the kwargs and otherwise default to checking the model instance attribute.
    """
    generation_config = kwargs.get("generation_config", {})
    return generation_config or _get_attr(instance, "_generation_config", {})


def tag_request_content_part_google(tag_prefix, span, integration, part, part_idx, content_idx):
    """Tag the generation span with request content parts."""
    text = _get_attr(part, "text", "")
    function_call = _get_attr(part, "function_call", None)
    function_response = _get_attr(part, "function_response", None)
    span.set_tag_str(
        "%s.request.contents.%d.parts.%d.text" % (tag_prefix, content_idx, part_idx), integration.trunc(str(text))
    )
    if function_call:
        function_call_dict = type(function_call).to_dict(function_call)
        span.set_tag_str(
            "%s.request.contents.%d.parts.%d.function_call.name" % (tag_prefix, content_idx, part_idx),
            function_call_dict.get("name", ""),
        )
        span.set_tag_str(
            "%s.request.contents.%d.parts.%d.function_call.args" % (tag_prefix, content_idx, part_idx),
            integration.trunc(str(function_call_dict.get("args", {}))),
        )
    if function_response:
        function_response_dict = type(function_response).to_dict(function_response)
        span.set_tag_str(
            "%s.request.contents.%d.parts.%d.function_response.name" % (tag_prefix, content_idx, part_idx),
            function_response_dict.get("name", ""),
        )
        span.set_tag_str(
            "%s.request.contents.%d.parts.%d.function_response.response" % (tag_prefix, content_idx, part_idx),
            integration.trunc(str(function_response_dict.get("response", {}))),
        )


def tag_response_part_google(tag_prefix, span, integration, part, part_idx, candidate_idx):
    """Tag the generation span with response part text and function calls."""
    text = _get_attr(part, "text", "")
    span.set_tag_str(
        "%s.response.candidates.%d.content.parts.%d.text" % (tag_prefix, candidate_idx, part_idx),
        integration.trunc(str(text)),
    )
    function_call = _get_attr(part, "function_call", None)
    if not function_call:
        return
    span.set_tag_str(
        "%s.response.candidates.%d.content.parts.%d.function_call.name" % (tag_prefix, candidate_idx, part_idx),
        _get_attr(function_call, "name", ""),
    )
    span.set_tag_str(
        "%s.response.candidates.%d.content.parts.%d.function_call.args" % (tag_prefix, candidate_idx, part_idx),
        integration.trunc(str(_get_attr(function_call, "args", {}))),
    )


def llmobs_get_metadata_google(kwargs, instance):
    metadata = {}
    model_config = getattr(instance, "_generation_config", {}) or {}
    model_config = model_config.to_dict() if hasattr(model_config, "to_dict") else model_config
    request_config = kwargs.get("generation_config", {}) or {}
    request_config = request_config.to_dict() if hasattr(request_config, "to_dict") else request_config

    parameters = ("temperature", "max_output_tokens", "candidate_count", "top_p", "top_k")
    for param in parameters:
        model_config_value = _get_attr(model_config, param, None)
        request_config_value = _get_attr(request_config, param, None)
        if model_config_value or request_config_value:
            metadata[param] = request_config_value or model_config_value
    return metadata


def extract_message_from_part_google(part, role=None):
    text = _get_attr(part, "text", "")
    function_call = _get_attr(part, "function_call", None)
    function_response = _get_attr(part, "function_response", None)
    message = {"content": text}
    if role:
        message["role"] = role
    if function_call:
        function_call_dict = function_call
        if not isinstance(function_call, dict):
            function_call_dict = type(function_call).to_dict(function_call)
        message["tool_calls"] = [
            {"name": function_call_dict.get("name", ""), "arguments": function_call_dict.get("args", {})}
        ]
    if function_response:
        function_response_dict = function_response
        if not isinstance(function_response, dict):
            function_response_dict = type(function_response).to_dict(function_response)
        message["content"] = "[tool result: {}]".format(function_response_dict.get("response", ""))
    return message


def get_llmobs_metrics_tags_google(integration_name, span):
    usage = {}
    input_tokens = span.get_metric("%s.response.usage.prompt_tokens" % integration_name)
    output_tokens = span.get_metric("%s.response.usage.completion_tokens" % integration_name)
    total_tokens = span.get_metric("%s.response.usage.total_tokens" % integration_name)

    if input_tokens is not None:
        usage[INPUT_TOKENS_METRIC_KEY] = input_tokens
    if output_tokens is not None:
        usage[OUTPUT_TOKENS_METRIC_KEY] = output_tokens
    if total_tokens is not None:
        usage[TOTAL_TOKENS_METRIC_KEY] = total_tokens
    return usage


def get_system_instructions_from_google_model(model_instance):
    """
    Extract system instructions from model and convert to []str for tagging.
    """
    from vertexai.generative_models._generative_models import Part
    raw_system_instructions = getattr(model_instance, "_system_instruction", [])
    if isinstance(raw_system_instructions, str):
        return [raw_system_instructions]
    elif isinstance(raw_system_instructions, Part):
        return [_get_attr(raw_system_instructions, "text", "")]
    elif not isinstance(raw_system_instructions, list):
        return []

    system_instructions = []
    for elem in raw_system_instructions:
        if isinstance(elem, str):
            system_instructions.append(elem)
        elif isinstance(elem, Part):
            system_instructions.append(_get_attr(elem, "text", ""))
    return system_instructions
