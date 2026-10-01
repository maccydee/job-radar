"""Write a .docx from Markdown, with no dependencies.

The tool asked for a .docx and handed back a .md. A teacher cannot attach
Markdown to a TES application and has no editor for it, so the deliverable
stopped one step short of being a deliverable.

A .docx is a zip of XML. This writes the minimum a word processor will open:
headings, bold, bullets and paragraphs. It is not a Word feature set, it is a
readable document you can edit and send.
"""

from __future__ import annotations

import io
import re
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from xml.sax.saxutils import escape

from .state import atomic_write_bytes

_CT = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>
<Default Extension="xml" ContentType="application/xml"/>
<Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>
<Override PartName="/word/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.styles+xml"/>
</Types>"""

# The two property parts are registered only when they are written. A part in
# the zip with no Override and no Relationship is invisible: Word and
# LibreOffice read the package through its relationships, not by listing the
# files, so the file looks right and the metadata never arrives.
_CT_PROPS = (
    '<Override PartName="/docProps/core.xml" ContentType="application/vnd.'
    'openxmlformats-package.core-properties+xml"/>\n'
    '<Override PartName="/docProps/app.xml" ContentType="application/vnd.'
    'openxmlformats-officedocument.extended-properties+xml"/>\n')

_RELS_PROPS = (
    '<Relationship Id="rId2" Type="http://schemas.openxmlformats.org/package/'
    '2006/relationships/metadata/core-properties" Target="docProps/core.xml"/>\n'
    '<Relationship Id="rId3" Type="http://schemas.openxmlformats.org/officeDocument/'
    '2006/relationships/extended-properties" Target="docProps/app.xml"/>\n')

_RELS = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="word/document.xml"/>
</Relationships>"""

_DOC_RELS = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles" Target="styles.xml"/>
</Relationships>"""

_APP = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Properties xmlns="http://schemas.openxmlformats.org/officeDocument/2006/extended-properties">
<DocSecurity>0</DocSecurity>
</Properties>"""

_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f]")


def _owner(md: str) -> str:
    """The name on the first `# ` line, or "" if the document has none.

    The candidate's own name is already in the document, so this needs no
    config and works for every user of the repo. "" is a real answer: a
    document with no name gets no properties at all, because the alternative
    is inventing an author, and a made-up one is worse than a blank.
    """
    for raw in md.splitlines():
        if raw.startswith("# "):
            # Bold markers are markup, not part of anybody's name; control
            # characters are not legal XML and would make the part unreadable.
            name = raw[2:].replace("**", "")
            return _CONTROL.sub("", name).strip()
    return ""


def _core(name: str) -> str:
    """docProps/core.xml: who made the document, and when.

    LibreOffice carries these straight into the PDF's XMP, and the PDF is the
    only file a recruiter ever receives. Without them the PDF had no author
    and no title, which is the one thing every other CV they open does have,
    and recruiters have been told to look at file properties.

    The title is the name alone, not "name CV": the same writer also produces
    cover letters, and a letter titled "CV" would be wrong in exactly the
    place somebody is checking for things that are wrong.

    Both timestamps are the real time of writing and are the same value,
    because this is a document made once. They are never offset or backdated;
    a document dated to look older than it is would be the very deception
    this metadata exists to avoid being mistaken for.
    """
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    n = escape(name)
    return (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
        '<cp:coreProperties xmlns:cp="http://schemas.openxmlformats.org/package/'
        '2006/metadata/core-properties" xmlns:dc="http://purl.org/dc/elements/1.1/" '
        'xmlns:dcterms="http://purl.org/dc/terms/" '
        'xmlns:dcmitype="http://purl.org/dc/dcmitype/" '
        'xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance">\n'
        f'<dc:title>{n}</dc:title>\n'
        f'<dc:creator>{n}</dc:creator>\n'
        f'<cp:lastModifiedBy>{n}</cp:lastModifiedBy>\n'
        f'<dcterms:created xsi:type="dcterms:W3CDTF">{now}</dcterms:created>\n'
        f'<dcterms:modified xsi:type="dcterms:W3CDTF">{now}</dcterms:modified>\n'
        '</cp:coreProperties>')


