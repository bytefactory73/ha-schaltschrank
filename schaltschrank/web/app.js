const searchInput = document.querySelector("#search");
const resultsNode = document.querySelector("#results");
const detailNode = document.querySelector("#detail");
const welcomeNode = document.querySelector("#welcome");
const settingsNode = document.querySelector("#settings");
const searchViewNode = document.querySelector("#search-view");
const overviewNode = document.querySelector("#overview-list");
const graphNode = document.querySelector("#graph-view");
const overviewItemsNode = document.querySelector("#overview-items");
const detailContent = document.querySelector("#detail-content");
const assignmentDialog = document.querySelector("#assignment-dialog");
const deviceDialog = document.querySelector("#device-dialog");
const deviceChannelDialog = document.querySelector("#device-channel-dialog");
const fuseEditDialog = document.querySelector("#fuse-edit-dialog");
const importDialog = document.querySelector("#import-dialog");
const lastLocationKey = "schaltschrank-last-location";
const reviewModeKey = "schaltschrank-review-mode";
const naturalCollator = new Intl.Collator("de-DE", { numeric: true, sensitivity: "base" });
let searchTimer;
let currentDetail;
let assignmentMode = "assignment";
let reviewModeEnabled = sessionStorage.getItem(reviewModeKey) === "true";
let importToken;
let toastTimer;
let currentSearchItems = [];
let selectedSearchCategory = "";
let selectedReviewSearchTerm = "";
let currentOverviewItems = [];
let selectedTerminalLocation = "";
let currentPageState = { view: "home" };
const autocompleteOptions = { locations: [], fuses: [], fiDevices: [], positions: ["u", "o", "li", "mi", "re", "1", "2"] };
let assignmentTerminalLookupTimer;
let assignmentTerminalLookupSequence = 0;
const SEARCH_CATEGORIES = [
  { id: "device", label: "Gerät", types: ["device"] },
  { id: "channel", label: "Kanal", types: ["channel"] },
  { id: "terminal", label: "Klemme", types: ["terminal"] },
  { id: "load", label: "Verbraucher", types: ["load"] },
  { id: "group", label: "Gruppenadresse", types: ["group"] },
  { id: "fuse", label: "Sicherung", types: ["fuse"] },
  { id: "fi", label: "FI", types: ["fi"] },
  { id: "homeassistant", label: "Home Assistant", types: ["homeassistant"] },
];
const OVERVIEW_CATEGORIES = [
  { id: "fuses", label: "Sicherungen", countKey: "fuses", typeLabel: "SICHERUNG" },
  { id: "terminals", label: "Klemmen", countKey: "terminals", typeLabel: "KLEMME" },
  { id: "devices", label: "Geräte", countKey: "devices", typeLabel: "GERÄT" },
  { id: "groups", label: "Gruppenadressen", countKey: "group_addresses", typeLabel: "GRUPPENADRESSE" },
  // Verbraucher have no page of their own: an entry opens the Klemme that feeds it.
  { id: "loads", label: "Verbraucher", countKey: "loads", typeLabel: "VERBRAUCHER" },
  { id: "locations", label: "Orte", countKey: "locations", typeLabel: "ORT" },
  // Only shown when the server finds Home Assistant's files (the stats then carry the count).
  { id: "homeassistant", label: "Home Assistant", countKey: "ha_entities", typeLabel: "HOME ASSISTANT", optional: true },
];
// `hex` colours the wires in the Gesamtgraph (white is drawn light grey so it stays visible).
const WIRE_COLORS = [
  { code: "grge", name: "Grün-Gelb", css: "grge", hex: "#5d8f17" },
  { code: "br", name: "Braun", css: "br", hex: "#875437" },
  { code: "bk", name: "Schwarz", css: "bk", hex: "#252a29" },
  { code: "bl", name: "Blau", css: "bl", hex: "#3278bd" },
  { code: "gr", name: "Grau", css: "gr", hex: "#929997" },
  { code: "wh", name: "Weiß", css: "wh", hex: "#c4c9c5" },
  { code: "rd", name: "Rot", css: "rd", hex: "#cf3b35" },
  { code: "gn", name: "Grün", css: "gn", hex: "#25804a" },
  { code: "ye", name: "Gelb", css: "ye", hex: "#d8b818" },
  { code: "og", name: "Orange", css: "og", hex: "#e88730" },
  { code: "vi", name: "Violett", css: "vi", hex: "#754c9d" },
];

const esc = (value) => String(value ?? "").replace(/[&<>"']/g, (char) => ({
  "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
}[char]));

function reviewDateLabel(value) {
  if (!value) return "";
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? "" : new Intl.DateTimeFormat("de-DE", {
    dateStyle: "medium",
    timeStyle: "short",
  }).format(date);
}
function usedCheckboxMarkup(used) {
  return `<span class="used-checkbox${used ? " is-used" : ""}" role="img" aria-label="${used ? "In Verwendung" : "Nicht in Verwendung"}" title="${used ? "In Verwendung" : "Nicht in Verwendung"}"><span aria-hidden="true"></span></span>`;
}

function terminalUsageClass(used) {
  return used ? "" : "terminal-unused";
}

function reviewControlMarkup(type, id, reviewedAt, className = "") {
  if (!reviewModeEnabled) return "";
  const timestamp = reviewDateLabel(reviewedAt);
  const checked = Boolean(reviewedAt);
  return `<span class="review-control ${className}"><label class="review-checkbox"><input type="checkbox" data-review-type="${esc(type)}" data-review-id="${esc(id)}"${checked ? " checked" : ""}><span>Überprüft</span></label><small class="review-timestamp" aria-live="polite">${timestamp ? `Am ${esc(timestamp)}` : "Noch nicht überprüft"}</small></span>`;
}

function buildSearchQuery() {
  return [searchInput.value.trim(), selectedReviewSearchTerm].filter(Boolean).join(" ");
}

function renderReviewSearchTerm() {
  const tags = document.querySelector("#search-filter-tags");
  const label = selectedReviewSearchTerm;
  tags.innerHTML = label
    ? `<span class="search-filter-tag"><span>${esc(label)}</span><button type="button" data-remove-review-search aria-label="${esc(label)}-Filter entfernen">×</button></span>`
    : "";
  document.querySelectorAll("[data-review-search-term]").forEach((button) => {
    const selected = button.dataset.reviewSearchTerm === label;
    button.classList.toggle("selected", selected);
    button.setAttribute("aria-pressed", String(selected));
  });
}

function extractReviewSearchTerm() {
  const value = searchInput.value;
  const hasNotReviewed = /nicht\s+überprüft/i.test(value);
  const hasReviewed = /überprüft/i.test(value);
  if (!hasNotReviewed && !hasReviewed) return false;

  selectedReviewSearchTerm = hasNotReviewed ? "Nicht überprüft" : "Überprüft";
  const caret = searchInput.selectionStart ?? value.length;
  const clean = (text) => text.replace(/nicht\s+überprüft/gi, " ").replace(/überprüft/gi, " ").replace(/\s+/g, " ").trim();
  searchInput.value = clean(value);
  searchInput.setSelectionRange(clean(value.slice(0, caret)).length, clean(value.slice(0, caret)).length);
  renderReviewSearchTerm();
  return true;
}

function clearReviewSearchTerm() {
  selectedReviewSearchTerm = "";
  renderReviewSearchTerm();
}

renderReviewSearchTerm();

async function request(url, options) {
  const response = await fetch(url, options);
  const data = response.headers.get("content-type")?.includes("application/json")
    ? await response.json()
    : await response.blob();
  if (!response.ok) throw new Error(data.error || `HTTP ${response.status}`);
  return data;
}

// Sends a JSON payload to a write endpoint.
function postJson(url, payload) {
  return request(url, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(payload) });
}

// Reloads the record page shown and keeps the scroll position (after a change on it).
async function refreshRecord() {
  const scrollY = window.scrollY;
  await openRecord(currentPageState.type, currentPageState.id, false);
  window.scrollTo(0, scrollY);
}

// Write controls (edit, add, connect, cut, delete) exist only in the Überprüfungsmodus.
function reviewOnly(markup) {
  return reviewModeEnabled ? markup : "";
}

// Static write controls carry `.review-only`; styles.css hides them unless the body has `review-mode`.
function applyReviewMode() {
  document.body.classList.toggle("review-mode", reviewModeEnabled);
}

// Asks first, then runs a write request; shows its error as a toast. Returns the response, or null.
async function confirmedPost(question, url, payload) {
  if (!confirm(question)) return null;
  try {
    return await postJson(url, payload);
  } catch (error) {
    notify(error.message, true);
    return null;
  }
}

function notify(message, isError = false) {
  const toast = document.querySelector("#toast");
  toast.textContent = message;
  toast.classList.toggle("is-error", isError);
  toast.classList.add("visible");
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => toast.classList.remove("visible"), 3200);
}

function setWireColor(code) {
  document.querySelector("#wire-color-value").value = code;
  document.querySelectorAll(".wire-color-option").forEach((button) => {
    const selected = button.dataset.color === code;
    button.setAttribute("aria-checked", String(selected));
    button.classList.toggle("selected", selected);
  });
}

function initializeWireColorPicker() {
  const picker = document.querySelector("#wire-color-options");
  picker.innerHTML = WIRE_COLORS.map((color) => `<button class="wire-color-option" type="button" role="radio" aria-checked="false" data-color="${color.code}" title="${color.name}"><i class="wire-swatch wire-swatch-${color.css}"></i><span>${color.code}</span></button>`).join("");
  picker.addEventListener("click", (event) => {
    const button = event.target.closest("[data-color]");
    if (button) setWireColor(button.dataset.color);
  });
  setWireColor(document.querySelector("#wire-color-value").value || "bk");
}

async function loadPicklists(location = "") {
  try {
    const locations = await request("api/locations");
    autocompleteOptions.locations = locations.map((item) => item.name);
  } catch { /* Keep manual entry available if options are offline. */ }
  try {
    // A Klemme can sit in another room than its Sicherung: every Sicherung is offered (its room is shown).
    autocompleteOptions.fuses = await request("api/fuses");
  } catch { /* Keep manual entry available if options are offline. */ }
  document.querySelectorAll('[data-suggestions]').forEach((input) => {
    if (document.activeElement === input && input.dataset.autocompleteTyping === "true") renderAutocomplete(input);
  });
}

function renderAutocomplete(input) {
  const popup = input.parentElement.querySelector(".autocomplete-popup");
  if (!popup) return;
  if (input.dataset.selectedSuggestion === "true") return;
  const kind = input.dataset.suggestions;
  const options = autocompleteOptions[kind] || [];
  const query = input.value.trim().toLocaleLowerCase();
  const matches = options.filter((item) => {
    const display = kind === "locations" || kind === "positions" ? item : kind === "fiDevices"
      ? [item.number ? `FI ${item.number}` : "FI", item.label].filter(Boolean).join(" · ")
      : kind === "fuses" ? `${item.label} ${item.location}` : item.label;
    return !query || display.toLocaleLowerCase().includes(query);
  });
  const sortValue = (item) => kind === "locations" || kind === "positions" ? item
    : kind === "fiDevices" ? item.number ? `0 ${item.number}` : `1 ${item.label}`
      : item.label;
  matches.sort((left, right) => naturalCollator.compare(sortValue(left), sortValue(right)));
  matches.length = Math.min(matches.length, 8);
  popup.innerHTML = matches.map((item) => {
    // FI and Sicherung are picked by id (the same designation can exist in several rooms).
    const value = kind === "locations" || kind === "positions" ? item : kind === "fiDevices" || kind === "fuses" ? String(item.id) : item.label;
    const display = kind === "locations" || kind === "positions" ? item : kind === "fiDevices"
      ? [item.number ? `FI ${item.number}` : "FI", item.label].filter(Boolean).join(" · ")
      : item.label;
    const detail = kind === "fuses" ? [item.location, item.rating].filter(Boolean).join(" · ") : "";
    return `<button class="autocomplete-option" type="button" role="option" data-value="${esc(value)}" data-display="${esc(display)}"><strong>${esc(display)}</strong>${detail ? `<small>${esc(detail)}</small>` : ""}</button>`;
  }).join("");
  popup.hidden = matches.length === 0;
  popup.classList.toggle("opens-up", popup.hidden === false && window.innerHeight - input.getBoundingClientRect().bottom < 190);
  input.parentElement.classList.toggle("is-open", !popup.hidden);
  input.setAttribute("aria-expanded", String(!popup.hidden));
}

function initializeAutocomplete() {
  document.querySelectorAll("[data-suggestions]").forEach((input) => {
    const popup = input.parentElement.querySelector(".autocomplete-popup");
    input.setAttribute("aria-autocomplete", "list");
    input.addEventListener("input", (event) => {
      if (!event.isTrusted) return;
      input.dataset.autocompleteTyping = "true";
      delete input.dataset.selectedSuggestion;
      // Typing forgets a picked Sicherung; the server then looks the designation up (and never creates one).
      if (input.dataset.suggestions === "fuses") input.form.elements.fuse_id.value = "";
      renderAutocomplete(input);
    });
    input.addEventListener("keydown", (event) => {
      if (event.key === "Escape") {
        popup.hidden = true;
        input.parentElement.classList.remove("is-open");
        input.setAttribute("aria-expanded", "false");
        delete input.dataset.autocompleteTyping;
      } else if (event.key === "ArrowDown" && !popup.hidden) {
        event.preventDefault();
        popup.querySelector(".autocomplete-option")?.focus();
      }
    });
    popup.addEventListener("mousedown", (event) => event.preventDefault());
    popup.addEventListener("click", (event) => {
      const option = event.target.closest("[data-value]");
      if (!option) return;
      input.value = option.dataset.display || option.dataset.value;
      input.dataset.selectedSuggestion = "true";
      delete input.dataset.autocompleteTyping;
      if (input.dataset.suggestions === "fiDevices") input.form.elements.existing_fi_id.value = option.dataset.value;
      if (input.dataset.suggestions === "fuses") input.form.elements.fuse_id.value = option.dataset.value;
      popup.hidden = true;
      input.parentElement.classList.remove("is-open");
      input.setAttribute("aria-expanded", "false");
      input.dispatchEvent(new Event("input", { bubbles: true }));
      input.dispatchEvent(new Event("change", { bubbles: true }));
      popup.hidden = true;
      input.focus();
    });
  });
  document.addEventListener("click", (event) => {
    if (event.target.closest(".autocomplete-field")) return;
    document.querySelectorAll(".autocomplete-popup").forEach((popup) => {
      popup.hidden = true;
      popup.parentElement.classList.remove("is-open");
    });
    document.querySelectorAll('[data-suggestions]').forEach((input) => input.setAttribute("aria-expanded", "false"));
  });
}

async function loadFuseFiOptions(locationId, selectedFiId = "") {
  const form = document.querySelector("#fuse-edit-form");
  const selectedFiInput = form.elements.existing_fi_display;
  const selectedFiIdInput = form.elements.existing_fi_id;
  const numberInput = form.elements.fi_number;
  const labelInput = form.elements.fi_label;
  const editControls = document.querySelector("#existing-fi-edit-controls");
  const warning = editControls.querySelector(".existing-fi-warning");
  autocompleteOptions.fiDevices = [];
  selectedFiInput.value = "";
  selectedFiIdInput.value = "";
  if (!locationId) {
    numberInput.readOnly = false;
    labelInput.readOnly = false;
    editControls.hidden = true;
    warning.hidden = true;
    return;
  }
  try {
    const locationQuery = /^\d+$/.test(String(locationId))
      ? `location_id=${encodeURIComponent(locationId)}`
      : `location=${encodeURIComponent(locationId)}`;
    autocompleteOptions.fiDevices = await request(`api/fi-devices?${locationQuery}`);
  } catch {
    autocompleteOptions.fiDevices = [];
  }
  const selectedFi = autocompleteOptions.fiDevices.find((item) => String(item.id) === String(selectedFiId));
  if (selectedFi) {
    selectedFiIdInput.value = String(selectedFi.id);
    selectedFiInput.value = [selectedFi.number ? `FI ${selectedFi.number}` : "FI", selectedFi.label].filter(Boolean).join(" · ");
    numberInput.value = selectedFi.number || "";
    labelInput.value = selectedFi.label || "";
  }
  numberInput.readOnly = Boolean(selectedFi);
  labelInput.readOnly = Boolean(selectedFi);
  editControls.hidden = !selectedFi;
  editControls.querySelector("#edit-existing-fi").textContent = "FI-Daten bearbeiten";
  warning.hidden = true;
}

async function openFuseEditor(record = null) {
  const form = document.querySelector("#fuse-edit-form");
  const locationField = document.querySelector("#fuse-location-field");
  const editControls = document.querySelector("#existing-fi-edit-controls");
  form.reset();
  form.elements.fuse_id.value = record?.id || "";
  document.querySelector("#delete-fuse-button").hidden = !record;
  form.elements.location.required = !record;
  form.elements.location.value = record ? "" : localStorage.getItem(lastLocationKey) || "";
  locationField.hidden = Boolean(record);
  form.elements.label.value = record?.label || "";
  form.elements.rating.value = record?.rating || "";
  form.elements.description.value = record?.description || "";
  form.elements.fi_number.value = record?.fi_number || "";
  form.elements.fi_label.value = record?.fi_label || "";
  form.elements.fi_number.readOnly = false;
  form.elements.fi_label.readOnly = false;
  editControls.hidden = true;
  editControls.querySelector("#edit-existing-fi").textContent = "FI-Daten bearbeiten";
  editControls.querySelector(".existing-fi-warning").hidden = true;
  form.querySelectorAll('input[name="phases"]').forEach((checkbox) => {
    checkbox.checked = (record?.phases || []).includes(checkbox.value);
  });
  document.querySelector("#fuse-edit-dialog h2").textContent = record ? "Sicherung bearbeiten" : "Neue Sicherung";
  form.querySelector('button[type="submit"]').textContent = record ? "Sicherung speichern" : "Sicherung anlegen";
  document.querySelector("#fuse-edit-error").textContent = "";
  const locationKey = record?.location_id || form.elements.location.value;
  await loadFuseFiOptions(locationKey, record?.fi_id || "");
  fuseEditDialog.showModal();
  (record ? form.elements.label : form.elements.location).focus();
}

function resultMarkup(item) {
  const labels = { device: "GERÄT", channel: "KANAL", group: "GRUPPENADRESSE", terminal: "KLEMME", load: "VERBRAUCHER", location: "ORT", fuse: "SICHERUNG", fi: "FI", homeassistant: "HOME ASSISTANT" };
  // A Home Assistant hit opens the channel or device it belongs to.
  if (item.type === "homeassistant") item = { ...item, type: item.target_type, id: item.target_id, label: labels.homeassistant, typeClass: "homeassistant" };
  const typeClass = item.typeClass || item.type;
  const terminalParts = item.type === "terminal" ? terminalLabelParts(item.title) : null;
  const title = item.type === "terminal" ? item.wire_color ? terminalParts.number : formatTerminalLabel(item.title) : item.title;
  const locationAttribute = item.type === "location" ? ` data-location="${esc(item.title)}"` : "";
  // Only a Klemme can be unused (greyed out); other hits carry no such flag.
  const resultTitle = `<span class="${item.type === "terminal" ? terminalUsageClass(item.used) : ""}">${esc(title)}</span>${item.type === "terminal" && item.wire_color ? `<span class="result-terminal-color">${terminalWireMarkup(item.wire_color, terminalParts.position)}</span>` : ""}`;
  return `<button class="result-item" type="button" data-type="${esc(item.type)}" data-id="${esc(item.id)}"${locationAttribute}${item.type ? "" : " disabled"}>
    <span class="result-type result-type-${typeClass}">${item.label || labels[item.type] || item.type}</span>
    <span class="result-copy"><strong>${resultTitle}</strong><small>${esc(item.subtitle || "")}</small></span>
    <span class="result-chevron" aria-hidden="true">›</span>
  </button>`;
}

function renderSearchResults(items) {
  const filtersNode = document.querySelector("#search-filters");
  const available = SEARCH_CATEGORIES.map((category) => ({
    ...category,
    count: items.filter((item) => category.types.includes(item.type)).length,
  })).filter((category) => category.count > 0);
  if (!available.some((category) => category.id === selectedSearchCategory)) selectedSearchCategory = "";
  filtersNode.hidden = available.length === 0;
  filtersNode.innerHTML = available.map((category) => `<button class="search-filter${selectedSearchCategory === category.id ? " selected" : ""}" type="button" data-search-filter="${category.id}" aria-pressed="${selectedSearchCategory === category.id}">${category.label}<span>${category.count}</span></button>`).join("");

  const shown = selectedSearchCategory
    ? items.filter((item) => SEARCH_CATEGORIES.find((category) => category.id === selectedSearchCategory)?.types.includes(item.type))
    : items;
  document.querySelector("#result-count").textContent = String(shown.length);
  resultsNode.innerHTML = shown.length
    ? shown.map(resultMarkup).join("")
    : '<div class="empty-results">Keine passenden Einträge</div>';
}

async function search() {
  const query = buildSearchQuery();
  if (!query) {
    clearSearchHistoryEntry();
    currentSearchItems = [];
    selectedSearchCategory = "";
    document.querySelector("#search-filters").hidden = true;
    document.querySelector("#search-filters").innerHTML = "";
    resultsNode.innerHTML = '<div class="empty-results">Suchbegriff eingeben</div>';
    document.querySelector("#result-count").textContent = "0";
    searchViewNode.hidden = true;
    restoreCurrentPage();
    return;
  }
  showSearchPage();
  resultsNode.innerHTML = '<div class="empty-results">Suche …</div>';
  try {
    const items = await request(`api/search?q=${encodeURIComponent(query)}`);
    if (buildSearchQuery() !== query) return;
    currentSearchItems = items;
    renderSearchResults(items);
  } catch (error) {
    if (buildSearchQuery() !== query) return;
    currentSearchItems = [];
    document.querySelector("#search-filters").hidden = true;
    resultsNode.innerHTML = `<div class="empty-results error-copy">${esc(error.message)}</div>`;
  }
}

function showSearchPage() {
  searchViewNode.hidden = false;
  welcomeNode.hidden = true;
  settingsNode.hidden = true;
  detailNode.hidden = true;
  overviewNode.hidden = true;
  graphNode.hidden = true;
  document.querySelector("#crumb-current").textContent = "SUCHERGEBNISSE";
}

function updateSearchHistory(push = false) {
  const state = history.state && typeof history.state === "object" ? history.state : {};
  const currentView = state.schaltschrank;
  const savedState = { ...state, scrollY: window.scrollY };
  const searchState = {
    view: "search",
    query: searchInput.value.trim(),
    reviewTerm: selectedReviewSearchTerm,
    category: selectedSearchCategory,
    scrollY: window.scrollY,
    returnView: currentView?.view === "search" ? currentView.returnView : currentPageState,
  };
  if (push && currentView?.view !== "search") {
    history.replaceState(savedState, "");
    history.pushState({ ...savedState, schaltschrank: searchState }, "");
  } else history.replaceState({ ...savedState, schaltschrank: searchState }, "");
}

function clearSearchHistoryEntry() {
  const state = history.state && typeof history.state === "object" ? history.state : {};
  const currentView = state.schaltschrank;
  if (currentView?.view === "search") {
    history.replaceState({ ...state, schaltschrank: currentView.returnView || currentPageState }, "");
  }
}

async function restoreSearchPage(state) {
  currentPageState = state.returnView || { view: "home" };
  selectedSearchCategory = state.category || "";
  searchInput.value = state.query || "";
  selectedReviewSearchTerm = state.reviewTerm || "";
  renderReviewSearchTerm();
  showSearchPage();
  await search();
  requestAnimationFrame(() => requestAnimationFrame(() => window.scrollTo(0, Number(state.scrollY) || 0)));
}

async function openRecord(type, id, trackHistory = true) {
  if (type === "location") {
    openOverview("locations", trackHistory);
    return;
  }
  try {
    const data = await request(`api/record?type=${encodeURIComponent(type)}&id=${encodeURIComponent(id)}`);
    const recordState = { view: "record", type, id: String(id) };
    if (trackHistory) pushViewHistory(recordState);
    currentPageState = recordState;
    currentDetail = data;
    searchViewNode.hidden = true;
    welcomeNode.hidden = true;
    settingsNode.hidden = true;
    graphNode.hidden = true;
    overviewNode.hidden = true;
    detailNode.hidden = false;
    renderDetail(data);
  } catch (error) {
    notify(error.message, true);
  }
}

// "+" button of the edges: opens the add dialog after a Klemme (terminalId) or as a branch below a node (nodeId).
function pathAddButton({ terminalId = "", nodeId = "", nodeType = "", label = "", location = "", branch = false }, title) {
  return `<button class="vertical-edge add-edge" type="button" data-path-add data-terminal-id="${esc(terminalId)}" data-node-id="${esc(nodeId)}" data-node-type="${esc(nodeType)}" data-label="${esc(label)}" data-location="${esc(location)}" data-branch="${branch ? "1" : ""}" title="${esc(title)}" aria-label="${esc(title)}">+</button>`;
}

// ---- Tausch-, Trenn- und Abzweig-Buttons zwischen zwei Knoten gibt es nur im Überprüfungsmodus ----
// "⊕": a new or existing Klemme goes into the wire above the node (or in front of it, at the top of a path).
function pathInsertButton(nodeId, nodeType, title = "Klemme in diese Verbindung einfügen") {
  return `<button class="vertical-edge insert-edge" type="button" data-path-insert="${esc(nodeId)}" data-node-type="${esc(nodeType)}" title="${esc(title)}" aria-label="${esc(title)}">⊕</button>`;
}

// The plain arrow between two stations of a path; outside review mode only a small gap (no arrows to read past).
function pathArrow(symbol = "↕", className = "vertical-edge") {
  return reviewModeEnabled ? `<span class="${className}">${symbol}</span>` : '<span class="path-gap" aria-hidden="true"></span>';
}

// `insertNodeId`: the node below the wire; "⊕" puts a new or existing Klemme into that wire.
function pathEdge({ swapNodeId = null, swapChildId = null, dropNodeId = null, branchNodeId = null, branchType = null, insertNodeId = null, insertType = null } = {}) {
  if (!reviewModeEnabled || !(swapNodeId || dropNodeId || branchNodeId || insertNodeId)) return pathArrow();
  const insert = insertNodeId ? pathInsertButton(insertNodeId, insertType) : "";
  const swap = swapNodeId
    ? `<button class="vertical-edge swap-edge" type="button" data-swap-node="${esc(swapNodeId)}" data-swap-child="${esc(swapChildId ?? "")}" title="Reihenfolge tauschen" aria-label="Reihenfolge tauschen">⇅</button>`
    : "";
  const drop = dropNodeId
    ? `<button class="vertical-edge drop-edge" type="button" data-drop-node="${esc(dropNodeId)}" title="Verbindung trennen" aria-label="Verbindung trennen">✕</button>`
    : "";
  const branch = branchNodeId ? pathAddButton({ nodeId: branchNodeId, nodeType: branchType, branch: true }, "Weiteren Abzweig hinzufügen") : "";
  return `<div class="edge-actions">${swap}${drop}${insert}${branch}</div>`;
}

// Joins path markup pieces with edges; an edge offers swap/drop/branch buttons when its lower piece has the matching node.
// `leadingEdge` also puts an edge above the first piece (the top of a branch column).
function joinPathParts(parts, leadingEdge = false) {
  return parts.map((part, index) => `${(index || leadingEdge) && !part.noEdge ? pathEdge(part) : ""}${part.markup}`).join("");
}

// Sends a reorder action and reloads the record so every path follows the new order.
async function postReorder(payload) {
  await postJson("api/connections/reorder", payload);
  await refreshRecord();
}

// Conductor select on the Klemme page: connects the Klemme to N, an FI's neutral or PE right away; the empty
// choice takes a connected Klemme off its conductor again.
detailContent.addEventListener("change", async (event) => {
  const select = event.target.closest("[data-conductor-select]");
  if (!select || (!select.value && !select.dataset.current)) return;
  if (!select.value && !confirm("Die Klemme vom Leiter trennen? Sie erscheint dann in der Prüfliste.")) {
    select.value = select.dataset.current;
    return;
  }
  try {
    const data = await postJson("api/terminals/conductor", { terminal_id: select.dataset.terminalId, conductor: select.value });
    notify(data.wire_phase ? `Klemme an ${data.wire_phase} angeschlossen.` : "Klemme vom Leiter getrennt.");
    await refreshRecord();
  } catch (error) {
    notify(error.message, true);
  }
});

// FI page: the select places another Sicherung of the location below this FI.
detailContent.addEventListener("change", async (event) => {
  const select = event.target.closest("[data-fi-add-fuse]");
  if (!select?.value) return;
  try {
    await postJson("api/fuses/fi", { fuse_id: select.value, fi_id: select.dataset.fiAddFuse });
    notify("Sicherung dem FI zugeordnet.");
    await refreshRecord();
  } catch (error) {
    notify(error.message, true);
  }
});

// ✕ on a Verbraucher card: this Klemme (or Kanal) no longer feeds it.
detailContent.addEventListener("click", async (event) => {
  const cut = event.target.closest("[data-disconnect-load]");
  if (!cut) return;
  event.stopImmediatePropagation();
  const data = await confirmedPost(cut.dataset.confirm || "Diesen Verbraucher von der Klemme trennen?", "api/loads/disconnect", { node_id: Number(cut.dataset.disconnectLoad) });
  if (!data) return;
  notify(data.removed ? "Verbraucher getrennt und entfernt (keine Klemme speist ihn mehr)." : "Verbraucher getrennt.");
  loadStats();
  await refreshRecord();
});

// Review-mode actions inside a record page: edit a Verbraucher, take a Sicherung off its FI, add a Klemme to a Sicherung.
detailContent.addEventListener("click", async (event) => {
  const editLoad = event.target.closest("[data-edit-load]");
  const cutFuse = event.target.closest("[data-fi-cut-fuse]");
  const fuseAdd = event.target.closest("[data-fuse-add-terminal]");
  const feed = event.target.closest("[data-feed-load]");
  if (!editLoad && !cutFuse && !fuseAdd && !feed) return;
  event.stopImmediatePropagation();
  if (feed) {
    const load = currentDetail.loads.find((item) => String(item.id) === feed.dataset.feedLoad);
    openPathAddDialog({ feedLoadId: feed.dataset.feedLoad, feedRole: feed.dataset.feedRole, label: load?.name || "", location: load?.location || "",
      replaceNodeId: feed.dataset.replaceNode || "" });
  } else if (editLoad) {
    openLoadEditor(currentDetail.loads.find((load) => String(load.id) === editLoad.dataset.editLoad));
  } else if (fuseAdd) {
    openPathAddDialog({ fuseId: currentDetail.record.id, nodeType: "fuse", label: currentDetail.record.label, location: currentDetail.record.location });
  } else if (await confirmedPost(`Sicherung ${cutFuse.dataset.label} von diesem FI trennen?`, "api/fuses/fi", { fuse_id: cutFuse.dataset.fiCutFuse, fi_id: null })) {
    notify("Sicherung vom FI getrennt.");
    await refreshRecord();
  }
});

detailContent.addEventListener("click", async (event) => {
  const button = event.target.closest("[data-swap-node], [data-drop-node]");
  if (!button) return;
  event.stopImmediatePropagation();
  if (button.dataset.dropNode && !confirm("Diese Verbindung trennen?")) return;
  try {
    const optional = (value) => (value ? Number(value) : null);
    await postReorder(button.dataset.dropNode
      ? { action: "drop", node_id: Number(button.dataset.dropNode) }
      : { action: "swap", node_id: Number(button.dataset.swapNode), child_id: optional(button.dataset.swapChild) });
  } catch (error) {
    notify(error.message, true);
  }
});

function pushViewHistory(viewState) {
  const state = history.state && typeof history.state === "object" ? history.state : {};
  const currentState = state.schaltschrank;
  if (currentState?.view === viewState.view
    && currentState.type === viewState.type
    && currentState.id === viewState.id
    && currentState.category === viewState.category
    && currentState.location === viewState.location) return;
  history.replaceState({ ...state, scrollY: window.scrollY }, "");
  history.pushState({ ...state, schaltschrank: viewState, scrollY: 0 }, "", viewUrl(viewState));
}

// ---- Address of a page in the URL hash, so every page can be linked, bookmarked and reloaded:
// #terminal/14, #fuse/3, #fi/1, #device/1.1.5, #channel/1.1.5:3, #group/1%2F2%2F3,
// #overview/terminals[/<Ort>], #graph, #settings; the start page has none.
function viewHash(state) {
  const part = encodeURIComponent;
  if (state?.view === "record") return `#${part(state.type)}/${part(state.id)}`;
  if (state?.view === "overview") return `#overview/${part(state.category)}${state.location ? `/${part(state.location)}` : ""}`;
  if (state?.view === "graph" || state?.view === "settings") return `#${state.view}`;
  return "";
}

function viewUrl(state) {
  return `${location.pathname}${location.search}${viewHash(state)}`;
}

