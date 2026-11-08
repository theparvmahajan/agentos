"use client";

import "@xyflow/react/dist/style.css";
import { Background, Controls, Position, ReactFlow, ReactFlowProvider, useReactFlow } from "@xyflow/react";
import type { Edge, NodeTypes } from "@xyflow/react";
import { useEffect, useMemo } from "react";
import { buildGraph } from "@/lib/layout";
import type { Task } from "@/lib/types";
import TaskNode from "./TaskNode";
import type { TaskNodeType } from "./TaskNode";

const nodeTypes: NodeTypes = { task: TaskNode };
// Fit the whole graph in view, but never blow a small one up beyond its natural size.
const FIT = { padding: 0.2, maxZoom: 1 };

interface Props {
  tasks: Task[];
  selectedKey: string | null;
  now: number;
  onSelect: (key: string | null) => void;
}

function Graph({ tasks, selectedKey, now, onSelect }: Props) {
  const { fitView } = useReactFlow();
  const { nodes, edges } = useMemo(() => {
    const graph = buildGraph(tasks, selectedKey, now);
    return {
      edges: graph.edges,
      nodes: graph.nodes.map((node) => ({
        ...node,
        handles: node.handles.map((h) => ({ ...h, position: h.position === "left" ? Position.Left : Position.Right })),
      })),
    };
  }, [tasks, selectedKey, now]);

  // Re-fit the view only when tasks appear or disappear, not on every status change, so the
  // graph does not jump around while the user is looking at it.
  const shape = tasks.map((t) => t.key).join("|");
  useEffect(() => {
    const frame = requestAnimationFrame(() => fitView({ ...FIT, duration: 250 }));
    return () => cancelAnimationFrame(frame);
  }, [shape, fitView]);

  return (
    <ReactFlow
      nodes={nodes as TaskNodeType[]}
      edges={edges as Edge[]}
      nodeTypes={nodeTypes}
      nodesDraggable={false}
      nodesConnectable={false}
      elementsSelectable
      fitView
      fitViewOptions={FIT}
      minZoom={0.25}
      maxZoom={1.5}
      colorMode="system"
      onNodeClick={(_, node) => onSelect(node.id)}
      onPaneClick={() => onSelect(null)}
      aria-label="Task graph"
    >
      <Background gap={20} />
      <Controls showInteractive={false} />
    </ReactFlow>
  );
}

export default function GraphView(props: Props) {
  return (
    <ReactFlowProvider>
      <Graph {...props} />
    </ReactFlowProvider>
  );
}
