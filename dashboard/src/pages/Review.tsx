import { useState } from "react";
import { api } from "../lib/api";
import type { Proposal } from "../lib/api";
import { useAsync } from "../lib/useAsync";
import { Async, ago } from "../components/ui";

export default function Review() {
  const state = useAsync<Proposal[]>(() => api.proposals(), []);
  const [busy, setBusy] = useState<string | null>(null);
  const [reviewer, setReviewer] = useState("david");
  const [error, setError] = useState<string | null>(null);

  async function decide(id: string, decision: "approve" | "reject") {
    setBusy(id);
    setError(null);
    try {
      // Goes through the API's compare-and-swap. A raw UPDATE would let two
      // reviewers both believe they won.
      await api.decide(id, decision, reviewer);
      state.reload();
    } catch (err) {
      setError((err as Error).message);
    } finally {
      setBusy(null);
    }
  }

  return (
    <>
      <div className="head">
        <h2>Review</h2>
        <span className="count">{state.data?.length ?? 0} pending</span>
      </div>

      <div style={{ marginBottom: 14, maxWidth: 260 }}>
        <input
          type="text"
          value={reviewer}
          onChange={(e) => setReviewer(e.target.value)}
          placeholder="Your name (recorded on the decision)"
        />
      </div>

      {error && (
        <div className="panel" style={{ padding: 14, marginBottom: 14, color: "var(--bad)" }}>
          {error}
        </div>
      )}

      <Async state={state} empty="No proposals awaiting review.">
        {(proposals) => (
          <div style={{ display: "grid", gap: 12 }}>
            {proposals.map((proposal) => (
              <div key={proposal.id} className="panel" style={{ padding: 16 }}>
                <div style={{ display: "flex", justifyContent: "space-between" }}>
                  <strong>{proposal.kind}</strong>
                  <span className="muted">{ago(proposal.created_at)}</span>
                </div>
                {proposal.rationale && (
                  <p style={{ margin: "8px 0" }}>{proposal.rationale}</p>
                )}
                <pre className="mono muted" style={{ margin: "8px 0", overflowX: "auto" }}>
                  {JSON.stringify(proposal.payload, null, 2)}
                </pre>
                <div style={{ display: "flex", gap: 8, marginTop: 10 }}>
                  <button
                    className="btn btn--ok"
                    disabled={busy === proposal.id || !reviewer.trim()}
                    onClick={() => decide(proposal.id, "approve")}
                  >
                    Approve
                  </button>
                  <button
                    className="btn btn--bad"
                    disabled={busy === proposal.id || !reviewer.trim()}
                    onClick={() => decide(proposal.id, "reject")}
                  >
                    Reject
                  </button>
                  {/* Applying a merge stays a backend job — deliberately not
                      an action available here. */}
                </div>
              </div>
            ))}
          </div>
        )}
      </Async>
    </>
  );
}