// View state of an address; anything unknown or malformed is the start page.
function viewFromHash(hash) {
  try {
    const [first, ...rest] = hash.replace(/^#/, "").split("/").map(decodeURIComponent);
    if (first === "graph" || first === "settings") return { view: first };
    if (first === "overview" && rest[0]) return { view: "overview", category: rest[0], ...(rest[1] ? { location: rest[1] } : {}) };
    if (first && rest.length) return { view: "record", type: first, id: rest.join("/") };
  } catch { /* malformed escape: start page */ }
  return { view: "home" };
}

// Opens the page of a Home Assistant entity (its Klemmen path where there is one); one without a clear place shows the
// Home Assistant overview with a note. Address: `#ha/<entity_id>`.
async function openHaEntity(entityId, trackHistory = true) {
  try {
    const target = await request(`api/homeassistant/target?entity_id=${encodeURIComponent(entityId)}`);
    if (target.target_type) {
      await openRecord(target.target_type, target.target_id, trackHistory);
      return;
    }
    notify(`${target.title}: im Schaltschrank nicht eindeutig zugeordnet.`, true);
  } catch (error) {
    notify(error.message, true);
  }
  openOverview("homeassistant", trackHistory);
}

// Hand-over from the Schaltschrank symbol in Home Assistant's entity dialog: the module there stores the entity
// in localStorage (same origin as this page behind ingress) and opens this panel. A fresh entry is taken once.
const HA_HANDOFF_KEY = "schaltschrank-open-entity";
function takeHaHandoff() {
  let entry = null;
  try {
    entry = JSON.parse(localStorage.getItem(HA_HANDOFF_KEY) || "null");
    localStorage.removeItem(HA_HANDOFF_KEY);
  } catch { return false; }
  if (!entry?.entity_id || Date.now() - Number(entry.at) > 60000) return false;
  openHaEntity(entry.entity_id);
  return true;
}
window.addEventListener("storage", (event) => {
  if (event.key === HA_HANDOFF_KEY && event.newValue) takeHaHandoff();
});

// Shows a view state without a new history entry.
function openView(state) {
  if (state.view === "settings") showSettings(false);
  else if (state.view === "record" && state.type === "ha") openHaEntity(state.id, false);
  else if (state.view === "record") openRecord(state.type, state.id, false);
  else if (state.view === "graph") openGraph(false);
  else if (state.view === "overview") {
    if (state.category === "terminals") selectedTerminalLocation = state.location || "";
    openOverview(state.category, false);
  } else showHome(false);
}

// Opens the page of the address (a pasted link or an address typed in the address bar); the history entry
// remembers it, so back/forward go through popstate afterwards.
function openFromAddress() {
  const view = viewFromHash(location.hash);
  history.replaceState({ ...(history.state || {}), schaltschrank: view }, "");
  openView(view);
}

function restoreHistoryScroll(scrollY) {
  requestAnimationFrame(() => requestAnimationFrame(() => window.scrollTo(0, Number(scrollY) || 0)));
}

function showHome(trackHistory = true) {
  if (trackHistory) pushViewHistory({ view: "home" });
  currentPageState = { view: "home" };
  currentDetail = null;
  searchInput.value = "";
  clearReviewSearchTerm();
  currentSearchItems = [];
  searchViewNode.hidden = true;
  detailNode.hidden = true;
  settingsNode.hidden = true;
  graphNode.hidden = true;
  overviewNode.hidden = true;
  welcomeNode.hidden = false;
  document.querySelector("#crumb-current").textContent = "STARTSEITE";
  document.querySelector("#settings-toggle").setAttribute("aria-label", "Einstellungen öffnen");
  document.querySelector("#settings-toggle").title = "Einstellungen";
}

function showSettings(trackHistory = true) {
  if (trackHistory) pushViewHistory({ view: "settings" });
  currentPageState = { view: "settings" };
  currentDetail = null;
  searchInput.value = "";
  clearReviewSearchTerm();
  currentSearchItems = [];
  searchViewNode.hidden = true;
  detailNode.hidden = true;
  welcomeNode.hidden = true;
  graphNode.hidden = true;
  overviewNode.hidden = true;
  settingsNode.hidden = false;
  document.querySelector("#crumb-current").textContent = "EINSTELLUNGEN";
  document.querySelector("#settings-toggle").setAttribute("aria-label", "Einstellungen schließen");
  document.querySelector("#settings-toggle").title = "Zur Startseite";
}

function restoreCurrentPage() {
  openView(currentPageState || { view: "home" });
}

function renderHomeOverviews(stats) {
  document.querySelector("#home-overview-buttons").innerHTML = OVERVIEW_CATEGORIES.filter((category) => !category.optional || stats[category.countKey] !== undefined).map((category) => `<button class="overview-card" type="button" data-open-overview="${category.id}"><strong>${category.label}</strong><span class="overview-card-count">${(stats[category.countKey] || 0).toLocaleString("de-DE")}</span><small>Alle ${category.label.toLowerCase()}</small></button>`).join("");
}

function groupTerminalOverviewItems(items) {
  const groups = new Map();
  const fusedGroupsByTerminal = new Map();
  items.forEach((item) => {
    if (!item.terminal_number || item.fuse_id == null) return;
    const channels = item.device_channels || [];
    const channel = channels.length === 1 ? channels[0] : null;
    const terminalKey = JSON.stringify([item.subtitle, item.terminal_number]);
    const groupKey = !channels.length
      ? `unlinked:${JSON.stringify([terminalKey, item.fuse_id])}`
      : channel?.physical_channel
        ? `group:${JSON.stringify([item.subtitle, item.terminal_number, item.fuse_id, channel.device_id, channel.physical_channel])}`
        : "";
    if (!groupKey) return;
    if (!fusedGroupsByTerminal.has(terminalKey)) fusedGroupsByTerminal.set(terminalKey, new Map());
    const groupsByFuse = fusedGroupsByTerminal.get(terminalKey);
    if (!groupsByFuse.has(String(item.fuse_id))) groupsByFuse.set(String(item.fuse_id), new Set());
    groupsByFuse.get(String(item.fuse_id)).add(groupKey);
  });
  items.forEach((item, index) => {
    const channels = item.device_channels || [];
    const channel = channels.length === 1 ? channels[0] : null;
    const terminalKey = JSON.stringify([item.subtitle, item.terminal_number]);
    const groupsByFuse = fusedGroupsByTerminal.get(terminalKey);
    const unlinkedGroupKey = `unlinked:${JSON.stringify([terminalKey, item.fuse_id ?? null])}`;
    const groupKey = channel && channel.physical_channel && item.terminal_number
      ? `group:${JSON.stringify([item.subtitle, item.terminal_number, item.fuse_id ?? null, channel.device_id, channel.physical_channel])}`
      : !channels.length && item.terminal_number
        ? (() => {
          if (item.fuse_id != null) {
            const fuseGroups = groupsByFuse?.get(String(item.fuse_id));
            const linkedGroups = [...(fuseGroups || [])].filter((key) => key.startsWith("group:"));
            return linkedGroups.length === 1 ? linkedGroups[0] : unlinkedGroupKey;
          }
          const possibleGroups = [...(groupsByFuse || new Map()).values()]
            .map((fuseGroups) => {
              const linkedGroups = [...fuseGroups].filter((key) => key.startsWith("group:"));
              return linkedGroups.length === 1 ? linkedGroups[0] : linkedGroups.length === 0 && fuseGroups.size === 1 ? [...fuseGroups][0] : null;
            })
            .filter(Boolean);
          const uniqueGroups = [...new Set(possibleGroups)];
          return uniqueGroups.length === 1 ? uniqueGroups[0] : unlinkedGroupKey;
        })()
        : `single:${index}`;
    let group = groups.get(groupKey);
    if (!group) {
      group = { firstIndex: index, items: [] };
      groups.set(groupKey, group);
    }
    group.items.push(item);
  });
  return [...groups.values()].sort((left, right) => left.firstIndex - right.firstIndex).map((group) => group.items);
}

function terminalDeviceLinkMarkup(channel) {
  const hasChannel = channel.physical_channel && channel.object_number !== null && channel.object_number !== undefined;
  const type = hasChannel ? "channel" : "device";
  const id = hasChannel ? `${channel.device_id}:${channel.object_number}` : channel.device_id;
  const label = channel.physical_channel ? `KANAL ${channel.physical_channel}` : "GERÄT";
  return `<button class="overview-device-label" type="button" data-type="${type}" data-id="${esc(id)}"><strong>${esc(channel.device_label || channel.device_id)}</strong><span>${esc(label)}</span></button>`;
}

function terminalFuseMarkup(item) {
  const label = item.fuse_label || "–";
  const terminalLabel = formatTerminalLabel(item.terminal_label || String(item.title).replace(/^Klemme\s+/, ""), item.position);
  const title = `Sicherung für ${terminalLabel}`;
  const content = `<span class="sr-only">Sicherung </span><strong>${esc(label)}</strong><svg class="overview-fuse-icon" viewBox="0 0 24 12" aria-hidden="true"><path d="M4 2.5h16a2 2 0 0 1 2 2v3a2 2 0 0 1-2 2H4a2 2 0 0 1-2-2v-3a2 2 0 0 1 2-2Z"/><path d="M7 3v6m10-6v6M9 6h6"/></svg>`;
  return item.fuse_id
    ? `<button class="overview-item-fuse-inline overview-item-fuse-button" type="button" data-type="fuse" data-id="${esc(item.fuse_id)}" title="${esc(title)}">${content}</button>`
    : `<span class="overview-item-fuse-inline" title="${esc(title)}">${content}</span>`;
}

function fuseMetadataMarkup(item) {
  const phaseList = Array.isArray(item.phases) ? item.phases : String(item.phases || "").split(/\s*,\s*/).filter(Boolean);
  const phases = phaseList.length ? `<span class="overview-fuse-phases">${esc(phaseList.join(", "))}</span>` : "";
  const normalizedRating = String(item.rating || "").replace(/\s/g, "");
  const ratingClass = normalizedRating === "10A" ? "is-10a" : normalizedRating === "16A" ? "is-16a" : "";
  const rating = item.rating ? `<span class="overview-fuse-rating ${ratingClass}">${esc(item.rating)}</span>` : "";
  return `${phases}${rating}`;
}

// Header badge with the FI of a Sicherung (links to the FI page).
// Header label of a Sicherung (opens it); "Nicht zugeordnet" without one.
function fuseHeaderBadge(fuseId, label = "", rating = "") {
  return fuseId
    ? `<button class="fuse-header-badge" type="button" data-type="fuse" data-id="${esc(fuseId)}"><span>SICHERUNG</span><strong>${esc(label)}</strong>${rating ? `<small>${esc(rating)}</small>` : ""}</button>`
    : '<span class="fuse-header-badge is-unassigned"><span>SICHERUNG</span><strong>Nicht zugeordnet</strong></span>';
}

function fuseFiBadge(record) {
  if (!record.fi_id && !record.fi_number && !record.fi_label) {
    return '<span class="fuse-header-badge is-unassigned"><span>FI</span><strong>Nicht zugeordnet</strong></span>';
  }
  const title = record.fi_number ? `FI ${record.fi_number}` : record.fi_label || "FI";
  const detail = record.fi_number && record.fi_label ? `<small>${esc(record.fi_label)}</small>` : "";
  const content = `<span>FI</span><strong>${esc(title)}</strong>${detail}`;
  return record.fi_id
    ? `<button class="fuse-header-badge" type="button" data-type="fi" data-id="${esc(record.fi_id)}">${content}</button>`
    : `<span class="fuse-header-badge">${content}</span>`;
}

function fuseOverviewItemMarkup(item, sourceIndex = null) {
  const sourceAttribute = sourceIndex === null ? "" : ` data-fuse-overview-source="${sourceIndex}" style="grid-row:${sourceIndex + 1}"`;
  return `<button class="overview-item overview-item-fuse" type="button" data-type="fuse" data-id="${esc(item.id)}"${sourceAttribute}><span class="overview-fuse-copy"><span class="overview-fuse-heading"><strong>${esc(item.title)}</strong>${fuseMetadataMarkup(item)}</span><small>${esc(item.location || "")}</small></span>${sourceIndex === null ? '<span class="overview-item-arrow" aria-hidden="true">›</span>' : ""}</button>`;
}

function fuseOverviewFi(item) {
  const segments = String(item.subtitle || "").split(" · ");
  const fiIndex = segments.findIndex((segment) => /^FI\s+/.test(segment));
  if (fiIndex < 0) return null;
  const number = segments[fiIndex].replace(/^FI\s+/, "").trim();
  const nameSegments = segments.slice(fiIndex + 1);
  const phases = String(item.phases || "").trim();
  const phaseIndex = phases ? nameSegments.lastIndexOf(phases) : -1;
  if (phaseIndex >= 0) nameSegments.splice(phaseIndex, 1);
  return { id: item.fi_id, number, label: nameSegments.join(" · ").trim() };
}

function fuseOverviewGroupMarkup(fi, items) {
  const sourceMarkup = items.map((item, index) => fuseOverviewItemMarkup(item, index)).join("");
  const fiTitle = fi.number ? `FI ${fi.number}` : "FI";
  const targetTag = fi.id ? "button" : "div";
  const targetAttributes = fi.id ? ` type="button" data-type="fi" data-id="${esc(fi.id)}"` : "";
  return `<div class="overview-fuse-graph"><div class="overview-fuse-canvas"><svg class="overview-fuse-wires" aria-hidden="true"></svg><${targetTag} class="overview-fuse-target${fi.id ? " overview-fuse-target-button" : ""}" data-fuse-overview-target${targetAttributes} style="grid-row:1 / span ${items.length}"><span>FI</span><strong>${esc(fiTitle)}</strong>${fi.label ? `<small>${esc(fi.label)}</small>` : ""}</${targetTag}><div class="overview-fuse-sources">${sourceMarkup}</div></div></div>`;
}

function drawFuseOverviewWires() {
  const zoom = Number.parseFloat(getComputedStyle(document.body).zoom) || 1;
  overviewItemsNode.querySelectorAll(".overview-fuse-canvas").forEach((canvas) => {
    const svg = canvas.querySelector(".overview-fuse-wires");
    const bounds = canvas.getBoundingClientRect();
    const target = canvas.querySelector("[data-fuse-overview-target]");
    if (!bounds.width || !bounds.height || !target) return;
    const width = bounds.width / zoom;
    const height = bounds.height / zoom;
    svg.setAttribute("viewBox", `0 0 ${width} ${height}`);
    svg.removeAttribute("width");
    svg.removeAttribute("height");
    svg.replaceChildren();
    const targetRect = target.getBoundingClientRect();
    const targetX = (targetRect.right - bounds.left) / zoom;
    const targetY = (targetRect.top - bounds.top + targetRect.height / 2) / zoom;
    const namespace = "http://www.w3.org/2000/svg";
    canvas.querySelectorAll("[data-fuse-overview-source]").forEach((source) => {
      const sourceRect = source.getBoundingClientRect();
      const x1 = (sourceRect.left - bounds.left) / zoom;
      const y1 = (sourceRect.top - bounds.top + sourceRect.height / 2) / zoom;
      const bend = x1 - Math.max(18, (x1 - targetX) * .55);
      const path = document.createElementNS(namespace, "path");
      path.setAttribute("class", "overview-fuse-wire-path");
      path.setAttribute("d", `M ${x1} ${y1} C ${bend} ${y1}, ${bend} ${targetY}, ${targetX} ${targetY}`);
      svg.append(path);
    });
  });
}

function updateFuseOverviewTargets() {
  const zoom = Number.parseFloat(getComputedStyle(document.body).zoom) || 1;
  const viewportHeight = window.innerHeight;
  overviewItemsNode.querySelectorAll(".overview-fuse-canvas").forEach((canvas) => {
    const target = canvas.querySelector("[data-fuse-overview-target]");
    const sources = [...canvas.querySelectorAll("[data-fuse-overview-source]")];
    if (!target || !sources.length) return;
    const targetRect = target.getBoundingClientRect();
    const currentOffset = Number(target.dataset.overviewOffset || 0) * zoom;
    const layoutTop = targetRect.top - currentOffset;
    const targetHeight = targetRect.height / zoom;
    const originalCenter = layoutTop / zoom + targetHeight / 2;
    const firstRect = sources[0].getBoundingClientRect();
    const lastRect = sources.at(-1).getBoundingClientRect();
    const firstFuseCenter = (firstRect.top + firstRect.height / 2) / zoom;
    const lastFuseCenter = (lastRect.top + lastRect.height / 2) / zoom;
    const viewportTop = targetHeight / 2 + 12;
    const viewportBottom = viewportHeight - targetHeight / 2 - 12;
    const visibleCenter = Math.max(viewportTop, Math.min(viewportBottom, originalCenter));
    const limitedCenter = Math.max(firstFuseCenter, Math.min(lastFuseCenter, visibleCenter));
    const nextOffset = limitedCenter - targetHeight / 2 - layoutTop / zoom;
    target.style.transform = `translateY(${nextOffset}px)`;
    target.dataset.overviewOffset = String(nextOffset);
  });
  drawFuseOverviewWires();
}

let fuseOverviewLayoutFrame = 0;
function scheduleFuseOverviewLayout() {
  if (fuseOverviewLayoutFrame) return;
  fuseOverviewLayoutFrame = requestAnimationFrame(() => {
    fuseOverviewLayoutFrame = 0;
    updateFuseOverviewTargets();
  });
}

// ---- Home Assistant (read-only): the KNX entities with their channel or device, Klemme, Sicherung and FI.
const HA_PLATFORMS = { light: "Licht", switch: "Schalter", cover: "Rollladen", climate: "Heizung", sensor: "Sensor", binary_sensor: "Binärsensor" };
const HA_STATES = { on: "an", off: "aus", open: "offen", closed: "geschlossen", opening: "öffnet", closing: "schließt",
  heat: "heizen", cool: "kühlen", auto: "auto", unavailable: "nicht verfügbar", unknown: "unbekannt" };
const HA_GROUPS = [
  { status: "channel", title: "Einem Kanal zugeordnet" },
  { status: "device", title: "Einem Gerät zugeordnet", note: "Die Adressen liegen auf mehreren Kanälen dieses Geräts." },
  { status: "ambiguous", title: "Prüfliste: mehrdeutig", note: "Mehrere Geräte kommen in Frage; nichts wird geraten." },
  { status: "unknown", title: "Prüfliste: unbekannt", note: "Die Gruppenadressen fehlen in der Datenbank." },
];

// The live state as short text ("an", "21,5 °C"); empty outside the add-on.
function haStateText(state) {
  if (!state) return "";
  const value = Number(state.state);
  return state.state !== "" && Number.isFinite(value)
    ? `${value.toLocaleString("de-DE")}${state.unit ? ` ${state.unit}` : ""}`
    : HA_STATES[state.state] || state.state;
}

// Where an entity sits in the cabinet: device, channel, Klemmen, Sicherung, FI, Verbraucher.
function haPlaceText(item) {
  const fi = (item.fis || []).map((fi) => fi.number ? `FI ${fi.number}` : fi.label);
  return [
    item.device_label && `${item.device_label}${item.product ? ` (${item.product})` : ""}`,
    item.channel_label,
    (item.terminals || []).length && `Klemme ${item.terminals.map((terminal) => terminal.label).join(", ")}`,
    ...(item.fuses || []).map((fuse) => fuse.label),
    ...fi,
    ...(item.loads || []).map((load) => load.name),
    item.note && !item.device_id ? item.note : "",
  ].filter(Boolean).join(" · ");
}

// One entity row; it opens the entity's page (its Klemme, Verbraucher, channel or device; chosen by the server)
// unless `link` is false.
function haEntityRow(item, link = true) {
  const target = item.target_type ? [item.target_type, item.target_id] : null;
  const state = haStateText(item.state);
  const copy = `<span class="overview-item-copy"><strong>${esc(item.name)}</strong><small>${esc([item.entity_id, HA_PLATFORMS[item.platform] || item.platform].filter(Boolean).join(" · "))}</small><small>${esc(haPlaceText(item))}</small></span><span class="overview-item-type">${esc(state || "HOME ASSISTANT")}</span>`;
  return link && target
    ? `<button class="overview-item ha-entity" type="button" data-type="${target[0]}" data-id="${esc(target[1])}">${copy}<span class="overview-item-arrow" aria-hidden="true">›</span></button>`
    : `<div class="overview-item ha-entity">${copy}</div>`;
}

// The entities grouped by how they are mapped (the review groups last).
function haEntityGroups(items) {
  return HA_GROUPS.map((group) => {
    const rows = items.filter((item) => item.status === group.status);
    return rows.length
      ? `<div class="ha-group"><div class="ha-group-heading"><h3>${group.title} <span>${rows.length}</span></h3>${group.note ? `<small>${group.note}</small>` : ""}</div>${rows.map((item) => haEntityRow(item)).join("")}</div>`
      : "";
  }).join("") || '<div class="overview-empty">Keine Einträge vorhanden.</div>';
}

// Section of a device or channel page with its Home Assistant entities (none: no section).
function haDetailSection(items, link = true) {
  if (!items?.length) return "";
  return `<section class="detail-section"><div class="section-heading"><div><span class="section-tag">HOME ASSISTANT</span><h3>${items.length} ${items.length === 1 ? "Entität" : "Entitäten"}</h3></div></div><div class="overview-items">${items.map((item) => haEntityRow(item, link)).join("")}</div></section>`;
}

function renderOverviewItems(category, items) {
  const definition = OVERVIEW_CATEGORIES.find((item) => item.id === category);
  if (category === "homeassistant") {
    document.querySelector("#overview-location-filters").hidden = true;
    overviewItemsNode.innerHTML = haEntityGroups(items);
    return;
  }
  if (category === "terminals") {
    const locationOf = (item) => item.subtitle || "Ohne Ort";
    const locations = [...new Set(items.map(locationOf))].sort((left, right) => left.localeCompare(right, undefined, { numeric: true, sensitivity: "base" }));
    if (!locations.includes(selectedTerminalLocation)) selectedTerminalLocation = "";
    const filterOptions = [
      { value: "", label: "Alle Orte", count: items.length },
      ...locations.map((location) => ({ value: location, label: location, count: items.filter((item) => locationOf(item) === location).length })),
    ];
    const filtersNode = document.querySelector("#overview-location-filters");
    filtersNode.hidden = locations.length === 0;
    filtersNode.innerHTML = filterOptions.map((filter) => `<button class="search-filter${selectedTerminalLocation === filter.value ? " selected" : ""}" type="button" data-location-filter="${esc(filter.value)}" aria-pressed="${selectedTerminalLocation === filter.value}">${esc(filter.label)}<span>${filter.count}</span></button>`).join("");
    const visibleItems = selectedTerminalLocation ? items.filter((item) => locationOf(item) === selectedTerminalLocation) : items;
    document.querySelector("#overview-count").textContent = `${visibleItems.length.toLocaleString("de-DE")} Einträge`;
    overviewItemsNode.innerHTML = groupTerminalOverviewItems(visibleItems).map((group) => {
      const first = group[0];
      const firstPosition = String(first.position || String(first.terminal_label || first.title || "").split("/")[1] || "").trim();
      const firstNumber = String(first.terminal_number || "");
      const firstDisplayNumber = firstPosition && firstNumber.endsWith(`/${firstPosition}`)
        ? firstNumber.slice(0, -(firstPosition.length + 1))
        : firstNumber;
      const descriptions = [...new Set(group.map((item) => item.element_description || ""))];
      const sharedDescription = descriptions.length === 1 ? descriptions[0] : "";
      const deviceChannels = [...new Map(group.flatMap((item) => item.device_channels || []).map((channel) => [JSON.stringify([channel.device_id, channel.physical_channel, channel.object_number]), channel])).values()];
      const sendGroupNames = [...new Set(deviceChannels.map((channel) => channel.send_group_name).filter(Boolean))];
      const sendGroupName = sendGroupNames.join(" · ");
      const terminalContext = sharedDescription && sendGroupName
        ? `<small class="overview-element-description">${esc(sharedDescription)}<br>${esc(sendGroupName)}</small>`
        : `<small class="overview-element-description">${esc(sharedDescription || sendGroupName)}</small>`;
      const deviceLabels = deviceChannels.map(terminalDeviceLinkMarkup).join("");
      // A Klemme wired on behind another one has that one's Sicherung (it comes with `fuse_id` from the tree).
      const fuseItems = [...new Map(group.filter((item) => item.fuse_id).map((item) => [String(item.fuse_id), item])).values()];
      const fuseMarkup = fuseItems.map((item) => terminalFuseMarkup(item)).join("") || terminalFuseMarkup(first);
      const terminalLabels = [...group].reverse().map((item) => {
        const label = formatTerminalLabel(item.terminal_label || String(item.title).replace(/^Klemme\s+/, ""), item.position);
        const position = String(item.position || String(item.terminal_label || item.title || "").split("/")[1] || "").trim();
        const description = item.element_description ? ` title="${esc(item.element_description)}"` : "";
        const content = item.wire_color
          ? terminalWireMarkup(item.wire_color, position)
          : `<strong>${esc(label)}</strong>`;
        const reviewControl = reviewControlMarkup("terminal", item.id, item.reviewed_at, "overview-terminal-review");
        return `<span class="overview-terminal-entry"><button class="overview-terminal-element ${terminalUsageClass(item.used)}" type="button" data-type="${esc(item.type)}" data-id="${esc(item.id)}"${description}>${content}</button>${reviewControl}</span>`;
      }).join("");
      return `<div class="overview-terminal-group"><div class="overview-terminal-group-head"><span class="overview-terminal-group-identity"><span class="overview-terminal-location">${esc(first.subtitle || "")}</span><span class="overview-terminal-klemme-line"><strong>${esc(firstDisplayNumber)}</strong><span class="overview-terminal-elements">${terminalLabels}</span>${deviceLabels}</span></span>${sharedDescription || sendGroupName ? terminalContext : ""}${fuseMarkup}</div></div>`;
    }).join("") || '<div class="overview-empty">Keine Einträge vorhanden.</div>';
    return;
  }
  document.querySelector("#overview-location-filters").hidden = true;
  document.querySelector("#overview-location-filters").innerHTML = "";
  if (category === "fuses") {
    const fusesByFi = new Map();
    const unlinkedFuses = [];
    const compareFuses = (left, right) => naturalCollator.compare(left.title, right.title)
      || naturalCollator.compare(left.location || "", right.location || "");
    for (const item of [...items].sort(compareFuses)) {
      const fi = fuseOverviewFi(item);
      if (!fi) {
        unlinkedFuses.push(item);
        continue;
      }
      const fiKey = JSON.stringify([item.location, fi.number, fi.label]);
      if (!fusesByFi.has(fiKey)) fusesByFi.set(fiKey, { ...fi, items: [] });
      fusesByFi.get(fiKey).items.push(item);
    }
    const overviewGroups = [
      ...[...fusesByFi.values()].map((group) => ({ fi: group, items: group.items })),
      ...unlinkedFuses.map((item) => ({ items: [item] })),
    ].sort((left, right) => compareFuses(left.items[0], right.items[0]));
    overviewItemsNode.innerHTML = items.length
      ? overviewGroups.map((group) => group.fi
        ? fuseOverviewGroupMarkup(group.fi, group.items)
        : fuseOverviewItemMarkup(group.items[0])).join("")
      : '<div class="overview-empty">Keine Einträge vorhanden.</div>';
    requestAnimationFrame(() => requestAnimationFrame(scheduleFuseOverviewLayout));
    return;
  }
  overviewItemsNode.innerHTML = items.length
    ? items.map((item) => {
      const subtitle = item.subtitle ? `<small>${esc(item.subtitle)}</small>` : "";
      const title = esc(item.title);
      const locationAttribute = item.type === "location" ? ` data-location="${esc(item.title)}"` : "";
      const row = `<button class="overview-item" type="button" data-type="${esc(item.type)}" data-id="${esc(item.id)}"${locationAttribute}><span class="overview-item-copy"><strong>${title}</strong>${subtitle}</span><span class="overview-item-type">${definition.typeLabel}</span><span class="overview-item-arrow" aria-hidden="true">›</span></button>`;
      // A location is renamed or deleted from its row (review mode).
      return item.type === "location" && reviewModeEnabled
        ? `<div class="overview-row-action">${row}<button class="tiny-action" type="button" data-edit-location="${esc(item.id)}">Bearbeiten</button></div>`
        : row;
    }).join("")
    : '<div class="overview-empty">Keine Einträge vorhanden.</div>';
}

async function openOverview(category, trackHistory = true) {
  const definition = OVERVIEW_CATEGORIES.find((item) => item.id === category);
  if (!definition) return;
  try {
    const items = await request(`api/overview?category=${encodeURIComponent(category)}`);
    currentOverviewItems = items;
    const overviewState = { view: "overview", category, ...(category === "terminals" ? { location: selectedTerminalLocation } : {}) };
    if (trackHistory) pushViewHistory(overviewState);
    currentPageState = overviewState;
    currentDetail = null;
    searchViewNode.hidden = true;
    welcomeNode.hidden = true;
    graphNode.hidden = true;
    settingsNode.hidden = true;
    detailNode.hidden = true;
    overviewNode.hidden = false;
    document.querySelector("#crumb-current").textContent = definition.label.toUpperCase();
    document.querySelector("#overview-title").textContent = definition.label;
    document.querySelector("#overview-count").textContent = `${items.length.toLocaleString("de-DE")} Einträge`;
    document.querySelector("#add-fuse").hidden = category !== "fuses";
    document.querySelector("#add-device").hidden = category !== "devices";
    document.querySelector("#add-terminal-overview").hidden = category !== "terminals";
    document.querySelector("#add-location").hidden = category !== "locations";
    document.querySelector("#add-load").hidden = category !== "loads";
    renderOverviewItems(category, items);
  } catch (error) {
    notify(error.message, true);
  }
}

// ---- Gesamtgraph: read-only overview. Conductors (L1-L3, N, PE) carry FI, Sicherungen and Klemmen;
// every path ends in its Verbraucher. Three modes: Sicherungen (paths), Verbraucher (each with its L, N and PE
// feed) and Leiter (the conductor tree).
const GRAPH_TYPE_NAMES = { phase: "Leiter", fi: "FI", fuse: "Sicherung", terminal: "Klemme", channel: "Gerät · Kanal", load: "Verbraucher" };

// Poles of an FI from the phases its Sicherungen use: one L (with N) is a 2-pole FI, more are a 4-pole one.
function graphFiPoles(phases) {
  return phases.length <= 1 ? "2-polig" : "4-polig";
}
const GRAPH_NO_LOCATION = "Ohne Ort";
const GRAPH_REVIEW_KINDS = { mismatch: "Widerspruch", neutral: "N offen", pe: "PE offen", load: "Verbraucher unvollständig", unfed: "ohne Verbraucher" };
// Filter, depth, folded nodes, mode and the Prüfliste toggle survive leaving and re-entering the page.
const graphState = { roots: [], review: [], loads: 0, ready: false, depth: 99, collapsed: new Set(), filter: "", location: "", mode: "fuses", flaggedOnly: false,
  // Conductor busbars drawn in the Sicherungen view (each can be switched off); Verbraucher with feeds by load id.
  rails: { L1: true, L2: true, L3: true, N: true, PE: true }, loadFeeds: null };
// Busbars from the left: L1, L2, L3, N, PE (along the devices the order is PE, N, L from the top: orthogonalGraphTap).
const RAIL_CODES = ["L1", "L2", "L3", "N", "PE"];
const RAIL_COLORS = { L1: "#7a4a1e", L2: "#1d2420", L3: "#6b7570", N: "#2a62a3", PE: "#5d8f17" };

// Folds every node with followers that sits on the last visible level (depth 99 = nothing folded);
// conductor start nodes do not count as a level.
function applyGraphDepth(depth) {
  graphState.depth = depth;
  graphState.collapsed = new Set();
  const walk = (node, level) => {
    if (node.children.length && level + 1 >= depth) graphState.collapsed.add(node.id);
    node.children.forEach((child) => walk(child, level + 1));
  };
  graphState.roots.forEach((root) => (root.type === "phase" ? root.children.forEach((child) => walk(child, 0)) : walk(root, 0)));
}

// Conductor role of a code: L1/L2/L3 -> L, N/N4 -> N, PE -> PE.
function conductorRole(code) {
  if (!code) return "";
  if (code === "PE") return "PE";
  return code.startsWith("N") ? "N" : "L";
}

// Role of a Klemme: its conductor, else its wire colour (green-yellow PE, blue N, any other L).
function terminalRole(terminal) {
  if (terminal.wire_phase) return conductorRole(terminal.wire_phase);
  if (terminal.wire_color === "grge") return "PE";
  return terminal.wire_color === "bl" ? "N" : "L";
}

// Klemme labels lose their prefix (the card says Klemme); manual channels lose their internal object id.
// A Kanal shows device and channel only: the ETS object number the backend appends to tell apart a channel and
// one of its ETS objects (" · Obj. 96") is internal.
function graphTitle(node) {
  return node.title.replace(/^Klemme /, "").replace(/ · Obj\. \S+$/, "");
}

// Keeps nodes that pass the test (a hit keeps its whole subtree) and the way to them; null when nothing passes.
function pruneGraph(node, test) {
  if (!test) return node;
  if (test(node)) return { ...node, matched: true, forceOpen: true };
  const kept = node.children.map((child) => pruneGraph(child, test)).filter(Boolean);
  return kept.length ? { ...node, children: kept, forceOpen: true } : null;
}

// Klemmen named in the Prüfliste.
function flaggedTerminalIds() {
  // A Verbraucher hanging directly on a Sicherung is listed with that Sicherung, not with a Klemme.
  return new Set(graphState.review.filter((item) => item.entity_type !== "fuse").map((item) => item.entity_id));
}

// Combined test of the filter words and the "nur Auffälligkeiten" toggle; null when neither is active.
function graphTest(words) {
  const flagged = graphState.flaggedOnly ? flaggedTerminalIds() : null;
  if (!words.length && !flagged) return null;
  return (node) => {
    const text = `${node.title} ${node.subtitle} ${node.location} ${node.code} ${node.neutral} ${node.wire_phase} ${GRAPH_TYPE_NAMES[node.type]}`.toLowerCase();
    const wordsOk = words.every((word) => text.includes(word));
    const flagOk = !flagged || (["terminal", "load"].includes(node.type) && flagged.has(node.entity_id));
    return wordsOk && flagOk;
  };
}

// `badge` is extra markup behind the title (e.g. the phase of a Sicherung).
function graphCardMarkup(node, open, badge = "") {
  const isTerminal = node.type === "terminal";
  const title = graphTitle(node);
  const wire = isTerminal && node.wire_color ? wireColorChip(node.wire_color, false) : "";
  // An FI starts its own neutral (N4 for FI 4); a conductor card shows its colour.
  const neutral = node.type === "fi" && node.neutral ? `<span class="graph-neutral">${esc(node.neutral)}</span>` : "";
  const conductor = node.type === "phase" ? ` graph-conductor graph-conductor-${esc(node.code.toLowerCase())}` : "";
  const target = node.entity_type && node.entity_id ? ` data-type="${esc(node.entity_type)}" data-id="${esc(node.entity_id)}"` : " disabled";
  // In the Sicherungen view a Verbraucher gets its N and PE Klemmen as cards in their own wires.
  if (node.type === "load" && !node.plain && graphState.loadFeeds?.has(node.load_id)) return graphLoadJoinMarkup(node, graphState.loadFeeds.get(node.load_id));
  // `fiOut`: an FI box whose phases leave it one by one; `fiFeed`: a Sicherung fed by such a phase line.
  // `attrs`: further data attributes (N/PE branch rows, see `graphJoinKlemmen`).
  const taps = `${graphTapsAttribute(node.taps)}${node.bus ? ` data-bus="${esc(node.bus)}"` : ""}${node.attrs || ""}`
    + `${node.fiOut ? ` data-fi-out="${esc(node.fiOut)}"` : ""}`
    + `${node.fiFeed ? ` data-fi="${esc(node.fiFeed.fi)}" data-fi-feed="${esc(JSON.stringify(node.fiFeed.phases))}"` : ""}`;
  const toggle = node.children.length
    ? `<button class="graph-toggle" type="button" data-graph-toggle="${node.id}" aria-expanded="${open}" title="${open ? "Einklappen" : `${node.children.length} Folgeknoten aufklappen`}">${open ? "−" : node.children.length}</button>`
    : "";
  // A Klemme card is headed by the room the Klemme is in (its type is plain from the card).
  // An FI is headed by its poles, from the phases of its Sicherungen (Leiter view: its followers).
  const fiHeading = node.type === "fi" ? `FI · ${graphFiPoles([...new Set(node.children.flatMap((child) => child.phases || []))])}` : "";
  const heading = isTerminal && node.location ? esc(node.location) : node.label || fiHeading || GRAPH_TYPE_NAMES[node.type];
  // A narrow Klemme card may cut its room: the full room as tooltip.
  const headingTitle = isTerminal && node.location ? ` title="${esc(node.location)}"` : "";
  return `<div class="graph-card graph-${node.type}${conductor}${node.extraClass ? ` ${node.extraClass}` : ""}${node.matched ? " is-match" : ""}${isTerminal && !node.used ? " is-unused" : ""}"${taps}><button class="graph-card-main" type="button"${target}><small${headingTitle}>${heading}</small><strong>${esc(title)}${neutral}${wire}${badge || node.badge || ""}</strong>${node.subtitle ? `<span>${esc(node.subtitle)}</span>` : ""}</button>${toggle}</div>`;
}

// Busbar tap of an N or PE Klemme (null for L): an FI's neutral (N4) taps the bar beside its FI box (`bus`).
function graphFeedTaps(role, code) {
  return role === "N" || role === "PE" ? [{ code: role, bus: role === "N" && /^N\d+$/.test(code) ? code : "", part: 0.5 }] : null;
}

// Taps from the conductor busbars to a card: [{ code: "L1", bus: "", part: 0.5 }]; `bus` names an FI's neutral bar
// to tap instead, `part` is the height on the card where the tap ends. Read back by `drawGraphRails`.
function graphTapsAttribute(taps) {
  return taps?.length ? ` data-taps="${esc(JSON.stringify(taps))}"` : "";
}

// Orthogonal path of a tap around the cards: from the bar along the nearest free row gap, down the column
// gap in front of the destination card, then into the Klemme. `lane` keeps N and PE apart. Straight when free;
// falls back to straight when no gap or the column gap is blocked. Returns the points of a polyline.
// N and PE run above the devices: along the free row gap just above the destination's row (PE outermost, i.e.
// highest, like on the busbars), then down the column gap in front of the Klemme (PE further left).
// Heights taken by the cards (3px around each) between `left` and `right`, overlapping ones merged; `skip`: cards
// a wire may touch (its own ends).
function graphBlockedSpans(obstacles, left, right, skip = []) {
  const merged = [];
  obstacles.filter((card) => !skip.includes(card) && card.right > left && card.left < right)
    .map((card) => [card.top - 3, card.bottom + 3]).sort((a, b) => a[0] - b[0]).forEach(([top, bottom]) => {
      const last = merged.at(-1);
      if (last && top <= last[1]) last[1] = Math.max(last[1], bottom);
      else merged.push([top, bottom]);
    });
  return merged;
}

function orthogonalGraphTap(x0, yT, xEnd, destination, obstacles, lane) {
  const xv = destination.left - 7 - lane * 6;
  const blocks = graphBlockedSpans(obstacles, Math.min(x0, xv), Math.max(x0, xv));
  const straight = [[x0, yT], [xEnd, yT]];
  if (!blocks.length) return straight;
  // Free row gaps between the card intervals.
  const gaps = blocks.map((block, index) => [block[1], blocks[index + 1]?.[0] ?? Infinity]).filter(([top, bottom]) => bottom - top >= 6);
  if (blocks[0][0] > 24) gaps.unshift([18, blocks[0][0]]);
  // A wire that is free stays straight (PE and N Klemmen stand above their row's devices, graphLiftStacks); else it
  // runs along the lowest gap above the Klemme.
  if (!blocks.some(([top, bottom]) => yT > top && yT < bottom)) return straight;
  const above = gaps.filter(([, bottom]) => bottom !== Infinity && bottom <= yT).sort((a, b) => b[1] - a[1])[0];
  const gap = above || gaps.sort((a, b) => Math.min(Math.abs(yT - a[0]), Math.abs(yT - a[1])) - Math.min(Math.abs(yT - b[0]), Math.abs(yT - b[1])))[0];
  if (!gap) return straight;
  // Close above the devices: N lowest, PE 4px above it.
  const y = above
    ? Math.round(Math.max(gap[0] + 2, gap[1] - 4 - lane * 4)) + 0.5
    : Math.round(Math.min(Math.max(yT, gap[0] + 3 + lane * 4), gap[1] === Infinity ? gap[0] + 3 + lane * 4 : gap[1] - 3)) + 0.5;
  // The column gap in front of the destination must be free between the corridor and the Klemme.
  const low = Math.min(y, yT);
  const high = Math.max(y, yT);
  if (obstacles.some((o) => o !== destination && o.left < xv && o.right > xv && o.bottom > low && o.top < high)) return straight;
  return [[x0, y], [xv, y], [xv, yT], [xEnd, yT]];
}

// SVG for one wire (line points); PE is drawn as a protective conductor: green with yellow dashes on top.
function graphWireMarkup(points, code, extraClass = "") {
  const pointList = points.map((point) => point.join(",")).join(" ");
  const base = `<polyline class="graph-wire-line ${extraClass}" points="${pointList}" fill="none" stroke="${graphInk(RAIL_COLORS[code])}"/>`;
  return code === "PE" ? `${base}<polyline class="graph-wire-line graph-pe-stripe ${extraClass}" points="${pointList}" fill="none" stroke="#e5cb2b"/>` : base;
}

// Lines below an FI box, from its left edge: the phase lines (L1, L2, L3), then its neutral bar; its Sicherungen
// start right of them (`.graph-fi-out` in styles.css keeps the same distance).
const FI_LINE_X = 12;
const FI_LINE_STEP = 9;
const FI_BUS_X = 44;

// Busbars L1, L2, L3, N, PE as vertical lines on the left with a horizontal tap to every element carrying
// `data-taps`. An FI box (`data-bus`) starts its own neutral (N4): a short bar beside the box that the N
// Klemmen behind it tap instead of the main N. Drawn after layout (positions are measured).
// Verbraucher fed by the same N/PE Klemmen (drawn once, `data-join-key`/`data-join-ref`) stand in one column, so
// the shared wires run straight down: behind a narrower Klemmen stack the gap to its Verbraucher grows (the cards keep
// their width); `--align-extra` lengthens the stack's L/N/PE wires by as much (styles.css).
const JOIN_STACKS = ".graph-klemmen-stack, .graph-join-feeds";

// Height of an N/PE wire in the middle of a row, where its CSS pieces lie: an N line is a 1.5px border from the
// middle down (its centre 0.75px lower), the PE stripe is centred. The SVG pieces meet them without a step.
function graphWireMid(rect, code) {
  return rect.top + rect.height / 2 + (code === "PE" ? 0 : 0.75);
}

// Extra gap behind a Klemmen stack set by `graphAlignSharedLoads` (CSS px).
function graphAlignExtra(element) {
  return parseFloat(element?.closest(JOIN_STACKS)?.style.getPropertyValue("--align-extra")) || 0;
}

function graphAlignSharedLoads(area, scale) {
  area.querySelectorAll("[data-load-align]").forEach((stack) => {
    stack.style.removeProperty("--align-extra");
    stack.style.marginRight = "";
    delete stack.dataset.loadAlign;
  });
  // Groups of stacks joined by a shared wire (union-find over the stack elements).
  const parent = new Map();
  const find = (node) => (parent.get(node) === node ? node : find(parent.get(node)));
  const join = (a, b) => {
    [a, b].forEach((node) => { if (!parent.has(node)) parent.set(node, node); });
    parent.set(find(a), find(b));
  };
  area.querySelectorAll("[data-join-ref]").forEach((row) => {
    // A Klemmen box at the L Klemme stands further left: it is not aligned with the rows it feeds.
    const sourceRow = area.querySelector(`[data-join-key="${CSS.escape(row.dataset.joinRef)}"]`);
    const source = sourceRow?.classList.contains("graph-join-head") ? null : sourceRow?.closest(JOIN_STACKS);
    const target = row.closest(JOIN_STACKS);
    if (source && target) join(source, target);
  });
  const groups = new Map();
  parent.forEach((_, node) => groups.set(find(node), [...(groups.get(find(node)) || []), node]));
  // Groups whose heights overlap share one column (their lanes run side by side, never behind a Verbraucher), and
  // so does every other Klemmen stack within that height (the lanes pass its Verbraucher too).
  const span = (stack) => { const rect = stack.getBoundingClientRect(); return [rect.top, rect.bottom]; };
  const merged = [];
  [...groups.values()].map((stacks) => {
    const spans = stacks.map(span);
    return { stacks, top: Math.min(...spans.map(([top]) => top)), bottom: Math.max(...spans.map(([, bottom]) => bottom)) };
  }).sort((a, b) => a.top - b.top).forEach((group) => {
    const last = merged.at(-1);
    if (last && group.top < last.bottom) {
      last.stacks.push(...group.stacks);
      last.bottom = Math.max(last.bottom, group.bottom);
    } else merged.push(group);
  });
  const grouped = new Set(merged.flatMap((group) => group.stacks));
  area.querySelectorAll(JOIN_STACKS).forEach((stack) => {
    // A Klemmen box at the L Klemme (12/u with 12/o and 12) has its Kanäle behind it, no Verbraucher.
    if (grouped.has(stack) || stack.querySelector(":scope > .graph-join-head")) return;
    const [top, bottom] = span(stack);
    merged.find((group) => top < group.bottom && bottom > group.top)?.stacks.push(stack);
  });
  merged.map((group) => group.stacks).forEach((stacks) => {
    const rights = stacks.map((stack) => stack.getBoundingClientRect().right);
    const widest = Math.max(...rights);
    stacks.forEach((stack, index) => {
      const missing = (widest - rights[index]) / scale;
      if (missing < 0.5) return;
      stack.style.setProperty("--align-extra", `${missing}px`);
      stack.style.marginRight = `${missing}px`;
      stack.dataset.loadAlign = "1";
    });
  });
}

// In a Klemmen stack PE and N stand above the L Klemme. So the L wire still runs straight through the row, every
// card on its line (Sicherung, Kanal, …, back along the first followers) moves down by their height and the wires
// of these nodes run at `--lane` plus that height. A follower further down starts a row of its own and gets its own
// lane; an FI box keeps its place.
function graphLiftStacks(area) {
  area.querySelectorAll("[data-lift]").forEach((element) => {
    element.style.removeProperty("--lane");
    // A new line (`.graph-wrap`) only gets a lane here; its offset belongs to graphFixWraps.
    if (!element.classList.contains("graph-wrap")) element.style.marginTop = "";
    delete element.dataset.lift;
  });
  const baseLane = parseFloat(getComputedStyle(area).getPropertyValue("--lane")) || 28;
  const lifts = new Map();
  const raise = (node, lift) => lifts.set(node, Math.max(lifts.get(node) || 0, lift));
  // Every Klemmen stack or box holding its L Klemme below PE/N (also a box with the Kanäle behind it, whose
  // Verbraucher stands further down the path).
  area.querySelectorAll(".graph-klemmen-stack").forEach((stack) => {
    const lCard = stack.querySelector(":scope > .graph-card:first-child");
    const lift = lCard ? lCard.offsetTop : 0;
    const start = stack.parentElement;
    if (lift < 1 || !start?.classList.contains("graph-node")) return;
    // Up: the cards in front of it on the same line (Sicherung, Kanal, …) as long as it is the first follower.
    for (let node = start; node;) {
      raise(node, lift);
      let child = node.parentElement;
      while (child && child !== area && !child.classList.contains("graph-child") && !child.classList.contains("graph-node")) child = child.parentElement;
      if (!child?.classList.contains("graph-child") || child.parentElement.classList.contains("graph-fi-out")
        || child.parentElement.firstElementChild !== child) break;
      // A row broken onto a new line (print column limit, `.graph-wrap`): only the new line gets the lane, the cards
      // of the line above stay where they are.
      const wrap = child.parentElement.parentElement;
      if (wrap?.classList.contains("graph-wrap")) {
        raise(wrap, lift);
        break;
      }
      node = child.parentElement.parentElement;
    }
    // Down: its first followers on the same line (the Kanäle behind the Klemme). A Verbraucher is not moved: it spans
    // the PE, N and L rows (it gets the lower lane from the node above anyway).
    for (let node = start.querySelector(":scope > .graph-children > .graph-child:first-child > .graph-node");
      node && !node.querySelector(":scope > .graph-card.graph-load");
      node = node.querySelector(":scope > .graph-children > .graph-child:first-child > .graph-node")) raise(node, lift);
  });
  lifts.forEach((lift, node) => {
    const lane = `${baseLane + lift}px`;
    node.style.setProperty("--lane", lane);
    node.dataset.lift = "1";
    const child = node.parentElement;
    if (child?.classList.contains("graph-child")) {
      child.style.setProperty("--lane", lane);
      child.dataset.lift = "1";
    }
    const card = node.querySelector(":scope > .graph-card");
    if (card) {
      card.style.marginTop = `${(parseFloat(getComputedStyle(card).marginTop) || 0) + lift}px`;
      card.dataset.lift = "1";
    }
    // A Klemmen box in a row (12/u with 12/o and 12 behind several Verbraucher) or a Gerät-Kanal stack behind it
    // moves down by the rest: its own rows already stand above its L card.
    const box = card ? null : node.querySelector(":scope > .graph-klemmen-stack");
    const own = box?.querySelector(":scope > .graph-card:first-child")?.offsetTop || 0;
    if (box && lift > own) {
      box.style.marginTop = `${lift - own}px`;
      box.dataset.lift = "1";
      // The L line through a Gerät-Kanal stack keeps to its card: the lane counts from the moved stack's top.
      if (!box.classList.contains("graph-klemmen-box")) box.style.setProperty("--lane", `${baseLane + own}px`);
    }
  });
  // Further followers of a moved node start rows of their own: they keep the normal lane (it would be inherited).
  lifts.forEach((lift, node) => {
    node.querySelectorAll(":scope > .graph-children > .graph-child:not(:first-child)").forEach((child) => {
      if (child.dataset.lift) return;
      child.style.setProperty("--lane", `${baseLane}px`);
      child.dataset.lift = "1";
    });
  });
}

// The wires of rows broken onto a new line (print column limit, `graphFixWraps`; drawn by CSS) as thin bands: the
// exit down beside the card (`vertical`) and the run back left above the new line. Other wires may cross them but
// never run along them.
function graphWrapLines(area, local) {
  return [...area.querySelectorAll(".graph-wrap")].flatMap((wrap) => {
    const rect = local(wrap.getBoundingClientRect());
    const style = getComputedStyle(wrap);
    const value = (name) => parseFloat(style.getPropertyValue(name)) || 0;
    const y = rect.top + value("--wrap-mid");
    const x = rect.left + value("--wrap-card") + 11;
    return [{ left: rect.left - 3, right: x + 3, top: y - 7, bottom: y + 7, vertical: false },
      { left: x - 2, right: x + 2, top: rect.top + value("--wrap-lane"), bottom: y, vertical: true }];
  });
}

function drawGraphRails() {
  const enabled = RAIL_CODES.filter((code) => graphState.rails[code]);
  const railX = Object.fromEntries(enabled.map((code, index) => [code, 16 + index * 20]));
  document.querySelectorAll("#graph-canvas .graph-rail-area").forEach((area) => {
    const svg = area.querySelector(".graph-rail-svg");
    const box = area.getBoundingClientRect();
    // Measured rects include the page zoom (body zoom 1.35); SVG coordinates do not.
    // The exact zoom from the SVG (offsetWidth is rounded: far down a tall graph that drifts by pixels).
    const scale = svg.getScreenCTM()?.d || box.width / area.offsetWidth || 1;
    // Room for lanes is set anew below (after lifting and aligning).
    area.querySelectorAll("[data-lane-space]").forEach((stack) => {
      stack.style.removeProperty("--lane-space");
      stack.style.marginRight = "";
      delete stack.dataset.laneSpace;
    });
    graphLiftStacks(area);
    graphAlignSharedLoads(area, scale);
    const local = (rect) => ({ left: (rect.left - box.left) / scale, top: (rect.top - box.top) / scale,
      bottom: (rect.bottom - box.top) / scale, height: rect.height / scale });
    // An empty N/PE row whose wire is drawn here runs into its Verbraucher in one piece: the left edge of the
    // Verbraucher, and its CSS stub is hidden (a stub beside a drawn wire shows a step when they shift apart in print).
    area.querySelectorAll(".graph-join-svg").forEach((row) => row.classList.remove("graph-join-svg"));
    const graphLoadEntry = (row) => {
      const load = row.closest(".graph-node-stack, .graph-load-join, .graph-load-group")?.querySelector(".graph-card.graph-load");
      if (!load) return null;
      row.classList.add("graph-join-svg");
      return local(load.getBoundingClientRect()).left;
    };
    const width = area.scrollWidth;
    const height = area.scrollHeight;
    svg.setAttribute("width", width);
    svg.setAttribute("height", height);
    svg.setAttribute("viewBox", `0 0 ${width} ${height}`);
    let markup = enabled.map((code) => `<text class="graph-rail-name" x="${railX[code]}" y="12" fill="${graphInk(RAIL_COLORS[code])}">${code}</text>${graphWireMarkup([[railX[code], 18], [railX[code], height]], code, "graph-rail")}`).join("");
    // Neutral bars of the FI boxes: from below the box down to its lowest tap (drawn after the taps, no loose end).
    const buses = {};
    const busEnds = {};
    const busStarts = {};
    const busDots = [];
    if (graphState.rails.N) {
      area.querySelectorAll("[data-bus]").forEach((card) => {
        const rect = local(card.getBoundingClientRect());
        // Right of the FI's phase lines (they leave the box at its lower left, see below).
        const x = Math.round(rect.left + FI_BUS_X) + 0.5;
        buses[card.dataset.bus] = x;
        busStarts[card.dataset.bus] = rect.bottom;
        busEnds[card.dataset.bus] = rect.bottom;
        markup += `<text class="graph-tap-label" x="${x + 5}" y="${rect.bottom + 12}" fill="${RAIL_COLORS.N}">${esc(card.dataset.bus)}</text>`;
      });
    }
    // Every card is an obstacle the N/PE wires are led around.
    const cardRects = new Map([...area.querySelectorAll(".graph-card:not(.graph-join-ghost)")].map((card) => {
      const rect = local(card.getBoundingClientRect());
      return [card, { ...rect, right: rect.left + card.getBoundingClientRect().width / scale }];
    }));
    const obstacles = [...cardRects.values()];
    const wrapLines = graphWrapLines(area, local);
    // Column gap between cards (the L labels sit at its start).
    const wireGap = parseFloat(getComputedStyle(area).getPropertyValue("--graph-gap")) || 22;
    // Height of the outgoing wire below the top of a card (`--lane` minus the card's top margin).
    const wireLane = (parseFloat(getComputedStyle(area).getPropertyValue("--lane")) || 28) - 3;
    area.querySelectorAll("[data-taps]").forEach((card) => {
      const rect = local(card.getBoundingClientRect());
      JSON.parse(card.dataset.taps).forEach((tap) => {
        const onBus = tap.bus && tap.bus in buses;
        const x = onBus ? buses[tap.bus] : railX[tap.code];
        if (x === undefined || !graphState.rails[tap.code]) return;
        const y = Math.round(rect.top + rect.height * tap.part) + 0.5;
        if (onBus) busEnds[tap.bus] = Math.max(busEnds[tap.bus], y);
        // Taps to the N/PE Klemme of a Verbraucher are led around the cards.
        const toKlemme = card.classList.contains("graph-join-terminal");
        // N/PE directly at the conductor (no Klemme): one wire from the busbar straight into the Verbraucher.
        const loadCard = card.classList.contains("graph-join-direct")
          ? card.closest(".graph-load-join, .graph-node-stack, .graph-load-group")?.querySelector(".graph-load") : null;
        const points = toKlemme
          ? orthogonalGraphTap(x, y, rect.left, cardRects.get(card), obstacles, tap.code === "PE" ? 1 : 0)
          : [[x, y], [loadCard ? local(loadCard.getBoundingClientRect()).left : rect.left, y]];
        markup += graphWireMarkup(points, tap.code, `graph-tap${toKlemme ? " graph-tap-feed" : ""}`);
        // Tiny conductor label like the L1 labels: left-aligned at the start of the column gap in front of the
        // card, just above the wire (N, N4 or PE); a direct wire has its label at the Verbraucher.
        const yEnd = points.at(-1)[1];
        if (!loadCard) markup += `<text class="graph-wire-label" x="${rect.left - wireGap + 2}" y="${yEnd - 2}" fill="${graphInk(RAIL_COLORS[tap.code])}">${esc(onBus ? tap.bus : tap.code)}</text>`;
        // Junction dot where the tap leaves its bar; FI bar dots wait until the bar's end is known.
        if (onBus) busDots.push({ bus: tap.bus, x, y: points[0][1] });
        else markup += `<circle cx="${x}" cy="${points[0][1]}" r="2.8" fill="${graphInk(RAIL_COLORS[tap.code])}"/>`;
      });
    });
    // An N/PE Klemme drawn once below a Sicherung but feeding several Verbraucher: its wire branches off (dot) in
    // the gap behind the drawn chain, runs down into the free row gap below that block, along it to the gap in
    // front of the further Verbraucher and down into its empty row, whose stub leads into the Verbraucher.
    // N and PE keep a few pixels apart. Where a further Verbraucher branches off while the wire runs on down, a dot.
    const joinDrops = new Map();
    // Every wire from a drawn block of N/PE Klemmen to a further Verbraucher, with the lane of its block: where the
    // vertical wires of two blocks would lie on top of each other (same column, overlapping heights), the later one
    // moves one lane (20px) to the right (its N and PE together, 6px apart), so bundles keep a clear gap.
    const collectJoinRoutes = () => {
      const routes = [];
      area.querySelectorAll("[data-join-ref]").forEach((row) => {
        row.style.removeProperty("--join-lane");
        const source = area.querySelector(`[data-join-key="${CSS.escape(row.dataset.joinRef)}"]`);
        // Rows fed from a Klemmen box (12/o in the box of 12/u) are wired from there (see below).
        if (!source || source.classList.contains("graph-join-head")) return;
        const code = row.dataset.joinCode === "PE" ? "PE" : "N";
        if (!graphState.rails[code]) return;
        const shift = code === "PE" ? 6 : 0;
        const from = local(source.getBoundingClientRect());
        const to = local(row.getBoundingClientRect());
        // Right ends of the rows, plus the extra gap an aligned stack has behind it (not the lane room).
        const rowRight = from.left + source.getBoundingClientRect().width / scale;
        const fromRight = rowRight + graphAlignExtra(source);
        const toRight = to.left + row.getBoundingClientRect().width / scale + graphAlignExtra(row);
        // Bottom of the block the chain stands in (its Klemmen and its Verbraucher).
        const block = local((source.closest(".graph-node-stack, .graph-load-join, .graph-load-group") || source).getBoundingClientRect());
        const xOut = Math.round(fromRight + wireGap / 2 + shift) + 0.5;
        // Aligned stacks (graphAlignSharedLoads) differ by fractions of a pixel at most: one straight vertical wire.
        const xNear = Math.round(toRight + wireGap / 2 + shift) + 0.5;
        // A branch row has no wire into a Verbraucher of its own: its wire starts at the row's end (`xRow`).
        // It is drawn from its last Klemme on in one line (a CSS piece in front would sit a pixel off).
        const lastCard = source.classList.contains("graph-join-branch") ? [...source.querySelectorAll(".graph-card:not(.graph-join-ghost)")].at(-1) : null;
        const xRow = lastCard ? cardRects.get(lastCard).right : null;
        // Each branch row is a bundle of its own (its own lane), not one with the rows of its stack.
        const owner = xRow !== null ? source : source.closest(JOIN_STACKS) || source;
        const xIn = Math.abs(xNear - xOut) < 3 ? xOut : xNear;
        // The wire turns in the free gap below the drawn block; a row far below in another column (a new line of the
        // print column limit) is reached through the gap right above its own block, not around it from below.
        const target = local((row.closest(".graph-node-stack, .graph-load-join, .graph-load-group") || row).getBoundingClientRect());
        const yGap = xIn !== xOut && target.top > block.bottom + 20 ? target.top - 6 - shift : block.bottom + 5 + shift;
        routes.push({ row, code, shift, owner, xOut, xRow, xIn,
          y0: graphWireMid(from, code), yGap: Math.round(yGap) + 0.5, y1: graphWireMid(to, code) });
      });
      const owners = new Map();
      routes.forEach((route) => {
        const owner = owners.get(route.owner) || { x: route.xOut - route.shift, top: route.y0, bottom: route.y0 };
        owner.bottom = Math.max(owner.bottom, route.y1, route.yGap);
        owners.set(route.owner, owner);
      });
      const placed = [];
      [...owners.values()].sort((a, b) => a.top - b.top).forEach((owner) => {
        const taken = new Set(placed.filter((other) => Math.abs(other.x - owner.x) < 3 && other.top < owner.bottom && owner.top < other.bottom).map((other) => other.lane));
        owner.lane = 0;
        while (taken.has(owner.lane)) owner.lane += 1;
        placed.push(owner);
      });
      return { routes, owners };
    };
    let { routes, owners } = collectJoinRoutes();
    // A column with several lanes gets room for them: its Verbraucher move right (`--lane-space`), the lanes stay
    // near the Klemmen; then the wires are measured again.
    const laneRoom = new Map();
    owners.forEach((owner) => {
      if (owner.lane) laneRoom.set(Math.round(owner.x), Math.max(laneRoom.get(Math.round(owner.x)) || 0, owner.lane * 20 + 10));
    });
    if (laneRoom.size) {
      area.querySelectorAll(JOIN_STACKS).forEach((stack) => {
        const rect = local(stack.getBoundingClientRect());
        const x = rect.left + stack.getBoundingClientRect().width / scale + graphAlignExtra(stack) + wireGap / 2;
        const room = [...laneRoom].find(([column]) => Math.abs(column - x) < 3)?.[1];
        if (!room) return;
        stack.style.setProperty("--lane-space", `${room}px`);
        stack.style.marginRight = `${graphAlignExtra(stack) + room}px`;
        stack.dataset.laneSpace = "1";
      });
      ({ routes, owners } = collectJoinRoutes());
    }
    routes.forEach(({ row, code, owner, xOut, xRow, xIn, y0, yGap, y1 }) => {
      const offset = owners.get(owner).lane * 20;
      // The row's stub starts at its lane (styles.css `.graph-join-shared::after`).
      if (offset) row.style.setProperty("--join-lane", `${offset}px`);
      const out = xOut + offset;
      const into = xIn + offset;
      // From a branch row the wire only turns down (no junction); it runs on into the Verbraucher in one piece.
      const entry = graphLoadEntry(row);
      markup += graphWireMarkup([...(xRow !== null ? [[xRow, y0]] : []), [out, y0], [out, yGap], [into, yGap], [into, y1], ...(entry !== null ? [[entry, y1]] : [])], code, "graph-join-route");
      if (xRow === null) markup += `<circle cx="${out}" cy="${y0}" r="2.6" fill="${graphInk(RAIL_COLORS[code])}"/>`;
      const drop = joinDrops.get(`${code}:${into}`) || { code, x: into, ys: [] };
      drop.ys.push(y1);
      joinDrops.set(`${code}:${into}`, drop);
    });
    // Branch rows and rows behind a Klemmen box: the wire leaves the last shared Klemme (12/o), runs down the column
    // gap behind it and into the first Klemme of the row (4, 1, 2); an empty row (Verbraucher right at 12/o) is
    // crossed to its stub into the Verbraucher. Junction dots where the vertical wire runs on.
    const fromWires = new Map();
    // `target`: the card the wire ends at (not an obstacle for it); none for an empty row.
    // `entry`: the wire runs on from `x1` into the Verbraucher there (an empty row without its own stub).
    const addFromWire = (sourceKey, code, x1, y1, target = null, entry = null) => {
      const source = area.querySelector(`[data-join-card="${CSS.escape(sourceKey)}"]`);
      if (!source || !graphState.rails[code]) return;
      const wires = fromWires.get(source) || { code, rect: cardRects.get(source), ends: [] };
      wires.ends.push([x1, y1, target, entry]);
      fromWires.set(source, wires);
    };
    area.querySelectorAll("[data-join-from]").forEach((card) => {
      const code = card.closest(".graph-join-pe") ? "PE" : "N";
      addFromWire(card.dataset.joinFrom, code, cardRects.get(card).left, graphWireMid(cardRects.get(card), code), cardRects.get(card));
    });
    area.querySelectorAll("[data-join-ref]").forEach((row) => {
      if (!area.querySelector(`[data-join-key="${CSS.escape(row.dataset.joinRef)}"]`)?.classList.contains("graph-join-head")) return;
      const code = row.dataset.joinCode === "PE" ? "PE" : "N";
      const to = local(row.getBoundingClientRect());
      const stub = to.left + row.getBoundingClientRect().width / scale + graphAlignExtra(row) + wireGap / 2 + (code === "PE" ? 6 : 0);
      addFromWire(row.dataset.joinRef, code, stub, graphWireMid(to, code), null, graphLoadEntry(row));
    });
    // Whether an orthogonal wire runs through a card other than the ones it connects.
    // Whether an orthogonal wire runs through a card other than the ones it connects, or along a wrap line (a
    // horizontal piece on a horizontal one, a vertical piece on a vertical one; crossing one is fine).
    const hits = (box, [x1, y1], [x2, y2]) => Math.max(x1, x2) > box.left + 1 && Math.min(x1, x2) < box.right - 1
      && Math.max(y1, y2) > box.top + 1 && Math.min(y1, y2) < box.bottom - 1;
    const crossesCards = (points, own) => points.slice(1).some((end, index) => {
      const start = points[index];
      const vertical = start[0] === end[0];
      return obstacles.some((card) => !own.includes(card) && hits(card, start, end))
        || wrapLines.some((line) => line.vertical === vertical && hits(line, start, end));
    });
    fromWires.forEach(({ code, rect, ends }, source) => {
      // At the height of the Klemme's row wire: CSS places it at the middle of the row's inner box, below its top
      // border (a dashed line between the rows of a box).
      const row = source.closest(".graph-join-wire") || source;
      const y0 = local(row.getBoundingClientRect()).top + row.clientTop + row.clientHeight / 2 + (code === "PE" ? 0 : 0.75);
      const ink = graphInk(RAIL_COLORS[code]);
      // PE 6px in front of N, so the two wires out of one Klemmen box never lie on top of each other.
      const lane = code === "PE" ? 6 : 0;
      // A vertical wire in the column gap right behind the Klemme, or right in front of a row it feeds.
      const behind = Math.round(rect.right + Math.max(3, Math.max(4, Math.min(wireGap, Math.min(...ends.map(([x1]) => x1)) - rect.right) / 2) - lane)) + 0.5;
      const before = (x1) => Math.round(x1 - Math.max(3, Math.min(wireGap, x1 - rect.right) / 2 + lane)) + 0.5;
      const path = (x, [x1, y1]) => [[rect.right, y0], [x, y0], [x, y1], [x1, y1]];
      const inChain = !source.closest(".graph-join-head");
      // One vertical wire for all rows when it passes no card: every piece once (overlapping lines would look
      // thicker), junction dots where it runs on.
      const x = [behind, ...ends.map(([x1]) => before(x1))].find((candidate) => ends.every((end) => !crossesCards(path(candidate, end), [rect, end[2]])));
      if (x !== undefined) {
        const ys = [y0, ...ends.map(([, y1]) => y1)];
        const low = Math.min(...ys);
        const high = Math.max(...ys);
        markup += graphWireMarkup([[rect.right, y0], [x, y0]], code, "graph-join-route");
        if (high > low) markup += graphWireMarkup([[x, low], [x, high]], code, "graph-join-route");
        ends.forEach(([x1, y1, , entry]) => { markup += graphWireMarkup([[x, y1], [entry ?? x1, y1]], code, "graph-join-route"); });
        ys.forEach((y, index) => {
          if ((index === 0 && inChain) || (y > low && y < high)) markup += `<circle cx="${x}" cy="${y}" r="2.6" fill="${ink}"/>`;
        });
        return;
      }
      // Else each row gets its own wire around the cards: behind the Klemme along a free gap between the rows to
      // the column gap in front of the row (16/o in the box of 16/u on to 31/mi further up).
      ends.forEach((end) => {
        const [x1, y1, target] = end;
        const xb = before(x1);
        const left = Math.min(behind, xb);
        const right = Math.max(behind, xb);
        // The free gaps between the rows, not on a wrap line running back.
        const merged = graphBlockedSpans([...obstacles, ...wrapLines.filter((line) => !line.vertical)], left, right, [rect, target]);
        const corridors = merged.slice(1).map(([top], index) => (merged[index][1] + top) / 2 - lane)
          .sort((a, b) => Math.abs(a - y0) - Math.abs(b - y0));
        const points = [behind, xb].map((candidate) => path(candidate, end)).find((candidate) => !crossesCards(candidate, [rect, target]))
          || corridors.map((y) => [[rect.right, y0], [behind, y0], [behind, Math.round(y) + 0.5], [xb, Math.round(y) + 0.5], [xb, y1], [x1, y1]])
            .find((candidate) => !crossesCards(candidate, [rect, target]))
          || path(behind, end);
        if (end[3] !== null && end[3] !== undefined) points.push([end[3], y1]);
        markup += graphWireMarkup(points, code, "graph-join-route");
        if (inChain) markup += `<circle cx="${points[1][0]}" cy="${y0}" r="2.6" fill="${ink}"/>`;
      });
    });
    // A Klemmen box whose Verbraucher stands further down the path (behind its Kanäle): its PE and N wires run
    // straight from the box to the Verbraucher, in the free band above the Kanäle (graphLiftStacks).
    area.querySelectorAll("[data-join-route]").forEach((group) => {
      const load = group.querySelector(":scope > .graph-card");
      if (!load) return;
      const loadLeft = local(load.getBoundingClientRect()).left;
      group.querySelectorAll(".graph-klemmen-box > .graph-join-wire:not(.graph-join-branch)").forEach((row) => {
        const code = row.classList.contains("graph-join-pe") ? "PE" : "N";
        if (!graphState.rails[code]) return;
        const rect = local(row.getBoundingClientRect());
        const right = rect.left + row.getBoundingClientRect().width / scale;
        const y = Math.round(rect.top + rect.height / 2) + 0.5;
        markup += graphWireMarkup([[right, y], [loadLeft, y]], code, "graph-join-route");
        const label = row.querySelector(".graph-join-phase")?.textContent || code;
        markup += `<text class="graph-wire-label" x="${loadLeft - wireGap + 2}" y="${y - 2}" fill="${graphInk(RAIL_COLORS[code])}">${esc(label)}</text>`;
      });
    });
    // The row stubs start at the vertical wire (styles.css `.graph-join-shared::after`); above its lowest end every
    // branch is a junction.
    joinDrops.forEach(({ code, x, ys }) => {
      const lowest = Math.max(...ys);
      ys.filter((y) => y < lowest).forEach((y) => { markup += `<circle cx="${x}" cy="${y}" r="2.6" fill="${graphInk(RAIL_COLORS[code])}"/>`; });
    });
    // Phase lines out of an FI box: its Sicherungen stand below it, moved a little to the right; each phase leaves
    // the box at its bottom near the left edge (L1 outermost), runs down its own line and turns right into the
    // Sicherungen of that phase.
    area.querySelectorAll("[data-fi-out]").forEach((fiCard) => {
      const rect = local(fiCard.getBoundingClientRect());
      const inputs = JSON.parse(fiCard.dataset.taps || "[]").filter((tap) => tap.code !== "N");
      const feeds = [...area.querySelectorAll(`[data-fi-feed][data-fi="${CSS.escape(fiCard.dataset.fiOut)}"]`)].map((card) => ({
        left: local(card.getBoundingClientRect()).left,
        // At the height of the card's outgoing wire (the lane, like the wires behind the Sicherung).
        y: Math.round(local(card.getBoundingClientRect()).top + wireLane) + 0.5,
        phases: JSON.parse(card.dataset.fiFeed),
      }));
      inputs.forEach((tap, index) => {
        if (!graphState.rails[tap.code]) return;
        const own = feeds.filter((feed) => feed.phases.includes(tap.code));
        if (!own.length) return;
        const x = Math.round(rect.left + FI_LINE_X + index * FI_LINE_STEP) + 0.5;
        // A Sicherung on several phases gets each line a little lower.
        const ys = own.map((feed) => feed.y + feed.phases.indexOf(tap.code) * 5);
        const yBottom = Math.max(...ys);
        markup += graphWireMarkup([[x, rect.bottom], [x, yBottom]], tap.code, "graph-fi-line");
        own.forEach((feed, feedIndex) => {
          const y = ys[feedIndex];
          markup += graphWireMarkup([[x, y], [feed.left, y]], tap.code, "graph-fi-line");
          markup += `<text class="graph-wire-label" x="${feed.left - 2}" y="${y - 2}" fill="${graphInk(RAIL_COLORS[tap.code])}" text-anchor="end">${tap.code}</text>`;
          // Junction dot where a feed branches off the line; at its end it is only a corner.
          if (y < yBottom) markup += `<circle cx="${x}" cy="${y}" r="2.4" fill="${graphInk(RAIL_COLORS[tap.code])}"/>`;
        });
      });
    });
    Object.entries(buses).forEach(([name, x]) => {
      if (busEnds[name] > busStarts[name]) markup += `<line class="graph-rail graph-bus" x1="${x}" y1="${busStarts[name]}" x2="${x}" y2="${busEnds[name]}" stroke="${RAIL_COLORS.N}"/>`;
    });
    // At the bar's last tap the wire only turns a corner: no dot there.
    busDots.filter((dot) => dot.y < busEnds[dot.bus]).forEach((dot) => {
      markup += `<circle cx="${dot.x}" cy="${dot.y}" r="2.8" fill="${RAIL_COLORS.N}"/>`;
    });
    svg.innerHTML = markup;
  });
}

const graphRailObserver = new ResizeObserver(() => drawGraphRails());

// A section body with the conductor busbars at its left.
function graphRailAreaMarkup(inner) {
  // `graph-top`: every card sits at the top of its followers, wires run at card height (Verbraucher wires meet there).
  return `<div class="graph-rail-area graph-top"><svg class="graph-rail-svg" aria-hidden="true"></svg>${inner}</div>`;
}

// Conductors a wire carries, decided by the card it leads to and the card it comes from.
function graphPhases(child, parent, inherited) {
  if (parent?.type === "phase") return [parent.code];
  if (child.type === "fuse" && child.phases?.length) return child.phases;
  if (parent?.type === "fi" && child.type === "terminal") return parent.neutral ? [parent.neutral] : inherited;
  if (child.type === "load" && parent?.wire_phase) return [parent.wire_phase];
  return inherited;
}

// Tiny label on the wire into a card; a single conductor gets its colour (N4 is coloured like N).
function graphPhaseMarkup(phases, extraClass = "") {
  if (!phases.length) return "";
  const code = phases[0];
  const tone = phases.length === 1 && /^(L[123]|N\d*|PE)$/.test(code) ? ` graph-phase-${code.startsWith("N") ? "n" : code.toLowerCase()}` : "";
  return `<span class="graph-phase${tone}${extraClass ? ` ${extraClass}` : ""}" title="Leiter ${esc(phases.join(", "))}">${esc(phases.join("·"))}</span>`;
}

// Colour of the wire leaving a card: behind a Klemme the colour of its wire, otherwise the colour of
// the conductor it carries (L1 brown, L2 black, L3 grey, N blue).
function graphWireColor(parent, phases) {
  const klemme = parent?.type === "terminal"
    ? WIRE_COLORS.find((color) => color.code === String(parent.wire_color || "").split("/")[0])?.hex : "";
  const code = phases?.[0] || "";
  return klemme || RAIL_COLORS[/^N\d*$/.test(code) ? "N" : code] || "";
}

// Colours on paper (print layout): light ones are darkened until they read well on white; on screen unchanged.
function graphInk(hex) {
  if (!graphState.printing || !/^#[0-9a-f]{6}$/i.test(hex)) return hex;
  const rgb = [1, 3, 5].map((index) => parseInt(hex.slice(index, index + 2), 16) / 255);
  const linear = rgb.map((c) => (c <= 0.04045 ? c / 12.92 : ((c + 0.055) / 1.055) ** 2.4));
  const luminance = 0.2126 * linear[0] + 0.7152 * linear[1] + 0.0722 * linear[2];
  if (luminance <= 0.16) return hex;
  const factor = (0.16 / luminance) ** (1 / 2.2);
  return `#${rgb.map((c) => Math.round(c * factor * 255).toString(16).padStart(2, "0")).join("")}`;
}

function graphWireStyle(color) {
  return color ? ` style="--graph-wire: ${graphInk(color)}"` : "";
}

// L, N and PE Klemme of one Verbraucher as one box with a row per Klemme (each row opens its Klemme).
function graphKlemmenBoxMarkup(node, open, load) {
  return `<div class="graph-klemmen-stack graph-klemmen-box">${graphCardMarkup({ ...node, label: "Klemmen" }, open)}${graphJoinKlemmen(graphState.loadFeeds.get(load.load_id), "", true)}</div>`;
}

// Cable of a Klemme: its location and number (14/u, 14/o and 14 are one cable).
function graphCable(terminal) {
  return `${terminal.location}|${graphTitle(terminal).split("/")[0]}`;
}

// The N or PE Klemme of a Verbraucher belongs to the cable of this L Klemme.
function graphSameCable(node, load) {
  const cable = graphCable(node);
  return graphState.loadFeeds.get(load.load_id).feeds.some((feed) => feed.role !== "L" && graphCable(feed.terminal) === cable);
}

// Sicherungen view: the one Verbraucher further down this L Klemme's open path whose N/PE Klemmen are on the same
// cable (e.g. 14/u → 30/2 → Gerät-Kanal → Verbraucher with 14/o and 14), so they join the Klemme in its box; null
// otherwise.
function graphCableLoad(node, open) {
  if (!open || node.type !== "terminal" || !graphState.loadFeeds) return null;
  const found = [];
  const walk = (current) => current.children.forEach((child) => {
    if (child.type === "load") {
      // A tail is the wire on to a shared Verbraucher box already drawn elsewhere.
      if (!child.tail && graphState.loadFeeds.has(child.load_id) && graphSameCable(node, child)) found.push(child);
    } else if (child.children.length && (child.forceOpen || !graphState.collapsed.has(child.id))) {
      walk(child);
    }
  });
  walk(node);
  return found.length === 1 ? found[0] : null;
}

// Sicherungen view, several Verbraucher (or N/PE chains running on) behind this L Klemme: the first N/PE Klemmen of
// their chains on the same cable (12/o and 12 for 12/u) that are not drawn yet below this Sicherung; { PE, N } lists
// of Klemmen, or null when there are none.
function graphCableHeads(node, open) {
  if (!open || node.type !== "terminal" || !graphState.loadFeeds || !graphState.joinSeen) return null;
  const cable = graphCable(node);
  const heads = { PE: new Map(), N: new Map() };
  const walk = (current) => current.children.forEach((child) => {
    if (child.type === "load") {
      if (child.tail || !graphState.loadFeeds.has(child.load_id)) return;
      Object.keys(heads).forEach((role) => {
        const head = graphFeedChain(graphState.loadFeeds.get(child.load_id), role)?.[0];
        if (head && graphCable(head) === cable && !graphState.joinSeen.has(graphJoinKey(role, [head]))) heads[role].set(head.entity_id, head);
      });
    } else if (graphIsOpen(child)) {
      walk(child);
    }
  });
  walk(node);
  return heads.PE.size || heads.N.size ? { PE: [...heads.PE.values()], N: [...heads.N.values()] } : null;
}

// Rows of these first Klemmen in the L Klemme's box (PE, then N; tapped from their busbar). The rows of the
// Verbraucher further on start behind them (`graphJoinKlemmen`), their wires come from here (`drawGraphRails`).
function graphCableHeadsMarkup(heads) {
  return ["PE", "N"].map((role) => heads[role].map((terminal) => {
    const key = graphJoinKey(role, [terminal]);
    graphState.joinSeen.add(key);
    graphState.joinHeads.add(key);
    const taps = graphFeedTaps(role, terminal.wire_phase || role);
    return `<div class="graph-join-wire graph-join-${role.toLowerCase()} graph-join-head" data-join-key="${esc(key)}">${graphJoinCardMarkup(role, [terminal], 0, { taps })}</div>`;
  }).join("")).join("");
}

// The path with one Verbraucher node replaced by a tail (the wire on to the Verbraucher box at the right).
function graphTailFor(node, load) {
  return { ...node, children: node.children.map((child) => (child === load ? { ...load, tail: true, children: [] } : graphTailFor(child, load))) };
}

// Print with a column limit (`graphState.printColumns`, PDF dialog): the most box columns a row may use.
function graphMaxColumns() {
  return graphState.printColumns || Infinity;
}

// A node is open when it has followers and is not folded.
function graphIsOpen(node) {
  return node.children.length > 0 && (node.forceOpen || !graphState.collapsed.has(node.id));
}

// The Verbraucher a Klemme or Gerät-Kanal ends in when it is its only follower (drawn as a stack with the N and PE
// Klemmen, two columns together); null otherwise.
function graphStackLoad(node) {
  const lone = graphIsOpen(node) && node.children.length === 1 ? node.children[0] : null;
  return lone?.type === "load" && !lone.tail && ["terminal", "channel"].includes(node.type) && graphState.loadFeeds?.has(lone.load_id) ? lone : null;
}

// Columns a path takes from this node on (a tail is only a wire and takes none).
function graphDepth(node) {
  if (node.tail) return 0;
  return 1 + (graphIsOpen(node) ? Math.max(0, ...node.children.map(graphDepth)) : 0);
}

// The followers of a node in column `col` must break onto a new line when they would pass the column limit; a
// Klemme with its Verbraucher (stack) moves as a whole. The new line continues in the row's `home` column, the one
// right after its Sicherung (2, behind an FI 3), so the Sicherungen column stays free.
function graphWrapsAfter(node, col) {
  return col + 1 + (node.children.some((child) => graphStackLoad(child)) ? 1 : 0) > graphMaxColumns();
}

// Followers of a card to the right; each follower's wire in its own colour, the short stub out of the card takes
// the first one's colour. Past the column limit they continue below the card from column 2 (`graph-wrap`, its
// position and the wire back are set by `graphFixWraps`).
function graphFollowersMarkup(node, open, inherited, col = 1, home = 2) {
  if (!open) return "";
  const wrap = graphWrapsAfter(node, col);
  // A broken row moves back only as far as it needs to fit flush right into the column limit (at most to `home`).
  const childCol = wrap ? Math.max(home, Math.min(col, graphMaxColumns() + 1 - Math.max(...node.children.map(graphDepth)))) : col + 1;
  const wires = node.children.map((child) => graphWireColor(node, graphPhases(child, node, inherited)));
  const followers = `<div class="graph-children"${graphWireStyle(wires[0])}>${node.children.map((child, index) => {
    const phases = graphPhases(child, node, inherited);
    return `<div class="graph-child"${graphWireStyle(wires[index])}>${graphPhaseMarkup(phases)}${graphNodeMarkup(child, phases, childCol, home)}</div>`;
  }).join("")}</div>`;
  return wrap ? `<div class="graph-wrap" data-wrap-col="${col}" data-wrap-home="${childCol}"${graphWireStyle(wires[0])}><i class="graph-wrap-out" aria-hidden="true"></i>${followers}</div>` : followers;
}

// A card with its followers to the right; the connector lines are drawn by CSS in the colour of their wire. `col` is
// the box column of the card in its row, `home` the column a broken row continues in (print column limit).
function graphNodeMarkup(node, inherited = [], col = 1, home = col + 1) {
  const open = graphIsOpen(node);
  // The end of a row in a shared Verbraucher block: only the wire on to the shared box.
  if (node.tail) return '<div class="graph-load-tail"></div>';
  // Sicherungen view: a Klemme (or Gerät-Kanal) whose only follower is its Verbraucher gets the N and PE Klemme
  // stacked below it, so L, N and PE run side by side straight into the Verbraucher without an extra column.
  const lone = graphStackLoad(node);
  if (lone) {
    const phases = graphPhases(lone, node, inherited);
    const wire = graphWireStyle(graphWireColor(node, phases));
    // A Klemme makes one box with the N and PE Klemme of its cable; a Gerät-Kanal (or a Klemme of another cable)
    // stays a card above them.
    const stack = node.type === "terminal" && graphSameCable(node, lone) ? graphKlemmenBoxMarkup(node, open, lone)
      : `<div class="graph-klemmen-stack">${graphCardMarkup(node, open)}${graphJoinKlemmen(graphState.loadFeeds.get(lone.load_id), node.location, true)}</div>`;
    // The wire colour also on the stack: its L wire runs on behind the L Klemme to the stack's right edge.
    return `<div class="graph-node graph-node-stack"${wire}>${stack}<div class="graph-children"${wire}><div class="graph-child"${wire}>${graphPhaseMarkup(phases)}<div class="graph-node">${graphCardMarkup({ ...lone, plain: true }, false)}</div></div></div></div>`;
  }
  // A Verbraucher further down the path whose N/PE Klemmen are on this Klemme's cable: the Klemme's box holds them,
  // the Verbraucher stands once at the right over the whole block, the path's L wire runs into it and the N/PE
  // wires are led below the path (`drawGraphRails`, `data-join-route`).
  // Only when the whole block fits into the column limit: the Verbraucher stands behind the deepest card.
  const cableLoad = graphCableLoad(node, open);
  const routed = cableLoad && col + graphDepth(graphTailFor(node, cableLoad)) <= graphMaxColumns() ? graphTailFor(node, cableLoad) : null;
  if (routed) {
    const row = `<div class="graph-node">${graphKlemmenBoxMarkup(node, open, cableLoad)}${graphFollowersMarkup(routed, open, inherited, col, home)}</div>`;
    return `<div class="graph-load-group graph-klemmen-route" data-join-route><div class="graph-load-group-feeds"><div class="graph-load-group-row">${row}</div></div>${graphCardMarkup({ ...cableLoad, plain: true }, false)}</div>`;
  }
  // `top` keeps the card at the top of its followers (Sicherungen view) instead of in their middle; a node whose
  // followers break onto a new line stacks them below its card.
  const classes = `${node.top ? " graph-node-top" : ""}${open && graphWrapsAfter(node, col) ? " graph-node-wrap" : ""}`;
  // Several Verbraucher behind it with N/PE Klemmen of its cable (12/u with 12/o and 12): one Klemmen box for them.
  const heads = graphCableHeads(node, open);
  if (heads) {
    const box = `<div class="graph-klemmen-stack graph-klemmen-box">${graphCardMarkup({ ...node, label: "Klemmen" }, open)}${graphCableHeadsMarkup(heads)}</div>`;
    return `<div class="graph-node${classes}">${box}${graphFollowersMarkup(node, open, inherited, col, home)}</div>`;
  }
  return `<div class="graph-node${classes}">${graphCardMarkup(node, open)}${graphFollowersMarkup(node, open, inherited, col, home)}</div>`;
}

// Location of a chain: its top node (below a conductor start node, the start node's children are the tops).
function graphChainTops() {
  return graphState.roots.flatMap((root) => (root.type === "phase" ? root.children : [root]));
}

// The conductor section: each start node keeps only the chains of the chosen location.
function graphConductorTrees(test) {
  return graphState.roots.filter((root) => root.type === "phase").map((root) => {
    const children = root.children.filter((child) => !graphState.location || (child.location || GRAPH_NO_LOCATION) === graphState.location);
    if (!children.length && (graphState.location || test)) return null;
    const tree = { ...root, children: children.map((child) => pruneGraph(child, test)).filter(Boolean) };
    return test && !tree.children.length ? null : tree;
  }).filter(Boolean);
}

function graphTreesMarkup(trees) {
  return `<div class="graph-trees">${trees.map((tree) => `<div class="graph-tree">${graphNodeMarkup(tree)}</div>`).join("")}</div>`;
}

// Conductor band on top (one card per start node, jumps to its section) and a section per conductor,
// whose chains start with a short wire carrying the conductor label.
function graphConductorMarkup(conductors) {
  if (!conductors.length) return "";
  const band = conductors.map((root) => `<button class="graph-band-card graph-conductor-${esc(root.code.toLowerCase())}" type="button" data-graph-jump="${esc(root.code)}"><strong>${esc(root.code)}</strong><span>${esc(root.subtitle)}</span><small>${root.children.length.toLocaleString("de-DE")} Abgänge</small></button>`).join("");
  const sections = conductors.map((root) => {
    const trees = root.children.map((child) => `<div class="graph-tree"><div class="graph-child graph-feed-in">${graphPhaseMarkup([root.code])}${graphNodeMarkup(child, [root.code])}</div></div>`).join("");
    return `<section class="graph-group graph-conductor-group graph-conductor-${esc(root.code.toLowerCase())}" id="graph-conductor-${esc(root.code)}"><h3><span class="graph-conductor-badge">${esc(root.code)}</span>${esc(root.subtitle)} <small>${root.children.length.toLocaleString("de-DE")} Abgänge</small></h3><div class="graph-trees">${trees}</div></section>`;
  }).join("");
  return `<div class="graph-conductor-band" aria-label="Leiter">${band}</div>${sections}`;
}

// Every Verbraucher with the Klemmen that feed it and the path above each Klemme.
function collectGraphLoads() {
  const loads = new Map();
  const walk = (node, path) => {
    if (node.type === "load") {
      const terminal = path[path.length - 1];
      const entry = loads.get(node.load_id) || { key: node.load_id, title: node.title, location: node.location, no_pe: node.no_pe,
        page: [node.entity_type || "load", node.entity_id || String(node.load_id)], feeds: [] };
      // Directly below N, an FI (its neutral, N4) or PE: an N or PE feed without Klemme.
      if (terminal?.type === "phase" || terminal?.type === "fi") {
        const code = terminal.type === "fi" ? terminal.neutral || "N" : terminal.code;
        entry.feeds.push({ terminal: { title: "direkt", wire_phase: code, entity_id: "" }, role: code === "PE" ? "PE" : "N", path: [], direct: true });
      }
      // Directly on a Sicherung: its L feed is the Sicherung itself (the path includes it, so it groups there).
      else if (terminal?.type === "fuse") entry.feeds.push({ terminal: { ...terminal, wire_phase: (terminal.phases || []).join("/") || "L" }, role: "L", path, fuse: true });
      else if (terminal) entry.feeds.push({ terminal, role: terminalRole(terminal), path: path.slice(0, -1) });
      loads.set(node.load_id, entry);
      return;
    }
    node.children.forEach((child) => walk(child, [...path, node]));
  };
  graphState.roots.forEach((root) => walk(root, []));
  return [...loads.values()].sort((a, b) => naturalCollator.compare(a.location || "", b.location || "") || naturalCollator.compare(a.title, b.title));
}

// One feed column: conductor chip, the path (FI, Sicherung, Kanal) and the Klemme; "fehlt" when missing.
// `shown` lists the path types to name (the Sicherungen view leaves out the Sicherung it is listed under).
// `noPe`: the Verbraucher has no PE connection at all (no PE is not missing then).
function graphFeedMarkup(feeds, role, shown = ["fi", "fuse", "channel"], noPe = false) {
  const own = feeds.filter((feed) => feed.role === role);
  if (!own.length && role === "PE" && noPe) return '<span class="graph-feed-none">ohne PE</span>';
  if (!own.length) return `<span class="graph-feed-missing">${role} fehlt</span>`;
  return own.map((feed) => {
    // The path, plus the Klemmen in front of the feed's Klemme (20/o for N → 20/o → 32/1) where a path is named.
    // (A Verbraucher directly on a Sicherung has that Sicherung as its feed: it is not repeated in the path.)
    const via = [...feed.path.filter((node) => shown.includes(node.type) && node.id !== feed.terminal.id), ...(shown.length ? graphFeedKlemmen(feed) : [])].map(graphTitle).join(" · ");
    // Without a conductor (no Sicherung or FI) the phase is unknown.
    const code = feed.terminal.wire_phase || `${role}?`;
    const taps = graphFeedTaps(role, code);
    if (feed.direct) return `<span class="graph-feed graph-feed-direct"${graphTapsAttribute(taps)}>${graphPhaseMarkup([code])}<strong>direkt</strong></span>`;
    return `<button class="graph-feed" type="button" data-type="${esc(feed.terminal.entity_type || "terminal")}" data-id="${esc(feed.terminal.entity_id)}"${graphTapsAttribute(taps)}>${graphPhaseMarkup([code])}<strong>${esc(graphTitle(feed.terminal))}</strong>${via ? `<small>${esc(via)}</small>` : ""}</button>`;
  }).join("");
}

function graphLoadsMarkup(words) {
  const flagged = graphState.flaggedOnly ? flaggedTerminalIds() : null;
  // Same order as the main view: by Sicherung, then Verbraucher.
  const rows = collectGraphFuses().flatMap((group) => group.loads)
    .filter((load, index, all) => all.indexOf(load) === index)
    .filter((load) => (!graphState.location || (load.location || GRAPH_NO_LOCATION) === graphState.location)
      && (!flagged || loadIsFlagged(load, flagged))
      && words.every((word) => loadSearchText(load).includes(word)));
  if (!rows.length) return "";
  const body = rows.map((load) => `<div class="graph-load-row"><div class="graph-load-name"><strong>${esc(load.title)}</strong><small>${esc(load.location || "")}</small></div><div class="graph-feed-cell">${graphFeedMarkup(load.feeds, "L")}</div><div class="graph-feed-cell">${graphFeedMarkup(load.feeds, "N")}</div><div class="graph-feed-cell">${graphFeedMarkup(load.feeds, "PE", undefined, load.no_pe)}</div></div>`).join("");
  return `<section class="graph-group graph-loads"><div class="graph-load-row graph-load-head"><span>Verbraucher</span><span>L · Sicherung · Kanal · Klemme</span><span>N · FI · Klemme</span><span>PE · Klemme</span></div>${body}</section>`;
}

// Sicherungen of a Verbraucher: those on the paths of its L feeds.
function loadFuseIds(load) {
  return new Set(load.feeds.filter((feed) => feed.role === "L")
    .flatMap((feed) => feed.path.filter((node) => node.type === "fuse").map((node) => node.entity_id)));
}

// Every Sicherung (with its FI) in natural order, each with the Verbraucher it supplies; Verbraucher
// without a Sicherung come last in a group of their own (fuse: null).
function collectGraphFuses() {
  const fuses = new Map();
  const walk = (node, path) => {
    if (node.type === "fuse" && !fuses.has(node.entity_id)) {
      fuses.set(node.entity_id, { fuse: node, fi: [...path].reverse().find((item) => item.type === "fi"), loads: [] });
    }
    node.children.forEach((child) => walk(child, [...path, node]));
  };
  graphState.roots.forEach((root) => walk(root, []));
  const loose = { fuse: null, fi: null, loads: [] };
  collectGraphLoads().forEach((load) => {
    const ids = [...loadFuseIds(load)].filter((id) => fuses.has(id));
    if (!ids.length) loose.loads.push(load);
    ids.forEach((id) => fuses.get(id).loads.push(load));
  });
  const ordered = [...fuses.values()].sort((a, b) => naturalCollator.compare(a.fuse.location || "", b.fuse.location || "")
    || naturalCollator.compare(a.fuse.title, b.fuse.title));
  return loose.loads.length ? [...ordered, loose] : ordered;
}

// Text a Verbraucher is found by: its name, location and everything on its feeds.
function loadSearchText(load) {
  return `${load.title} ${load.location} ${load.feeds.map((feed) => `${feed.terminal.title} ${feed.terminal.wire_phase} ${feed.path.map((node) => node.title).join(" ")}`).join(" ")}`.toLowerCase();
}

// A Verbraucher missing N or PE, or fed by a Klemme on the Prüfliste.
function loadIsFlagged(load, flagged) {
  const roles = new Set(load.feeds.map((feed) => feed.role));
  return !roles.has("N") || (!roles.has("PE") && !load.no_pe) || load.feeds.some((feed) => flagged.has(feed.terminal.entity_id));
}

// Verbraucher at the end of a path in the Sicherungen view: the L wire comes along the path (top lane); the N and
// PE Klemmen stand as cards in their own wires below it, fed from their busbar (tap) and wired into the Verbraucher.
function graphLoadJoinMarkup(node, load) {
  return `<div class="graph-load-join"><div class="graph-join-feeds">${graphJoinKlemmen(load)}</div>${graphCardMarkup({ ...node, plain: true }, false)}</div>`;
}

// Klemmen directly in front of a feed's Klemme on its path (20/o for N → 20/o → 32/1), nearest last.
function graphFeedKlemmen(feed) {
  const before = [];
  for (let index = feed.path.length - 1; index >= 0 && feed.path[index].type === "terminal"; index -= 1) before.unshift(feed.path[index]);
  return before;
}

// N and PE Klemme of a Verbraucher as cards in their wires (fed by a busbar tap; "fehlt" when missing).
// `home`: room of the L Klemme above them; an N/PE chain starting in another room reaches left into the free space.
// `peFirst`: rows of a Klemmen stack (PE, N, then the L Klemme).
function graphJoinKlemmen(load, home = "", peFirst = false) {
  // PE and N Klemmen of one cable (same room and Klemmen number, e.g. 20 and 20/o) form one box without a gap, like
  // a Klemmen box with L; further Klemmen of the chains join it only where they are of one cable as well.
  const chains = { PE: graphFeedChain(load, "PE"), N: graphFeedChain(load, "N") };
  const keyOf = (role) => graphJoinKey(role, chains[role]);
  // Klemmen at the start of a chain already drawn in the box of the L Klemme in front (12/o in the box of 12/u):
  // the row starts behind them, its wire comes from there.
  // The same for a Klemme drawn in the Klemmen box of another Verbraucher of its cable (16/o with 16/u for
  // "Steckdose", while 16/o also feeds 31/mi): only that box draws it (`graphCableOwners`).
  const skipOf = (role) => {
    if (!chains[role]) return 0;
    const head = graphJoinKey(role, chains[role].slice(0, 1));
    const owner = graphState.joinOwners?.get(head);
    return graphState.joinHeads?.has(head) || (owner !== undefined && owner !== load.key) ? 1 : 0;
  };
  const skips = { PE: skipOf("PE"), N: skipOf("N") };
  // Other chains below this Sicherung that start with Klemmen of this one: drawn here as branch rows. Not when
  // they only share the Klemmen of that box: then every Verbraucher draws its own rows beside its L (wired from
  // the box), so everything going to one Verbraucher stays close together.
  const branchesOf = (role) => (chains[role] && !graphState.joinSeen?.has(keyOf(role))
    ? graphJoinBranches(role, chains[role]).filter(({ from }) => from > skips[role]) : []);
  const branches = { PE: branchesOf("PE"), N: branchesOf("N") };
  const seen = graphState.joinSeen;
  // Rows of one role: a finished row ({ html }) or a chain of Klemmen ({ role, chain, skip, from, ... }): the main
  // row (its Verbraucher beside it) and the branch rows (12/o → 2 below 12/o → 1, wired on to further Verbraucher).
  const rowsOf = (role) => {
    const feed = load.feeds.find((item) => item.role === role);
    // No PE connection at all: no row for it.
    if (!feed && role === "PE" && load.no_pe) return [];
    if (!feed) return [{ html: `<div class="graph-join-missing graph-join-${role.toLowerCase()}">${role} fehlt</div>` }];
    const code = feed.terminal.wire_phase || role;
    const taps = graphFeedTaps(role, code);
    // Directly at N/Nx/PE (no Klemme): no card, the wire just runs through its narrow row (`.graph-join-direct`).
    if (feed.direct) return [{ html: `<div class="graph-join-wire graph-join-${role.toLowerCase()}"><span class="graph-join-direct" title="direkt, ohne Klemme"${graphTapsAttribute(taps)}></span>${graphPhaseMarkup([code], "graph-join-phase")}</div>` }];
    // Every Klemme the wire runs through, from the busbar to the Verbraucher (e.g. N → 20/o → 32/1); the busbar
    // tap ends at the first one, the badge with the conductor sits on the last one.
    const chain = chains[role];
    // Below one Sicherung every Klemme is drawn once: a chain that feeds a further Verbraucher only leaves an empty
    // row there; `drawGraphRails` leads its wire from the drawn chain (`data-join-key`) to it (`data-join-ref`).
    const key = graphJoinKey(role, chain);
    // The same when the whole chain stands in the Klemmen box of another Verbraucher drawn further on (80/o).
    if (seen?.has(key) || skips[role] >= chain.length) {
      return [{ html: `<div class="graph-join-wire graph-join-${role.toLowerCase()} graph-join-shared" data-join-ref="${esc(key)}" data-join-code="${esc(role)}">${graphPhaseMarkup([code], "graph-join-phase")}</div>` }];
    }
    seen?.add(key);
    branches[role].forEach(({ chain: other }) => seen.add(graphJoinKey(role, other)));
    const skip = skips[role];
    // The first Klemme of the chain in the L Klemme's room stands below it (same column), the rest follows to the
    // right; a chain starting in another room reaches left into the free space (`--before`: Klemmen in front of the
    // last). Its branch rows keep the same columns.
    const shown = chain.length - skip;
    const reach = home && shown > 1 && chain[skip].location !== home;
    const chainAttrs = `class="graph-join-chain${reach ? " graph-join-chain-reach" : ""}"${reach ? ` style="--before: ${shown - 1}"` : ""}`;
    return [{ role, code, chain, skip, from: skip, key, taps, chainAttrs, main: true },
      ...branches[role].map(({ chain: other, from }) => ({ role, code, chain: other, skip, from, key: graphJoinKey(role, other), chainAttrs }))];
  };
  // One row's markup; `pair` ("top"/"bottom") with its `partner`: PE and N of one cable as one box without a gap,
  // further Klemmen of the two chains join it only where they are of one cable as well (else `graph-join-split`).
  const rowMarkup = (row, pair = "", partner = null) => {
    if (row.html) return row.html;
    const { role, code, chain, skip, from, key, main } = row;
    const split = (index) => {
      const other = partner?.chain[index - skip + partner.skip];
      return Boolean(pair) && (!other || graphCable(other) !== graphCable(chain[index]));
    };
    const cards = chain.map((terminal, index) => (index < skip ? "" : graphJoinCardMarkup(role, chain, index, {
      taps: main && index === 0 ? row.taps : null, ghost: index < from,
      from: index === from && from ? graphJoinKey(role, chain.slice(0, from)) : "",
      badge: index === chain.length - 1 ? graphPhaseMarkup([code], "graph-phase-inline") : "", split: index >= from && split(index) }))).join("");
    const classes = `graph-join-wire graph-join-${role.toLowerCase()}${main ? "" : " graph-join-branch"}${pair ? ` graph-join-pair-${pair}` : ""}`;
    // The main row's wire runs into the Verbraucher with its conductor label; a branch row's wire is drawn on.
    const inner = chain.length - skip > 1 || !main ? `<div ${row.chainAttrs}>${cards}</div>` : cards;
    return `<div class="${classes}"${seen ? ` data-join-key="${esc(key)}"` : ""}>${inner}${main ? graphPhaseMarkup([code], "graph-join-phase") : ""}</div>`;
  };
  // In a Klemmen stack PE stands on top, then N, then the L Klemme (styles.css `order`); elsewhere N, PE below L.
  if (!peFirst) return [...rowsOf("N"), ...rowsOf("PE")].map((row) => rowMarkup(row)).join("");
  // Stack: the rows of one cable (same room and Klemmen number: 3/pe and 3/n) stand together, PE above N; one PE
  // and one N row starting in the same column form a box.
  const groups = new Map();
  [...rowsOf("PE"), ...rowsOf("N")].forEach((row, index) => {
    const group = row.html ? `row:${index}` : graphCable(row.chain[row.from]);
    groups.set(group, [...(groups.get(group) || []), row]);
  });
  return [...groups.values()].map((rows) => {
    const [pe, n] = [rows.filter((row) => row.role === "PE"), rows.filter((row) => row.role === "N")];
    const pairs = pe.length === 1 && n.length === 1 && pe[0].from - pe[0].skip === n[0].from - n[0].skip;
    return pairs ? `${rowMarkup(pe[0], "top", n[0])}${rowMarkup(n[0], "bottom", pe[0])}` : rows.map((row) => rowMarkup(row)).join("");
  }).join("");
}

// Klemmen of a Verbraucher's N or PE wire from the busbar on (N → 20/o → 32/1); null when missing or direct.
function graphFeedChain(load, role) {
  const feed = load.feeds.find((item) => item.role === role);
  return feed && !feed.direct ? [...graphFeedKlemmen(feed), feed.terminal] : null;
}

// Key of an N/PE chain below one Sicherung (also of its first Klemmen, for the branch rows).
function graphJoinKey(role, chain) {
  return `${graphState.joinSection}:${role}:${chain.map((terminal) => terminal.entity_id).join(">")}`;
}

// One Klemme of an N/PE chain. Every drawn one is a branch point (`data-join-card`); `ghost` keeps the column of a
// Klemme drawn in the row above free; `from` marks the first Klemme of a branch row (its wire comes from there).
function graphJoinCardMarkup(role, chain, index, { taps = null, badge = "", split = false, ghost = false, from = "" } = {}) {
  const terminal = chain[index];
  const attrs = ghost ? ' inert aria-hidden="true"'
    : ` data-join-card="${esc(graphJoinKey(role, chain.slice(0, index + 1)))}"${from ? ` data-join-from="${esc(from)}"` : ""}`;
  return graphCardMarkup({
    ...terminal, type: "terminal", title: graphTitle(terminal), subtitle: "", children: [], entity_type: "terminal",
    taps, badge, attrs, extraClass: `graph-join-terminal${split ? " graph-join-split" : ""}${ghost ? " graph-join-ghost" : ""}` }, false);
}

// Chains of this Sicherung (`graphState.joinChains`) not drawn yet that start with Klemmen of `chain` or of an
// earlier branch: [{ chain, from }], `from` = number of shared Klemmen in front. A chain ending in a shared Klemme
// is no branch (nothing left to draw).
function graphJoinBranches(role, chain) {
  const drawn = [chain];
  const keys = new Set([graphJoinKey(role, chain)]);
  const found = [];
  (graphState.joinChains?.[role] || []).forEach((other) => {
    const key = graphJoinKey(role, other);
    if (keys.has(key) || graphState.joinSeen?.has(key)) return;
    const from = Math.max(...drawn.map((row) => {
      let shared = 0;
      while (shared < row.length && shared < other.length && row[shared].entity_id === other[shared].entity_id) shared += 1;
      return shared;
    }));
    if (!from || from >= other.length) return;
    keys.add(key);
    drawn.push(other);
    found.push({ chain: other, from });
  });
  return found;
}

// First N/PE Klemmen that a Klemmen box (L Klemme with its Verbraucher, `graphStackLoad`) draws because they are of
// its cable: key of the one-Klemme chain → Verbraucher of that box. Other chains starting with them start behind them.
function graphCableOwners(cards) {
  const owners = new Map();
  const walk = (node) => {
    const lone = node.type === "terminal" ? graphStackLoad(node) : null;
    if (lone && graphSameCable(node, lone)) {
      ["PE", "N"].forEach((role) => {
        const head = graphFeedChain(graphState.loadFeeds.get(lone.load_id), role)?.[0];
        if (head && graphCable(head) === graphCable(node)) owners.set(graphJoinKey(role, [head]), lone.load_id);
      });
    }
    if (graphIsOpen(node)) node.children.forEach(walk);
  };
  cards.forEach(walk);
  return owners;
}

// N and PE chains of all Verbraucher in the given paths (one Sicherung or a shared block), for the branch rows.
function graphSectionChains(cards) {
  const chains = { N: [], PE: [] };
  const done = new Set();
  const walk = (node) => {
    if (node.type === "load" && !done.has(node.load_id) && graphState.loadFeeds?.has(node.load_id)) {
      done.add(node.load_id);
      Object.keys(chains).forEach((role) => {
        const chain = graphFeedChain(graphState.loadFeeds.get(node.load_id), role);
        if (chain) chains[role].push(chain);
      });
    }
    if (graphIsOpen(node)) node.children.forEach(walk);
  };
  cards.forEach(walk);
  return chains;
}

// Verbraucher card: name and location, then its L, N and PE feed side by side (Sicherung and FI are on the parent card).
function graphLoadCardMarkup(load) {
  // The L Klemme is the one on the path; N and PE are shown so their busbar taps have an end.
  // The name opens the Verbraucher: its Klemme or Sicherung page, else its own page.
  return `<div class="graph-card graph-load graph-load-card"><button class="graph-load-card-name" type="button" data-type="${esc(load.page[0])}" data-id="${esc(load.page[1])}"><small>Verbraucher</small><strong>${esc(load.title)}</strong>${load.location ? `<span>${esc(load.location)}</span>` : ""}</button><div class="graph-load-card-feeds">${["L", "N", "PE"].map((role) => `<div class="graph-feed-cell">${graphFeedMarkup(load.feeds, role, [], load.no_pe)}</div>`).join("")}</div></div>`;
}

// The Verbraucher a Sicherung's path ends in, when the path is one line (no branches, nothing folded); else null.
function graphLoneLoad(node) {
  for (let current = node; current; current = current.children[0]) {
    if (graphState.collapsed.has(current.id) && !current.forceOpen) return null;
    if (current.children.length !== 1) return null;
    const child = current.children[0];
    if (child.type === "load") return ["terminal", "channel"].includes(current.type) && graphState.loadFeeds?.has(child.load_id) ? child : null;
  }
  return null;
}


// Neighbouring Sicherungen that feed the same Verbraucher (e.g. L1, L2, L3 of a Kraftsteckdose) share one block:
// each row keeps its path to the L Klemme, the N and PE Klemmen come once below them, the Verbraucher box once
// to the right over all rows. `entries` are {card, phases}; `wrap` puts one row or block into its container.
function graphFuseRowsMarkup(entries, wrap, col = 1) {
  const parts = [];
  for (let index = 0; index < entries.length;) {
    // Each Sicherung (or Sicherungen sharing one Verbraucher block) draws every N/PE Klemme once (`graphJoinKlemmen`).
    graphState.joinSection = (graphState.joinSection || 0) + 1;
    graphState.joinSeen = new Set();
    // The block's Verbraucher stands behind the longest row: only when that fits into the column limit.
    const lone = graphLoneLoad(entries[index].card);
    const load = lone && col + graphDepth(graphTailFor(entries[index].card, lone)) <= graphMaxColumns() ? lone : null;
    let end = index + 1;
    while (load && end < entries.length && graphLoneLoad(entries[end].card)?.load_id === load.load_id) end += 1;
    // All N/PE chains of these paths, so a chain drawn first takes the branches of its Klemmen along.
    graphState.joinChains = graphSectionChains(entries.slice(index, end).map((entry) => entry.card));
    graphState.joinHeads = new Set();
    graphState.joinOwners = graphCableOwners(entries.slice(index, end).map((entry) => entry.card));
    if (end - index < 2) {
      parts.push(wrap(graphNodeMarkup(entries[index].card, entries[index].phases, col)));
    } else {
      const rows = entries.slice(index, end).map((entry) => `<div class="graph-load-group-row">${graphNodeMarkup(graphTailFor(entry.card, graphLoneLoad(entry.card)), entry.phases, col)}</div>`).join("");
      const join = `<div class="graph-klemmen-stack graph-klemmen-box graph-load-group-join">${graphJoinKlemmen(graphState.loadFeeds.get(load.load_id))}</div>`;
      parts.push(wrap(`<div class="graph-load-group"><div class="graph-load-group-feeds">${rows}${join}</div>${graphCardMarkup({ ...load, plain: true }, false)}</div>`));
    }
    index = end;
  }
  graphState.joinSeen = null;
  graphState.joinChains = null;
  graphState.joinHeads = null;
  graphState.joinOwners = null;
  return parts.join("");
}

// N Klemmen (the Klemme an N wire ends at) of all Verbraucher in a subtree.
function graphNeutralKeys(node, keys = new Set()) {
  if (node.type === "load") {
    const feed = graphState.loadFeeds?.get(node.load_id)?.feeds.find((item) => item.role === "N" && !item.direct);
    if (feed) keys.add(feed.terminal.entity_id);
  }
  node.children.forEach((child) => graphNeutralKeys(child, keys));
  return keys;
}

// N Klemme of the last Verbraucher of a subtree in display order (where the next follower should continue), or null.
function graphLastNeutralKey(node) {
  if (node.type === "load") return [...graphNeutralKeys(node)][0] || null;
  for (let index = node.children.length - 1; index >= 0; index -= 1) {
    const key = graphLastNeutralKey(node.children[index]);
    if (key) return key;
  }
  return null;
}

// Display order only: Verbraucher on the same N Klemme move close together, so their shared N/PE wires stay short.
// Followers are placed one by one, continuing with the current N Klemme: first those fed by it alone, then those
// fed by it among others (inside them the branch on it, `lead`, comes first), else the next in tree order. After a
// follower the order continues with the N Klemme of its last Verbraucher.
function graphGroupByNeutral(node, lead = null) {
  const remaining = node.children.map((child) => ({ child, keys: graphNeutralKeys(child) }));
  const placed = [];
  let current = lead;
  while (remaining.length) {
    const only = remaining.findIndex((entry) => current && entry.keys.size === 1 && entry.keys.has(current));
    const among = remaining.findIndex((entry) => current && entry.keys.has(current));
    const index = only >= 0 ? only : among >= 0 ? among : 0;
    const [entry] = remaining.splice(index, 1);
    const shown = graphGroupByNeutral(entry.child, entry.keys.has(current) ? current : null);
    placed.push(shown);
    current = graphLastNeutralKey(shown) || current;
  }
  return { ...node, children: placed };
}

// Main view: Sicherungen in natural order (by room and designation, not by phase; an FI's Sicherungen below it),
// each followed by its path Kanal -> Klemme -> (further Klemmen) -> Verbraucher; the Verbraucher is the end point.
function graphFusesMarkup(words) {
  const test = graphTest(words);
  const groups = collectGraphFuses();
  // Each shown Sicherung with its path; a Sicherung without FI taps its phase busbar directly.
  const blocks = [];
  const fiBlocks = new Map();
  let shownFuses = 0;
  groups.filter((group) => group.fuse
    && (!graphState.location || (group.fuse.location || GRAPH_NO_LOCATION) === graphState.location)).forEach((group) => {
    const fuse = group.fuse;
    const subtitle = [fuse.subtitle, group.fi ? `${graphTitle(group.fi)} · ${group.fi.neutral}` : "ohne FI"].filter(Boolean).join(" · ");
    // Low on the card, so it does not run along the N/PE taps of the Verbraucher in the same row.
    const taps = group.fi ? [] : (fuse.phases || []).map((code) => ({ code, part: 0.78 }));
    // Behind an FI the Sicherung is fed by the FI's line of its phase (drawn by `drawGraphRails`).
    const fiFeed = group.fi ? { fi: group.fi.entity_id, phases: fuse.phases || [] } : null;
    const card = { ...fuse, subtitle, badge: graphPhaseMarkup(fuse.phases || [], "graph-phase-inline"), top: true, taps, fiFeed };
    // The FI is a filter hit for its Sicherungen.
    const fiHit = words.length && !graphState.flaggedOnly && words.every((word) => `${subtitle} ${fuse.title}`.toLowerCase().includes(word));
    const pruned = fiHit ? { ...card, matched: true, forceOpen: true } : pruneGraph(card, test);
    if (!pruned) return;
    shownFuses += 1;
    const entry = { card: graphGroupByNeutral(pruned), phases: fuse.phases || [] };
    if (!group.fi) {
      // Neighbouring Sicherungen without FI form one run (so they can share a Verbraucher block).
      const last = blocks.at(-1);
      if (last && !last.fi) last.entries.push(entry); else blocks.push({ fi: null, entries: [entry] });
      return;
    }
    // Sicherungen behind the same FI share one FI block, placed where its first Sicherung is in the order.
    let block = fiBlocks.get(group.fi.entity_id);
    if (!block) {
      block = { fi: group.fi, entries: [] };
      fiBlocks.set(group.fi.entity_id, block);
      blocks.push(block);
    }
    block.entries.push(entry);
  });
  // FI box: only the phases its Sicherungen use run in, plus N (taps, spread evenly over the box). Each phase leaves
  // it as its own line down to the Sicherungen of that phase; its neutral (N4) leaves as a bar beside it.
  // All its Sicherungen count, also filtered ones.
  const fiPhases = (fi) => ["L1", "L2", "L3"].filter((code) => groups.some((group) => group.fi?.entity_id === fi.entity_id && (group.fuse.phases || []).includes(code)));
  const fiTaps = (fi) => {
    const codes = [...fiPhases(fi), "N"];
    return codes.map((code, index) => ({ code, part: (index + 1) / (codes.length + 1) }));
  };
  const fiCard = (fi) => ({ ...fi, children: [], label: `FI · ${graphFiPoles(fiPhases(fi))}`, taps: fiTaps(fi), bus: fi.neutral, fiOut: fi.entity_id });
  const trees = blocks.map((block) => (!block.fi
    ? graphFuseRowsMarkup(block.entries, (markup) => `<div class="graph-tree">${markup}</div>`)
    : `<div class="graph-tree"><div class="graph-node graph-node-top graph-fi-block">${graphCardMarkup(fiCard(block.fi), false)}<div class="graph-children graph-fi-out">${graphFuseRowsMarkup(block.entries, (markup) => `<div class="graph-child">${markup}</div>`, 1)}</div></div></div>`));
  const flagged = graphState.flaggedOnly ? flaggedTerminalIds() : null;
  const loose = (groups.find((group) => !group.fuse)?.loads || []).filter((load) =>
    (!graphState.location || (load.location || GRAPH_NO_LOCATION) === graphState.location)
    && (!flagged || loadIsFlagged(load, flagged)) && words.every((word) => loadSearchText(load).includes(word)));
  const sections = [];
  if (trees.length) sections.push(`<section class="graph-group"><h3>Sicherungen <small>${shownFuses.toLocaleString("de-DE")} · bis zum Verbraucher</small></h3>${graphRailAreaMarkup(`<div class="graph-trees">${trees.join("")}</div>`)}</section>`);
  if (loose.length) sections.push(`<section class="graph-group"><h3>Verbraucher ohne Sicherung <small>${loose.length.toLocaleString("de-DE")}</small></h3>${graphRailAreaMarkup(`<div class="graph-load-list">${loose.map(graphLoadCardMarkup).join("")}</div>`)}</section>`);
  return sections.join("");
}

// Jump to a Klemme inside the Gesamtgraph (Prüfliste): scroll it into view and highlight it. When the current view
// does not show it (folded, filtered or another mode), the folds and filters are opened and the other modes tried.
function focusGraphTerminal(terminalId, label) {
  const find = () => document.querySelector(`#graph-canvas [data-type="terminal"][data-id="${CSS.escape(terminalId)}"]`);
  let target = find();
  if (!target) {
    Object.assign(graphState, { collapsed: new Set(), filter: "", flaggedOnly: false, location: "" });
    document.querySelector("#graph-filter").value = "";
    for (const mode of [graphState.mode, "fuses", "tree"]) {
      graphState.mode = mode;
      renderGraph();
      target = find();
      if (target) break;
    }
  }
  if (!target) {
    notify(`Klemme ${label} ist im Graph nicht angeschlossen.`, true);
    return;
  }
  const card = target.closest(".graph-card") || target;
  scrollGraphTo(card);
  // The box stays marked until the next jump; the pulse only runs at the start.
  document.querySelectorAll("#graph-canvas .is-focus").forEach((node) => node.classList.remove("is-focus"));
  card.classList.add("is-focus");
}

// Centre an element of the Gesamtgraph: sideways inside the canvas, up/down on the page. The sideways jump is
// instant, because the sticky scrollbar sync writes the old position back and would stop a smooth scroll.
function scrollGraphTo(element) {
  const canvas = graphCanvasNode;
  const box = canvas.getBoundingClientRect();
  const scale = box.width / canvas.offsetWidth || 1; // body zoom: rects are zoomed, scroll offsets are not
  let rect = element.getBoundingClientRect();
  canvas.scrollLeft += (rect.left - box.left + rect.width / 2) / scale - canvas.clientWidth / 2;
  graphBarNode.scrollLeft = canvas.scrollLeft;
  rect = element.getBoundingClientRect();
  window.scrollTo({ top: window.scrollY + rect.top + rect.height / 2 - window.innerHeight / 2, behavior: "smooth" });
}

// The Prüfliste: what the conductor and Verbraucher assignment could not settle.
function renderGraphReview() {
  const items = graphState.review;
  document.querySelector("#graph-review-count").textContent = items.length.toLocaleString("de-DE");
  document.querySelector("#graph-review-items").innerHTML = items.length
    ? items.map((item) => `<button class="graph-review-item graph-review-${esc(item.kind)}" type="button" data-graph-focus="${esc(item.entity_id)}" data-graph-focus-label="${esc(item.label)}"><span class="graph-review-kind">${esc(GRAPH_REVIEW_KINDS[item.kind] || item.kind)}</span><strong>${esc(item.label)}</strong><small>${esc(item.location)}</small><span class="graph-review-message">${esc(item.message)}</span></button>`).join("")
    : '<div class="overview-empty">Keine offenen Punkte.</div>';
}

function renderGraph() {
  const words = graphState.filter.toLowerCase().split(/\s+/).filter(Boolean);
  const test = graphTest(words);
  // Totals count every Sicherung, Klemme, Verbraucher ... once, however often it appears in the graph.
  const unique = { phase: new Set(), fi: new Set(), fuse: new Set(), terminal: new Set(), channel: new Set(), load: new Set() };
  const count = (node) => {
    unique[node.type].add(node.type === "load" ? node.load_id : node.entity_id || `node:${node.id}`);
    node.children.forEach(count);
  };
  graphState.roots.forEach(count);
  const locations = [...new Set(graphChainTops().map((node) => node.location || GRAPH_NO_LOCATION))]
    .sort((a, b) => (a === GRAPH_NO_LOCATION) - (b === GRAPH_NO_LOCATION) || naturalCollator.compare(a, b));
  if (graphState.location && !locations.includes(graphState.location)) graphState.location = "";
  const select = document.querySelector("#graph-location");
  select.innerHTML = `<option value="">Alle Orte</option>${locations.map((name) => `<option value="${esc(name)}">${esc(name)}</option>`).join("")}`;
  select.value = graphState.location;
  graphNode.querySelectorAll("[data-graph-depth]").forEach((button) => button.setAttribute("aria-pressed", String(Number(button.dataset.graphDepth) === graphState.depth)));
  graphNode.querySelectorAll("[data-graph-mode]").forEach((button) => button.setAttribute("aria-pressed", String(button.dataset.graphMode === graphState.mode)));
  graphNode.querySelector(".graph-levels:not(.graph-modes)").hidden = graphState.mode !== "tree";
  document.querySelector("#graph-flagged").checked = graphState.flaggedOnly;
  let markup;
  // Busbar toggles only belong to the Sicherungen view.
  const railToggles = document.querySelector("#graph-rail-toggles");
  railToggles.hidden = graphState.mode !== "fuses";
  railToggles.querySelectorAll("[data-graph-rail]").forEach((box) => { box.checked = graphState.rails[box.dataset.graphRail]; });
  graphState.loadFeeds = graphState.mode === "fuses" ? new Map(collectGraphLoads().map((load) => [load.key, load])) : null;
  if (graphState.mode === "fuses") {
    markup = graphFusesMarkup(words);
  } else if (graphState.mode === "loads") {
    markup = graphLoadsMarkup(words);
  } else {
    // Conductors first, then chains that hang on no conductor yet, grouped by location.
    const conductors = graphConductorTrees(test);
    const loose = locations.filter((name) => !graphState.location || name === graphState.location).map((name) => {
      const trees = graphState.roots.filter((root) => root.type !== "phase" && (root.location || GRAPH_NO_LOCATION) === name).map((root) => pruneGraph(root, test)).filter(Boolean);
      return trees.length ? `<section class="graph-group"><h3>${esc(name)} <small>ohne Leiter</small></h3>${graphTreesMarkup(trees)}</section>` : "";
    }).join("");
    markup = graphConductorMarkup(conductors) + loose;
  }
  document.querySelector("#graph-canvas").innerHTML = markup || `<div class="overview-empty">${test ? "Keine Treffer im Graph." : "Noch keine Verbindungen vorhanden."}</div>`;
  syncGraphScrollbar();
  // Taps are measured from the layout, so they are redrawn whenever a rail area changes size (fonts, wrapping).
  graphRailObserver.disconnect();
  document.querySelectorAll("#graph-canvas .graph-rail-area").forEach((area) => graphRailObserver.observe(area));
  requestAnimationFrame(drawGraphRails);
  // A new mode or filter starts at the left, where the L busbars are.
  if (graphState.lastMode !== graphState.mode) document.querySelector("#graph-canvas").scrollLeft = 0;
  graphState.lastMode = graphState.mode;
  renderGraphReview();
  const hits = test && graphState.mode === "tree" ? (markup.match(/is-match/g) || []).length : 0;
  document.querySelector("#graph-summary").textContent = test && graphState.mode === "tree"
    ? `${hits} Treffer`
    : [["fi", "FI"], ["fuse", "Sicherungen"], ["terminal", "Klemmen"], ["channel", "Kanäle"], ["load", "Verbraucher"]]
      .map(([type, label]) => `${unique[type].size.toLocaleString("de-DE")} ${label}`).join(" · ");
}

async function openGraph(trackHistory = true) {
  try {
    const graph = await request("api/graph");
    Object.assign(graphState, { roots: graph.roots, review: graph.review, loads: graph.loads });
    if (!graphState.ready) {
      applyGraphDepth(graphState.depth);
      graphState.ready = true;
    }
    if (trackHistory) pushViewHistory({ view: "graph" });
    currentPageState = { view: "graph" };
    currentDetail = null;
    searchViewNode.hidden = true;
    welcomeNode.hidden = true;
    settingsNode.hidden = true;
    detailNode.hidden = true;
    overviewNode.hidden = true;
    graphNode.hidden = false;
    document.querySelector("#crumb-current").textContent = "GESAMTGRAPH";
    document.querySelector("#graph-filter").value = graphState.filter;
    renderGraph();
  } catch (error) {
    notify(error.message, true);
  }
}

function openLocationTerminals(location) {
  selectedTerminalLocation = location;
  openOverview("terminals");
}

function linkButton(type, id, title, subtitle = "", inlineAccessory = "", modifierClass = "") {
  return `<button class="relation-link${modifierClass ? ` ${esc(modifierClass)}` : ""}" type="button" data-type="${esc(type)}" data-id="${esc(id)}">
    <span>${esc(title)}${inlineAccessory}</span>${subtitle ? `<small>${esc(subtitle)}</small>` : ""}<b aria-hidden="true">›</b>
  </button>`;
}

function node(type, title, subtitle = "", modifierClass = "") {
  return `<div class="path-node${modifierClass ? ` ${esc(modifierClass)}` : ""}"><span class="path-node-type">${esc(type)}</span><strong>${esc(title)}</strong>${subtitle ? `<small>${esc(subtitle)}</small>` : ""}</div>`;
}

function pathNodeButton(type, id, label, title, subtitle = "", modifierClass = "") {
  return `<button class="path-node path-node-button${modifierClass ? ` ${esc(modifierClass)}` : ""}" type="button" data-type="${esc(type)}" data-id="${esc(id)}">
    <span class="path-node-type">${esc(label)}</span><strong>${esc(title)}</strong>${subtitle ? `<small>${esc(subtitle)}</small>` : ""}<b aria-hidden="true">›</b>
  </button>`;
}

function fiPathNode(fiId, fiNumber = "", fiLabel = "", location = "") {
  if (!fiId && !fiNumber && !fiLabel) return "";
  const title = fiNumber ? `FI ${fiNumber}` : fiLabel || "FI";
  const subtitle = [fiNumber && fiLabel ? fiLabel : "", location].filter(Boolean).join(" · ");
  return fiId
    ? pathNodeButton("fi", fiId, "FI", title, subtitle)
    : node("FI", title, subtitle);
}

function fusePathNode(fuseId, label, rating = "", location = "", fi = {}) {
  const subtitle = [location, rating].filter(Boolean).join(" · ");
  const fiNode = fiPathNode(fi.id, fi.number, fi.label, fi.location);
  return `<div class="fuse-path-stack">${fiNode}${fiNode ? pathArrow("↓", "fuse-path-edge") : ""}${pathNodeButton("fuse", fuseId, "SICHERUNG", label, subtitle, "fuse-channel-path")}</div>`;
}

function etsChannelLabel(channel) {
  return channel.physical_channel
    ? `Kanal ${channel.physical_channel}`
    : `ETS-Objekt ${channel.object_number}`;
}

function firstSendGroupName(channel) {
  if (channel.send_group_name) return channel.send_group_name;
  const objects = channel.objects || [channel];
  const sendGroups = objects.flatMap((object) => object.groups || [])
    .filter((group) => String(group.direction || "").split("/").includes("S") && group.name?.trim())
    .sort((left, right) => String(left.address || "").localeCompare(String(right.address || ""), undefined, { numeric: true, sensitivity: "base" }));
  return sendGroups[0]?.name || "";
}

function isSetGroup(group) {
  return String(group.direction || "").split("/").includes("S");
}

function orderedGroups(groups) {
  return [...groups].sort((left, right) => Number(isSetGroup(right)) - Number(isSetGroup(left)));
}

// Verbraucher at the end of a Klemmenpfad: name, location and its L, N and PE Klemmen (each opens the Klemme).
// `nodeId`: the Verbraucher link (load node) shown here; in review mode its card gets an ✕ that cuts it.
function loadPathNodes(loads, nodeId = null, linkName = true) {
  if (!loads.length) return "";
  const cut = reviewModeEnabled && nodeId
    ? `<button class="load-disconnect" type="button" data-disconnect-load="${esc(nodeId)}" title="Verbraucher von dieser Klemme trennen" aria-label="Verbraucher trennen">✕</button>`
    : "";
  const feedMarkup = (load, role) => {
    const feeds = load.feeds.filter((feed) => feed.role === role);
    // In review mode a missing feed is a button that assigns a Klemme to it.
    if (!feeds.length) {
      // A Verbraucher without any PE connection: nothing missing.
      if (role === "PE" && load.no_pe) return '<span class="load-feed load-feed-none">ohne PE</span>';
      return reviewModeEnabled
        ? `<button class="load-feed load-feed-missing load-feed-assign" type="button" data-feed-load="${esc(load.id)}" data-feed-role="${role}" title="${role}-Klemme zuweisen">${role} fehlt</button>`
        : `<span class="load-feed load-feed-missing">${role} fehlt</span>`;
    }
    // Connected directly to PE: no Klemme to open; in review mode the ✕ cuts that link.
    // Directly on a Sicherung (its L): the Sicherung opens its page; in review mode the ✕ cuts that link too.
    const direct = (feed) => feed.fuse_id
      ? `<span class="load-feed load-feed-direct">${graphPhaseMarkup([feed.wire_phase], "graph-phase-inline")}<button class="load-feed-link" type="button" data-type="fuse" data-id="${esc(feed.fuse_id)}" title="${esc(feed.location)}">${esc(feed.label)}</button>${reviewOnly(`<button class="load-feed-cut" type="button" data-disconnect-load="${esc(feed.node_id)}" data-confirm="Verbraucher von der Sicherung ${esc(feed.label)} trennen?" title="Von der Sicherung trennen" aria-label="Von der Sicherung trennen">✕</button>`)}</span>`
      : `<span class="load-feed load-feed-direct">${graphPhaseMarkup([feed.wire_phase], "graph-phase-inline")}<span>direkt</span>${reviewOnly(`<button class="load-feed-cut" type="button" data-disconnect-load="${esc(feed.node_id)}" data-confirm="Direkte ${esc(feed.wire_phase)}-Verbindung dieses Verbrauchers trennen?" title="Direkte ${esc(feed.wire_phase)}-Verbindung trennen" aria-label="Direkte Verbindung trennen">✕</button>`)}</span>`;
    // N and PE of this one Verbraucher (review mode): ⇄ puts it on another Klemme or directly on a conductor, ✕
    // cuts it; other Verbraucher on the same Klemme keep theirs.
    const own = (feed) => (feed.node_id && role !== "L" ? reviewOnly(`<button class="load-feed-change" type="button" data-feed-load="${esc(load.id)}" data-feed-role="${role}" data-replace-node="${esc(feed.node_id)}" title="${role} nur dieses Verbrauchers ändern" aria-label="${role} ändern">⇄</button><button class="load-feed-cut" type="button" data-disconnect-load="${esc(feed.node_id)}" data-confirm="${esc(`${role} ${formatTerminalLabel(feed.label)} von diesem Verbraucher trennen? Andere Verbraucher an der Klemme bleiben angeschlossen.`)}" title="${role} von diesem Verbraucher trennen" aria-label="${role} trennen">✕</button>`) : "");
    const chip = (feed) => `<button class="load-feed" type="button" data-type="terminal" data-id="${esc(feed.terminal_id)}" title="${esc(feed.location)}">${graphPhaseMarkup([feed.wire_phase || `${role}?`], "graph-phase-inline")}<span>${esc(formatTerminalLabel(feed.label))}</span></button>`;
    return feeds.map((feed) => (feed.direct ? direct(feed) : own(feed) ? `<span class="load-feed-group">${chip(feed)}${own(feed)}</span>` : chip(feed))).join("");
  };
  // ✎ opens the Verbraucher dialog (rename, description, delete).
  const edit = (load) => reviewOnly(`<button class="load-disconnect load-edit" type="button" data-edit-load="${esc(load.id)}" title="Verbraucher bearbeiten oder löschen" aria-label="Verbraucher bearbeiten">✎</button>`);
  const subtitle = (load) => [load.location, load.description].filter(Boolean).join(" · ");
  // The name opens the Verbraucher's own page (not on that page itself).
  const name = (load) => (linkName
    ? `<button class="load-path-name" type="button" data-type="load" data-id="${esc(load.id)}" title="Verbraucher öffnen"><strong>${esc(load.name)}</strong></button>`
    : `<strong>${esc(load.name)}</strong>`);
  const nodes = loads.map((load) => `<div class="path-node load-path-node">${cut}${edit(load)}<span class="path-node-type">VERBRAUCHER</span>${name(load)}${subtitle(load) ? `<small>${esc(subtitle(load))}</small>` : ""}<span class="load-feeds">${["L", "N", "PE"].map((role) => feedMarkup(load, role)).join("")}</span></div>`).join("");
  return loads.length > 1 ? `<div class="load-path-stack">${nodes}</div>` : nodes;
}

function terminalPathNode(title, subtitle = "", wireColor = "", id = null, used = true) {
  const interactive = id !== null;
  const tag = interactive ? "button" : "div";
  const attributes = interactive ? ` type="button" data-type="terminal" data-id="${esc(id)}"` : "";
  const classes = `path-node terminal-path-node${interactive ? " terminal-path-link" : ""}`;
  const terminalParts = wireColor ? terminalLabelParts(title) : null;
  const displayTitle = terminalParts ? terminalParts.number : title;
  return `<${tag} class="${classes}"${attributes}><span class="path-node-type">KLEMME</span><span class="terminal-number-line"><strong class="${terminalUsageClass(used)}">${esc(displayTitle)}</strong>${terminalParts ? terminalWireMarkup(wireColor, terminalParts.position) : ""}</span>${subtitle ? `<small>${esc(subtitle)}</small>` : ""}</${tag}>`;
}

function terminalLabelParts(value, position = "") {
  const label = String(value || "").replace(/^Klemme\s+/i, "");
  const [number, embeddedPosition = ""] = label.split("/", 2);
  return { number, position: String(position || embeddedPosition).trim() };
}

function formatTerminalLabel(value, position = "") {
  const [terminalNumber, embeddedPosition = ""] = String(value || "").split("/", 2);
  const terminalPosition = String(position || embeddedPosition).trim();
  return terminalPosition ? `${terminalNumber}/${terminalPosition}` : terminalNumber;
}

function chips(items, type, key, labelKey, subKey = "") {
  if (!items?.length) return '<span class="muted-inline">Keine Zuordnung</span>';
  const orderedItems = type === "group" ? orderedGroups(items) : items;
  return orderedItems.map((item) => linkButton(
    type,
    item[key],
    item[labelKey],
    subKey ? item[subKey] : "",
    "",
    isSetGroup(item) ? "group-set-link" : "",
  )).join("");
}

function wireColorChip(code, showCode = true) {
  const colors = String(code || "").split("/").map((part) => WIRE_COLORS.find((item) => item.code === part)).filter(Boolean);
  return colors.length
    ? `<span class="wire-chip">${colors.map((color) => `<i class="wire-swatch wire-swatch-${color.css}" title="${color.name}"></i>`).join("")}${showCode ? colors.map((color) => color.code).join("/") : ""}</span>`
    : '<span class="wire-chip wire-chip-none">Farbe offen</span>';
}

function terminalWireMarkup(color, position = "") {
  return `<span class="terminal-wire-position">${position ? `<span class="terminal-position-label">${esc(position)}</span>` : ""}${wireColorChip(color, false)}</span>`;
}

// Klemme picker (location, number, position, wire colour) of a form; `side` is the field prefix ("path").
function setTerminalPickerColor(side, code) {
  const form = document.querySelector(`[data-terminal-colors="${side}"]`).closest("form");
  form.elements[`${side}_wire_color`].value = code;
  form.querySelectorAll(`[data-terminal-colors="${side}"] [data-color]`).forEach((button) => {
    const selected = button.dataset.color === code;
    button.setAttribute("aria-checked", String(selected));
    button.classList.toggle("selected", selected);
  });
}

function initializeTerminalColorPickers() {
  document.querySelectorAll("[data-terminal-colors]").forEach((picker) => {
    const side = picker.dataset.terminalColors;
    picker.innerHTML = `<button class="wire-color-option" type="button" role="radio" aria-checked="false" data-color="" title="Keine Farbe"><i class="wire-swatch wire-swatch-none"></i><span>–</span></button>${WIRE_COLORS.map((color) => `<button class="wire-color-option" type="button" role="radio" aria-checked="false" data-color="${color.code}" title="${color.name}"><i class="wire-swatch wire-swatch-${color.css}"></i><span>${color.code}</span></button>`).join("")}`;
    picker.addEventListener("click", (event) => {
      const button = event.target.closest("[data-color]");
      if (button) setTerminalPickerColor(side, button.dataset.color);
    });
  });
}

const terminalLookupTimers = {};

// Every suggestion popup in every dialog: a click on a suggestion keeps the focus in its field (so the choice
// still works), and leaving the field closes the popup.
const SUGGESTION_POPUPS = ".picker-options, .autocomplete-popup, [data-terminal-suggestions]";
document.addEventListener("mousedown", (event) => {
  if (event.target.closest(SUGGESTION_POPUPS)) event.preventDefault();
});
document.addEventListener("focusout", (event) => {
  const field = event.target.closest?.(".picker-field, .autocomplete-field");
  if (!field || field.contains(event.relatedTarget)) return;
  field.querySelectorAll(SUGGESTION_POPUPS).forEach((popup) => { popup.hidden = true; });
  field.classList.remove("is-open");
  event.target.setAttribute?.("aria-expanded", "false");
});

// Shows a suggestion popup only with content and while its field is the active one; otherwise it stays closed,
// so it never covers the next fields. Returns whether it is open.
function showSuggestions(popup, input, markup) {
  const open = Boolean(markup) && document.activeElement === input;
  popup.innerHTML = open ? markup : "";
  popup.hidden = !open;
  if (open) popup.classList.toggle("opens-up", window.innerHeight - input.getBoundingClientRect().bottom < 190);
  input.parentElement.classList.toggle("is-open", open);
  input.setAttribute("aria-expanded", String(open));
  return open;
}

async function loadTerminalPickerOptions(side) {
  const options = document.querySelector(`[data-terminal-options="${side}"]`);
  const form = options.closest("form");
  const numberInput = form.elements[`${side}_terminal_number`];
  const number = numberInput.value.trim();
  const location = form.elements[`${side}_location`].value.trim();
  if (!number) {
    showSuggestions(options, numberInput, "");
    return;
  }
  try {
    const query = new URLSearchParams({ number, location });
    const terminals = await request(`api/terminals?${query}`);
    if (number !== numberInput.value.trim()) return;
    // No hits: no popup (a new Klemme is simply typed in).
    showSuggestions(options, numberInput, terminals.map((terminal) => {
      const terminalParts = terminalLabelParts(terminal.label, terminal.position);
      const title = terminal.wire_color ? terminalParts.number : formatTerminalLabel(terminal.label, terminal.position);
      const wireLabel = terminal.wire_color ? terminalWireMarkup(terminal.wire_color, terminalParts.position) : "";
      return `<button class="picker-option" type="button" data-terminal-choice="${side}" data-number="${esc(terminal.terminal_number)}" data-position="${esc(terminal.position)}" data-color="${esc(terminal.wire_color)}" data-location="${esc(terminal.location)}"><strong class="${terminalUsageClass(terminal.used)}">${esc(title)}</strong>${wireLabel}<small>${esc(terminal.location)}</small></button>`;
    }).join(""));
  } catch (error) {
    showSuggestions(options, numberInput, `<span class="picker-option-note">Klemmen konnten nicht geladen werden: ${esc(error.message)}</span>`);
  }
}

function scheduleTerminalPickerOptions(side) {
  clearTimeout(terminalLookupTimers[side]);
  terminalLookupTimers[side] = setTimeout(() => loadTerminalPickerOptions(side), 180);
}

// Path-add dialog: adds a Klemme or a Gerät-Kanal at the end of a Klemme's path or as a branch (another follower).
const pathAddDialog = document.querySelector("#path-add-dialog");
const pathAddForm = document.querySelector("#path-add-form");
let channelLookupTimer;
const NODE_TYPE_NAMES = { fuse: "Sicherung", terminal: "Klemme", channel: "Kanal", load: "Verbraucher" };

function setPathAddKind(kind) {
  pathAddForm.querySelectorAll("[data-path-panel]").forEach((panel) => { panel.hidden = panel.dataset.pathPanel !== kind; });
  pathAddForm.elements.path_location.required = kind === "terminal";
  pathAddForm.elements.path_terminal_number.required = kind === "terminal";
  pathAddForm.querySelectorAll("[data-terminal-options], [data-path-channel-options], [data-path-load-options]").forEach((options) => { options.hidden = true; });
}

// `fuseId`: a Klemme or a Verbraucher wired directly to that Sicherung (Sicherung page; no Gerät-Kanal there).
// `feedLoadId`/`feedRole`: a Klemme as the missing L/N/PE feed of
// that Verbraucher (its card on the Klemme page); the wire colour starts at the role's colour.
const FEED_COLORS = { N: "bl", PE: "grge", L: "" };

// Choices for an N without Klemme: the main N and every FI's neutral (first entry: use a Klemme instead).
async function loadNeutralOptions(select) {
  select.length = 1;
  try {
    const neutrals = await request("api/neutrals");
    neutrals.forEach((item) => select.add(new Option([item.label, item.subtitle].filter(Boolean).join(" · "), item.value)));
  } catch (error) {
    notify(error.message, true);
  }
}

// `insertNodeId`: a Klemme (new or existing) goes into the wire above that node.
// `replaceNodeId`: the feed of that Verbraucher being changed (it is cut once the new one is connected).
function openPathAddDialog({ terminalId = "", nodeId = "", fuseId = "", nodeType = "", label = "", location = "", branch = "", feedLoadId = "", feedRole = "", insertNodeId = "", replaceNodeId = "" }) {
  pathAddForm.reset();
  pathAddForm.elements.replace_node_id.value = replaceNodeId;
  pathAddForm.elements.terminal_id.value = terminalId;
  pathAddForm.elements.node_id.value = nodeId;
  pathAddForm.elements.fuse_id.value = fuseId;
  pathAddForm.elements.feed_load_id.value = feedLoadId;
  pathAddForm.elements.feed_role.value = feedRole;
  pathAddForm.elements.insert_node_id.value = insertNodeId;
  pathAddForm.elements.path_location.value = localStorage.getItem(lastLocationKey) || location || "";
  setTerminalPickerColor("path", FEED_COLORS[feedRole] || "");
  // At the path end and below a Klemme: Klemme, Gerät-Kanal or Verbraucher; below a Kanal: Klemme or Verbraucher.
  const kindsAllowed = !feedRole && !insertNodeId && (fuseId || nodeType !== "fuse" && (!nodeId || nodeType === "terminal" || nodeType === "channel"));
  pathAddForm.querySelector(".path-add-kinds").hidden = !kindsAllowed;
  pathAddForm.querySelector('input[name="kind"][value="channel"]').closest("label").hidden = nodeType === "channel" || Boolean(fuseId);
  setPathAddKind("terminal");
  // N and PE can do without a Klemme: an empty number connects the Verbraucher directly to the conductor (PE, or
  // the neutral chosen below: N or an FI's, never guessed).
  if (feedRole === "PE" || feedRole === "N") {
    pathAddForm.elements.path_location.required = false;
    pathAddForm.elements.path_terminal_number.required = false;
  }
  const neutralField = pathAddForm.querySelector(".feed-neutral-field");
  neutralField.hidden = feedRole !== "N";
  if (feedRole === "N") loadNeutralOptions(neutralField.querySelector("select"));
  document.querySelector("#path-add-title").textContent = feedRole ? `${feedRole}-Klemme ${replaceNodeId ? "ändern" : "zuweisen"}`
    : insertNodeId ? "Klemme einfügen" : fuseId ? "An Sicherung anschließen" : branch ? "Abzweig hinzufügen" : "Am Pfadende hinzufügen";
  document.querySelector("#path-add-from").textContent = feedRole
    ? `Für Verbraucher ${label}${location ? ` · ${location}` : ""}${feedRole === "PE" ? " – ohne Klemmen-Nummer direkt an PE" : feedRole === "N" ? " – ohne Klemme: unten den Neutralleiter wählen" : ""}`
    : insertNodeId
    ? `In die Verbindung über ${NODE_TYPE_NAMES[nodeType] || "dem Eintrag"} – neue oder vorhandene Klemme, in jedem Raum`
    : fuseId
    ? `An Sicherung ${label}${location ? ` · ${location}` : ""}`
    : nodeId
      ? `Weiterer Abzweig unter ${NODE_TYPE_NAMES[nodeType] || "Knoten"}`
      : `${branch ? "Abzweig an" : "Nach"} Klemme ${label}${location ? ` · ${location}` : ""}`;
  document.querySelector("#path-add-error").textContent = "";
  pathAddDialog.showModal();
  pathAddForm.elements.path_terminal_number.focus();
}

// Forget a picked channel (hidden ids) once the user types something else.
function clearPickedChannel() {
  pathAddForm.elements.device_id.value = "";
  pathAddForm.elements.object_number.value = "";
}

async function loadChannelOptions() {
  const options = document.querySelector("[data-path-channel-options]");
  const input = pathAddForm.elements.channel_search;
  const query = input.value.trim();
  if (!query) {
    showSuggestions(options, input, "");
    return;
  }
  try {
    const channels = await request(`api/channels?${new URLSearchParams({ q: query })}`);
    // Without matches the box stays closed so it does not cover the form
    showSuggestions(options, input, channels.map((channel) => `<button class="picker-option" type="button" data-path-channel data-device-id="${esc(channel.device_id)}" data-object-number="${esc(channel.object_number)}" data-title="${esc(channel.title)}"><strong>${esc(channel.title)}</strong><small>${esc(channel.subtitle)}</small></button>`).join(""));
  } catch (error) {
    showSuggestions(options, input, `<span class="picker-option-note">Kanäle konnten nicht geladen werden: ${esc(error.message)}</span>`);
  }
}

// Verbraucher search of the path-add dialog (same look as the channel search).
async function loadLoadOptions() {
  const options = document.querySelector("[data-path-load-options]");
  const input = pathAddForm.elements.load_search;
  const query = input.value.trim();
  if (!query) {
    showSuggestions(options, input, "");
    return;
  }
  try {
    const loads = await request(`api/loads?${new URLSearchParams({ q: query })}`);
    showSuggestions(options, input, loads.map((load) => `<button class="picker-option" type="button" data-path-load data-load-id="${esc(load.id)}" data-title="${esc(load.name)}"><strong>${esc(load.name)}</strong><small>${esc([load.location, load.terminal_number ? `Kabel ${load.terminal_number}` : ""].filter(Boolean).join(" · "))}</small></button>`).join(""));
  } catch (error) {
    showSuggestions(options, input, `<span class="picker-option-note">Verbraucher konnten nicht geladen werden: ${esc(error.message)}</span>`);
  }
}

const pathAddMessages = {
  terminal: (data) => (data.inserted && data.conductor_link
    ? `Die Klemme ist an ${data.conductor} und bleibt dort: sie ist jetzt ${data.conductor === "PE" ? "PE" : "N"} dieses Geräts und Verbrauchers.`
    : data.inserted ? (data.created ? "Neue Klemme angelegt und eingefügt." : "Klemme eingefügt.") : data.conductor_link ? "Klemme mit dem Kanal verbunden. Sie bleibt an ihrem Leiter (N/PE); der Kanal steht auf ihrer Klemmen-Seite."
    : data.created ? "Neue Klemme angelegt und verbunden." : data.already_exists ? "Diese Verbindung besteht bereits." : "Klemme verbunden."),
  channel: (data) => (data.created ? "Neuer Kanal angelegt und verbunden." : "Kanal verbunden."),
  load: (data) => (data.created ? "Neuer Verbraucher angelegt und verbunden." : data.already_exists ? "Dieser Verbraucher ist bereits verbunden." : "Verbraucher verbunden."),
};

function renderChannelFuseGraph(terminals) {
  const sourcesByTerminal = new Map();
  for (const terminal of terminals) {
    const terminalNumber = String(terminal.terminal_number || terminal.terminal || terminal.label || "").split("/", 1)[0];
    const groupKey = JSON.stringify([terminal.location || "", terminalNumber]);
    let source = sourcesByTerminal.get(groupKey);
    if (!source) {
      source = { terminalNumber, location: terminal.location || "", members: [], fuseById: new Map() };
      sourcesByTerminal.set(groupKey, source);
    }
    source.members.push(terminal);
    if (terminal.fuse_id && !source.fuseById.has(String(terminal.fuse_id))) {
      source.fuseById.set(String(terminal.fuse_id), terminal);
    }
    for (const fuse of terminal.path_fuses || []) {
      if (source.fuseById.has(String(fuse.id))) continue;
      source.fuseById.set(String(fuse.id), {
        fuse_id: fuse.id,
        fuse: fuse.label,
        fuse_rating: fuse.rating,
        fi_id: fuse.fi_id,
        fi_number: fuse.fi_number,
        fi_label: fuse.fi_label,
        fi_location: fuse.fi_location,
        location: fuse.location,
        in_path: true,
        path_terminal_label: fuse.terminal_label,
      });
    }
  }
  const sourceList = [...sourcesByTerminal.values()];
  const sourceMarkup = sourceList.map((source, index) => {
    const memberMarkup = source.members.map((terminal) => {
      const position = String(terminal.position || String(terminal.terminal || terminal.label || "").split("/")[1] || "").trim();
      const number = String(terminal.terminal_number || terminal.terminal || terminal.label || "").split("/", 1)[0];
      const removeButtons = reviewOnly((terminal.assignment_ids || []).map((assignmentId) =>
        `<button class="remove-assignment" type="button" data-delete-assignment="${esc(assignmentId)}" title="Zuordnung entfernen" aria-label="Klemmen-Zuordnung entfernen">×</button>`
      ).join(""));
      const terminalAriaLabel = `Klemme ${number}${position ? ` ${position}` : ""}${terminal.wire_color ? ` ${terminal.wire_color}` : ""}`;
      return `<div class="channel-graph-terminal-member"><button class="channel-graph-terminal-link" type="button" data-type="terminal" data-id="${esc(terminal.terminal_id)}" aria-label="${esc(terminalAriaLabel)}">${source.members.length === 1 ? `<strong class="${terminalUsageClass(terminal.used)}">Klemme ${esc(number)}</strong>` : ""}${terminalWireMarkup(terminal.wire_color, position)}</button>${removeButtons}</div>`;
    }).join("");
    const description = [...new Set(source.members.map((terminal) => terminal.description).filter(Boolean))].join(" · ");
    const groupTitle = source.members.length > 1 ? `<div class="channel-graph-source-heading"><strong class="channel-graph-source-title">Klemme ${esc(source.terminalNumber)}</strong><div class="channel-graph-terminal-members">${memberMarkup}</div></div>` : "";
    return `<div class="path-node terminal-path-node channel-graph-source" data-graph-source="${index}" style="grid-row:${index + 1}"><span class="path-node-type">KLEMME</span>${groupTitle}${source.members.length === 1 ? `<div class="channel-graph-terminal-members">${memberMarkup}</div>` : ""}<small>${esc([source.location, description].filter(Boolean).join(" · "))}</small></div>`;
  }).join("");
  const fuseRuns = [];
  const lastFuseRun = new Map();
  sourceList.forEach((source, sourceIndex) => {
    const connectedFuseIds = new Set(source.fuseById.keys());
    for (const [key, previousRun] of lastFuseRun) {
      if (previousRun.lastSourceIndex < sourceIndex - 1 || !connectedFuseIds.has(key)) lastFuseRun.delete(key);
    }
    for (const [fuseKey, terminal] of source.fuseById) {
      const existing = lastFuseRun.get(fuseKey);
      if (existing && existing.lastSourceIndex === sourceIndex - 1) {
        existing.sourceIndexes.push(sourceIndex);
        existing.lastSourceIndex = sourceIndex;
        continue;
      }
      const run = { ...terminal, sourceIndexes: [sourceIndex], lastSourceIndex: sourceIndex };
      fuseRuns.push(run);
      lastFuseRun.set(fuseKey, run);
    }
  });
  const fuseMarkup = fuseRuns.map((fuse, index) => {
    const sourceIndexes = fuse.sourceIndexes.map(String);
    const firstSourceIndex = fuse.sourceIndexes[0];
    const lastSourceIndex = fuse.sourceIndexes.at(-1);
    const rating = fuse.fuse_rating ? `<span class="overview-fuse-rating ${String(fuse.fuse_rating).replace(/\s/g, "") === "10A" ? "is-10a" : String(fuse.fuse_rating).replace(/\s/g, "") === "16A" ? "is-16a" : ""}">${esc(fuse.fuse_rating)}</span>` : "";
    const pathLabel = fuse.in_path
      ? `Sicherung im Pfad · Klemme ${formatTerminalLabel(fuse.path_terminal_label)} · ${fuse.location || ""}`
      : fuse.location || "";
    const fiNode = fiPathNode(fuse.fi_id, fuse.fi_number, fuse.fi_label, fuse.fi_location);
    return `<div class="channel-graph-fuse-stack" data-graph-target="${index}" data-connected-sources="${sourceIndexes.join(" ")}" style="grid-row:${firstSourceIndex + 1} / ${lastSourceIndex + 2}">${fiNode}${fiNode ? pathArrow("↓", "fuse-path-edge") : ""}<button class="path-node path-node-button fuse-channel-path channel-graph-fuse" type="button" data-type="fuse" data-id="${esc(fuse.fuse_id)}"><span class="path-node-type">${fuse.in_path ? "SICHERUNG IM PFAD" : "SICHERUNG"}</span><span class="channel-graph-fuse-title"><strong>${esc(fuse.fuse)}</strong>${rating}</span><small>${esc(pathLabel)}</small><b aria-hidden="true">›</b></button></div>`;
  }).join("");
  const unassigned = sourceList.flatMap((source, index) => !source.fuseById.size
    ? [`<div class="path-node channel-graph-unassigned" style="grid-row:${index + 1}"><span class="path-node-type">SICHERUNG</span><strong>Nicht zugeordnet</strong></div>`]
    : []).join("");
  return `<div class="fuse-node-graph channel-fuse-graph"><div class="fuse-graph-canvas"><svg class="fuse-graph-wires" aria-hidden="true"></svg><div class="fuse-graph-sources">${sourceMarkup}</div><div class="fuse-graph-targets">${fuseMarkup}${unassigned}</div></div></div>`;
}

function groupDeviceChannels(channels) {
  const grouped = new Map();
  for (const channel of channels) {
    const key = channel.physical_channel
      ? `physical:${channel.device_id}:${channel.physical_channel}`
      : `object:${channel.device_id}:${channel.object_number}`;
    let group = grouped.get(key);
    if (!group) {
      group = { ...channel, objects: [], terminals: [], terminalById: new Map() };
      grouped.set(key, group);
    }
    if (!group.reviewed_at && channel.reviewed_at) group.reviewed_at = channel.reviewed_at;
    group.objects.push(channel);
    for (const terminal of channel.terminals || []) {
      let existing = group.terminalById.get(terminal.terminal_id);
      if (!existing) {
        existing = {
          ...terminal,
          assignment_ids: terminal.assignment_id ? [terminal.assignment_id] : [],
        };
        group.terminalById.set(terminal.terminal_id, existing);
        group.terminals.push(existing);
      } else if (terminal.assignment_id && !existing.assignment_ids.includes(terminal.assignment_id)) {
        existing.assignment_ids.push(terminal.assignment_id);
        existing.assignment_id ||= terminal.assignment_id;
      }
    }
  }
  return [...grouped.values()].map((group) => {
    delete group.terminalById;
    return group;
  });
}

// Button that opens the dialog of a manual channel (rename, description, delete).
function manualChannelEditButton(channel, className, label) {
  return `<button class="${className}" type="button" data-edit-manual-channel data-device-id="${esc(channel.device_id)}" data-object-number="${esc(channel.object_number)}" data-physical-channel="${esc(channel.physical_channel)}" data-name="${esc(channel.name)}">${label}</button>`;
}

function renderChannel(channel, expandObjectsByDefault = false) {
  const manualChannel = Boolean(channel.manual_channel);
  const terminals = channel.terminals || [];
  const objects = channel.objects || [channel];
  const objectRows = objects.map((object) => {
    const objectName = object.name || object.function || object.data_type || "";
    const objectGroups = object.groups || [];
    const groupLinks = objectGroups.length
      ? `<div class="link-list">${chips(objectGroups, "group", "address", "address", "name")}</div>`
      : '<span class="muted-inline">Keine Gruppenadressen</span>';
    return `<div class="ets-object-entry">${linkButton("channel", `${object.device_id}:${object.object_number}`, `ETS-Objekt ${object.object_number}`, objectName)}${groupLinks}</div>`;
  }).join("");
  const channelSubtitle = manualChannel
    ? channel.name || "Manuell hinzugefügt"
    : [firstSendGroupName(channel), `${objects.length} ETS-Objekt${objects.length === 1 ? "" : "e"}`].filter(Boolean).join(" · ");
  const channelTitle = manualChannel ? channel.physical_channel || channel.name || "Kanal" : channel.physical_channel ? `Kanal ${channel.physical_channel}` : channel.name || channel.function || "ETS-Objekt";
  const reviewControl = expandObjectsByDefault ? "" : reviewControlMarkup(
    "channel",
    `${channel.device_id}:${channel.object_number}`,
    channel.reviewed_at,
    "channel-review-control",
  );
  const channelTree = manualChannel
    ? `<div class="channel-tree" aria-label="Kanal-Zuordnungen"><section class="channel-tree-branch"><span class="column-label">KLEMMEN</span>${terminals.length ? renderChannelFuseGraph(terminals) : '<span class="muted-inline">Noch keine Klemmen zugeordnet</span>'}</section></div>`
    : `<div class="channel-tree" aria-label="Kanal-Zuordnungen">
      <section class="channel-tree-branch"><span class="column-label">KLEMME · SICHERUNG</span>${terminals.length ? renderChannelFuseGraph(terminals) : '<span class="muted-inline">Noch keine Klemmen zugeordnet</span>'}</section>
      <section class="channel-tree-branch"><button class="ets-object-toggle" type="button" data-toggle-ets-objects aria-expanded="${expandObjectsByDefault}" aria-label="ETS-Objekte ${expandObjectsByDefault ? "ausblenden" : "anzeigen"}"><span class="ets-object-toggle-mark" aria-hidden="true">${expandObjectsByDefault ? "−" : "+"}</span><span class="column-label">ETS-OBJEKTE</span></button><div class="ets-object-list"${expandObjectsByDefault ? "" : " hidden"}>${objectRows}</div></section>
    </div>`;
  return `<article class="channel-row">
    <div class="channel-head"><button class="channel-title" type="button" data-type="channel" data-id="${esc(`${channel.device_id}:${channel.object_number}`)}">
      <span class="channel-number">${esc(channel.physical_channel || channel.object_number)}</span><span><strong>${esc(channelTitle)}</strong><small>${esc(channelSubtitle)}</small></span>
    </button>${reviewControl}${reviewOnly(`${manualChannel ? manualChannelEditButton(channel, "tiny-action", "Bearbeiten") : ""}<button class="tiny-action" type="button" data-assign-device="${esc(channel.device_id)}" data-assign-channel="${esc(channel.object_number)}" data-assign-manual-channel="${manualChannel}">+ Klemme</button>`)}</div>
    ${channelTree}
  </article>`;
}

function renderDetail(data) {
  const record = data.record;
  document.querySelector(".detail-heading").classList.toggle("detail-heading-fuse", data.type === "fuse");
  // A Verbraucher's name is a whole sentence: a smaller heading than the short Klemme or Sicherung labels.
  document.querySelector(".detail-heading").classList.toggle("detail-heading-load", data.type === "load");
  const typeNames = { device: "GERÄT", group: "GRUPPENADRESSE", channel: "KANAL", terminal: "KLEMME", fuse: "SICHERUNG", fi: "FI", load: "VERBRAUCHER" };
  document.querySelector("#detail-kind").textContent = typeNames[data.type] || data.type.toUpperCase();
  document.querySelector("#crumb-current").textContent = typeNames[data.type] || data.type.toUpperCase();
  const title = data.type === "device" ? record.manual_name || record.device_id
    : data.type === "group" ? record.address
      : data.type === "channel" ? record.physical_channel ? `Kanal ${record.physical_channel}` : etsChannelLabel(record)
          : data.type === "terminal" ? formatTerminalLabel(record.terminal_number || record.label, record.position)
            : data.type === "fuse" ? record.label
              : data.type === "load" ? record.name
                : record.number ? `FI ${record.number}` : "FI";
  const subtitle = data.type === "device"
    ? [record.is_manual ? "Manuell · ohne KNX-Adresse" : "", record.product, record.manufacturer_display || record.manufacturer, record.location, record.description].filter(Boolean).join(" · ")
    : data.type === "group" ? record.name || "Gruppenadresse"
      : data.type === "channel" ? [firstSendGroupName(record) || firstSendGroupName({ groups: data.groups || [] }) || record.name || record.function, record.data_type].filter(Boolean).join(" · ")
        : data.type === "terminal" ? [record.location, record.description].filter(Boolean).join(" · ")
            : data.type === "fuse" ? record.location || ""
              : data.type === "load" ? [record.location, record.terminal_number ? `Kabel ${record.terminal_number}` : "", record.description].filter(Boolean).join(" · ")
                : [record.label, record.location].filter(Boolean).join(" · ");
  const titleElement = document.querySelector("#detail-title");
  titleElement.classList.toggle("has-parent-link", data.type === "channel");
  if (data.type === "channel") {
    const parentTitle = record.manual_name || record.device_id;
    titleElement.innerHTML = `<button class="detail-parent-link" type="button" data-type="device" data-id="${esc(record.device_id)}" aria-label="Gerät ${esc(parentTitle)} öffnen"><small>GERÄT</small><strong>${esc(parentTitle)}</strong><span aria-hidden="true">›</span></button><span class="detail-title-separator" aria-hidden="true">›</span><span>${esc(title || "Eintrag")}</span>`;
  } else {
    titleElement.textContent = title || "Eintrag";
  }
  const titleAction = document.querySelector("#detail-title-action");
  // Edit buttons of the page itself: Sicherung, FI and manual devices (ETS devices come from the KNX project).
  const editButton = (attribute) => reviewOnly(`<button class="button button-small button-secondary" type="button" ${attribute}>Bearbeiten</button>`);
  titleAction.innerHTML = data.type === "fuse"
    ? `<div class="detail-fuse-actions">${fuseFiBadge(record)}<span class="detail-fuse-labels">${fuseMetadataMarkup(record)}</span>${editButton("data-edit-fuse")}</div>`
    : data.type === "fi" ? editButton("data-edit-fi")
      : data.type === "device" && record.is_manual ? editButton("data-edit-device")
        : "";
  document.querySelector("#detail-subtitle").textContent = subtitle;
  const reviewTypes = new Set(["device", "channel", "terminal", "fuse", "fi"]);
  const reviewId = data.type === "device" ? record.device_id
    : data.type === "channel" ? `${record.device_id}:${record.object_number}`
      : record.id;
  const reviewNode = document.querySelector("#detail-review");
  reviewNode.innerHTML = !reviewTypes.has(data.type)
    ? ""
    : reviewControlMarkup(data.type, reviewId, record.reviewed_at, "detail-review-control");
  reviewNode.hidden = !reviewNode.innerHTML;
  const detailBadge = document.querySelector("#detail-badge");
  detailBadge.hidden = !["terminal", "load"].includes(data.type);
  detailBadge.classList.toggle("terminal-header-badges", ["terminal", "load"].includes(data.type));
  if (data.type === "terminal") {
    // The Sicherung of the path (a Klemme wired on behind another one has the Sicherung of that one).
    detailBadge.innerHTML = `<span class="terminal-used-header">${usedCheckboxMarkup(record.used)}<span>In Verwendung</span></span>${fuseHeaderBadge(record.fuse_id, record.fuse, record.fuse_rating)}${editButton("data-edit-terminal")}`;
  } else if (data.type === "load") {
    // The Sicherung(en) feeding the Verbraucher, each with its FI in front when it has one.
    const fuses = record.fuses || [];
    detailBadge.innerHTML = fuses.length
      ? fuses.map((fuse) => `${fuse.fi_id || fuse.fi_number ? fuseFiBadge(fuse) : ""}${fuseHeaderBadge(fuse.id, fuse.label, fuse.rating)}`).join("")
      : fuseHeaderBadge(null);
  } else {
    detailBadge.textContent = "";
  }

  if (data.type === "device") {
    const channels = groupDeviceChannels(data.channels);
    const channelAction = reviewOnly(record.is_manual
      ? `<button class="button button-small button-secondary" type="button" data-add-manual-channel="${esc(record.device_id)}">+ Kanal hinzufügen</button>`
      : `<button class="button button-small button-secondary" type="button" data-assign-device="${esc(record.device_id)}">+ Klemme zuordnen</button>`);
    const channelHeading = record.is_manual ? "MANUELLE KANÄLE" : "GERÄTEKANÄLE";
    const channelMarkup = channels.length
      ? channels.map((channel) => renderChannel(channel)).join("")
      : `<div class="no-data">${record.is_manual ? "Noch keine Kanäle angelegt." : "Keine Kanalobjekte im ETS-Projekt gefunden."}</div>`;
    const channelCount = channels.length === 1 ? "1 Kanal" : `${channels.length} Kanäle`;
    detailContent.innerHTML = `<section class="detail-section"><div class="section-heading"><div><span class="section-tag">${channelHeading}</span><h3>${record.is_manual ? channelCount : `${channelCount} · ${data.channels.length} ETS-Objekte`}</h3></div>${channelAction}</div>
      ${channelMarkup}</section>
      ${haDetailSection(data.home_assistant)}
      ${record.is_manual ? '<section class="source-note"><span class="source-mark"></span><span>Manuell angelegtes Gerät</span></section>' : `<section class="source-note"><span class="source-mark"></span><span>Quelle: ETS-Projekt</span></section>`}`;
  } else if (data.type === "group") {
    const linkedChannels = groupDeviceChannels(data.channels);
    detailContent.innerHTML = `<section class="detail-section"><div class="section-heading"><div><span class="section-tag">VERKNÜPFTE OBJEKTE</span><h3>${linkedChannels.length} Kanäle</h3></div></div>
      ${linkedChannels.length ? linkedChannels.map((channel) => {
        const channelTitle = `${channel.device_id} · ${etsChannelLabel(channel)}`;
        const channelSubtitle = [channel.product, firstSendGroupName(channel) || channel.name || channel.function].filter(Boolean).join(" · ");
        return `<article class="linked-channel"><div class="path-strip">${pathNodeButton("channel", `${channel.device_id}:${channel.object_number}`, "GERÄT · KANAL", channelTitle, channelSubtitle)}<span class="path-edge">→</span>${node("GRUPPENADRESSE", record.address, record.name || "", isSetGroup(channel) ? "group-set-node" : "")}</div></article>`;
      }).join("") : '<div class="no-data">Keine Kanalverknüpfungen im ETS-Projekt gefunden.</div>'}</section>`;
  } else if (data.type === "channel") {
    const channelRows = data.channels?.length ? data.channels : [{
      ...record,
      groups: data.groups,
      terminals: data.terminals,
    }];
    const compactChannel = groupDeviceChannels(channelRows)[0];
    const objectCount = compactChannel.objects.length;
    const channelHeading = record.manual_channel ? "MANUELLER KANAL" : "GERÄTEKANÄLE";
    const channelCount = record.manual_channel ? "Manuell angelegt" : `1 Kanal · ${objectCount} ETS-Objekt${objectCount === 1 ? "" : "e"}`;
    const editChannelButton = record.manual_channel
      ? manualChannelEditButton(record, "button button-small button-secondary", "Kanal bearbeiten")
      : "";
    const channelActions = reviewOnly(`${editChannelButton}<button class="button button-small button-secondary" type="button" data-assign-device="${esc(record.device_id)}" data-assign-channel="${esc(record.object_number)}" data-assign-manual-channel="${Boolean(record.manual_channel)}">+ Klemme zuordnen</button>`);
    detailContent.innerHTML = `<section class="detail-section"><div class="section-heading"><div><span class="section-tag">${channelHeading}</span><h3>${channelCount}</h3></div><div class="detail-actions">${channelActions}</div></div>${renderChannel(compactChannel, true)}</section>${haDetailSection(data.home_assistant, false)}`;
  } else if (data.type === "terminal") {
    const terminalSubtitle = [record.location, record.description].filter(Boolean).join(" · ");
    // A Klemme on the path: this page's Klemme plain, every other one (above or below it) opens its own page.
    const klemmeNode = (terminalId) => {
      if (String(terminalId) === String(record.id)) {
        return terminalPathNode(formatTerminalLabel(record.terminal_number || record.label, record.position), terminalSubtitle, record.wire_color, null, record.used);
      }
      const klemme = data.path_terminals?.[String(terminalId)];
      return klemme ? terminalPathNode(formatTerminalLabel(klemme.terminal_number || klemme.label, klemme.position),
        [klemme.location, klemme.description].filter(Boolean).join(" · "), klemme.wire_color, terminalId, klemme.used) : "";
    };
    // The one Sicherung of the path.
    const fuseNode = record.fuse_id
      ? fusePathNode(record.fuse_id, record.fuse, record.fuse_rating, record.location,
        { id: record.fuse_fi_id, number: record.fuse_fi_number, label: record.fuse_fi_label, location: record.fuse_fi_location })
      : "";
    // Kanäle by key; each Klemme of the path brings the ones it is wired to.
    const channelPaths = new Map();
    const addConnectedPath = (key, strip, removeButton) => {
      if (!channelPaths.has(key)) channelPaths.set(key, { strip, removeButton });
    };
    // A channel with its text button for removing the connection; the button is left out where the wire has its own X.
    const connectedPathMarkup = ({ strip, removeButton }, withRemove = true) =>
      `<div class="terminal-channel-connection"><div class="path-strip">${strip}</div>${withRemove ? removeButton : ""}</div>`;
    for (const channel of data.device_channels || []) {
      const hasObject = channel.first_object_number !== null;
      const channelNode = hasObject
        ? pathNodeButton("channel", `${channel.device_id}:${channel.first_object_number}`, "GERÄT · KANAL", `${channel.device_label || channel.device_id} · Kanal ${channel.physical_channel}`, [channel.product, channel.send_group_name || "Keine S-Gruppenadresse"].filter(Boolean).join(" · "))
        : node("GERÄT · KANAL", `${channel.device_label || channel.device_id} · Kanal ${channel.physical_channel}`, [channel.product, "ETS-Objekte nicht zugeordnet"].filter(Boolean).join(" · "));
      const removeButton = reviewModeEnabled
        ? `<button class="remove-text channel-connection-remove" type="button" data-delete-channel-link data-terminal-id="${esc(channel.terminal_id)}" data-device-id="${esc(channel.device_id)}" data-physical-channel="${esc(channel.physical_channel)}">Verbindung entfernen</button>`
        : "";
      addConnectedPath(`physical:${channel.device_id}:${channel.physical_channel}`, channelNode, removeButton);
    }
    for (const channel of data.channels) {
      const key = channel.physical_channel
        ? `physical:${channel.device_id}:${channel.physical_channel}`
        : `object:${channel.device_id}:${channel.object_number}`;
      if (channelPaths.has(key)) continue;
      const channelTitle = `${channel.device_label || channel.device_id} · ${etsChannelLabel(channel)}`;
      const channelSubtitle = [channel.product, firstSendGroupName(channel) || channel.name || channel.function].filter(Boolean).join(" · ");
      const removeButton = reviewModeEnabled
        ? channel.physical_channel
          ? `<button class="remove-text channel-connection-remove" type="button" data-delete-channel-link data-terminal-id="${esc(channel.terminal_id)}" data-device-id="${esc(channel.device_id)}" data-physical-channel="${esc(channel.physical_channel)}">Verbindung entfernen</button>`
          : channel.assignment_id
            ? `<button class="remove-text channel-connection-remove" type="button" data-delete-assignment="${esc(channel.assignment_id)}">Verbindung entfernen</button>`
            : ""
        : "";
      addConnectedPath(key, pathNodeButton("channel", `${channel.device_id}:${channel.object_number}`, "GERÄT · KANAL", channelTitle, channelSubtitle), removeButton);
    }
    // The path in the order of the connection tree (Sicherung, Kanäle and every Klemme on it), with swap arrows.
    const chain = data.chain || [];
    const shownChannels = new Set();
    const shownLoads = new Set();
    // Path pieces of one chain entry (empty when there is nothing to show for it).
    const refParts = (ref) => {
      // A Klemme can go into the wire above a Kanal, a Klemme (both with something above them) or a Verbraucher.
      const insertNodeId = ref.type === "load" || (["channel", "terminal"].includes(ref.type) && ref.drop_node_id) ? ref.node_id : null;
      const part = (markup) => (markup ? [{ markup, swapNodeId: ref.swap_node_id, swapChildId: ref.swap_child_id, dropNodeId: ref.drop_node_id, branchNodeId: ref.branch_node_id, branchType: ref.branch_type, insertNodeId, insertType: ref.type }] : []);
      if (ref.type === "fuse") return part(fuseNode);
      if (ref.type === "terminal") return part(klemmeNode(ref.terminal_id));
      if (ref.type === "load") {
        shownLoads.add(ref.load_id);
        return part(loadPathNodes((data.loads || []).filter((load) => load.id === ref.load_id), ref.node_id));
      }
      if (ref.type !== "channel") return [];
      const key = ref.channel_key.startsWith("object:")
        ? `object:${ref.device_id}:${ref.channel_key.slice("object:".length)}`
        : `physical:${ref.device_id}:${ref.channel_key}`;
      shownChannels.add(key);
      const entry = channelPaths.get(key);
      return part(entry && connectedPathMarkup(entry, !ref.drop_node_id));
    };
    // Entries below the Klemme are branches, grouped by their real parent; an entry whose parent is not listed stays in line.
    const listed = new Set(chain.map((ref) => ref.node_id));
    const isBranch = (ref) => ref.down_parent_id != null && listed.has(ref.down_parent_id);
    const followersOf = new Map();
    chain.filter(isBranch).forEach((ref) => followersOf.set(ref.down_parent_id, [...(followersOf.get(ref.down_parent_id) || []), ref]));
    // "+" below a Gerät-Kanal at the end of a branch column: a Klemme or Verbraucher behind that Kanal.
    const channelEndAdd = (ref) => (reviewModeEnabled && ref.type === "channel"
      ? [{ noEdge: true, markup: `<div class="edge-actions">${pathAddButton({ nodeId: ref.node_id, nodeType: "channel", branch: true }, "Klemme oder Verbraucher an diesem Kanal anschließen")}</div>` }]
      : []);
    // An entry with its followers: one follower continues the line, several stand side by side.
    const lineParts = (ref, inBranch = false) => {
      const followers = followersOf.get(ref.node_id) || [];
      if (followers.length === 1) return [...refParts(ref), ...lineParts(followers[0], inBranch)];
      if (followers.length < 2) return [...refParts(ref), ...(inBranch ? channelEndAdd(ref) : [])];
      const columns = followers.map((follower) => `<div class="path-branch">${joinPathParts(lineParts(follower, true), true)}</div>`).join("");
      return [...refParts(ref), { noEdge: true, markup: `<div class="path-branches">${columns}</div>` }];
    };
    const parts = chain.length
      ? chain.filter((ref) => !isBranch(ref)).flatMap((ref) => lineParts(ref))
      : [{ markup: `${fuseNode}${fuseNode ? pathArrow() : ""}${klemmeNode(record.id)}` }];
    // Channel paths the tree does not list stay visible behind the chain.
    for (const [key, entry] of channelPaths) {
      if (!shownChannels.has(key)) parts.push({ markup: connectedPathMarkup(entry) });
    }
    // Verbraucher the chain does not show at their node follow the path end.
    const restLoads = loadPathNodes((data.loads || []).filter((load) => !shownLoads.has(load.id)));
    const endsInBranches = /class="path-branches"/.test(parts.at(-1)?.markup || "");
    if (restLoads) parts.push({ markup: restLoads });
    // The "+" at the end appends behind the last Klemme of the path; a path that ends in branches has its "+" on
    // every branch, so the extra one at the end would only repeat them. Only in review mode.
    // Follow the line below this page's Klemme as long as it does not branch; its last Klemme is the end.
    let endId = record.id;
    for (let ref = [...chain].reverse().find((entry) => !isBranch(entry)); ref; ref = (followersOf.get(ref.node_id) || []).length === 1 ? followersOf.get(ref.node_id)[0] : null) {
      if (ref.type === "terminal") endId = ref.terminal_id;
    }
    const endKlemme = String(endId) === String(record.id) ? record : data.path_terminals?.[String(endId)] || record;
    const addButton = reviewModeEnabled && !endsInBranches
      ? `<div class="edge-actions">${pathAddButton({ terminalId: endId, label: formatTerminalLabel(endKlemme.terminal_number || endKlemme.label, endKlemme.position), location: endKlemme.location }, "Klemme, Gerät-Kanal oder Verbraucher am Pfadende hinzufügen")}</div>`
      : "";
    // A Klemme without Sicherung (any colour) can be connected by hand to N, an FI's neutral or PE (review mode only).
    const options = record.conductor_options || [];
    // The empty choice disconnects a connected Klemme; `data-current` restores the select when that is cancelled.
    const currentOption = options.find((option) => option.label === record.wire_phase)?.value || "";
    const conductorControl = reviewModeEnabled && options.length
      ? `<label class="conductor-control">Leiter<select data-conductor-select data-terminal-id="${esc(record.id)}" data-current="${esc(currentOption)}"><option value="">${record.wire_phase ? "– vom Leiter trennen –" : "– nicht angeschlossen –"}</option>${options.map((option) => `<option value="${esc(option.value)}"${option.value === currentOption ? " selected" : ""}>${esc(option.label)}${option.subtitle ? ` · ${esc(option.subtitle)}` : ""}</option>`).join("")}</select></label>`
      : "";
    // "⊕" in front of the first entry when it is a Klemme or Kanal: a further Klemme in front of it (review mode).
    const first = chain.find((ref) => !isBranch(ref));
    const topInsert = reviewModeEnabled && first && ["terminal", "channel"].includes(first.type)
      ? `<div class="edge-actions">${pathInsertButton(first.node_id, first.type, "Klemme davor einfügen")}</div>`
      : "";
    detailContent.innerHTML = `<section class="detail-section"><div class="section-heading"><div><span class="section-tag">KLEMMENPFAD</span></div>${conductorControl}</div>
      <div class="path-stack">${topInsert}${joinPathParts(parts)}${addButton}</div></section>`;
  } else if (data.type === "fuse") {
    const terminalsById = new Map();
    for (const item of data.terminals) {
      const terminalId = String(item.id);
      let terminal = terminalsById.get(terminalId);
      if (!terminal) {
        terminal = { ...item, channels: new Map() };
        terminalsById.set(terminalId, terminal);
      }
      if (!item.device_id) continue;
      const channelKey = item.physical_channel
        ? `physical:${item.device_id}:${item.physical_channel}`
        : `object:${item.device_id}:${item.object_number}`;
      terminal.channels.set(channelKey, item);
    }
    const channelCount = new Set([...terminalsById.values()].flatMap((terminal) => [...terminal.channels.keys()])).size;
    const terminalNode = (terminalId) => {
      const terminal = terminalsById.get(String(terminalId));
      if (!terminal) return node("KLEMME", "Klemme nicht gefunden");
      const label = formatTerminalLabel(terminal.terminal_number || terminal.label, terminal.position);
      return terminalPathNode(label, [terminal.location, terminal.description].filter(Boolean).join(" · "), terminal.wire_color, terminal.id, terminal.used);
    };
    // Markup of one device channel of a terminal.
    const channelPathMarkup = (channel) => {
      const title = `${channel.device_label || channel.device_id} · ${channel.physical_channel ? `Kanal ${channel.physical_channel}` : etsChannelLabel(channel)}`;
      const subtitle = [channel.product, firstSendGroupName(channel) || channel.channel_name || channel.name || channel.function].filter(Boolean).join(" · ");
      const channelId = channel.object_number !== null && channel.object_number !== undefined
        ? `${channel.device_id}:${channel.object_number}`
        : "";
      return channelId
        ? pathNodeButton("channel", channelId, "GERÄT · KANAL", title, subtitle)
        : node("GERÄT · KANAL", title, subtitle);
    };
    const channelsByKey = new Map();
    for (const terminal of terminalsById.values()) {
      for (const [key, channel] of terminal.channels) if (!channelsByKey.has(key)) channelsByKey.set(key, channel);
    }
    const fuseNode = fusePathNode(record.id, record.label, record.rating, record.location);
    // Graph items follow the connection tree (Klemmen wired on behind a Klemme are nodes of it too).
    const graphItem = (treeNode) => {
      let html;
      if (treeNode.type === "fuse") html = fuseNode;
      else if (treeNode.type === "terminal") html = terminalNode(treeNode.entity_id);
      else {
        const key = treeNode.channel_key.startsWith("object:")
          ? `object:${treeNode.device_id}:${treeNode.channel_key.slice("object:".length)}`
          : `physical:${treeNode.device_id}:${treeNode.channel_key}`;
        const channel = channelsByKey.get(key);
        html = channel ? channelPathMarkup(channel)
          : treeNode.entity_id ? pathNodeButton("channel", treeNode.entity_id, "GERÄT · KANAL", graphTitle(treeNode), treeNode.subtitle)
            : node("GERÄT · KANAL", graphTitle(treeNode), treeNode.subtitle);
      }
      // An edge below a node swaps the two when the chain rules allow it (a node with other followers is split);
      // both buttons only exist in the Überprüfungsmodus.
      const children = treeNode.children.map((child) => ({
        ...graphItem(child),
        swapAttrs: reviewModeEnabled && treeNode.swappable_children.includes(child.id) ? `data-swap-node="${esc(treeNode.id)}" data-swap-child="${esc(child.id)}" title="Reihenfolge tauschen" aria-label="Reihenfolge tauschen"` : "",
        dropAttrs: reviewModeEnabled ? `data-drop-node="${esc(child.id)}"` : "",
      }));
      return { html, children };
    };
    const fuseItem = data.fuse_graph ? graphItem(data.fuse_graph) : null;
    // The page is about this Sicherung (and its FI sits in the header), so the graph starts below it.
    const graphRoots = !fuseItem ? []
      : data.fuse_graph.type === "fuse" ? fuseItem.children.map((child) => ({ ...child, swapAttrs: "", dropAttrs: "" }))
        : [fuseItem];
    const graph = data.fuse_graph?.children.length ? nodeGraphMarkup(graphRoots) : "";
    const pathContent = graph || '<div class="no-data">Diese Sicherung ist noch keiner Klemme zugeordnet.</div>';
    // A Klemme or a Verbraucher wired directly to this Sicherung (review mode); further entries follow with the "+"
    // on the Klemme page.
    const addTerminal = reviewOnly('<button class="button button-small button-secondary" type="button" data-fuse-add-terminal>+ Anschließen</button>');
    // Verbraucher hanging directly on this Sicherung (no Klemme in between); the ✕ on a card cuts that link.
    const directLoads = (data.loads || []).map((load) => loadPathNodes([load],
      load.feeds.find((feed) => String(feed.fuse_id) === String(record.id))?.node_id)).join("");
    const loadsSection = directLoads
      ? `<section class="detail-section"><div class="section-heading"><div><span class="section-tag">VERBRAUCHER DIREKT AN DER SICHERUNG</span><h3>${data.loads.length} Verbraucher</h3></div></div><div class="path-stack">${directLoads}</div></section>`
      : "";
    detailContent.innerHTML = `<section class="detail-section"><div class="section-heading"><div><span class="section-tag">SICHERUNGSPFAD</span><h3>${terminalsById.size} Klemmen · ${channelCount} Kanalgruppen</h3></div><div class="detail-actions">${addTerminal}</div></div>${pathContent}</section>${loadsSection}`;
  } else if (data.type === "fi") {
    // In review mode every Sicherung can be taken off the FI (✕) and another one of the location placed below it.
    const fuseRows = data.fuses.map((fuse) => {
      const row = `<button class="overview-item overview-item-fuse" type="button" data-type="fuse" data-id="${esc(fuse.id)}"><span class="overview-item-copy"><strong>${esc(fuse.label)}</strong><small>${esc([fuse.location, fuse.rating].filter(Boolean).join(" · "))}</small></span><span class="overview-item-arrow" aria-hidden="true">›</span></button>`;
      return reviewModeEnabled
        ? `<div class="overview-row-action">${row}<button class="tiny-action is-danger" type="button" data-fi-cut-fuse="${esc(fuse.id)}" data-label="${esc(fuse.label)}" title="Sicherung von diesem FI trennen">✕ trennen</button></div>`
        : row;
    }).join("");
    const addFuse = reviewOnly(data.assignable_fuses?.length
      ? `<label class="conductor-control">Sicherung zuordnen<select data-fi-add-fuse="${esc(record.id)}"><option value="">– wählen –</option>${data.assignable_fuses.map((fuse) => `<option value="${esc(fuse.id)}">${esc([fuse.label, fuse.rating, fuse.fi_number ? `jetzt FI ${fuse.fi_number}` : ""].filter(Boolean).join(" · "))}</option>`).join("")}</select></label>`
      : "");
    detailContent.innerHTML = `<section class="detail-section"><div class="section-heading"><div><span class="section-tag">ZUGEORDNETE SICHERUNGEN</span><h3>${data.fuses.length} Sicherungen</h3></div>${addFuse}</div><div class="overview-items">${fuseRows || '<div class="overview-empty">Keine Sicherungen zugeordnet.</div>'}</div></section>`;
  } else if (data.type === "load") {
    // Its own page (also for a Verbraucher without Klemme or Sicherung): its card with L, N, PE; in review mode ✎
    // edits it, a missing feed is assigned and every Klemme feed can be changed or cut, as on the Klemme page.
    detailContent.innerHTML = `<section class="detail-section"><div class="section-heading"><div><span class="section-tag">ANSCHLÜSSE</span><h3>L, N und PE</h3></div></div><div class="path-stack">${loadPathNodes([record], null, false)}</div></section>`;
  }
  requestAnimationFrame(() => requestAnimationFrame(drawAllGraphWires));
}

// Left-to-right node graph: one column per depth, leaves in rows of their own, parents centred beside their children.
// Takes a list of root items {html, children, swapAttrs, dropAttrs}; those attributes (on a child) put swap/drop buttons on its wire.
function nodeGraphMarkup(roots) {
  const cells = [];
  const swaps = [];
  let rows = 0;
  let columns = 0;
  const place = (item, depth, parentId) => {
    const id = cells.length;
    cells.push("");
    const firstRow = rows + 1;
    if (item.children.length) item.children.forEach((child) => place(child, depth + 1, id));
    else rows += 1;
    columns = Math.max(columns, depth + 1);
    cells[id] = `<div class="tree-graph-node" data-graph-id="${id}" data-graph-parent="${parentId ?? ""}" style="grid-column: ${depth + 1}; grid-row: ${firstRow} / ${rows + 1}">${item.html}</div>`;
    if (item.swapAttrs || item.dropAttrs) {
      const swap = item.swapAttrs ? `<button class="tree-graph-swap" type="button" ${item.swapAttrs}>⇄</button>` : "";
      const drop = item.dropAttrs ? `<button class="tree-graph-swap tree-graph-drop" type="button" title="Verbindung trennen" aria-label="Verbindung trennen" ${item.dropAttrs}>✕</button>` : "";
      swaps.push(`<div class="tree-graph-actions" data-graph-edge="${id}">${swap}${drop}</div>`);
    }
  };
  roots.forEach((root) => place(root, 0, null));
  return `<div class="tree-graph"><div class="tree-graph-canvas" style="--columns: ${columns}"><svg class="tree-graph-wires" aria-hidden="true"></svg>${cells.join("")}${swaps.join("")}</div></div>`;
}

function drawAllGraphWires() {
  drawFuseGraphWires();
  drawTreeGraphWires();
}

// Draws a wire from every parent node to each child and puts the swap buttons on the wire middles.
function drawTreeGraphWires() {
  detailContent.querySelectorAll(".tree-graph-canvas").forEach((canvas) => {
    const svg = canvas.querySelector(".tree-graph-wires");
    const bounds = canvas.getBoundingClientRect();
    if (!bounds.width || !bounds.height) return;
    // Screen pixels per layout pixel (page zoom), so wires and buttons use the canvas' own units.
    const zoom = bounds.width / canvas.offsetWidth;
    svg.setAttribute("viewBox", `0 0 ${canvas.offsetWidth} ${canvas.offsetHeight}`);
    svg.replaceChildren();
    const nodes = new Map([...canvas.querySelectorAll("[data-graph-id]")].map((cell) => [cell.dataset.graphId, cell]));
    nodes.forEach((child, id) => {
      const parent = nodes.get(child.dataset.graphParent);
      if (!parent) return;
      const parentRect = parent.getBoundingClientRect();
      const childRect = child.getBoundingClientRect();
      const startX = (parentRect.right - bounds.left) / zoom;
      const startY = (parentRect.top + parentRect.height / 2 - bounds.top) / zoom;
      const endX = (childRect.left - bounds.left) / zoom;
      const endY = (childRect.top + childRect.height / 2 - bounds.top) / zoom;
      const bend = Math.max(14, (endX - startX) * 0.5);
      const path = document.createElementNS("http://www.w3.org/2000/svg", "path");
      path.setAttribute("d", `M ${startX} ${startY} C ${startX + bend} ${startY}, ${endX - bend} ${endY}, ${endX} ${endY}`);
      path.setAttribute("class", "tree-graph-wire");
      svg.append(path);
      const swap = canvas.querySelector(`[data-graph-edge="${id}"]`);
      if (swap) Object.assign(swap.style, { left: `${(startX + endX) / 2}px`, top: `${(startY + endY) / 2}px` });
    });
  });
}

function drawFuseGraphWires() {
  const zoom = Number.parseFloat(getComputedStyle(document.body).zoom) || 1;
  detailContent.querySelectorAll(".fuse-node-graph").forEach((graph) => {
    const canvas = graph.querySelector(".fuse-graph-canvas");
    const svg = graph.querySelector(".fuse-graph-wires");
    const bounds = canvas.getBoundingClientRect();
    if (!bounds.width || !bounds.height) return;
    const width = bounds.width / zoom;
    const height = bounds.height / zoom;
    svg.setAttribute("viewBox", `0 0 ${width} ${height}`);
    svg.removeAttribute("width");
    svg.removeAttribute("height");
    svg.replaceChildren();
    const namespace = "http://www.w3.org/2000/svg";
    const targets = [...graph.querySelectorAll("[data-graph-target]")];
    graph.querySelectorAll("[data-graph-source]").forEach((source) => {
      const sourceRect = source.getBoundingClientRect();
      const sourceId = source.dataset.graphSource;
      const connectedTargets = targets.filter((target) => target.dataset.connectedSources.split(" ").includes(sourceId));
      connectedTargets.forEach((target, sourcePortIndex) => {
        const targetRect = target.getBoundingClientRect();
        const sourceY = (sourceRect.top + sourceRect.height * ((sourcePortIndex + 1) / (connectedTargets.length + 1)) - bounds.top) / zoom;
        const targetY = (targetRect.top + targetRect.height / 2 - bounds.top) / zoom;
        const startX = (sourceRect.right - bounds.left) / zoom;
        const endX = (targetRect.left - bounds.left) / zoom;
        const bend = Math.max(28, (endX - startX) * 0.48);
        const pathData = `M ${startX} ${sourceY} C ${startX + bend} ${sourceY}, ${endX - bend} ${targetY}, ${endX} ${targetY}`;
        const path = document.createElementNS(namespace, "path");
        path.setAttribute("d", pathData);
        path.setAttribute("class", "fuse-graph-wire");
        svg.append(path);
      });
    });
  });
}

function resetAssignmentAutocomplete(form) {
  form.querySelectorAll("[data-suggestions]").forEach((input) => {
    delete input.dataset.autocompleteTyping;
    delete input.dataset.selectedSuggestion;
    input.setAttribute("aria-expanded", "false");
    input.parentElement.classList.remove("is-open");
    input.parentElement.querySelector(".autocomplete-popup").hidden = true;
  });
}

function fillAssignment(deviceId = "", objectNumber = "", manualChannel = false) {
  assignmentMode = "assignment";
  const form = document.querySelector("#assignment-form");
  form.reset();
  resetAssignmentAutocomplete(form);
  form.querySelector("[data-terminal-suggestions]").hidden = true;
  form.elements.terminal_number.parentElement.classList.remove("is-open");
  form.elements.terminal_number.setAttribute("aria-expanded", "false");
  form.elements.terminal_id.value = "";
  form.elements.used.checked = true;
  document.querySelectorAll(".channel-assignment-field").forEach((field) => {
    field.hidden = manualChannel;
    field.querySelector("input").required = !manualChannel;
  });
  document.querySelectorAll(".terminal-fuse-field").forEach((field) => { field.hidden = false; });
  document.querySelector("#assignment-title").textContent = "Klemme zuordnen";
  document.querySelector("#assignment-submit").textContent = "Zuordnung speichern";
  form.elements.device_id.value = deviceId;
  form.elements.object_number.value = objectNumber;
  form.elements.location.value = localStorage.getItem(lastLocationKey) || "";
  setWireColor("bk");
  loadPicklists(form.elements.location.value);
  document.querySelector("#assignment-error").textContent = "";
  // Only an existing Klemme (edit mode) can be deleted.
  document.querySelector("#delete-terminal-button").hidden = assignmentMode !== "edit";
  assignmentDialog.showModal();
  form.elements.location.focus();
}

async function loadAssignmentTerminalSuggestions() {
  const form = document.querySelector("#assignment-form");
  const numberInput = form.elements.terminal_number;
  const popup = form.querySelector("[data-terminal-suggestions]");
  const number = numberInput.value.trim();
  const sequence = ++assignmentTerminalLookupSequence;
  if (!number) {
    showSuggestions(popup, numberInput, "");
    return;
  }
  try {
    const query = new URLSearchParams({ number, location: form.elements.location.value.trim() });
    const terminals = await request(`api/terminals?${query}`);
    if (sequence !== assignmentTerminalLookupSequence || number !== numberInput.value.trim()) return;
    // No hits: no popup.
    showSuggestions(popup, numberInput, terminals.map((terminal) => {
      const label = formatTerminalLabel(terminal.terminal_number || terminal.label, terminal.position);
      const detail = [terminal.location, terminal.wire_color].filter(Boolean).join(" · ");
      return `<button class="picker-option" type="button" role="option" data-assignment-terminal data-number="${esc(terminal.terminal_number)}" data-position="${esc(terminal.position)}" data-location="${esc(terminal.location)}" data-color="${esc(terminal.wire_color)}"><strong>${esc(label)}</strong><small>${esc(detail)}</small></button>`;
    }).join(""));
  } catch (error) {
    if (sequence !== assignmentTerminalLookupSequence) return;
    showSuggestions(popup, numberInput, `<span class="picker-option-note">Klemmen konnten nicht geladen werden: ${esc(error.message)}</span>`);
  }
}

function scheduleAssignmentTerminalSuggestions() {
  clearTimeout(assignmentTerminalLookupTimer);
  assignmentTerminalLookupTimer = setTimeout(loadAssignmentTerminalSuggestions, 160);
}

function initializeAssignmentTerminalSuggestions() {
  const form = document.querySelector("#assignment-form");
  const numberInput = form.elements.terminal_number;
  const popup = form.querySelector("[data-terminal-suggestions]");
  numberInput.addEventListener("input", scheduleAssignmentTerminalSuggestions);
  numberInput.addEventListener("keydown", (event) => {
    if (event.key === "Escape") {
      popup.hidden = true;
      numberInput.parentElement.classList.remove("is-open");
      numberInput.setAttribute("aria-expanded", "false");
    } else if (event.key === "ArrowDown" && !popup.hidden) {
      event.preventDefault();
      popup.querySelector("[data-assignment-terminal]")?.focus();
    }
  });
  popup.addEventListener("mousedown", (event) => event.preventDefault());
  popup.addEventListener("click", (event) => {
    const option = event.target.closest("[data-assignment-terminal]");
    if (!option) return;
    form.elements.terminal_number.value = option.dataset.number;
    form.elements.position.value = option.dataset.position;
    form.elements.location.value = option.dataset.location;
    if (option.dataset.color) setWireColor(option.dataset.color);
    popup.hidden = true;
    numberInput.parentElement.classList.remove("is-open");
    numberInput.setAttribute("aria-expanded", "false");
    loadPicklists(form.elements.location.value);
    form.elements.position.focus();
  });
}

function fillNewTerminal() {
  assignmentMode = "terminal";
  const form = document.querySelector("#assignment-form");
  form.reset();
  resetAssignmentAutocomplete(form);
  form.elements.terminal_id.value = "";
  form.elements.used.checked = true;
  document.querySelectorAll(".channel-assignment-field").forEach((field) => {
    field.hidden = true;
    field.querySelector("input").required = false;
  });
  document.querySelectorAll(".terminal-fuse-field").forEach((field) => { field.hidden = true; });
  document.querySelector("#assignment-title").textContent = "Klemme hinzufügen";
  document.querySelector("#assignment-submit").textContent = "Klemme anlegen";
  form.elements.location.value = selectedTerminalLocation || localStorage.getItem(lastLocationKey) || "";
  setWireColor("bk");
  loadPicklists(form.elements.location.value);
  document.querySelector("#assignment-error").textContent = "";
  // Only an existing Klemme (edit mode) can be deleted.
  document.querySelector("#delete-terminal-button").hidden = assignmentMode !== "edit";
  assignmentDialog.showModal();
  form.elements.location.focus();
}

function fillTerminalEdit(record) {
  assignmentMode = "edit";
  const form = document.querySelector("#assignment-form");
  form.reset();
  resetAssignmentAutocomplete(form);
  form.elements.terminal_id.value = record.id;
  document.querySelectorAll(".channel-assignment-field").forEach((field) => {
    field.hidden = true;
    field.querySelector("input").required = false;
  });
  document.querySelectorAll(".terminal-fuse-field").forEach((field) => { field.hidden = false; });
  document.querySelector("#assignment-title").textContent = "Klemme bearbeiten";
  document.querySelector("#assignment-submit").textContent = "Änderungen speichern";
  form.elements.location.value = record.location || "";
  form.elements.terminal_number.value = record.terminal_number || String(record.label || "").split("/")[0];
  form.elements.position.value = record.position || "";
  form.elements.fuse.value = record.fuse || "";
  form.elements.fuse_id.value = record.fuse_id || "";
  form.elements.description.value = record.description || "";
  form.elements.used.checked = Boolean(record.used);
  setWireColor(record.wire_color || "");
  loadPicklists(record.location || "");
  document.querySelector("#assignment-error").textContent = "";
  // Only an existing Klemme (edit mode) can be deleted.
  document.querySelector("#delete-terminal-button").hidden = assignmentMode !== "edit";
  assignmentDialog.showModal();
  form.elements.terminal_number.focus();
}

function showImportPreview(data) {
  importToken = data.token;
  document.querySelector("#import-step-upload").hidden = true;
  document.querySelector("#import-step-preview").hidden = false;
  document.querySelector("#preview-summary").innerHTML = `<strong>${data.valid_count} gültig</strong><span>${data.invalid_count} fehlerhaft</span>${data.truncated ? "<span>Vorschau auf 200 Zeilen begrenzt</span>" : ""}`;
  document.querySelector("#preview-rows").innerHTML = data.rows.map((row) => `<tr><td>${row.row}</td><td>${esc(row.device_id)} / ${esc(row.object_number)}</td><td>${esc(row.location)} / ${esc(formatTerminalLabel(row.terminal_number, row.position))}</td><td>${esc(row.wire_color)}${row.fuse ? ` · ${esc(row.fuse)} ${esc(row.fuse_rating)}` : ""}</td><td class="${row.valid ? "check-ok" : "check-error"}">${row.valid ? "OK" : esc(row.errors.join(" "))}</td></tr>`).join("") || '<tr><td colspan="5">Keine Datenzeilen gefunden.</td></tr>';
  document.querySelector("#apply-import").disabled = data.valid_count === 0;
}

async function loadStats() {
  try {
    const stats = await request("api/stats");
    // The status line only shows a problem; with a reachable database it stays hidden.
    showSearchStatus("");
    document.querySelector("#stat-devices").textContent = stats.devices.toLocaleString("de-DE");
    document.querySelector("#stat-groups").textContent = stats.group_addresses.toLocaleString("de-DE");
    renderHomeOverviews(stats);
  } catch {
    showSearchStatus("Datenbank nicht erreichbar");
  }
}

// Status line below the main search: shown only with a message.
function showSearchStatus(message) {
  const status = document.querySelector("#search-status");
  status.textContent = message;
  status.closest(".search-caption").hidden = !message;
}

searchInput.addEventListener("input", () => {
  extractReviewSearchTerm();
  clearTimeout(searchTimer);
  if (buildSearchQuery()) {
    selectedSearchCategory = "";
    updateSearchHistory(true);
    showSearchPage();
    searchTimer = setTimeout(search, 110);
  } else {
    search();
  }
});
document.querySelector("#review-search-terms").addEventListener("click", (event) => {
  const button = event.target.closest("[data-review-search-term]");
  if (!button) return;
  selectedReviewSearchTerm = selectedReviewSearchTerm === button.dataset.reviewSearchTerm
    ? ""
    : button.dataset.reviewSearchTerm;
  renderReviewSearchTerm();
  searchInput.focus();
  searchInput.dispatchEvent(new Event("input", { bubbles: true }));
});
document.querySelector("#search-filter-tags").addEventListener("click", (event) => {
  if (!event.target.closest("[data-remove-review-search]")) return;
  clearReviewSearchTerm();
  searchInput.focus();
  searchInput.dispatchEvent(new Event("input", { bubbles: true }));
});
searchInput.addEventListener("keydown", (event) => {
  if (event.key === "Escape") {
    searchInput.value = "";
    clearReviewSearchTerm();
    search();
    searchInput.blur();
  }
});
document.querySelector("#search-filters").addEventListener("click", (event) => {
  const button = event.target.closest("[data-search-filter]");
  if (!button) return;
  selectedSearchCategory = selectedSearchCategory === button.dataset.searchFilter ? "" : button.dataset.searchFilter;
  updateSearchHistory();
  renderSearchResults(currentSearchItems);
});
document.querySelector("#overview-location-filters").addEventListener("click", (event) => {
  const button = event.target.closest("[data-location-filter]");
  if (!button) return;
  selectedTerminalLocation = button.dataset.locationFilter;
  // The chosen location is part of the address (#overview/terminals/<Ort>).
  currentPageState = { ...currentPageState, location: selectedTerminalLocation };
  history.replaceState({ ...(history.state || {}), schaltschrank: currentPageState }, "", viewUrl(currentPageState));
  renderOverviewItems("terminals", currentOverviewItems);
});
resultsNode.addEventListener("click", (event) => {
  const button = event.target.closest("[data-type][data-id]");
  if (!button) return;
  updateSearchHistory();
  clearTimeout(searchTimer);
  searchInput.value = "";
  clearReviewSearchTerm();
  selectedSearchCategory = "";
  currentSearchItems = [];
  searchViewNode.hidden = true;
  if (button.dataset.type === "location") openLocationTerminals(button.dataset.location);
  else openRecord(button.dataset.type, button.dataset.id);
});
document.querySelector("#brand-home").addEventListener("click", () => showHome());
document.querySelector("#settings-toggle").addEventListener("click", () => {
  if (settingsNode.hidden) showSettings();
  else showHome();
});
const reviewModeToggle = document.querySelector("#review-mode-toggle");
reviewModeToggle.checked = reviewModeEnabled;
applyReviewMode();
reviewModeToggle.addEventListener("change", () => {
  reviewModeEnabled = reviewModeToggle.checked;
  sessionStorage.setItem(reviewModeKey, String(reviewModeEnabled));
  applyReviewMode();
});
// Settings: restart the server (loads new program files after an update), then reload this page once it answers
// again, with fresh web files.
document.querySelector("#restart-server").addEventListener("click", async (event) => {
  if (!confirm("Server neu starten? Offene Eingaben gehen verloren.")) return;
  const button = event.currentTarget;
  button.disabled = true;
  try {
    await postJson("api/restart", {});
  } catch (error) {
    button.disabled = false;
    notify(error.message, true);
    return;
  }
  notify("Server startet neu …");
  await reloadAfterRestart(button);
});

// Waits until the restarted server answers again, then reloads the page with fresh web files.
async function reloadAfterRestart(button) {
  const wait = (ms) => new Promise((resolve) => { setTimeout(resolve, ms); });
  await wait(1000);
  for (let attempt = 0; attempt < 60; attempt += 1) {
    try {
      await request("api/stats");
      await Promise.all(["app.js", "styles.css", "./"].map((url) => fetch(url, { cache: "reload" })));
      location.reload();
      return;
    } catch {
      await wait(500);
    }
  }
  button.disabled = false;
  notify("Der Server antwortet nicht. Bitte dort prüfen.", true);
}

// A file as base64 text for a JSON upload.
async function fileBase64(file) {
  const bytes = new Uint8Array(await file.arrayBuffer());
  let binary = "";
  for (let offset = 0; offset < bytes.length; offset += 0x8000) binary += String.fromCharCode(...bytes.subarray(offset, offset + 0x8000));
  return btoa(binary);
}

// Settings: replace the whole database with a file. The server checks it, backs the current one up, swaps it
// and restarts; the page then reloads on the new data.
document.querySelector("#import-database").addEventListener("click", () => document.querySelector("#database-file").click());
document.querySelector("#database-file").addEventListener("change", async (event) => {
  const input = event.currentTarget;
  const file = input.files[0];
  input.value = "";
  if (!file) return;
  if (file.size > 10 * 1024 * 1024) {
    notify("Die Datei darf höchstens 10 MB groß sein.", true);
    return;
  }
  if (!confirm(`„${file.name}“ importieren?\n\nDie gesamte aktuelle Datenbank wird durch diese Datei ersetzt. Vorher wird sie gesichert (Ordner backups); danach startet der Server neu.`)) return;
  const button = document.querySelector("#import-database");
  button.disabled = true;
  try {
    const data = await postJson("api/database/import", { filename: file.name, file_base64: await fileBase64(file) });
    const counts = data.counts;
    notify(`Importiert: ${counts.terminals} Klemmen, ${counts.fuses} Sicherungen, ${counts.devices} Geräte, ${counts.loads} Verbraucher. Server startet neu …`);
  } catch (error) {
    button.disabled = false;
    notify(error.message, true);
    return;
  }
  await reloadAfterRestart(button);
});
document.querySelector("#add-fuse").addEventListener("click", () => openFuseEditor());
// Device dialog: a new manual device, or (`record`) one to edit or delete.
function openDeviceEditor(record = null) {
  const form = document.querySelector("#device-form");
  form.reset();
  form.elements.device_id.value = record?.device_id || "";
  for (const [field, key] of [["name", "manual_name"], ["manufacturer", "manufacturer"], ["product", "product"], ["location", "location"], ["description", "description"]]) {
    form.elements[field].value = record?.[key] || "";
  }
  document.querySelector("#device-dialog-title").textContent = record ? "Gerät bearbeiten" : "Gerät hinzufügen";
  document.querySelector("#device-submit").textContent = record ? "Änderungen speichern" : "Gerät anlegen";
  document.querySelector("#delete-device").hidden = !record;
  document.querySelector("#device-error").textContent = "";
  deviceDialog.showModal();
  form.elements.name.focus();
}

document.querySelector("#add-device").addEventListener("click", () => openDeviceEditor());
document.querySelector("#device-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const payload = Object.fromEntries(new FormData(event.currentTarget).entries());
  const editing = Boolean(payload.device_id);
  try {
    const result = await postJson(editing ? "api/devices/update" : "api/devices", payload);
    deviceDialog.close();
    notify(editing ? "Gerät gespeichert." : "Gerät hinzugefügt.");
    loadStats();
    if (editing) {
      await refreshRecord();
      return;
    }
    await openOverview("devices");
    await openRecord("device", result.device_id);
  } catch (error) {
    document.querySelector("#device-error").textContent = error.message;
  }
});
document.querySelector("#delete-device").addEventListener("click", async () => {
  const form = document.querySelector("#device-form");
  const result = await confirmedPost(
    `Gerät ${form.elements.name.value} wirklich löschen?\n\nAlle Kanäle des Geräts werden gelöscht; verbundene Klemmen bleiben erhalten und werden nur vom Kanal getrennt. Diese Aktion kann nicht rückgängig gemacht werden.`,
    "api/devices/delete", { device_id: form.elements.device_id.value });
  if (!result) return;
  deviceDialog.close();
  notify(`Gerät gelöscht (${result.channels_removed} Kanäle).`);
  loadStats();
  openOverview("devices");
});

// Verbraucher dialog (Klemme page, review mode): rename, description, delete.
const loadDialog = document.querySelector("#load-dialog");
// Edit a Verbraucher, or (no `load`) create a new one fed by an existing Klemme (Verbraucher page).
function openLoadEditor(load, create = false) {
  if (!load && !create) return;
  const form = document.querySelector("#load-form");
  form.reset();
  form.elements.load_id.value = load?.id || "";
  form.elements.name.value = load?.name || "";
  form.elements.description.value = load?.description || "";
  form.elements.no_pe.checked = Boolean(load?.no_pe);
  form.querySelectorAll(".load-create-only").forEach((element) => { element.hidden = !create; });
  if (create) {
    form.elements.location.value = localStorage.getItem(lastLocationKey) || "";
    setLoadFeed("terminal");
  } else {
    form.elements.location.required = false;
    form.elements.terminal_number.required = false;
  }
  document.querySelector("#load-title").textContent = create ? "Neuer Verbraucher" : "Verbraucher bearbeiten";
  document.querySelector("#delete-load").hidden = create;
  document.querySelector("#load-error").textContent = "";
  loadDialog.showModal();
  form.elements.name.focus();
}
// New Verbraucher: fed by a Klemme (room, number, position) or directly by a Sicherung (picked from all rooms).
function setLoadFeed(feed) {
  const form = document.querySelector("#load-form");
  form.elements.feed.value = feed;
  form.querySelectorAll("[data-load-feed]").forEach((fieldset) => { fieldset.hidden = fieldset.dataset.loadFeed !== feed; });
  form.elements.location.required = feed === "terminal";
  form.elements.terminal_number.required = feed === "terminal";
  form.elements.fuse.required = feed === "fuse";
}
document.querySelector("#load-form").addEventListener("change", (event) => {
  if (event.target.name === "feed") setLoadFeed(event.target.value);
});
document.querySelector("#add-load").addEventListener("click", () => {
  loadPicklists();
  openLoadEditor(null, true);
});
document.querySelector("#load-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const fields = Object.fromEntries(new FormData(event.currentTarget).entries());
  try {
    if (!fields.load_id) {
      // New Verbraucher: it opens on the page of the Klemme or Sicherung feeding it (add N and PE there).
      const data = await postJson("api/loads/create", fields);
      if (fields.feed !== "fuse") localStorage.setItem(lastLocationKey, fields.location);
      loadDialog.close();
      notify(data.created ? "Verbraucher angelegt." : data.already_exists ? "Diesen Verbraucher gibt es dort schon." : "Verbraucher verbunden.");
      loadStats();
      if (data.terminal_id) openRecord("terminal", data.terminal_id);
      else openRecord("fuse", data.fuse_id);
      return;
    }
    await postJson("api/loads/update", fields);
    loadDialog.close();
    notify("Verbraucher gespeichert.");
    await refreshRecord();
  } catch (error) {
    document.querySelector("#load-error").textContent = error.message;
  }
});
document.querySelector("#delete-load").addEventListener("click", async () => {
  const form = document.querySelector("#load-form");
  const result = await confirmedPost(
    `Verbraucher ${form.elements.name.value} wirklich löschen?\n\nAlle Verbindungen zu seinen Klemmen werden getrennt; die Klemmen bleiben erhalten.`,
    "api/loads/delete", { load_id: form.elements.load_id.value });
  if (!result) return;
  loadDialog.close();
  notify("Verbraucher gelöscht.");
  loadStats();
  // On its own page there is nothing left to show: the list of all Verbraucher.
  if (currentPageState?.type === "load") openOverview("loads");
  else await refreshRecord();
});

