let currentPayload=null;
let currentPreview=null;
let currentEligibilityPayload=null;
let currentEligibilityPreview=null;
let currentProviderPayload=null;
let currentProviderPreview=null;
let currentCatalogAssociationPayload=null;
let currentCatalogAssociationPreview=null;
const drawer=document.getElementById('assignment-drawer');
const skyReview=document.getElementById('sky-review');
let reviewDrawerVisible=false;
try{
  reviewDrawerVisible=sessionStorage.getItem('sdb-review-tools-visible')==='true';
}catch(error){
  reviewDrawerVisible=false;
}
function pointDisplayId(point){return point.source_display_name||point.source_id;}
function escapeHtml(value){
  return String(value).replaceAll('&','&amp;').replaceAll('<','&lt;')
    .replaceAll('>','&gt;').replaceAll('"','&quot;');
}
function selectedSourceHtml(point,suffix=''){
  const label=escapeHtml(pointDisplayId(point));
  const provenance=(point.provenance||[]).find(
    value=>String(value.access_url||'').startsWith('https://')
  );
  const source=provenance
    ? `<a href="${escapeHtml(provenance.access_url)}" target="_blank" rel="noopener">${label}</a>`
    : label;
  return `${escapeHtml(point.provider)} · ${source} · ${Number(point.separation_arcsec).toFixed(2)} arcsec${escapeHtml(suffix)}`;
}
function pointRunTarget(point){
  if(!point.run_target_sdbid) return '';
  return window.SDB_TARGET_NAMES[point.run_target_sdbid]||point.run_target_sdbid;
}
function postDrawerState(){
  skyReview.contentWindow?.postMessage(
    {type:'sdb-review-drawer-state',visible:reviewDrawerVisible},
    window.location.origin,
  );
}
function syncDrawerVisibility(){
  drawer.hidden=!reviewDrawerVisible;
  document.body.classList.toggle('drawer-open',reviewDrawerVisible);
}
function setDrawerVisibility(visible){
  reviewDrawerVisible=Boolean(visible);
  try{
    sessionStorage.setItem(
      'sdb-review-tools-visible',String(reviewDrawerVisible),
    );
  }catch(error){
    // The toggle still works when browser storage is unavailable.
  }
  syncDrawerVisibility();
  postDrawerState();
}
function clearDrawerSelection(){
  currentPayload=null;
  currentPreview=null;
  currentEligibilityPayload=null;
  currentEligibilityPreview=null;
  currentProviderPayload=null;
  currentProviderPreview=null;
  currentCatalogAssociationPayload=null;
  currentCatalogAssociationPreview=null;
  document.getElementById('apply').disabled=true;
  document.getElementById('apply-eligibility').disabled=true;
  document.getElementById('apply-provider-result').disabled=true;
  document.getElementById('apply-catalog-association').disabled=true;
  document.querySelectorAll('.detection').forEach(section=>section.classList.remove('active'));
  document.getElementById('detection-editors').hidden=true;
  document.querySelector('.preview-grid').hidden=true;
  document.getElementById('provider-result-editor').hidden=true;
  document.getElementById('provider-result-preview-panel').hidden=true;
  document.getElementById('catalog-association-editor').hidden=true;
  document.getElementById('catalog-association-preview-panel').hidden=true;
  document.getElementById('drawer-title').textContent='Review tools';
  document.getElementById('selected-source').textContent='';
  document.getElementById('assignment-prompt').textContent='Select a plotted catalog source to review it.';
}
function closeDrawer(){
  clearDrawerSelection();
  setDrawerVisibility(false);
}
function showDetection(point,detectionId){
  const section=document.querySelector(`.detection[data-detection="${detectionId}"]`);
  if(!section){clearDrawerSelection();return;}
  document.querySelectorAll('.detection').forEach(value=>value.classList.toggle('active',value===section));
  document.getElementById('detection-editors').hidden=false;
  document.querySelector('.preview-grid').hidden=false;
  document.getElementById('provider-result-editor').hidden=true;
  document.getElementById('provider-result-preview-panel').hidden=true;
  document.getElementById('catalog-association-editor').hidden=true;
  document.getElementById('catalog-association-preview-panel').hidden=true;
  document.getElementById('drawer-title').textContent='Photometry assignment';
  resetEligibilityControls(section);
  const combinedSystem=section.querySelector('.composite-scope');
  if(combinedSystem) updateCombinedSystemControl(combinedSystem);
  document.getElementById('selected-source').innerHTML=selectedSourceHtml(point);
  document.getElementById('assignment-prompt').textContent='Review the selected catalog detection. All bands are selected by default.';
  document.getElementById('preview').textContent='Choose assignments, then preview.';
  document.getElementById('eligibility-preview').textContent='Choose a band action, then preview.';
  document.getElementById('apply').disabled=true;
  document.getElementById('apply-eligibility').disabled=true;
  currentPayload=null;
  currentPreview=null;
  currentEligibilityPayload=null;
  currentEligibilityPreview=null;
  currentProviderPayload=null;
  currentProviderPreview=null;
  currentCatalogAssociationPayload=null;
  currentCatalogAssociationPreview=null;
}
function showProviderReview(point){
  document.querySelectorAll('.detection').forEach(section=>section.classList.remove('active'));
  document.getElementById('detection-editors').hidden=true;
  document.querySelector('.preview-grid').hidden=true;
  const editor=document.getElementById('provider-result-editor');
  editor.hidden=false;
  editor.classList.add('active');
  document.getElementById('provider-result-preview-panel').hidden=false;
  document.getElementById('catalog-association-editor').hidden=true;
  document.getElementById('catalog-association-preview-panel').hidden=true;
  document.getElementById('drawer-title').textContent='Provider result review';
  const runTarget=pointRunTarget(point);
  document.getElementById('selected-source').innerHTML=selectedSourceHtml(
    point,runTarget?` · catalog query for ${runTarget}`:''
  );
  document.getElementById('assignment-prompt').textContent='Review this catalog provider result.';
  document.getElementById('provider-result-context').textContent=`${point.provider} · ${point.status}${runTarget?` · result belongs to the catalog run for ${runTarget}`:''}${point.note?` · ${point.note}`:''}`;
  for(const button of editor.querySelectorAll('.preview-provider-result')){
    const action=button.dataset.action;
    button.hidden=!((point.status==='ambiguous' && ['accept_candidate','reviewed_no_match'].includes(action))||(['transient_failure','permanent_failure'].includes(point.status)&&action==='retry'));
    button.dataset.runId=point.run_id;
    button.dataset.rawRowId=point.raw_row_id??'';
  }
  document.getElementById('provider-result-preview').textContent='Choose an action, then preview.';
  document.getElementById('apply-provider-result').disabled=true;
  currentProviderPayload=null;
  currentProviderPreview=null;
}
function showCatalogAssociation(point,detectionId){
  document.querySelectorAll('.detection').forEach(section=>section.classList.remove('active'));
  const hasPhotometry=detectionId!=null && point.status==='accepted';
  document.getElementById('detection-editors').hidden=!hasPhotometry;
  document.querySelector('.preview-grid').hidden=!hasPhotometry;
  if(hasPhotometry){
    const section=document.querySelector(`.detection[data-detection="${detectionId}"]`);
    if(section){
      section.classList.add('active');
      resetEligibilityControls(section);
    }
  }
  document.getElementById('provider-result-editor').hidden=true;
  document.getElementById('provider-result-preview-panel').hidden=true;
  const editor=document.getElementById('catalog-association-editor');
  editor.hidden=false;
  editor.classList.add('active');
  document.getElementById('catalog-association-preview-panel').hidden=false;
  document.getElementById('drawer-title').textContent=hasPhotometry?'Source association and photometry':'Catalog source association';
  const runTarget=pointRunTarget(point);
  document.getElementById('selected-source').innerHTML=selectedSourceHtml(point);
  document.getElementById('assignment-prompt').textContent=hasPhotometry
    ? 'This source is accepted for the current target. Its photometry can be assigned below.'
    : 'Decide whether this discovered source belongs to the current target.';
  document.getElementById('catalog-association-context').textContent=`${point.provider} · ${point.status}${runTarget?` · discovered by the catalog query for ${runTarget}`:''}${point.note?` · ${point.note}`:''}`;
  for(const button of editor.querySelectorAll('.preview-catalog-association')){
    button.dataset.detectionId=point.detection_id;
    button.dataset.rawRowId=point.raw_row_id;
  }
  document.getElementById('catalog-association-preview').textContent='Choose an action, then preview.';
  document.getElementById('apply-catalog-association').disabled=true;
  currentCatalogAssociationPayload=null;
  currentCatalogAssociationPreview=null;
}
window.addEventListener('message',event=>{
  if(event.origin!==window.location.origin) return;
  if(event.source!==skyReview.contentWindow) return;
  if(event.data?.type==='sdb-review-drawer-ready'){
    postDrawerState();
    return;
  }
  if(event.data?.type==='sdb-review-drawer-toggle'){
    setDrawerVisibility(event.data.visible);
    return;
  }
  if(event.data?.type==='sdb-review-relatives'){
    openRelativesDialog();
    return;
  }
  if(event.data?.type!=='sdb-review-selection') return;
  const point=event.data.point;
  if(!point){clearDrawerSelection();return;}
  const detectionId=point.raw_row_id==null?null:window.SDB_RAW_ROW_DETECTIONS[String(point.raw_row_id)];
  if(point.kind==='catalog_association'){
    showCatalogAssociation(point,detectionId);
    return;
  }
  if(point.kind==='catalog'&&(point.status==='ambiguous'||['transient_failure','permanent_failure'].includes(point.status))){
    showProviderReview(point);
    return;
  }
  if(detectionId==null){clearDrawerSelection();return;}
  showDetection(point,detectionId);
});
document.getElementById('close-drawer').addEventListener('click',closeDrawer);
clearDrawerSelection();
syncDrawerVisibility();
function payloadFor(section){
  const combinedSystem=section.querySelector('.composite-scope');
  const scopeTarget=section.querySelector('.scope-target');
  return {
    detection_id:Number(section.dataset.detection),
    scope_target:scopeTarget?.value||window.SDB_TARGET,
    contributors:[...section.querySelectorAll('.contributor:checked')].map(x=>x.value),
    include_composite_scope:Boolean(combinedSystem?.checked),
    measurement_ids:[...section.querySelectorAll('.measurement:checked')].map(x=>Number(x.value)),
    target_role:'',
    target_state:'',
  };
}
function eligibilityPayloadFor(section){
  const changes=[...section.querySelectorAll('.eligibility-toggle')]
    .filter(button=>button.dataset.desiredExcluded!==button.dataset.currentExcluded)
    .map(button=>({
      measurement_id:Number(button.dataset.measurement),
      excluded:button.dataset.desiredExcluded==='true',
    }));
  return {changes};
}
function updateCombinedSystemControl(checkbox){
  const field=checkbox.closest('.combined-system-control')?.querySelector('.scope-target-field');
  if(field) field.hidden=!checkbox.checked;
}
function updateEligibilityControl(button){
  const current=button.dataset.currentExcluded==='true';
  const desired=button.dataset.desiredExcluded==='true';
  const changed=current!==desired;
  const state=button.closest('.band-row').querySelector('.eligibility-state');
  state.classList.toggle('pending',changed);
  state.textContent=changed
    ? (desired?'Will be excluded from fit':'Will be included in fit')
    : state.dataset.currentLabel;
  button.textContent=changed
    ? (current?'Keep excluded':'Keep included')
    : (current?'Include in fit':'Exclude from fit');
  button.setAttribute('aria-pressed',String(changed));
}
function resetEligibilityControls(section){
  section.querySelectorAll('.eligibility-toggle').forEach(button=>{
    button.dataset.desiredExcluded=button.dataset.currentExcluded;
    updateEligibilityControl(button);
  });
}
async function request(url,payload){
  const response=await fetch(url,{method:'POST',headers:{'content-type':'application/json'},body:JSON.stringify(payload)});
  const value=await response.json();
  if(!response.ok) throw new Error(value.detail||response.statusText);
  return value;
}
function renderHumanSummary(element,value){
  const summary=value?.human_summary;
  if(!summary){element.textContent=typeof value==='string'?value:JSON.stringify(value,null,2);return;}
  element.classList.remove('muted');
  element.replaceChildren();
  const heading=document.createElement('h3');
  heading.textContent=summary.title;
  element.appendChild(heading);
  for(const [name,rows,className] of [
    ['Context',summary.facts||[],''],
    ['Changes',summary.changes||[],''],
    ['Warnings',summary.warnings||[],'summary-warning'],
  ]){
    if(!rows.length)continue;
    const label=document.createElement('strong');
    label.textContent=name;
    if(className)label.className=className;
    element.appendChild(label);
    const list=document.createElement('ul');
    if(className)list.className=className;
    for(const row of rows){const item=document.createElement('li');item.textContent=row;list.appendChild(item);}
    element.appendChild(list);
  }
  const details=document.createElement('details');
  const detailsLabel=document.createElement('summary');
  detailsLabel.textContent='Technical details';
  const raw=document.createElement('pre');
  raw.textContent=JSON.stringify(value,null,2);
  details.append(detailsLabel,raw);
  element.appendChild(details);
}
function renderRequestError(element,error){element.classList.add('muted');element.textContent=error.message;}
function prefillReason(inputId,preview){
  const input=document.getElementById(inputId);
  if(!input||!preview||!preview.suggested_reason)return;
  if(!input.value||input.value===input.dataset.suggestedReason){
    input.value=preview.suggested_reason;
    input.dataset.suggestedReason=preview.suggested_reason;
  }
}
document.querySelectorAll('.preview').forEach(button=>button.addEventListener('click',async()=>{
  const section=button.closest('.detection');
  currentPayload=payloadFor(section);
  try{
    currentPreview=await request('/api/decision/preview',currentPayload);
    renderHumanSummary(document.getElementById('preview'),currentPreview);
    prefillReason('reason',currentPreview);
    document.getElementById('apply').disabled=!currentPreview.has_changes;
  }catch(error){renderRequestError(document.getElementById('preview'),error);}
}));
document.querySelectorAll('.composite-scope').forEach(checkbox=>checkbox.addEventListener('change',()=>{
  updateCombinedSystemControl(checkbox);
}));
document.querySelectorAll('.contributor,.measurement,.composite-scope,.scope-target').forEach(control=>control.addEventListener('change',()=>{
  currentPayload=null;
  currentPreview=null;
  document.getElementById('preview').textContent='Assignment changed; preview again.';
  document.getElementById('apply').disabled=true;
}));
document.querySelectorAll('.eligibility-toggle').forEach(button=>button.addEventListener('click',()=>{
  const current=button.dataset.currentExcluded==='true';
  const desired=button.dataset.desiredExcluded==='true';
  button.dataset.desiredExcluded=String(desired===current?!current:current);
  updateEligibilityControl(button);
  currentEligibilityPayload=null;
  currentEligibilityPreview=null;
  document.getElementById('eligibility-preview').textContent='Include/exclude changed; preview again.';
  document.getElementById('apply-eligibility').disabled=true;
}));
document.querySelectorAll('.preview-eligibility').forEach(button=>button.addEventListener('click',async()=>{
  const section=button.closest('.detection');
  currentEligibilityPayload=eligibilityPayloadFor(section);
  if(!currentEligibilityPayload.changes.length){
    currentEligibilityPreview=null;
    document.getElementById('eligibility-preview').textContent='Use Include or Exclude for at least one band.';
    document.getElementById('apply-eligibility').disabled=true;
    return;
  }
  try{
    currentEligibilityPreview=await request('/api/eligibility/preview',currentEligibilityPayload);
    renderHumanSummary(document.getElementById('eligibility-preview'),currentEligibilityPreview);
    prefillReason('reason',currentEligibilityPreview);
    document.getElementById('apply-eligibility').disabled=!currentEligibilityPreview.has_changes;
  }catch(error){renderRequestError(document.getElementById('eligibility-preview'),error);}
}));
document.querySelectorAll('.preview-provider-result').forEach(button=>button.addEventListener('click',async()=>{
  currentProviderPayload={
    action:button.dataset.action,
    run_id:Number(button.dataset.runId),
    raw_row_id:button.dataset.rawRowId===''?null:Number(button.dataset.rawRowId),
  };
  try{
    currentProviderPreview=await request('/api/provider-result/preview',currentProviderPayload);
    renderHumanSummary(document.getElementById('provider-result-preview'),currentProviderPreview);
    prefillReason('reason',currentProviderPreview);
    document.getElementById('apply-provider-result').disabled=!currentProviderPreview.has_changes;
  }catch(error){
    currentProviderPreview=null;
    renderRequestError(document.getElementById('provider-result-preview'),error);
    document.getElementById('apply-provider-result').disabled=true;
  }
}));
document.querySelectorAll('.preview-catalog-association').forEach(button=>button.addEventListener('click',async()=>{
  currentCatalogAssociationPayload={
    target:window.SDB_TARGET,
    action:button.dataset.action,
    detection_id:Number(button.dataset.detectionId),
    raw_row_id:Number(button.dataset.rawRowId),
  };
  try{
    currentCatalogAssociationPreview=await request('/api/catalog-association/preview',currentCatalogAssociationPayload);
    renderHumanSummary(document.getElementById('catalog-association-preview'),currentCatalogAssociationPreview);
    prefillReason('reason',currentCatalogAssociationPreview);
    document.getElementById('apply-catalog-association').disabled=!currentCatalogAssociationPreview.has_changes;
  }catch(error){
    currentCatalogAssociationPreview=null;
    renderRequestError(document.getElementById('catalog-association-preview'),error);
    document.getElementById('apply-catalog-association').disabled=true;
  }
}));
document.getElementById('apply').addEventListener('click',async()=>{
  if(!currentPayload||!currentPreview) return;
  const actor=document.getElementById('actor').value;
  const reason=document.getElementById('reason').value;
  if(!actor||!reason){alert('Actor and reason are required.');return;}
  const payload={...currentPayload,actor,reason,state_token:currentPreview.state_token};
  if(!confirm('Apply the displayed lifecycle and assignment changes?')) return;
  try{
    const value=await request('/api/decision/apply',payload);
    renderHumanSummary(document.getElementById('preview'),value);
    document.getElementById('apply').disabled=true;
    setTimeout(()=>location.reload(),700);
  }catch(error){renderRequestError(document.getElementById('preview'),error);}
});
document.getElementById('apply-eligibility').addEventListener('click',async()=>{
  if(!currentEligibilityPayload||!currentEligibilityPreview)return;
  const actor=document.getElementById('actor').value;
  const reason=document.getElementById('reason').value;
  if(!actor||!reason){alert('Actor and reason are required.');return;}
  if(!confirm('Apply the displayed fit include/exclude changes?'))return;
  const payload={...currentEligibilityPayload,actor,reason,state_token:currentEligibilityPreview.state_token};
  try{
    const value=await request('/api/eligibility/apply',payload);
    renderHumanSummary(document.getElementById('eligibility-preview'),value);
    document.getElementById('apply-eligibility').disabled=true;
    setTimeout(()=>location.reload(),700);
  }catch(error){renderRequestError(document.getElementById('eligibility-preview'),error);}
});
document.getElementById('apply-provider-result').addEventListener('click',async()=>{
  if(!currentProviderPayload||!currentProviderPreview)return;
  const actor=document.getElementById('actor').value;
  const reason=document.getElementById('reason').value;
  if(!actor||!reason){alert('Actor and reason are required.');return;}
  if(!confirm('Apply the displayed provider result action?'))return;
  const payload={...currentProviderPayload,actor,reason,state_token:currentProviderPreview.state_token};
  try{
    const value=await request('/api/provider-result/apply',payload);
    renderHumanSummary(document.getElementById('provider-result-preview'),value);
    document.getElementById('apply-provider-result').disabled=true;
    setTimeout(()=>location.reload(),700);
  }catch(error){renderRequestError(document.getElementById('provider-result-preview'),error);}
});
document.getElementById('apply-catalog-association').addEventListener('click',async()=>{
  if(!currentCatalogAssociationPayload||!currentCatalogAssociationPreview)return;
  const actor=document.getElementById('actor').value;
  const reason=document.getElementById('reason').value;
  if(!actor||!reason){alert('Actor and reason are required.');return;}
  if(!confirm('Apply the displayed catalog source association?'))return;
  const payload={...currentCatalogAssociationPayload,actor,reason,state_token:currentCatalogAssociationPreview.state_token};
  try{
    const value=await request('/api/catalog-association/apply',payload);
    renderHumanSummary(document.getElementById('catalog-association-preview'),value);
    document.getElementById('apply-catalog-association').disabled=true;
    setTimeout(()=>location.reload(),700);
  }catch(error){renderRequestError(document.getElementById('catalog-association-preview'),error);}
});
const lifecycleDialog=document.getElementById('lifecycle-dialog');
let lifecyclePreview=null;
function selectedLifecycleRole(){return document.querySelector('input[name="lifecycle-role"]:checked').value;}
function updateLifecycleWarning(){document.getElementById('lifecycle-warning').hidden=selectedLifecycleRole()!=='composite';}
document.querySelectorAll('input[name="lifecycle-role"]').forEach(input=>input.addEventListener('change',()=>{
  lifecyclePreview=null;
  document.getElementById('apply-lifecycle').disabled=true;
  document.getElementById('lifecycle-preview').textContent='Role changed; preview again.';
  updateLifecycleWarning();
}));
document.getElementById('classify-target').addEventListener('click',()=>{
  updateLifecycleWarning();
  lifecycleDialog.showModal();
});
document.getElementById('preview-lifecycle').addEventListener('click',async()=>{
  const role=selectedLifecycleRole();
  const payload={target:window.SDB_TARGET,role,state:role==='composite'?'system_only':'active'};
  try{
    lifecyclePreview=await request('/api/lifecycle/preview',payload);
    renderHumanSummary(document.getElementById('lifecycle-preview'),lifecyclePreview);
    prefillReason('lifecycle-reason',lifecyclePreview);
    document.getElementById('apply-lifecycle').disabled=!lifecyclePreview.has_changes;
  }catch(error){renderRequestError(document.getElementById('lifecycle-preview'),error);}
});
document.getElementById('apply-lifecycle').addEventListener('click',async()=>{
  if(!lifecyclePreview)return;
  const actor=document.getElementById('lifecycle-actor').value;
  const reason=document.getElementById('lifecycle-reason').value;
  if(!actor||!reason){alert('Actor and reason are required.');return;}
  const role=selectedLifecycleRole();
  const payload={target:window.SDB_TARGET,role,state:role==='composite'?'system_only':'active',actor,reason,state_token:lifecyclePreview.state_token};
  if(!confirm('Apply the displayed target modelling role?'))return;
  try{
    const value=await request('/api/lifecycle/apply',payload);
    renderHumanSummary(document.getElementById('lifecycle-preview'),value);
    document.getElementById('apply-lifecycle').disabled=true;
    setTimeout(()=>location.reload(),700);
  }catch(error){renderRequestError(document.getElementById('lifecycle-preview'),error);}
});
const relativesDialog=document.getElementById('relatives-dialog');
let relativesPreview=null;
async function refreshRelativesPreview(){
  const element=document.getElementById('relatives-preview');
  element.classList.add('muted');
  element.textContent='Loading current SIMBAD relatives…';
  document.getElementById('apply-relatives').disabled=true;
  try{
    relativesPreview=await request('/api/relatives/preview',{target:window.SDB_TARGET});
    renderHumanSummary(element,relativesPreview);
    prefillReason('relatives-reason',relativesPreview);
    document.getElementById('apply-relatives').disabled=!relativesPreview.has_changes;
  }catch(error){relativesPreview=null;renderRequestError(element,error);}
}
function openRelativesDialog(){
  if(!relativesDialog.open)relativesDialog.showModal();
  refreshRelativesPreview();
}
document.getElementById('preview-relatives').addEventListener('click',refreshRelativesPreview);
document.getElementById('apply-relatives').addEventListener('click',async()=>{
  if(!relativesPreview)return;
  const actor=document.getElementById('relatives-actor').value;
  const reason=document.getElementById('relatives-reason').value;
  if(!actor||!reason){alert('Actor and reason are required.');return;}
  if(!confirm('Import and reconcile the displayed immediate stellar relatives?'))return;
  const button=document.getElementById('apply-relatives');
  button.disabled=true;
  button.textContent='Importing…';
  try{
    const value=await request('/api/relatives/apply',{target:window.SDB_TARGET,actor,reason,state_token:relativesPreview.state_token});
    renderHumanSummary(document.getElementById('relatives-preview'),value);
    setTimeout(()=>location.reload(),1000);
  }catch(error){renderRequestError(document.getElementById('relatives-preview'),error);button.disabled=false;}
  finally{button.textContent='Import and reconcile stellar relatives';}
});
const nearbyImportDialog=document.getElementById('nearby-import-dialog');
let nearbyImportSearch=null;
function updateNearbyImportButton(){
  document.getElementById('apply-nearby-import').disabled=
    document.querySelectorAll('#nearby-import-rows input:checked').length===0;
}
function renderNearbyImportRows(value){
  const body=document.getElementById('nearby-import-rows');
  body.innerHTML=value.candidates.map(row=>{
    const params=new URLSearchParams({submit:'submit id',Ident:row.main_id});
    const simbadUrl=`https://simbad.cds.unistra.fr/simbad/sim-id?${params}`;
    const objectType=row.object_type_description||row.object_type_label||
      row.primary_object_type||
      (row.object_types||[]).join(', ')||'—';
    let status='New';
    if(row.current_target){
      status='Current target';
    }else if(row.existing_sdbid){
      status=`<a href="/target/${encodeURIComponent(row.existing_sdbid)}">${escapeHtml(row.existing_sdbid)}</a>`;
    }else if(row.blocked_reason){
      status=`Context only · ${escapeHtml(row.blocked_reason)}`;
    }
    const selection=row.selectable
      ? `<input type="checkbox" value="${escapeHtml(row.main_id)}" aria-label="Import ${escapeHtml(row.main_id)}">`
      : '—';
    return `<tr><td>${selection}</td>`+
      `<td><a href="${escapeHtml(simbadUrl)}" target="_blank" rel="noopener">${escapeHtml(row.main_id)}</a></td>`+
      `<td>${escapeHtml(objectType)}</td>`+
      `<td>${escapeHtml(row.spectral_type||'—')}</td>`+
      `<td>${Number(row.separation_arcsec).toFixed(2)}″</td>`+
      `<td>${status}</td></tr>`;
  }).join('');
  document.getElementById('nearby-import-results').hidden=false;
  body.querySelectorAll('input').forEach(
    input=>input.addEventListener('change',updateNearbyImportButton)
  );
  updateNearbyImportButton();
}
async function searchNearbyImport(){
  const radius=Number(document.getElementById('nearby-import-radius').value);
  const status=document.getElementById('nearby-import-search-status');
  const button=document.getElementById('search-nearby-import');
  if(!Number.isFinite(radius)||radius<=0||radius>600){
    alert('Radius must be between 1 and 600 arcsec.');
    return;
  }
  button.disabled=true;
  button.textContent='Searching…';
  document.getElementById('apply-nearby-import').disabled=true;
  document.getElementById('nearby-import-results').hidden=true;
  document.getElementById('nearby-import-summary').textContent='';
  document.getElementById('nearby-import-target-links').innerHTML='';
  status.classList.add('muted');
  status.textContent='Searching SIMBAD around the target position…';
  try{
    nearbyImportSearch=await request('/api/nearby-import/search',{
      target:window.SDB_TARGET,
      radius_arcsec:radius,
    });
    renderNearbyImportRows(nearbyImportSearch);
    status.textContent=`${nearbyImportSearch.candidates.length} object(s), ${nearbyImportSearch.new_count} available to import, ${nearbyImportSearch.blocked_count} context only; sorted by distance.`;
  }catch(error){
    nearbyImportSearch=null;
    renderRequestError(status,error);
  }finally{
    button.disabled=false;
    button.textContent='Search SIMBAD';
  }
}
document.getElementById('nearby-import').addEventListener('click',()=>{
  if(!nearbyImportDialog.open)nearbyImportDialog.showModal();
  if(!nearbyImportSearch)searchNearbyImport();
});
document.getElementById('search-nearby-import').addEventListener('click',searchNearbyImport);
document.getElementById('apply-nearby-import').addEventListener('click',async()=>{
  const selected=[
    ...document.querySelectorAll('#nearby-import-rows input:checked')
  ].map(input=>input.value);
  if(!selected.length)return;
  if(!confirm(`Import ${selected.length} selected SIMBAD object(s) and fill provider coverage?`))return;
  const button=document.getElementById('apply-nearby-import');
  const summary=document.getElementById('nearby-import-summary');
  button.disabled=true;
  button.textContent='Importing and updating…';
  try{
    const value=await request('/api/nearby-import/apply',{
      target:window.SDB_TARGET,
      main_ids:selected,
    });
    renderHumanSummary(summary,value);
    const links=value.items.filter(item=>item.sdbid).map(item=>
      `<a href="/target/${encodeURIComponent(item.sdbid)}">Open ${escapeHtml(item.requested_name)}</a>`
    );
    document.getElementById('nearby-import-target-links').innerHTML=links.join('');
    for(const input of document.querySelectorAll('#nearby-import-rows input:checked')){
      input.checked=false;
      input.disabled=true;
      input.closest('tr').lastElementChild.textContent='Imported';
    }
  }catch(error){
    renderRequestError(summary,error);
    button.disabled=false;
  }finally{
    button.textContent='Import selected';
    updateNearbyImportButton();
  }
});
const catalogCoverageDialog=document.getElementById('catalog-coverage-dialog');
let catalogCoveragePreview=null;
async function refreshCatalogCoveragePreview(){
  const element=document.getElementById('catalog-coverage-preview');
  const applyButton=document.getElementById('apply-catalog-coverage');
  element.classList.add('muted');
  element.textContent='Checking direct provider coverage…';
  applyButton.disabled=true;
  try{
    catalogCoveragePreview=await request('/api/catalog-coverage/preview',{target:window.SDB_TARGET});
    renderHumanSummary(element,catalogCoveragePreview);
    applyButton.disabled=!catalogCoveragePreview.has_changes||!catalogCoveragePreview.action_available;
  }catch(error){
    catalogCoveragePreview=null;
    renderRequestError(element,error);
  }
}
document.getElementById('catalog-coverage').addEventListener('click',()=>{
  if(!catalogCoverageDialog.open)catalogCoverageDialog.showModal();
  refreshCatalogCoveragePreview();
});
document.getElementById('preview-catalog-coverage').addEventListener('click',refreshCatalogCoveragePreview);
document.getElementById('apply-catalog-coverage').addEventListener('click',async()=>{
  if(!catalogCoveragePreview)return;
  if(!confirm('Complete the displayed catalog normalization and provider gaps?'))return;
  const button=document.getElementById('apply-catalog-coverage');
  button.disabled=true;
  button.textContent='Updating…';
  try{
    const value=await request('/api/catalog-coverage/apply',{
      target:window.SDB_TARGET,
      state_token:catalogCoveragePreview.state_token,
    });
    catalogCoveragePreview=value;
    renderHumanSummary(document.getElementById('catalog-coverage-preview'),value);
    setTimeout(()=>location.reload(),1000);
  }catch(error){
    renderRequestError(document.getElementById('catalog-coverage-preview'),error);
    button.disabled=false;
  }finally{
    button.textContent='Complete catalog gaps';
  }
});
