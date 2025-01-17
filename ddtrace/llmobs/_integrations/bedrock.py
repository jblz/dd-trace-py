from typing import Any
from typing import Dict
from typing import List
from typing import Optional

from ddtrace._trace.span import Span
from ddtrace.internal.logger import get_logger
from ddtrace.llmobs._constants import INPUT_MESSAGES
from ddtrace.llmobs._constants import INPUT_TOKENS_METRIC_KEY
from ddtrace.llmobs._constants import METADATA
from ddtrace.llmobs._constants import METRICS
from ddtrace.llmobs._constants import MODEL_NAME
from ddtrace.llmobs._constants import MODEL_PROVIDER
from ddtrace.llmobs._constants import OUTPUT_MESSAGES
from ddtrace.llmobs._constants import OUTPUT_TOKENS_METRIC_KEY
from ddtrace.llmobs._constants import PARENT_ID_KEY
from ddtrace.llmobs._constants import PROPAGATED_PARENT_ID_KEY
from ddtrace.llmobs._constants import SPAN_KIND
from ddtrace.llmobs._constants import TOTAL_TOKENS_METRIC_KEY
from ddtrace.llmobs._integrations import BaseLLMIntegration
from ddtrace.llmobs._utils import _get_llmobs_parent_id
from ddtrace.llmobs._utils import safe_json


log = get_logger(__name__)


class BedrockIntegration(BaseLLMIntegration):
    _integration_name = "bedrock"

    def _llmobs_set_tags(
        self,
        span: Span,
        args: List[Any],
        kwargs: Dict[str, Any],
        response: Optional[Any] = None,
        operation: str = "",
    ) -> None:
        """Extract prompt/response tags from a completion and set them as temporary "_ml_obs.*" tags."""
        if span.get_tag(PROPAGATED_PARENT_ID_KEY) is None:
            parent_id = _get_llmobs_parent_id(span) or "undefined"
            span.set_tag(PARENT_ID_KEY, parent_id)
        parameters = {}
        if span.get_tag("bedrock.request.temperature"):
            parameters["temperature"] = float(span.get_tag("bedrock.request.temperature") or 0.0)
        if span.get_tag("bedrock.request.max_tokens"):
            parameters["max_tokens"] = int(span.get_tag("bedrock.request.max_tokens") or 0)

        prompt = kwargs.get("prompt", "")
        input_messages = self._extract_input_message(prompt)

        span.set_tag_str(SPAN_KIND, "llm")
        span.set_tag_str(MODEL_NAME, span.get_tag("bedrock.request.model") or "")
        span.set_tag_str(MODEL_PROVIDER, span.get_tag("bedrock.request.model_provider") or "")

        span.set_tag_str(INPUT_MESSAGES, safe_json(input_messages))
        span.set_tag_str(METADATA, safe_json(parameters))
        if span.error or response is None:
            span.set_tag_str(OUTPUT_MESSAGES, safe_json([{"content": ""}]))
        else:
            output_messages = self._extract_output_message(response)
            span.set_tag_str(OUTPUT_MESSAGES, safe_json(output_messages))
        metrics = self._llmobs_metrics(span, response)
        span.set_tag_str(METRICS, safe_json(metrics))

    @staticmethod
    def _llmobs_metrics(span: Span, response: Optional[Dict[str, Any]]) -> Dict[str, Any]:
        metrics = {}
        if response and response.get("text"):
            prompt_tokens = int(span.get_tag("bedrock.usage.prompt_tokens") or 0)
            completion_tokens = int(span.get_tag("bedrock.usage.completion_tokens") or 0)
            metrics[INPUT_TOKENS_METRIC_KEY] = prompt_tokens
            metrics[OUTPUT_TOKENS_METRIC_KEY] = completion_tokens
            metrics[TOTAL_TOKENS_METRIC_KEY] = prompt_tokens + completion_tokens
        return metrics

    @staticmethod
    def _extract_input_message(prompt):
        """Extract input messages from the stored prompt.
        Anthropic allows for messages and multiple texts in a message, which requires some special casing.
        """
        if isinstance(prompt, str):
            return [{"content": prompt}]
        if not isinstance(prompt, list):
            log.warning("Bedrock input is not a list of messages or a string.")
            return [{"content": ""}]
        input_messages = []
        for p in prompt:
            content = p.get("content", "")
            if isinstance(content, list) and isinstance(content[0], dict):
                for entry in content:
                    if entry.get("type") == "text":
                        input_messages.append({"content": entry.get("text", ""), "role": str(p.get("role", ""))})
                    elif entry.get("type") == "image":
                        # Store a placeholder for potentially enormous binary image data.
                        input_messages.append({"content": "([IMAGE DETECTED])", "role": str(p.get("role", ""))})
            else:
                input_messages.append({"content": content, "role": str(p.get("role", ""))})
        return input_messages

    @staticmethod
    def _extract_output_message(response):
        """Extract output messages from the stored response.
        Anthropic allows for chat messages, which requires some special casing.
        """
        if isinstance(response["text"], str):
            return [{"content": response["text"]}]
        if isinstance(response["text"], list):
            if isinstance(response["text"][0], str):
                return [{"content": str(content)} for content in response["text"]]
            if isinstance(response["text"][0], dict):
                return [{"content": response["text"][0].get("text", "")}]
