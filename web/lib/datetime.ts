/**
 * Centralised date/time formatting.
 *
 * Rule: always show times in the **device timezone**. If the runtime somehow
 * fails to expose one (extremely rare — usually older WebViews) fall back to
 * Asia/Shanghai (UTC+8) so users in our primary deployment region still see
 * sensible times.
 *
 * Locale: zh-CN, 24-hour clock. We're a Chinese product so AM/PM is noise.
 *
 * All inputs are ISO-8601 strings as emitted by the backend (`+00:00` /
 * `Z`-suffixed UTC). `new Date()` parses these correctly and JS internally
 * converts to local on render.
 */

const FALLBACK_TZ = "Asia/Shanghai";
const LOCALE = "zh-CN";

let cachedTz: string | null = null;

export function getTimeZone(): string {
  if (cachedTz) return cachedTz;
  try {
    const tz = Intl.DateTimeFormat().resolvedOptions().timeZone;
    cachedTz = tz && tz.length ? tz : FALLBACK_TZ;
  } catch {
    cachedTz = FALLBACK_TZ;
  }
  return cachedTz;
}

const baseOpts: Intl.DateTimeFormatOptions = {
  hour12: false,
  timeZone: getTimeZone(),
};

/** "HH:mm:ss" — for chat bubble timestamps */
export function formatClockTime(iso: string | Date): string {
  return new Date(iso).toLocaleTimeString(LOCALE, {
    ...baseOpts,
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
  });
}

/** "HH:mm" — compact, for conversation list right-side */
export function formatClockShort(iso: string | Date): string {
  return new Date(iso).toLocaleTimeString(LOCALE, {
    ...baseOpts,
    hour: "2-digit",
    minute: "2-digit",
  });
}

/** "MM-dd HH:mm" — when not today */
export function formatDateTime(iso: string | Date): string {
  return new Date(iso).toLocaleString(LOCALE, {
    ...baseOpts,
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
  });
}

/** "yyyy-MM-dd HH:mm:ss" — for tables / audit views */
export function formatFull(iso: string | Date): string {
  return new Date(iso).toLocaleString(LOCALE, {
    ...baseOpts,
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
  });
}

/**
 * Conversation list smart label:
 *   - today   → "14:23"
 *   - yesterday → "昨天"
 *   - within 7 days → "3天前"
 *   - older  → "05-13"
 */
export function formatRelative(iso: string | Date): string {
  const d = new Date(iso);
  const now = new Date();
  const sameDay =
    d.getFullYear() === now.getFullYear() &&
    d.getMonth() === now.getMonth() &&
    d.getDate() === now.getDate();
  if (sameDay) return formatClockShort(d);

  const diffDays = Math.floor((now.getTime() - d.getTime()) / 86_400_000);
  if (diffDays === 1) return "昨天";
  if (diffDays > 1 && diffDays < 7) return `${diffDays}天前`;

  return d.toLocaleDateString(LOCALE, {
    ...baseOpts,
    month: "2-digit",
    day: "2-digit",
  });
}
