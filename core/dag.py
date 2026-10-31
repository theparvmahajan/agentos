from collections import defaultdict, deque


class CycleError(ValueError):
    def __init__(self, nodes: list[str]):
        self.nodes = nodes
        super().__init__(f"dependency cycle among or behind tasks: {', '.join(nodes)}")


def topological_order(deps: dict[str, set[str]]) -> list[str]:
    """Order nodes so every node comes after everything it depends on.

    `deps` maps each node to the set of nodes it depends on. Every dependency
    must also appear as a key (callers validate that first). Raises CycleError
    if the graph has a cycle.
    """
    indegree = {node: len(parents) for node, parents in deps.items()}
    children: dict[str, list[str]] = defaultdict(list)
    for node, parents in deps.items():
        for parent in parents:
            children[parent].append(node)

    queue = deque(node for node, degree in indegree.items() if degree == 0)
    order: list[str] = []
    while queue:
        node = queue.popleft()
        order.append(node)
        for child in children[node]:
            indegree[child] -= 1
            if indegree[child] == 0:
                queue.append(child)

    if len(order) != len(deps):
        raise CycleError(sorted(n for n, degree in indegree.items() if degree > 0))
    return order