import { api } from "../lib/api";
import type { Connector, Failure, QueueCounts } from "../lib/api";
import { useAsync } from "../lib/useAsync";
import { Async, ago } from "../components/ui";

/** One screen, not three. A single-user tool should not fragment navigation. */
export default function Ops() {
  const connectors = useAsync<Connector[]>(() => api.connectors(), []);
  const queue = useAsync<QueueCounts>(() => api.queue(), []);
  const failures = useAsync<Failure[]>(() => api.failures(), []);

  return (
    <>
      <div className="head"><h2>Failures &amp; ops</h2></div>

      <section style={{ marginBottom: 22 }}>
        <h3 style={{ fontSize: 13, color: "var(--muted)" }}>Connectors</h3>
        <Async state={connectors} empty="No connector has reported in yet.">
          {(rows) => (
            <div className="panel">
              <table>
                <thead>
                  <tr><th>Source</th><th>Last success</th><th>Outcome</th><th>Seen / new</th></tr>
                </thead>
                <tbody>
                  {rows.map((row) => (
                    <tr key={row.source}>
                      <td>
                        <span className={`dot ${row.stale ? "dot--bad" : "dot--ok"}`} />{" "}
                        {row.source}
                      </td>
                      <td className={row.stale ? "" : "muted"}>
                        {/* Liveness, not item freshness: a duplicate delivery
                            or an empty poll is a healthy round trip. */}
                        {row.last_success_at ? ago(row.last_success_at) : "never"}
                        {row.stale && <strong style={{ color: "var(--bad)" }}> · stale</strong>}
                      </td>
                      <td className="muted">{row.last_outcome}{row.detail ? ` (${row.detail})` : ""}</td>
                      <td className="mono">{row.items_seen} / {row.items_created}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </Async>
      </section>

      <section style={{ marginBottom: 22 }}>
        <h3 style={{ fontSize: 13, color: "var(--muted)" }}>Queue</h3>
        <Async state={queue} empty="Queue unavailable.">
          {(counts) => (
            <div className="panel" style={{ padding: 16, display: "flex", gap: 26 }}>
              {([["pending", counts.pending], ["started", counts.started],
                 ["finished", counts.finished], ["failed", counts.failed]] as const).map(
                ([label, value]) => (
                  <div key={label}>
                    <div className="muted" style={{ fontSize: 12 }}>{label}</div>
                    <div style={{ fontSize: 21 }}>{value}</div>
                  </div>
                )
              )}
              {counts.oldest_pending_seconds !== null && (
                <div>
                  <div className="muted" style={{ fontSize: 12 }}>oldest wait</div>
                  <div style={{ fontSize: 21 }}>
                    {Math.round(counts.oldest_pending_seconds)}s
                  </div>
                </div>
              )}
            </div>
          )}
        </Async>
      </section>

      <section>
        <h3 style={{ fontSize: 13, color: "var(--muted)" }}>Dead letter</h3>
        <Async state={failures} empty="No open failures.">
          {(rows) => (
            <div className="panel">
              <table>
                <thead>
                  <tr><th>Stage</th><th>Error</th><th>Item</th><th>When</th></tr>
                </thead>
                <tbody>
                  {rows.map((row) => (
                    <tr key={row.id}>
                      <td>{row.stage}</td>
                      <td className="mono">{row.error_type}</td>
                      <td className="mono muted">{row.item_id.slice(0, 8)}</td>
                      <td className="muted">{ago(row.occurred_at)}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </Async>
      </section>
    </>
  );
}