// FI dialog (FI page, review mode): number, label, delete.
const fiDialog = document.querySelector("#fi-dialog");
function openFiEditor(record) {
  const form = document.querySelector("#fi-form");
  form.elements.fi_id.value = record.id;
  form.elements.number.value = record.number || "";
  form.elements.label.value = record.label || "";
  document.querySelector("#fi-error").textContent = "";
  fiDialog.showModal();
  form.elements.number.focus();
}
document.querySelector("#fi-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  try {
    await postJson("api/fi/update", Object.fromEntries(new FormData(event.currentTarget).entries()));
    fiDialog.close();
    notify("FI gespeichert.");
    await refreshRecord();
  } catch (error) {
    document.querySelector("#fi-error").textContent = error.message;
  }
});
document.querySelector("#delete-fi").addEventListener("click", async () => {
  const form = document.querySelector("#fi-form");
  const fuses = currentDetail?.type === "fi" ? currentDetail.fuses.length : 0;
  const result = await confirmedPost(
    `FI wirklich löschen?\n\n${fuses} Sicherung(en) kommen wieder direkt an ihre Phase; blaue Klemmen am Neutralleiter dieses FI werden getrennt und erscheinen in der Prüfliste. Diese Aktion kann nicht rückgängig gemacht werden.`,
    "api/fi/delete", { fi_id: form.elements.fi_id.value });
  if (!result) return;
  fiDialog.close();
  notify("FI gelöscht.");
  openOverview("fuses");
});

