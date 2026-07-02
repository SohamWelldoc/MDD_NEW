import unittest

from services.hld.hld_generator import (
    _find_logical_view_diagram_insert_pos,
    _insert_at_position,
    _normalize_section_two_numbering,
)


SAMPLE_MARKDOWN = """# Demo — High-Level Design

## 2 Logical View

### 2.0 Architecture Decisions (Checkpoint B)

| Decision | Source | Design Impact |
| --- | --- | --- |
| Use MongoDB | Feature contract / Checkpoint B | Defines persistence boundary. |

### 2.0.1 Evidence and Confidence Summary

| Evidence Area | Count / Status | HLD Usage |
| --- | --- | --- |
| Mapped flows | 2 | Grounds sequence diagrams. |

### 2.0.2 Open Questions and To Be Confirmed

| Item | Source | Impact |
| --- | --- | --- |
| Confirm API shape | Feature contract unresolved list | Confirm before MDD. |

### 2.1 Food Module Logical View

Food responsibilities.

### 2.2 CGM Connection Service Logical View

CGM responsibilities.

### 2.3 Interactions and Flows

Flow steps.

### 2.z Requirements Traceability

| AC ID | Requirement (full) | Verifies (BL) | Mapped Code Symbol |
| --- | --- | --- | --- |
| AC-1 | Example | BL-1 | FoodController |

## 3 Security Approach
"""


class HLDSectionOrderTests(unittest.TestCase):
    def test_diagrams_insert_after_checkpoint_blocks(self) -> None:
        insert_pos = _find_logical_view_diagram_insert_pos(SAMPLE_MARKDOWN)
        updated = _insert_at_position(
            SAMPLE_MARKDOWN,
            insert_pos,
            "### 2.1 Feature Architecture Flow\n\nDiagram body.",
        )
        open_questions_pos = updated.find("### 2.0.2 Open Questions")
        diagram_pos = updated.find("### 2.1 Feature Architecture Flow")
        food_pos = updated.find("### 2.1 Food Module Logical View")
        self.assertGreater(diagram_pos, open_questions_pos)
        self.assertGreater(food_pos, diagram_pos)

    def test_renumber_module_sections_from_two_five(self) -> None:
        normalized = _normalize_section_two_numbering(SAMPLE_MARKDOWN)
        self.assertIn("### 2.5 Food Module Logical View", normalized)
        self.assertIn("### 2.6 CGM Connection Service Logical View", normalized)
        self.assertIn("### 2.7 Interactions and Flows", normalized)
        self.assertIn("### 2.8 Requirements Traceability", normalized)
        self.assertNotIn("### 2.1 Food Module Logical View", normalized)

    def test_full_section_two_order_after_diagram_insert_and_renumber(self) -> None:
        insert_pos = _find_logical_view_diagram_insert_pos(SAMPLE_MARKDOWN)
        diagram_block = "\n\n".join([
            "### 2.1 Feature Architecture Flow",
            "Feature flow diagram.",
            "### 2.2 Primary Interaction Sequence",
            "Sequence diagram.",
            "### 2.3 Feature Lifecycle",
            "Lifecycle diagram.",
            "### 2.4 Architecture Decisions and Evidence",
            "Decision diagram.",
        ])
        with_diagrams = _insert_at_position(SAMPLE_MARKDOWN, insert_pos, diagram_block)
        markdown = _normalize_section_two_numbering(with_diagrams)
        positions = {
            "open_questions": markdown.find("### 2.0.2 Open Questions"),
            "feature_flow": markdown.find("### 2.1 Feature Architecture Flow"),
            "food_module": markdown.find("### 2.5 Food Module Logical View"),
            "traceability": markdown.find("### 2.8 Requirements Traceability"),
        }
        self.assertTrue(all(pos >= 0 for pos in positions.values()))
        self.assertLess(positions["open_questions"], positions["feature_flow"])
        self.assertLess(positions["feature_flow"], positions["food_module"])
        self.assertLess(positions["food_module"], positions["traceability"])


if __name__ == "__main__":
    unittest.main()
