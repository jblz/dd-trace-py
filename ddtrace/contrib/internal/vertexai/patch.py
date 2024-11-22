import os
import sys

import vertexai

from ddtrace import config
from ddtrace.contrib.internal.vertexai._utils import TracedAsyncVertexAIStreamResponse
from ddtrace.contrib.internal.vertexai._utils import TracedVertexAIStreamResponse
from ddtrace.contrib.internal.vertexai._utils import tag_request
from ddtrace.contrib.internal.vertexai._utils import tag_response
from ddtrace.contrib.trace_utils import unwrap
from ddtrace.contrib.trace_utils import with_traced_module
from ddtrace.contrib.trace_utils import wrap
from ddtrace.llmobs._integrations import VertexAIIntegration
from ddtrace.llmobs._integrations.utils import extract_model_name_google
from ddtrace.pin import Pin


config._add(
    "vertexai",
    {
        "span_prompt_completion_sample_rate": float(os.getenv("DD_VERTEXAI_SPAN_PROMPT_COMPLETION_SAMPLE_RATE", 1.0)),
        "span_char_limit": int(os.getenv("DD_VERTEXAI_SPAN_CHAR_LIMIT", 128)),
    },
)


def get_version():
    # type: () -> str
    return getattr(vertexai, "__version__", "")


@with_traced_module
def traced_generate(vertexai, pin, func, instance, args, kwargs):
    return _traced_generate(vertexai, pin, func, instance, args, kwargs, instance, False)


@with_traced_module
async def traced_agenerate(vertexai, pin, func, instance, args, kwargs):
    return await _traced_agenerate(vertexai, pin, func, instance, args, kwargs, instance, False)


@with_traced_module
def traced_send_message(vertexai, pin, func, instance, args, kwargs):
    return _traced_generate(vertexai, pin, func, instance, args, kwargs, instance._model, True)


@with_traced_module
async def traced_send_message_async(vertexai, pin, func, instance, args, kwargs):
    return await _traced_agenerate(vertexai, pin, func, instance, args, kwargs, instance._model, True)


def _traced_generate(vertexai, pin, func, instance, args, kwargs, model_instance, is_chat):
    integration = vertexai._datadog_integration
    stream = kwargs.get("stream", False)
    generations = None
    span = integration.trace(
        pin,
        "%s.%s" % (instance.__class__.__name__, func.__name__),
        provider="google",
        model=extract_model_name_google(model_instance, "_model_name"),
        submit_to_llmobs=False,
    )
    try:
        tag_request(span, integration, instance, args, kwargs)
        generations = func(*args, **kwargs)
        if stream:
            return TracedVertexAIStreamResponse(generations, integration, span, is_chat)
        tag_response(span, generations, integration)
    except Exception:
        span.set_exc_info(*sys.exc_info())
        raise
    finally:
        # streamed spans will be finished separately once the stream generator is exhausted
        if span.error or not stream:
            span.finish()
    return generations


async def _traced_agenerate(vertexai, pin, func, instance, args, kwargs, model_instance, is_chat):
    integration = vertexai._datadog_integration
    stream = kwargs.get("stream", False)
    generations = None
    span = integration.trace(
        pin,
        "%s.%s" % (instance.__class__.__name__, func.__name__),
        provider="google",
        model=extract_model_name_google(model_instance, "_model_name"),
        submit_to_llmobs=False,
    )
    try:
        tag_request(span, integration, instance, args, kwargs)
        generations = await func(*args, **kwargs)
        if stream:
            return TracedAsyncVertexAIStreamResponse(generations, integration, span, is_chat)
        tag_response(span, generations, integration)
    except Exception:
        span.set_exc_info(*sys.exc_info())
        raise
    finally:
        # streamed spans will be finished separately once the stream generator is exhausted
        if span.error or not stream:
            span.finish()
    return generations


def patch():
    if getattr(vertexai, "_datadog_patch", False):
        return

    vertexai._datadog_patch = True

    Pin().onto(vertexai)
    integration = VertexAIIntegration(integration_config=config.vertexai)
    vertexai._datadog_integration = integration

    wrap("vertexai", "generative_models.GenerativeModel.generate_content", traced_generate(vertexai))
    wrap("vertexai", "generative_models.GenerativeModel.generate_content_async", traced_agenerate(vertexai))
    wrap("vertexai", "generative_models.ChatSession.send_message", traced_send_message(vertexai))
    wrap("vertexai", "generative_models.ChatSession.send_message_async", traced_send_message_async(vertexai))


def unpatch():
    if not getattr(vertexai, "_datadog_patch", False):
        return

    vertexai._datadog_patch = False

    unwrap(vertexai.generative_models.GenerativeModel, "generate_content")
    unwrap(vertexai.generative_models.GenerativeModel, "generate_content_async")
    unwrap(vertexai.generative_models.ChatSession, "send_message")
    unwrap(vertexai.generative_models.ChatSession, "send_message_async")

    delattr(vertexai, "_datadog_integration")
