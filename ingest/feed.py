"""Streaming reads of the PHOL XML files, and the element helpers the parsers
share.

The product files are up to 689 MB, so nothing here loads a whole file: each
<Product> (or <Fund>) is handled as its end event arrives and its subtree is
then cleared, which keeps memory flat at the size of one product.
"""
from __future__ import annotations

from typing import Iterator, Optional

from lxml import etree

NS = "http://admin.privatehealth.gov.au/ws/Schemas"
XSI_NIL = "{http://www.w3.org/2001/XMLSchema-instance}nil"


def q(name: str) -> str:
    return f"{{{NS}}}{name}"


def child(el: Optional[etree._Element], name: str) -> Optional[etree._Element]:
    """A child element, or None when it is absent or xsi:nil."""
    if el is None:
        return None
    c = el.find(q(name))
    if c is None or c.get(XSI_NIL) == "true":
        return None
    return c


def children(el: Optional[etree._Element], name: str) -> list[etree._Element]:
    if el is None:
        return []
    return [c for c in el.findall(q(name)) if c.get(XSI_NIL) != "true"]


def text(el: Optional[etree._Element], name: str) -> Optional[str]:
    """The trimmed text of a child element; None when absent, nil or empty."""
    c = child(el, name)
    if c is None:
        return None
    t = (c.text or "").strip()
    return t or None


def boolean(value: Optional[str]) -> Optional[bool]:
    if value is None:
        return None
    if value in ("true", "false"):
        return value == "true"
    raise ValueError(f"not a schema boolean: {value!r}")


def number(value: Optional[str]) -> Optional[float]:
    if value is None or value == "":
        return None
    return float(value)


def integer(value: Optional[str]) -> Optional[int]:
    if value is None or value == "":
        return None
    return int(value)


def root_count(path: str, root_tag: str) -> int:
    """The Count attribute the publisher writes on the root element. It is the
    government's own statement of how many records the file holds, so the
    load is checked against it rather than against our own count."""
    # The root carries no namespace in this feed (<Products Count="...">); only its children do.
    for _, el in etree.iterparse(path, events=("start",), tag=root_tag, huge_tree=True):
        return int(el.get("Count"))
    raise ValueError(f"{path}: no <{root_tag}> element")


def iter_elements(path: str, tag: str) -> Iterator[etree._Element]:
    """Yield each <tag> element in the file, clearing it once consumed."""
    for _, el in etree.iterparse(path, events=("end",), tag=q(tag), huge_tree=True):
        yield el
        el.clear()
        while el.getprevious() is not None:
            del el.getparent()[0]


def to_dict(el: etree._Element) -> object:
    """A small element as plain data: attributes and children, recursively.
    Used for the fund-level records, which are small and whose exact shape
    is kept in JSON rather than modelled one column at a time. A child that
    repeats becomes a list; a child that appears once is a plain value."""
    if el.get(XSI_NIL) == "true":
        return None
    attrs = {k: v for k, v in el.attrib.items() if not k.startswith("{")}
    kids = list(el)
    if not kids:
        value = (el.text or "").strip() or None
        return {**attrs, "_text": value} if attrs else value
    grouped: dict[str, list] = {}
    for c in kids:
        grouped.setdefault(etree.QName(c).localname, []).append(to_dict(c))
    out: dict = dict(attrs)
    for name, values in grouped.items():
        out[name] = values if len(values) > 1 else values[0]
    return out
