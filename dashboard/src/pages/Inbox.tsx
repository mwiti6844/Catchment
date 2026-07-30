import { useNavigate } from "react-router-dom";
import { api } from "../lib/api";
import type { ItemPage } from "../lib/api";
import { useAsync } from "../lib/useAsync";
import { Async, StatusDot, ago } from "../components/ui";

export default function Inbox() {
  const navigate = useNavigate();
  const state = useAsync<ItemPage>(() => api.items({ limit: 100 }), []);

  return (
    <>
      <div className="head">
        <h2>Inbox</h2>
        <span className="count">{state.data?.total ?? 0} items</span>
      </div>
      <Async state={state} empty="No items ingested yet.">
        {(page) => (
          <div className="panel">
            <table>
              <thead>
                <tr>
                  <th>Status</th><th>Source</th><th>From</th>
                  <th>Chars</th><th>Tags</th><th>Ingested</th>
                </tr>
              </thead>
              <tbody>
                {page.items.map((item) => (
                  <tr
                    key={item.id}
                    data-clickable
                    onClick={() => navigate(`/items/${item.id}`)}
                  >
                    <td>
                      <StatusDot status={item.status} />{" "}
                      <span className="muted">{item.status}</span>
                    </td>
                    <td>{item.source}</td>
                    {/* Author, not content — the list view never shows text. */}
                    <td>{item.author ?? <span className="muted">—</span>}</td>
                    <td className="mono">{item.extracted_chars ?? "—"}</td>
                    <td>{item.tag_count}</td>
                    <td className="muted">{ago(item.ingested_at)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </Async>
    </>
  );
}
