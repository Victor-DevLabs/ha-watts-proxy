/*
 * Watts Schedule Card: graphical editor of the weekly schedule of Watts thermostats
 * (BT-CT02-RF central unit) published by the Watts Proxy add-on (0.9.0 or later;
 * 0.7.1-0.8.x also work through the old attribute names).
 *
 * It uses the text.*_schedule entities of the add-on, whose attributes are:
 *   week            the 336 characters of the schedule (Monday first, half hours; 5 = comfort, 0 = eco)
 *   command_topic   MQTT topic where the new schedule is published
 *   pending         true while the central unit has not confirmed the last change
 *   zone            zone name
 *
 * Card configuration (YAML):
 *   type: custom:watts-schedule-card
 *   entity: text.watts_zone_2_schedule     # optional: without it, a selector with every zone
 *   title: Schedules                       # optional
 *   names:                                 # optional: custom zone names
 *     text.watts_zone_2_schedule: Living room
 *
 * Saving sends the whole schedule through the same path as the text entity; the add-on
 * validates it and waits for the central unit to confirm it.
 * The language follows Home Assistant (English or Spanish).
 */

const WSC_VERSION = "0.2.0";
const SLOTS = 48;

const I18N = {
  en: {
    days: ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"],
    allEco: "all eco", comfort: "Comfort", eco: "Eco",
    hint: "Drag to paint; start on eco to set comfort, and the other way round.",
    copy: "Copy", to: "to", monFri: "Mon–Fri", satSun: "Sat–Sun", allDays: "every day",
    discard: "Discard", save: "Save", zone: "Zone", from: "Source day", targets: "Target days",
    title: "Schedules", titleZone: "Schedule {z}",
    unsavedConfirm: "There are unsaved changes in this zone. Discard them?",
    saved: "Saved: confirmed by the central unit ✓",
    notConfirmed: "The central unit did not confirm the change. The draft is still here: save again or discard.",
    sending: "Sending to the central unit… (it may take a few seconds)",
    unsaved: "Unsaved changes", pendingChange: "A change is waiting for confirmation by the central unit",
    noSchedule: "This zone has no known schedule yet (wait for the central unit to send it).",
    noZones: "No zones: the Watts Proxy add-on (0.7.1 or later) with allow_control is needed.",
    sendError: "Could not send: {e}",
    cardName: "Watts: weekly schedule",
    cardDesc: "Graphical editor of the schedule of Watts thermostats (Watts Proxy add-on).",
  },
  es: {
    days: ["Lun", "Mar", "Mié", "Jue", "Vie", "Sáb", "Dom"],
    allEco: "todo eco", comfort: "Confort", eco: "Eco",
    hint: "Arrastra para pintar; empieza sobre eco para poner confort, y al revés.",
    copy: "Copiar", to: "a", monFri: "Lun–Vie", satSun: "Sáb–Dom", allDays: "todos los días",
    discard: "Descartar", save: "Guardar", zone: "Zona", from: "Día de origen", targets: "Días de destino",
    title: "Horarios", titleZone: "Horario {z}",
    unsavedConfirm: "Hay cambios sin guardar en esta zona. ¿Descartarlos?",
    saved: "Guardado: la central lo ha confirmado ✓",
    notConfirmed: "La central no ha confirmado el cambio. El borrador sigue aquí: vuelve a guardar o descarta.",
    sending: "Enviando a la central… (puede tardar unos segundos)",
    unsaved: "Cambios sin guardar", pendingChange: "Hay un cambio pendiente de confirmar por la central",
    noSchedule: "Esta zona todavía no tiene horario conocido (espera a que la central lo envíe).",
    noZones: "No hay zonas: hace falta el complemento Watts Proxy 0.7.1 o posterior con allow_control.",
    sendError: "No se pudo enviar: {e}",
    cardName: "Watts: horario semanal",
    cardDesc: "Editor gráfico del horario de los termostatos Watts (complemento Watts Proxy).",
  },
};

function i18n(lang) {
  return I18N[(lang || "en").slice(0, 2)] || I18N.en;
}

// ------------------------------------------------------------------ pure logic (tested in Node)

