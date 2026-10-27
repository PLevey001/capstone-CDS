import { useCallback, useEffect, useRef, useState } from "react";
import { ArrowRight, LoaderCircle, Sparkles } from "lucide-react";
import {
  api,
  ApiError,
  utcDate,
  type AiStatus,
  type AssessmentItem,
  type BriefFact,
  type BriefVersion,
  type FactSheet,
  type SavedBrief,
} from "../api";

export type BriefState = {
  busy: boolean;
  value: SavedBrief | null;
  error: string;
  revision?: string;
  requestId?: string;
  selectedId?: string;
};
export const NO_BRIEF: BriefState = { busy: false, value: null, error: "" };

export default function BriefPanel({
  caseId,
  sourceRevision,
  state,
  onChange,
  onSelect,
}: {
  caseId: string;
  sourceRevision: string;
  state: BriefState;
  onChange: (patch: Partial<BriefState>) => void;
  onSelect: (fact: BriefFact) => void;
}) {
  const { busy, value: saved, error } = state;
  const brief = saved?.document;
  const [sheet, setSheet] = useState<FactSheet | null>(null);
  const [sheetRevision, setSheetRevision] = useState("");
  const [versions, setVersions] = useState<BriefVersion[]>([]);
  // Selection belongs to the parent, like the in-flight request and saved document.
  const selected = state.selectedId ?? "";
  const [loadError, setLoadError] = useState("");
  const [attempt, setAttempt] = useState(0);
  const [loading, setLoading] = useState(true);
  const [status, setStatus] = useState<AiStatus | null>(null);
  const [focused, setFocused] = useState("");
  const change = useRef(onChange);
  change.current = onChange;

  useEffect(() => {
    if (busy) return;
    const controller = new AbortController();
    setSheet(null);
    setLoadError("");
    setLoading(true);
    const options = { signal: controller.signal };
    async function load() {
      try {
        const [facts, history] = await Promise.all([
          api<FactSheet>(`/cases/${caseId}/brief`, options),
          api<BriefVersion[]>(`/cases/${caseId}/briefs`, options),
        ]);
        const id = selected || history[0]?.id;
        const value = id
          ? await api<SavedBrief>(`/cases/${caseId}/briefs/${id}`, options)
          : null;
        if (!controller.signal.aborted) {
          setSheet(facts);
          setSheetRevision(sourceRevision);
          setVersions(history);
          change.current({ value });
          setFocused("");
        }
      } catch (failure) {
        if (!controller.signal.aborted)
          setLoadError((failure as Error).message);
      } finally {
        if (!controller.signal.aborted) setLoading(false);
      }
    }
    void load();
    return () => controller.abort();
  }, [caseId, sourceRevision, attempt, selected, busy]);

  const checkModel = useCallback(() => {
    setStatus(null);
    api<AiStatus>("/ai/status")
      .then(setStatus)
      .catch((failure) =>
        setStatus({
          available: false,
          model: "",
          endpoint: "",
          reason: failure.message,
        }),
      );
  }, []);
  useEffect(checkModel, [checkModel]);

  const freshnessUnknown =
    loading || !sheet || sheetRevision !== sourceRevision || !!loadError;
  const stale =
    !freshnessUnknown &&
    (saved?.stale || brief?.input_digest !== sheet?.input_digest);
  // A new POST result can arrive before the refreshed history includes its version.
  const displayedVersions =
    brief && !versions.some((version) => version.id === brief.id)
      ? [
          {
            id: brief.id,
            version: brief.version,
            generated_at: brief.generated_at,
            stale: !!saved?.stale,
          },
          ...versions,
        ]
      : versions;
  const requestedVersion = displayedVersions.find(
    (version) => version.id === selected,
  )?.version;
  const showFact = (id: string) => {
    setFocused(id);
    document
      .getElementById(`brief-fact-${id}`)
      ?.scrollIntoView({ block: "center" });
  };
  const statements = (title: string, items: AssessmentItem[]) => (
    <>
      <h4>{title} · unverified AI</h4>
      {items.length ? (
        <ul className="brief-statements" aria-label={title}>
          {items.map((item) => (
            <li key={item.id}>
              <small>{item.id}</small>
              <p>{item.text}</p>
              {item.reasoning && (
                <p>
                  <strong>Reasoning:</strong> {item.reasoning}
                </p>
              )}
              {item.alternatives && (
                <>
                  <strong>Alternative explanations</strong>
                  <ul>
                    {item.alternatives.map((text, i) => (
                      <li key={i}>{text}</li>
                    ))}
                  </ul>
                </>
              )}
              {item.limitations && (
                <>
                  <strong>Uncertainty and limitations</strong>
                  <ul>
                    {item.limitations.map((text, i) => (
                      <li key={i}>{text}</li>
                    ))}
                  </ul>
                </>
              )}
              <div className="brief-citations">
                <span>Historical fact references (not proof)</span>
                {item.facts.map((id) => (
                  <button
                    key={id}
                    className="fact-chip"
                    aria-label={`Show fact ${id}`}
                    title={brief?.facts.find((fact) => fact.id === id)?.text}
                    onClick={() => showFact(id)}
                  >
                    {id}
                  </button>
                ))}
              </div>
            </li>
          ))}
        </ul>
      ) : (
        <p>No items generated.</p>
      )}
    </>
  );
  const facts = (items: BriefFact[], historical: boolean) => (
    <ol className="brief-facts">
      {items.map((fact) => (
        <li
          key={fact.id}
          id={`${historical ? "brief-fact" : "current-fact"}-${fact.id}`}
          className={historical && focused === fact.id ? "focused" : ""}
        >
          <span className="fact-id">{fact.id}</span>
          <span className="fact-text">{fact.text}</span>
          {fact.source_id && (
            <button
              className="icon-button"
              aria-label={`Open the source of fact ${fact.id}`}
              title="Open the pinned saved run"
              onClick={() => onSelect(fact)}
            >
              <ArrowRight size={15} />
            </button>
          )}
        </li>
      ))}
    </ol>
  );

  return (
    <div className="activity-list brief-panel">
      <section aria-labelledby="brief-heading">
        <div className="brief-heading">
          <h3 id="brief-heading">AI case brief</h3>
          <span role="status">
            {status === null
              ? "Checking for a local model…"
              : status.available
                ? `Local model ready: ${status.model}`
                : "Local model unavailable"}
          </span>
        </div>
        <p className="notice" role="note">
          Unverified AI assessment. Citations are references, not proof of
          meaning. Inferences are hypotheses, not evidence. Every generated
          category needs human review against saved results and coverage.
        </p>
        <p className="brief-intro">
          Each successful generation saves an immutable JSON version in this
          case’s SQLite database. Download JSON exports that exact saved
          document. Chat conversations remain ephemeral.
        </p>
        {!!displayedVersions.length && (
          <label>
            Saved brief version
            <select
              aria-label="Saved brief version"
              value={selected || brief?.id || ""}
              disabled={busy}
              onChange={(event) => onChange({ selectedId: event.target.value })}
            >
              {displayedVersions.map((version) => (
                <option key={version.id} value={version.id}>
                  Version {version.version} · {utcDate(version.generated_at)}
                  {freshnessUnknown
                    ? " · freshness unknown"
                    : version.stale
                      ? " · stale"
                      : ""}
                </option>
              ))}
            </select>
          </label>
        )}
        <button
          disabled={busy || loading}
          onClick={() => setAttempt((value) => value + 1)}
        >
          Refresh saved history
        </button>
        {loading && <p role="status">Loading fact sheet and saved history…</p>}
        {loadError && (
          <p role="alert" className="form-error">
            {loadError}{" "}
            <button onClick={() => setAttempt(attempt + 1)}>
              Retry saved history
            </button>
          </p>
        )}
        {brief && (
          <div className="brief-result">
            <h4>Saved version {brief.version}</h4>
            {selected && selected !== brief.id && (
              <p className="notice" role="status">
                Requested version {requestedVersion ?? selected} has not loaded.
                Still displaying saved version {brief.version}; Download JSON
                exports version {brief.version}.
              </p>
            )}
            {freshnessUnknown && (
              <p className="notice" role="status">
                Freshness unknown until current facts are checked. This
                version’s historical references and download remain available.
              </p>
            )}
            {!freshnessUnknown && !stale && (
              <p role="status">Matches the last checked fact snapshot.</p>
            )}
            {stale && (
              <p className="notice" role="status">
                Stale brief: case name, sources or results changed. Generate a
                new version for case chat. This version’s historical references
                and download remain available.
              </p>
            )}
            <a
              className="secondary-button"
              href={`/api/cases/${caseId}/briefs/${brief.id}/download`}
              download
            >
              Download JSON
            </a>
            {statements("Overall summary", brief.overall_summary)}
            {statements("Observations", brief.observations)}
            {statements("Inferences / hypotheses", brief.inferences)}
            {statements("Recommended review / questions", brief.review_next)}
            <p className="brief-provenance brief-generation">
              Generated by {brief.model} in{" "}
              {(brief.duration_ms / 1000).toFixed(1)} s ·{" "}
              {utcDate(brief.generated_at)} · Saved as version {brief.version}.
            </p>
            <p className="brief-provenance">
              {brief.context.notice} Input budget: {brief.context.input_bytes}/
              {brief.context.input_limit_bytes} UTF-8 bytes.
            </p>
            <h4>
              Historical fact snapshot · {brief.facts.length} computed facts
            </h4>
            <p>
              The model received {brief.model_facts.length} of these facts. JSON
              records the exact supplied subset and omissions. Source links open
              the original saved runs.
            </p>
            {facts(brief.facts, true)}
          </div>
        )}
        {status && !status.available && (
          <p className="notice">{status.reason}</p>
        )}
        {error && (
          <p role="alert" className="form-error">
            {error}
          </p>
        )}
        <div className="brief-actions">
          <button
            className="primary-button"
            disabled={
              busy ||
              loading ||
              !status?.available ||
              !sheet?.saved_sources ||
              !!loadError
            }
            onClick={async () => {
              const requestId = crypto.randomUUID();
              onChange({
                busy: true,
                error: "",
                revision: sourceRevision,
                requestId,
              });
              try {
                const value = await api<SavedBrief>(`/cases/${caseId}/brief`, {
                  method: "POST",
                });
                onChange({
                  busy: false,
                  value,
                  selectedId: value.document.id,
                  revision: sourceRevision,
                  requestId,
                });
              } catch (failure) {
                // Structured application rejections occur before saving. A missing/invalid
                // response or unexpected server error cannot establish whether it committed.
                const rejected =
                  failure instanceof ApiError &&
                  (failure.status < 500 ||
                    [502, 503, 504].includes(failure.status));
                onChange({
                  busy: false,
                  error: rejected
                    ? (failure as Error).message
                    : "Save outcome unknown: the response did not confirm whether a version was saved. Refresh and check saved history before generating again; the request may still be finishing.",
                  ...(!rejected ? { selectedId: "" } : {}),
                  requestId,
                });
                checkModel();
              }
            }}
          >
            {busy ? (
              <LoaderCircle className="spin" size={15} />
            ) : (
              <Sparkles size={15} />
            )}
            {busy
              ? "Generating and saving on this machine…"
              : brief
                ? "Generate again"
                : "Generate brief"}
          </button>
          {status && !status.available && (
            <button className="secondary-button" onClick={checkModel}>
              Check again
            </button>
          )}
          {sheet && !sheet.saved_sources && (
            <small>Available once a source has saved analysis results.</small>
          )}
        </div>
      </section>
      <section aria-label="Current fact sheet">
        <h3>Current fact sheet</h3>
        <p>Computed by CDS from saved results · no model involved</p>
        {sheet &&
          (brief ? (
            <details>
              <summary>
                View current full computed facts ({sheet.facts.length})
              </summary>
              {facts(sheet.facts, false)}
            </details>
          ) : (
            facts(sheet.facts, false)
          ))}
      </section>
    </div>
  );
}
