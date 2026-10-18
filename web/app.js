const chatLog = document.querySelector("#chat-log");
const form = document.querySelector("#chat-form");
const queryBox = document.querySelector("#query");
const sendButton = document.querySelector("#send-button");
const settingsPanel = document.querySelector("#settings-panel");
const settingsToggle = document.querySelector("#settings-toggle");
const settingsClose = document.querySelector("#settings-close");
const conversationHistory = [];

function rememberTurn(entry) {
  conversationHistory.push(entry);
  if (conversationHistory.length > 12) {
    conversationHistory.splice(0, conversationHistory.length - 12);
  }
}

function summarizeResponseContext(data) {
  const context = {};
  const recipeName = data.missing_data?.recipe_name || data.recipe?.name;
  if (recipeName) context.recipe = recipeName;
  const sourceIngredients = data.missing_data?.raw_ingredient_names;
  if (sourceIngredients?.length) context.recipe_ingredient_names = sourceIngredients.slice(0, 30);
  const sourceItems = data.missing_data?.raw_ingredient_items;
  if (sourceItems?.length) context.recipe_ingredient_items = sourceItems.slice(0, 30).map(item => ({
    name: item.name ?? null,
    quantity_raw: item.quantity_raw ?? null,
  }));
  const sourceServings = data.missing_data?.recipe_servings_raw ?? data.missing_data?.recipe_servings;
  if (sourceServings != null) context.recipe_servings = String(sourceServings);
  if (data.ingredient_results?.length) {
    context.ingredient_lookups = data.ingredient_results.slice(0, 10).map(result => ({
      name: result.query,
      status: result.match_status || (result.found ? "found" : "not_found"),
      records: (result.matches || []).slice(0, 3).map(match => ({
        name: match.name,
        dataset_key: match.source?.dataset_key,
      })),
    }));
  }
  if (data.dataset_scope) context.dataset_scope = data.dataset_scope;
  if (data.composition_assessment?.ingredients?.length) {
    context.composition_ingredients = data.composition_assessment.ingredients
      .slice(0, 12).map(food => food.name || food.query);
  }
  return context;
}

function formatStatus(status) {
  const labels = {
    NOT_EVALUATED: "NOT EVALUATED",
    CALCULATED: "CALCULATED · NOT ASSESSED",
  };
  return labels[status] || String(status || "").replaceAll("_", " ");
}

function setSettingsOpen(open) {
  settingsPanel.classList.toggle("is-open", open);
  settingsToggle.setAttribute("aria-expanded", String(open));
  if (open) settingsClose.focus();
  else settingsToggle.focus();
}

settingsToggle.addEventListener("click", () => {
  setSettingsOpen(!settingsPanel.classList.contains("is-open"));
});
settingsClose.addEventListener("click", () => setSettingsOpen(false));
document.addEventListener("keydown", event => {
  if (event.key === "Escape" && settingsPanel.classList.contains("is-open")) {
    setSettingsOpen(false);
  }
});
window.addEventListener("resize", () => {
  if (window.innerWidth > 720 && settingsPanel.classList.contains("is-open")) {
    settingsPanel.classList.remove("is-open");
    settingsToggle.setAttribute("aria-expanded", "false");
  }
});

function addMessage(role, text, payload = null) {
  const article = document.createElement("article");
  article.className = `message ${role}`;
  const avatar = document.createElement("div");
  avatar.className = "avatar";
  avatar.textContent = role === "assistant" ? "R" : "S";
  const body = document.createElement("div");
  body.className = "message-body";
  const meta = document.createElement("div");
  meta.className = "message-meta";
  meta.textContent = role === "assistant" ? "RENALLY · PROTOTYPE" : "YOU";
  const bubble = document.createElement("div");
  bubble.className = "bubble";
  const message = document.createElement("p");
  message.textContent = text;
  bubble.append(message);
  if (payload) bubble.append(renderResult(payload));
  body.append(meta, bubble);
  article.append(role === "assistant" ? avatar : body, role === "assistant" ? body : avatar);
  chatLog.append(article);
  chatLog.scrollTop = chatLog.scrollHeight;
}

function renderComparisonTable(rows, caption) {
  if (!rows?.length) return null;
  const wrap = document.createElement("div");
  wrap.className = "comparison-block";
  const label = document.createElement("p");
  label.className = "quiet-note comparison-caption";
  label.textContent = caption;
  const tableWrap = document.createElement("div");
  tableWrap.className = "comparison-scroll";
  const table = document.createElement("table");
  table.className = "comparison-table";
  const thead = document.createElement("thead");
  const header = document.createElement("tr");
  for (const text of ["Nutrient", "Original", "Replacement", "Change"]) {
    const cell = document.createElement("th");
    cell.textContent = text;
    header.append(cell);
  }
  thead.append(header);
  const tbody = document.createElement("tbody");
  for (const row of rows) {
    const tr = document.createElement("tr");
    const nutrient = document.createElement("th");
    nutrient.scope = "row";
    nutrient.textContent = row.label || row.nutrient;
    tr.append(nutrient);
    for (const [index, value] of [row.original, row.replacement, row.delta].entries()) {
      const cell = document.createElement("td");
      const formatted = Number(value).toFixed(1);
      cell.textContent = `${index === 2 && value > 0 ? "+" : ""}${formatted} ${row.unit || ""}`;
      tr.append(cell);
    }
    tbody.append(tr);
  }
  table.append(thead, tbody);
  tableWrap.append(table);
  wrap.append(label, tableWrap);
  return wrap;
}

