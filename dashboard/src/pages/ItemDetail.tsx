import { Link, useParams } from "react-router-dom";
import { api } from "../lib/api";
import type { ItemDetail as Detail } from "../lib/api";
import { useAsync } from "../lib/useAsync";
import { Async, Chip } from "../components/ui";

const LANGFUSE_BASE =
  import.meta.env.VITE_LANGFUSE_URL ?? "http://localhost:3001";

export default function ItemDetail() {
  const { id = "" } = useParams();
  const state = useAsync<Detail>(() => api.item(id), [id]);

  return (
    <>
      <div className="head">
        <h2>Item</h2>
        <Link to="/" className="muted">← Inbox</Link>
      </div>
      <Async state={state}>
        {(item) => (
          <div style={{ display: "grid", gap: 16 }}>
            <div className="panel" style={{ padding: 16 }}>
              <div style={{ display: "flex", gap: 8, flexWrap: "wrap" }}>
                <Chip>{item.source}</Chip>
                <Chip>{item.kind}</Chip>
                {item.author && <Chip>{item.author}</Chip>}
                <Chip>
                  {item.has_embedding
                    ? `embedded · ${item.embedding_model}`
                    : "no embedding"}
                </Chip>
              </div>
              <div className="muted mono" style={{ marginTop: 10 }}>
                {item.source_id}
              </div>
            </div>

            <div className="panel" style={{ padding: 16 }}>
              <h3 style={{ margin: "0 0 10px", fontSize: 13 }}>
                Tags
                {item.extractor && (
                  <span className="muted"> · via {item.extractor}</span>
                )}
              </h3>
              {item.tags.length === 0 && <span className="muted">Untagged.</span>}
              <table>
                <tbody>
                  {item.tags.map((tag) => (
                    <tr key={tag.slug}>
                      <td>{tag.label}</td>
                      <td className="mono">{tag.confidence.toFixed(2)}</td>
                      <td className="muted">{tag.assigned_by}</td>
                      <td>
                        {tag.trace_id ? (
                          <a
                            href={`${LANGFUSE_BASE}/project/catchment/traces/${tag.trace_id}`}
                            target="_blank"
                            rel="noreferrer"
                          >
                            trace
                          </a>
                        ) : (
                          // A rule-based fallback has no model call to link to.
                          // That absence is itself the useful signal.
                          <span className="muted">no model call</span>
                        )}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>

            <div className="panel" style={{ padding: 16 }}>
              <h3 style={{ margin: "0 0 10px", fontSize: 13 }}>Extracted text</h3>
              {item.text ? (
                <div className="text">{item.text}</div>
              ) : (
                <span className="muted">Nothing extracted yet.</span>
              )}
            </div>
          </div>
        )}
      </Async>
    </>
  );
}
