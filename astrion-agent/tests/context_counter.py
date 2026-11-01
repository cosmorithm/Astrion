"""A deterministic synthetic tokenizer for budget tests; not a model tokenizer."""

from context_store import encode


class CodepointCounter:
    identifier = "test-codepoint-v1"

    def count_text(self, text):
        return len(text)

    def count_request(self, messages, tools):
        return self.count_text(encode({"messages": messages, "tools": tools or []}))
