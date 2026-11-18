// How a row of components shares its width (no DOM here, so it can be tested on its own).

/**
 * How many of a row's n components go side by side in `width` pixels, each at least `basis` wide with `gap`
 * between them: as many as fit, then spread over balanced lines (5 with room for 4 go 3 + 2, not 4 + 1;
 * 3 with room for 2 go 2 + 1, the last one as wide as the line).
 */
export function columnsFor(n, width, basis, gap) {
  if (!(n > 0)) return 1;
  const fit = Math.max(1, Math.floor((width + gap) / (basis + gap)));
  if (fit >= n) return n;
  const lines = Math.ceil(n / fit);
  return Math.ceil(n / lines);
}
