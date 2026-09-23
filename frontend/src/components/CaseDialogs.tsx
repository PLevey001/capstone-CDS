import { useState } from "react";
import { LoaderCircle, Trash2 } from "lucide-react";
import { api, type Case } from "../api";
import Modal from "./Modal";

export function RenameCase({
  item,
  onClose,
  onRenamed,
}: {
  item: Case;
  onClose: () => void;
  onRenamed: (item: Case) => void;
}) {
  const [name, setName] = useState(item.name);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  return (
    <Modal title="Rename case" onClose={onClose} busy={busy}>
      <form
        onSubmit={async (e) => {
          e.preventDefault();
          if (busy || !name.trim()) return;
          setBusy(true);
          setError("");
          try {
            const updated = await api<Case>(`/cases/${item.id}`, {
              method: "PATCH",
              headers: { "Content-Type": "application/json" },
              body: JSON.stringify({ name: name.trim() }),
            });
            onRenamed(updated);
          } catch (e) {
            setError((e as Error).message);
            setBusy(false);
          }
        }}
      >
        <p className="modal-description">
          Update the name shown throughout your workspace. The change is
          recorded in this case’s activity log.
        </p>
        <label>
          Case name
          <input
            autoFocus
            required
            maxLength={120}
            value={name}
            disabled={busy}
            onChange={(e) => setName(e.target.value)}
          />
        </label>
        {error && (
          <p className="form-error" role="alert">
            {error}
          </p>
        )}
        <div className="modal-actions">
          <button
            type="button"
            className="secondary-button"
            disabled={busy}
            onClick={onClose}
          >
            Cancel
          </button>
          <button
            className="primary-button"
            disabled={busy || !name.trim() || name.trim() === item.name}
          >
            {busy && <LoaderCircle className="spin" size={15} />}Save name
          </button>
        </div>
      </form>
    </Modal>
  );
}

export function DeleteCase({
  item,
  processing,
  onClose,
  onDeleted,
}: {
  item: Case;
  processing: boolean;
  onClose: () => void;
  onDeleted: (cleanupPending: number) => void;
}) {
  const [confirmation, setConfirmation] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  return (
    <Modal title="Delete case" onClose={onClose} busy={busy}>
      <form
        onSubmit={async (e) => {
          e.preventDefault();
          if (busy || processing || confirmation !== item.name) return;
          setBusy(true);
          setError("");
          try {
            const result = await api<{ cleanup_pending: number }>(
              `/cases/${item.id}`,
              {
                method: "DELETE",
              },
            );
            onDeleted(result.cleanup_pending);
          } catch (e) {
            setError((e as Error).message);
            setBusy(false);
          }
        }}
      >
        <p className="modal-description">
          Permanently delete <strong className="case-name">{item.name}</strong>{" "}
          and all of its stored evidence copies, analysis history, findings,
          jobs, and activity records. Original files you uploaded from stay in
          their original locations. This cannot be undone.
        </p>
        {processing && (
          <p className="notice" role="status">
            Wait for queued and running analyses to finish before deleting this
            case.
          </p>
        )}
        <label>
          Type the case name to confirm
          <input
            autoFocus
            required
            autoComplete="off"
            value={confirmation}
            disabled={busy}
            onChange={(e) => setConfirmation(e.target.value)}
          />
        </label>
        {error && (
          <p className="form-error" role="alert">
            {error}
          </p>
        )}
        <div className="modal-actions">
          <button
            type="button"
            className="secondary-button"
            disabled={busy}
            onClick={onClose}
          >
            Cancel
          </button>
          <button
            className="danger-button"
            disabled={busy || processing || confirmation !== item.name}
          >
            {busy ? (
              <LoaderCircle className="spin" size={15} />
            ) : (
              <Trash2 size={15} />
            )}
            {busy ? "Deleting…" : "Delete permanently"}
          </button>
        </div>
      </form>
    </Modal>
  );
}
