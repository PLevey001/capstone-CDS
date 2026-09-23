import { date, type AnalysisRun, type Detail } from "../api";

export default function AnalysisHistory({
  detail,
  runs,
  selected,
  onSelect,
}: {
  detail: Detail;
  runs: AnalysisRun[];
  selected: string;
  onSelect: (id: string) => void;
}) {
  const run = detail.run;
  const active =
    detail.current_job_status === "queued" ||
    detail.current_job_status === "running";
  const saved = run && run.status !== "running";
  return (
    <section className="analysis-history" aria-label="Analysis history">
      <label htmlFor="analysis-run">
        Analysis history{" "}
        <span>
          {runs.length} {runs.length === 1 ? "run" : "runs"}
        </span>
      </label>
      <select
        id="analysis-run"
        value={selected}
        onChange={(event) => onSelect(event.target.value)}
      >
        <option value="">Latest saved results</option>
        {runs.map((item) => (
          <option key={item.id} value={item.id}>
            Run {item.sequence} ·{" "}
            {item.status === "completed" ? "finished" : item.status} ·{" "}
            {item.started_at ? date(item.started_at) : "time not recorded"}
            {item.legacy ? " · imported history" : ""}
          </option>
        ))}
      </select>
      {run ? (
        <>
          <p>
            <strong>Viewing run {run.sequence}</strong> ·{" "}
            {run.status === "completed" ? "finished" : run.status}
            {selected
              ? " · selection stays on this run"
              : " · follows latest saved results"}
          </p>
          {run.legacy && (
            <p>
              Imported from the results saved before history tracking. Earlier
              overwritten attempts cannot be reconstructed; missing measurements
              remain unknown.
            </p>
          )}
          {run.status === "interrupted" && (
            <p>
              This attempt stopped before results were saved. Its processing
              progress does not establish coverage.
            </p>
          )}
          <details>
            <summary>Run details</summary>
            <dl>
              <dt>Run ID</dt>
              <dd>
                <code>{run.id}</code>
              </dd>
              <dt>Started</dt>
              <dd>{run.started_at ? date(run.started_at) : "Not recorded"}</dd>
              <dt>Finished</dt>
              <dd>
                {run.finished_at ? date(run.finished_at) : "Not recorded"}
              </dd>
              <dt>Parser</dt>
              <dd>{String(run.metadata.parser_version ?? "Not recorded")}</dd>
              <dt>Tool version</dt>
              <dd>
                {String(
                  run.metadata.sleuthkit_version ?? "Not recorded / not used",
                )}
              </dd>
            </dl>
            <p>Recorded settings</p>
            <pre>{JSON.stringify(run.settings, null, 2)}</pre>
          </details>
          {saved && (
            <div className="run-exports">
              <a
                className="secondary-button"
                href={`/api/cases/${detail.case_id}/export?format=csv&run_id=${run.id}`}
              >
                Export run {run.sequence} CSV
              </a>
              <a
                className="secondary-button"
                href={`/api/cases/${detail.case_id}/export?format=json&run_id=${run.id}`}
              >
                Export run {run.sequence} JSON
              </a>
            </div>
          )}
        </>
      ) : (
        <p>
          No saved results yet. A history entry is created when processing
          starts.
        </p>
      )}
      {active && (
        <p className="history-current-job" role="status">
          Current analysis: {detail.current_job_status}. Previous saved runs
          remain available.
        </p>
      )}
    </section>
  );
}
