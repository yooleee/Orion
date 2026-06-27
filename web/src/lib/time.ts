// =============================================================================
// web/src/lib/time.ts
// -----------------------------------------------------------------------------
// Responsible for: Client-side relative-time formatting. The relay ships ISO
//                  timestamps + a display_tz and lets the SPA render "10h ago" /
//                  "due in 3d" / "5d overdue" — matching the design's relative-time
//                  treatment and the old dashboard's progressive-enhancement approach.
// Role in project: Presentation-only date math. Deadlines are compared against "today
//                  in the relay's display zone" so every viewer sees the same urgency
//                  the relay classified them with (one zone, KI-20).
// =============================================================================

const MINUTE = 60_000;
const HOUR = 60 * MINUTE;
const DAY = 24 * HOUR;

/**
 * Format an ISO timestamp as a short relative label ("10h ago", "2d ago").
 *
 * Args:
 *   iso: an ISO 8601 timestamp (the relay's UTC string).
 *   now: the reference instant (defaults to real now; injectable for tests).
 *
 * Returns: a compact past-relative label. Falls back to "" on an unparseable input
 * (the caller then renders nothing rather than "Invalid Date").
 *
 * Why: the home/timeline show "last activity" as a glanceable relative time, not a raw
 * timestamp. Kept compact (h/d/mo) to fit the design's narrow timestamp column.
 */
export function relativeTime(iso: string, now: number = Date.now()): string {
  const then = Date.parse(iso);
  if (Number.isNaN(then)) return "";
  const diff = now - then;
  if (diff < MINUTE) return "just now";
  if (diff < HOUR) return `${Math.floor(diff / MINUTE)}m ago`;
  if (diff < DAY) return `${Math.floor(diff / HOUR)}h ago`;
  const days = Math.floor(diff / DAY);
  if (days < 30) return `${days}d ago`;
  if (days < 365) return `${Math.floor(days / 30)}mo ago`;
  return `${Math.floor(days / 365)}y ago`;
}

/**
 * Return today's calendar date (YYYY-MM-DD) in the given IANA timezone.
 *
 * Why: deadlines are date-only and judged against the relay's display zone. en-CA gives
 * an ISO-shaped date, so the result sorts/parses like the wire due_date strings.
 */
export function todayInTz(tz: string, now: number = Date.now()): string {
  try {
    return new Intl.DateTimeFormat("en-CA", { timeZone: tz }).format(now);
  } catch {
    // Unknown zone → fall back to the host's local date (still YYYY-MM-DD via en-CA).
    return new Intl.DateTimeFormat("en-CA").format(now);
  }
}

/**
 * Whole calendar days from today-in-tz to a date-only deadline (negative if past).
 *
 * Args:
 *   dueDate: an ISO "YYYY-MM-DD" deadline.
 *   tz: the display timezone (so "today" matches the relay's classification).
 *
 * Why: both dates are date-only, so we diff them as UTC midnights — no DST/offset drift,
 * and the result is a clean integer day count for the deadline label.
 */
export function daysUntil(dueDate: string, tz: string, now: number = Date.now()): number {
  const today = todayInTz(tz, now);
  const a = Date.parse(`${today}T00:00:00Z`);
  const b = Date.parse(`${dueDate}T00:00:00Z`);
  if (Number.isNaN(a) || Number.isNaN(b)) return 0;
  return Math.round((b - a) / DAY);
}

/**
 * Classify a deadline date as overdue / due_soon / upcoming (client-side, for colour).
 *
 * Args:
 *   dueDate: the ISO deadline.
 *   tz: the display timezone.
 *
 * Why: a milestone's `nearest_due` arrives as a bare date (no state), but the chip needs a
 * status to colour by. This mirrors the relay's classify_item rule (overdue < today;
 * due_soon within 7 days; else upcoming) so the milestone chip agrees with the per-item
 * states the server already classified. Returns a Status the StatusSignal can render.
 */
export function deadlineState(
  dueDate: string,
  tz: string,
  now: number = Date.now(),
): "overdue" | "due_soon" | "upcoming" {
  const days = daysUntil(dueDate, tz, now);
  if (days < 0) return "overdue";
  if (days <= 7) return "due_soon";
  return "upcoming";
}

/**
 * The human label for a deadline chip, e.g. "5d overdue", "due in 3d", "due today".
 *
 * Args:
 *   dueDate: the ISO deadline.
 *   tz: the display timezone.
 *
 * Why: pairs with the StatusSignal glyph/colour — the text carries the magnitude so the
 * chip stays legible without colour. Past dates read "Nd overdue", future "due in Nd".
 */
export function deadlineLabel(dueDate: string, tz: string, now: number = Date.now()): string {
  const days = daysUntil(dueDate, tz, now);
  if (days < 0) return `${Math.abs(days)}d overdue`;
  if (days === 0) return "due today";
  if (days === 1) return "due in 1d";
  return `due in ${days}d`;
}

/**
 * The compact relative-day label for the Scheduling time column ("2d ago" / "today" / "in 3d").
 *
 * Args:
 *   dueDate: the ISO deadline.
 *   tz: the display timezone.
 *
 * Why: the schedule's fixed-width time column pairs a glyph + this compact magnitude (the
 * design's wording), distinct from `deadlineLabel`'s "Nd overdue" / "due in Nd" used by the
 * inline deadline chips. Past reads "Nd ago", future "in Nd".
 */
export function scheduleTime(dueDate: string, tz: string, now: number = Date.now()): string {
  const days = daysUntil(dueDate, tz, now);
  if (days < 0) return `${Math.abs(days)}d ago`;
  if (days === 0) return "today";
  return `in ${days}d`;
}