// Location dialog (Orte overview, review mode): new, rename, delete (only when empty).
const locationDialog = document.querySelector("#location-dialog");
function openLocationEditor(item = null) {
  const form = document.querySelector("#location-form");
  form.reset();
  form.elements.location_id.value = item?.id || "";
  form.elements.name.value = item?.title || "";
  form.elements.description.value = item?.description || "";
  document.querySelector("#location-dialog-title").textContent = item ? "Ort bearbeiten" : "Ort hinzufügen";
  document.querySelector("#location-submit").textContent = item ? "Speichern" : "Ort anlegen";
  document.querySelector("#delete-location").hidden = !item;
  document.querySelector("#location-error").textContent = "";
  locationDialog.showModal();
  form.elements.name.focus();
}
document.querySelector("#add-location").addEventListener("click", () => openLocationEditor());
document.querySelector("#location-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const payload = Object.fromEntries(new FormData(event.currentTarget).entries());
  try {
    await postJson(payload.location_id ? "api/locations/update" : "api/locations/create", payload);
    locationDialog.close();
    notify(payload.location_id ? "Ort gespeichert." : "Ort angelegt.");
    loadStats();
    loadPicklists();
    openOverview("locations", false);
  } catch (error) {
    document.querySelector("#location-error").textContent = error.message;
  }
});
document.querySelector("#delete-location").addEventListener("click", async () => {
  const form = document.querySelector("#location-form");
  const result = await confirmedPost(`Ort ${form.elements.name.value} wirklich löschen?`, "api/locations/delete", { location_id: form.elements.location_id.value });
  if (!result) return;
  locationDialog.close();
  notify("Ort gelöscht.");
  loadStats();
  loadPicklists();
  openOverview("locations", false);
});
document.querySelector("#device-channel-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const payload = Object.fromEntries(new FormData(event.currentTarget).entries());
  const editing = Boolean(payload.object_number);
  try {
    const result = await postJson(editing ? "api/device-channels/update" : "api/device-channels", payload);
    deviceChannelDialog.close();
    notify(editing ? "Kanal gespeichert." : "Kanal hinzugefügt.");
    await openRecord("device", result.device_id);
  } catch (error) {
    document.querySelector("#device-channel-error").textContent = error.message;
  }
});
document.querySelector("#delete-device-channel").addEventListener("click", async () => {
  const form = document.querySelector("#device-channel-form");
  const deviceId = form.elements.device_id.value;
  const objectNumber = form.elements.object_number.value;
  const channelLabel = form.elements.physical_channel.value;
  if (!confirm(`Kanal ${channelLabel} wirklich löschen?\n\nZugeordnete Klemmen werden von diesem Kanal getrennt. Diese Aktion kann nicht rückgängig gemacht werden.`)) return;
  try {
    const result = await postJson("api/device-channels/delete", { device_id: deviceId, object_number: objectNumber });
    deviceChannelDialog.close();
    notify(result.assignments_removed
      ? `Kanal gelöscht; ${result.assignments_removed} Klemmen-Zuordnung(en) entfernt.`
      : "Kanal gelöscht.");
    await openRecord("device", deviceId);
  } catch (error) {
    document.querySelector("#device-channel-error").textContent = error.message;
  }
});
document.querySelector("#home-overview-buttons").addEventListener("click", (event) => {
  const button = event.target.closest("[data-open-overview]");
  if (button) openOverview(button.dataset.openOverview);
});
document.querySelector("#open-graph").addEventListener("click", () => openGraph());

