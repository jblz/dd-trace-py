import json
import sys
from typing import Any
from typing import Dict
from typing import List

import wrapt

from ddtrace import config
from ddtrace.contrib.trace_utils import ext_service
from ddtrace.ext import SpanTypes
from ddtrace.internal import core
from ddtrace.internal.logger import get_logger
from ddtrace.internal.schema import schematize_service_name


log = get_logger(__name__)


_AI21 = "ai21"
_AMAZON = "amazon"
_ANTHROPIC = "anthropic"
_COHERE = "cohere"
_META = "meta"
_STABILITY = "stability"


class TracedBotocoreStreamingBody(wrapt.ObjectProxy):
    """
    This class wraps the StreamingBody object returned by botocore api calls, specifically for Bedrock invocations.
    Since the response body is in the form of a stream object, we need to wrap it in order to tag the response data
    and fire completion events as the user consumes the streamed response.
    """

    def __init__(self, wrapped, ctx: core.ExecutionContext):
        super().__init__(wrapped)
        self._body = []
        self._execution_ctx = ctx

    def read(self, amt=None):
        """Wraps around method to tags the response data and finish the span as the user consumes the stream."""
        try:
            body = self.__wrapped__.read(amt=amt)
            self._body.append(json.loads(body))
            if self.__wrapped__.tell() == int(self.__wrapped__._content_length):
                formatted_response = _extract_text_and_response_reason(self._execution_ctx, self._body[0])
                model_provider = self._execution_ctx["model_provider"]
                model_name = self._execution_ctx["model_name"]
                should_set_choice_ids = model_provider == _COHERE and "embed" not in model_name
                core.dispatch(
                    "botocore.bedrock.process_response",
                    [self._execution_ctx, formatted_response, None, self._body[0], should_set_choice_ids],
                )
            return body
        except Exception:
            core.dispatch("botocore.patched_bedrock_api_call.exception", [self._execution_ctx, sys.exc_info()])
            raise

    def readlines(self):
        """Wraps around method to tags the response data and finish the span as the user consumes the stream."""
        try:
            lines = self.__wrapped__.readlines()
            for line in lines:
                self._body.append(json.loads(line))
            formatted_response = _extract_text_and_response_reason(self._execution_ctx, self._body[0])
            model_provider = self._execution_ctx["model_provider"]
            model_name = self._execution_ctx["model_name"]
            should_set_choice_ids = model_provider == _COHERE and "embed" not in model_name
            core.dispatch(
                "botocore.bedrock.process_response",
                [self._execution_ctx, formatted_response, None, self._body[0], should_set_choice_ids],
            )
            return lines
        except Exception:
            core.dispatch("botocore.patched_bedrock_api_call.exception", [self._execution_ctx, sys.exc_info()])
            raise

    def __iter__(self):
        """Wraps around method to tags the response data and finish the span as the user consumes the stream."""
        try:
            for line in self.__wrapped__:
                self._body.append(json.loads(line["chunk"]["bytes"]))
                yield line
            metadata = _extract_streamed_response_metadata(self._execution_ctx, self._body)
            formatted_response = _extract_streamed_response(self._execution_ctx, self._body)
            model_provider = self._execution_ctx["model_provider"]
            model_name = self._execution_ctx["model_name"]
            should_set_choice_ids = (
                model_provider == _COHERE and "is_finished" not in self._body[0] and "embed" not in model_name
            )
            core.dispatch(
                "botocore.bedrock.process_response",
                [self._execution_ctx, formatted_response, metadata, self._body, should_set_choice_ids],
            )
        except Exception:
            core.dispatch("botocore.patched_bedrock_api_call.exception", [self._execution_ctx, sys.exc_info()])
            raise


