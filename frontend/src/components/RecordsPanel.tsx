import { useEffect, useRef, useState } from "react";
import { ChevronLeft, ChevronRight, Search, X } from "lucide-react";
import { api, utcDate, type Detail, type ParsedRecord } from "../api";

export default function RecordsPanel({
  evidence,
  selectedId,
  onSelect,
  onSource,
}: {
  evidence: Detail;
  selectedId: number | null;
  onSelect: (id: number | null) => void;
  onSource: (id: number) => void;
}) {
  const [query, setQuery] = useState("");
  const [offset, setOffset] = useState(0);
  const [items, setItems] = useState<ParsedRecord[]>([]);
  const [total, setTotal] = useState(0);
  const [selected, setSelected] = useState<ParsedRecord | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [retry, setRetry] = useState(0);
  const selectedPanel = useRef<HTMLElement>(null);

  useEffect(() => {
    selectedPanel.current?.scrollIntoView({ block: "nearest" });
  }, [selected?.id]);

  useEffect(() => {
    const controller = new AbortController();
    setLoading(true);
    setError("");
    const load = async () => {
      if (!evidence.run_id) {
        setItems([]);
        setTotal(0);
        setLoading(false);
        return;
      }
      try {
        const params = new URLSearchParams({
          run_id: evidence.run_id,
          q: query,
          offset: String(offset),
        });
        const [page, record] = await Promise.all([
          api<{ items: ParsedRecord[]; total: number }>(
            `/evidence/${evidence.id}/records?${params}`,
            { signal: controller.signal },
          ),
          selectedId === null
            ? null
            : api<ParsedRecord>(
                `/evidence/${evidence.id}/records/${selectedId}`,
                { signal: controller.signal },
              ),
        ]);
        if (record && record.run_id !== evidence.run_id) {
          throw new Error(
            "The selected record does not belong to this saved run.",
          );
        }
        if (!controller.signal.aborted) {
          setItems(page.items);
          setTotal(page.total);
          setSelected(record);
        }
      } catch (failure) {
        if (!controller.signal.aborted) setError((failure as Error).message);
      } finally {
        if (!controller.signal.aborted) setLoading(false);
      }
    };
    void load();
    return () => controller.abort();
  }, [evidence.id, evidence.run_id, query, offset, selectedId, retry]);

  const exports = new URLSearchParams({
    run_id: evidence.run_id || "",
    q: query,
  });
  return (
    <section className="records-panel" aria-label="Parsed records">
      <h3>
        Parsed records <span>{evidence.record_count}</span>
      </h3>
      <p className="muted">
        Saved parsed records. Recorded URLs are displayed as evidence text.
      </p>
      {evidence.coverage?.scope && (
        <p className="muted">{evidence.coverage.scope}</p>
      )}
      <div className="search-input artifact-search">
        <Search size={16} />
        <input
          aria-label="Search records"
          placeholder="Search summaries or URLs…"
          maxLength={200}
          value={query}
          onChange={(event) => {
            setQuery(event.target.value);
            setOffset(0);
          }}
        />
      </div>
      {evidence.run_id && (
        <>
          <div className="record-exports">
            <a
              className="secondary-button"
              href={`/api/cases/${evidence.case_id}/records/export?${exports}&format=csv`}
            >
              Export matching records CSV
            </a>
            <a
              className="secondary-button"
              href={`/api/cases/${evidence.case_id}/records/export?${exports}&format=json`}
            >
              Export matching records JSON
            </a>
          </div>
          <p className="muted">
            Exports include all matches from this run. CSV prefixes formula-like
            text with an apostrophe; JSON preserves saved text.
          </p>
        </>
      )}
      {loading ? (
        <p role="status">Loading records…</p>
      ) : error ? (
        <>
          <p className="form-error" role="alert">
            {error}
          </p>
          <button
            className="secondary-button"
            onClick={() => setRetry(retry + 1)}
          >
            Retry records
          </button>
        </>
      ) : (
        <>
          {selected && (
            <section
              ref={selectedPanel}
              className="record-detail"
              aria-label="Selected record"
            >
              <div className="record-heading">
                <h3>Saved record</h3>
                <button
                  className="icon-button"
                  aria-label="Clear selected record"
                  onClick={() => onSelect(null)}
                >
                  <X size={16} />
                </button>
              </div>
              <dl>
                <dt>Record kind</dt>
                <dd>{selected.kind}</dd>
                <dt>Summary</dt>
                <dd>{selected.summary}</dd>
                {selected.kind === "browser_visit" && (
                  <>
                    <dt>Browser schema</dt>
                    <dd>
                      {String(selected.details.browser ?? "Not recorded")}
                    </dd>
                    <dt>URL</dt>
                    <dd className="record-url">
                      {String(selected.details.url ?? "URL record missing")}
                    </dd>
                    <dt>Title</dt>
                    <dd>{String(selected.details.title ?? "Not recorded")}</dd>
                  </>
                )}
                <dt>Timestamp (UTC)</dt>
                <dd>
                  {selected.at ? utcDate(selected.at) : "No usable timestamp"}
                </dd>
                <dt>Original record key</dt>
                <dd>{selected.source_key}</dd>
                <dt>Saved run</dt>
                <dd>
                  <code>{selected.run_id}</code>
                </dd>
                <dt>Parser</dt>
                <dd>{selected.parser}</dd>
              </dl>
              <button
                className="secondary-button record-source"
                onClick={() => onSource(selected.artifact_id)}
              >
                Open source artifact: {selected.artifact_path}
              </button>
              <p className="muted">
                A record alone does not identify a person or establish their
                actions. Original fields and timestamp semantics retained by
                this parser are listed below.
              </p>
              <details>
                <summary>Original fields and parsing notes</summary>
                <p className="muted">
                  Large integers are saved as exact decimal text and listed in
                  integer_text_fields to avoid rounding in the browser.
                </p>
                <pre>{JSON.stringify(selected.details, null, 2)}</pre>
              </details>
            </section>
          )}
          <div className="record-list">
            {items.map((record) => (
              <button
                key={record.id}
                className="record-row"
                onClick={() => onSelect(record.id)}
                aria-pressed={selectedId === record.id}
              >
                <strong>{record.summary}</strong>
                <span>
                  {record.kind === "browser_visit"
                    ? String(record.details.url ?? "URL record missing")
                    : record.kind}
                </span>
                <small>
                  {record.at ? utcDate(record.at) : "No usable timestamp"} ·
                  Record {record.source_key}
                </small>
              </button>
            ))}
          </div>
          {!items.length && (
            <p className="muted">
              {query
                ? "No matching records in this run."
                : "No parsed records saved for this run. Check its coverage for what was examined."}
            </p>
          )}
          <div className="pagination" aria-label="Record pagination">
            <span>
              {total ? offset + 1 : 0}–{Math.min(offset + items.length, total)}{" "}
              of {total} records
            </span>
            <button
              className="icon-button"
              aria-label="Previous record page"
              disabled={offset === 0}
              onClick={() => setOffset(offset - 50)}
            >
              <ChevronLeft size={17} />
            </button>
            <button
              className="icon-button"
              aria-label="Next record page"
              disabled={offset + 50 >= total}
              onClick={() => setOffset(offset + 50)}
            >
              <ChevronRight size={17} />
            </button>
          </div>
        </>
      )}
    </section>
  );
}
