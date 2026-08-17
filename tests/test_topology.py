"""Topology constraint tests (CLAUDE.md red line 2: safety comes from topology, asserted, not commented).

Constraint 1: execute_action's only inbound edge is the approved branch of await_decision
Constraint 2: no START -> execute_action path bypasses interrupt (await_decision)
Constraint 3: the ticket-action MCP client/address appears only inside the execute_action body
Also: await_decision really does call interrupt; request_approval is unavoidable before the gate
"""

import ast
import sys
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv  # noqa: E402

load_dotenv(ROOT / ".env")

from agent.graph import build_graph  # noqa: E402


def _edges():
    g = build_graph()  # no checkpointer needed to inspect the topology
    return list(g.get_graph().edges)


def _adjacency(edges, exclude: set[str] = frozenset()):
    adj = defaultdict(set)
    for e in edges:
        if e.source in exclude or e.target in exclude:
            continue
        adj[e.source].add(e.target)
    return adj


def _reachable(adj, start: str) -> set[str]:
    seen, stack = set(), [start]
    while stack:
        n = stack.pop()
        if n in seen:
            continue
        seen.add(n)
        stack.extend(adj.get(n, ()))
    return seen


def test_constraint1_execute_action_single_inbound_edge():
    incoming = [e for e in _edges() if e.target == "execute_action"]
    assert len(incoming) == 1, f"execute_action must have exactly one inbound edge, got {incoming}"
    assert incoming[0].source == "await_decision", f"inbound edge must come from await_decision, got {incoming[0]}"
    assert incoming[0].conditional, "inbound edge must be conditional (the approved branch), not a direct wire"


def test_constraint2_no_path_bypassing_interrupt():
    edges = _edges()
    # delete await_decision (the node holding interrupt) and execute_action must go unreachable from START
    adj = _adjacency(edges, exclude={"await_decision"})
    reach = _reachable(adj, "__start__")
    assert "execute_action" not in reach, \
        f"a path around interrupt exists! reachable set: {sorted(reach)}"
    # same with request_approval (the approvals-table write) gone -- the approval record is not skippable either
    adj2 = _adjacency(edges, exclude={"request_approval"})
    reach2 = _reachable(adj2, "__start__")
    assert "execute_action" not in reach2, "a path around request_approval exists!"
    # on the real topology execute_action IS reachable (keeps this test honest: the graph isn't just broken)
    full = _reachable(_adjacency(edges), "__start__")
    assert "execute_action" in full, "execute_action unreachable, so the topology test proves nothing"


def _module_ast(path: Path) -> ast.Module:
    return ast.parse(path.read_text())


def _function_def(tree: ast.Module, name: str) -> ast.FunctionDef:
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return node
    raise AssertionError(f"function {name} not found")


def test_constraint3_action_client_only_in_execute_action():
    """The action server's capability handles (URL env name / port) may only appear inside execute_action.

    Read the scope carefully: guards.py's injection regexes **mention** tool names so they can spot
    attack text; that is detection, not invocation. Capability = action server address + mcp_call.
    """
    agent_dir = ROOT / "agent"
    markers = ("MCP_ACTION_URL", "8102")
    for py in agent_dir.glob("*.py"):
        tree = _module_ast(py)
        exec_range = range(0, 0)
        if py.name == "graph.py":
            fn = _function_def(tree, "execute_action")
            exec_range = range(fn.lineno, fn.end_lineno + 1)
        for node in ast.walk(tree):
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                if any(m in node.value for m in markers):
                    assert py.name == "graph.py" and node.lineno in exec_range, \
                        f"{py.name}:{node.lineno} references action server address {node.value!r}, only allowed inside execute_action"


def test_constraint3b_generic_mcp_call_only_in_execute_action():
    """mcp_call() with an arbitrary URL may only be issued inside execute_action (lookups use lookup_call)."""
    agent_dir = ROOT / "agent"
    for py in agent_dir.glob("*.py"):
        if py.name == "mcp_client.py":  # where it is defined
            continue
        tree = _module_ast(py)
        exec_range = range(0, 0)
        if py.name == "graph.py":
            fn = _function_def(tree, "execute_action")
            exec_range = range(fn.lineno, fn.end_lineno + 1)
        for node in ast.walk(tree):
            if (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                    and node.func.id == "mcp_call"):
                assert py.name == "graph.py" and node.lineno in exec_range, \
                    f"{py.name}:{node.lineno} calls mcp_call() outside execute_action"


def test_await_decision_contains_interrupt_and_no_side_effects_before_it():
    tree = _module_ast(ROOT / "agent" / "graph.py")
    fn = _function_def(tree, "await_decision")
    # interrupt must be the first real statement in the body (after the docstring) -- side effects come after
    stmts = [s for s in fn.body
             if not (isinstance(s, ast.Expr) and isinstance(s.value, ast.Constant))]
    first = stmts[0]
    calls = [n for n in ast.walk(first) if isinstance(n, ast.Call)
             and isinstance(n.func, ast.Name) and n.func.id == "interrupt"]
    assert calls, "await_decision's first statement must call interrupt() (no side effects before it)"


def test_no_other_module_imports_action_tools():
    """Peripheral code (api/, the evals runner) must not reach the action server directly either, whitelist aside."""
    allowed = {ROOT / "agent" / "graph.py", ROOT / "mcp_action" / "server.py"}
    scan_dirs = [ROOT / "agent", ROOT / "api", ROOT / "llm"]
    for d in scan_dirs:
        if not d.exists():
            continue
        for py in d.rglob("*.py"):
            if py in allowed:
                continue
            text = py.read_text()
            assert "MCP_ACTION_URL" not in text and ":8102" not in text, \
                f"{py} references the action server address"
