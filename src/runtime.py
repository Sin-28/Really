"""Application composition for the local RenAlly prototype.

Keep dependency construction in one place so the CLI, web interface, and
evaluation command all use the same graph and retrieval/constraint pipeline.
"""
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

from src.constraints.constraint_engine import ConstraintEngine
from src.graph.neo4j_graph import Neo4jKnowledgeGraph
from src.pipeline.renally_pipeline import RenAllyPipeline
from src.recipes.candidate_generator import CandidateGenerator
from src.recipes.substitution_analyzer import SubstitutionAnalyzer
from src.retrieval.graph_retriever import GraphRetriever
from src.recipes.ingredient_mapping import MAPPING_VERSION, USDAIngredientMapper
from src.recipes.ingredient_reference import PreferredUSDAReferenceManager
from src.recipes.ingredient_identity_resolution import (
    IngredientIdentityResolver,
    IngredientReferenceReviewQueue,
)
from src.recipes.hybrid_ingredient_mapping import HybridIngredientMappingService
from src.recipes.recipe_nutrient_enrichment import RecipeNutrientEnrichmentService
from src.recipes.ingredient_quantity_optimiser import IngredientQuantityOptimiser
from src.recipes.culinary_quantity_validation import CulinaryQuantityValidationService
from src.recipes.daily_meal_planner import DailyMealPlanner


@dataclass
class RenAllyRuntime:
    """Shared application components built from one local data directory."""

    kg: Neo4jKnowledgeGraph
    retriever: GraphRetriever
    engine: ConstraintEngine
    candidate_generator: CandidateGenerator
    substitution_analyzer: SubstitutionAnalyzer

    def create_pipeline(self, generator: Optional[Any] = None) -> RenAllyPipeline:
        """Create a request pipeline using the shared deterministic services."""
        return RenAllyPipeline(
            self.kg,
            self.retriever,
            self.engine,
            self.candidate_generator,
            generator,
        )

    def create_recipe_nutrient_enrichment(self, *, mapping_service=None,
                                          mapping_mode="READ_ONLY",
                                          mapping_persist=False,
                                          mapping_use_slm=False):
        """Create the optional Step 5B-1 enrichment over shared graph services."""
        if mapping_service is None:
            mapping_service = HybridIngredientMappingService(
                self.kg, self.retriever,
                mode=mapping_mode,
                persist_mappings=mapping_persist,
                use_slm=mapping_use_slm,
            )
        return RecipeNutrientEnrichmentService(self.kg, mapping_service)

    def create_ingredient_quantity_optimiser(self):
        return IngredientQuantityOptimiser(self.kg)

    def create_culinary_quantity_validator(self, *, model_provider=None):
        return CulinaryQuantityValidationService(self.kg, model_provider=model_provider)

    def create_daily_meal_planner(self):
        return DailyMealPlanner(
            self.kg, self.retriever, self.create_recipe_nutrient_enrichment(),
            self.create_ingredient_quantity_optimiser(),
            self.create_culinary_quantity_validator(),
        )

    def close(self):
        self.kg.close()


@dataclass
class IngredientMappingRuntime:
    """Shared graph and retrieval dependencies for offline ingredient mapping."""

    kg: Neo4jKnowledgeGraph
    retriever: GraphRetriever
    mapper: USDAIngredientMapper

    def close(self):
        self.kg.close()


@dataclass
class IngredientIdentityRuntime:
    """Opt-in identity resolution and review tools; not used by adaptation."""

    kg: Neo4jKnowledgeGraph
    retriever: GraphRetriever
    mapper: USDAIngredientMapper
    references: PreferredUSDAReferenceManager
    resolver: IngredientIdentityResolver
    review_queue: IngredientReferenceReviewQueue

    def close(self):
        self.kg.close()


def create_runtime(data_dir: Path) -> RenAllyRuntime:
    """Build the common Neo4j graph, retrieval, and constraint services."""
    kg = Neo4jKnowledgeGraph(data_dir)
    retriever = GraphRetriever(kg)
    engine = ConstraintEngine(kg.ingredients)
    return RenAllyRuntime(
        kg=kg,
        retriever=retriever,
        engine=engine,
        candidate_generator=CandidateGenerator(kg),
        substitution_analyzer=SubstitutionAnalyzer(kg, retriever, engine),
    )


def create_ingredient_mapping_runtime(data_dir: Path, *, mapping_version=MAPPING_VERSION) -> IngredientMappingRuntime:
    """Build the existing Neo4j-backed mapping dependencies in one place."""
    kg = Neo4jKnowledgeGraph(data_dir)
    retriever = GraphRetriever(kg)
    return IngredientMappingRuntime(
        kg=kg,
        retriever=retriever,
        mapper=USDAIngredientMapper(kg, retriever, mapping_version=mapping_version),
    )


def create_ingredient_identity_runtime(data_dir: Path, *, mapping_version=MAPPING_VERSION,
                                       allow_reference_defaults=True,
                                       auto_verify_exact=False) -> IngredientIdentityRuntime:
    """Build Step 4.7 resolution and review services without wiring adaptation."""
    mapping_runtime = create_ingredient_mapping_runtime(data_dir, mapping_version=mapping_version)
    references = PreferredUSDAReferenceManager(mapping_runtime.kg)
    return IngredientIdentityRuntime(
        kg=mapping_runtime.kg,
        retriever=mapping_runtime.retriever,
        mapper=mapping_runtime.mapper,
        references=references,
        resolver=IngredientIdentityResolver(
            mapping_runtime.mapper, references,
            allow_reference_defaults=allow_reference_defaults,
            auto_verify_exact=auto_verify_exact,
        ),
        review_queue=IngredientReferenceReviewQueue(references),
    )
