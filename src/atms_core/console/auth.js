// The sign in gate shared by the pages. The token stays in this tab only.
import { AuthError, call, token } from "./api.js";

export function auth({ gate, form, input, error, signout }, { start, stop }) {
  function signOut(message) {
    stop();
    token.clear();
    signout.hidden = true;
    error.hidden = !message; error.textContent = message || "";
    gate.hidden = false; input.value = ""; input.focus();
  }

  form.addEventListener("submit", async (e) => {
    e.preventDefault();
    token.set(input.value.trim());
    try { await call("/v1/map"); gate.hidden = true; signout.hidden = false; start(); }
    catch (err) { token.clear(); error.hidden = false; error.textContent = err instanceof AuthError ? "That token is not accepted for operators." : err.message; }
  });
  signout.addEventListener("click", () => signOut(""));

  return {
    signOut,
    begin() { if (token.get()) { gate.hidden = true; signout.hidden = false; start(); } else signOut(""); },
  };
}
