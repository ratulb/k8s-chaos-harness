from __future__ import annotations

import copy
import xml.etree.ElementTree as ET
from typing import Any, Iterable, Mapping


class XMLform:
    def __init__(
        self,
        source: Any = None,
        *,
        tag: str | None = None,
        attributes: Mapping[str, Any] | None = None,
        text: Any = None,
        children: Iterable[Any] | None = None,
    ) -> None:
        if isinstance(source, (str, bytes, bytearray)):
            payload = source.decode("utf-8") if isinstance(source, (bytes, bytearray)) else source
            stripped = payload.lstrip()
            if stripped.startswith("<"):
                self._element = ET.fromstring(payload)
            else:
                self._element = ET.Element(stripped or tag or "root")
        elif isinstance(source, ET.Element):
            self._element = source
        elif source is None:
            self._element = ET.Element(tag or "root")
        else:
            self._element = ET.Element(str(source))

        if tag is not None and not (isinstance(source, (str, bytes, bytearray)) and str(source).lstrip().startswith("<")):
            self._element.tag = tag

        if attributes:
            for key, value in attributes.items():
                self.setAttribute(key, value)
        if text is not None:
            self._element.text = "" if text is None else str(text)
        if children:
            self.addChildren(children)

    @property
    def tag(self) -> str:
        return self._element.tag

    @tag.setter
    def tag(self, value: str) -> None:
        self._element.tag = value

    @property
    def text(self) -> str | None:
        return self._element.text

    @text.setter
    def text(self, value: Any) -> None:
        self._element.text = None if value is None else str(value)

    def getAttributes(self) -> dict[str, str]:
        return dict(self._element.attrib)

    def getAttribute(self, name: str, default: Any = None) -> Any:
        return self._element.attrib.get(name, default)

    def hasAttribute(self, name: str) -> bool:
        return name in self._element.attrib

    def setAttribute(self, name: str, value: Any) -> "XMLform":
        self._element.set(name, "" if value is None else str(value))
        return self

    def setAttributes(self, attributes: Mapping[str, Any]) -> "XMLform":
        for key, value in attributes.items():
            self.setAttribute(key, value)
        return self

    def removeAttribute(self, name: str) -> "XMLform":
        if name in self._element.attrib:
            del self._element.attrib[name]
        return self

    def addChild(self, child: Any) -> "XMLform":
        if isinstance(child, XMLform):
            self._element.append(child._element)
        elif isinstance(child, ET.Element):
            self._element.append(child)
        else:
            raise TypeError("addChild expects an XMLform or ElementTree.Element")
        return self

    def addChildren(self, children: Iterable[Any]) -> "XMLform":
        for child in children:
            self.addChild(child)
        return self

    def removeChild(self, child: Any) -> "XMLform":
        if isinstance(child, XMLform):
            self._element.remove(child._element)
        elif isinstance(child, ET.Element):
            self._element.remove(child)
        return self

    @property
    def children(self) -> list["XMLform"]:
        return [XMLform(element) for element in self._element]

    def find(self, path: str) -> "XMLform | None":
        found = self._element.find(path)
        return XMLform(found) if found is not None else None

    def findall(self, path: str) -> list["XMLform"]:
        return [XMLform(element) for element in self._element.findall(path)]

    def toString(self, pretty: bool = False) -> str:
        element = copy.deepcopy(self._element)
        if pretty:
            try:
                ET.indent(element, space="  ")
            except AttributeError:
                pass
        return ET.tostring(element, encoding="unicode")

    def __str__(self) -> str:
        return self.toString()

    def __repr__(self) -> str:
        return f"XMLform({self.toString()!r})"

    def __eq__(self, other: Any) -> bool:
        if not isinstance(other, XMLform):
            return False
        if self._element.tag != other._element.tag:
            return False
        if self._element.attrib != other._element.attrib:
            return False
        if (self._element.text or "").strip() != (other._element.text or "").strip():
            return False
        left = list(self._element)
        right = list(other._element)
        if len(left) != len(right):
            return False
        return all(XMLform(a) == XMLform(b) for a, b in zip(left, right))

    def __len__(self) -> int:
        return len(self._element)

    def __iter__(self):
        for element in self._element:
            yield XMLform(element)
