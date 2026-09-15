export const DISPLAY_TIMEZONE = "Asia/Karachi";

// SQLite dates may lack an offset. Backend timestamps are UTC, never browser local time.
export function parseUtcTime(value) {
  if (value == null || value === "") return null;
  if (value instanceof Date) return Number.isNaN(value.getTime()) ? null : value;
  if (typeof value === "number") {
    const date = new Date(value * 1000);
    return Number.isNaN(date.getTime()) ? null : date;
  }
  let text = String(value).trim().replace(" ", "T");
  if (!/(Z|[+-]\d{2}:?\d{2})$/i.test(text)) {
    if (/^\d{4}-\d{2}-\d{2}$/.test(text)) text += "T00:00:00";
    text += "Z";
  }
  const date = new Date(text);
  return Number.isNaN(date.getTime()) ? null : date;
}

export function formatTime(value, { withDate = true, seconds = true } = {}) {
  const date = parseUtcTime(value);
  if (!date) return "—";
  return new Intl.DateTimeFormat("en-GB", {
    timeZone: DISPLAY_TIMEZONE,
    ...(withDate ? { day: "2-digit", month: "short" } : {}),
    hour: "2-digit", minute: "2-digit",
    ...(seconds ? { second: "2-digit" } : {}),
    hourCycle: "h23",
  }).format(date);
}

export function chartTime(value) {
  return formatTime(value, { withDate: false, seconds: false });
}
