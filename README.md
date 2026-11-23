# Leadpoet sales agent

The operator calls `harness.run_task(task, model=assigned_model, ...)` on an exact revision of this repository. The `lab` branch contains the November 18 Arena champion and the synthetic workflow runner. Registration, assignments, pricing, budgets, receipts and verifier decisions belong to the subnet.

* **lead_research**: supply the ICP in `task["icp"]` inside the normal Arena execute sandbox. Tyche uses the assigned model for every Codex session. The gateway controls provider routing. Existing competition runners can still call `run_icp(icp)`.
* **workflow**: supply the synthetic prompt, files, initial state and allowed tools in `task["scenario"]`. The host supplies a budgeted `complete(body)` callback and optionally a Poet9B world exposing `schemas()`, `call(name, args)`, `s` and `log`. The default world supports chained campaign tasks. File tools and CRM writes operate in memory. A malformed, truncated or mismatched model response cannot complete the task.

The workflow runner is adapted from Poet9B's run loop and mock worlds. The host constructs training scenarios using its guarded builders, grades terminal world state, and retains the original messages and tool results privately. Reference answers and grader notes are never part of the model input. Public source contains no held-out scenario packs or operational credentials.

Run the offline control with `python -m unittest discover -s tests` from this directory. It exercises a real mock-world write, a tool continuation, a final answer, an incorrect returned model, and research dispatch.