// PDF export of the Gesamtgraph as shown (mode, filter, location, folds). A dialog takes the settings (paper,
// orientation, margin, scale, header, page numbers, legend, Prüfliste); the graph is scaled to the page width and
// split into pages by hand: every page is its own clipped copy of the graph, cut in the lowest gap between boxes
// that still fits, so no box is cut and every page is filled as far as possible. The browser's print dialog saves
// it ("Als PDF sichern"); the suggested file name comes from the page title.
const GRAPH_PDF_KEY = "schaltschrank-graph-pdf";
const GRAPH_PDF_DEFAULTS = { paper: "A4", orientation: "portrait", margin: 10, scale: "fit", columns: "4", header: true, numbers: true, legend: true, review: false };
// Paper sizes in mm (portrait: width, height); the names are CSS @page sizes.
const PAPER_SIZES = { A3: [297, 420], A4: [210, 297], A5: [148, 210], letter: [216, 279] };
const GRAPH_MODE_NAMES = { fuses: "Sicherungen", loads: "Verbraucher", tree: "Leiter" };
const PX_PER_MM = 96 / 25.4;
// Heights of the header and page-number lines on each page (CSS px, unscaled).
const PRINT_HEAD = 26;
const PRINT_FOOT = 18;
// Everything that must not be cut by a page end: boxes, texts, labels on the wires.
const PRINT_BOXES = ".graph-card, .graph-klemmen-box, .graph-join-missing, .graph-feed-missing, .graph-feed, .graph-load-row,"
  + " .graph-load-name, .graph-review summary, .graph-review-item, .graph-legend, .graph-phase, .overview-list-heading h2,"
  + " .overview-list-heading p, .graph-group > h3, h3, text";

