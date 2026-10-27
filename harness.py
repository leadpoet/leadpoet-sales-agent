"""Leadpoet Arena's synchronous, single-input entrypoint.

Where research control lives, for source review:
- tyche_arena/host.py: run() and _research() open Leadpoet Codex sessions (runtime.session) with TYCHE's MCP tools
  (tool_configuration) and hosted web search off. The model's output chooses each tool call and its inputs.
- Observation to action: each model tool call reaches tyche_tools.py call(), and ResearchTools.call() in
  .agents/skills/lead-sourcing/scripts/research_tools.py dispatches it by name. lookup() runs the provider call the
  model specified (tool, inputs, target) and returns the result to the model; claim() and review() save the company
  and judgments the model chose from those results.
- Finish versus continue: the model calls tyche_finish (ResearchTools.finish) when it decides the evidence is enough,
  or keeps researching. supervise_worker() in scripts/codex_tyche.py starts another Codex turn only while no
  validated output exists and time remains; code otherwise stops research only at the deadline or budget limit.
- Decision summaries: tyche_arena/tools.py reports the model's accept, reject and defer decisions, its first
  discovery search and its final review with Leadpoet's log_decision, using the text the model wrote in those calls.
"""


def run_icp(icp: dict) -> list[dict]:
    from tyche_arena.host import run

    return run(icp)
