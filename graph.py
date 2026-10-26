"""Top-level Plan -> Execute/Reviewer network -> replan workflow.

Use build_graph() for custom configuration; graph is the default compiled graph.
Importing this module builds graph structure only, without CLI/API calls.
"""
from pathlib import Path
from langchain_core.runnables import RunnableConfig
from langgraph.graph import END, START, StateGraph

from orchestrator.agents.plan import build_plan_agent
from orchestrator.agents.initialize import build_initialize_agent
from orchestrator.agents.execute import build_execute_agent
from orchestrator.agents.reviewer import build_reviewer_agent
from orchestrator.runtime.network import build_execution_graph, task_signature, message_delta
from orchestrator.runtime.two_phase import CONFIG_ROOT, read_settings
from orchestrator.runtime.status import team_progress
from orchestrator.runtime.artifacts import plan_handoff_artifact
from orchestrator.schemas import PlanGraph
from orchestrator.state import WorkflowState as PlanState


def build_graph(config_path: str | Path = CONFIG_ROOT / 'plan.yaml', *,
                initialize_config_path: str | Path | None = None,
                execute_config_path: str | Path | None = None,
                reviewer_config_path: str | Path | None = None,
                dispatch_execute: bool = True, **llm_options):
    settings = read_settings(config_path)
    max_replans = settings.get('max_replans', 2)
    if isinstance(max_replans, bool) or not isinstance(max_replans, int) or max_replans < 0:
        raise ValueError('max_replans must be a nonnegative integer')
    if max_replans and (not isinstance(settings.get('replan_prompt'), str) or not settings['replan_prompt'].strip()):
        raise ValueError('Missing replan_prompt')
    initialize_options = {'config_path': initialize_config_path} if initialize_config_path else {}
    initializer = build_initialize_agent(**initialize_options, **llm_options)
    planner = build_plan_agent(config_path, **llm_options)
    execute_options = {'config_path': execute_config_path} if execute_config_path else {}
    reviewer_options = {'config_path': reviewer_config_path} if reviewer_config_path else {}
    executor = build_execute_agent(**execute_options, **llm_options) if dispatch_execute else None
    reviewer = build_reviewer_agent(**reviewer_options, **llm_options) if dispatch_execute else None

    def announce_plan(state: PlanState):
        return {'messages': [plan_handoff_artifact(
            attempt=state.get('replan_count', 0) + 1,
            approved_count=len(state.get('approved_work', [])))]}

    def prepare_replan(state: PlanState):
        return {"replan_count": state["replan_count"] + 1, "messages": [team_progress("Team đang lập lại kế hoạch và giữ nguyên công việc đã được Reviewer duyệt.")]}

    def announce_execution(state: PlanState):
        count = len(state['plan']['nodes'])
        return {'messages': [team_progress(f'Bắt đầu thực thi {count} nhiệm vụ; mỗi Execute pass sẽ được Reviewer kiểm tra lại.')]}

    def summarize_execution(state: PlanState):
        results = state['results']
        passed = sum(item['status'] == 'completed' for item in results.values())
        failed = [key for key, item in results.items() if item['status'] == 'failed']
        skipped = [key for key, item in results.items() if item['status'] == 'skipped']
        content = f'Kết quả lần chạy: {passed}/{len(results)} nhiệm vụ đã được Reviewer duyệt.'
        if failed:
            content += ' Chưa đạt: ' + ', '.join(failed) + '.'
        if skipped:
            content += ' Chưa chạy do phụ thuộc: ' + ', '.join(skipped) + '.'
        if state['status'] == 'failed':
            content += (' Team sẽ lập lại kế hoạch.' if state['replan_count'] < max_replans
                        else ' Đã hết lượt lập lại kế hoạch; cần xử lý các trở ngại còn lại.')
        return {'messages': [team_progress(content)]}

    async def run_network(state: PlanState, config: RunnableConfig):
        network = build_execution_graph(PlanGraph.model_validate(state["plan"]), executor,
            reviewer=reviewer, messages=state["messages"], approved_work=state["approved_work"])
        network_config = {**config, "recursion_limit": max(config.get("recursion_limit", 25), len(state["plan"]["nodes"]) + 2)}
        result = await network.ainvoke({"workspace": state["workspace"], "results": {}, "messages": state["messages"]}, config=network_config)
        success = all(item["status"] == "completed" for item in result["results"].values())
        approved = {task_signature(item["task"]): item for item in state["approved_work"]}
        for task in state["plan"]["nodes"]:
            entry = result["results"][task["id"]]
            if entry["status"] == "completed" and entry.get("review", {}).get("assessment", {}).get("status") == "pass":
                approved[task_signature(task)] = {"task": task, "result": entry}
        history = [*state["execution_history"], {
            "attempt": state["replan_count"] + 1,
            "natural_plan": state["natural_plan"], "plan": state["plan"],
            "results": result["results"],
        }]
        error = ""
        if not success:
            error = "Some Execute nodes failed, blocked or were skipped"
            if state["replan_count"] >= max_replans:
                error += f"; replan limit reached ({max_replans})"
        return {"results": result["results"], "execution_history": history, "approved_work": list(approved.values()),
                "status": "completed" if success else "failed", "error": error,
                "messages": message_delta(result.get("messages", []), state["messages"])}

    workflow = StateGraph(PlanState)
    workflow.add_node('initialize', initializer)
    workflow.add_node('announce_plan', announce_plan)
    workflow.add_node('plan', planner)
    workflow.add_edge('announce_plan', 'plan')
    workflow.add_edge(START, 'initialize')
    workflow.add_conditional_edges('initialize', lambda s: 'announce_plan'
        if s['status'] == 'ready_for_plan' else END, ['announce_plan', END])
    if dispatch_execute:
        workflow.add_node('announce_execution', announce_execution)
        workflow.add_node('execute_network', run_network)
        workflow.add_node('summarize_execution', summarize_execution)
        workflow.add_node('prepare_replan', prepare_replan)
        workflow.add_conditional_edges('plan', lambda s: 'announce_execution' if s['status'] == 'planned' else END, ['announce_execution', END])
        workflow.add_edge('announce_execution', 'execute_network')
        workflow.add_edge('execute_network', 'summarize_execution')
        workflow.add_conditional_edges('summarize_execution', lambda s: 'prepare_replan'
            if s['status'] == 'failed' and s['replan_count'] < max_replans else END, ['prepare_replan', END])
        workflow.add_edge('prepare_replan', 'announce_plan')
    else:
        workflow.add_edge('plan', END)
    return workflow.compile().with_config(recursion_limit=max(25, 7 * (max_replans + 1) + 5))


graph = build_graph()
