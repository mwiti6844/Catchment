import { useMemo, useState } from "react";
import { useNavigate, useParams } from "react-router-dom";
import { api } from "../lib/api";
import type { TagGraph, TagSummary } from "../lib/api";
import { NODE_H, NODE_W, columnLabel, layout } from "../lib/layout";
import { useAsync } from "../lib/useAsync";
import { Async, Chip } from "../components/ui";

const DEPTHS = [1, 2, 3];

/**
 * The tag graph explorer.
 *
 * The point of this page is watching the taxonomy take shape and drift, which
 * a flat table cannot show: a tag list tells you what exists, not how it is
 * arranged. The seed tag lives in the URL so a particular view is a link you
 * can keep.
 */
export default function Graph() {
  const { tagId } = useParams();
  const navigate = useNavigate();
  const [depth, setDepth] = useState(2);
  const [filter, setFilter] = useState("");

  const tags = useAsync(() => api.tags(), []);
  const graph = useAsync<TagGraph | null>(
    () => (tagId ? api.tagGraph(tagId, depth) : Promise.resolve(null)),
    [tagId, depth],
  );

  const visible = useMemo(() => {
    const needle = filter.trim().toLowerCase();
    const rows = tags.data ?? [];
    return needle
      ? rows.filter((tag) => tag.label.toLowerCase().includes(needle) ||
                             tag.slug.includes(needle))
      : rows;
  }, [tags.data, filter]);

  return (
    <>
      <div className="head">
        <h2>Tag graph</h2>
        <span className="count">
          {tags.data ? `${tags.data.length} tags` : ""}
          {graph.data ? ` · ${graph.data.nodes.length} in view` : ""}
        </span>
      </div>

      <div className="explorer">
        <div className="panel explorer__list">
          <input
            type="search"
            value={filter}
            onChange={(event) => setFilter(event.target.value)}
            placeholder="Filter tags"
            style={{ border: 0, borderBottom: "1px solid var(--line)", borderRadius: 0 }}
          />
          <Async state={tags} empty="No tags yet.">
            {(rows: TagSummary[]) => (
              <ul className="taglist">
                {visible.map((tag) => (
                  <li key={tag.id}>
                    <button
                      className={`taglist__row${tag.id === tagId ? " is-active" : ""}`}
                      onClick={() => navigate(`/graph/${tag.id}`)}
                    >
                      <span className="taglist__label">{tag.label}</span>
                      <span className="muted mono">
                        {/* Edge counts are how an unplaced tag announces
                            itself: zero of both means the classifier coined it
                            and never used it again. */}
                        {tag.item_count} · {tag.parent_count}↑{tag.child_count}↓
                      </span>
                    </button>
                  </li>
                ))}
                {visible.length === 0 && rows.length > 0 && (
                  <li className="muted" style={{ padding: "12px 14px" }}>
                    No tag matches.
                  </li>
                )}
              </ul>
            )}
          </Async>
        </div>

        <div>
          <div className="explorer__controls">
            <span className="muted">Depth</span>
            {DEPTHS.map((value) => (
              <button
                key={value}
                className={`btn${value === depth ? " btn--ok" : ""}`}
                onClick={() => setDepth(value)}
                disabled={!tagId}
              >
                {value}
              </button>
            ))}
            {graph.data?.truncated && (
              /* Surfaced rather than silent: a trimmed graph that looks whole
                 is misleading in exactly the way this page exists to prevent. */
              <Chip>view trimmed — some distant tags are hidden</Chip>
            )}
          </div>

          <Async state={graph} empty="Pick a tag to explore its neighbourhood.">
            {(data: TagGraph) => <GraphCanvas graph={data} />}
          </Async>
        </div>
      </div>
    </>
  );
}

function GraphCanvas({ graph }: { graph: TagGraph }) {
  const navigate = useNavigate();
  const view = useMemo(() => layout(graph), [graph]);

  return (
    <div className="panel graph">
      <svg
        width={view.width}
        height={view.height + 26}
        role="img"
        aria-label={`Tag neighbourhood of ${graph.root.label}, ${graph.nodes.length} tags`}
      >
        <defs>
          <marker
            id="arrow" markerWidth="7" markerHeight="7"
            refX="6" refY="3.5" orient="auto"
          >
            <path d="M0,0 L7,3.5 L0,7 z" fill="var(--line)" />
          </marker>
        </defs>

        {view.columns.map((column) => (
          <text
            key={column.level}
            x={column.x}
            y={view.height + 18}
            className="graph__axis"
          >
            {columnLabel(column.level)}
          </text>
        ))}

        {view.edges.map((edge) => (
          <path
            key={edge.key}
            d={edge.path}
            className={`graph__edge${edge.backwards ? " graph__edge--back" : ""}`}
            markerEnd="url(#arrow)"
          />
        ))}

        {view.nodes.map(({ node, x, y }) => {
          const isRoot = node.id === graph.root.id;
          return (
            <g
              key={node.id}
              transform={`translate(${x} ${y})`}
              className="graph__node"
              onClick={() => navigate(`/graph/${node.id}`)}
              role="button"
              tabIndex={0}
              onKeyDown={(event) =>
                event.key === "Enter" && navigate(`/graph/${node.id}`)
              }
            >
              <title>
                {`${node.label} — ${node.item_count} items, ${node.status}`}
              </title>
              <rect
                width={NODE_W}
                height={NODE_H}
                rx={9}
                className={
                  isRoot ? "graph__box graph__box--root"
                  : node.status !== "active" ? "graph__box graph__box--faded"
                  : "graph__box"
                }
              />
              <text x={12} y={17} className="graph__label">
                {truncate(node.label)}
              </text>
              <text x={12} y={31} className="graph__meta">
                {node.item_count} item{node.item_count === 1 ? "" : "s"}
                {node.status !== "active" ? ` · ${node.status}` : ""}
              </text>
            </g>
          );
        })}
      </svg>

      <div className="graph__footer muted">
        Seeded on <strong>{graph.root.label}</strong>{" "}
        <span className="mono">({graph.root.slug})</span> at depth {graph.depth}.
        Click any tag to re-seed.
      </div>
    </div>
  );
}

/** Boxes are fixed width, so a long label must be cut rather than overflow. */
function truncate(label: string, max = 21): string {
  return label.length <= max ? label : `${label.slice(0, max - 1)}…`;
}