function appendFoodComPreview(container, record) {
  const warning = document.createElement("p");
  warning.className = "data-note";
  warning.textContent = "Food.com ingredient quantities and servings are shown exactly as provided. Their units and links to USDA food records are not verified, so they are not used for nutrient totals or dietary checks.";
  container.append(warning);
  const sourceUrl = record.source?.dataset_url || record.source?.url;
  if (sourceUrl) {
    const link = document.createElement("a");
    link.className = "source-link";
    link.href = sourceUrl;
    link.target = "_blank";
    link.rel = "noreferrer";
    link.textContent = record.source.name || record.source_dataset || "Food.com recipe source";
    container.append(link);
  }
  const sourceServings = record.recipe_servings_raw ?? record.recipe_servings;
  if (sourceServings != null || record.recipe_yield) {
    const servings = document.createElement("p");
    servings.className = "quiet-note";
    servings.textContent = [
      sourceServings != null ? `Source servings: ${sourceServings}` : null,
      record.recipe_yield ? `Source yield: ${record.recipe_yield}` : null,
    ].filter(Boolean).join(" · ");
    container.append(servings);
  }
  const sourceItems = record.raw_ingredient_items;
  if (sourceItems?.length || record.raw_ingredient_names?.length) {
    const details = document.createElement("details");
    details.className = "result-details";
    const summary = document.createElement("summary");
    summary.textContent = sourceItems?.length
      ? "Food.com ingredients · source quantities as provided"
      : "Food.com ingredient names · source quantities unavailable";
    const list = document.createElement("ul");
    const rows = sourceItems?.length
      ? sourceItems
      : record.raw_ingredient_names.map(name => ({name, quantity_raw: null}));
    for (const item of rows) {
      const row = document.createElement("li");
      if (item.name && item.quantity_raw != null && String(item.quantity_raw).trim()) {
        row.textContent = `${item.name} · ${item.quantity_raw}`;
      } else if (item.name) {
        row.textContent = `${item.name} · quantity not listed`;
      } else if (item.quantity_raw != null && String(item.quantity_raw).trim()) {
        row.textContent = `Unmatched source quantity: ${item.quantity_raw}`;
      } else {
        row.textContent = "Source ingredient entry has no name or quantity";
      }
      list.append(row);
    }
    details.append(summary, list);
    container.append(details);
  }
  if (record.method?.length) {
    const details = document.createElement("details");
    details.className = "result-details";
    const summary = document.createElement("summary");
    summary.textContent = "Original cooking instructions · unverified source text";
    const list = document.createElement("ol");
    for (const step of record.method) {
      const row = document.createElement("li");
      row.textContent = step;
      list.append(row);
    }
    details.append(summary, list);
    container.append(details);
  }
}

function appendIngredientResolution(container, enrichment) {
  const heading = document.createElement("h4");
  heading.className = "plan-heading";
  heading.textContent = "Ingredient identity resolution";
  container.append(heading);
  const note = document.createElement("p");
  note.className = "quiet-note";
  note.textContent = "USDA candidates are checked using the recipe context and local Qwen when needed. A matched food identity does not validate the source quantity or provide nutrient totals.";
  container.append(note);
  if (enrichment.ingredient_limit_reached) {
    const limit = document.createElement("p");
    limit.className = "data-note";
    limit.textContent = `Only the first 20 of ${enrichment.source_ingredient_count} source ingredients were checked in this chat response.`;
    container.append(limit);
  }
  if (enrichment.error) {
    const error = document.createElement("p");
    error.className = "data-note";
    error.textContent = enrichment.error;
    container.append(error);
  }
  const list = document.createElement("div");
  list.className = "ingredient-resolution-list";
  for (const row of enrichment.results || []) {
    const item = document.createElement("div");
    item.className = "ingredient-resolution-item";
    const head = document.createElement("div");
    head.className = "result-head";
    const name = document.createElement("strong");
    name.textContent = row.original_name || row.original_text || "Unparsed ingredient";
    const badge = document.createElement("span");
    badge.className = "status-pill";
    const mapping = row.mapping || {};
    badge.textContent = mapping.fdc_id != null ? "USDA ID FOUND" :
      mapping.resolved_family ? "FAMILY FOUND" : "UNRESOLVED";
    if (mapping.fdc_id == null) badge.classList.add("status-unknown");
    head.append(name, badge);
    item.append(head);
    const identity = document.createElement("p");
    identity.className = "quiet-note";
    identity.textContent = mapping.fdc_id != null
      ? `${mapping.usda_description || "USDA food"} · FDC ${mapping.fdc_id}`
      : mapping.resolved_family
        ? `${mapping.resolved_family.replaceAll("_", " ")} · specific variant unresolved`
        : "No specific USDA food identity could be selected.";
    item.append(identity);
    if (mapping.fdc_id == null && !mapping.resolved_family && mapping.unresolved_attributes?.length) {
      const missing = mapping.unresolved_attributes.filter(value => value !== "data_type").slice(0, 4);
      if (missing.length) {
        const reason = document.createElement("p");
        reason.className = "quiet-note";
        reason.textContent = `Source does not specify: ${missing.map(value => value.replaceAll("_", " ")).join(", ")}.`;
        item.append(reason);
      }
    }
    const quantity = document.createElement("p");
    quantity.className = "quiet-note";
    const amount = row.quantity_raw || (enrichment.quantity_alignment_status === "MISMATCHED_ARRAY_LENGTHS"
      ? "not assignable because source lists do not align" : "not listed");
    quantity.textContent = `Source amount: ${amount} · gram amount ${row.quantity_g == null ? "unresolved" : `${row.quantity_g} g (${String(row.quantity_status || "parsed").replaceAll("_", " ").toLowerCase()})`}`;
    item.append(quantity);
    if (mapping.fdc_id == null && mapping.candidates?.length) {
      const details = document.createElement("details");
      details.className = "result-details";
      const summary = document.createElement("summary");
      summary.textContent = "Possible USDA records (not confirmed)";
      const options = document.createElement("ul");
      const plausible = new Set((mapping.candidate_ids || []).map(String));
      for (const candidate of mapping.candidates) {
        if (plausible.size && !plausible.has(String(candidate.fdc_id))) continue;
        const option = document.createElement("li");
        option.textContent = `${candidate.description || "USDA food"} · FDC ${candidate.fdc_id}`;
        options.append(option);
      }
      details.append(summary, options);
      item.append(details);
    }
    list.append(item);
  }
  container.append(list);
}

