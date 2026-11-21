"""Unit tests for the tiny XMLform helper used elsewhere in the project.

Each test isolates one behavior: serialization, attributes, nesting, parsing,
pretty printing, and structural equality. Keeping these explicit makes it easy
to see which XML contract downstream scripts rely on.
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from common.xmlform import XMLform


class TestXMLform(unittest.TestCase):
    def test_build_and_serialize(self):
        # Build a root element and attach a child with two attributes.
        # Attributes are serialized in insertion order and numeric values are
        # normalized to strings by XMLform.
        root = XMLform("data")
        root.addChild(XMLform("item", attributes={"name": "xyz", "count": 1}))

        # The expected string pins down exact spacing and self-closing style.
        self.assertEqual(root.toString(), '<data><item name="xyz" count="1" /></data>')

    def test_attributes(self):
        # Start with an element that has no attributes.
        node = XMLform("item")

        # setAttribute stores values as strings and add/get APIs must agree.
        node.setAttribute("name", "xyz")
        node.setAttribute("count", 2)
        self.assertEqual(node.getAttributes(), {"name": "xyz", "count": "2"})

        # getAttribute must fall back to the supplied default for missing keys.
        self.assertEqual(node.getAttribute("missing", "d"), "d")

        # Removal must be observable through hasAttribute.
        node.removeAttribute("count")
        self.assertFalse(node.hasAttribute("count"))

    def test_text_and_children(self):
        # Text content and child elements are independent properties.
        root = XMLform("workflow")
        start = XMLform("start", text="starting")
        root.addChild(start)

        # addChildren accepts nested structures, so add a second child whose
        # own child carries an attribute.
        root.addChildren([XMLform("next_node").addChild(XMLform("decision", attributes={"id": 100}))])

        # Verify both the rendered text and the resulting child count.
        self.assertIn("<start>starting</start>", root.toString())
        self.assertEqual(len(root.children), 2)

    def test_parse_string(self):
        # Parsing an existing document should preserve tag names and text.
        form = XMLform('<documents><xrule> something</xrule></documents>')
        self.assertEqual(form.tag, "documents")
        self.assertEqual(form.find("xrule").text.strip(), "something")

        # str(form) must round-trip back to the original markup.
        self.assertEqual(str(form), "<documents><xrule> something</xrule></documents>")

    def test_pretty(self):
        # pretty=True should introduce line breaks for human-readable output.
        form = XMLform("data")
        form.addChild(XMLform("item", attributes={"name": "xyz"}))
        out = form.toString(pretty=True)
        self.assertIn("\n", out)

    def test_equality(self):
        # Equality is structural: a built tree must equal a parsed tree when
        # tags, attributes, and children match.
        a = XMLform("data")
        a.addChild(XMLform("item", attributes={"name": "xyz"}))
        b = XMLform('<data><item name="xyz" /></data>')
        self.assertEqual(a, b)


if __name__ == "__main__":
    unittest.main(verbosity=2)
