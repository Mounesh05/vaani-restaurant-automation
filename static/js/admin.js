/**
 * admin.js — AM Restaurant Admin Panel
 *
 * v2 change: Cancel / Restore actions are now POST forms (not <a> links)
 * for CSRF safety. We intercept the form's submit event to show a
 * confirmation dialog before the POST request is sent.
 */

document.querySelectorAll("form.admin-action-form").forEach((form) => {
  form.addEventListener("submit", (event) => {
    const action = form.dataset.action || "modify";
    const name   = form.dataset.name   || "this guest";
    const date   = form.dataset.date   || "the selected date";
    const time   = form.dataset.time   || "the selected time";

    const label   = action === "cancel" ? "Cancel" : "Restore";
    const message = `${label} booking for ${name} on ${date} at ${time}?`;

    if (!window.confirm(message)) {
      event.preventDefault();
    }
  });
});