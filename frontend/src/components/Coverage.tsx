import { type Coverage, type Evidence, bytes, date } from "../api";

export const coverageLabels = {
  complete: "Complete for scope",
  partial: "Coverage gaps",
  failed: "Analysis failed",
  unknown: "Not recorded",
  pending: "Awaiting results",
};
export type CoverageFilter = keyof typeof coverageLabels;

export function coverageState(evidence: Evidence): CoverageFilter {
  if (evidence.status === "queued" || evidence.status === "running")
    return "pending";
  return evidence.coverage?.status ?? "unknown";
}

export function CoverageBadge({ evidence }: { evidence: Evidence }) {
  const status = coverageState(evidence);
  return (
    <span className={`coverage-badge coverage-${status}`}>
      {coverageLabels[status]}
    </span>
  );
}

export function CoverageSummary({
  evidence,
  value,
  onChange,
}: {
  evidence: Evidence[];
  value: CoverageFilter | "all";
  onChange: (value: CoverageFilter | "all") => void;
}) {
  if (!evidence.length) return null;
  return (
    <section className="coverage-summary" aria-label="Analysis coverage">
      <div>
        <strong>Analysis coverage</strong>
        <p>
          Measured scope for each source. Job completion alone does not mean all
          evidence was examined.
        </p>
      </div>
      <div className="coverage-filters">
        <button aria-pressed={value === "all"} onClick={() => onChange("all")}>
          All sources <b>{evidence.length}</b>
        </button>
        {Object.entries(coverageLabels).map(([state, label]) => (
          <button
            key={state}
            aria-pressed={value === state}
            className={`coverage-${state}`}
            onClick={() => onChange(state as CoverageFilter)}
          >
            {label}{" "}
            <b>
              {evidence.filter((item) => coverageState(item) === state).length}
            </b>
          </button>
        ))}
      </div>
    </section>
  );
}

const stepLabels: Record<string, string> = {
  complete: "Complete for scope",
  partial: "Partial",
  failed: "Failed",
  skipped: "Skipped",
  unsupported: "Unsupported",
  unknown: "Unknown",
};

function quantity(value: number, unit: string) {
  return unit === "bytes"
    ? bytes(value)
    : `${value.toLocaleString()} ${value === 1 && unit.endsWith("s") ? unit.slice(0, -1) : unit}`;
}

export function CoverageDetails({ evidence }: { evidence: Evidence }) {
  const coverage: Coverage | null = evidence.coverage;
  const active = evidence.status === "queued" || evidence.status === "running";
  return (
    <section className="coverage-details" aria-label="Coverage details">
      <div className="coverage-heading">
        <h3>What was examined</h3>
        <CoverageBadge evidence={evidence} />
      </div>
      {active && (
        <p className="coverage-note">
          Coverage is measured when the current analysis finishes.{" "}
          {coverage
            ? "The measurements and artifacts below belong to the last saved analysis."
            : "No measurements are available yet."}
        </p>
      )}
      {!coverage ? (
        <p className="coverage-note">
          {active
            ? "The progress indicator tracks processing stages, not evidence coverage."
            : evidence.status === "interrupted"
              ? "This attempt was interrupted before coverage was saved. No completeness is assumed."
              : "No coverage measurements were saved for this result. Analyze again to measure its scope; earlier results will be kept."}
        </p>
      ) : (
        <>
          <p className="coverage-scope">{coverage.scope}</p>
          {active && (
            <p className="coverage-note">
              Last saved result: {coverageLabels[coverage.status]}
            </p>
          )}
          <ul className="coverage-steps">
            {coverage.steps.map((step) => (
              <li key={step.id}>
                <div className="coverage-heading">
                  <strong>{step.label}</strong>
                  <span className={`coverage-badge coverage-${step.status}`}>
                    {stepLabels[step.status] ?? step.status}
                  </span>
                </div>
                <p>{step.detail}</p>
                <small>
                  {quantity(step.processed, step.unit)} processed
                  {step.total !== null
                    ? ` of ${quantity(step.total, step.unit)}`
                    : " · total scope unknown"}
                </small>
                {step.total !== null && step.total > 0 && (
                  <progress
                    className={`coverage-${step.status}`}
                    aria-label={`${step.label}: measured scope`}
                    max={step.total}
                    value={step.processed}
                  />
                )}
                <details>
                  <summary>Measurement details</summary>
                  <code>
                    {step.parser} · {step.reason}
                  </code>
                  {step.partition_offset !== undefined && (
                    <p>
                      Partition start: sector{" "}
                      {step.partition_offset.toLocaleString()} ·{" "}
                      {step.sector_size}-byte sectors
                    </p>
                  )}
                  {step.records_returned !== undefined && (
                    <p>
                      {step.records_returned.toLocaleString()} records returned
                      by the tool · {step.malformed_records ?? 0} malformed
                    </p>
                  )}
                </details>
              </li>
            ))}
          </ul>
          <p className="coverage-note">
            No findings in the indexed results does not establish that no
            relevant evidence exists.
          </p>
          <details className="coverage-run">
            <summary>Analysis run and limits</summary>
            <p>
              Recorded{" "}
              {coverage.finished_at
                ? date(coverage.finished_at)
                : "time unavailable"}
            </p>
            <code>{coverage.run_id}</code>
            {coverage.tool_version && <p>{coverage.tool_version}</p>}
            <pre>{JSON.stringify(coverage.limits, null, 2)}</pre>
          </details>
        </>
      )}
    </section>
  );
}
