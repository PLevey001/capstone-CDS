import { useCallback, useEffect, useState } from "react";
import {
  ArrowRight,
  CircleAlert,
  LoaderCircle,
  ScrollText,
  Sparkles,
} from "lucide-react";
import {
  api,
  utcDate,
  type AiStatus,
  type BriefFact,
  type BriefStatement,
  type CaseBrief,
  type FactSheet,
} from "../api";

export type BriefState = {
  busy: boolean;
  value: CaseBrief | null;
  error: string;
  revision?: string;
  requestId?: string;
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
  const { busy, value: brief, error: briefError } = state;
  const [sheet, setSheet] = useState<FactSheet | null>(null);
  const [sheetRevision, setSheetRevision] = useState("");
  const [sheetError, setSheetError] = useState("");
  const [attempt, setAttempt] = useState(0);
  const [status, setStatus] = useState<AiStatus | null>(null);
  const [focused, setFocused] = useState("");

  useEffect(() => {
    const controller = new AbortController();
    setSheet(null);
    setSheetRevision("");
    setSheetError("");
    api<FactSheet>(`/cases/${caseId}/brief`, { signal: controller.signal })
      .then((result) => {
        if (!controller.signal.aborted) {
          setSheet(result);
          setSheetRevision(sourceRevision);
        }
      })
      .catch((failure) => {
        if (!controller.signal.aborted) setSheetError(failure.message);
      });
    return () => controller.abort();
  }, [caseId, sourceRevision, attempt]);

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

  // A brief worded from an earlier fact sheet numbers its facts differently.
  const current =
    !!brief &&
    !!sheet &&
    !busy &&
    !briefError &&
    !sheetError &&
    sheetRevision === sourceRevision &&
    state.revision === sourceRevision &&
    brief.facts_digest === sheet.facts_digest;
  const showFact = (id: string) => {
    setFocused(id);
    document
      .getElementById(`brief-fact-${id}`)
      ?.scrollIntoView({ block: "center" });
  };

  const statements = (title: string, items: BriefStatement[]) =>
    items.length > 0 && (
      <>
        <h4>{title}</h4>
        <ul className="brief-statements" aria-label={title}>
          {items.map((statement, index) => (
            <li key={index}>
              <p>{statement.text}</p>
              <div className="brief-citations">
                <span>References (not proof)</span>
                {statement.facts.map((id) => (
                  <button
                    key={id}
                    className="fact-chip"
                    disabled={!current}
                    title={brief?.facts.find((fact) => fact.id === id)?.text}
                    aria-label={`Show fact ${id}`}
                    onClick={() => showFact(id)}
                  >
                    {id}
                  </button>
                ))}
              </div>
            </li>
          ))}
        </ul>
      </>
    );

  return (
    <div className="activity-list brief-panel">
      <section aria-labelledby="brief-worded">
        <div className="brief-heading">
          <h3 id="brief-worded">Worded brief</h3>
          <span role="status">
            {status === null
              ? "Checking for a local model…"
              : status.available
                ? `Local model ready: ${status.model}`
                : "Local model unavailable"}
          </span>
        </div>
        <p className="notice" role="note">
          Unverified AI wording. Citations are references, not proof of meaning.
          Every generated statement may be wrong or misleading. Check the saved
          run and coverage.
        </p>
        {brief ? (
          <div className="brief-result">
            {!current && (
              <div className="notice" role="status">
                <CircleAlert size={16} />
                Saved results changed after this brief was worded. Generate it
                again to match the current fact sheet.
              </div>
            )}
            {statements("Overview", brief.overview)}
            {statements("Look at first", brief.review)}
            <p className="brief-provenance brief-generation">
              Worded by {brief.model} on this machine in{" "}
              {(brief.duration_ms / 1000).toFixed(1)} s ·{" "}
              {utcDate(brief.generated_at.replace(/\.\d+/, ""))} ·{" "}
              {brief.overview.length + brief.review.length} statements shown
              {brief.withheld > 0 &&
                `, ${brief.withheld} withheld (invalid references, numbers, format, length, or overflow)`}
              . Not saved: it is cleared when the page reloads.
            </p>
            <p className="brief-provenance">
              {brief.context?.notice} Input budget: {brief.context?.input_bytes}
              /{brief.context?.input_limit_bytes} UTF-8 bytes; output reserve:{" "}
              {brief.context?.output_tokens} tokens.
            </p>
          </div>
        ) : (
          <p className="brief-intro">
            A model running on this machine can word a short brief from the fact
            sheet below. It receives a bounded selection with scope and omission
            notices. Reference checks do not verify its conclusions.
          </p>
        )}
        {status && !status.available && (
          <div className="notice">
            <CircleAlert size={16} />
            <span>{status.reason}</span>
          </div>
        )}
        {briefError && (
          <p role="alert" className="form-error">
            {briefError}
          </p>
        )}
        <div className="brief-actions">
          <button
            className="primary-button"
            disabled={
              busy ||
              !status?.available ||
              !sheet?.saved_sources ||
              sheetRevision !== sourceRevision ||
              !!sheetError
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
                const value = await api<CaseBrief>(`/cases/${caseId}/brief`, {
                  method: "POST",
                });
                onChange({
                  busy: false,
                  value,
                  revision: sourceRevision,
                  requestId,
                });
              } catch (failure) {
                onChange({
                  busy: false,
                  error: (failure as Error).message,
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
              ? "Wording the brief on this machine…"
              : brief
                ? "Generate again"
                : "Generate brief"}
          </button>
          {status && !status.available && (
            <button className="secondary-button" onClick={checkModel}>
              Check again
            </button>
          )}
          {busy && <small>This can take a minute or more without a GPU.</small>}
          {sheet && !sheet.saved_sources && (
            <small>Available once a source has saved analysis results.</small>
          )}
        </div>
      </section>

      <section aria-labelledby="brief-facts">
        <div className="brief-heading">
          <h3 id="brief-facts">
            Fact sheet {sheet && <span>{sheet.facts.length}</span>}
          </h3>
          <span>Computed by CDS from saved results · no model involved</span>
        </div>
        {sheetError ? (
          <div className="timeline-state">
            <p role="alert" className="form-error">
              {sheetError}
            </p>
            <button
              className="secondary-button"
              onClick={() => setAttempt(attempt + 1)}
            >
              Retry fact sheet
            </button>
          </div>
        ) : sheet && sheetRevision === sourceRevision ? (
          <ol className="brief-facts">
            {sheet.facts.map((fact) => (
              <li
                key={fact.id}
                id={`brief-fact-${fact.id}`}
                className={current && focused === fact.id ? "focused" : ""}
              >
                <span className="fact-id">{fact.id}</span>
                <span className="fact-text">{fact.text}</span>
                {fact.source_id && (
                  <button
                    className="icon-button"
                    aria-label={`Open the source of fact ${fact.id}`}
                    title="Open in evidence details"
                    onClick={() => onSelect(fact)}
                  >
                    <ArrowRight size={15} />
                  </button>
                )}
              </li>
            ))}
          </ol>
        ) : (
          <p role="status" className="timeline-state">
            Loading fact sheet…
          </p>
        )}
      </section>
      <p className="audit-note">
        <ScrollText size={12} /> The fact sheet counts and quotes saved CDS
        results; it draws no conclusions. Model wording can still be wrong or
        misleading, so read each statement against the facts it cites. Neither
        establishes who did something or why.
      </p>
    </div>
  );
}
