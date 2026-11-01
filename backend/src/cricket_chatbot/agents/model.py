"""A Strands model provider that talks to Ollama through the app's OllamaClient.

Going through OllamaClient (not Strands' own Ollama provider) keeps everything the rest of the app relies on:
the circuit breaker and health status, failure injection from the dashboard, timeouts, Prometheus token
metrics, OpenTelemetry spans, and the fake LLM in the test suite.
"""

import json
import uuid
from collections.abc import AsyncGenerator
from typing import Any, TypeVar

from pydantic import BaseModel
from strands.models.model import Model
from strands.types.content import ContentBlock, Messages
from strands.types.streaming import StreamEvent
from strands.types.tools import ToolChoice, ToolSpec

from ..llm.ollama import OllamaClient
from ..observability.agent_runs import observer

T = TypeVar("T", bound=BaseModel)


class OllamaClientModel(Model):
    def __init__(self, client: OllamaClient, *, model_id: str, think: bool = False, keep_alive: str = "30m",
                 options: dict | None = None):
        self.client = client
        self.config: dict[str, Any] = {"model_id": model_id, "think": think, "keep_alive": keep_alive, "options": options or {}}

    def update_config(self, **model_config: Any) -> None:
        self.config.update(model_config)

    def get_config(self) -> dict[str, Any]:
        return self.config

    # --- Strands messages -> Ollama messages ---------------------------------------------
    @staticmethod
    def _content(role: str, block: ContentBlock) -> list[dict]:
        if "text" in block:
            return [{"role": role, "content": block["text"]}]
        if "toolUse" in block:
            return [{"role": role, "content": "", "tool_calls": [
                {"function": {"name": block["toolUse"]["name"], "arguments": block["toolUse"]["input"]}}]}]
        if "toolResult" in block:
            out = []
            for c in block["toolResult"]["content"]:
                text = json.dumps(c["json"], ensure_ascii=False) if "json" in c else c.get("text", "")
                out.append({"role": "tool", "content": text})
            return out
        return []  # images, reasoning blocks etc. are not sent back to Ollama

    def format_messages(self, messages: Messages, system_prompt: str | None) -> list[dict]:
        out = [{"role": "system", "content": system_prompt}] if system_prompt else []
        for m in messages:
            for block in m["content"]:
                out.extend(self._content(m["role"], block))
        return out

    @staticmethod
    def format_tools(tool_specs: list[ToolSpec] | None) -> list[dict] | None:
        if not tool_specs:
            return None
        return [{"type": "function", "function": {"name": t["name"], "description": t["description"],
                                                  "parameters": t["inputSchema"]["json"]}} for t in tool_specs]

    # --- Ollama chunks -> Strands stream events -------------------------------------------
    async def stream(self, messages: Messages, tool_specs: list[ToolSpec] | None = None, system_prompt: str | None = None,
                     *, tool_choice: ToolChoice | None = None, **kwargs: Any) -> AsyncGenerator[StreamEvent, None]:
        cfg = self.config
        yield {"messageStart": {"role": "assistant"}}
        tool_used, text_open, reasoning_open, last = False, False, False, {}
        try:
            stream = self._chunks(cfg, messages, tool_specs, system_prompt)
            async for chunk in stream:
                msg = chunk.get("message") or {}
                if msg.get("thinking"):
                    if not reasoning_open:
                        yield {"contentBlockStart": {"start": {}}}
                        reasoning_open = True
                    yield {"contentBlockDelta": {"delta": {"reasoningContent": {"text": msg["thinking"]}}}}
                if msg.get("content"):
                    if reasoning_open:
                        yield {"contentBlockStop": {}}
                        reasoning_open = False
                    if not text_open:
                        yield {"contentBlockStart": {"start": {}}}
                        text_open = True
                    yield {"contentBlockDelta": {"delta": {"text": msg["content"]}}}
                for call in msg.get("tool_calls") or []:
                    fn = call.get("function", {})
                    args = fn.get("arguments", {})
                    if isinstance(args, str):
                        try:
                            args = json.loads(args)
                        except json.JSONDecodeError:
                            args = {}
                    for blk in (text_open, reasoning_open):
                        if blk:
                            yield {"contentBlockStop": {}}
                    text_open = reasoning_open = False
                    yield {"contentBlockStart": {"start": {"toolUse": {"name": fn.get("name", ""), "toolUseId": f"tooluse_{uuid.uuid4().hex[:20]}"}}}}
                    yield {"contentBlockDelta": {"delta": {"toolUse": {"input": json.dumps(args)}}}}
                    yield {"contentBlockStop": {}}
                    tool_used = True
                if chunk.get("done"):
                    last = chunk
        except BaseException as e:
            if not isinstance(e, GeneratorExit):
                observer.note_llm(cfg["model_id"], ok=False)  # attributed to the agent run in progress
            raise
        observer.note_llm(cfg["model_id"], last.get("prompt_eval_count", 0) or 0, last.get("eval_count", 0) or 0)
        if text_open or reasoning_open:
            yield {"contentBlockStop": {}}
        reason = "tool_use" if tool_used else ("max_tokens" if last.get("done_reason") == "length" else "end_turn")
        yield {"messageStop": {"stopReason": reason}}
        pin, pout = last.get("prompt_eval_count", 0) or 0, last.get("eval_count", 0) or 0
        yield {"metadata": {"usage": {"inputTokens": pin, "outputTokens": pout, "totalTokens": pin + pout},
                            "metrics": {"latencyMs": int((last.get("total_duration") or 0) / 1e6)}}}

    def _chunks(self, cfg, messages, tool_specs, system_prompt):
        return self.client.chat_stream(model=cfg["model_id"], messages=self.format_messages(messages, system_prompt),
                                       tools=self.format_tools(tool_specs), think=cfg["think"],
                                       options=cfg["options"], keep_alive=cfg["keep_alive"])

    async def structured_output(self, output_model: type[T], prompt: Messages, system_prompt: str | None = None,
                                **kwargs: Any) -> AsyncGenerator[dict[str, T | Any], None]:
        """JSON-mode fallback: ask for JSON matching the schema and validate it."""
        schema = json.dumps(output_model.model_json_schema())
        sys = (system_prompt or "") + f"\nReply with JSON only, matching this schema: {schema}"
        text = ""
        async for ev in self.stream(prompt, None, sys):
            text += ev.get("contentBlockDelta", {}).get("delta", {}).get("text", "")
        start, end = text.find("{"), text.rfind("}")
        yield {"output": output_model.model_validate_json(text[start:end + 1] if start >= 0 else "{}")}
