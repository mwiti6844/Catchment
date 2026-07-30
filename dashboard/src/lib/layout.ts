import type { TagGraph, TagNode } from "./api";

/**
 * Layered layout for the tag graph.
 *
 * The taxonomy is a DAG with an explicit direction — broader to narrower — and
 * the API already hands back that direction as a signed `level`. So the graph
 * is drawn in columns rather than by a force simulation: a physics layout would
 * throw away the one piece of structure that is actually known, and would place
 * the same graph differently on every reload, which makes drift impossible to
 * see. Columns also mean no layout library.
 *
 * Pure functions over plain data — no DOM, no React — so the geometry can be
 * reasoned about (and, if it ever earns a test runner, tested) on its own.
 */

export const NODE_W = 170;
export const NODE_H = 40;
export const COL_GAP = 78;
export const ROW_GAP = 14;
export const PAD = 24;

export type PlacedNode = { node: TagNode; x: number; y: number };
export type PlacedEdge = { key: string; path: string; backwards: boolean };
export type Layout = {
  nodes: PlacedNode[];
  edges: PlacedEdge[];
  columns: { level: number; x: number; count: number }[];
  width: number;
  height: number;
};

/** Group nodes by level, ordering each column deterministically. */
function columnsOf(nodes: TagNode[]): Map<number, TagNode[]> {
  const byLevel = new Map<number, TagNode[]>();
  for (const node of nodes) {
    byLevel.set(node.level, [...(byLevel.get(node.level) ?? []), node]);
  }
  for (const [level, group] of byLevel) {
    // Busiest first, then by label: identical data must produce an identical
    // picture, or a change in the drawing stops meaning a change in the graph.
    byLevel.set(
      level,
      [...group].sort(
        (a, b) => b.item_count - a.item_count || a.label.localeCompare(b.label),
      ),
    );
  }
  return byLevel;
}

function columnHeight(count: number): number {
  return count * NODE_H + Math.max(0, count - 1) * ROW_GAP;
}

function edgePath(from: PlacedNode, to: PlacedNode): PlacedEdge {
  const y1 = from.y + NODE_H / 2;
  const y2 = to.y + NODE_H / 2;
  const backwards = to.x <= from.x;

  if (backwards) {
    // A sibling or upward edge. Bulging out to the right keeps it visible
    // instead of hiding it underneath the boxes it connects.
    const out = from.x + NODE_W + 34;
    return {
      key: `${from.node.id}->${to.node.id}`,
      path: `M ${from.x + NODE_W} ${y1} C ${out} ${y1}, ${out} ${y2}, ${to.x + NODE_W} ${y2}`,
      backwards: true,
    };
  }

  const x1 = from.x + NODE_W;
  const x2 = to.x;
  const bend = Math.max(26, (x2 - x1) / 2);
  return {
    key: `${from.node.id}->${to.node.id}`,
    path: `M ${x1} ${y1} C ${x1 + bend} ${y1}, ${x2 - bend} ${y2}, ${x2} ${y2}`,
    backwards: false,
  };
}

export function layout(graph: TagGraph): Layout {
  const byLevel = columnsOf(graph.nodes);
  const levels = [...byLevel.keys()].sort((a, b) => a - b);

  const tallest = Math.max(
    1,
    ...levels.map((level) => columnHeight(byLevel.get(level)!.length)),
  );
  const height = tallest + PAD * 2;

  const placed: PlacedNode[] = [];
  const columns: Layout["columns"] = [];

  levels.forEach((level, index) => {
    const group = byLevel.get(level)!;
    const x = PAD + index * (NODE_W + COL_GAP);
    // Columns are centred against each other so the seed sits on the eye line
    // rather than at the top of a lopsided diagram.
    const top = PAD + (tallest - columnHeight(group.length)) / 2;

    columns.push({ level, x, count: group.length });
    group.forEach((node, row) => {
      placed.push({ node, x, y: top + row * (NODE_H + ROW_GAP) });
    });
  });

  const positions = new Map(placed.map((entry) => [entry.node.id, entry]));
  const edges = graph.edges.flatMap((edge) => {
    const from = positions.get(edge.parent);
    const to = positions.get(edge.child);
    // The API only returns edges with both ends in the node set, so a miss here
    // means the two disagreed. Dropping it beats drawing a line to nowhere.
    return from && to ? [edgePath(from, to)] : [];
  });

  const width = PAD * 2 + levels.length * NODE_W + Math.max(0, levels.length - 1) * COL_GAP;
  return { nodes: placed, edges, columns, width, height };
}

/** Column heading: what this level means relative to the seed. */
export function columnLabel(level: number): string {
  if (level === 0) return "seed";
  const hops = Math.abs(level);
  const direction = level < 0 ? "broader" : "narrower";
  return `${direction} · ${hops} hop${hops === 1 ? "" : "s"}`;
}
