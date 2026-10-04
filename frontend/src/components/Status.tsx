import { Check, CircleAlert, LoaderCircle } from "lucide-react";
import { type Evidence } from "../api";

export default function Status({ value }: { value: Evidence["status"] }) {
  return (
    <span className={`status ${value}`}>
      {value === "running" ? (
        <LoaderCircle className="spin" size={12} />
      ) : value === "completed" ? (
        <Check size={12} />
      ) : value === "failed" ? (
        <CircleAlert size={12} />
      ) : (
        <span className="status-dot" />
      )}
      {value === "completed"
        ? "Finished"
        : value[0].toUpperCase() + value.slice(1)}
    </span>
  );
}
