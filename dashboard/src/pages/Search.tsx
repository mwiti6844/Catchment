import { useState } from "react";
import { Link } from "react-router-dom";
import { api } from "../lib/api";
import type { SearchResponse } from "../lib/api";
import { Chip, Spinner, ago } from "../components/ui";

export default function Search() {
  const [query, setQuery] = useState("");
  const [result, setResult] = useState<SearchResponse | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);

  async function run(event: React.FormEvent) {
    event.preventDefault();
    if (!query.trim()) return;
    setLoading(true);
    setError(null);
    try {
      setResult(await api.search(query));
    } catch (err) {
      setError((err as Error).message);
    } finally {
      setLoading(false);
    }
  }

  return (
    <>
      <div className="head">
        <h2>Search</h2>
        {result && (
          <span className="count">
            {result.seed_count} matched · {result.expanded_count} via graph ·{" "}
            {result.tags_walked} tags walked
          </span>
        )}
      </div>

      {/* Submit-driven, not keystroke-driven: every query costs an embedding
          call, and a search-as-you-type would fire one per character. */}
      <form onSubmit={run} style={{ display: "flex", gap: 10, marginBottom: 16 }}>
        <input
          type="search"
          value={query}
          onChange={(e) => setQuery(e.target.value)}
          placeholder="What are you looking for?"
          autoFocus
        />
        <button className="btn" disabled={loading || !query.trim()}>
          {loading ? <Spinner /> : "Search"}
        </button>
      </form>

      {error && (
        <div className="panel" style={{ padding: 16, color: "var(--bad)" }}>
          {error}
        </div>
      )}

      {result && (
        <div className="panel enter">
          <table>
            <thead>
              <tr>
                <th>Why</th><th>Source</th><th>From</th>
                <th>Chars</th><th>Score</th><th>Ingested</th>
              </tr>
            </thead>
            <tbody>
              {result.hits.map((hit) => (
                <tr key={hit.item_id}>
                  <td>
                    {/* Surfacing the route is the point: a graph hit appearing
                        without explanation looks like a bad result. */}
                    <Chip>
                      {hit.route === "seed"
                        ? "direct match"
                        : `${hit.graph_depth} hop${hit.graph_depth === 1 ? "" : "s"} · ${hit.matched_tags} tags`}
                    </Chip>
                  </td>
                  <td>
                    <Link to={`/items/${hit.item_id}`}>{hit.source}</Link>
                  </td>
                  <td>{hit.author ?? <span className="muted">—</span>}</td>
                  <td className="mono">{hit.preview_chars ?? "—"}</td>
                  <td className="mono">{hit.score.toFixed(2)}</td>
                  <td className="muted">{ago(hit.ingested_at)}</td>
                </tr>
              ))}
            </tbody>
          </table>
          {result.hits.length === 0 && (
            <div className="muted" style={{ padding: 20 }}>No matches.</div>
          )}
        </div>
      )}
    </>
  );
}
