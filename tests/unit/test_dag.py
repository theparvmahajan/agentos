import pytest

from core.dag import CycleError, topological_order


def test_chain_is_ordered():
    deps = {"c": {"b"}, "b": {"a"}, "a": set()}
    assert topological_order(deps) == ["a", "b", "c"]


def test_every_node_comes_after_its_dependencies():
    deps = {"d": {"b", "c"}, "b": {"a"}, "c": {"a"}, "a": set()}
    order = topological_order(deps)
    for node, parents in deps.items():
        for parent in parents:
            assert order.index(parent) < order.index(node)


def test_independent_nodes_keep_input_order():
    deps = {"x": set(), "y": set(), "z": set()}
    assert topological_order(deps) == ["x", "y", "z"]


def test_empty_graph():
    assert topological_order({}) == []


def test_cycle_is_detected():
    with pytest.raises(CycleError) as exc:
        topological_order({"a": {"b"}, "b": {"a"}})
    assert exc.value.nodes == ["a", "b"]


def test_self_dependency_is_a_cycle():
    with pytest.raises(CycleError):
        topological_order({"a": {"a"}})


def test_nodes_behind_a_cycle_are_reported():
    deps = {"a": set(), "b": {"c"}, "c": {"b"}, "d": {"c"}}
    with pytest.raises(CycleError) as exc:
        topological_order(deps)
    assert exc.value.nodes == ["b", "c", "d"]