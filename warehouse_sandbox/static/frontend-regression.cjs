"use strict";

// Source and function-level checks only. No browser, DOM renderer, or network access.
// Run: node warehouse_sandbox/static/frontend-regression.cjs
const fs = require("fs");
const path = require("path");
const vm = require("vm");
const assert = require("assert/strict");
const root = path.resolve(__dirname, "../..");
const source = fs.readFileSync(path.join(__dirname, "app.js"), "utf8");
const html = fs.readFileSync(path.join(__dirname, "index.html"), "utf8");
const dataset = JSON.parse(fs.readFileSync(path.join(root, "examples/sandbox/demo.json"), "utf8"));
const ids = Array.from(html.matchAll(/id="([^"]+)"/g), (match) => match[1]);
assert.equal(ids.length, new Set(ids).size, "HTML IDs must be unique");
for (const match of source.matchAll(/\$\("([^"]+)"\)/g)) assert(ids.includes(match[1]), `Missing DOM reference ${match[1]}`);

const settings = {forklift_count:3,speed_mps:2.25,handling_seconds:30.5,loading_seconds:25.25,lookahead_minutes:30.5,adoption_percent:33.33,min_benefit_seconds:5.5,min_optimizer_score:0.25,seed:42};
const controls = Object.keys(settings).map((name) => ({name,value:"0",disabled:false}));
const stepButton = {disabled:false};
const nodes = Object.fromEntries(ids.map((id) => [id,{value:"",disabled:false,hidden:false,textContent:"",classList:{toggle(){}},querySelectorAll(){return [];}}]));
nodes.lookahead = controls.find((control) => control.name === "lookahead_minutes");
nodes.adoption = controls.find((control) => control.name === "adoption_percent");
nodes["scenario-form"].querySelectorAll = (selector) => selector === "input[name]" ? controls : [...controls,stepButton,nodes["run-button"]];
const tabs = ["results","replay","recommendations","model"].map((name) => ({dataset:{tab:name},tabIndex:-1,attrs:{"aria-controls":`${name}-view`},setAttribute(key,value){this.attrs[key]=value;},getAttribute(key){return this.attrs[key];},focus(){this.focused=true;}}));
const previousResult = {identity:"previous comparison"};
let postedImport;
const context = {
  $: (id) => nodes[id], document:{querySelectorAll:() => tabs},
  state:{dataset,result:previousResult,busy:false,time:12,branch:"baseline",tab:"results"},
  Number,Math,Array,Set,Error,console,
  renderReplay(){},pause(){},notice(){},
  applyDataset(data){context.state.dataset=data;context.state.result=null;},
  async request(endpoint,body){if(endpoint === "/api/import"){postedImport=body;return {dataset,settings,summary:{pallets:dataset.inventory.length,orders:dataset.orders.length},warnings:[]};}throw Error("Simulated API failure");},
};
vm.createContext(context);
for (const prefix of ["const number =","function setBusy(","function updateSettingLabels(","function setSettings(","function previewForkliftCount(","function shipmentKey(","function originalPolicy(","function optimizerScore(","function optimizerComponentRows(","function showTab(","function settings("]) {
  const line = source.split("\n").find((line) => line.trim().startsWith(prefix));
  assert(line, `Helper ${prefix} must be present`); vm.runInContext(line,context);
}
function loadAsync(name) {const start=source.indexOf(`  async function ${name}`);assert(start>=0);vm.runInContext(source.slice(start,source.indexOf("\n  function ",start+2)),context);}
loadAsync("importFiles");loadAsync("runScenario");

const exportStart = source.indexOf("  function hostedExport(");
vm.runInContext(source.slice(exportStart, source.indexOf("  function download(", exportStart)), context);
const exportResult = {proposed:{suggestions:[{pallet_id:"=DANGEROUS",reason:'quoted "reason"',score_components:{t_saved:60}}]}};
assert.equal(JSON.parse(context.hostedExport(exportResult,"json")).proposed.suggestions[0].pallet_id,"=DANGEROUS");
const csvExport = context.hostedExport(exportResult,"csv");
assert(csvExport.includes("'=DANGEROUS"),"Hosted CSV neutralizes formula injection");
assert(csvExport.includes('quoted ""reason""'),"Hosted CSV escapes quotes");

async function main() {
  context.setBusy(true);
  assert([...controls,stepButton,nodes["run-button"]].every((control) => control.disabled),"Busy scenario must lock inputs and step controls");
  context.setBusy(false);
  assert(controls.every((control) => !control.disabled));
  for(const [value,expected] of [[999999999,20],[-7,3],[Infinity,3],["2.9",2],["",3]]) assert.equal(context.previewForkliftCount(value,3),expected);
  context.setSettings(settings);
  for(const control of controls) assert.equal(Number(control.value),settings[control.name],"Restored values must not be rounded");
  for(const name of ["speed_mps","handling_seconds","loading_seconds","lookahead_minutes","adoption_percent","min_benefit_seconds","min_optimizer_score"]){const input=Array.from(html.matchAll(/<input\b[^>]*>/g),(match) => match[0]).find((tag) => tag.includes(`name="${name}"`));assert(input.includes('step="any"'),`${name} must allow valid fractional settings`);}
  context.showTab("replay",true);
  assert.equal(context.state.tab,"replay");
  for(const tab of tabs){const active=tab.dataset.tab === "replay";assert.equal(nodes[tab.attrs["aria-controls"]].hidden,!active);assert.equal(tab.attrs["aria-selected"],String(active));assert.equal(tab.tabIndex,active ? 0 : -1);}
  assert(tabs[1].focused,"Keyboard-selected tab receives focus");
  const shipmentMap = new Map([{shipment_id:"appointment:SHARED",order_id:"SHARED"},{shipment_id:"order:SHARED",order_id:"SHARED"}].map((truck) => [context.shipmentKey(truck),truck]));
  assert.equal(shipmentMap.size,2,"Appointment and order display IDs must not collide");
  assert.equal(context.shipmentKey({order_id:"LEGACY"}),"LEGACY","Legacy results remain readable");
  const sourceResult={policy:{id:"original_optimizer_phase1"}}, sourceSuggestion={score:0.345,score_components:{t_saved:53,p_load:1,w_order:0.5,c_move:83,c_opportunity:30}};
  assert.equal(context.optimizerScore(sourceSuggestion,sourceResult),0.345,"The original V(m) score is shown unchanged");
  assert.equal(context.optimizerScore(sourceSuggestion,{}),null,"Earlier policy scores must not be presented as source V(m)");
  const components=context.optimizerComponentRows(sourceSuggestion);
  assert.equal(components.find((component) => component.key === "c_move").text,"83.0 sec","Source movement cost is separate from replay task duration");
  assert.equal(components.find((component) => component.key === "p_load").text,"100.0%");
  assert(context.optimizerComponentRows({}).every((component) => component.text === "Unavailable"),"Missing source values are not fabricated");
  await context.runScenario({preventDefault(){}});
  assert.equal(context.state.result,previousResult,"A failed API run preserves the previous comparison");
  assert.equal(context.state.time,12,"A failed API run preserves replay position");
  assert(controls.every((control) => !control.disabled));
  const wrapper=JSON.stringify({dataset,settings,run_id:"0123456789abcdef0123456789abcdef"});
  nodes["dataset-files"].files=[{name:"saved-run.json",size:Buffer.byteLength(wrapper),async text(){return wrapper;}}];
  await context.importFiles();
  assert.equal(postedImport.data,wrapper,"Import forwards the full run wrapper for server validation");
  for(const control of controls) assert.equal(Number(control.value),settings[control.name],"Imports restore validated scenario settings");
  console.log("PASS: frontend source references, busy controls, preview limits, fractional restore, accessible tab state, shipment namespaces, source scores/components, failed-run preservation, full-run imports.");
  console.log("These are source and helper checks; browser rendering is not tested.");
}
main().catch((error) => {console.error(error);process.exitCode=1;});
