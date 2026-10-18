import { useEffect, useRef, useState } from "react";
import { ArrowRight, LoaderCircle, Sparkles } from "lucide-react";
import { api, type AiStatus, type BriefFact, type ChatAnswer } from "../api";

type Turn = { question: string; result: ChatAnswer };
type GlossaryEntry = ChatAnswer["glossary"][number];

export default function LearnPanel({
  caseId,
  onSelect,
}: {
  caseId: string;
  onSelect: (fact: BriefFact) => void;
}) {
  const [mode, setMode] = useState<"concepts" | "case">("concepts");
  const [question, setQuestion] = useState("");
  const [turns, setTurns] = useState<Turn[]>([]);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [status, setStatus] = useState<AiStatus | null>(null);
  const [attempt, setAttempt] = useState(0);
  const [glossary, setGlossary] = useState<GlossaryEntry[]>([]);
  const [glossaryError, setGlossaryError] = useState("");
  const [glossaryAttempt, setGlossaryAttempt] = useState(0);
  const pending = useRef<AbortController | null>(null);

  useEffect(() => {
    const controller = new AbortController();
    setStatus(null);
    api<AiStatus>("/ai/status", { signal: controller.signal })
      .then((value) => {
        if (!controller.signal.aborted) setStatus(value);
      })
      .catch((failure) => {
        if (!controller.signal.aborted)
          setStatus({
            available: false,
            model: "",
            endpoint: "",
            reason: failure.message,
          });
      });
    return () => controller.abort();
  }, [attempt]);

  useEffect(() => {
    const controller = new AbortController();
    setGlossaryError("");
    api<GlossaryEntry[]>("/ai/glossary", { signal: controller.signal })
      .then((value) => {
        if (!controller.signal.aborted) setGlossary(value);
      })
      .catch((failure) => {
        if (!controller.signal.aborted) setGlossaryError(failure.message);
      });
    return () => controller.abort();
  }, [glossaryAttempt]);

  useEffect(() => () => pending.current?.abort(), []);

  function clear() {
    pending.current?.abort();
    pending.current = null;
    setTurns([]);
    setQuestion("");
    setError("");
    setBusy(false);
  }

  async function send() {
    if (busy || !question.trim()) return;
    const controller = new AbortController();
    pending.current = controller;
    setBusy(true);
    setError("");
    const history = turns.flatMap((turn) => [
      { role: "user", content: turn.question },
      {
        role: "assistant",
        content:
          turn.result.uncertainty ??
          turn.result.answer.map((item) => item.text).join("\n"),
      },
    ]);
    try {
      const result = await api<ChatAnswer>(`/cases/${caseId}/chat`, {
        method: "POST",
        signal: controller.signal,
        body: JSON.stringify({
          mode,
          message: question,
          history,
          facts_digest:
            mode === "case"
              ? (turns.at(-1)?.result.facts_digest ?? null)
              : null,
        }),
      });
      if (!controller.signal.aborted) {
        setTurns((previous) => [...previous, { question, result }]);
        setQuestion("");
      }
    } catch (failure) {
      if (!controller.signal.aborted) {
        setError((failure as Error).message);
        setAttempt((value) => value + 1);
      }
    } finally {
      if (!controller.signal.aborted) setBusy(false);
    }
  }

  const starters =
    mode === "concepts"
      ? [
          "What does SHA-256 tell me?",
          "Does a deleted entry mean its contents were recovered?",
          "How do coverage and job completion differ?",
        ]
      : [
          "What do the saved results cover?",
          "Which coverage limits should I review?",
          "What do these facts say about deleted entries?",
        ];

  return (
    <div className="activity-list brief-panel learn-panel">
      {!caseId && (
        <p className="notice">Create or select a case to start learning.</p>
      )}
      <section aria-labelledby="learn-heading">
        <div className="brief-heading">
          <h3 id="learn-heading">Learn with AI</h3>
          <span role="status">
            {status === null
              ? "Checking for a local model…"
              : status.available
                ? `Local model ready: ${status.model}`
                : "Local model unavailable"}
          </span>
        </div>
        <p className="notice" role="note">
          <strong>Unverified AI wording.</strong> Citations are references, not
          proof of meaning. Every generated statement may be wrong or
          misleading. Check the saved run and coverage.
        </p>
        <p className="brief-intro">
          Ephemeral conversation: kept in browser memory only, cleared on reload
          or when leaving this tab, changing case, mode, name, or source
          results. Conversation text is not saved in the audit log. The model
          has no tools, internet, commands, or evidence controls.
        </p>
        <label>
          Learning mode
          <select
            aria-label="Learning mode"
            value={mode}
            onChange={(event) => {
              clear();
              setMode(event.target.value as "concepts" | "case");
            }}
          >
            <option value="concepts">
              Explain concepts · no case facts sent
            </option>
            <option value="case">Ask about this case · saved facts only</option>
          </select>
        </label>
        <p className="brief-intro">
          {mode === "concepts"
            ? "General educational explanation. No case has been examined in this mode; user descriptions are not verified observations."
            : "Case observations use a fresh, bounded server fact sheet. Earlier answers and user claims are not evidence. Missing facts do not establish absence."}
        </p>
        {status && !status.available && (
          <div className="notice">
            {status.reason}
            <button
              className="secondary-button"
              onClick={() => setAttempt(attempt + 1)}
            >
              Check again
            </button>
          </div>
        )}
        <div className="brief-actions" aria-label="Starter questions">
          {starters.map((starter) => (
            <button
              className="secondary-button"
              key={starter}
              disabled={busy}
              onClick={() => setQuestion(starter)}
            >
              {starter}
            </button>
          ))}
        </div>
      </section>
      <section aria-label="Conversation" aria-live="polite" aria-busy={busy}>
        {turns.map((turn, index) => (
          <article className="learn-turn" key={index}>
            <h4>You</h4>
            <p className="learn-text">{turn.question}</p>
            <h4>
              {mode === "concepts"
                ? "General explanation"
                : "Case observations"}{" "}
              · unverified AI
            </h4>
            {turn.result.uncertainty && <p>{turn.result.uncertainty}</p>}
            {turn.result.answer.map((statement, itemIndex) => (
              <div key={itemIndex}>
                <p className="learn-text">{statement.text}</p>
                <div className="brief-citations">
                  <span>References (not proof)</span>
                  {statement.facts.map((id) => {
                    const fact = turn.result.facts.find(
                      (entry) => entry.id === id,
                    );
                    const lesson = turn.result.glossary.find(
                      (entry) => entry.id === id,
                    );
                    if (lesson) {
                      return (
                        <div className="learn-reference" key={id}>
                          <strong>Reviewed concept reference · {id}</strong>
                          <p>{lesson.text}</p>
                          <a href={lesson.url} target="_blank" rel="noreferrer">
                            {lesson.title} · reference
                          </a>
                        </div>
                      );
                    }
                    return (
                      <details key={id}>
                        <summary>{id}</summary>
                        <p>{fact?.text}</p>
                        {fact?.source_id && (
                          <button
                            className="secondary-button"
                            onClick={() => onSelect(fact)}
                            aria-label={`Open source for ${id}`}
                          >
                            <ArrowRight size={14} />
                            Open saved run
                          </button>
                        )}
                      </details>
                    );
                  })}
                </div>
              </div>
            ))}
            <p className="brief-provenance">
              {turn.result.context.notice} Input:{" "}
              {turn.result.context.input_bytes}/
              {turn.result.context.input_limit_bytes} UTF-8 bytes; output
              reserve: {turn.result.context.output_tokens} tokens.{" "}
              {turn.result.withheld} statements withheld (invalid references,
              numbers, format, length, or overflow).
            </p>
          </article>
        ))}
        {busy && (
          <p role="status">
            <LoaderCircle size={15} className="spin" /> Generating on this
            machine… CPU inference can take a minute or more.
          </p>
        )}
      </section>
      <form
        onSubmit={(event) => {
          event.preventDefault();
          void send();
        }}
      >
        <label htmlFor="learn-question">Your question</label>
        <textarea
          id="learn-question"
          rows={3}
          value={question}
          maxLength={2000}
          disabled={busy}
          aria-describedby="learn-limits"
          onChange={(event) => setQuestion(event.target.value)}
        />
        <p id="learn-limits" className="brief-intro">
          Each message allows up to 2000 UTF-8 bytes; at most four prior turns.
          All fields must also fit a 16 KiB JSON request and the model context
          budget, including reference notes. These limits apply together, so
          maximum-length messages may require clearing sooner. History is never
          silently trimmed.
        </p>
        {error && (
          <p role="alert" className="form-error">
            {error}
          </p>
        )}
        <div className="brief-actions">
          <button
            className="primary-button"
            type="submit"
            disabled={
              busy ||
              !caseId ||
              !status?.available ||
              !question.trim() ||
              turns.length > 4
            }
          >
            <Sparkles size={15} />
            {error ? "Retry question" : "Send question"}
          </button>
          <button className="secondary-button" type="button" onClick={clear}>
            Clear conversation
          </button>
        </div>
        {turns.length > 4 && (
          <p role="status">
            Conversation limit reached. Clear the conversation to ask another
            question.
          </p>
        )}
      </form>
      <details>
        <summary>Reviewed local concept notes · available without AI</summary>
        {glossaryError && (
          <div>
            <p role="alert" className="form-error">
              {glossaryError}
            </p>
            <button
              className="secondary-button"
              onClick={() => setGlossaryAttempt(glossaryAttempt + 1)}
            >
              Retry concept notes
            </button>
          </div>
        )}
        {glossary.map((entry) => (
          <div key={entry.id}>
            <h4>{entry.title}</h4>
            <p>{entry.text}</p>
            <a href={entry.url} target="_blank" rel="noreferrer">
              Background reference
            </a>
          </div>
        ))}
      </details>
    </div>
  );
}
