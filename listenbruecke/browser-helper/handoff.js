const field = document.getElementById("handoff");
const copy = document.getElementById("copy");
const status = document.getElementById("status");
let expires = 0;
let capturedState = null;

function clear(message) {
  field.value = "";
  copy.disabled = true;
  status.textContent = message;
  // An older handoff tab must never erase a newer login attempt.
  void chrome.storage.session.get("pending").then(({pending}) => {
    if (pending && pending.state === capturedState) {
      return chrome.storage.session.remove("pending");
    }
  }).catch(() => {});
}

chrome.storage.session.get("pending").then(({pending}) => {
  if (!pending || pending.expires <= Date.now()) {
    clear("Die Anmeldung ist abgelaufen. Bitte in der Listenbrücke neu starten.");
    return;
  }
  expires = pending.expires;
  capturedState = pending.state;
  field.value = JSON.stringify({code: pending.code, state: pending.state});
  copy.disabled = false;
  status.textContent = "Wechsle nach dem Kopieren zurück zur Listenbrücke.";
  setTimeout(() => clear("Die Anmeldung ist abgelaufen. Bitte neu starten."), expires - Date.now());
}).catch(() => clear("Die Anmeldedaten konnten nicht geladen werden. Bitte erneut anmelden."));

copy.addEventListener("click", async () => {
  if (Date.now() >= expires) return clear("Die Anmeldung ist abgelaufen. Bitte neu starten.");
  try {
    await navigator.clipboard.writeText(field.value);
    clear("Kopiert. Jetzt im Monsieur-Cuisine-Dialog der Listenbrücke einfügen.");
  } catch (_) {
    field.focus();
    field.select();
    status.textContent = "Bitte Strg+C (Mac: Cmd+C) drücken und in der Listenbrücke einfügen.";
  }
});

field.addEventListener("copy", () => {
  setTimeout(() => clear("Kopiert. Jetzt in der Listenbrücke einfügen."), 0);
});
