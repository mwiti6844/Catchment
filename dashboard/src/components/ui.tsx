import type { ReactNode } from "react";

export function Spinner() {
  return <span className="spinner" role="status" aria-label="Loading" />;
}

export function StatusDot({ status }: { status: string }) {
  const tone =
    status === "classified" ? "ok" : status === "failed" ? "bad"
    : status === "pending" ? "warn" : "";
  return <span className={`dot ${tone ? `dot--${tone}` : ""}`} />;
}

export function Chip({ children }: { children: ReactNode }) {
  return <span className="chip">{children}</span>;
}

/** One place for the three states every screen has, so none invents its own. */
export function Async<T>({
  state,
  children,
  empty = "Nothing here yet.",
}: {
  state: { data: T | null; error: Error | null; loading: boolean };
  children: (data: T) => ReactNode;
  empty?: string;
}) {
  if (state.loading && state.data === null) {
    return (
      <div className="panel" style={{ padding: 20 }}>
        <Spinner />
      </div>
    );
  }
  if (state.error) {
    return (
      <div className="panel" style={{ padding: 20 }}>
        <strong style={{ color: "var(--bad)" }}>Could not load.</strong>
        <div className="muted mono" style={{ marginTop: 6 }}>
          {state.error.message}
        </div>
      </div>
    );
  }
  const data = state.data;
  const isEmpty =
    data === null || (Array.isArray(data) && data.length === 0);
  if (isEmpty) {
    return (
      <div className="panel muted" style={{ padding: 20 }}>
        {empty}
      </div>
    );
  }
  return <div className="enter">{children(data as T)}</div>;
}

export function ago(iso: string): string {
  const seconds = (Date.now() - new Date(iso).getTime()) / 1000;
  if (seconds < 60) return "just now";
  if (seconds < 3600) return `${Math.floor(seconds / 60)}m ago`;
  if (seconds < 86400) return `${Math.floor(seconds / 3600)}h ago`;
  return `${Math.floor(seconds / 86400)}d ago`;
}
