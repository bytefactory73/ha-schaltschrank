// Schaltschrank symbol in Home Assistant's entity dialog (more-info), for KNX entities.
//
// Setup (once): copy this file to /config/www/schaltschrank-link.js and add to configuration.yaml
//   frontend:
//     extra_module_url:
//       - /local/schaltschrank-link.js?v=2
// then restart Home Assistant and reload the browser.
//
// A click stores the entity in localStorage and opens the Schaltschrank panel; the app there (served behind
// ingress from the same origin) takes the entry and opens the entity's channel or device. Nothing in Home
// Assistant is changed. The dialog's inner structure is not a public interface: when an update changes it, the
// symbol just does not appear (errors are swallowed), everything else works as before.
(() => {
  const SLUG = "schaltschrank";
  const HANDOFF_KEY = "schaltschrank-open-entity";
  const BUTTON_CLASS = "schaltschrank-link";
  // A small cabinet: frame, two DIN rails, three breakers.
  const ICON = "M4 2h16v2H4zM4 20h16v2H4zM4 2h2v20H4zM18 2h2v20h-2zM8 6h8v2H8zM8 10h8v2H8zM8 14h2v4H8zM11 14h2v4h-2zM14 14h2v4h-2z";

  const appRoot = () => document.querySelector("home-assistant");
  const hass = () => appRoot()?.hass;

  // First element matching `selector` below `node`, also inside nested shadow roots (only used within the dialog).
  function deepFind(node, selector, depth = 0) {
    if (!node || depth > 8) return null;
    const direct = node.querySelector?.(selector);
    if (direct) return direct;
    for (const child of node.querySelectorAll?.("*") || []) {
      const found = child.shadowRoot && deepFind(child.shadowRoot, selector, depth + 1);
      if (found) return found;
    }
    return null;
  }

  // The sidebar path of the add-on (its ingress panel), else the usual one of a local add-on.
  function panelPath() {
    const panels = Object.values(hass()?.panels || {});
    const panel = panels.find((item) => String(item.config?.ingress || item.url_path || "").includes(SLUG));
    return `/${panel ? panel.url_path : `local_${SLUG}`}`;
  }

  const currentDialog = () => appRoot()?.shadowRoot?.querySelector("ha-more-info-dialog");
  const dialogEntityId = (dialog) => dialog?._entityId || dialog?.entityId || dialog?._params?.entityId || "";

  function openInCabinet(entityId) {
    try {
      localStorage.setItem(HANDOFF_KEY, JSON.stringify({ entity_id: entityId, at: Date.now() }));
    } catch {
      return;
    }
    currentDialog()?.closeDialog?.();
    // Home Assistant's own way to change the page without a reload.
    history.pushState(null, "", panelPath());
    window.dispatchEvent(new CustomEvent("location-changed", { detail: { replace: false } }));
  }

  // The first of the dialog's own header buttons (history, settings, menu). The dialog wraps its header in
  // `ha-adaptive-dialog`, so the buttons are slotted children with a slot name containing "action"; older
  // versions put them directly into `ha-dialog-header` (slot "actionItems").
  function firstActionItem(dialog) {
    const slotted = [...dialog.shadowRoot.querySelectorAll("[slot]")]
      .find((element) => /action/i.test(element.slot) && element.tagName !== "SLOT");
    if (slotted) return slotted;
    const header = deepFind(dialog.shadowRoot, "ha-dialog-header");
    return header && [...header.querySelectorAll('[slot="actionItems"]')].find((element) => element.tagName !== "SLOT");
  }

  // Adds the symbol to the open dialog of a KNX entity (and removes it for other entities).
  function update() {
    const dialog = currentDialog();
    if (!dialog?.shadowRoot) return;
    const entityId = dialogEntityId(dialog);
    let button = dialog.shadowRoot.querySelector(`.${BUTTON_CLASS}`) || deepFind(dialog.shadowRoot, `.${BUTTON_CLASS}`);
    if (hass()?.entities?.[entityId]?.platform !== "knx") {
      button?.remove();
      return;
    }
    if (!button) {
      const anchor = firstActionItem(dialog);
      if (!anchor) return;
      button = document.createElement("ha-icon-button");
      button.className = BUTTON_CLASS;
      button.slot = anchor.slot;
      button.path = ICON;
      button.label = "Im Schaltschrank öffnen";
      button.title = "Im Schaltschrank öffnen";
      button.addEventListener("click", (event) => {
        event.stopPropagation();
        openInCabinet(button.dataset.entityId);
      });
      anchor.parentNode.insertBefore(button, anchor);
    }
    button.dataset.entityId = entityId;
  }

  // Marker to check in the browser console that the module is loaded (`window.schaltschrankLink`).
  window.schaltschrankLink = "2";

  setInterval(() => {
    try {
      update();
    } catch {
      /* Home Assistant changed its dialog: no symbol, nothing else is affected */
    }
  }, 400);
})();