function hhmm(slot) {
  if (slot >= SLOTS) return "24:00";
  return `${String(Math.floor(slot / 2)).padStart(2, "0")}:${slot % 2 ? "30" : "00"}`;
}

function dayLabel(days, names = I18N.en.days) {
  // [0,1,2,4] -> "Mon–Wed, Fri"; [5,6] -> "Sat–Sun"; [0,2] -> "Mon, Wed" (same as the add-on)
  const parts = [];
  let i = 0;
  while (i < days.length) {
    let j = i;
    while (j + 1 < days.length && days[j + 1] === days[j] + 1) j++;
    if (j - i >= 2 || (j - i === 1 && days[i] === 5)) parts.push(`${names[days[i]]}–${names[days[j]]}`);
    else for (let k = i; k <= j; k++) parts.push(names[days[k]]);
    i = j + 1;
  }
  return parts.join(", ");
}

function dayRuns(week, d, t = I18N.en) {
  const day = week.slice(d * SLOTS, (d + 1) * SLOTS);
  const runs = [];
  let i = 0;
  while (i < SLOTS) {
    if (day[i] !== "0") {
      let j = i;
      while (j < SLOTS && day[j] === day[i]) j++;
      runs.push(`${hhmm(i)}–${hhmm(j)}`);
      i = j;
    } else i++;
  }
  return runs.length ? runs.join(", ") : t.allEco;
}

function scheduleText(week, lang = "en") {
  // Same text as the Schedule entity of the add-on.
  if (typeof week !== "string" || week.length < 336) return "";
  const t = i18n(lang);
  const groups = new Map();
  for (let d = 0; d < 7; d++) {
    const r = dayRuns(week, d, t);
    if (!groups.has(r)) groups.set(r, []);
    groups.get(r).push(d);
  }
  return [...groups.entries()].map(([r, days]) => `${dayLabel(days, t.days)}: ${r}`).join("; ");
}

function copyDay(week, from, targets) {
  const src = week.slice(from * SLOTS, (from + 1) * SLOTS);
  const arr = week.split("");
  for (const d of targets) for (let i = 0; i < SLOTS; i++) arr[d * SLOTS + i] = src[i];
  return arr.join("");
}

function paint(week, a, b, value) {
  // Paint the cells from a to b (both included, any order) with value ("5" or "0").
  const arr = week.split("");
  const [lo, hi] = a <= b ? [a, b] : [b, a];
  for (let i = lo; i <= hi; i++) arr[i] = value;
  return arr.join("");
}

// Attributes, with the names used before 0.9.0 as a fallback.
function zoneAttrs(a) {
  a = a || {};
  return {
    week: a.week ?? a.semana,
    topic: a.command_topic ?? a.comando,
    pending: a.pending ?? a.orden_en_curso,
    zone: a.zone ?? a.zona,
  };
}

if (typeof module !== "undefined") module.exports = { scheduleText, copyDay, paint, dayLabel, hhmm, zoneAttrs };

// ------------------------------------------------------------------ card

