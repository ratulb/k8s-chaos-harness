import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from common.xmlform import XMLform


class TestXMLform(unittest.TestCase):
    def test_build_and_serialize(self):
        root = XMLform("data")
        root.addChild(XMLform("item", attributes={"name": "xyz", "count": 1}))
        self.assertEqual(root.toString(), '<data><item name="xyz" count="1" /></data>')
    def test_attributes(self):
        node = XMLform("item")
        node.setAttribute("name", "xyz")
        node.setAttribute("count", 2)
        self.assertEqual(node.getAttributes(), {"name": "xyz", "count": "2"})
        self.assertEqual(node.getAttribute("missing", "d"), "d")
        node.removeAttribute("count")
        self.assertFalse(node.hasAttribute("count"))

    def test_text_and_children(self):
        root = XMLform("workflow")
        start = XMLform("start", text="starting")
        root.addChild(start)
        root.addChildren([XMLform("next_node").addChild(XMLform("decision", attributes={"id": 100}))])
        self.assertIn("<start>starting</start>", root.toString())
        self.assertEqual(len(root.children), 2)

    def test_parse_string(self):
        form = XMLform('<documents><xrule> something</xrule></documents>')
        self.assertEqual(form.tag, "documents")
        self.assertEqual(form.find("xrule").text.strip(), "something")
        self.assertEqual(str(form), "<documents><xrule> something</xrule></documents>")

    def test_pretty(self):
        form = XMLform("data")
        form.addChild(XMLform("item", attributes={"name": "xyz"}))
        out = form.toString(pretty=True)
        self.assertIn("\n", out)

    def test_equality(self):
        a = XMLform("data")
        a.addChild(XMLform("item", attributes={"name": "xyz"}))
        b = XMLform('<data><item name="xyz" /></data>')
        self.assertEqual(a, b)


if __name__ == "__main__":
    unittest.main(verbosity=2)
