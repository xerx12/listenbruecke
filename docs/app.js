"use strict";
const checkboxes = [...document.querySelectorAll("[data-demo-check]")];
for (const checkbox of checkboxes) {
  checkbox.addEventListener("change", () => {
    for (const other of checkboxes) other.checked = checkbox.checked;
    document.querySelector(".demo-scene").classList.toggle("demo-completed", checkbox.checked);
    document.querySelector("[data-demo-status]").textContent = checkbox.checked ? "Erledigt" : "Offen";
    document.getElementById("demo-caption").textContent = checkbox.checked ? "Demo: Tomaten sind in allen Beispiel-Listen erledigt." : "Ein interaktives Beispiel. Keine Konten verbunden.";
  });
}
const copyButton = document.getElementById("copy-install");
let copyTimer;
copyButton.addEventListener("click", async () => {
  const message = document.getElementById("copy-message");
  try {
    await navigator.clipboard.writeText(document.getElementById("install-command").textContent);
    message.textContent = "Installationsbefehle kopiert.";
  } catch {
    message.textContent = "Bitte die Befehle im Terminal-Feld markieren und kopieren.";
  }
  message.hidden = false;
  clearTimeout(copyTimer);
  copyTimer = setTimeout(() => { message.hidden = true; }, 4000);
});
