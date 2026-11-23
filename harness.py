

def run_icp(icp: dict) -> list[dict]:
    from tyche_arena.host import run

    return run(icp)


def run_task(task: dict, *, model: str, complete=None, world=None, max_steps=90):
    """Dispatch a task from the trusted host. The host owns assignment and budget."""
    if not isinstance(model, str) or "/" not in model or not model.isascii():
        raise ValueError("an assigned provider model id is required")
    if task["type"] == "lead_research":
        from tyche_arena.host import run
        return run(task["icp"], assigned_model=model)
    if task["type"] == "workflow":
        from sales_workflow.runner import run_workflow
        return run_workflow(task["scenario"], model, complete, world=world, max_steps=max_steps)
    raise ValueError("unsupported task type")
