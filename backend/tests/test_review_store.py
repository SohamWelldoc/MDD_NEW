import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from services.review import approval_workflow
from routes import reviews as review_routes
from services.review.change_planner import build_change_plan
from services.review.diff_builder import build_diff
from services.review.review_db import db_path, find_active_job, list_audit, load_job, save_job
from services.review.entity_extractor import extract_section_entities
from services.review.section_detector import detect_feedback_section
from services.review.review_store import (
    add_feedback,
    create_review,
    create_version_files,
    find_feedback,
    load_review,
    mark_mdd_reviews_stale,
)
from services.review.feedback_classifier import classify_feedback
from services.review.validation import sanitize_and_validate_revision
from services.review.versioning import compare_versions, finalize_review, restore_version


def _source(markdown="# Title\n\nInitial body."):
    return {
        "markdown": markdown,
        "source_path": "HLD_20260629123721.json",
        "docx_path": None,
        "metadata": {"job_id": "job-1", "timestamp": "20260629123721"},
    }


class ReviewStoreTests(unittest.TestCase):
    def test_create_review_and_feedback_persists(self):
        with tempfile.TemporaryDirectory() as tmp:
            with patch.dict(os.environ, {"ARTIFACT_BASE_DIR": tmp}):
                review = create_review(
                    document_type="hld",
                    product="als",
                    release="7.1",
                    module_slug=None,
                    created_by="reviewer",
                    source=_source(),
                )
                feedback = add_feedback(
                    review,
                    feedback="Change the logical view wording.",
                    target_section="Title",
                    change_type="correction",
                    priority="high",
                    target_kind="section",
                    reviewer_expectation="The wording is corrected.",
                    base_version="v1",
                    reviewer="reviewer",
                )

                reloaded = load_review(review["review_id"], product="als", release="7.1")
                self.assertEqual(reloaded["current_version"], "v1")
                self.assertEqual(reloaded["feedback_items"][0]["feedback_id"], feedback["feedback_id"])
                self.assertEqual(reloaded["feedback_items"][0]["status"], "open")
                self.assertEqual(reloaded["feedback_items"][0]["change_type"], "correction")
                self.assertEqual(reloaded["feedback_items"][0]["priority"], "high")
                self.assertEqual(reloaded["versions"][0]["artifact_name"], "HLD_20260629123721")
                self.assertTrue(Path(reloaded["versions"][0]["markdown_path"]).is_file())

    def test_feedback_conflict_when_base_version_is_old(self):
        with tempfile.TemporaryDirectory() as tmp:
            with patch.dict(os.environ, {"ARTIFACT_BASE_DIR": tmp}):
                review = create_review(
                    document_type="hld",
                    product="als",
                    release="7.1",
                    module_slug=None,
                    created_by="reviewer",
                    source=_source(),
                )
                review["current_version"] = "v2"
                feedback = add_feedback(
                    review,
                    feedback="Apply this to the old version.",
                    target_section=None,
                    base_version="v1",
                    reviewer="reviewer",
                )

                self.assertEqual(feedback["status"], "conflict")

    def test_approve_draft_creates_new_version_and_applies_feedback(self):
        with tempfile.TemporaryDirectory() as tmp:
            with patch.dict(os.environ, {"ARTIFACT_BASE_DIR": tmp}):
                def fake_export(markdown, output_path, document_type, title=None):
                    Path(output_path).write_text("docx placeholder", encoding="utf-8")
                    return output_path

                with patch.object(approval_workflow, "export_revision_docx", fake_export):
                    review = create_review(
                        document_type="hld",
                        product="als",
                        release="7.1",
                        module_slug=None,
                        created_by="reviewer",
                        source=_source(),
                    )
                    feedback = add_feedback(
                        review,
                        feedback="Update the title.",
                        target_section="Title",
                        base_version="v1",
                        reviewer="reviewer",
                    )
                    draft = create_version_files(
                        review_dir=Path(review["review_dir"]),
                        document_type="hld",
                        module_slug=None,
                        version="v2_draft",
                        status="draft",
                        markdown="# Updated Title\n\nInitial body.",
                        metadata={"base_version": "v1", "feedback_id": feedback["feedback_id"]},
                    )
                    review["versions"].append(draft)

                    result = approval_workflow.approve_draft(
                        review,
                        draft_version="v2_draft",
                        feedback_id=feedback["feedback_id"],
                        decided_by="approver",
                    )

                    self.assertEqual(result["version"], "v2")
                    self.assertEqual(result["review"]["current_version"], "v2")
                    accepted = next(item for item in result["review"]["versions"] if item["version"] == "v2")
                    self.assertRegex(accepted["artifact_name"], r"^HLD_\d{14}$")
                    self.assertEqual(find_feedback(result["review"], feedback["feedback_id"])["status"], "applied")

    def test_hld_change_marks_mdd_reviews_stale(self):
        with tempfile.TemporaryDirectory() as tmp:
            with patch.dict(os.environ, {"ARTIFACT_BASE_DIR": tmp}):
                create_review(
                    document_type="mdd",
                    product="als",
                    release="7.1",
                    module_slug="Food_Module",
                    created_by="reviewer",
                    source=_source("# Food Module\n\nInitial body."),
                )

                marked = mark_mdd_reviews_stale("als", "7.1", "v2", "approver")
                stale = load_review(marked[0], product="als", release="7.1")

                self.assertTrue(marked)
                self.assertTrue(stale["stale"])
                self.assertEqual(stale["stale_reason"], "HLD changed to v2")

    def test_vague_diagram_feedback_is_classified_as_diagram_change(self):
        result = classify_feedback(
            feedback="Make the diagram more better",
            target_section="2.3 Feature Lifecycle",
            document_type="hld",
        )

        self.assertIn("diagram_change", result["tags"])
        self.assertEqual(result["classification"], "diagram_change")

    def test_general_feedback_is_not_forced_to_diagram_change(self):
        result = classify_feedback(
            feedback="Add missing authorization details and mark unknown items as to be confirmed",
            target_section="3. Security Approach",
            document_type="hld",
        )

        self.assertIn("security_content", result["tags"])
        self.assertIn("content_addition", result["tags"])
        self.assertNotIn("diagram_change", result["tags"])

    def test_exact_intent_preserve_existing_and_add_diagram(self):
        result = classify_feedback(
            feedback="Keep the old diagram and add another diagram for failure handling.",
            target_section="Deployment Diagram",
            document_type="hld",
            change_type="diagram",
            target_kind="diagram",
        )

        self.assertEqual(result["exact_intent"], "preserve_existing_and_add_diagram")
        self.assertIn("Keep every existing Mermaid block unchanged.", result["intent_constraints"])

    def test_exact_intent_add_diagram_for_these_steps(self):
        result = classify_feedback(
            feedback="Add a diagram for these steps.",
            target_section=None,
            document_type="hld",
            change_type="diagram",
            target_kind="diagram",
        )

        self.assertEqual(result["exact_intent"], "add_new_diagram")

    def test_change_plan_and_diff_include_review_metadata(self):
        with tempfile.TemporaryDirectory() as tmp:
            with patch.dict(os.environ, {"ARTIFACT_BASE_DIR": tmp}):
                review = create_review(
                    document_type="hld",
                    product="als",
                    release="7.1",
                    module_slug=None,
                    created_by="reviewer",
                    source=_source(),
                )
                feedback = add_feedback(
                    review,
                    feedback="Add missing authorization details.",
                    target_section="3. Security Approach",
                    change_type="addition",
                    priority="high",
                    target_kind="section",
                    reviewer_expectation="Authorization gaps are called out.",
                    base_version="v1",
                    reviewer="reviewer",
                )
                plan = build_change_plan(review, feedback["feedback_id"], "reviewer")
                diff = build_diff("# A\nold\n```mermaid\nflowchart LR\nA-->B\n```", "# A\nnew\n```mermaid\nflowchart LR\nA-->B\nB-->C\n```", target_section="A")

                self.assertEqual(plan["target_section"], "3. Security Approach")
                self.assertEqual(plan["classification"]["priority"], "high")
                self.assertIn("change_summary", diff)
                self.assertTrue(diff["mermaid_changed"])

    def test_revision_job_records_status_transitions(self):
        with tempfile.TemporaryDirectory() as tmp:
            with patch.dict(os.environ, {"ARTIFACT_BASE_DIR": tmp}):
                review = create_review(
                    document_type="hld",
                    product="als",
                    release="7.1",
                    module_slug=None,
                    created_by="reviewer",
                    source=_source(),
                )
                feedback = add_feedback(
                    review,
                    feedback="Update the overview.",
                    target_section="Title",
                    base_version="v1",
                    reviewer="reviewer",
                )
                job_id = "job-1"
                review_routes.revision_jobs[job_id] = {
                    "review_id": review["review_id"],
                    "job_id": job_id,
                    "status": "pending",
                    "progress": 0,
                    "message": "queued",
                    "started_at": "now",
                    "completed_at": None,
                }

                def fake_revision(review_arg, feedback_id, requested_by, progress_callback=None):
                    progress_callback(50, "halfway")
                    return {
                        "review": review_arg,
                        "feedback": feedback,
                        "draft_version": "v2_draft",
                        "classification": {},
                        "change_plan": {},
                        "evidence_summary": {},
                        "validation_report": {},
                        "diff": {},
                    }

                with patch.object(review_routes, "create_draft_revision", fake_revision):
                    request = type("Request", (), {"feedback_id": feedback["feedback_id"], "requested_by": "reviewer"})()
                    review_routes._run_revision_job(job_id, review["review_id"], request, "als", "7.1")

                self.assertEqual(review_routes.revision_jobs[job_id]["status"], "completed")
                self.assertEqual(review_routes.revision_jobs[job_id]["progress"], 100)

    def test_sqlite_metadata_store_is_created_and_mirrors_review(self):
        with tempfile.TemporaryDirectory() as tmp:
            with patch.dict(os.environ, {"ARTIFACT_BASE_DIR": tmp}):
                review = create_review(
                    document_type="hld",
                    product="als",
                    release="7.1",
                    module_slug=None,
                    created_by="reviewer",
                    source=_source(),
                )
                reloaded = load_review(review["review_id"], product="als", release="7.1")

                self.assertTrue(db_path("als", "7.1").is_file())
                self.assertEqual(reloaded["review_id"], review["review_id"])

    def test_persisted_revision_jobs_support_duplicate_detection(self):
        with tempfile.TemporaryDirectory() as tmp:
            with patch.dict(os.environ, {"ARTIFACT_BASE_DIR": tmp}):
                review = create_review(
                    document_type="hld",
                    product="als",
                    release="7.1",
                    module_slug=None,
                    created_by="reviewer",
                    source=_source(),
                )
                job = {
                    "review_id": review["review_id"],
                    "job_id": "job-sqlite",
                    "feedback_id": "fb-1",
                    "status": "processing",
                    "progress": 25,
                    "message": "Working",
                    "started_at": "now",
                    "completed_at": None,
                }
                save_job("als", "7.1", job)

                self.assertEqual(load_job("als", "7.1", "job-sqlite")["progress"], 25)
                self.assertEqual(find_active_job("als", "7.1", review["review_id"], "fb-1")["job_id"], "job-sqlite")

    def test_compare_restore_finalize_and_audit_flow(self):
        with tempfile.TemporaryDirectory() as tmp:
            with patch.dict(os.environ, {"ARTIFACT_BASE_DIR": tmp}):
                review = create_review(
                    document_type="hld",
                    product="als",
                    release="7.1",
                    module_slug=None,
                    created_by="reviewer",
                    source=_source("# Title\n\nInitial body."),
                )
                v2 = create_version_files(
                    review_dir=Path(review["review_dir"]),
                    document_type="hld",
                    module_slug=None,
                    version="v2",
                    status="approved_revision",
                    markdown="# Title\n\nUpdated body.",
                )
                review["versions"].append(v2)
                review["current_version"] = "v2"

                diff = compare_versions(review, "v1", "v2")
                restored = restore_version(review, version="v1", restored_by="reviewer", reason="rollback")
                finalized = finalize_review(restored["review"], version=restored["version"], finalized_by="architect", role="architect", comment="ready")

                self.assertIn("change_summary", diff)
                self.assertEqual(finalized["review"]["status"], "finalized")
                self.assertTrue(any(event.get("event") == "review_finalized" for event in finalized["review"]["audit"]))
                self.assertTrue(list_audit("als", "7.1", review["review_id"]))

    def test_validation_reports_blocking_and_quality_score(self):
        with tempfile.TemporaryDirectory() as tmp:
            with patch.dict(os.environ, {"ARTIFACT_BASE_DIR": tmp}):
                clean, report = sanitize_and_validate_revision(
                    markdown="# Title\n\nNo diagram change.\n```mermaid\nflowchart LR\nA-->B\n```",
                    document_type="mdd",
                    product="als",
                    release="7.1",
                    old_markdown="# Title\n\n```mermaid\nflowchart LR\nA-->B\n```",
                    classification={"tags": ["diagram_change"]},
                )

                self.assertIn("# Title", clean)
                self.assertTrue(report["blocking"])
                self.assertLess(report["quality_score"], 100)

    def test_validation_blocks_when_add_diagram_intent_not_satisfied(self):
        _clean, report = sanitize_and_validate_revision(
            markdown="# Flow\n\n```mermaid\nflowchart LR\nA-->B\n```",
            document_type="mdd",
            product="als",
            release="7.1",
            old_markdown="# Flow\n\n```mermaid\nflowchart LR\nA-->B\n```",
            classification={
                "tags": ["diagram_change"],
                "exact_intent": "preserve_existing_and_add_diagram",
            },
        )

        issue_types = {issue["type"] for issue in report["blocking"]}
        self.assertIn("diagram_addition_not_performed", issue_types)

    def test_validation_allows_preserved_old_diagram_plus_new_one(self):
        _clean, report = sanitize_and_validate_revision(
            markdown=(
                "# Flow\n\n"
                "```mermaid\nflowchart LR\nA-->B\n```\n\n"
                "```mermaid\nflowchart LR\nB-->C\n```\n"
            ),
            document_type="mdd",
            product="als",
            release="7.1",
            old_markdown="# Flow\n\n```mermaid\nflowchart LR\nA-->B\n```",
            classification={
                "tags": ["diagram_change"],
                "exact_intent": "preserve_existing_and_add_diagram",
            },
        )

        issue_types = {issue["type"] for issue in report["blocking"]}
        self.assertNotIn("diagram_addition_not_performed", issue_types)
        self.assertNotIn("existing_diagram_not_preserved", issue_types)

    def test_validation_requires_entities_in_added_diagram(self):
        _clean, report = sanitize_and_validate_revision(
            markdown=(
                "# Interactions and Flows\n\n"
                "The Food Module sends a request to the CGM Connection Service.\n\n"
                "```mermaid\nsequenceDiagram\n"
                "participant FoodModule as Food Module\n"
                "participant CGMService as CGM Connection Service\n"
                "FoodModule->>CGMService: Request glucose context\n"
                "```\n"
            ),
            document_type="mdd",
            product="als",
            release="7.1",
            old_markdown="# Interactions and Flows\n\nThe Food Module sends a request to the CGM Connection Service.",
            classification={
                "tags": ["diagram_change"],
                "exact_intent": "add_new_diagram",
                "extracted_entities": ["Food Module", "CGM Connection Service"],
            },
        )

        issue_types = {issue["type"] for issue in report["blocking"]}
        self.assertNotIn("diagram_missing_section_entities", issue_types)

    def test_change_plan_tracks_evidence_traceability_and_claims(self):
        with tempfile.TemporaryDirectory() as tmp:
            with patch.dict(os.environ, {"ARTIFACT_BASE_DIR": tmp}):
                review = create_review(
                    document_type="hld",
                    product="als",
                    release="7.1",
                    module_slug=None,
                    created_by="reviewer",
                    source=_source(),
                )
                feedback = add_feedback(
                    review,
                    feedback="Add Redis cache and JWT security details.",
                    target_section="Security",
                    change_type="addition",
                    base_version="v1",
                    reviewer="reviewer",
                )
                plan = build_change_plan(review, feedback["feedback_id"], "reviewer")

                self.assertEqual(plan["traceability"]["feedback_id"], feedback["feedback_id"])
                self.assertTrue(plan["unsupported_claims"])

    def test_section_detector_maps_feedback_to_best_heading(self):
        markdown = (
            "# HLD\n\n"
            "## Logical View\n\nService responsibilities.\n\n"
            "## Security Approach\n\nAuthentication and authorization details.\n\n"
            "## Deployment Diagram\n\n```mermaid\nflowchart LR\nA-->B\n```\n"
        )

        security = detect_feedback_section(
            markdown=markdown,
            feedback="Add missing authorization and token handling details.",
            change_type="addition",
            target_kind="section",
        )
        diagram = detect_feedback_section(
            markdown=markdown,
            feedback="Improve the mermaid diagram.",
            change_type="diagram",
            target_kind="diagram",
        )

        self.assertEqual(security["target_section"], "Security Approach")
        self.assertEqual(diagram["target_section"], "Deployment Diagram")

    def test_vague_steps_diagram_feedback_maps_to_interactions_flow(self):
        markdown = (
            "# HLD\n\n"
            "## Overview\n\nGeneral design.\n\n"
            "## Interactions and Flows\n\n"
            "The Food Glucose Prediction Flow is triggered when a user logs a meal. "
            "1. The Food Module sends a request to the CGM Connection Service. "
            "2. The CGM Connection Service retrieves glucose data from the Libre CGM device. "
            "3. The Food Module displays the predicted label.\n\n"
            "## Security Approach\n\nAuthentication details.\n"
        )

        result = detect_feedback_section(
            markdown=markdown,
            feedback="Add a diagram for these steps.",
            change_type="diagram",
            target_kind="diagram",
        )

        self.assertEqual(result["target_section"], "Interactions and Flows")
        self.assertGreaterEqual(result["confidence"], 0.55)

    def test_entity_extraction_finds_flow_actors(self):
        section = (
            "The Food Glucose Prediction Flow is triggered when a user logs a meal. "
            "The Food Module sends a request to the CGM Connection Service. "
            "The CGM Connection Service retrieves glucose data from the Libre CGM device."
        )

        entities = extract_section_entities(section)

        self.assertIn("Food Module", entities)
        self.assertIn("CGM Connection Service", entities)
        self.assertIn("Libre CGM", entities)

    def test_feedback_without_target_section_is_auto_mapped(self):
        with tempfile.TemporaryDirectory() as tmp:
            with patch.dict(os.environ, {"ARTIFACT_BASE_DIR": tmp}):
                review = create_review(
                    document_type="hld",
                    product="als",
                    release="7.1",
                    module_slug=None,
                    created_by="reviewer",
                    source=_source(
                        "# HLD\n\n"
                        "## Logical View\n\nService responsibilities.\n\n"
                        "## Security Approach\n\nAuthentication and authorization details.\n"
                    ),
                )
                feedback = add_feedback(
                    review,
                    feedback="Add token handling to authorization details.",
                    target_section=None,
                    change_type="addition",
                    priority="medium",
                    target_kind="section",
                    reviewer="reviewer",
                )

                self.assertEqual(feedback["target_section"], "Security Approach")
                self.assertGreater(feedback["section_detection"]["confidence"], 0)

    def test_change_plan_includes_accuracy_preview_metadata(self):
        with tempfile.TemporaryDirectory() as tmp:
            with patch.dict(os.environ, {"ARTIFACT_BASE_DIR": tmp}):
                review = create_review(
                    document_type="hld",
                    product="als",
                    release="7.1",
                    module_slug=None,
                    created_by="reviewer",
                    source=_source(
                        "# HLD\n\n"
                        "## Interactions and Flows\n\n"
                        "The Food Module sends a request to the CGM Connection Service. "
                        "The CGM Connection Service retrieves glucose data from the Libre CGM device.\n"
                    ),
                )
                feedback = add_feedback(
                    review,
                    feedback="Add a diagram for these steps.",
                    target_section=None,
                    change_type="diagram",
                    priority="medium",
                    target_kind="diagram",
                    reviewer="reviewer",
                )
                plan = build_change_plan(review, feedback["feedback_id"], "reviewer")

                self.assertEqual(plan["exact_intent"], "add_new_diagram")
                self.assertIn("Food Module", plan["extracted_entities"])
                self.assertEqual(plan["target_section"], "Interactions and Flows")
                self.assertIn("planned_action", plan)

    def test_mermaid_diff_describes_content_changes_when_count_same(self):
        diff = build_diff(
            "# Flow\n```mermaid\nflowchart LR\nA-->B\n```",
            "# Flow\n```mermaid\nflowchart LR\nA-->B\nB-->C\n```",
            target_section="Flow",
        )

        self.assertTrue(diff["mermaid_changed"])
        self.assertIn("mermaid_change_details", diff)
        self.assertIn("B-->C", diff["mermaid_change_details"][0]["added_lines"])
        self.assertTrue(any("modified" in item for item in diff["change_summary"]))


if __name__ == "__main__":
    unittest.main()
