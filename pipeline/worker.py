"""New process per call: load the selected snapshot, never stale imported code."""
import argparse
import json
import os
import sys
import time
from datetime import datetime, timezone
from itertools import count
from pathlib import Path


def install_api_diagnostics(path, role, model):
    """Observe HTTP attempts without changing payloads, retries or response handling."""
    import httpx
    original_send = httpx.Client.send
    calls = count(1)

    def save(record):
        try:
            with path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(record, ensure_ascii=False) + "\n")
        except Exception as exc:
            # Diagnostics must never turn a successful inference into a failure.
            print(f"API diagnostics could not be saved: {type(exc).__name__}", file=sys.stderr, flush=True)

    def length(value):
        if isinstance(value, str):
            return len(value)
        if isinstance(value, list):
            return sum(length(part.get("text")) for part in value if isinstance(part, dict))
        return 0

    def nonwhitespace_length(value):
        if isinstance(value, str):
            return sum(not char.isspace() for char in value)
        if isinstance(value, list):
            return sum(nonwhitespace_length(part.get("text")) for part in value if isinstance(part, dict))
        return 0

    def numeric_usage(value):
        if not isinstance(value, dict):
            return {}
        return {key: numeric_usage(item) if isinstance(item, dict) else item
                for key, item in value.items() if isinstance(item, (int, float, dict))}

    def send(client, request, *args, **kwargs):
        record = dict(call_id=next(calls), role=role, requested_model=model,
                      started_at=datetime.now(timezone.utc).isoformat())
        try:
            payload = json.loads(request.content)
            if isinstance(payload, dict):
                for key in ("model", "max_tokens", "max_completion_tokens", "response_format", "reasoning_effort", "thinking", "generationConfig"):
                    if key in payload:
                        record["request_" + key] = payload[key]
        except Exception:
            pass
        save(dict(record, event="request_started"))
        started = time.monotonic()
        try:
            response = original_send(client, request, *args, **kwargs)
        except Exception as exc:
            save(dict(record, event="transport_error", elapsed_seconds=round(time.monotonic() - started, 3),
                      error_type=type(exc).__name__))
            raise
        record.update(event="response", elapsed_seconds=round(time.monotonic() - started, 3),
                      http_status=response.status_code,
                      request_id=response.headers.get("x-request-id") or response.headers.get("request-id")
                                 or response.headers.get("x-goog-request-id"))
        try:
            # Never consume a stream or mutate the response to collect diagnostics.
            data = response.json()
            record["response_type"] = type(data).__name__
            if isinstance(data, dict):
                record.update(response_fields=sorted(data), returned_model=data.get("model", data.get("modelVersion")),
                              usage=numeric_usage(data.get("usage", data.get("usageMetadata", {}))))
                choices = data.get("choices", data.get("candidates", []))
                record["choices_count"] = len(choices) if isinstance(choices, list) else None
                first = choices[0] if isinstance(choices, list) and choices and isinstance(choices[0], dict) else {}
                message = first.get("message", {})
                message = message if isinstance(message, dict) else {}
                content = message.get("content") if "choices" in data else data.get("content")
                parts = first.get("content", {}).get("parts", []) if isinstance(first.get("content"), dict) else []
                if "candidates" in data:
                    content = [part for part in parts if isinstance(part, dict) and not part.get("thought", False)]
                # Count native calls in the same first response used for text diagnostics.
                if "choices" in data:
                    tool_calls = message.get("tool_calls")
                    record["tool_call_count"] = len(tool_calls) if isinstance(tool_calls, list) else 0
                elif "candidates" in data:
                    record["tool_call_count"] = sum(isinstance(part, dict) and "functionCall" in part for part in parts)
                else:
                    record["tool_call_count"] = sum(isinstance(part, dict) and part.get("type") == "tool_use"
                                                    for part in content) if isinstance(content, list) else 0
                record.update(finish_reason=first.get("finish_reason", first.get("finishReason", data.get("stop_reason"))),
                              message_fields=sorted(message), content_type=type(content).__name__,
                              content_length=length(content), content_nonwhitespace_length=nonwhitespace_length(content))
                record["reasoning_lengths"] = {key: length(message[key]) for key in
                                               ("reasoning_content", "reasoning", "thinking", "analysis") if key in message}
                if isinstance(content, list):
                    record["content_block_types"] = [part.get("type") for part in content if isinstance(part, dict)]
                    record["thinking_length"] = sum(length(part.get("thinking")) for part in content if isinstance(part, dict))
                if "candidates" in data:
                    record.update(content_type=type(parts).__name__,
                                  content_length=sum(length(part.get("text")) for part in parts if isinstance(part, dict) and not part.get("thought")),
                                  thinking_length=sum(length(part.get("text")) for part in parts if isinstance(part, dict) and part.get("thought")))
                if isinstance(data.get("error"), dict):
                    record["error_type"] = data["error"].get("type")
                    record["error_code"] = data["error"].get("code")
        except Exception as exc:
            record["response_diagnostic_error"] = type(exc).__name__
        save(record)
        if response.status_code == 402:
            # Fixed controller signal, including for older archived harnesses.
            # SystemExit bypasses ordinary agent/tool exception recovery.
            print("API HTTP 402: insufficient balance; stopping run", file=sys.stderr, flush=True)
            raise SystemExit(78)
        return response

    httpx.Client.send = send
    return original_send


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("request", type=Path)
    args = parser.parse_args()
    request = json.loads(args.request.read_text())
    workspace = Path(request["workspace"])
    from api_config import load_config
    sys.path.insert(0, request.get("runtime_workspace", str(workspace)))
    role = "task_agent" if request["kind"] == "task" else "meta_agent"
    os.environ["RSI_CONFIG_PATH"] = request["model"]["config_path"]
    os.environ["RSI_AGENT_ROLE"] = role
    model = load_config(request["model"]["config_path"], roles=(role,))[role].model
    install_api_diagnostics(Path(request["log_path"]).parent / "api_calls.jsonl",
                            os.environ.get("RSI_AGENT_ROLE"), model)
    kwargs = dict(model=model, chat_history_file=request["log_path"])
    if request["kind"] == "task":
        from harness.task.task_agent import TaskAgent
        prediction, history = TaskAgent(**kwargs).forward(request["inputs"])
        result = dict(prediction=prediction, history=history)
    else:
        if request['stage'] not in {'stability_repair', 'forgetting_repair'}:
            raise ValueError(f"Unknown Meta repair stage: {request['stage']}")
        from harness.meta.improve_agent import ImproveAgent
        history = ImproveAgent(**kwargs).forward(
            repo_path=Path(request.get('candidate_path', workspace / 'harness/task')),
            eval_path=request["history_path"],
            context_paths=request["context_paths"],
            instruction=request["instruction"],
            stage=request["stage"],
            iterations_left=request.get("iterations_left"))
        result = dict(history=history)
    Path(request["result_path"]).write_text(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