function renderResult(data) {
  const fragment = document.createDocumentFragment();
  if (data.tool_trace?.length) {
    const details = document.createElement("details");
    details.className = "result-details tool-trace";
    const summary = document.createElement("summary");
    summary.textContent = "What RenAlly checked";
    const list = document.createElement("ul");
    for (const step of data.tool_trace) {
      const row = document.createElement("li");
      row.textContent = step;
      list.append(row);
    }
    details.append(summary, list);
    fragment.append(details);
  }
  if (data.slm_warning) {
    const warning = document.createElement("p");
    warning.className = "data-note error-note";
    warning.textContent = data.slm_warning;
    fragment.append(warning);
  }
  if (data.reference_guidance?.length) {
    const details = document.createElement("details");
    details.className = "result-details";
    const summary = document.createElement("summary");
    summary.textContent = "Clinical reference context (not meal limits)";
    const list = document.createElement("ul");
    for (const rule of data.reference_guidance) {
      const row = document.createElement("li");
      const value = rule.value == null ? "individualized" : `${rule.type} ${rule.value} ${rule.unit || ""}`;
      row.textContent = `${rule.nutrient}: ${value} (${rule.scope}; ${rule.source})`;
      list.append(row);
    }
    details.append(summary, list);
    fragment.append(details);
  }
  if (data.profile_conflicts?.length) {
    for (const text of data.profile_conflicts) {
      const note = document.createElement("p");
      note.className = "data-note";
      note.textContent = `Profile conflict: ${text}`;
      fragment.append(note);
    }
  }
  if (!Object.keys(data.active_budgets || {}).length && data.conditions?.length) {
    const note = document.createElement("p");
    note.className = "data-note";
    note.textContent = "Selected profile references are context only. No numeric meal or remaining-intake limit was entered, so they did not filter this result.";
    fragment.append(note);
  }
  if (data.composition_assessment) {
    const assessment = data.composition_assessment;
    const card = document.createElement("section");
    card.className = "result-card composition-card";
    const head = document.createElement("div");
    head.className = "result-head";
    const title = document.createElement("strong");
    title.textContent = assessment.recipe?.name || "Ingredient combination";
    const pill = document.createElement("span");
    pill.className = `status-pill ${assessment.status === "FAIL" ? "status-fail" : assessment.status === "PASS" ? "" : "status-unknown"}`;
    pill.textContent = formatStatus(assessment.status);
    head.append(title, pill);
    const contents = document.createElement("div");
    contents.className = "result-body";
    const basis = document.createElement("p");
    basis.className = "quiet-note";
    const hasCompositionTotals = Object.keys(assessment.nutrients_per_serving || {}).length > 0;
    basis.textContent = hasCompositionTotals
      ? `Ingredient weights are per serving. Totals below show one serving${data.servings > 1 ? ` and ${data.servings} requested servings` : ""}. USDA values use each food's recorded condition; cooking yield and nutrient retention are not modeled.`
      : "Totals are withheld until every requested food and weight can be matched to a FoodData Central record.";
    contents.append(basis);
    if (assessment.recipe?.description) {
      const description = document.createElement("p");
      description.className = "data-note";
      description.textContent = assessment.recipe.description;
      contents.append(description);
    }

    if (assessment.ingredients?.length) {
      const ingredientDetails = document.createElement("details");
      ingredientDetails.className = "result-details";
      const summary = document.createElement("summary");
      summary.textContent = "Foods, amounts and FoodData Central sources";
      const list = document.createElement("ul");
      for (const food of assessment.ingredients) {
        const row = document.createElement("li");
        if (food.found === false) {
          const options = food.ambiguous_options?.map(option => {
            const details = [option.description, option.source?.name].filter(Boolean).join(" · ");
            return details ? `${option.name} (${details})` : option.name;
          }).join("; ");
          row.textContent = `${food.query}: ${food.ambiguous_options?.length ? `ambiguous${options ? ` — ${options}` : ""}` : "no FoodData Central record found"}`;
        } else {
          row.textContent = `${food.name}: ${Number(food.amount_g).toFixed(2)} g per serving${food.ingredient_type ? ` · ${food.ingredient_type}` : ""}`;
          if (food.source?.url) {
            const source = document.createElement("a");
            source.className = "source-link";
            source.href = food.source.url;
            source.target = "_blank";
            source.rel = "noreferrer";
            source.textContent = `${food.source.name || "USDA FoodData Central"}${food.source.fdc_id ? ` · FDC ${food.source.fdc_id}` : ""}`;
            row.append(" ", source);
          }
        }
        list.append(row);
      }
      ingredientDetails.append(summary, list);
      contents.append(ingredientDetails);
    }

    const appendNutrients = (values, heading) => {
      if (!Object.keys(values || {}).length) return;
      const label = document.createElement("p");
      label.className = "comparison-caption";
      label.textContent = heading;
      const grid = document.createElement("div");
      grid.className = "nutrient-grid";
      for (const [key, value] of Object.entries(values)) {
        const item = document.createElement("div");
        item.className = "nutrient-item";
        const nutrient = document.createElement("span");
        nutrient.textContent = key.replaceAll("_", " ");
        const amount = document.createElement("strong");
        amount.textContent = `${Number(value).toFixed(2)} ${assessment.nutrient_units?.[key] || ""}`;
        item.append(nutrient, amount);
        grid.append(item);
      }
      contents.append(label, grid);
    };
    appendNutrients(assessment.nutrients_per_serving, "Calculated nutrients · 1 serving");
    if (data.servings > 1) {
      appendNutrients(assessment.nutrients_for_requested_servings, `Calculated nutrients · ${data.servings} servings`);
    }
    if (assessment.unavailable_nutrients?.length) {
      const missing = document.createElement("p");
      missing.className = "data-note";
      missing.textContent = `Not calculated because source values are missing: ${assessment.unavailable_nutrients.map(name => name.replaceAll("_", " ")).join(", ")}. Missing values are not treated as zero.`;
      contents.append(missing);
    }
    if (assessment.method?.length) {
      const methodDetails = document.createElement("details");
      methodDetails.className = "result-details";
      const summary = document.createElement("summary");
      summary.textContent = assessment.method_source === "slm_generated"
        ? "SLM suggested preparation · validated foods only"
        : assessment.method_source === "slm_adapted"
          ? "SLM adapted the local recipe method · validated foods only"
          : assessment.method_source === "local_recipe_adapted"
            ? "Local recipe method with added validated foods"
            : assessment.method_source === "local_recipe" ? "Method from local recipe graph" : "Suggested preparation";
      const list = document.createElement("ol");
      for (const step of assessment.method) {
        const row = document.createElement("li");
        row.textContent = step;
        list.append(row);
      }
      methodDetails.append(summary, list);
      contents.append(methodDetails);
    }
    if (assessment.method_check) {
      const validation = document.createElement("p");
      validation.className = `data-note ${assessment.method_check.passed ? "" : "error-note"}`;
      validation.textContent = assessment.method_check.passed
        ? ["slm_generated", "slm_adapted"].includes(assessment.method_source)
          ? "SLM steps were rendered from validated ingredient IDs; no extra foods or quantities were added."
          : "Suggested method checked against the validated ingredient list."
        : `Method failed validation: ${(assessment.method_check.issues || []).join("; ")}`;
      contents.append(validation);
    }
    if (assessment.recipe?.source?.url) {
      const source = document.createElement("a");
      source.className = "source-link";
      source.href = assessment.recipe.source.url;
      source.target = "_blank";
      source.rel = "noreferrer";
      source.textContent = `${assessment.recipe.source.name || "Recipe source"}${assessment.recipe.source.basis ? ` · ${assessment.recipe.source.basis}` : ""}`;
      contents.append(source);
    }
    if (assessment.constraint_check?.reasons?.length || assessment.constraint_check?.not_evaluated?.length) {
      const check = document.createElement("p");
      check.className = "data-note";
      const messages = [...(assessment.constraint_check.reasons || []), ...(assessment.constraint_check.not_evaluated || [])];
      check.textContent = `Constraint check: ${messages.join("; ")}`;
      contents.append(check);
    }
    card.append(head, contents);
    fragment.append(card);
  }
  if (Object.keys(data.active_budgets || {}).length) {
    const note = document.createElement("p");
    note.className = "data-note";
    const labels = Object.entries(data.active_budgets).map(([name, rule]) =>
      `${name} ≤ ${rule.max} ${rule.unit} (${rule.scope})`
    );
    const catalogLookup = data.ingredient_results?.some(result => result.catalog_lookup);
    note.textContent = data.constraints_applied === false
      ? `Limits entered: ${labels.join("; ")}. They were not applied to this ${catalogLookup ? "catalog membership lookup" : "per-100 g ingredient lookup"}.`
      : `Applied limits: ${labels.join("; ")}.`;
    fragment.append(note);
  }
  if (data.ingredient_results?.length) {
    const heading = document.createElement("h3");
    heading.className = "plan-heading";
    heading.textContent = "Ingredient lookup";
    fragment.append(heading);
    if (data.missing_data?.raw_ingredient_names?.length) {
      const note = document.createElement("p");
      note.className = "quiet-note";
      note.textContent = "Ingredient names were searched in Foundation Foods and SR Legacy. Results are catalog name matches only; they are not linked to the source recipe. Source quantities remain unverified and are not used for nutrient calculations.";
      fragment.append(note);
    }
    for (const result of data.ingredient_results) {
      const card = document.createElement("section");
      card.className = "result-card";
      const head = document.createElement("div");
      head.className = "result-head";
      const title = document.createElement("strong");
      title.textContent = result.catalog_lookup ? result.query : (result.found ? result.name : result.query);
      const pill = document.createElement("span");
      pill.className = "status-pill";
      if (result.catalog_lookup) {
        pill.textContent = result.match_status === "exact" ? "EXACT NAME" :
          result.match_status === "matches_found" ? "MATCHES FOUND" : "NOT FOUND";
      } else if (result.ambiguous) {
        pill.textContent = "CHOOSE SOURCE";
      } else {
        pill.textContent = result.found ? "DATA FOUND" : "NOT FOUND";
      }
      head.append(title, pill);
      const contents = document.createElement("div");
      contents.className = "result-body";
      if (result.catalog_lookup) {
        if (result.matches?.length) {
          const note = document.createElement("p");
          note.className = "quiet-note";
          note.textContent = result.match_status === "exact"
            ? "The requested name matches a label or alias in the selected USDA data scope."
            : "These USDA labels contain the requested ingredient words; check each source and recorded food state.";
          contents.append(note);
          const list = document.createElement("ul");
          list.className = "catalog-match-list";
          for (const match of result.matches) {
            const row = document.createElement("li");
            const name = document.createElement("strong");
            name.textContent = match.name;
            const description = document.createElement("span");
            description.textContent = match.description || "USDA FoodData Central record";
            row.append(name, description);
            if (match.source?.url) {
              const source = document.createElement("a");
              source.className = "source-link";
              source.href = match.source.url;
              source.target = "_blank";
              source.rel = "noreferrer";
              source.textContent = `${match.source.name || "USDA FoodData Central"}${match.source.fdc_id ? ` · FDC ${match.source.fdc_id}` : ""}`;
              row.append(source);
            }
            list.append(row);
          }
          contents.append(list);
        } else {
          const note = document.createElement("p");
          note.className = "data-note";
          note.textContent = result.message;
          contents.append(note);
        }
        if (result.related_matches?.length) {
          const related = document.createElement("details");
          const summary = document.createElement("summary");
          summary.textContent = "Related records (not confirmed matches)";
          related.append(summary);
          const list = document.createElement("ul");
          list.className = "catalog-match-list";
          for (const match of result.related_matches) {
            const row = document.createElement("li");
            const name = document.createElement("strong");
            name.textContent = match.name;
            const description = document.createElement("span");
            description.textContent = match.description || "USDA FoodData Central record";
            row.append(name, description);
            list.append(row);
          }
          related.append(list);
          contents.append(related);
        }
      } else if (result.ambiguous) {
        const note = document.createElement("p");
        note.className = "data-note";
        note.textContent = result.message || "Separate USDA records match this name. Choose a source or food state.";
        contents.append(note);
        const list = document.createElement("ul");
        list.className = "catalog-match-list";
        for (const match of result.matches || []) {
          const row = document.createElement("li");
          const name = document.createElement("strong");
          name.textContent = match.name;
          const description = document.createElement("span");
          description.textContent = match.description || "USDA FoodData Central record";
          row.append(name, description);
          if (match.source?.url) {
            const source = document.createElement("a");
            source.className = "source-link";
            source.href = match.source.url;
            source.target = "_blank";
            source.rel = "noreferrer";
            source.textContent = `${match.source.name || "USDA FoodData Central"}${match.source.fdc_id ? ` · FDC ${match.source.fdc_id}` : ""}`;
            row.append(source);
          }
          list.append(row);
        }
        contents.append(list);
      } else if (result.found) {
        const basis = document.createElement("p");
        basis.className = "quiet-note";
        const type = result.ingredient_type ? ` · type: ${result.ingredient_type}` : "";
        basis.textContent = `${result.description}${type} · nutrient values are per 100 g of this recorded food state.`;
        contents.append(basis);
        const grid = document.createElement("div");
        grid.className = "nutrient-grid";
        for (const [key, value] of Object.entries(result.nutrients_per_100g || {})) {
          const item = document.createElement("div");
          item.className = "nutrient-item";
          const label = document.createElement("span");
          label.textContent = result.nutrient_names?.[key] || key.replaceAll("_", " ");
          const amount = document.createElement("strong");
          amount.textContent = `${Number(value).toFixed(1)} ${result.nutrient_units?.[key] || ""}`;
          item.append(label, amount);
          grid.append(item);
        }
        contents.append(grid);
        if (result.source?.url) {
          const source = document.createElement("a");
          source.className = "source-link";
          source.href = result.source.url;
          source.target = "_blank";
          source.rel = "noreferrer";
          source.textContent = `${result.source.name || "Source"}${result.source.fdc_id ? ` · FDC ${result.source.fdc_id}` : ""}`;
          contents.append(source);
        }
      } else {
        const note = document.createElement("p");
        note.className = "data-note";
        note.textContent = result.message;
        contents.append(note);
      }
      card.append(head, contents);
      fragment.append(card);
    }
  }
  if (data.substitution_assessment) {
    const assessment = data.substitution_assessment;
    const card = document.createElement("section");
    card.className = "result-card substitution-card";
    const head = document.createElement("div");
    head.className = "result-head";
    const title = document.createElement("strong");
    title.textContent = assessment.recipe?.name || "Ingredient substitution check";
    const pill = document.createElement("span");
    const isPass = ["PASS_SAME_AMOUNT", "PASS_ADJUSTED_AMOUNT"].includes(assessment.status);
    pill.className = `status-pill ${assessment.status === "FAIL_LIMITS" ? "status-fail" : isPass ? "" : "status-unknown"}`;
    const statusLabels = {
      PASS_SAME_AMOUNT: "PASS · SAME AMOUNT",
      PASS_ADJUSTED_AMOUNT: "PASS · ADJUSTED",
      FAIL_LIMITS: "FAILS LIMITS",
      COMPARISON_ONLY: "COMPARISON ONLY",
      NOT_ASSESSABLE: "NOT ASSESSABLE",
      AMBIGUOUS_RECIPE: "CHOOSE A RECIPE",
      SOURCE_NOT_IN_RECIPE: "NOT IN RECIPE",
      NO_RECIPE_CONTEXT: "NO RECIPE PORTION",
      RECIPE_NOT_FOUND: "RECIPE NOT FOUND",
      NEEDS_DETAILS: "NEEDS DETAILS",
      AMBIGUOUS_INGREDIENT: "SPECIFY FOOD STATE",
      ADJUSTMENT_NOT_VERIFIABLE: "AMOUNT NOT VERIFIED",
    };
    pill.textContent = statusLabels[assessment.status] || assessment.status;
    head.append(title, pill);
    const contents = document.createElement("div");
    contents.className = "result-body";
    const basis = document.createElement("p");
    basis.className = "quiet-note";
    basis.textContent = assessment.recipe
      ? `Amounts and nutrient totals are per serving${data.servings > 1 ? `; request covers ${data.servings} servings` : ""}. USDA ingredient values are attached to their recorded food state.`
      : "Ingredient comparison is per 100 g of each recorded food state; no recipe portion was available.";
    contents.append(basis);

    const foods = document.createElement("div");
    foods.className = "substitution-foods";
    for (const [label, food] of [["Replace", assessment.original], ["With", assessment.substitute]]) {
      if (!food) continue;
      const item = document.createElement("div");
      item.className = "substitution-food";
      const name = document.createElement("strong");
      name.textContent = `${label}: ${food.name}`;
      const description = document.createElement("small");
      description.textContent = food.description || "USDA FoodData Central record";
      item.append(name, description);
      if (food.source?.url) {
        const source = document.createElement("a");
        source.className = "source-link";
        source.href = food.source.url;
        source.target = "_blank";
        source.rel = "noreferrer";
        source.textContent = `${food.source.name || "USDA FoodData Central"}${food.source.fdc_id ? ` · FDC ${food.source.fdc_id}` : ""}`;
        item.append(source);
      }
      foods.append(item);
    }
    if (foods.childElementCount) contents.append(foods);
    for (const ambiguous of assessment.ambiguous_ingredients || []) {
      const note = document.createElement("p");
      note.className = "data-note";
      const options = ambiguous.options.map(option => option.description
        ? `${option.name} (${option.description})`
        : option.name).join("; ");
      note.textContent = `Multiple records matched ${ambiguous.side} ingredient “${ambiguous.query}”: ${options}.`;
      contents.append(note);
    }

    if (assessment.recommended_amount_g != null) {
      const portion = document.createElement("p");
      portion.className = "data-note";
      const replacementName = assessment.substitute?.name || "replacement";
      portion.textContent = `${replacementName}: ${Number(assessment.recommended_amount_g).toFixed(2)} g per serving. ${assessment.amount_basis || ""}`;
      contents.append(portion);
    } else if (assessment.same_amount_g != null) {
      const portion = document.createElement("p");
      portion.className = "data-note";
      const rangeNote = assessment.same_amount_within_prototype_range === false
        ? " This equal-weight amount is outside the recorded prototype range."
        : "";
      portion.textContent = `Compared at the original ${Number(assessment.same_amount_g).toFixed(1)} g per serving.${rangeNote}`;
      contents.append(portion);
    }
    if (assessment.protein_equivalent_amount_g != null) {
      const equivalent = document.createElement("p");
      equivalent.className = "data-note";
      const rangeNote = assessment.protein_equivalent_within_prototype_range === false
        ? " This amount is outside the recorded prototype portion range."
        : "";
      equivalent.textContent = `Protein-equivalent comparison: ${Number(assessment.protein_equivalent_amount_g).toFixed(1)} g of the replacement matches protein only.${rangeNote}`;
      contents.append(equivalent);
    }

    const sameAmountTable = renderComparisonTable(
      assessment.nutrient_comparison,
      assessment.recipe ? "Whole-recipe totals at the same ingredient weight (per serving)" : "Food nutrient values (per 100 g)",
    );
    if (sameAmountTable) contents.append(sameAmountTable);
    const proteinEquivalentTable = renderComparisonTable(
      assessment.protein_equivalent_comparison,
      "Whole-recipe totals at the portion that matches protein (per serving)",
    );
    if (proteinEquivalentTable) contents.append(proteinEquivalentTable);
    if (assessment.unavailable_nutrients?.length) {
      const missing = document.createElement("p");
      missing.className = "data-note";
      missing.textContent = `No comparison shown for: ${assessment.unavailable_nutrients.map(name => name.replaceAll("_", " ")).join(", ")}. Missing source values are not treated as zero.`;
      contents.append(missing);
    }
    if (assessment.limit_check?.reasons?.length || assessment.limit_check?.adjustment_issue) {
      const note = document.createElement("p");
      note.className = "data-note error-note";
      const details = [...(assessment.limit_check.reasons || [])];
      if (assessment.limit_check.adjustment_issue) details.push(assessment.limit_check.adjustment_issue);
      note.textContent = `Limit check: ${details.join("; ")}`;
      contents.append(note);
    }
    if (assessment.candidate_recipes?.length > 1) {
      const note = document.createElement("p");
      note.className = "data-note";
      note.textContent = `Recipes containing the original ingredient: ${assessment.candidate_recipes.map(recipe => recipe.name).join("; ")}.`;
      contents.append(note);
    }
    if (assessment.recipe?.description) {
      const note = document.createElement("p");
      note.className = "data-note";
      note.textContent = assessment.recipe.description;
      contents.append(note);
    }
    if (assessment.recipe?.source?.url) {
      const source = document.createElement("a");
      source.className = "source-link";
      source.href = assessment.recipe.source.url;
      source.target = "_blank";
      source.rel = "noreferrer";
      source.textContent = `${assessment.recipe.source.name || "Recipe source"}${assessment.recipe.source.basis ? ` · ${assessment.recipe.source.basis}` : ""}`;
      contents.append(source);
    }
    card.append(head, contents);
    fragment.append(card);
  }
  if (data.meal_plan) {
    const plan = data.meal_plan;
    const heading = document.createElement("h3");
    heading.className = "plan-heading";
    heading.textContent = `Catalog meal plan · ${plan.meals?.length || 0} meals · ${plan.servings} serving(s) · ${formatStatus(plan.status)}`;
    fragment.append(heading);
    for (const meal of plan.meals || []) {
      const card = document.createElement("section");
      card.className = "result-card plan-meal";
      const head = document.createElement("div");
      head.className = "result-head";
      const title = document.createElement("strong");
      title.textContent = `${meal.slot}: ${meal.name}`;
      const pill = document.createElement("span");
      pill.className = `status-pill ${meal.status === "FAIL" ? "status-fail" : meal.status === "PASS" ? "" : "status-unknown"}`;
      pill.textContent = formatStatus(meal.status);
      head.append(title, pill);
      const contents = document.createElement("div");
      contents.className = "result-body";
      const basis = document.createElement("p");
      basis.className = "quiet-note";
      basis.textContent = meal.status === "UNVERIFIED"
        ? "Nutrient totals are withheld because this recipe has no validated ingredient weights."
        : `Ingredient weights and nutrient values are per serving${plan.servings > 1 ? `; planned for ${plan.servings} people` : ""}.`;
      contents.append(basis);
      if (meal.raw_ingredient_items?.length || meal.raw_ingredient_names?.length || meal.method?.length) {
        appendFoodComPreview(contents, meal);
      }
      const grid = document.createElement("div");
      grid.className = "nutrient-grid";
      const units = {energy_kcal:"kcal", sodium_mg:"mg", potassium_mg:"mg", phosphorus_mg:"mg", protein_g:"g", carbohydrate_g:"g", fiber_g:"g", saturated_fat_g:"g", added_sugar_g:"g"};
      for (const [key, value] of Object.entries(meal.nutrients_per_serving || {})) {
        const item = document.createElement("div");
        item.className = "nutrient-item";
        const label = document.createElement("span");
        label.textContent = key.replaceAll("_", " ");
        const amount = document.createElement("strong");
        amount.textContent = `${Number(value).toFixed(1)} ${units[key] || ""}`;
        item.append(label, amount);
        grid.append(item);
      }
      if (Object.keys(meal.nutrients_per_serving || {}).length) {
        contents.append(grid);
      }
      if (meal.ingredients?.length) {
        const ingredients = document.createElement("details");
        ingredients.className = "result-details";
        const summary = document.createElement("summary");
        summary.textContent = "Validated ingredients and sources";
        const list = document.createElement("ul");
        for (const food of meal.ingredients) {
          const row = document.createElement("li");
          const type = food.ingredient_type ? ` · ${food.ingredient_type}` : "";
          row.textContent = `${food.name}: ${food.amount_g} g per serving${type}`;
          if (food.source?.url) {
            const link = document.createElement("a");
            link.className = "source-link";
            link.href = food.source.url;
            link.target = "_blank";
            link.rel = "noreferrer";
            link.textContent = `${food.source.name || "Source"}${food.source.fdc_id ? ` · FDC ${food.source.fdc_id}` : ""}`;
            row.append(link);
          }
          list.append(row);
        }
        ingredients.append(summary, list);
        contents.append(ingredients);
      }
      const description = document.createElement("p");
      description.className = "data-note";
      description.textContent = meal.description;
      contents.append(description);
      if (meal.reasons?.length) {
        const note = document.createElement("p");
        note.className = "data-note error-note";
        note.textContent = `Limit check: ${meal.reasons.join("; ")}`;
        contents.append(note);
      }
      card.append(head, contents);
      fragment.append(card);
    }
    const totals = document.createElement("p");
    totals.className = "data-note";
    const unitMap = {energy_kcal:"kcal", sodium_mg:"mg", potassium_mg:"mg", phosphorus_mg:"mg", protein_g:"g", carbohydrate_g:"g", fiber_g:"g", saturated_fat_g:"g", added_sugar_g:"g"};
    const totalText = Object.entries(plan.daily_totals_for_requested_servings || {})
      .map(([key, value]) => `${key.replaceAll("_", " ")}: ${Number(value).toFixed(1)} ${unitMap[key] || ""}`)
      .join(" · ");
    const totalsLabel = plan.status === "UNVERIFIED"
      ? "withheld because at least one meal has unverified ingredient quantities"
      : totalText || "not fully available";
    totals.textContent = `Catalog sum across all selected meals (${plan.servings} serving(s)): ${totalsLabel}.`;
    fragment.append(totals);
    if (plan.remaining_reasons?.length) {
      const note = document.createElement("p");
      note.className = "data-note error-note";
      note.textContent = `Remaining-today limit check: ${plan.remaining_reasons.join("; ")}`;
      fragment.append(note);
    }
    if (plan.remaining_not_evaluated?.length) {
      const note = document.createElement("p");
      note.className = "data-note";
      note.textContent = `Not evaluated: ${plan.remaining_not_evaluated.join("; ")}`;
      fragment.append(note);
    }
  }
  if (data.recipe) {
    const card = document.createElement("section");
    card.className = "result-card";
    const head = document.createElement("div");
    head.className = "result-head";
    const title = document.createElement("strong");
    title.textContent = data.recipe.name;
    const pill = document.createElement("span");
    const selectedStatus = data.selected?.status || "DATA FOUND";
    pill.className = `status-pill ${selectedStatus === "FAIL" ? "status-fail" : selectedStatus === "PASS" ? "" : "status-unknown"}`;
    pill.textContent = formatStatus(selectedStatus);
    head.append(title, pill);
    const contents = document.createElement("div");
    contents.className = "result-body";
    if (data.selected) {
      const basis = document.createElement("p");
      basis.className = "quiet-note";
      basis.textContent = `Listed ingredient weights are per serving · totals below are for ${data.servings} serving(s)`;
      contents.append(basis);
      const grid = document.createElement("div");
      grid.className = "nutrient-grid";
      const units = {energy_kcal:"kcal", sodium_mg:"mg", potassium_mg:"mg", phosphorus_mg:"mg", protein_g:"g", carbohydrate_g:"g", fiber_g:"g", saturated_fat_g:"g", added_sugar_g:"g"};
      const totals = data.selected.nutrients_for_requested_servings || {};
      for (const [key, value] of Object.entries(totals)) {
        const item = document.createElement("div");
        item.className = "nutrient-item";
        const label = document.createElement("span");
        label.textContent = key.replaceAll("_", " ");
        const amount = document.createElement("strong");
        amount.textContent = `${Number(value).toFixed(1)} ${units[key] || ""}`;
        const perServing = document.createElement("small");
        const oneServing = data.selected.nutrients_per_serving?.[key];
        perServing.textContent = oneServing == null ? "" : `${Number(oneServing).toFixed(1)} ${units[key] || ""} per serving`;
        item.append(label, amount, perServing);
        grid.append(item);
      }
      contents.append(grid);
      const ingredients = document.createElement("details");
      ingredients.className = "result-details";
      const summary = document.createElement("summary");
      summary.textContent = "Validated ingredients and sources";
      const list = document.createElement("ul");
      for (const food of data.selected.ingredients) {
        const row = document.createElement("li");
        const type = food.ingredient_type ? ` · ${food.ingredient_type}` : "";
        row.textContent = `${food.name}: ${food.amount_g} g per serving${type}`;
        if (food.source?.url) {
          const link = document.createElement("a");
          link.className = "source-link";
          link.href = food.source.url;
          link.target = "_blank";
          link.rel = "noreferrer";
          link.textContent = `${food.source.name || "Source"}${food.source.fdc_id ? ` · FDC ${food.source.fdc_id}` : ""}`;
          row.append(link);
        }
        list.append(row);
      }
      ingredients.append(summary, list);
      contents.append(ingredients);
    }
    const note = document.createElement("p");
    note.className = "data-note";
    note.textContent = data.recipe.description;
    contents.append(note);
    if (data.evaluations?.length) {
      const evaluated = document.createElement("details");
      evaluated.className = "result-details";
      const summary = document.createElement("summary");
      summary.textContent = `Candidate checks (${data.evaluations.length})`;
      const list = document.createElement("div");
      list.className = "eval-list";
      for (const candidate of data.evaluations) {
        const row = document.createElement("div");
        row.className = "eval-item";
        const items = candidate.ingredients.map(x => `${x.name} ${x.amount_g}g`).join(" · ");
        row.textContent = `${formatStatus(candidate.status)} · ${items}`;
        list.append(row);
      }
      evaluated.append(summary, list);
      contents.append(evaluated);
    }
    card.append(head, contents);
    fragment.append(card);
  }
  if (data.missing_data) {
    const missing = data.missing_data;
    if (missing.raw_ingredient_items?.length || missing.raw_ingredient_names?.length || missing.method?.length) {
      const card = document.createElement("section");
      card.className = "result-card";
      const head = document.createElement("div");
      head.className = "result-head";
      const title = document.createElement("strong");
      title.textContent = missing.recipe_name || "Unverified recipe record";
      const pill = document.createElement("span");
      pill.className = "status-pill status-unknown";
      pill.textContent = missing.source_dataset === "Food.com Recipes and Reviews (Kaggle)"
        ? "NUTRIENTS UNAVAILABLE" : "UNVERIFIED";
      head.append(title, pill);
      const contents = document.createElement("div");
      contents.className = "result-body";
      const note = document.createElement("p");
      note.className = "data-note";
      note.textContent = `Lookup status: ${missing.message}`;
      contents.append(note);
      if (missing.source_dataset === "Food.com Recipes and Reviews (Kaggle)" && data.ingredient_mapping_enrichment) {
        appendIngredientResolution(contents, data.ingredient_mapping_enrichment);
      }
      appendFoodComPreview(contents, missing);
      card.append(head, contents);
      fragment.append(card);
    } else {
      const note = document.createElement("p");
      note.className = "data-note";
      note.textContent = `Lookup status: ${missing.message}`;
      fragment.append(note);
    }
  }
  return fragment;
}

