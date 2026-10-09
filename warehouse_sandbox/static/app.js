"use strict";

(() => {
  const $ = (id) => document.getElementById(id);
  const state = {dataset: null, result: null, branch: "baseline", tab: "results", time: 0, playing: false, frame: null, lastFrame: null, lastPaint: 0, busy: false};
  const ns = "http://www.w3.org/2000/svg";
  const number = (n, digits = 0) => Number(n || 0).toLocaleString(undefined, {maximumFractionDigits: digits, minimumFractionDigits: digits});
  const minutes = (seconds) => number(Number(seconds || 0) / 60, 1);
  const time = (seconds) => { const s = Math.floor(Math.max(0, Number(seconds || 0))); return [Math.floor(s / 3600), Math.floor(s / 60) % 60, s % 60].map((x) => String(x).padStart(2, "0")).join(":"); };
  const safe = (value) => String(value ?? "");
  function element(tag, className, text) { const e = document.createElement(tag); if (className) e.className = className; if (text !== undefined) e.textContent = safe(text); return e; }
  function svgElement(tag, attributes, text) { const e = document.createElementNS(ns, tag); for (const [key, value] of Object.entries(attributes || {})) e.setAttribute(key, safe(value)); if (text !== undefined) e.textContent = safe(text); return e; }
  function cell(text, className) { return element("td", className, text); }
  function notice(message, error = false, details = []) { const area = $("notice"); area.replaceChildren(element("span", "", message)); if (details.length) {const ul = element("ul"); details.forEach((item) => ul.append(element("li", "", item))); area.append(ul);} area.className = error ? "notice error" : "notice"; area.hidden = !message; }
  async function request(path, body) { const response = await fetch(path, body === undefined ? {} : {method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify(body)}); let data; try {data = await response.json();} catch (_) {throw new Error("The local server returned an unreadable response.");} if (!response.ok) {const error = new Error(data.error || `Request failed (${response.status}).`); error.details = data.details || []; throw error;} return data; }
  function setBusy(busy, task = "run") { state.busy = busy; for (const control of $("scenario-form").querySelectorAll("input, button")) control.disabled = busy; $("run-button").disabled = busy || !state.dataset; $("demo-button").disabled = busy; $("dataset-files").disabled = busy; $("run-button").textContent = busy && task === "run" ? "Running comparison…" : "Run comparison"; $("demo-button").textContent = busy && task === "demo" ? "Loading demo…" : "Load demo"; $("scenario-form").classList.toggle("busy", busy); }
  function updateSettingLabels() { $("lookahead-output").textContent = number(Number($("lookahead").value), Number($("lookahead").value) % 1 ? 1 : 0) + " min"; $("adoption-output").textContent = number(Number($("adoption").value), Number($("adoption").value) % 1 ? 1 : 0) + "%"; }
  function setSettings(values) { if (values) for (const input of $("scenario-form").querySelectorAll("input[name]")) if (values[input.name] !== undefined) input.value = values[input.name]; updateSettingLabels(); renderReplay(); }
  function previewForkliftCount(value, fallback) { const count = Number(value); return Math.min(20, Math.max(1, Math.floor(Number.isFinite(count) && count > 0 ? count : fallback || 1))); }
  function shipmentKey(truck) { return truck.shipment_id || truck.order_id; }
  function originalPolicy(result) { return !result || result.policy?.id === "original_optimizer_phase1"; }
  function optimizerScore(suggestion, result) { return originalPolicy(result) && typeof suggestion.score === "number" && Number.isFinite(suggestion.score) ? suggestion.score : null; }
  function optimizerComponentRows(suggestion) {const values = suggestion.score_components || {}; return [["t_saved","Load travel saving (T_saved)","seconds"],["p_load","Load probability (P_load)","probability"],["w_order","Order urgency weight (W_order)","weight"],["c_move","Source movement cost (C_move)","seconds"],["c_opportunity","Fleet opportunity cost (C_opportunity)","seconds"]].map(([key,label,unit]) => {const value = values[key]; const text = typeof value !== "number" || !Number.isFinite(value) ? "Unavailable" : unit === "probability" ? `${number(value * 100,1)}%` : unit === "seconds" ? `${number(value,1)} sec` : number(value,3); return {key,label,text};});}
  function showTab(name, focus = false) { if (!["results", "replay", "recommendations", "model"].includes(name)) return; state.tab = name; for (const tab of document.querySelectorAll("[data-tab]")) {const active = tab.dataset.tab === name; tab.setAttribute("aria-selected", String(active)); tab.tabIndex = active ? 0 : -1; $(tab.getAttribute("aria-controls")).hidden = !active; if (active && focus) tab.focus();} }
  function resetResults() {
    pause(); state.result = null; state.time = 0; state.branch = "baseline";
    $("comparison-body").replaceChildren(emptyRow(4, "No comparison has been run. Review the scenario settings, then select Run comparison.")); $("historical-labor-note").hidden = true;
    $("suggestions-body").replaceChildren(emptyRow(7, "Run a comparison to generate staging recommendations.")); $("shipments-body").replaceChildren(emptyRow(8, "Run a comparison to see shipment outcomes.")); $("suggestion-count").textContent = "0";
    $("optimizer-policy").textContent = "Original optimizer · Phase 1"; $("optimizer-policy").className = "policy-indicator";
    $("optimizer-source").textContent = "The copied PrePositionScheduler generates candidates using MovementScorer and FeasibilityEngine. The replay harness models their operating consequences.";
    $("recommendations-summary").textContent = "Run a comparison to generate recommendations.";
    $("result-caption").textContent = "Data is loaded. Review the scenario settings, then run a comparison.";
    ["export-csv", "export-json", "play-button", "timeline-slider", "reset-replay"].forEach((id) => $(id).disabled = true);
    $("timeline-slider").value = "0"; $("timeline-slider").max = "1"; $("timeline-time").textContent = time(0); $("timeline-duration").textContent = "/ " + time(0);
    $("warnings-list").replaceChildren(); $("calibration-value").textContent = "Not yet measured"; $("calibration-text").textContent = "Import observed departure times to compare baseline estimates with historical results.";
    $("model-baseline").textContent = state.dataset?.historical_moves?.length ? "Historical replay" : "Current policy model";
    $("baseline-toggle").textContent = state.dataset?.historical_moves?.length ? "Historical replay" : "Current policy";
    $("replay-subtitle").textContent = "Inspect the initial warehouse layout";
    $("baseline-label").textContent = state.dataset?.historical_moves?.length ? "Historical replay" : "Current policy model";
    $("model-baseline-text").textContent = state.dataset?.historical_moves?.length ? "Both branches attempt recorded relocations at their observed times. The proposed policy adds accepted recommendations. Historical relocation labor is unmeasured." : "Loading begins after truck arrival. The proposed policy can stage pallets for released orders before their planned arrivals.";
    $("assumptions-list").replaceChildren(element("li", "", "Movement and loading are modeled. Results estimate a counterfactual and do not prove actual savings."), element("li", "", "The simulator produces suggestions and has no production writeback or equipment dispatch."));
    setBranch("baseline");
  }
  function emptyRow(cols, text) { const row = element("tr"); const td = cell(text, "empty-table compact-empty"); td.colSpan = cols; row.append(td); return row; }
  function applyDataset(dataset, source) {
    state.dataset = dataset; resetResults(); $("facility-name").textContent = dataset.facility.name;
    $("facility-context").textContent = dataset.facility.name;
    history.replaceState({}, "", "/");
    const dt = new Date(dataset.facility.start_time); $("facility-time").textContent = Number.isNaN(dt.getTime()) ? dataset.facility.start_time : `Replay starts ${dt.toLocaleString(undefined, {month: "short", day: "numeric", hour: "numeric", minute: "2-digit"})}`;
    $("dataset-source").textContent = source === "DEMO" ? "Synthetic demo" : source === "SAVED RUN" ? "Saved run" : "Imported";
    $("dataset-stats").replaceChildren(...[[dataset.inventory.length, "Pallets"], [dataset.orders.length, "Orders"], [dataset.docks.length, "Docks"]].map(([value, label]) => {const e = element("div"); e.append(element("dt", "", label), element("dd", "", number(value))); return e;}));
    renderDataSummary();
    $("forklift-count").value = Math.min(20, Math.max(1, dataset.forklifts.length));
    $("run-button").disabled = state.busy; renderReplay();
  }
  async function loadDemo() { setBusy(true, "demo"); try {const dataset = await request("/api/demo"); applyDataset(dataset, "DEMO"); notice("");} catch (error) {notice(error.message, true, error.details);} finally {setBusy(false);} }
  async function importFiles() {
    const files = Array.from($("dataset-files").files || []); if (!files.length) return;
    pause(); setBusy(true, "import"); notice("Importing and validating your warehouse data…");
    try {
      if (files.reduce((total, file) => total + file.size, 0) > 5 * 1024 * 1024) throw new Error("Choose files with a combined size under 5 MB.");
      const isJson = files.length === 1 && files[0].name.toLowerCase().endsWith(".json"); let body;
      if (isJson) body = {format: "json", data: await files[0].text()};
      else {if (files.some((file) => !file.name.toLowerCase().endsWith(".csv"))) throw new Error("Import one JSON file, or select the warehouse CSV tables together."); const names = files.map((file) => file.name.toLowerCase()); if (new Set(names).size !== names.length) throw new Error("Choose only one file for each CSV table."); const tables = {}; for (const file of files) tables[file.name.toLowerCase()] = await file.text(); body = {format: "csv", files: tables}; if ($("csv-facility-name").value.trim()) body.facility_name = $("csv-facility-name").value.trim(); if ($("csv-start-time").value.trim()) body.start_time = $("csv-start-time").value.trim();}
      const response = await request("/api/import", body); applyDataset(response.dataset, "IMPORTED"); setSettings(response.settings); notice(`Imported ${number(response.summary.pallets)} pallets and ${number(response.summary.orders)} orders.${response.settings ? " Saved scenario settings restored." : ""} Your dataset is ready to simulate.`, false, response.warnings || []);
    } catch (error) {notice(error.message, true, error.details);} finally {setBusy(false); $("dataset-files").value = "";}
  }
  function settings() { const result = {}; for (const input of $("scenario-form").querySelectorAll("input[name]")) result[input.name] = Number(input.value); return result; }
  async function runScenario(event) {
    event.preventDefault(); if (!state.dataset || state.busy) return; pause(); setBusy(true); notice("Running both branches with identical warehouse inputs…");
    try {state.result = await request("/api/run", {dataset: state.dataset, settings: settings()}); state.time = 0; state.branch = "proposed"; renderResults(); showTab("results"); history.replaceState({}, "", state.result.storage_mode === "browser" ? "/" : `/?run=${encodeURIComponent(state.result.run_id)}`); notice(`Comparison complete: ${number(state.result.baseline.metrics.truck_count)} baseline shipments and ${number(state.result.proposed.metrics.truck_count)} proposed shipments.`);}
    catch (error) {notice(error.message, true, error.details);} finally {setBusy(false);}
  }
  function renderResults() {
    const result = state.result, baseline = result.baseline.metrics, proposed = result.proposed.metrics, comparison = result.comparison;
    const measures = [{name:"Shipments completed",key:"truck_count",unit:"",lower:false},{name:"Average shipment dwell",key:"avg_dwell_seconds",unit:"min",seconds:true},{name:"95th percentile dwell",key:"p95_dwell_seconds",unit:"min",seconds:true},{name:"Shipments departing late",key:"late_trucks",unit:""},{name:"Total lateness",key:"total_lateness_seconds",unit:"min",seconds:true},{name:"Forklift travel",key:"travel_meters",unit:"m"},{name:"Forklift busy time",key:"forklift_busy_seconds",unit:"min",seconds:true},{name:"Units loaded",key:"completed_units",unit:"",lower:false}];
    $("comparison-body").replaceChildren(...measures.map((measure) => {const a = Number(baseline[measure.key] || 0), b = Number(proposed[measure.key] || 0), delta = b - a; const format = (value) => (measure.seconds ? minutes(value) : number(value)) + (measure.unit ? ` ${measure.unit}` : ""); const row = element("tr"); const favorable = measure.lower === false ? delta > 0 : delta < 0; const change = cell(Math.abs(delta) < .01 ? "—" : `${delta > 0 ? "+" : "−"}${format(Math.abs(delta))}`, "numeric " + (Math.abs(delta) < .01 ? "delta-neutral" : favorable ? "delta-positive" : "delta-negative")); if (measure.key === "avg_dwell_seconds" && Math.abs(delta) >= .01) change.append(element("span", "cell-sub", `${number(Math.abs(comparison.avg_dwell_saved_percent || 0), 1)}% ${delta < 0 ? "less" : "more"}`)); row.append(cell(measure.name), cell(format(a), "numeric"), cell(format(b), "numeric"), change); return row;}));
    $("historical-labor-note").hidden = !baseline.historical_move_labor_unmeasured;
    $("baseline-label").textContent = result.baseline_label;
    $("optimizer-policy").textContent = originalPolicy(result) ? "Original optimizer · Phase 1" : "Older saved policy · rerun required"; $("optimizer-policy").className = "policy-indicator" + (originalPolicy(result) ? "" : " legacy");
    $("result-caption").textContent = originalPolicy(result) ? `${result.baseline_label} and original optimizer recommendations use identical operating conditions. Results are estimates, not measured savings.` : "This saved comparison used an earlier recommendation policy. Select Run comparison to evaluate the original optimizer.";
    $("baseline-toggle").textContent = result.baseline_label === "Historical replay" ? "Historical replay" : "Current policy";
    $("replay-subtitle").textContent = "Movement paths are schematic; task time includes handling.";
    const end = Math.ceil(Math.max(baseline.makespan_seconds || 0, proposed.makespan_seconds || 0, 1)); $("timeline-slider").max = end; $("timeline-slider").value = 0; $("timeline-duration").textContent = "/ " + time(end);
    ["export-csv", "export-json", "play-button", "timeline-slider", "reset-replay"].forEach((id) => $(id).disabled = false);
    renderSuggestions(); renderShipments(); renderModel(); setBranch("proposed");
  }
  function renderDataSummary() {const data = state.dataset; const appointments = new Set(data.orders.map((order) => order.appointment_id ? `appointment:${order.appointment_id}` : `order:${order.order_id}`)); const rows = [["Schema version",data.schema_version],["Orders",number(data.orders.length)],["Shipments / appointments",number(appointments.size)],["Inventory units",number(data.inventory.reduce((sum,pallet) => sum + pallet.quantity,0))],["Locations",number(data.locations.length)],["Forklifts",number(data.forklifts.length)],["Orders with planned arrival",`${number(data.orders.filter((order) => order.scheduled_arrival_seconds !== undefined).length)} / ${number(data.orders.length)}`],["Orders with observed departure",`${number(data.orders.filter((order) => order.observed_departure_seconds !== undefined).length)} / ${number(data.orders.length)}`],["Recorded relocations",number(data.historical_moves?.length || 0)]]; $("data-summary-body").replaceChildren(...rows.map(([name,value]) => {const row = element("tr"); row.append(cell(name),cell(value)); return row;}));}
  function renderSuggestions() {
    const suggestions = state.result.proposed.suggestions || []; $("suggestion-count").textContent = number(suggestions.length);
    const accepted = suggestions.filter((item) => item.adopted).length; $("recommendations-summary").textContent = `${number(suggestions.length)} recommendations generated; ${number(accepted)} accepted and ${number(suggestions.length - accepted)} skipped in this scenario.`;
    if (!suggestions.length) {$("suggestions-body").replaceChildren(emptyRow(7, "No eligible staging moves met the optimizer score and travel-saving thresholds in this scenario.")); return;}
    const rows = suggestions.map((suggestion) => {
      const row = element("tr", "table-clickable"); row.tabIndex = 0; row.setAttribute("aria-label", `Jump to ${safe(suggestion.pallet_id)} recommendation at ${time(suggestion.at_seconds)}`);
      const pallet = cell(); pallet.append(element("span", "cell-main", suggestion.pallet_id), element("span", "cell-sub", `${suggestion.order_id} · ${suggestion.owner_id}`));
      const reason = element("details", "suggestion-reason"); reason.append(element("summary", "", "Why this move?")); if (suggestion.optimizer_reason) reason.append(element("p","reason-label","Original optimizer"),element("p","",suggestion.optimizer_reason),element("p","reason-label","Replay evaluation")); reason.append(element("p", "", suggestion.reason || "No reason was provided.")); pallet.append(reason);
      const move = cell("", "move-cell"); move.append(document.createTextNode(safe(suggestion.from_location_id)), element("span", "move-arrow", "→"), document.createTextNode(safe(suggestion.to_location_id)));
      const score = optimizerScore(suggestion,state.result), scoreCell = cell(score === null ? "Unavailable" : number(score,3), "numeric optimizer-score-cell");
      if (score !== null) {const breakdown = element("details", "optimizer-breakdown"); const list = element("dl", "optimizer-components"); for (const component of optimizerComponentRows(suggestion)) {const entry = element("div"); entry.append(element("dt","",component.label),element("dd","",component.text)); list.append(entry);} breakdown.append(element("summary","","Score components"),list,element("p","", "C_move covers pallet travel and handling; it excludes the forklift's empty trip. The staging task cost includes that empty travel. Phase 1 P_load is a known-demand lookup, not a learned forecast.")); scoreCell.append(breakdown);}
      const decision = cell(); const badge = element("span", "decision " + (suggestion.adopted ? "adopted" : "skipped"), suggestion.adopted ? "Accepted" : "Skipped"); badge.title = safe(suggestion.reason); decision.append(badge);
      row.append(cell(time(suggestion.at_seconds)), pallet, move, scoreCell, cell(`${number(suggestion.estimated_load_travel_saved_seconds, 1)} sec`, "numeric"), cell(`${number(suggestion.move_cost_seconds, 1)} sec`, "numeric"), decision);
      const jump = () => {pause(); state.time = Number(suggestion.at_seconds || 0); showTab("replay"); setBranch("proposed"); $("replay-heading").scrollIntoView({behavior: "smooth", block: "center"});}; row.addEventListener("click", (event) => {if (!event.target.closest("details")) jump();}); row.addEventListener("keydown", (event) => {if (event.target === row && (event.key === "Enter" || event.key === " ")) {event.preventDefault(); jump();}});
      return row;
    }); $("suggestions-body").replaceChildren(...rows);
  }
  function renderShipments() {
    const base = new Map((state.result.baseline.trucks || []).map((truck) => [shipmentKey(truck),truck])), proposed = new Map((state.result.proposed.trucks || []).map((truck) => [shipmentKey(truck),truck]));
    const ids = Array.from(new Set([...base.keys(),...proposed.keys()])); if (!ids.length) {$("shipments-body").replaceChildren(emptyRow(8, "No completed shipments in either branch. Inspect model warnings and replay events.")); return;}
    const max = Math.max(1,...Array.from(base.values(),(truck) => truck.dwell_seconds),...Array.from(proposed.values(),(truck) => truck.dwell_seconds));
    $("shipments-body").replaceChildren(...ids.map((id) => {
      const baseline = base.get(id), outcome = proposed.get(id), truck = outcome || baseline, row = element("tr"), order = cell();
      order.append(element("span","cell-main",truck.appointment_id || truck.order_id),element("span","cell-sub",`${truck.order_ids?.length > 1 ? `${number(truck.order_ids.length)} orders · ` : ""}${safe(truck.owner_id)}`));
      const barsCell = cell(), bars = element("div","bar-comparison"); for (const [record,className] of [[baseline,"bar-baseline"],[outcome,"bar-proposed"]]) {const bar = element("span",className); bar.style.width = record ? `${Math.max(1,record.dwell_seconds / max * 100)}%` : "0%"; if (!record) bar.hidden = true; bars.append(bar);} bars.setAttribute("aria-label",`Baseline ${baseline ? `${minutes(baseline.dwell_seconds)} minutes` : "not completed"}, proposed ${outcome ? `${minutes(outcome.dwell_seconds)} minutes` : "not completed"}`); barsCell.append(bars);
      const saved = baseline && outcome ? baseline.dwell_seconds - outcome.dwell_seconds : null; const delta = saved === null || Math.abs(saved) < .1 ? "—" : `${saved > 0 ? "−" : "+"}${minutes(Math.abs(saved))} min`;
      const statusCell = cell(); statusCell.append(element("span","shipment-status" + (outcome?.late_seconds > 0 || !outcome ? " late" : ""),outcome ? outcome.late_seconds > 0 ? `Late by ${minutes(outcome.late_seconds)} min` : "On time" : "Not completed"));
      row.append(order,cell(truck.dock_id),cell(time(truck.arrival_seconds)),barsCell,cell(baseline ? `${minutes(baseline.dwell_seconds)} min` : "Not completed","numeric"),cell(outcome ? `${minutes(outcome.dwell_seconds)} min` : "Not completed","numeric"),cell(delta,"numeric " + (saved !== null && saved > .1 ? "delta-positive" : saved !== null && saved < -.1 ? "delta-negative" : "")),statusCell); return row;
    }));
  }
  function renderModel() {
    const result = state.result; $("model-baseline").textContent = result.baseline_label;
    $("optimizer-source").textContent = originalPolicy(result) ? "The copied PrePositionScheduler.generate_candidates runs with the original MovementScorer and FeasibilityEngine. Scores use the replay's simulated clock. The replay harness separately models equipment travel, task duration, capacity and loading outcomes." : "This saved run predates direct use of the original optimizer. Run a new comparison to generate original Phase 1 recommendations and source score components.";
    $("model-baseline-text").textContent = result.baseline_label === "Historical replay" ? "Both branches attempt recorded relocations at their observed times. The proposed policy adds accepted recommendations. Interventions can make an observed move infeasible; recorded relocation labor is unmeasured." : "Loading begins after truck arrival. The proposed policy can stage pallets for released orders before their planned arrivals. Both branches use the same equipment and task times.";
    const calibration = result.calibration || {}; const count = Number(calibration.observed_trucks || 0); $("calibration-value").textContent = count ? `${minutes(calibration.mae_departure_seconds)} min mean absolute error` : "Not calibrated to observations";
    $("calibration-text").textContent = count ? `Baseline departure estimates compared with ${number(count)} observed shipments. Lower error improves confidence in the baseline; it does not validate the counterfactual.` : "No observed departure times were supplied. Treat this as an exploratory estimate until the model is calibrated against your operation.";
    $("warnings-list").replaceChildren(...(result.warnings || []).map((text) => {const e = element("div", "warning-item"); e.append(element("span", "", "△"), element("span", "", text)); return e;}));
    $("assumptions-list").replaceChildren(...(result.assumptions || []).map((text) => element("li", "", text)));
  }
  function setBranch(branch) { state.branch = branch; $("baseline-toggle").classList.toggle("active", branch === "baseline"); $("proposed-toggle").classList.toggle("active", branch === "proposed"); $("baseline-toggle").setAttribute("aria-pressed", String(branch === "baseline")); $("proposed-toggle").setAttribute("aria-pressed", String(branch === "proposed")); renderReplay(); }
  function interpolatePath(points, fraction) { const lengths = points.slice(1).map((p, i) => Math.abs(p.x - points[i].x) + Math.abs(p.y - points[i].y)); let remain = Math.max(0, Math.min(1, fraction)) * lengths.reduce((a, b) => a + b, 0); for (let i = 0; i < lengths.length; i++) {if (remain <= lengths[i] || i === lengths.length - 1) {const f = lengths[i] ? remain / lengths[i] : 1; return {x: points[i].x + (points[i + 1].x - points[i].x) * f, y: points[i].y + (points[i + 1].y - points[i].y) * f};} remain -= lengths[i];} return points[points.length - 1]; }
  function pathBetween(start, via, end) { const points = [start]; for (const destination of [via, end]) {const last = points[points.length - 1]; if (last.x !== destination.x) points.push({x: destination.x, y: last.y}); if (last.y !== destination.y) points.push({x: destination.x, y: destination.y});} if (points.length === 1) points.push(end); return points; }
  function eventText(event) {const pallet = safe(event.pallet_id), resource = safe(event.resource_id), order = safe(event.order_id); switch (event.type) {case "reposition_start": return `${resource} stages ${pallet}: ${event.from_location_id} → ${event.to_location_id}`; case "reposition_end": return `${pallet} staged at ${event.to_location_id}`; case "load_start": return `${resource} loads ${pallet} for ${order}`; case "load_end": return `${resource} completes loading ${pallet} (${number(event.quantity)} units)`; case "truck_departure": return `${order} departs the dock`; case "arrival": return `${order} arrives for loading`; case "release": return `${order} becomes visible to the warehouse`; case "history_move": return `Recorded relocation: ${pallet} → ${event.to_location_id}`; default: return safe(event.type).replaceAll("_", " ");} }
  function renderReplay() {
    const map = $("warehouse-map"); map.replaceChildren(); $("map-empty").hidden = Boolean(state.dataset); if (!state.dataset) return;
    const dataset = state.dataset, locations = new Map(dataset.locations.map((loc) => [loc.id, loc])); const points = dataset.locations; const minX = Math.min(...points.map((p) => p.x)), maxX = Math.max(...points.map((p) => p.x)), minY = Math.min(...points.map((p) => p.y)), maxY = Math.max(...points.map((p) => p.y));
    const spanX = Math.max(10, maxX - minX), spanY = Math.max(10, maxY - minY); const scale = Math.min(760 / spanX, 225 / spanY); const offsetX = (960 - spanX * scale) / 2 - minX * scale, offsetY = 182 - (minY + maxY) / 2 * scale; const xy = (p) => ({x: offsetX + p.x * scale, y: offsetY + p.y * scale});
    const palletMap = new Map(dataset.inventory.map((p) => [p.pallet_id, {...p}])); const forkliftMap = new Map(); const desiredCount = previewForkliftCount(state.result?.settings?.forklift_count ?? $("forklift-count").value, dataset.forklifts.length); for (let i = 0; i < desiredCount; i++) {const source = dataset.forklifts[i] || dataset.forklifts[i % dataset.forklifts.length]; let id = dataset.forklifts[i]?.id || `sim-forklift-${i + 1}`; while (forkliftMap.has(id)) id += "-extra"; const pos = locations.get(source?.start_location_id) || points[0]; forkliftMap.set(id, {id, position: {...pos}, path: null});}
    const branch = state.result?.[state.branch]; const events = (branch?.events || []).slice().sort((a, b) => a.at_seconds - b.at_seconds); let lastEvent = null, processed = 0; const active = new Map();
    for (const event of events) {
      if (event.at_seconds > state.time) break; lastEvent = event; processed++;
      if (event.type === "reposition_end" || event.type === "history_move") {const pallet = palletMap.get(event.pallet_id); if (pallet && event.to_location_id) pallet.location_id = event.to_location_id;}
      if (event.type === "load_end") {const pallet = palletMap.get(event.pallet_id); if (pallet) pallet.quantity = Math.max(0, pallet.quantity - Number(event.quantity || 0));}
      if (event.type === "reposition_start" || event.type === "load_start") {let fork = forkliftMap.get(event.resource_id); if (!fork) {fork = {id: event.resource_id, position: {...(locations.get(event.resource_start_location_id) || locations.get(event.from_location_id) || points[0])}, path: null}; forkliftMap.set(event.resource_id, fork);} const from = locations.get(event.from_location_id), to = locations.get(event.to_location_id), start = locations.get(event.resource_start_location_id) || fork.position; if (from && to) {const path = pathBetween(start, from, to); active.set(event.resource_id, {event, path, start, from, to});}}
      if (event.type === "reposition_end" || event.type === "load_end") {const fork = forkliftMap.get(event.resource_id), to = locations.get(event.to_location_id); if (fork && to) fork.position = {...to}; active.delete(event.resource_id);}
    }
    const paths = []; for (const [id, task] of active) {
      const fork = forkliftMap.get(id), event = task.event; const pickup = Number(event.pickup_at_seconds), arrival = Number(event.destination_arrival_seconds);
      if (Number.isFinite(pickup) && Number.isFinite(arrival)) {
        const handling = Number(state.result?.settings?.handling_seconds || 0), reachSource = pickup - handling;
        if (state.time < reachSource) fork.position = interpolatePath(pathBetween(task.start, task.start, task.from), (state.time - event.at_seconds) / Math.max(.001, reachSource - event.at_seconds));
        else if (state.time < pickup) fork.position = task.from;
        else if (state.time < arrival) fork.position = interpolatePath(pathBetween(task.from, task.from, task.to), (state.time - pickup) / Math.max(.001, arrival - pickup));
        else fork.position = task.to;
        if (state.time >= pickup) {fork.carrying = true; const pallet = palletMap.get(event.pallet_id); if (pallet) {if (event.type === "reposition_start") pallet.location_id = null; else pallet.quantity = Math.max(0, pallet.quantity - Number(event.quantity || 0));}}
      } else fork.position = interpolatePath(task.path, (state.time - event.at_seconds) / Math.max(1, Number(event.duration_seconds || 1)));
      fork.path = task.path; paths.push(task.path);
    }
    const counts = new Map(); for (const pallet of palletMap.values()) {if (pallet.quantity > 0) counts.set(pallet.location_id, (counts.get(pallet.location_id) || 0) + 1);}
    const floorX = xy({x: minX, y: minY}).x - 54, floorY = xy({x: minX, y: minY}).y - 38; const floorW = spanX * scale + 108, floorH = spanY * scale + 76;
    map.append(svgElement("rect", {x: floorX, y: floorY, width: floorW, height: floorH, rx: 13, class: "map-floor"}));
    for (let y = Math.ceil(floorY / 24) * 24; y < floorY + floorH; y += 24) map.append(svgElement("line", {x1: floorX + 2, y1: y, x2: floorX + floorW - 2, y2: y, stroke: "#dfe6ec", "stroke-dasharray": "1 8"}));
    for (const path of paths) map.append(svgElement("polyline", {points: path.map((p) => {const pos = xy(p); return `${pos.x},${pos.y}`;}).join(" "), class: "map-path"}));
    for (const loc of points) {
      const pos = xy(loc); const count = counts.get(loc.id) || 0, group = svgElement("g"); group.append(svgElement("title", {}, `${loc.id} · ${loc.kind} · ${count}/${loc.capacity_pallets} pallets · ${loc.temperature || "ambient"}`));
      const width = loc.kind === "dock" ? 60 : 52, height = loc.kind === "dock" ? 38 : 44;
      group.append(svgElement("rect", {x: pos.x - width / 2, y: pos.y - height / 2, width, height, rx: 4, class: `map-location ${loc.kind}`}));
      if (loc.kind === "dock") {group.append(svgElement("path", {d: `M${pos.x - 20} ${pos.y - 10}h40v20h-40zM${pos.x - 17} ${pos.y + 9}v5M${pos.x + 17} ${pos.y + 9}v5`, stroke: "#7c9aad", fill: "none"})); group.append(svgElement("text", {x: pos.x, y: pos.y + 3, "text-anchor": "middle", class: "map-label"}, "Dock"));}
      else {group.append(svgElement("text", {x: pos.x, y: pos.y + 2, "text-anchor": "middle", class: "map-count"}, count)); group.append(svgElement("text", {x: pos.x, y: pos.y + 14, "text-anchor": "middle", class: "map-label", "font-size": 8}, `/ ${loc.capacity_pallets}`));}
      group.append(svgElement("text", {x: pos.x, y: pos.y + height / 2 + 16, "text-anchor": "middle", class: "map-label"}, loc.id)); map.append(group);
    }
    const collision = new Map(); for (const fork of forkliftMap.values()) {const pos = xy(fork.position), key = `${Math.round(pos.x)},${Math.round(pos.y)}`, offset = collision.get(key) || 0; collision.set(key, offset + 1); const dx = (offset % 3) * 16 - 8, dy = -27 - Math.floor(offset / 3) * 16; const group = svgElement("g", {transform: `translate(${pos.x + dx},${pos.y + dy})`}); group.append(svgElement("title", {}, `${fork.id} · ${active.has(fork.id) ? "working" : "idle"}`)); group.append(svgElement("rect", {x: -7, y: -6, width: 14, height: 12, rx: 2, class: "map-forklift"})); group.append(svgElement("path", {d: "M-4 6v4h3M4 6v4H1", stroke: "#2a5275", "stroke-width": 2})); if (fork.carrying) group.append(svgElement("rect", {x: -4, y: 7, width: 8, height: 6, rx: 1, fill: "#bb9665", stroke: "#f6f8fa"})); if (desiredCount < 7 || active.has(fork.id)) group.append(svgElement("text", {x: 0, y: -11, "text-anchor": "middle", class: "map-forklift-label"}, fork.id)); map.append(group);}
    $("timeline-slider").value = state.time; $("timeline-time").textContent = time(state.time);
    $("map-status").textContent = state.result ? `${state.branch === "baseline" ? state.result.baseline_label : "With recommendations"} · ${number(active.size)} active forklifts` : "Initial state";
    $("current-event").textContent = lastEvent ? eventText(lastEvent) : state.result ? "Warehouse initialized. Advance the timeline to inspect the next event." : "Run a scenario to replay movements, loading, and staging decisions.";
    $("event-counter").textContent = state.result ? `${number(processed)} / ${number(events.length)} events` : "";
  }
  function pause() {state.playing = false; state.lastFrame = null; if (state.frame) cancelAnimationFrame(state.frame); state.frame = null; $("play-button").textContent = "Play"; $("play-button").setAttribute("aria-label", "Play replay");}
  function play() {if (!state.result) return; if (state.playing) {pause(); return;} if (state.time >= Number($("timeline-slider").max)) state.time = 0; state.playing = true; state.lastFrame = null; $("play-button").textContent = "Pause"; $("play-button").setAttribute("aria-label", "Pause replay"); const tick = (timestamp) => {if (!state.playing) return; if (state.lastFrame !== null) state.time = Math.min(Number($("timeline-slider").max), state.time + (timestamp - state.lastFrame) / 1000 * Number($("playback-speed").value)); state.lastFrame = timestamp; if (timestamp - state.lastPaint > 70 || state.time >= Number($("timeline-slider").max)) {renderReplay(); state.lastPaint = timestamp;} if (state.time >= Number($("timeline-slider").max)) pause(); else state.frame = requestAnimationFrame(tick);}; state.frame = requestAnimationFrame(tick); }
  function hostedExport(result, format) {
    if (format === "json") return JSON.stringify(result, null, 2);
    const fields = ["id", "at_seconds", "pallet_id", "sku_id", "owner_id", "order_id", "from_location_id", "to_location_id", "dock_id", "estimated_load_travel_saved_seconds", "move_cost_seconds", "score", "score_components", "policy_source", "adopted", "reason"];
    const cell = (value) => {let text = typeof value === "object" && value !== null ? JSON.stringify(value) : String(value ?? ""); if (/^[=+@\-\t\r]/.test(text)) text = "'" + text; return '"' + text.replace(/"/g, '""') + '"';};
    return [fields.join(","), ...result.proposed.suggestions.map((row) => fields.map((field) => cell(row[field])).join(","))].join("\r\n");
  }
  function download(format) {
    if (!state.result?.run_id) return;
    const a = element("a"), hosted = state.result.storage_mode === "browser";
    const objectUrl = hosted ? URL.createObjectURL(new Blob([hostedExport(state.result, format)], {type: format === "json" ? "application/json" : "text/csv;charset=utf-8"})) : null;
    a.href = objectUrl || `/api/runs/${encodeURIComponent(state.result.run_id)}/export?format=${format}`;
    a.download = `warehouse-replay-${state.result.run_id}.${format}`;
    document.body.append(a); a.click(); a.remove();
    if (objectUrl) setTimeout(() => URL.revokeObjectURL(objectUrl), 1000);
  }

  $("demo-button").addEventListener("click", loadDemo); $("dataset-files").addEventListener("change", importFiles); $("scenario-form").addEventListener("submit", runScenario); $("baseline-toggle").addEventListener("click", () => setBranch("baseline")); $("proposed-toggle").addEventListener("click", () => setBranch("proposed")); $("play-button").addEventListener("click", play); $("reset-replay").addEventListener("click", () => {pause(); state.time = 0; renderReplay();}); $("timeline-slider").addEventListener("input", () => {pause(); state.time = Number($("timeline-slider").value); renderReplay();}); $("export-csv").addEventListener("click", () => download("csv")); $("export-json").addEventListener("click", () => download("json"));
  $("change-data-button").addEventListener("click", () => showTab("model",true));
  const tabs = Array.from(document.querySelectorAll("[data-tab]")); for (const tab of tabs) {tab.addEventListener("click",() => showTab(tab.dataset.tab)); tab.addEventListener("keydown",(event) => {const index = tabs.indexOf(tab); let next; if (event.key === "ArrowRight") next = (index + 1) % tabs.length; else if (event.key === "ArrowLeft") next = (index + tabs.length - 1) % tabs.length; else if (event.key === "Home") next = 0; else if (event.key === "End") next = tabs.length - 1; if (next !== undefined) {event.preventDefault(); showTab(tabs[next].dataset.tab,true);}});}
  $("template-button").addEventListener("click", () => {const a = element("a"); a.href = "/api/templates"; a.download = "warehouse-replay-csv-template.zip"; document.body.append(a); a.click(); a.remove();});
  for (const button of document.querySelectorAll("[data-step]")) button.addEventListener("click", () => {const input = $("forklift-count"); input.value = Math.max(1, Math.min(20, Number(input.value) + Number(button.dataset.step))); input.dispatchEvent(new Event("input", {bubbles: true}));});
  $("scenario-form").addEventListener("input", () => {updateSettingLabels(); if (state.result) $("result-caption").textContent = "Scenario settings changed. Select Run comparison to refresh the results."; else renderReplay();});
  document.addEventListener("visibilitychange", () => {if (document.hidden) pause();});
  async function initialize() {
    const runId = new URLSearchParams(location.search).get("run");
    if (!runId) {await loadDemo(); return;}
    setBusy(true, "demo");
    try {
      const saved = await request(`/api/runs/${encodeURIComponent(runId)}`);
      if (!saved.dataset) throw new Error("This saved run does not include a replay dataset. Load a dataset to create a new run.");
      applyDataset(saved.dataset, "SAVED RUN"); state.result = saved; state.time = 0;
      setSettings(saved.settings);
      renderResults(); history.replaceState({}, "", `/?run=${encodeURIComponent(saved.run_id)}`); const savedWarnings = (saved.warnings || []).filter((warning) => warning.includes("predates the replay fixes") || warning.includes("original optimizer")); if (!originalPolicy(saved)) savedWarnings.push("This saved comparison used the earlier policy. Run a new comparison to use the original optimizer."); notice("Saved comparison reopened.",false,Array.from(new Set(savedWarnings)));
    } catch (error) {await loadDemo(); notice(`Could not reopen the saved run: ${error.message} Demo data is ready to try.`, true, error.details);}
    finally {setBusy(false);}
  }
  initialize();
})();
