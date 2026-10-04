import { useEffect, useState } from "react";
import { ChevronLeft, ChevronRight, Clock } from "lucide-react";
import { api, utcDate, type Timeline, type TimelineEvent } from "../api";

const PAGE_SIZE = 100;

export default function TimelinePanel({
  caseId,
  sourceRevision,
  onSelect,
}: {
  caseId: string;
  sourceRevision: string;
  onSelect: (event: TimelineEvent) => void;
}) {
  const [from, setFrom] = useState("");
  const [through, setThrough] = useState("");
  const [formError, setFormError] = useState("");
  const [request, setRequest] = useState({
    start: "",
    end: "",
    offset: 0,
    revision: "",
  });
  const [timeline, setTimeline] = useState<Timeline | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");

  useEffect(() => {
    const controller = new AbortController();
    setLoading(true);
    setError("");
    const params = new URLSearchParams({
      start: request.start,
      end: request.end,
      offset: String(request.offset),
      limit: String(PAGE_SIZE),
      revision: request.revision,
    });
    api<Timeline>(`/cases/${caseId}/timeline?${params}`, {
      signal: controller.signal,
    })
      .then((result) => {
        if (!controller.signal.aborted) setTimeline(result);
      })
      .catch((failure) => {
        if (!controller.signal.aborted) setError(failure.message);
      })
      .finally(() => {
        if (!controller.signal.aborted) setLoading(false);
      });
    return () => controller.abort();
  }, [caseId, sourceRevision, request]);

  return (
    <div className="activity-list timeline-panel">
      <form
        className="timeline-filters"
        onSubmit={(event) => {
          event.preventDefault();
          // datetime-local supplies wall-clock text; the label and Z make it UTC.
          const start = from ? `${from}Z` : "";
          const end = through ? `${through}Z` : "";
          if (start && end && Date.parse(start) > Date.parse(end)) {
            setFormError("From must be earlier than or equal to Through.");
            return;
          }
          setFormError("");
          setRequest({ start, end, offset: 0, revision: "" });
        }}
      >
        <label>
          From (UTC)
          <input
            type="datetime-local"
            step="1"
            value={from}
            onChange={(event) => setFrom(event.target.value)}
          />
        </label>
        <label>
          Through (UTC)
          <input
            type="datetime-local"
            step="1"
            value={through}
            onChange={(event) => setThrough(event.target.value)}
          />
        </label>
        <div className="timeline-actions">
          <button className="primary-button" type="submit">
            Apply
          </button>
          <button
            className="secondary-button"
            type="button"
            onClick={() => {
              setFrom("");
              setThrough("");
              setFormError("");
              setRequest({ start: "", end: "", offset: 0, revision: "" });
            }}
          >
            Clear
          </button>
        </div>
      </form>
      {formError && (
        <p role="alert" className="form-error">
          {formError}
        </p>
      )}
      <p className="timeline-range">
        {request.start || request.end
          ? `Applied range: ${request.start ? utcDate(request.start) : "any start"} through ${request.end ? utcDate(request.end) : "any end"} (inclusive).`
          : "Showing all times in UTC."}
      </p>
      {loading ? (
        <p role="status" className="timeline-state">
          Loading timeline…
        </p>
      ) : error ? (
        <div className="timeline-state">
          <p role="alert" className="form-error">
            {error}
          </p>
          <button
            className="secondary-button"
            onClick={() => setRequest({ ...request })}
          >
            Retry timeline
          </button>
        </div>
      ) : (
        timeline && (
          <>
            <div className="pagination" aria-label="Timeline pagination">
              <span>
                {timeline.total ? timeline.offset + 1 : 0}–
                {Math.min(
                  timeline.offset + timeline.events.length,
                  timeline.total,
                )}{" "}
                of {timeline.total} timestamps
              </span>
              <button
                className="icon-button"
                aria-label="Previous timeline page"
                disabled={timeline.offset === 0}
                onClick={() =>
                  setRequest({
                    ...request,
                    offset: Math.max(0, timeline.offset - PAGE_SIZE),
                    revision: timeline.revision,
                  })
                }
              >
                <ChevronLeft size={17} />
              </button>
              <button
                className="icon-button"
                aria-label="Next timeline page"
                disabled={timeline.offset + timeline.limit >= timeline.total}
                onClick={() =>
                  setRequest({
                    ...request,
                    offset: timeline.offset + PAGE_SIZE,
                    revision: timeline.revision,
                  })
                }
              >
                <ChevronRight size={17} />
              </button>
            </div>
            {timeline.events.length ? (
              <ol className="timeline-events">
                {timeline.events.map((event) => (
                  <li key={event.id}>
                    <button
                      className="activity-row timeline-event"
                      onClick={() => onSelect(event)}
                    >
                      <span className="activity-icon">
                        <Clock size={15} />
                      </span>
                      <span className="timeline-event-description">
                        <strong>
                          {event.timestamp_label}
                          {event.deleted ? " · deleted" : ""}
                        </strong>
                        {event.summary && (
                          <span className="timeline-path">{event.summary}</span>
                        )}
                        <span className="timeline-path">
                          {event.artifact_path} · {event.source_name}
                        </span>
                        <small>
                          {event.origin === "import"
                            ? "Imported into CDS · open source"
                            : event.origin === "record"
                              ? "Parsed browser record · open visit"
                              : "Filesystem timestamp · open artifact"}
                        </small>
                      </span>
                      <time dateTime={event.at}>{utcDate(event.at)}</time>
                    </button>
                  </li>
                ))}
              </ol>
            ) : (
              <div className="empty-state">
                <Clock size={28} />
                <h2>No timestamps in this range</h2>
                <p>Clear the range or add evidence to this case.</p>
              </div>
            )}
          </>
        )
      )}
      <p className="audit-note">
        Filesystem timestamps describe accessed, modified, metadata changed, or
        created times; they do not prove user actions. Zero timestamps are
        omitted. Browser visits are labeled separately and may include synced or
        imported activity. Sources without usable file or record times show when
        they were imported into CDS.
      </p>
    </div>
  );
}
