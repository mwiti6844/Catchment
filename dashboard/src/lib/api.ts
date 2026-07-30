// All calls go to /internal on the same origin. Vite proxies that to the
// loopback-bound internal API in development; in a built deployment the same
// path is served behind the same loopback binding. The token is injected by
// the proxy, so it never reaches client-side JavaScript.

export type ItemSummary = {
  id: string; source: string; kind: string; author: string | null;
  ingested_at: string; extracted_chars: number | null;
  has_embedding: boolean; status: string; tag_count: number;
};
export type ItemPage = { items: ItemSummary[]; total: number; limit: number; offset: number };
export type ItemTagView = {
  label: string; slug: string; origin: string; confidence: number;
  assigned_by: string; trace_id: string | null;
};
export type ItemDetail = {
  id: string; source: string; source_id: string; kind: string;
  author: string | null; url: string | null; published_at: string | null;
  ingested_at: string; text: string | null; extractor: string | null;
  language: string | null; has_embedding: boolean;
  embedding_model: string | null; tags: ItemTagView[];
};
export type SearchHit = {
  item_id: string; score: number; route: string; distance: number | null;
  graph_depth: number | null; matched_tags: number | null;
  source: string; author: string | null; ingested_at: string;
  preview_chars: number | null;
};
export type SearchResponse = {
  query_chars: number; seed_count: number; expanded_count: number;
  tags_walked: number; hits: SearchHit[];
};
export type Proposal = {
  id: string; kind: string; rationale: string | null;
  proposed_by: string; created_at: string; payload: Record<string, unknown>;
};
// An approved merge executes as part of the decision, so the response reports
// what it moved. `assignments_moved` is null for a rejection, and also when a
// merge was approved but could not be applied — the status distinguishes them.
export type Decision = {
  id: string; status: string; reviewed_by: string | null;
  reviewed_at: string | null; assignments_moved: number | null;
};
export type Failure = {
  id: string; item_id: string; stage: string; error_type: string;
  detail: string | null; occurred_at: string;
};
export type Connector = {
  source: string; last_success_at: string | null; last_attempt_at: string;
  last_outcome: string; detail: string | null; items_seen: number;
  items_created: number; stale: boolean; stale_after_seconds: number;
};
export type TagSummary = {
  id: string; slug: string; label: string; status: string; origin: string;
  item_count: number; parent_count: number; child_count: number;
};
// `level` is signed: negative broader, 0 the seed, positive narrower. It is the
// column index, which is why the client never has to infer direction itself.
export type TagNode = {
  id: string; slug: string; label: string; status: string; origin: string;
  item_count: number; level: number;
};
export type TagEdge = { parent: string; child: string; relation: string };
export type TagGraph = {
  root: TagNode; depth: number; nodes: TagNode[];
  edges: TagEdge[]; truncated: boolean;
};
export type TagTrend = {
  tag_id: string; slug: string; label: string;
  recent_count: number; prior_count: number; delta: number;
  // The items behind `recent_count`. Every figure on the Insights page links
  // through these, so a claim can be checked instead of believed.
  sample_item_ids: string[];
};
export type TrendReport = {
  window_days: number; window_start: string; window_end: string;
  prior_start: string; total_recent: number; total_prior: number;
  tags: TagTrend[];
};
export type QueueCounts = {
  queue: string; pending: number; started: number; finished: number;
  failed: number; deferred: number; scheduled: number;
  oldest_pending_seconds: number | null;
};

export class ApiError extends Error {
  constructor(readonly status: number, message: string) {
    super(message);
  }
}

async function get<T>(path: string): Promise<T> {
  const response = await fetch(`/internal${path}`, {
    headers: { Accept: "application/json" },
  });
  if (!response.ok) {
    // Surface the status; the body may carry a detail worth reading, but never
    // assume it is JSON — a proxy error page is HTML.
    let detail = response.statusText;
    try {
      detail = (await response.json()).detail ?? detail;
    } catch {
      /* keep statusText */
    }
    throw new ApiError(response.status, detail);
  }
  return response.json() as Promise<T>;
}

export const api = {
  items: (params: { source?: string; status?: string; limit?: number } = {}) => {
    const query = new URLSearchParams();
    if (params.source) query.set("source", params.source);
    if (params.status) query.set("status", params.status);
    query.set("limit", String(params.limit ?? 50));
    return get<ItemPage>(`/items?${query}`);
  },
  item: (id: string) => get<ItemDetail>(`/items/${id}`),
  search: (q: string) => get<SearchResponse>(`/search?q=${encodeURIComponent(q)}`),
  tags: () => get<TagSummary[]>("/tags"),
  tagGraph: (id: string, depth: number) =>
    get<TagGraph>(`/tags/${id}/graph?depth=${depth}`),
  insights: (windowDays: number) =>
    get<TrendReport>(`/insights?window_days=${windowDays}`),
  proposals: () => get<Proposal[]>("/proposals"),
  failures: () => get<Failure[]>("/failures"),
  connectors: () => get<Connector[]>("/connectors/health"),
  queue: () => get<QueueCounts>("/queue"),
  decide: async (
    id: string,
    decision: "approve" | "reject",
    reviewer: string,
  ): Promise<Decision> => {
    const response = await fetch(`/internal/proposals/${id}/decision`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ decision, reviewer }),
    });
    if (!response.ok) throw new ApiError(response.status, await response.text());
    return response.json() as Promise<Decision>;
  },
};
