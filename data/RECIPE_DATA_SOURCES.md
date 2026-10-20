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
- `RecipeIngredientParts`, `RecipeIngredientQuantities`, `RecipeServings`, and `RecipeInstructions` are retained in recipe details for included recipes. Quantity strings remain verbatim and are also attached to ingredient-text relationships by source position. Food.com quantities are source metadata and are never converted to weights.
- Food.com ingredient mentions remain `LISTS_INGREDIENT_TEXT` relationships to `FoodComIngredient` nodes. They are not linked as validated `CONTAINS` relationships to USDA foods. `details_json.ingredient_items[].parsed` retains the original text and normalized ingredient name for identity matching; its quantity and gram fields stay empty. Existing recipe records with older parsed gram fields remain readable, but the nutrient calculator ignores those fields for Food.com recipes. Nutrient totals and constraint checks require independently supplied ingredient weights.
- The Kaggle listing identifies the dataset license as CC0: Public Domain. The raw CSV is not copied into this repository; keep it in a local data/download folder.