function graphPdfSettings() {
  try { return { ...GRAPH_PDF_DEFAULTS, ...JSON.parse(localStorage.getItem(GRAPH_PDF_KEY) || "{}") }; } catch { return { ...GRAPH_PDF_DEFAULTS }; }
}

// Right edge of everything drawn in the view, in screen px from its left side.
function graphPrintRight(view) {
  const left = view.getBoundingClientRect().left;
  return Math.max(...[...view.querySelectorAll("*")].map((element) => element.getBoundingClientRect().right - left));
}

// Vertical extent [top, bottom] of every box that must stay whole, in view px (unscaled) from the view's top.
function graphPrintBoxes(view, zoom) {
  const top = view.getBoundingClientRect().top;
  return [...view.querySelectorAll(PRINT_BOXES)].map((element) => element.getBoundingClientRect())
    .filter((rect) => rect.height > 0 && rect.width > 0)
    .map((rect) => [(rect.top - top) / zoom, (rect.bottom - top) / zoom]);
}

// Page ranges [start, end] in view px: each page ends in the lowest gap between boxes that still fits on it.
// Only a box taller than a page (prevented by the scale) would be cut at the page end.
function graphPageCuts(boxes, total, pageHeight) {
  const cuts = [];
  for (let start = 0; start < total;) {
    let end = start + pageHeight;
    if (end < total) {
      // A cut through a box moves up to its top edge; boxes that touch (table rows) may be cut at their shared edge.
      const through = (cut) => boxes.find(([from, to]) => from < cut - 1 && to > cut + 1);
      let cut = end;
      for (let hit = through(cut); hit; hit = through(cut)) cut = hit[0] - 1;
      // Then into the middle of the gap, so neither neighbour shows a sliver on the other page.
      const above = Math.max(start, ...boxes.filter(([, to]) => to <= cut + 1).map(([, to]) => to));
      if (cut > start + 1) end = Math.max((above + cut) / 2, Math.min(cut, start + 2));
    } else {
      end = total;
    }
    cuts.push([start, end]);
    start = end;
  }
  return cuts;
}

