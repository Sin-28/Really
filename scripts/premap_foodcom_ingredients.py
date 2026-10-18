"""Bounded offline pre-mapping; defaults to read-only dry-run mode."""

import argparse
import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from src.recipes.ingredient_mapping_validator import SLMIngredientMappingValidator  # noqa: E402
from src.recipes.ingredient_premapping import IngredientPremappingWorkflow  # noqa: E402
from src.runtime import create_ingredient_mapping_runtime  # noqa: E402


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--write", action="store_true",
                        help="enable persistent mapping/cache writes; omitted runs read-only")
    parser.add_argument("--use-slm", action="store_true",
                        help="evaluate candidates with the configured local Qwen model")
    parser.add_argument("--batch-size", type=int, default=5,
                        help="maximum unique ingredients processed per invocation")
    parser.add_argument("--max-ingredients", type=int, default=25,
                        help="maximum unique context-aware ingredients in this job")
    parser.add_argument("--max-recipes", type=int, default=1000,
                        help="maximum Food.com recipes scanned for this sample")
    parser.add_argument("--recipe-batch-size", type=int, default=250)
    parser.add_argument("--dataset-scope", choices=("all", "foundation_foods", "sr_legacy"), default="all")
    parser.add_argument("--checkpoint", type=Path,
                        help="optional local JSON checkpoint path for resume")
    args = parser.parse_args(argv)

    runtime = create_ingredient_mapping_runtime(PROJECT_ROOT / "data")
    try:
        validator = SLMIngredientMappingValidator(runtime.kg) if args.use_slm else None
        workflow = IngredientPremappingWorkflow(
            runtime.kg, runtime.mapper, validator=validator,
        )

        def progress(event):
            print(json.dumps(event, ensure_ascii=False), file=sys.stderr, flush=True)

        report = workflow.run(
            dataset_scope=args.dataset_scope,
            batch_size=args.batch_size,
            max_ingredients=args.max_ingredients,
            max_recipes=args.max_recipes,
            recipe_batch_size=args.recipe_batch_size,
            dry_run=not args.write,
            write_enabled=args.write,
            checkpoint_path=args.checkpoint,
            progress_callback=progress,
        )
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 0
    finally:
        runtime.close()


if __name__ == "__main__":
    raise SystemExit(main())