def _extract_request_params(params: Dict[str, Any], provider: str) -> Dict[str, Any]:
    """
    Extracts request parameters including prompt, temperature, top_p, max_tokens, and stop_sequences.
    """
    request_body = json.loads(params.get("body"))
    model_id = params.get("modelId")
    if provider == _AI21:
        return {
            "prompt": request_body.get("prompt"),
            "temperature": request_body.get("temperature", ""),
            "top_p": request_body.get("topP", ""),
            "max_tokens": request_body.get("maxTokens", ""),
            "stop_sequences": request_body.get("stopSequences", []),
        }
    elif provider == _AMAZON and "embed" in model_id:
        return {"prompt": request_body.get("inputText")}
    elif provider == _AMAZON:
        text_generation_config = request_body.get("textGenerationConfig", {})
        return {
            "prompt": request_body.get("inputText"),
            "temperature": text_generation_config.get("temperature", ""),
            "top_p": text_generation_config.get("topP", ""),
            "max_tokens": text_generation_config.get("maxTokenCount", ""),
            "stop_sequences": text_generation_config.get("stopSequences", []),
        }
    elif provider == _ANTHROPIC:
        prompt = request_body.get("prompt", "")
        messages = request_body.get("messages", "")
        return {
            "prompt": prompt or messages,
            "temperature": request_body.get("temperature", ""),
            "top_p": request_body.get("top_p", ""),
            "top_k": request_body.get("top_k", ""),
            "max_tokens": request_body.get("max_tokens_to_sample", ""),
            "stop_sequences": request_body.get("stop_sequences", []),
        }
    elif provider == _COHERE and "embed" in model_id:
        return {
            "prompt": request_body.get("texts"),
            "input_type": request_body.get("input_type", ""),
            "truncate": request_body.get("truncate", ""),
        }
    elif provider == _COHERE:
        return {
            "prompt": request_body.get("prompt"),
            "temperature": request_body.get("temperature", ""),
            "top_p": request_body.get("p", ""),
            "top_k": request_body.get("k", ""),
            "max_tokens": request_body.get("max_tokens", ""),
            "stop_sequences": request_body.get("stop_sequences", []),
            "stream": request_body.get("stream", ""),
            "n": request_body.get("num_generations", ""),
        }
    elif provider == _META:
        return {
            "prompt": request_body.get("prompt"),
            "temperature": request_body.get("temperature", ""),
            "top_p": request_body.get("top_p", ""),
            "max_tokens": request_body.get("max_gen_len", ""),
        }
    elif provider == _STABILITY:
        # TODO: request/response formats are different for image-based models. Defer for now
        return {}
    return {}


def _extract_text_and_response_reason(ctx: core.ExecutionContext, body: Dict[str, Any]) -> Dict[str, List[str]]:
    text, finish_reason = "", ""
    model_name = ctx["model_name"]
    provider = ctx["model_provider"]
    try:
        if provider == _AI21:
            completions = body.get("completions", [])
            if completions:
                data = completions[0].get("data", {})
                text = data.get("text")
                finish_reason = completions[0].get("finishReason")
        elif provider == _AMAZON and "embed" in model_name:
            text = [body.get("embedding", [])]
        elif provider == _AMAZON:
            results = body.get("results", [])
            if results:
                text = results[0].get("outputText")
                finish_reason = results[0].get("completionReason")
        elif provider == _ANTHROPIC:
            text = body.get("completion", "") or body.get("content", "")
            finish_reason = body.get("stop_reason")
        elif provider == _COHERE and "embed" in model_name:
            text = body.get("embeddings", [[]])
        elif provider == _COHERE:
            generations = body.get("generations", [])
            text = [generation["text"] for generation in generations]
            finish_reason = [generation["finish_reason"] for generation in generations]
        elif provider == _META:
            text = body.get("generation")
            finish_reason = body.get("stop_reason")
        elif provider == _STABILITY:
            # TODO: request/response formats are different for image-based models. Defer for now
            pass
    except (IndexError, AttributeError, TypeError):
        log.warning("Unable to extract text/finish_reason from response body. Defaulting to empty text/finish_reason.")

    if not isinstance(text, list):
        text = [text]
    if not isinstance(finish_reason, list):
        finish_reason = [finish_reason]

    return {"text": text, "finish_reason": finish_reason}


def _extract_streamed_response(ctx: core.ExecutionContext, streamed_body: List[Dict[str, Any]]) -> Dict[str, List[str]]:
    text, finish_reason = "", ""
    model_name = ctx["model_name"]
    provider = ctx["model_provider"]
    try:
        if provider == _AI21:
            pass  # DEV: ai21 does not support streamed responses
        elif provider == _AMAZON and "embed" in model_name:
            pass  # DEV: amazon embed models do not support streamed responses
        elif provider == _AMAZON:
            text = "".join([chunk["outputText"] for chunk in streamed_body])
            finish_reason = streamed_body[-1]["completionReason"]
        elif provider == _ANTHROPIC:
            for chunk in streamed_body:
                if "completion" in chunk:
                    text += chunk["completion"]
                    if chunk["stop_reason"]:
                        finish_reason = chunk["stop_reason"]
                elif "delta" in chunk:
                    text += chunk["delta"].get("text", "")
                    if "stop_reason" in chunk["delta"]:
                        finish_reason = str(chunk["delta"]["stop_reason"])
        elif provider == _COHERE and "embed" in model_name:
            pass  # DEV: cohere embed models do not support streamed responses
        elif provider == _COHERE:
            if "is_finished" in streamed_body[0]:  # streamed response
                if "index" in streamed_body[0]:  # n >= 2
                    num_generations = int(ctx.get_item("num_generations") or 0)
                    text = [
                        "".join([chunk["text"] for chunk in streamed_body[:-1] if chunk["index"] == i])
                        for i in range(num_generations)
                    ]
                    finish_reason = [streamed_body[-1]["finish_reason"] for _ in range(num_generations)]
                else:
                    text = "".join([chunk["text"] for chunk in streamed_body[:-1]])
                    finish_reason = streamed_body[-1]["finish_reason"]
            else:
                text = [chunk["text"] for chunk in streamed_body[0]["generations"]]
                finish_reason = [chunk["finish_reason"] for chunk in streamed_body[0]["generations"]]
        elif provider == _META:
            text = "".join([chunk["generation"] for chunk in streamed_body])
            finish_reason = streamed_body[-1]["stop_reason"]
        elif provider == _STABILITY:
            pass  # DEV: we do not yet support image modality models
    except (IndexError, AttributeError):
        log.warning("Unable to extract text/finish_reason from response body. Defaulting to empty text/finish_reason.")

    if not isinstance(text, list):
        text = [text]
    if not isinstance(finish_reason, list):
        finish_reason = [finish_reason]

    return {"text": text, "finish_reason": finish_reason}