function readLimits(prefix, names) {
  const values = {};
  for (const name of names) {
    const field = document.querySelector(`#${prefix}-${name}`);
    if (field?.value !== "") values[name] = field.value;
  }
  return values;
}

function requestPayload(query, history = []) {
  const conditions = [...document.querySelectorAll('input[name="condition"]:checked')].map(input => input.value);
  return {
    query,
    conditions,
    servings: document.querySelector("#servings").value,
    meal_limits: readLimits("meal", ["sodium", "potassium", "phosphorus", "carbohydrate"]),
    remaining_limits: readLimits("remaining", ["sodium", "potassium", "phosphorus"]),
    use_slm: document.querySelector("#use-slm").checked,
    conversation_history: history,
  };
}

form.addEventListener("submit", async event => {
  event.preventDefault();
  const query = queryBox.value.trim();
  if (!query) return;
  const previousHistory = conversationHistory.slice(-12);
  rememberTurn({role: "user", content: query});
  addMessage("user", query);
  queryBox.value = "";
  sendButton.disabled = true;
  const typing = document.createElement("div");
  typing.className = "typing";
  typing.textContent = "Checking RenAlly's local catalog…";
  chatLog.append(typing);
  chatLog.scrollTop = chatLog.scrollHeight;
  try {
    const response = await fetch("/api/chat", {
      method: "POST",
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify(requestPayload(query, previousHistory)),
    });
    const data = await response.json();
    typing.remove();
    if (!response.ok) throw new Error(data.error || "The local server returned an error.");
    addMessage("assistant", data.answer || "I couldn't produce a verified answer from the available data.", data);
    rememberTurn({
      role: "assistant",
      content: data.answer || "",
      context: summarizeResponseContext(data),
    });
  } catch (error) {
    typing.remove();
    const message = error.message || "Couldn't reach the local RenAlly server. Start it with python web_app.py.";
    addMessage("assistant", message);
    rememberTurn({role: "assistant", content: message});
  } finally {
    sendButton.disabled = false;
    queryBox.focus();
  }
});