const STYLE = `
  :host { display: block; }
  ha-card { padding: 16px; }
  .head { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; margin-bottom: 12px; }
  .title { font-size: 1.2em; font-weight: 500; flex: 1; min-width: 120px; color: var(--primary-text-color); }
  select, button { font: inherit; color: var(--primary-text-color); background: var(--card-background-color, #fff);
    border: 1px solid var(--divider-color, #ccc); border-radius: 6px; padding: 4px 8px; }
  button { cursor: pointer; }
  button.primary { background: var(--primary-color, #03a9f4); color: var(--text-primary-color, #fff); border-color: transparent; }
  button:disabled { opacity: .45; cursor: default; }
  .grid { display: grid; grid-template-columns: 34px repeat(48, 1fr); row-gap: 3px; touch-action: none;
    user-select: none; -webkit-user-select: none; }
  .hour { font-size: 10px; color: var(--secondary-text-color); grid-column: span 6; padding-left: 1px;
    border-left: 1px solid var(--divider-color, #ccc); }
  .day { font-size: 12px; color: var(--secondary-text-color); align-self: center; }
  .day.today { color: var(--primary-color); font-weight: 600; }
  .c { height: 26px; background: var(--wsc-eco, rgba(66,165,245,.28)); cursor: pointer; }
  .c.on { background: var(--wsc-comfort, #ff9800); }
  .c.h { margin-left: 1px; }
  .c.now { box-shadow: inset 0 0 0 2px var(--primary-text-color); }
  .c.chg { opacity: .7; background-image: repeating-linear-gradient(45deg, transparent 0 3px, rgba(255,255,255,.35) 3px 5px); }
  .legend { display: flex; gap: 14px; font-size: 12px; color: var(--secondary-text-color); margin: 10px 0 6px; flex-wrap: wrap; }
  .sw { display: inline-block; width: 12px; height: 12px; border-radius: 2px; vertical-align: -2px; margin-right: 4px; }
  .tools { display: flex; gap: 6px; align-items: center; flex-wrap: wrap; font-size: 13px;
    color: var(--secondary-text-color); margin-top: 8px; }
  .text { font-size: 12px; color: var(--secondary-text-color); margin-top: 10px; line-height: 1.4; }
  .actions { display: flex; gap: 8px; justify-content: flex-end; align-items: center; margin-top: 12px; flex-wrap: wrap; }
  .status { flex: 1; font-size: 13px; color: var(--secondary-text-color); }
  .status.ok { color: var(--success-color, #43a047); }
  .status.warn { color: var(--warning-color, #ffa000); }
  .status.err { color: var(--error-color, #db4437); }
  .empty { color: var(--secondary-text-color); }
`;

// Outside the browser (tests in Node) there is no HTMLElement.
const BaseElement = typeof HTMLElement !== "undefined" ? HTMLElement : class {};

class WattsScheduleCard extends BaseElement {
  setConfig(config) {
    this._config = config || {};
    this._zone = this._config.entity || null;
    this._draft = null;       // schedule being edited (null = no changes)
    this._saving = null;      // { week, at } while waiting for confirmation
    this._status = null;      // { key, cls } or { text, cls }
    this._built = false;
  }

  static getStubConfig() { return {}; }
  getCardSize() { return 7; }

  set hass(hass) {
    const langChanged = this._hass && this._hass.language !== hass.language;
    this._hass = hass;
    if (langChanged) { this._built = false; this._grid = null; }
    const zones = this._zones();
    if (!this._zone || !zones.includes(this._zone)) this._zone = zones[0] || null;
    this._checkSaving();
    this._render();
  }

  get _t() { return i18n(this._hass && this._hass.language); }

  _zones() {
    if (!this._hass) return [];
    return Object.keys(this._hass.states)
      .filter((id) => id.startsWith("text."))
      .filter((id) => {
        const topic = zoneAttrs(this._hass.states[id].attributes).topic;
        return typeof topic === "string" && topic.startsWith("watts_proxy/");
      })
      .sort();
  }

  _zoneName(z) {
    // Custom name from the configuration (names: {entity: name}) or the one of the central unit.
    const names = this._config.names || {};
    const s = z && this._hass ? this._hass.states[z] : null;
    return names[z] || (s && zoneAttrs(s.attributes).zone) || z || "";
  }

  _attrs() {
    const s = this._zone && this._hass ? this._hass.states[this._zone] : null;
    return zoneAttrs(s ? s.attributes : {});
  }

  _real() {
    const w = this._attrs().week;
    return typeof w === "string" && w.length >= 336 ? w.slice(0, 336) : null;
  }

  _week() { return this._draft || this._real(); }

  _checkSaving() {
    if (!this._saving) return;
    const a = this._attrs();
    const real = this._real();
    if (!a.pending && real === this._saving.week) {
      this._saving = null;
      this._draft = null;
      this._status = { key: "saved", cls: "ok" };
      clearTimeout(this._statusTimer);
      this._statusTimer = setTimeout(() => { this._status = null; this._render(); }, 10000);
    } else if (!a.pending && Date.now() - this._saving.at > 15000 && real !== this._saving.week) {
      // The add-on gave up (or rejected the schedule): the draft is kept.
      this._saving = null;
      this._status = { key: "notConfirmed", cls: "err" };
    }
  }

  // ---- building

  _render() {
    if (!this._config) return;
    if (!this.shadowRoot) this.attachShadow({ mode: "open" });
    if (!this._built) this._build();
    this._update();
  }