// Places the lines broken at the column limit: each `graph-wrap` moves left so its first card stands in column 2,
// and down below the cards of its row; its wire runs from the card's right side down, back left above the new
// line and into it (`graph-wrap-out` and the wrap's pseudo elements, positioned by custom properties).
function graphFixWraps(root) {
  root.querySelectorAll(".graph-wrap").forEach((wrap) => {
    const node = wrap.parentElement;
    const card = node.firstElementChild.getBoundingClientRect();
    const col = Number(wrap.dataset.wrapCol);
    const home = Number(wrap.dataset.wrapHome);
    const gap = parseFloat(getComputedStyle(wrap).getPropertyValue("--graph-gap")) || 22;
    // The new line's first card goes under the row's card in the home column (columns differ in width); the cards
    // of the row from the home column on must stay clear above it.
    let lowest = card.bottom;
    let homeLeft = card.left;
    for (let up = node, steps = col; steps > home; steps -= 1) {
      up = up.parentElement.closest(".graph-node");
      if (!up) break;
      const rect = up.firstElementChild.getBoundingClientRect();
      homeLeft = rect.left;
      lowest = Math.max(lowest, rect.bottom);
    }
    wrap.style.marginLeft = `${homeLeft - card.left - 2 * gap}px`;
    // The wire back runs 8px below the row; below it room for the PE/N wires into the new line (drawGraphRails).
    wrap.style.marginTop = `${lowest - card.bottom + 30}px`;
    const box = wrap.getBoundingClientRect();
    // Into the first follower at its own lane (lifted below N/PE rows, graphLiftStacks).
    const first = wrap.querySelector(":scope > .graph-children > .graph-child");
    const stub = first.getBoundingClientRect().top - box.top + parseFloat(getComputedStyle(first, "::before").top);
    // The wire leaves at the node's lane, where its other wires leave too (Sicherungen view; below the PE/N rows of a
    // Klemmen box: at its L Klemme, graphLiftStacks), else at the middle of the card.
    const exit = node.closest(".graph-top")
      ? node.getBoundingClientRect().top + parseFloat(getComputedStyle(node).getPropertyValue("--lane")) - 3
      : card.top + card.height / 2;
    const mid = -22;
    wrap.style.setProperty("--wrap-card", `${card.right - box.left}px`);
    wrap.style.setProperty("--wrap-lane", `${exit - box.top}px`);
    wrap.style.setProperty("--wrap-mid", `${mid}px`);
    wrap.style.setProperty("--wrap-stub", `${stub}px`);
  });
}