_W = 'xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"'

# One typeface, declared on every style, and no shouting.
#
# Three things were wrong with the old block and all three were visible the
# moment somebody opened the file.
#
# `<w:caps/>` on Heading1 rendered every section as PROFILE, EXPERIENCE,
# SELECTED ACHIEVEMENTS. On a CV already carrying 28 distinct acronyms that
# is a page of capitals, and it is the generator shouting rather than
# anything the writer asked for.
#
# Only docDefaults named a font, and only for `ascii` and `hAnsi`. A style
# that does not name one inherits from the theme, and this file ships no
# theme part, so a word processor falls back to its own default for those
# runs: headings in one face and body text in another, in a document that
# never asked for two. Every style now names the font, and `cs` and
# `eastAsia` are set too so a single non-Latin character does not switch
# face mid-line.
#
# The maroon heading colour was a decision nobody made. Headings are near
# black; a CV is not the place for the generator to have an opinion.
_FONT = ('<w:rFonts w:ascii="Calibri" w:hAnsi="Calibri" w:cs="Calibri" '
         'w:eastAsia="Calibri"/>')

_STYLES = f"""<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<w:styles {_W}>
<w:docDefaults><w:rPrDefault><w:rPr>
  {_FONT}<w:sz w:val="20"/><w:szCs w:val="20"/>
</w:rPr></w:rPrDefault></w:docDefaults>

<w:style w:type="paragraph" w:default="1" w:styleId="Normal"><w:name w:val="Normal"/>
  <w:pPr><w:spacing w:after="100" w:line="252" w:lineRule="auto"/></w:pPr>
  <w:rPr>{_FONT}<w:sz w:val="20"/></w:rPr></w:style>

<w:style w:type="paragraph" w:styleId="Title"><w:name w:val="Title"/><w:pPr>
  <w:spacing w:after="40"/></w:pPr>
  <w:rPr>{_FONT}<w:b/><w:sz w:val="40"/><w:color w:val="111111"/>
  <w:spacing w:val="-10"/></w:rPr></w:style>

<w:style w:type="paragraph" w:styleId="Subtitle"><w:name w:val="Subtitle"/><w:pPr>
  <w:spacing w:after="40"/></w:pPr>
  <w:rPr>{_FONT}<w:sz w:val="21"/><w:color w:val="444444"/></w:rPr></w:style>

<!-- The rule is what separates one section from the next. Without it the
     headings sat at nearly body weight and the page read as one column of
     grey: a reader skimming for Experience had nothing to aim at. -->
<w:style w:type="paragraph" w:styleId="Heading1"><w:name w:val="heading 1"/><w:pPr>
  <w:spacing w:before="260" w:after="100"/>
  <w:pBdr><w:bottom w:val="single" w:sz="6" w:space="3" w:color="C8C8C8"/></w:pBdr>
  </w:pPr>
  <w:rPr>{_FONT}<w:b/><w:sz w:val="23"/><w:color w:val="111111"/></w:rPr></w:style>

<w:style w:type="paragraph" w:styleId="Heading2"><w:name w:val="heading 2"/><w:pPr>
  <w:spacing w:before="180" w:after="20"/><w:keepNext/></w:pPr>
  <w:rPr>{_FONT}<w:b/><w:sz w:val="21"/><w:color w:val="111111"/></w:rPr></w:style>

<!-- Three levels, because a CV has three: section, employer, role. The
     sub-roles under Deloitte were bold body text, which put them at the same
     weight as the employer above them and flattened seven years into a list
     of equals. -->
<w:style w:type="paragraph" w:styleId="Heading3"><w:name w:val="heading 3"/><w:pPr>
  <w:spacing w:before="140" w:after="20"/><w:keepNext/></w:pPr>
  <w:rPr>{_FONT}<w:b/><w:i/><w:sz w:val="20"/><w:color w:val="333333"/></w:rPr></w:style>

<w:style w:type="paragraph" w:styleId="ListParagraph"><w:name w:val="List Paragraph"/>
  <w:pPr><w:ind w:left="340" w:hanging="200"/>
  <w:spacing w:after="60" w:line="252" w:lineRule="auto"/></w:pPr>
  <w:rPr>{_FONT}</w:rPr></w:style>
</w:styles>"""


