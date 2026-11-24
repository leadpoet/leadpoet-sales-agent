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


def run_workflow(task, model, complete, *, world=None, max_steps=90, max_tokens=8192,
                 resume=None, record=None):
    """complete(body) must deliver one broker-authorized response or raise."""
    if not callable(complete) or type(max_steps) is not int or not 1 <= max_steps <= 90:
        raise ValueError("invalid workflow configuration")
    if type(max_tokens) is not int or not 1 <= max_tokens <= 8192:
        raise ValueError("invalid workflow token limit")
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
    if resume is not None:
        # Only the trusted operator provides a checkpoint from a verified training replay.
        if resume["tools"] != schemas:
            raise ValueError("checkpoint tool schemas differ from the assigned task")
        messages = copy.deepcopy(resume["messages"])
        if (len(messages) < 2 or messages[0]["role"] != "system"
                or messages[1] != {"role": "user", "content": task["prompt"]}
                or messages[-1]["role"] not in {"user", "tool"}):
            raise ValueError("checkpoint is not at an assistant decision")
        files = copy.deepcopy(resume["files"])
        world.s.clear()
        world.s.update(copy.deepcopy(resume["state"]))
        world.log[:] = copy.deepcopy(resume["log"])
    transcript = copy.deepcopy(messages)
    # Preserve available student reasoning in the private transcript. Only the
    # original messages that were visible to the student go into the next call.
    messages = [{k: v for k, v in m.items() if k not in ("reasoning", "reasoning_content")}
                for m in messages]
    used_ids = {c["id"] for m in messages for c in m.get("tool_calls", [])}
    answer = ""
    finish = "step_limit"
    event_index = 0
    offset = sum(m["role"] == "assistant" for m in messages)

    def emit(kind, **values):
        nonlocal event_index
        if record is not None:
            record(copy.deepcopy({"index": event_index, "kind": kind, **values}))
            event_index += 1

    emit("start", model=model, messages=transcript, tools=schemas, prefix_turns=offset)
    for step in range(max_steps):
        turn = offset + step
        emit("before", turn=turn, prefix_messages=len(messages), state=world.s, files=files, log=world.log)
        try:
            response = complete({"model": model, "messages": copy.deepcopy(messages), "tools": schemas,
                                 "max_tokens": max_tokens})
        except Exception as error:
            emit("interrupted", turn=turn, error_type=type(error).__name__)
            raise
        if response.get("model") != model:
            raise ValueError("provider returned a different model")
        choice, = response["choices"]
        reply = choice["message"]
        visible = {k: reply[k] for k in ("role", "content", "tool_calls", "reasoning", "reasoning_content")
                   if k in reply}
        if record is not None:
            emit("reply", turn=turn, model=response.get("model"), receipt=response.get("id"),
                 usage=response.get("usage"), finish_reason=choice.get("finish_reason"), message=visible)
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
            emit("tool", turn=turn, call_id=identity, name=name, output=result)
            msg = {"role": "tool", "tool_call_id": identity, "content": result[:20000]}
            messages.append(msg)
            transcript.append(msg)
    emit("finish", finish=finish, answer=answer, state=world.s, files=files, log=world.log)
    return {"finish": finish, "model": model, "messages": transcript, "tools": schemas,
            "out": {"files": {p: v for p, v in files.items() if initial.get(p) != v},
                    "answer": answer, "state": world.s, "log": world.log}}