// Sizes of every block box of the graph as laid out on screen, in document order; null for inline-level boxes
// (text, badges: they may grow with the print font without moving anything) and SVG.
// Inside a box (card text, table cell) nothing is fixed: the box keeps its size, its text may just run a bit wider.
const PRINT_LEAF_BOXES = ".graph-card-main, .graph-feed, .graph-load-name, .graph-join-missing, .graph-feed-missing, .graph-review-item";
// Sizes keep their fractions (computed border-box size; offsetWidth/Height are rounded, and the rounding of many rows
// adds up, so the copies drifted away from the wires drawn for the original by a few px further down).
function graphBoxSizes(elements) {
  return elements.map((element) => {
    if (!(element instanceof HTMLElement) || element.parentElement?.closest(PRINT_LEAF_BOXES)) return null;
    const style = getComputedStyle(element);
    return style.display.startsWith("inline") ? null : [parseFloat(style.width) || 0, parseFloat(style.height) || 0];
  });
}

// Gives every box of a page copy its measured size: the browser measures text a little differently when it prints,
// a title would wrap otherwise and push everything below it away from the wires drawn for the screen layout.
function graphFreezeBoxes(elements, sizes) {
  elements.forEach((element, index) => {
    const size = sizes[index];
    if (!size) return;
    element.style.boxSizing = "border-box";
    element.style.width = `${size[0]}px`;
    element.style.height = `${size[1]}px`;
    element.style.minHeight = "0";
    element.style.flex = "none";
  });
}

// One line about the export on top of every page: what is shown, with filter and location.
function graphPrintCaption() {
  const parts = [`Gesamtgraph · ${GRAPH_MODE_NAMES[graphState.mode] || ""}`];
  if (graphState.location) parts.push(`Ort: ${graphState.location}`);
  if (graphState.filter) parts.push(`Filter: „${graphState.filter}“`);
  if (graphState.flaggedOnly) parts.push("nur Auffälligkeiten");
  return parts.join(" · ");
}

// Builds the print layout (body class, scale, pages, @page rule) and returns a function that removes it again.
function graphPrintLayout(settings) {
  const [paperWidth, paperHeight] = PAPER_SIZES[settings.paper] || PAPER_SIZES.A4;
  const [pageWidth, pageHeight] = settings.orientation === "landscape" ? [paperHeight, paperWidth] : [paperWidth, paperHeight];
  const margin = Math.min(Math.max(Number(settings.margin) || 0, 0), 30);
  // Printable area in CSS px; a little reserve against rounding, else the browser breaks a sheet onto a 2nd page.
  const width = Math.floor((pageWidth - 2 * margin) * PX_PER_MM * 0.98);
  const height = Math.floor((pageHeight - 2 * margin) * PX_PER_MM * 0.98);
  const room = height - (settings.header ? PRINT_HEAD : 0) - (settings.numbers ? PRINT_FOOT : 0);
  // Print layout: nothing redraws the wires meanwhile.
  graphRailObserver.disconnect();
  // The graph is drawn anew for paper: print colours, rows broken at the column limit.
  graphState.printColumns = Number(settings.columns) || 0;
  graphState.printing = true;
  renderGraph();
  graphRailObserver.disconnect();
  const review = document.querySelector("#graph-review");
  const reviewOpen = review.open;
  if (settings.review) review.open = true;
  document.body.classList.add("graph-printing");
  document.body.classList.toggle("graph-print-review", Boolean(settings.review));
  document.body.classList.toggle("graph-print-no-legend", !settings.legend);
  const view = document.querySelector("#graph-view");
  // The wire layers still have their screen size; without them the view measures the graph itself.
  view.querySelectorAll(".graph-rail-svg").forEach((svg) => { svg.setAttribute("width", 0); svg.setAttribute("height", 0); });
  // The graph is laid out and measured at full size and only drawn smaller (`transform: scale`): a CSS zoom would
  // shrink the fonts, and the browser lays small fonts out differently when it prints (minimum font size), so the
  // boxes would no longer match the wires measured before.
  view.style.zoom = "1";
  graphFixWraps(view);
  drawGraphRails();
  // Drawing lifts cards below Klemmen boxes and N/PE rows (graphLiftStacks): the wrap wires are measured again at
  // the new lanes, then the wires are drawn for that layout.
  graphFixWraps(view);
  drawGraphRails();
  const boxes = graphPrintBoxes(view, 1);
  // Scale: the chosen one, at most the page width (by the drawn right edge), and the tallest box fits on a page.
  const chosen = settings.scale === "fit" ? 1 : Number(settings.scale) / 100 || 1;
  const tallest = Math.max(...boxes.map(([from, to]) => to - from));
  const scale = Math.min(chosen, width / graphPrintRight(view), room * 0.95 / tallest);
  // The graph ends with its last box (plus the view's padding), so no empty page follows.
  const total = Math.max(...boxes.map(([, to]) => to)) + 18;
  const cuts = graphPageCuts(boxes, total, room / scale);
  // Every page: header, its clipped copy of the graph (shifted to its part, drawn at the print scale), page number.
  const pages = document.createElement("div");
  pages.className = "graph-print-pages";
  const content = [...view.children];
  const all = (roots) => roots.flatMap((root) => [root, ...root.querySelectorAll("*")]);
  const sizes = graphBoxSizes(all(content));
  const caption = graphPrintCaption();
  const date = new Date().toLocaleDateString("de-DE");
  cuts.forEach(([start, end], index) => {
    const sheet = document.createElement("div");
    sheet.className = "graph-print-sheet";
    sheet.style.width = `${width}px`;
    if (settings.header) sheet.insertAdjacentHTML("beforeend", `<div class="graph-print-head"><span>${esc(caption)}</span><span>Stand ${esc(date)}</span></div>`);
    const clip = document.createElement("div");
    clip.className = "graph-print-clip";
    clip.style.height = `${Math.ceil((end - start) * scale)}px`;
    const shifted = document.createElement("div");
    shifted.className = "graph-print-shift";
    shifted.style.transform = `scale(${scale}) translateY(${-start}px)`;
    const copies = content.map((child) => child.cloneNode(true));
    shifted.append(...copies);
    graphFreezeBoxes(all(copies), sizes);
    shifted.querySelectorAll("[id]").forEach((element) => element.removeAttribute("id"));
    clip.append(shifted);
    sheet.append(clip);
    if (settings.numbers) sheet.insertAdjacentHTML("beforeend", `<div class="graph-print-foot">Seite ${index + 1} / ${cuts.length}</div>`);
    pages.append(sheet);
  });
  view.append(pages);
  view.style.zoom = "";
  const page = document.createElement("style");
  page.id = "graph-print-page";
  page.textContent = `@page { size: ${settings.paper} ${settings.orientation}; margin: ${margin}mm; }`;
  document.head.append(page);
  const title = document.title;
  document.title = "Gesamtgraph";
  return () => {
    pages.remove();
    page.remove();
    review.open = reviewOpen;
    document.title = title;
    document.body.classList.remove("graph-printing", "graph-print-review", "graph-print-no-legend");
    // Back to the screen graph (screen colours, no line breaks).
    graphState.printColumns = 0;
    graphState.printing = false;
    renderGraph();
  };
}

function printGraph(settings) {
  const restore = graphPrintLayout(settings);
  window.addEventListener("afterprint", restore, { once: true });
  window.print();
}

// "Als PDF …" opens the settings dialog with the last choice; "PDF erstellen" saves it and prints.
const graphPdfDialog = document.querySelector("#graph-pdf-dialog");
const graphPdfForm = document.querySelector("#graph-pdf-form");
document.querySelector("#graph-pdf").addEventListener("click", () => {
  const settings = graphPdfSettings();
  Object.entries(settings).forEach(([name, value]) => {
    const field = graphPdfForm.elements[name];
    if (!field) return;
    if (field.type === "checkbox") field.checked = Boolean(value);
    else field.value = String(value);
  });
  graphPdfDialog.showModal();
});
graphPdfForm.addEventListener("submit", (event) => {
  event.preventDefault();
  const form = graphPdfForm.elements;
  const settings = { paper: form.paper.value, orientation: form.orientation.value, margin: Number(form.margin.value), scale: form.scale.value, columns: form.columns.value,
    header: form.header.checked, numbers: form.numbers.checked, legend: form.legend.checked, review: form.review.checked };
  try { localStorage.setItem(GRAPH_PDF_KEY, JSON.stringify(settings)); } catch { /* the choice is only a convenience */ }
  graphPdfDialog.close();
  // Print after the dialog has closed, so it is not part of the printout.
  requestAnimationFrame(() => printGraph(settings));
});
// Sticky bottom scrollbar mirrors the sideways scroll of the graph canvas.
const graphCanvasNode = document.querySelector("#graph-canvas");
const graphBarNode = document.querySelector("#graph-hscroll");
function syncGraphScrollbar() {
  graphBarNode.firstElementChild.style.width = `${graphCanvasNode.scrollWidth}px`;
  graphBarNode.hidden = graphCanvasNode.scrollWidth <= graphCanvasNode.clientWidth;
  graphBarNode.scrollLeft = graphCanvasNode.scrollLeft;
}
graphBarNode.addEventListener("scroll", () => { graphCanvasNode.scrollLeft = graphBarNode.scrollLeft; });
graphCanvasNode.addEventListener("scroll", () => { graphBarNode.scrollLeft = graphCanvasNode.scrollLeft; });
window.addEventListener("resize", () => {
  if (graphNode.hidden) return;
  syncGraphScrollbar();
  drawGraphRails();
});
document.querySelector("#graph-rail-toggles").addEventListener("change", (event) => {
  const box = event.target.closest("[data-graph-rail]");
  if (!box) return;
  graphState.rails[box.dataset.graphRail] = box.checked;
  drawGraphRails();
});
document.querySelector("#graph-filter").addEventListener("input", (event) => {
  graphState.filter = event.target.value;
  renderGraph();
});
document.querySelector("#graph-location").addEventListener("change", (event) => {
  graphState.location = event.target.value;
  renderGraph();
});
document.querySelector("#graph-flagged").addEventListener("change", (event) => {
  graphState.flaggedOnly = event.target.checked;
  renderGraph();
});
graphNode.addEventListener("click", (event) => {
  const depth = event.target.closest("[data-graph-depth]");
  const mode = event.target.closest("[data-graph-mode]");
  const jump = event.target.closest("[data-graph-jump]");
  const focus = event.target.closest("[data-graph-focus]");
  const toggle = event.target.closest("[data-graph-toggle]");
  const card = event.target.closest("[data-type][data-id]");
  if (focus) {
    focusGraphTerminal(focus.dataset.graphFocus, focus.dataset.graphFocusLabel);
    return;
  }
  if (jump) {
    document.querySelector(`#graph-conductor-${CSS.escape(jump.dataset.graphJump)}`)?.scrollIntoView({ behavior: "smooth", block: "start" });
    return;
  }
  if (depth) applyGraphDepth(Number(depth.dataset.graphDepth));
  else if (mode) graphState.mode = mode.dataset.graphMode;
  else if (toggle) {
    const id = Number(toggle.dataset.graphToggle);
    if (!graphState.collapsed.delete(id)) graphState.collapsed.add(id);
  } else if (card) {
    openRecord(card.dataset.type, card.dataset.id);
    return;
  } else return;
  renderGraph();
});
overviewItemsNode.addEventListener("click", (event) => {
  if (event.target.closest("[data-review-type]")) return;
  const editLocation = event.target.closest("[data-edit-location]");
  if (editLocation) {
    openLocationEditor(currentOverviewItems.find((item) => item.id === editLocation.dataset.editLocation));
    return;
  }
  const button = event.target.closest("[data-type][data-id]");
  if (!button) return;
  if (button.dataset.type === "location") openLocationTerminals(button.dataset.location);
  else openRecord(button.dataset.type, button.dataset.id);
});
overviewItemsNode.addEventListener("change", async (event) => {
  const checkbox = event.target.closest('[data-review-type="terminal"]');
  if (!checkbox) return;
  const requestedState = checkbox.checked;
  checkbox.disabled = true;
  try {
    const result = await postJson("api/review", { type: "terminal", id: checkbox.dataset.reviewId, checked: requestedState });
    checkbox.closest(".review-control").querySelector(".review-timestamp").textContent = result.reviewed_at
      ? `Am ${reviewDateLabel(result.reviewed_at)}`
      : "Noch nicht überprüft";
    const item = currentOverviewItems.find((candidate) => candidate.id === result.id);
    if (item) item.reviewed_at = result.reviewed_at;
  } catch (error) {
    checkbox.checked = !requestedState;
    notify(error.message, true);
  } finally {
    checkbox.disabled = false;
  }
});
document.querySelector("#detail-badge").addEventListener("click", (event) => {
  const button = event.target.closest("[data-type][data-id]");
  if (button) openRecord(button.dataset.type, button.dataset.id);
});
window.addEventListener("resize", () => {
  drawAllGraphWires();
  scheduleFuseOverviewLayout();
});
window.addEventListener("scroll", scheduleFuseOverviewLayout, { passive: true });
window.addEventListener("popstate", async (event) => {
  const viewState = event.state?.schaltschrank;
  if (viewState?.view === "search") {
    await restoreSearchPage(viewState);
    return;
  }
  if (viewState?.view === "settings") {
    showSettings(false);
    restoreHistoryScroll(event.state?.scrollY);
    return;
  }
  if (viewState?.view === "record") {
    searchInput.value = "";
    clearReviewSearchTerm();
    await openRecord(viewState.type, viewState.id, false);
    restoreHistoryScroll(event.state?.scrollY);
    return;
  }
  if (viewState?.view === "overview") {
    searchInput.value = "";
    clearReviewSearchTerm();
    if (viewState.category === "terminals") selectedTerminalLocation = viewState.location || "";
    await openOverview(viewState.category, false);
    restoreHistoryScroll(event.state?.scrollY);
    return;
  }
  if (viewState?.view === "graph") {
    searchInput.value = "";
    clearReviewSearchTerm();
    await openGraph(false);
    restoreHistoryScroll(event.state?.scrollY);
    return;
  }
  // An entry without state comes from a link or a typed address (#terminal/14).
  if (!viewState) {
    openFromAddress();
    return;
  }
  showHome(false);
  restoreHistoryScroll(event.state?.scrollY);
});
// Browsers that report a typed address only as hashchange: popstate has not stored a view for it yet.
window.addEventListener("hashchange", () => {
  if (!history.state?.schaltschrank) openFromAddress();
});
detailNode.addEventListener("change", async (event) => {
  const checkbox = event.target.closest("[data-review-type]");
  if (!checkbox) return;
  const requestedState = checkbox.checked;
  checkbox.disabled = true;
  try {
    const result = await postJson("api/review", { type: checkbox.dataset.reviewType, id: checkbox.dataset.reviewId, checked: requestedState });
    const timestamp = checkbox.closest(".review-control").querySelector(".review-timestamp");
    timestamp.textContent = result.reviewed_at ? `Am ${reviewDateLabel(result.reviewed_at)}` : "Noch nicht überprüft";
    const { type, id } = result;
    if (currentDetail?.type === type) {
      const currentId = type === "device" ? currentDetail.record.device_id
        : type === "channel" ? `${currentDetail.record.device_id}:${currentDetail.record.object_number}`
          : String(currentDetail.record.id);
      if (currentId === id) currentDetail.record.reviewed_at = result.reviewed_at;
    }
    if (type === "terminal" && currentDetail?.type === "fuse") {
      currentDetail.terminals.forEach((terminal) => {
        if (String(terminal.id) === id) terminal.reviewed_at = result.reviewed_at;
      });
    }
    if (type === "channel" && currentDetail?.type === "fuse") {
      currentDetail.terminals.forEach((terminal) => {
        if (`${terminal.device_id}:${terminal.object_number}` === id) terminal.channel_reviewed_at = result.reviewed_at;
      });
    }
    if (type === "channel" && currentDetail?.type === "device") {
      const [deviceId, objectNumber] = id.split(":", 2);
      const selectedChannel = currentDetail.channels.find((channel) => channel.device_id === deviceId && channel.object_number === objectNumber);
      const physicalChannel = selectedChannel?.physical_channel;
      currentDetail.channels.forEach((channel) => {
        if (channel.device_id === deviceId && (physicalChannel
          ? channel.physical_channel === physicalChannel
          : channel.object_number === objectNumber)) channel.reviewed_at = result.reviewed_at;
      });
    }
  } catch (error) {
    checkbox.checked = !requestedState;
    notify(error.message, true);
  } finally {
    checkbox.disabled = false;
  }
});
detailNode.addEventListener("click", async (event) => {
  if (event.target.closest("[data-review-type]")) return;
  if (event.target.closest("[data-edit-terminal]") && currentDetail?.type === "terminal") {
    fillTerminalEdit(currentDetail.record);
    return;
  }
  const editFuse = event.target.closest("[data-edit-fuse]");
  if (editFuse && currentDetail?.type === "fuse") {
    await openFuseEditor(currentDetail.record);
    return;
  }
  if (event.target.closest("[data-edit-fi]") && currentDetail?.type === "fi") {
    openFiEditor(currentDetail.record);
    return;
  }
  if (event.target.closest("[data-edit-device]") && currentDetail?.type === "device") {
    openDeviceEditor(currentDetail.record);
    return;
  }
  const objectToggle = event.target.closest("[data-toggle-ets-objects]");
  if (objectToggle) {
    const expanded = objectToggle.getAttribute("aria-expanded") !== "true";
    objectToggle.setAttribute("aria-expanded", String(expanded));
    objectToggle.setAttribute("aria-label", expanded ? "ETS-Objekte ausblenden" : "ETS-Objekte anzeigen");
    objectToggle.querySelector(".ets-object-toggle-mark").textContent = expanded ? "−" : "+";
    objectToggle.closest(".channel-tree-branch").querySelector(".ets-object-list").hidden = !expanded;
    return;
  }
  const editManualChannel = event.target.closest("[data-edit-manual-channel]");
  if (editManualChannel) {
    const form = document.querySelector("#device-channel-form");
    form.reset();
    form.elements.device_id.value = editManualChannel.dataset.deviceId;
    form.elements.object_number.value = editManualChannel.dataset.objectNumber;
    form.elements.physical_channel.value = editManualChannel.dataset.physicalChannel;
    form.elements.name.value = editManualChannel.dataset.name;
    document.querySelector("#device-channel-title").textContent = "Kanal bearbeiten";
    document.querySelector("#device-channel-submit").textContent = "Änderungen speichern";
    document.querySelector("#delete-device-channel").hidden = false;
    document.querySelector("#device-channel-error").textContent = "";
    deviceChannelDialog.showModal();
    form.elements.physical_channel.focus();
    return;
  }
  const link = event.target.closest("[data-type][data-id]");
  if (link) {
    openRecord(link.dataset.type, link.dataset.id);
    return;
  }
  const addManualChannel = event.target.closest("[data-add-manual-channel]");
  if (addManualChannel) {
    const form = document.querySelector("#device-channel-form");
    form.reset();
    form.elements.device_id.value = addManualChannel.dataset.addManualChannel;
    form.elements.object_number.value = "";
    document.querySelector("#device-channel-title").textContent = "Kanal hinzufügen";
    document.querySelector("#device-channel-submit").textContent = "Kanal anlegen";
    document.querySelector("#delete-device-channel").hidden = true;
    document.querySelector("#device-channel-error").textContent = "";
    deviceChannelDialog.showModal();
    form.elements.physical_channel.focus();
    return;
  }
  const removeChannelLink = event.target.closest("[data-delete-channel-link]");
  if (removeChannelLink && confirm("Diese Klemmen-Verbindung mit dem Gerätekanal entfernen?")) {
    try {
      await postJson("api/channel-links/delete", {
        terminal_id: removeChannelLink.dataset.terminalId,
        device_id: removeChannelLink.dataset.deviceId,
        physical_channel: removeChannelLink.dataset.physicalChannel,
      });
      notify("Klemmen-Verbindung entfernt.");
      if (currentDetail?.type === "terminal") openRecord("terminal", currentDetail.record.id);
      loadStats();
    } catch (error) { notify(error.message, true); }
    return;
  }
  const assign = event.target.closest("[data-assign-device]");
  if (assign) {
    fillAssignment(assign.dataset.assignDevice, assign.dataset.assignChannel || "", assign.dataset.assignManualChannel === "true");
    return;
  }
  const pathAdd = event.target.closest("[data-path-add]");
  if (pathAdd) {
    openPathAddDialog(pathAdd.dataset);
    return;
  }
  const pathInsert = event.target.closest("[data-path-insert]");
  if (pathInsert) {
    openPathAddDialog({ insertNodeId: pathInsert.dataset.pathInsert, nodeType: pathInsert.dataset.nodeType });
    return;
  }
  const remove = event.target.closest("[data-delete-assignment]");
  if (remove && confirm("Diese Klemmen-Zuordnung entfernen?")) {
    try {
      await postJson("api/assignments/delete", { assignment_id: remove.dataset.deleteAssignment });
      notify("Zuordnung entfernt.");
      if (currentPageState?.view === "record") refreshRecord();
      loadStats();
    } catch (error) { notify(error.message, true); }
  }
});

document.querySelector("#add-terminal").addEventListener("click", () => fillAssignment());
document.querySelector("#add-terminal-overview").addEventListener("click", fillNewTerminal);
pathAddForm.addEventListener("change", (event) => {
  if (event.target.name === "kind") setPathAddKind(event.target.value);
});
pathAddForm.addEventListener("input", (event) => {
  const name = event.target.name;
  if (name === "channel_search") {
    // Typing in the search drops a picked channel and searches again.
    clearPickedChannel();
    clearTimeout(channelLookupTimer);
    channelLookupTimer = setTimeout(loadChannelOptions, 180);
  } else if (name === "device_name" || name === "channel_name") {
    // Entering a new device/channel replaces the picked one.
    clearPickedChannel();
    pathAddForm.elements.channel_search.value = "";
  } else if (name === "load_search") {
    // Same for the Verbraucher: typing drops the picked one and searches again.
    pathAddForm.elements.load_id.value = "";
    clearTimeout(channelLookupTimer);
    channelLookupTimer = setTimeout(loadLoadOptions, 180);
  } else if (name === "load_name") {
    pathAddForm.elements.load_id.value = "";
    pathAddForm.elements.load_search.value = "";
  }
});
pathAddForm.addEventListener("click", (event) => {
  const load = event.target.closest("[data-path-load]");
  if (load) {
    pathAddForm.elements.load_id.value = load.dataset.loadId;
    pathAddForm.elements.load_search.value = load.dataset.title;
    pathAddForm.elements.load_name.value = "";
    document.querySelector("[data-path-load-options]").hidden = true;
    return;
  }
  const option = event.target.closest("[data-path-channel]");
  if (!option) return;
  pathAddForm.elements.device_id.value = option.dataset.deviceId;
  pathAddForm.elements.object_number.value = option.dataset.objectNumber;
  pathAddForm.elements.channel_search.value = option.dataset.title;
  pathAddForm.elements.device_name.value = "";
  pathAddForm.elements.channel_name.value = "";
  document.querySelector("[data-path-channel-options]").hidden = true;
});
pathAddForm.addEventListener("submit", async (event) => {
  event.preventDefault();
  const fields = Object.fromEntries(new FormData(pathAddForm).entries());
  const payloads = {
    terminal: { location: fields.path_location, terminal_number: fields.path_terminal_number, position: fields.path_position, wire_color: fields.path_wire_color },
    channel: { device_id: fields.device_id, object_number: fields.object_number, device_name: fields.device_name, channel_name: fields.channel_name },
    load: { load_id: fields.load_id, load_name: fields.load_name },
  };
  const payload = payloads[fields.kind];
  try {
    // A Klemme as the missing N/PE (or L) feed of a Verbraucher.
    if (fields.feed_role) {
      const data = await postJson("api/loads/feed", { load_id: fields.feed_load_id, role: fields.feed_role, conductor: fields.feed_conductor,
        replace_node_id: fields.replace_node_id, ...payloads.terminal });
      if (fields.path_location) localStorage.setItem(lastLocationKey, fields.path_location);
      pathAddDialog.close();
      notify(data.terminal_id === null ? (data.already_exists ? `Der Verbraucher ist bereits direkt an ${data.conductor}.` : `Verbraucher direkt mit ${data.conductor} verbunden.`)
        : data.already_exists ? "Diese Klemme speist den Verbraucher bereits."
        : `${fields.feed_role}-Klemme ${data.terminal_created ? "angelegt und " : ""}zugewiesen${data.conductor ? ` (Leiter ${data.conductor})` : ""}.`);
      loadStats();
      await refreshRecord();
      return;
    }
    const data = await postJson("api/terminals/extend", { terminal_id: fields.terminal_id, node_id: fields.node_id, fuse_id: fields.fuse_id, insert_node_id: fields.insert_node_id, kind: fields.kind, ...payload });
    if (fields.kind === "terminal") {
      localStorage.setItem(lastLocationKey, fields.path_location);
      loadPicklists(fields.path_location);
    }
    pathAddDialog.close();
    notify(pathAddMessages[fields.kind](data));
    loadStats();
    // The page the dialog was opened from (Klemme or Sicherung) shows the new entry.
    if (currentPageState.view === "record") await refreshRecord();
    else openRecord("terminal", fields.terminal_id);
  } catch (error) { document.querySelector("#path-add-error").textContent = error.message; }
});
document.querySelector("#assignment-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const form = new FormData(event.currentTarget);
  const payload = Object.fromEntries(form.entries());
  payload.used = event.currentTarget.elements.used.checked;
  const editingTerminal = Boolean(payload.terminal_id);
  const creatingTerminal = assignmentMode === "terminal";
  try {
    const endpoint = editingTerminal ? "api/terminals/update" : creatingTerminal ? "api/terminals/create" : "api/assignments";
    const data = await postJson(endpoint, payload);
    localStorage.setItem(lastLocationKey, payload.location);
    loadPicklists(payload.location);
    assignmentDialog.close();
    notify(editingTerminal ? "Klemme gespeichert." : creatingTerminal ? "Klemme angelegt." : data.already_assigned ? "Diese Zuordnung besteht bereits." : "Klemme zugeordnet.");
    loadStats();
    if (editingTerminal) {
      openRecord("terminal", payload.terminal_id);
    } else if (creatingTerminal) {
      openRecord("terminal", data.terminal_id);
    } else if (currentPageState?.view === "record") {
      refreshRecord();
    }
  } catch (error) { document.querySelector("#assignment-error").textContent = error.message; }
});
document.querySelector("#fuse-edit-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const formData = new FormData(event.currentTarget);
  const payload = Object.fromEntries(formData.entries());
  payload.phases = formData.getAll("phases");
  try {
    const result = await postJson("api/fuses/update", payload);
    if (result.created && payload.location) localStorage.setItem(lastLocationKey, payload.location);
    fuseEditDialog.close();
    notify(result.created ? "Sicherung angelegt." : "Sicherung gespeichert.");
    if (result.id) openRecord("fuse", result.id);
    else if (currentDetail?.type === "fuse") openRecord("fuse", currentDetail.record.id);
    loadStats();
  } catch (error) {
    document.querySelector("#fuse-edit-error").textContent = error.message;
  }
});

document.querySelector("#delete-terminal-button").addEventListener("click", async () => {
  const record = currentDetail?.type === "terminal" ? currentDetail.record : null;
  if (!record) return;
  const label = formatTerminalLabel(record.terminal_number || record.label, record.position);
  const result = await confirmedPost(
    `Klemme ${label} (${record.location}) wirklich löschen?\n\n`
    + "Alle Verbindungen der Klemme (Sicherung, Leiter, Gerät-Kanäle, Verbraucher) werden entfernt. "
    + "Klemmen dahinter beginnen einen eigenen Pfad; Verbraucher, die nur diese Klemme speist, werden gelöscht.\n\n"
    + "Diese Aktion kann nicht rückgängig gemacht werden.",
    "api/terminals/delete", { terminal_id: record.id });
  if (!result) return;
  assignmentDialog.close();
  notify(`Klemme ${label} gelöscht.`);
  loadStats();
  openLocationTerminals(record.location);
});

document.querySelector("#delete-fuse-button").addEventListener("click", async () => {
  const form = document.querySelector("#fuse-edit-form");
  const fuseId = form.elements.fuse_id.value;
  if (!fuseId) return;
  const deleteButton = document.querySelector("#delete-fuse-button");
  deleteButton.disabled = true;
  try {
    const data = await request(`api/record?type=fuse&id=${encodeURIComponent(fuseId)}`);
    const activeConnections = new Set(data.terminals.map((terminal) => String(terminal.id))).size;
    const fuseName = data.record.label || form.elements.label.value || "Sicherung";
    const confirmed = confirm(
      `Sicherung ${fuseName} wirklich löschen?\n\n`
      + `Aktive Verbindungen: ${activeConnections} Klemmen.\n`
      + "Die Klemmen bleiben erhalten und werden von dieser Sicherung getrennt. "
      + "Kanalzuordnungen, Klemmenverbindungen und das FI-Gerät bleiben erhalten; die Phasen dieser Sicherung werden gelöscht.\n\n"
      + "Diese Aktion kann nicht rückgängig gemacht werden."
    );
    if (!confirmed) return;
    const result = await postJson("api/fuses/delete", { fuse_id: fuseId, expected_connections: activeConnections });
    fuseEditDialog.close();
    notify(`Sicherung ${result.label} gelöscht; ${result.active_connections} Klemmen-Verbindungen getrennt.`);
    if (currentDetail?.type === "fuse" && String(currentDetail.record.id) === String(result.id)) showHome();
    loadStats();
  } catch (error) {
    document.querySelector("#fuse-edit-error").textContent = error.message;
  } finally {
    deleteButton.disabled = false;
  }
});

document.querySelector("#fuse-edit-form").elements.existing_fi_display.addEventListener("input", (event) => {
  const form = event.currentTarget.form;
  const selected = autocompleteOptions.fiDevices.find((item) => String(item.id) === form.elements.existing_fi_id.value);
  const selectedLabel = selected ? [selected.number ? `FI ${selected.number}` : "FI", selected.label].filter(Boolean).join(" · ") : "";
  if (selected && event.currentTarget.value !== selectedLabel) {
    form.elements.existing_fi_id.value = "";
    form.elements.fi_number.value = "";
    form.elements.fi_label.value = "";
    form.elements.fi_number.readOnly = false;
    form.elements.fi_label.readOnly = false;
    document.querySelector("#existing-fi-edit-controls").hidden = true;
  }
});

document.querySelector("#fuse-edit-form").elements.existing_fi_display.addEventListener("change", (event) => {
  const form = event.currentTarget.form;
  const selected = autocompleteOptions.fiDevices.find((item) => String(item.id) === form.elements.existing_fi_id.value);
  if (!selected) return;
  form.elements.fi_number.value = selected.number || "";
  form.elements.fi_label.value = selected.label || "";
  form.elements.fi_number.readOnly = true;
  form.elements.fi_label.readOnly = true;
  const editControls = document.querySelector("#existing-fi-edit-controls");
  editControls.hidden = false;
  editControls.querySelector("#edit-existing-fi").textContent = "FI-Daten bearbeiten";
  editControls.querySelector(".existing-fi-warning").hidden = true;
});

document.querySelector("#edit-existing-fi").addEventListener("click", (event) => {
  const form = document.querySelector("#fuse-edit-form");
  const selected = autocompleteOptions.fiDevices.find((item) => String(item.id) === form.elements.existing_fi_id.value);
  const numberInput = form.elements.fi_number;
  const labelInput = form.elements.fi_label;
  const warning = document.querySelector("#existing-fi-edit-controls .existing-fi-warning");
  if (!selected) return;
  const editing = numberInput.readOnly;
  numberInput.readOnly = !editing;
  labelInput.readOnly = !editing;
  event.currentTarget.textContent = editing ? "Änderungen verwerfen" : "FI-Daten bearbeiten";
  warning.hidden = !editing;
  if (!editing) {
    numberInput.value = selected.number || "";
    labelInput.value = selected.label || "";
  }
});

document.querySelector("#assignment-form").elements.location.addEventListener("input", (event) => {
  loadPicklists(event.currentTarget.value);
  if (event.currentTarget.form.elements.terminal_number.value.trim()) scheduleAssignmentTerminalSuggestions();
});
document.querySelector("#fuse-edit-form").elements.location.addEventListener("change", (event) => {
  const form = event.currentTarget.form;
  form.elements.existing_fi_display.value = "";
  form.elements.existing_fi_id.value = "";
  form.elements.fi_number.value = "";
  form.elements.fi_label.value = "";
  loadFuseFiOptions(event.currentTarget.value);
});
// Klemme lookup, wire color and location handling of the path-add dialog.
document.querySelectorAll("#path-add-form").forEach((form) => {
  form.addEventListener("input", (event) => {
    const side = event.target.dataset.terminalSide;
    if (side) {
      form.elements[`${side}_position`].value = "";
      setTerminalPickerColor(side, "");
      scheduleTerminalPickerOptions(side);
    } else if (event.target.name?.endsWith("_location")) {
      loadPicklists();
      scheduleTerminalPickerOptions(event.target.name.split("_", 1)[0]);
    }
  });
  form.addEventListener("click", (event) => {
    const option = event.target.closest("[data-terminal-choice]");
    if (!option) return;
    const side = option.dataset.terminalChoice;
    form.elements[`${side}_location`].value = option.dataset.location;
    form.elements[`${side}_terminal_number`].value = option.dataset.number;
    form.elements[`${side}_position`].value = option.dataset.position;
    setTerminalPickerColor(side, option.dataset.color);
    document.querySelector(`[data-terminal-options="${side}"]`).hidden = true;
  });
});
document.querySelectorAll("[data-close]").forEach((button) => button.addEventListener("click", () => document.getElementById(button.dataset.close).close()));
document.querySelector("#open-import").addEventListener("click", () => importDialog.showModal());
document.querySelector("#xlsx-file").addEventListener("change", (event) => {
  const file = event.target.files[0];
  document.querySelector("#file-name").textContent = file?.name || "Keine Datei ausgewählt";
  document.querySelector("#preview-import").disabled = !file;
  document.querySelector("#import-error").textContent = "";
});
document.querySelector("#preview-import").addEventListener("click", async () => {
  const file = document.querySelector("#xlsx-file").files[0];
  if (!file) return;
  if (file.size > 10 * 1024 * 1024) {
    document.querySelector("#import-error").textContent = "Die Datei darf höchstens 10 MB groß sein.";
    return;
  }
  const payload = { filename: file.name, file_base64: await fileBase64(file) };
  try {
    const data = await postJson("api/import/preview", payload);
    showImportPreview(data);
  } catch (error) { document.querySelector("#import-error").textContent = error.message; }
});
document.querySelector("#back-to-upload").addEventListener("click", () => {
  document.querySelector("#import-step-preview").hidden = true;
  document.querySelector("#import-step-upload").hidden = false;
});
document.querySelector("#apply-import").addEventListener("click", async () => {
  try {
    const data = await postJson("api/import/apply", { token: importToken });
    importDialog.close();
    notify(`${data.imported} Zuordnungen gespeichert, ${data.skipped} übersprungen.`);
    loadStats();
    if (currentPageState?.view === "record") refreshRecord();
  } catch (error) { document.querySelector("#apply-error").textContent = error.message; }
});

initializeWireColorPicker();
initializeTerminalColorPickers();
initializeAutocomplete();
initializeAssignmentTerminalSuggestions();
loadStats();
loadPicklists();
// A reload keeps the view of its history entry; a new tab or a pasted link opens the page of the address.
const savedView = history.state?.schaltschrank;
// Opened from an entity dialog in Home Assistant: that entity wins over the remembered view.
if (takeHaHandoff()) { /* opens the entity's page */ }
else if (savedView?.view === "search") restoreSearchPage(savedView);
else if (savedView) openView(savedView);
else openFromAddress();
