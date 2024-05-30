import sys
from typing import AsyncGenerator
from typing import Dict
from typing import Generator
from typing import Tuple

from ddtrace.internal.logger import get_logger
from ddtrace.internal.utils import get_argument_value
from ddtrace.vendor import wrapt


log = get_logger(__name__)


def handle_stream_response(integration, resp, args, kwargs, span):
    if _is_async_generator(resp):
        return TracedOpenAIAsyncStream(resp, integration, span, args, kwargs)
    elif _is_generator(resp):
        return TracedOpenAIStream(resp, integration, span, args, kwargs)
    elif _is_stream_manager(resp):
        return TracedOpenAIStreamManager(resp, integration, span, args, kwargs)


def _process_finished_stream(integration, span, args, kwargs, streamed_chunks, message_accumulated=False):
    messages = None
    messages = kwargs.get("messages", None)
    chat_messages = get_argument_value(args, kwargs, 0, "messages")
    try:
        if message_accumulated:
            messages = _construct_accumulated_messages(streamed_chunks)
        else:
            messages = _construct_unaccumulated_messages(streamed_chunks)
        if integration.is_pc_sampled_span(span):
            _tag_streamed_chat_completion_response(integration, span, messages)
        if integration.is_pc_sampled_llmobs(span):
            integration.llmobs_set_tags(
                span,
                input_messages=chat_messages,
                formatted_response=messages,
            )
    except Exception:
        log.warning("Error processing streamed completion/chat response.", exc_info=True)


class BaseTracedOpenAIStream(wrapt.ObjectProxy):
    def __init__(self, wrapped, integration, span, args, kwargs, is_message_stream=False):
        super().__init__(wrapped)
        n = kwargs.get("n", 1) or 1
        self._dd_span = span
        self._streamed_chunks = [[] for _ in range(n)]
        self._dd_integration = integration
        self._kwargs = kwargs
        self._args = args
        self._is_message_stream = (
            is_message_stream  # message stream helper will have accumulated the message for us already
        )


class TracedOpenAIStream(BaseTracedOpenAIStream):
    def __enter__(self):
        self.__wrapped__.__enter__()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.__wrapped__.__exit__(exc_type, exc_val, exc_tb)

    def __iter__(self):
        return self

    def __next__(self):
        try:
            chunk = self.__wrapped__.__next__()
            _loop_handler(self._dd_span, chunk, self._streamed_chunks)
            return chunk
        except StopIteration:
            _process_finished_stream(
                self._dd_integration,
                self._dd_span,
                self._args,
                self._kwargs,
                self._streamed_chunks,
                self._is_message_stream,
            )
            self._dd_span.finish()
            self._dd_integration.metric(self._dd_span, "dist", "request.duration", self._dd_span.duration_ns)
            raise
        except Exception:
            self._dd_span.set_exc_info(*sys.exc_info())
            self._dd_span.finish()
            self._dd_integration.metric(self._dd_span, "dist", "request.duration", self._dd_span.duration_ns)
            raise

    def __stream_text__(self):
        for chunk in self:
            if chunk.type == "content_block_delta" and chunk.delta.type == "text_delta":
                yield chunk.delta.text


class TracedOpenAIStreamManager(BaseTracedOpenAIStream):
    def __enter__(self):
        self.__wrapped__.__enter__()
        stream = TracedOpenAIStream(
            self.__wrapped__._MessageStreamManager__stream,
            self._dd_integration,
            self._dd_span,
            self._args,
            self._kwargs,
            is_message_stream=True,
        )
        stream.text_stream = stream.__stream_text__()
        return stream


class TracedOpenAIAsyncStream(BaseTracedOpenAIStream):
    async def __aenter__(self):
        await self.__wrapped__.__aenter__()
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        await self.__wrapped__.__aexit__(exc_type, exc_val, exc_tb)

    def __aiter__(self):
        return self

    async def __anext__(self):
        try:
            chunk = await self.__wrapped__.__anext__()
            _loop_handler(self._dd_span, chunk, self._streamed_chunks)
            return chunk
        except StopAsyncIteration:
            _process_finished_stream(
                self._dd_integration,
                self._dd_span,
                self._args,
                self._kwargs,
                self._streamed_chunks,
                self._is_message_stream,
            )
            self._dd_span.finish()
            self._dd_integration.metric(self._dd_span, "dist", "request.duration", self._dd_span.duration_ns)
            raise
        except Exception:
            self._dd_span.set_exc_info(*sys.exc_info())
            self._dd_span.finish()
            self._dd_integration.metric(self._dd_span, "dist", "request.duration", self._dd_span.duration_ns)
            raise