def _extract_streamed_response_metadata(
    ctx: core.ExecutionContext, streamed_body: List[Dict[str, Any]]
) -> Dict[str, Any]:
    provider = ctx["model_provider"]
    metadata = {}
    if provider == _AI21:
        pass  # ai21 does not support streamed responses
    elif provider in [_AMAZON, _ANTHROPIC, _COHERE, _META] and streamed_body:
        metadata = streamed_body[-1].get("amazon-bedrock-invocationMetrics", {})
    elif provider == _STABILITY:
        # TODO: figure out extraction for image-based models
        pass
    return {
        "response.duration": metadata.get("invocationLatency", None),
        "usage.prompt_tokens": metadata.get("inputTokenCount", None),
        "usage.completion_tokens": metadata.get("outputTokenCount", None),
    }


def handle_bedrock_request(ctx: core.ExecutionContext) -> None:
    """Perform request param extraction and tagging."""
    request_params = _extract_request_params(ctx["params"], ctx["model_provider"])
    core.dispatch("botocore.patched_bedrock_api_call.started", [ctx, request_params])
    prompt = None
    for k, v in request_params.items():
        if k == "prompt" and ctx["bedrock_integration"].is_pc_sampled_llmobs(ctx[ctx["call_key"]]):
            prompt = v
    ctx.set_item("prompt", prompt)


def handle_bedrock_response(
    ctx: core.ExecutionContext,
    result: Dict[str, Any],
) -> Dict[str, Any]:
    metadata = result["ResponseMetadata"]
    http_headers = metadata["HTTPHeaders"]

    core.dispatch(
        "botocore.patched_bedrock_api_call.success",
        [
            ctx,
            str(metadata.get("RequestId", "")),
            str(http_headers.get("x-amzn-bedrock-invocation-latency", "")),
            str(http_headers.get("x-amzn-bedrock-input-token-count", "")),
            str(http_headers.get("x-amzn-bedrock-output-token-count", "")),
        ],
    )

    body = result["body"]
    result["body"] = TracedBotocoreStreamingBody(body, ctx)
    return result


def patched_bedrock_api_call(original_func, instance, args, kwargs, function_vars):
    params = function_vars.get("params")
    pin = function_vars.get("pin")
    model_meta = params.get("modelId").split(".")
    if len(model_meta) == 2:
        model_provider, model_name = model_meta
    else:
        _, model_provider, model_name = model_meta  # cross-region inference
    integration = function_vars.get("integration")
    submit_to_llmobs = integration.llmobs_enabled and "embed" not in model_name
    with core.context_with_data(
        "botocore.patched_bedrock_api_call",
        pin=pin,
        span_name=function_vars.get("trace_operation"),
        service=schematize_service_name(
            "{}.{}".format(ext_service(pin, int_config=config.botocore), function_vars.get("endpoint_name"))
        ),
        resource=function_vars.get("operation"),
        span_type=SpanTypes.LLM if submit_to_llmobs else None,
        call_key="instrumented_bedrock_call",
        call_trace=True,
        bedrock_integration=integration,
        params=params,
        model_provider=model_provider,
        model_name=model_name,
    ) as ctx:
        try:
            handle_bedrock_request(ctx)
            result = original_func(*args, **kwargs)
            result = handle_bedrock_response(ctx, result)
            return result
        except Exception:
            core.dispatch("botocore.patched_bedrock_api_call.exception", [ctx, sys.exc_info()])
            raise
