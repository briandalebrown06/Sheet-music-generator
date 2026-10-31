import { useState } from "react";
import type { CanonicalScore } from "./api";

export default function NotationChecks({
  report,
  onSelect,
}: {
  report: NonNullable<CanonicalScore["readability"]>;
  onSelect?: (ids: string[]) => void;
}) {
  const [page, setPage] = useState(0);
  return (
    <details className="notation-checks">
      <summary>
        Notation readability · {report.issues.length} passage
        {report.issues.length === 1 ? "" : "s"} to review
      </summary>
      <p>
        Maximum {report.statistics.maximum_voices_per_staff} sounding voices per
        staff; up to {report.statistics.maximum_ledger_lines} estimated ledger
        lines. These automated counts do not establish visual readability.
      </p>
      <ol className="piano-issues" start={page * 8 + 1}>
        {report.issues.slice(page * 8, page * 8 + 8).map((issue, index) => (
          <li key={`${issue.code}:${issue.measure}:${issue.staff}:${index}`}>
            <strong>
              Measure {issue.measure === 0 ? "pickup" : issue.measure} ·{" "}
              {issue.part_id} · {issue.staff} staff
              {issue.hand?.length ? ` · ${issue.hand.join(" / ")} hand` : ""}
            </strong>
            <p>{issue.detail}</p>
            {onSelect && issue.note_ids.length > 0 && (
              <button
                className="text-button"
                type="button"
                onClick={() => onSelect(issue.note_ids)}
              >
                Select {issue.note_ids.length} affected note
                {issue.note_ids.length === 1 ? "" : "s"}
              </button>
            )}
          </li>
        ))}
      </ol>
      {report.issues.length > 8 && (
        <div className="piano-issue-pages">
          <span>
            {page * 8 + 1}–{Math.min(report.issues.length, page * 8 + 8)} of{" "}
            {report.issues.length} passages
          </span>
          <button
            className="button secondary small"
            type="button"
            disabled={!page}
            onClick={() => setPage(page - 1)}
          >
            Previous notation issues
          </button>
          <button
            className="button secondary small"
            type="button"
            disabled={(page + 1) * 8 >= report.issues.length}
            onClick={() => setPage(page + 1)}
          >
            Next notation issues
          </button>
        </div>
      )}
      <ul>
        {report.limitations.map((item) => (
          <li key={item}>{item}</li>
        ))}
      </ul>
    </details>
  );
}
