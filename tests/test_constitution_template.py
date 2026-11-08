import re
import unittest
from pathlib import Path

TEXT = (Path(__file__).resolve().parents[1] / 'docs/constitution-template.md').read_text()
SECTIONS = ['Purpose', 'Boundaries', 'Principles', 'Evidence', 'Contributions', 'Governance', 'Amendment', 'Version log']


class ConstitutionTemplateTest(unittest.TestCase):
    def test_the_section_table_lists_the_sections_in_order(self):
        found = re.findall(r'^\| `## ([^`]+)` \|', TEXT, re.M)
        self.assertEqual(found, SECTIONS)

    def test_the_governance_block_has_one_pair_of_markers(self):
        starts = re.findall(r'<!-- governance:start v(\d+) -->', TEXT)
        ends = re.findall(r'<!-- governance:end v(\d+) -->', TEXT)
        self.assertEqual(len(starts), 1)
        self.assertEqual(starts, ends)
        block = TEXT.split(f'<!-- governance:start v{starts[0]} -->')[1].split('<!-- governance:end')[0]
        self.assertIn('docs/STANDARD.md', block)
        self.assertIn('never merges a level 1 PR', block)


if __name__ == '__main__':
    unittest.main()