  _build() {
    this._built = true;
    const t = this._t;
    const root = this.shadowRoot;
    const targets = [["lv", t.monFri], ["sd", t.satSun], ["all", t.allDays]];
    root.innerHTML = `<style>${STYLE}</style>
      <ha-card>
        <div class="head">
          <div class="title"></div>
          <select class="zone" aria-label="${t.zone}"></select>
        </div>
        <div class="body"></div>
        <div class="legend">
          <span><span class="sw" style="background:var(--wsc-comfort,#ff9800)"></span>${t.comfort}</span>
          <span><span class="sw" style="background:var(--wsc-eco,rgba(66,165,245,.28))"></span>${t.eco}</span>
          <span>${t.hint}</span>
        </div>
        <div class="tools">
          ${t.copy} <select class="from" aria-label="${t.from}">${t.days.map((d, i) => `<option value="${i}">${d}</option>`).join("")}</select>
          ${t.to} <select class="to" aria-label="${t.targets}">
            ${targets.map(([k, label]) => `<option value="${k}">${label}</option>`).join("")}
            ${t.days.map((d, i) => `<option value="d${i}">${d}</option>`).join("")}
          </select>
          <button class="copy">${t.copy}</button>
        </div>
        <div class="text"></div>
        <div class="actions">
          <div class="status"></div>
          <button class="discard">${t.discard}</button>
          <button class="save primary">${t.save}</button>
        </div>
      </ha-card>`;
    root.querySelector(".zone").addEventListener("change", (e) => {
      if (this._draft && !confirm(this._t.unsavedConfirm)) {
        e.target.value = this._zone;
        return;
      }
      this._zone = e.target.value;
      this._draft = null; this._saving = null; this._status = null;
      this._grid = null;
      this._update();
    });
    root.querySelector(".copy").addEventListener("click", () => this._copy());
    root.querySelector(".discard").addEventListener("click", () => {
      this._draft = null; this._status = null; this._update();
    });
    root.querySelector(".save").addEventListener("click", () => this._save());
  }

  _buildGrid(body) {
    const days = this._t.days;
    const g = document.createElement("div");
    g.className = "grid";
    let html = `<div></div>`;
    for (let h = 0; h < 24; h += 3) html += `<div class="hour">${String(h).padStart(2, "0")}</div>`;
    for (let d = 0; d < 7; d++) {
      html += `<div class="day" data-d="${d}">${days[d]}</div>`;
      for (let s = 0; s < SLOTS; s++) {
        const i = d * SLOTS + s;
        html += `<div class="c${s && s % 6 === 0 ? " h" : ""}" data-i="${i}" title="${days[d]} ${hhmm(s)}–${hhmm(s + 1)}"></div>`;
      }
    }
    g.innerHTML = html;
    body.innerHTML = "";
    body.appendChild(g);
    this._grid = g;
    this._cells = [...g.querySelectorAll(".c")];

    const cellAt = (ev) => {
      const el = this.shadowRoot.elementFromPoint(ev.clientX, ev.clientY);
      return el && el.dataset && el.dataset.i !== undefined ? +el.dataset.i : null;
    };
    g.addEventListener("pointerdown", (ev) => {
      const i = cellAt(ev);
      const week = this._week();
      if (i === null || !week || this._saving) return;
      ev.preventDefault();
      g.setPointerCapture(ev.pointerId);
      this._paintValue = week[i] === "0" ? "5" : "0";
      this._last = i;
      this._draft = paint(week, i, i, this._paintValue);
      this._status = null;
      this._update();
    });
    g.addEventListener("pointermove", (ev) => {
      if (this._paintValue === undefined) return;
      const i = cellAt(ev);
      if (i === null || i === this._last) return;
      // Within the same day, fill the gap between the previous cell and this one (fast moves).
      const sameDay = Math.floor(i / SLOTS) === Math.floor(this._last / SLOTS);
      this._draft = paint(this._draft, sameDay ? this._last : i, i, this._paintValue);
      this._last = i;
      this._update();
    });
    const end = () => { this._paintValue = undefined; this._last = undefined; };
    g.addEventListener("pointerup", end);
    g.addEventListener("pointercancel", end);
  }

