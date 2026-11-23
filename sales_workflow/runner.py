"""Run a guarded Poet9B scenario with an injected, metered model transport."""

import copy
import json

from .chain_world import ChainWorld
from .file_tools import FILE_TOOLS


def _file_call(files, name, args):
    if name == "list_files":
        return json.dumps(sorted(files))
    path = args.get("path")
    if not isinstance(path, str) or not path or len(path) > 255:
        return "error: invalid file name"
    if name == "read_file":
        return files.get(path, "error: no such file")
    content = args.get("content")
    if not isinstance(content, str) or len(content) > 100_000 or (path not in files and len(files) >= 256):
        return "error: file limit exceeded"
    files[path] = content
    return f"saved {path}"


def run_workflow(task, model, complete, *, world=None, max_steps=90):
    """complete(body) must deliver one broker-authorized response or raise."""
    if not callable(complete) or type(max_steps) is not int or not 1 <= max_steps <= 90:
        raise ValueError("invalid workflow configuration")
    if world is None:
        world = ChainWorld(task.get("state") or {}, task.get("tools") or [])
    initial = task["files"]
    files = dict(initial)
    schemas = FILE_TOOLS + world.schemas()
    tools = {schema["function"]["name"] for schema in schemas}
    file_names = {schema["function"]["name"] for schema in FILE_TOOLS}
    messages = [
        {"role": "system", "content": "Complete this sales workflow with the available workspace tools. "
         "Read the job instructions and use the sources provided. Finish with your answer."},
        {"role": "user", "content": task["prompt"]},
    ]
    transcript = copy.deepcopy(messages)
    used_ids = set()
    answer = ""
    finish = "step_limit"
    for _ in range(max_steps):
        response = complete({"model": model, "messages": copy.deepcopy(messages), "tools": schemas,
                             "max_tokens": 8192})
        if response.get("model") != model:
            raise ValueError("provider returned a different model")
        choice, = response["choices"]
        reply = choice["message"]
        if reply.get("role") != "assistant" or choice.get("finish_reason") not in {"stop", "tool_calls"}:
            raise ValueError("incomplete model reply")
        calls = reply.get("tool_calls") or []
        if len(calls) > 32 or (choice["finish_reason"] == "tool_calls") != bool(calls):
            raise ValueError("invalid model tool calls")
        message = {"role": "assistant", "content": reply.get("content") or ""}
        if not isinstance(message["content"], str):
            raise ValueError("unsupported assistant content")
        if calls:
            message["tool_calls"] = calls
        messages.append(message)
        entry = dict(message)
        for field in ("reasoning", "reasoning_content"):
            if isinstance(reply.get(field), str) and reply[field]:
                entry[field] = reply[field]
        transcript.append(entry)
        if not calls:
            if not message["content"].strip():
                raise ValueError("empty model answer")
            finish, answer = "answered", message["content"]
            break
        for call in calls:
            identity, function = call["id"], call["function"]
            if call.get("type") != "function" or not isinstance(identity, str) or not identity or identity in used_ids:
                raise ValueError("invalid tool call identity")
            used_ids.add(identity)
            name = function["name"]
            try:
                args = json.loads(function["arguments"])
                if not isinstance(args, dict):
                    raise ValueError
            except (TypeError, ValueError):
                result = "error: arguments must be a JSON object"
            else:
                if name not in tools:
                    result = "error: unknown tool"
                elif name in file_names:
                    result = _file_call(files, name, args)
                else:
                    result = world.call(name, args)
            msg = {"role": "tool", "tool_call_id": identity, "content": result[:20000]}
            messages.append(msg)
            transcript.append(msg)
    return {"finish": finish, "model": model, "messages": transcript, "tools": schemas,
            "out": {"files": {p: v for p, v in files.items() if initial.get(p) != v},
                    "answer": answer, "state": world.s, "log": world.log}}
