import pytest

from core.dag import topological_order
from scripts.benchmark import (
    SHAPES,
    Result,
    chain,
    critical_path,
    diamond,
    fan_out,
    ideal_makespan,
    render_table,
    speedup,
)


@pytest.mark.parametrize("name", list(SHAPES))
def test_every_shape_is_a_valid_graph_with_dependencies_listed_first(name):
    shape = SHAPES[name]
    topological_order({k: set(v) for k, v in shape.items()})  # raises on a cycle or unknown key
    seen: set[str] = set()
    for key, deps in shape.items():
        assert set(deps) <= seen, f"{key} is listed before one of its dependencies"
        seen.add(key)


@pytest.mark.parametrize(
    ("shape", "tasks", "path"),
    [(chain(4), 4, 4), (diamond(), 4, 3), (fan_out(8), 9, 2), (chain(1), 1, 1)],
)
def test_shape_sizes_and_critical_paths(shape, tasks, path):
    assert len(shape) == tasks
    assert critical_path(shape) == path


@pytest.mark.parametrize(
    ("shape", "workers", "rounds"),
    [
        (chain(4), 1, 4), (chain(4), 4, 4),          # a chain never gets faster
        (diamond(), 1, 4), (diamond(), 2, 3), (diamond(), 4, 3),
        (fan_out(8), 1, 9), (fan_out(8), 2, 5), (fan_out(8), 4, 3), (fan_out(8), 8, 2),
    ],
)
def test_ideal_makespan_counts_rounds(shape, workers, rounds):
    assert ideal_makespan(shape, workers, 0.5) == pytest.approx(rounds * 0.5)


def test_more_workers_never_make_the_ideal_slower():
    for shape in SHAPES.values():
        times = [ideal_makespan(shape, w, 1.0) for w in (1, 2, 4, 8)]
        assert times == sorted(times, reverse=True)


def result(mode, workers, walls, ideal):
    return Result("fan-out-8", mode, workers, 9, 2, ideal, walls)


def test_speedup_is_sequential_over_actual():
    assert speedup(4.5, 1.5) == pytest.approx(3.0)


def test_the_table_reports_median_range_speedup_and_efficiency():
    table = render_table([
        result("sequential executor", 0, [4.6, 4.7, 4.8], 4.5),
        result("scheduler + 4 workers", 4, [1.8, 2.0, 1.9], 1.5),
    ])
    row = next(line for line in table.splitlines() if "4 workers" in line)
    assert "| 1.90 |" in row  # median
    assert "1.80-2.00" in row  # range
    assert "2.47x" in row  # 4.7 / 1.9
    assert "79%" in row  # 1.5 / 1.9
    assert table.count("\n") == 3  # header, separator, 2 rows