  _update() {
    const root = this.shadowRoot;
    if (!root || !this._hass) return;
    const t = this._t;
    const zones = this._zones();
    const a = this._attrs();
    root.querySelector(".title").textContent = this._config.title ||
      (this._config.entity ? t.titleZone.replace("{z}", this._zoneName(this._zone)) : t.title);

    const sel = root.querySelector(".zone");
    sel.style.display = this._config.entity || zones.length < 2 ? "none" : "";
    const opts = zones.map((z) => `<option value="${z}">${this._zoneName(z)}</option>`).join("");
    if (sel.dataset.opts !== opts) { sel.innerHTML = opts; sel.dataset.opts = opts; }
    sel.value = this._zone || "";
    // With no title and no selector the header is not needed (e.g. on a zone page).
    const titleText = root.querySelector(".title").textContent.trim();
    root.querySelector(".head").style.display = !titleText && sel.style.display === "none" ? "none" : "";

    const body = root.querySelector(".body");
    const real = this._real();
    if (!this._zone || !real) {
      body.innerHTML = `<div class="empty">${zones.length ? t.noSchedule : t.noZones}</div>`;
      this._grid = null;
      return;
    }
    if (!this._grid || !body.contains(this._grid)) this._buildGrid(body);

    const week = this._week();
    const now = new Date();
    const today = (now.getDay() + 6) % 7;
    const nowIdx = today * SLOTS + now.getHours() * 2 + (now.getMinutes() >= 30 ? 1 : 0);
    for (let i = 0; i < 336; i++) {
      const c = this._cells[i];
      c.classList.toggle("on", week[i] !== "0");
      c.classList.toggle("now", i === nowIdx);
      c.classList.toggle("chg", !!this._draft && week[i] !== real[i]);
    }
    root.querySelectorAll(".day").forEach((el) => el.classList.toggle("today", +el.dataset.d === today));

    const dirty = !!this._draft && this._draft !== real;
    if (this._draft && !dirty && !this._saving) this._draft = null;
    root.querySelector(".text").textContent = scheduleText(week, this._hass.language);
    root.querySelector(".save").disabled = !dirty || !!this._saving;
    root.querySelector(".discard").disabled = !this._draft || !!this._saving;
    root.querySelector(".copy").disabled = !!this._saving;

    const st = root.querySelector(".status");
    let s = this._status;
    if (this._saving) s = { key: "sending", cls: "warn" };
    else if (!s && dirty) s = { key: "unsaved", cls: "warn" };
    else if (!s && a.pending) s = { key: "pendingChange", cls: "warn" };
    st.textContent = s ? (s.text || t[s.key]) : "";
    st.className = `status ${s ? s.cls : ""}`;
  }

  _copy() {
    const week = this._week();
    if (!week) return;
    const from = +this.shadowRoot.querySelector(".from").value;
    const to = this.shadowRoot.querySelector(".to").value;
    const sets = { lv: [0, 1, 2, 3, 4], sd: [5, 6], all: [0, 1, 2, 3, 4, 5, 6] };
    const targets = to.startsWith("d") ? [+to.slice(1)] : sets[to];
    this._draft = copyDay(week, from, targets.filter((d) => d !== from));
    this._status = null;
    this._update();
  }

  async _save() {
    const a = this._attrs();
    const week = this._draft;
    if (!week || !a.topic) return;
    this._saving = { week, at: Date.now() };
    this._status = null;
    this._update();
    try {
      await this._hass.callService("mqtt", "publish", { topic: a.topic, payload: week });
    } catch (e) {
      this._saving = null;
      this._status = { text: this._t.sendError.replace("{e}", e.message || e), cls: "err" };
      this._update();
    }
  }
}

if (typeof customElements !== "undefined" && !customElements.get("watts-schedule-card")) {
  customElements.define("watts-schedule-card", WattsScheduleCard);
  const t = i18n(typeof navigator !== "undefined" ? navigator.language : "en");
  window.customCards = window.customCards || [];
  window.customCards.push({ type: "watts-schedule-card", name: t.cardName, description: t.cardDesc });
  console.info(`%c WATTS-SCHEDULE-CARD %c ${WSC_VERSION} `, "background:#ff9800;color:#fff", "");
}
