# Recipe data sources

## Food.com - Recipes and Reviews (Kaggle)

- Source: [Food.com - Recipes and Reviews](https://www.kaggle.com/datasets/irkaal/foodcom-recipes-and-reviews)
- The dataset listing reports 522,517 recipes. Download `recipes.csv` from Kaggle; the reviews files are not needed by RenAlly.
- The listing recommends Parquet to preserve list-column schemas. The importer uses the CSV to avoid an extra runtime dependency and safely decodes its R-style `c("...")` list fields.
- Kaggle UI download: open the source above, choose `recipes.csv` from the files, download `recipes.zip`, and extract `recipes.csv` outside the repository. The importer also accepts a gzip-compressed `recipes.csv.gz`. Then run from the project root after configuring Neo4j in `.env`:

  ```sh
  python scripts/import_foodcom_recipes.py "/path/to/recipes.csv"
  ```

- Food.com is the only recipe catalog. The importer retains only recipes with at least one ingredient and a nonblank source quantity aligned with every ingredient. A complete import replaces existing `FoodComRecipe` records only after every source row has been processed; incomplete-quantity records and existing records missing from the expanded dataset are then removed. USDA imports remove legacy illustrative RenAlly recipe nodes and preserve Food.com records. `--limit N` imports a preview without deleting existing Food.com records. `--resume` skips complete records already imported from this dataset version. The quantity-filter update uses import version v4, so a full re-import removes older incomplete records.
- `RecipeIngredientParts`, `RecipeIngredientQuantities`, `RecipeServings`, and `RecipeInstructions` are retained in recipe details for included recipes. Quantity strings remain verbatim and are also attached to ingredient-text relationships by source position. A present quantity is not necessarily a weight or convertible amount; parsed weight conversions are stored separately from the raw source fields.
- Food.com ingredient mentions remain `LISTS_INGREDIENT_TEXT` relationships to `FoodComIngredient` nodes. They are not linked as validated `CONTAINS` relationships to USDA foods. `details_json.ingredient_items[].parsed` adds original text, name, quantity, unit, normalized name, gram amount when safely convertible, and status. For ranges, it stores quantity and gram endpoints separately. Per-item weights have no total gram amount without an item count. Volume and count conversions require an exact ingredient-specific reference factor with a source; none are assumed during import. Source fields remain unchanged, and existing recipe records without `parsed` remain readable. Nutrient totals and constraint checks remain unavailable until identities and gram amounts are independently validated.
- The Kaggle listing identifies the dataset license as CC0: Public Domain. The raw CSV is not copied into this repository; keep it in a local data/download folder.
