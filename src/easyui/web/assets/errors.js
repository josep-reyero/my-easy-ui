// The error every data call throws, and a plain description of any error for display.
// Shared by api.js (the server) and static-api.js (a static export).

export class ApiError extends Error {
  constructor({ status = 0, code = "error", message, hint = null, retryAfter = null, problems = null }) {
    super(message);
    this.status = status;
    this.code = code;
    this.hint = hint;
    this.retryAfter = retryAfter; // seconds, when the server asks us to wait
    this.problems = problems;
  }
}

/** Turn any thrown value into {message, hint, code} for display. */
export function describe(error) {
  if (error instanceof ApiError) return { message: error.message, hint: error.hint, code: error.code };
  return {
    message: "This part of the page could not be shown.",
    hint: "Reload the page. If it keeps happening, ask your agent to run easy-ui check.",
    code: "client_error",
  };
}
