// All times on screen are shown in the account owner's timezone. The backend
// stores UTC and decides the broker trading day from broker candle stamps; this
// only changes how those instants are rendered.
export const DISPLAY_TIMEZONE = "Asia/Karachi";

export function formatTime(value, { withDate = true } = {}) {
  if (!value) return "—";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return String(value);
  return date.toLocaleString("en-GB", {
    timeZone: DISPLAY_TIMEZONE,
    ...(withDate ? { day: "2-digit", month: "short" } : {}),
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
    hour12: false,
  });
}