_BOLD = re.compile(r"\*\*(.+?)\*\*")


def _runs(text: str) -> str:
    """Inline markup: bold only, which is all these documents use."""
    out, pos = [], 0
    for m in _BOLD.finditer(text):
        if m.start() > pos:
            out.append(f'<w:r><w:t xml:space="preserve">{escape(text[pos:m.start()])}</w:t></w:r>')
        out.append(f'<w:r><w:rPr><w:b/></w:rPr><w:t xml:space="preserve">{escape(m.group(1))}</w:t></w:r>')
        pos = m.end()
    if pos < len(text):
        out.append(f'<w:r><w:t xml:space="preserve">{escape(text[pos:])}</w:t></w:r>')
    return "".join(out) or '<w:r><w:t/></w:r>'


def _para(text: str, style: str | None = None) -> str:
    ppr = f'<w:pPr><w:pStyle w:val="{style}"/></w:pPr>' if style else ""
    return f"<w:p>{ppr}{_runs(text)}</w:p>"


def markdown_to_docx(md: str, out_path: Path) -> Path:
    body = []
    # The two lines under the name are the strapline and the contact details.
    # They are not body paragraphs and were rendered as though they were, so
    # the top of the CV had no shape to it at all.
    seen_title, after_title = False, 0
    for raw in md.splitlines():
        line = raw.rstrip()
        if not line.strip():
            continue
        if line.startswith("#### "):
            body.append(_para(line[5:], "Heading3"))
        elif line.startswith("### "):
            body.append(_para(line[4:], "Heading2"))
        elif line.startswith("## "):
            body.append(_para(line[3:], "Heading1"))
        elif line.startswith("# "):
            body.append(_para(line[2:], "Title"))
            seen_title = True
            continue
        elif re.match(r"^\s*[-*+]\s+", line):
            body.append(_para("• " + re.sub(r"^\s*[-*+]\s+", "", line), "ListParagraph"))
        elif set(line.strip()) <= {"-", "="} and len(line.strip()) > 2:
            continue                       # a horizontal rule
        elif seen_title and after_title < 2:
            body.append(_para(line, "Subtitle"))
            after_title += 1
        else:
            body.append(_para(line))

    doc = (f'<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
           f'<w:document {_W}><w:body>{"".join(body)}'
           f'<w:sectPr><w:pgSz w:w="11906" w:h="16838"/>'
           f'<w:pgMar w:top="900" w:right="1000" w:bottom="900" w:left="1000"/>'
           f'</w:sectPr></w:body></w:document>')

    # Built in memory, then written atomically. Writing the zip straight to
    # the final path truncates it first, so an interrupted write leaves a file
    # with a .docx name that no word processor will open, where a good one
    # used to be. These documents are one per application and regenerating one
    # costs a model call, so the old version is worth keeping. A CV is a few
    # kilobytes, so holding it in memory costs nothing.
    name = _owner(md)
    ct, rels = _CT, _RELS
    if name:
        ct = _CT.replace("</Types>", _CT_PROPS + "</Types>")
        rels = _RELS.replace("</Relationships>", _RELS_PROPS + "</Relationships>")
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("[Content_Types].xml", ct)
        z.writestr("_rels/.rels", rels)
        if name:
            z.writestr("docProps/core.xml", _core(name))
            z.writestr("docProps/app.xml", _APP)
        z.writestr("word/_rels/document.xml.rels", _DOC_RELS)
        z.writestr("word/styles.xml", _STYLES)
        z.writestr("word/document.xml", doc)
    return atomic_write_bytes(out_path, buf.getvalue())
