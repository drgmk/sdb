# SDB roadmap

The Python/SQLite port, hierarchy-aware photometry foundation, operator
decision model, review workspace, and SDF-compatible export path are
implemented. Current work is review-UI validation on representative systems.

Near-term operator-workflow follow-ups from the parity rehearsal:

- Repeat the parity rehearsal with the completed review-UI sequence and
  summary-first proposal/reference workflows.

## Review UI implementation sequence

1. **Implemented:** Correct and condense the SIMBAD system-context projection.
   - Group immediate relationships by direction and related SIMBAD OID. Keep
     all relationship IDs and bibliography as provenance, but show one
     relative in the UI. The six Argus Association parents currently shown
     for `sdbid-v3-073547.46-321214.0` are one parent with several references,
     not six hierarchy objects.
   - Distinguish a new import, an existing target that still needs structural
     reconciliation, and a fully reconciled relative. Only the first two
     states should enable the import/reconcile action.
   - Add a compact current-SIMBAD summary for each displayed SDB target:
     preferred SIMBAD name, distance where a valid parallax is available,
     spectral type, and primary object type.

2. **Implemented:** Redesign the left-hand system context around targets and
   relationships.
   - Use each target's SIMBAD `main_id` as the visible linked identity,
     including assignment choices and component links; keep the SDBID as the
     internal link value rather than repeating it in the system panel.
   - Give the requested target its own compact summary above system context;
     do not repeat it under nearby targets.
   - Show immediate relatives first with the same compact target facts, then
     show only nearby SDB targets that were not already listed. Keep
     context-only relatives brief.
   - Remove the band summary and hierarchy-candidate count from each target.
   - Consolidate the separate Hierarchy and Components sections into one
     provider-system tree. Each component should show its SDB target link, or
     that it is not imported, plus compact match/geometry state.
   - Keep immediate SIMBAD relatives separate from WDS/CCDM hierarchy because
     they are different evidence, but render the aggregated state from item 1.

3. **Implemented:** Declutter the plot and plotted-item list without
   discarding context.
   - Use short WDS/CCDM hover text (provider/system, component, status, and
     separation); keep full evidence in Selected point.
   - Default the list to assigned or actionable items: the requested target,
     accepted or ambiguous source rows, explicit system members, and
     cross-linked candidates. Leave review neighbours, unrelated nearby
     targets, and inactive rejected/no-match rows on the plot.
   - Add a `Show all plotted items` control rather than making the relevance
     boundary irreversible. Represent relevance in the review projection, not
     as persisted domain state.

4. **Implemented:** Compact the photometry matrix by detection.
   - Collapse `(provider, source_id, band)` rows to `(provider, source_id)`,
     showing the band count and an aggregate assignment state.
   - Do not collapse an entire provider to one row: one provider can have
     several detections with different ownership.
   - Mark mixed band assignments explicitly and keep all per-band values and
     controls in the assignment drawer. For the current HD 61005 rehearsal
     target this changes 26 matrix rows into 8 detection rows.

5. **Implemented:** Clarify and re-layout the photometry assignment drawer.
   - Replace `Composite scope target` plus `retain selected target as
     composite scope` with one optional `Measurement applies to the combined
     system` control. Show a `System target` selector only when it is enabled;
     keep contributor selection as the separate statement of which physical
     models contribute.
   - Put the Decision and Fit include/exclude preview buttons side by side in
     the compact drawer, with their preview boxes stacked underneath.
   - Rename `Fit eligibility preview` to `Fit include/exclude preview` and
     adjust the associated button and help text. Keep assignment and
     include/exclude as separate audited applies.
   - Replace the three-state include/exclude selector with one action button
     per band. The current state is shown directly; clicking proposes the
     opposite state and clicking again cancels the pending change.

6. **Implemented:** Add actions for ambiguous or failed provider results.
   - From the selected point/details UI, allow an operator to accept a
     candidate, record a reviewed no-match, or retry a genuine provider
     failure.
   - Reuse the relevance and selected-point work from item 3 so the workflow
     does not require translating UI target IDs into CLI commands.

Deliberately deferred:

- Teach SDF to consume the versioned joint-fit manifest and sum component
  predictions for unresolved measurements.
- Replace SDF's legacy ALMA lookup with SDB's cached, motion-aware lookup in a
  coordinated SDB/SDF deployment.
- Harden resumable bulk-provider operation at much larger target counts and
  revisit the Gaia TAP/VizieR bulk comparison.
- Add further photometric catalogs, spectra, and far-IR/mm products when a
  concrete science or parity requirement selects them.
- Consider a persistent authenticated review service only after the local
  operator workflow has settled.

See `plan.md` for the chronological implementation record.