queryBox.addEventListener("keydown", event => {
  if (event.key === "Enter" && !event.shiftKey) {
    event.preventDefault();
    form.requestSubmit();
  }
});

document.querySelectorAll(".suggestion").forEach(button => {
  button.addEventListener("click", () => {
    queryBox.value = button.dataset.query;
    queryBox.focus();
  });
});

document.querySelector("#clear-chat").addEventListener("click", () => {
  chatLog.replaceChildren();
  conversationHistory.length = 0;
  const message = document.createElement("article");
  message.className = "message assistant";
  const avatar = document.createElement("div");
  avatar.className = "avatar";
  avatar.textContent = "R";
  const body = document.createElement("div");
  body.className = "message-body";
  const meta = document.createElement("div");
  meta.className = "message-meta";
  meta.textContent = "RENALLY · PROTOTYPE";
  const bubble = document.createElement("div");
  bubble.className = "bubble";
  const text = document.createElement("p");
  text.textContent = "New conversation. Ask about a dish in the local catalog.";
  bubble.append(text);
  body.append(meta, bubble);
  message.append(avatar, body);
  chatLog.append(message);
});

document.querySelector("#servings-minus").addEventListener("click", () => {
  const input = document.querySelector("#servings");
  input.value = Math.max(1, Number(input.value || 1) - 1);
});
document.querySelector("#servings-plus").addEventListener("click", () => {
  const input = document.querySelector("#servings");
  input.value = Math.min(20, Number(input.value || 1) + 1);
});
