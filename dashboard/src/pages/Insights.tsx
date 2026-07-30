import { useState } from "react";
import { Link } from "react-router-dom";
import { api } from "../lib/api";
import type { TagTrend, TrendReport } from "../lib/api";
import { useAsync } from "../lib/useAsync";
import { Async } from "../components/ui";

const WINDOWS = [7, 14, 30];

/**
 * What has been arriving, and whether it is rising or fading.
 *
 * This is the only page that makes a claim about the user rather than
 * reporting what happened, so it is built to be checkable: no model call, no
 * scoring heuristic, and every row links to the items its number came from.
 * A figure you cannot open is a horoscope, and this page is deliberately not
 * one — hence the stated window and the item links on every row.
 */
export default function Insights() {
  const [windowDays, setWindowDays] = useState(7);
  const report = useAsync(() => api.insights(windowDays), [windowDays]);

  return (
    <>
      <div className="head">
        <h2>Insights</h2>
        {report.data && (
          <span className="count">
            {report.data.total_recent} items in the last {windowDays} days ·{" "}
            {report.data.total_prior} in the {windowDays} before
          </span>
        )}
      </div>

      <div className="explorer__controls" style={{ marginBottom: 16 }}>
        <span className="muted">Window</span>
        {WINDOWS.map((days) => (
          <button
            key={days}
            className={`btn${days === windowDays ? " btn--ok" : ""}`}
            onClick={() => setWindowDays(days)}
          >
            {days}d
          </button>
        ))}
      </div>

      <Async
        state={report}
        empty={`Nothing ingested in the last ${windowDays} days.`}
      >
        {(data: TrendReport) =>
          data.tags.length === 0 ? (
            <div className="panel muted" style={{ padding: 20 }}>
              {data.total_recent > 0
                ? `${data.total_recent} items arrived but none carry a tag yet.`
                : "Nothing ingested in this window."}
            </div>
          ) : (
            <Report data={data} />
          )
        }
      </Async>
    </>
  );
}

function Report({ data }: { data: TrendReport }) {
  const peak = Math.max(...data.tags.map((tag) => tag.recent_count), 1);

  return (
    <>
      <div className="panel">
        <table>
          <thead>
            <tr>
              <th>Tag</th>
              <th style={{ width: "34%" }}>This window</th>
              <th>Previous</th>
              <th>Change</th>
              <th>Items</th>
            </tr>
          </thead>
          <tbody>
            {data.tags.map((trend) => (
              <TrendRow key={trend.tag_id} trend={trend} peak={peak} />
            ))}
          </tbody>
        </table>
      </div>

      {/* The window is stated because a trend without one cannot be checked.
          Anyone can reproduce these counts from the inbox. */}
      <p className="muted mono" style={{ marginTop: 12, fontSize: 12 }}>
        Counted over ingestion time, {stamp(data.window_start)} →{" "}
        {stamp(data.window_end)}; previous window from {stamp(data.prior_start)}.
        Half-open intervals, so no item is counted twice.
      </p>
    </>
  );
}

function TrendRow({ trend, peak }: { trend: TagTrend; peak: number }) {
  const tone =
    trend.delta > 0 ? "var(--ok)" : trend.delta < 0 ? "var(--bad)" : "var(--muted)";

  return (
    <tr>
      <td>
        <Link to={`/graph/${trend.tag_id}`}>{trend.label}</Link>
      </td>
      <td>
        {/* A bar rather than a sparkline: two data points do not make a trend
            line, and drawing one would imply a shape that was never measured. */}
        <div className="bar">
          <div
            className="bar__fill"
            style={{ width: `${(trend.recent_count / peak) * 100}%` }}
          />
          <span className="bar__value mono">{trend.recent_count}</span>
        </div>
      </td>
      <td className="mono muted">{trend.prior_count}</td>
      <td className="mono" style={{ color: tone }}>
        {trend.delta > 0 ? `+${trend.delta}` : trend.delta}
      </td>
      <td>
        {/* The traceability that keeps this page honest: each count opens onto
            the items it was made from. */}
        {trend.sample_item_ids.map((id, index) => (
          <Link key={id} to={`/items/${id}`} className="mono" style={{ marginRight: 8 }}>
            #{index + 1}
          </Link>
        ))}
      </td>
    </tr>
  );
}

function stamp(iso: string): string {
  return new Date(iso).toLocaleString(undefined, {
    dateStyle: "medium",
    timeStyle: "short",
  });
}