def _construct_unaccumulated_messages(streamed_chunks):
    """Iteratively build up a list of messages."""
    messages = []
    message = {}
    message_complete = True
    prev_message = None
    for chunk in streamed_chunks:
        # if message wasn't completed, use the message in the function as we are still completing it
        if not message_complete:
            prev_message = message
        else:
            prev_message = None
        message, message_complete = _construct_message_from_streamed_chunks(chunk, prev_message=prev_message)

        if message_complete:
            messages.append(message)
    return messages


def _construct_accumulated_messages(streamed_chunks):
    """Iteratively build up a list of messages."""
    messages = []
    for chunk in streamed_chunks:
        if chunk and chunk.type == "message_start" and chunk.message:
            for content in chunk.message.content:
                if content.type == "text":
                    messages.append(
                        {
                            "content": content.text,
                            "role": chunk.message.role,
                            "finish_reason": chunk.message.stop_reason,
                            "usage": {
                                "input": chunk.message.usage.input_tokens,
                                "output": chunk.message.usage.output_tokens,
                            },
                        }
                    )
    return messages


def _construct_message_from_streamed_chunks(chunk, prev_message=None) -> Tuple[Dict[str, str], bool]:
    """Constructs a chat message dictionary from streamed chunks.
    The resulting message dictionary is of form {"content": "...", "role": "...", "finish_reason": "..."}
    """
    message = prev_message if prev_message is not None else {"content": ""}
    message_finished = False
    if getattr(chunk, "type", "") == "message_start":
        # this is the starting chunk
        chunk_content = getattr(chunk.message, "content", "")
        chunk_role = getattr(chunk.message, "role", "")
        chunk_usage = getattr(chunk.message, "usage", "")
        if chunk_content:
            message["content"] += chunk_content
        if chunk_role:
            message["role"] = chunk_role
        if chunk_usage:
            message["usage"] = {}
            message["usage"]["input"] = getattr(chunk_usage, "input_tokens", 0)
            message["usage"]["output"] = getattr(chunk_usage, "output_tokens", 0)
    elif getattr(chunk, "delta", None):
        # delta events contain new content
        content_block = chunk.delta
        if getattr(content_block, "type", "") == "text_delta":
            chunk_content = getattr(content_block, "text", "")
            if chunk_content:
                message["content"] += chunk_content

        elif getattr(chunk, "type", "") == "message_delta":
            # message delta events signal the end of the message
            chunk_finish_reason = getattr(content_block, "stop_reason", "")
            if chunk_finish_reason:
                message["finish_reason"] = content_block.stop_reason
                message["content"] = message["content"].strip()

                chunk_usage = getattr(chunk, "usage", {})
                if chunk_usage:
                    message_usage = message.get("usage", {"output": 0, "input": 0})
                    message_usage["output"] += getattr(chunk_usage, "output_tokens", 0)
                    message_usage["input"] += getattr(chunk_usage, "input_tokens", 0)
                    message["usage"] = message_usage
                message_finished = True

    return message, message_finished


def _tag_streamed_chat_completion_response(integration, span, messages):
    """Tagging logic for streamed chat completions."""
    if messages is None:
        return
    for idx, message in enumerate(messages):
        span.set_tag_str("anthropic.response.completions.%d.content" % idx, integration.trunc(message["content"]))
        span.set_tag_str("anthropic.response.completions.%d.role" % idx, message["role"])
        if message.get("finish_reason", None) is not None:
            span.set_tag_str("anthropic.response.completions.%d.finish_reason" % idx, message["finish_reason"])
        if message.get("usage", None) is not None:
            span.set_metric("anthropic.response.completions.%d.usage.input" % idx, message["usage"].get("input", 0))
            span.set_metric("anthropic.response.completions.%d.usage.output" % idx, message["usage"].get("output", 0))


def _loop_handler(span, chunk, streamed_chunks):
    """Sets the anthropic model tag and appends the chunk to the correct index in the streamed_chunks list.

    When handling a streamed chat/completion response, this function is called for each chunk in the streamed response.
    """
    if span.get_tag("anthropic.response.model") is None:
        span.set_tag("anthropic.response.model", chunk.message.model)
    streamed_chunks.append(chunk)


def _is_generator(resp):
    # type: (...) -> bool
    import anthropic

    if isinstance(resp, Generator):
        return True
    if hasattr(anthropic, "Stream") and isinstance(resp, anthropic.Stream):
        return True
    return False


def _is_async_generator(resp):
    # type: (...) -> bool
    import anthropic

    if isinstance(resp, AsyncGenerator):
        return True
    if hasattr(anthropic, "AsyncStream") and isinstance(resp, anthropic.AsyncStream):
        return True
    return False


def _is_stream_manager(resp):
    # type: (...) -> bool
    import anthropic

    return isinstance(resp, anthropic.lib.streaming._messages.MessageStreamManager)