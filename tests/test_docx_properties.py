"""Document properties on the .docx, which become the PDF's author.

The .docx had no docProps part at all, so the PDF made from it had no author
and no title. Every other document a recruiter opens has one, and recruiters
have been told to check file properties, so a blank author reads as a CV that
has been stripped of something. The candidate's name is already in the
document; it just was never written where a file-properties dialog looks.
"""

from __future__ import annotations

import sys
import tempfile
import zipfile
from pathlib import Path
from xml.etree import ElementTree as ET

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from jobradar.docx import markdown_to_docx

MD = """# Dana Whitfield

Engineering Manager

## Profile

I manage six engineers.
"""

# The five parts every document had before properties existed.
ORIGINAL_PARTS = {"[Content_Types].xml", "_rels/.rels",
                  "word/_rels/document.xml.rels", "word/styles.xml",
                  "word/document.xml"}

NS_CT = "{http://schemas.openxmlformats.org/package/2006/content-types}"
NS_REL = "{http://schemas.openxmlformats.org/package/2006/relationships}"
NS_DC = "{http://purl.org/dc/elements/1.1/}"
NS_CP = "{http://schemas.openxmlformats.org/package/2006/metadata/core-properties}"


def _build(md: str = MD) -> zipfile.ZipFile:
    out = Path(tempfile.mkdtemp()) / "cv.docx"
    markdown_to_docx(md, out)
    return zipfile.ZipFile(out)


def _core(z: zipfile.ZipFile) -> ET.Element:
    return ET.fromstring(z.read("docProps/core.xml"))


def test_the_candidates_name_is_the_author_and_the_title():
    """No author is the anomaly. The name comes from the `# ` heading so it
    needs no config and is right for every user of the repo."""
    root = _core(_build())
    assert root.findtext(f"{NS_DC}creator") == "Dana Whitfield"
    assert root.findtext(f"{NS_CP}lastModifiedBy") == "Dana Whitfield"
    # Name alone, not "name CV": cover letters come through the same writer.
    assert root.findtext(f"{NS_DC}title") == "Dana Whitfield"


def test_the_properties_are_registered_not_just_present():
    """The silent one. A part in the zip with no Override and no Relationship
    is ignored by Word and LibreOffice, which read the package through its
    relationships. The file looks right, opens fine, and the author never
    arrives. So check the registration itself, in both places."""
    z = _build()
    ct = ET.fromstring(z.read("[Content_Types].xml"))
    overrides = {o.get("PartName"): o.get("ContentType")
                 for o in ct.findall(f"{NS_CT}Override")}
    assert overrides["/docProps/core.xml"] == (
        "application/vnd.openxmlformats-package.core-properties+xml")
    assert overrides["/docProps/app.xml"] == (
        "application/vnd.openxmlformats-officedocument.extended-properties+xml")

    rels = ET.fromstring(z.read("_rels/.rels"))
    targets = {r.get("Target"): r.get("Type")
               for r in rels.findall(f"{NS_REL}Relationship")}
    assert targets["docProps/core.xml"].endswith("/metadata/core-properties")
    assert targets["docProps/app.xml"].endswith("/extended-properties")
    # Relationship ids are unique, or one of the three shadows another.
    ids = [r.get("Id") for r in rels.findall(f"{NS_REL}Relationship")]
    assert len(ids) == len(set(ids))
    # And the part both of them point at exists.
    names = set(z.namelist())
    assert "docProps/core.xml" in names and "docProps/app.xml" in names
    ET.fromstring(z.read("docProps/app.xml"))


def test_no_heading_means_no_properties_and_no_crash():
    """With no name there is nothing true to write, and an invented author is
    worse than a blank one. The document still has to open."""
    z = _build("Just a paragraph.\n\n## Profile\n\nNo title line at all.\n")
    names = set(z.namelist())
    assert not any(n.startswith("docProps/") for n in names)
    assert names == ORIGINAL_PARTS
    ct = z.read("[Content_Types].xml").decode("utf-8")
    rels = z.read("_rels/.rels").decode("utf-8")
    assert "docProps" not in ct and "docProps" not in rels, \
        "registered a part that is not in the zip"
    for n in names:
        ET.fromstring(z.read(n))


def test_a_heading_that_is_only_markup_is_not_a_name():
    """`# **` is a heading with no name in it. Writing an empty author would
    look like a property that was set, which it was not."""
    z = _build("# **\n\nbody\n")
    assert not any(n.startswith("docProps/") for n in z.namelist())


def test_the_body_is_unchanged_by_the_properties():
    """The properties are additive. All five original parts remain, still
    well-formed, and the document still holds the CV."""
    z = _build()
    assert ORIGINAL_PARTS <= set(z.namelist())
    for n in ORIGINAL_PARTS:
        ET.fromstring(z.read(n))
    doc = z.read("word/document.xml").decode("utf-8")
    assert 'w:val="Title"' in doc and "Dana Whitfield" in doc
    assert "I manage six engineers." in doc
    # The original registrations are still there next to the new ones.
    ct = z.read("[Content_Types].xml").decode("utf-8")
    assert "/word/document.xml" in ct and "/word/styles.xml" in ct
    rels = z.read("_rels/.rels").decode("utf-8")
    assert 'Target="word/document.xml"' in rels


def test_a_name_with_xml_in_it_still_makes_a_readable_part():
    """An ampersand in a name (or a company-style heading) unescaped makes the
    part unparseable, and a reader rejecting core.xml rejects the file."""
    root = _core(_build("# Dana & <Whitfield>\n\nbody\n"))
    assert root.findtext(f"{NS_DC}creator") == "Dana & <Whitfield>"


def test_bold_markers_are_not_part_of_the_name():
    root = _core(_build("# **Dana** Whitfield\n\nbody\n"))
    assert root.findtext(f"{NS_DC}creator") == "Dana Whitfield"


def test_created_and_modified_are_present_and_the_same_moment():
    """Real time of writing, never offset. No exact value is asserted: the
    point is that both exist, are W3CDTF, and were not made to differ."""
    root = _core(_build())
    created = root.findtext("{http://purl.org/dc/terms/}created")
    modified = root.findtext("{http://purl.org/dc/terms/}modified")
    assert created and created == modified
    from datetime import datetime
    datetime.strptime(created, "%Y-%m-%dT%H:%M:%SZ")
