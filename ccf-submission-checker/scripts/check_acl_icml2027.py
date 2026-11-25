#!/usr/bin/env python3
"""Check local ACL 2027 (ARR/ACL) and ICML 2027 research TeX/PDF packages."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import re
import shutil
import subprocess

from check_cvpr2027 import expand_inputs

TEMPLATES = Path(__file__).resolve().parents[2] / "ccf-latex-templates"
GUIDE = {
    "acl": ("acl", "acl_natbib", (595.276, 841.89), "A4", ("acl-2027-main-cfp", "acl-arr-cfp")),
    "icml": ("icml2027", "icml2027", (612, 792), "US Letter", ("icml-2027-author-instructions", "icml-2027-author-ai")),
}
PACKAGE = re.compile(r"\\usepackage\s*(?:\[([^]]*)\])?\s*\{([^}]+)\}")
SECTION = re.compile(r"\\section\*?\s*(?:\[[^]]*\])?\s*\{\s*([^{}]+?)\s*\}")
REFERENCES = re.compile(r"\\(?:bibliography\s*\{|printbibliography\b|begin\s*\{thebibliography\})")
APPENDIX = re.compile(r"\\appendix\b")
SECTION_TITLES = {
    "acl": {"limitations": {"limitations"}},
    "icml": {
        "impact_statement": {"impact statement"},
        "ai_disclosure": {"ai disclosure statement", "ai use statement"},
    },
}


def check_source(tex, project, venue, stage, add):
    package, bibliography_style, *_ = GUIDE[venue]
    visited = set()
    try:
        text = expand_inputs(tex, project, visited=visited)
    except (OSError, ValueError) as error:
        add("source_inputs", "not_verified", f"Inspect the rendered build and inputs: {error}")
        return
    add("source_inputs", "pass", f"Read {len(visited)} local TeX files")
    if re.search(r"\\if[A-Za-z@]*\b", text):
        add("source_declarations", "not_verified", "Conditional TeX requires inspection of the active build")
        return

    body_parts = re.split(r"\\begin\s*\{document\}", text, maxsplit=1)
    preamble, body = body_parts if len(body_parts) == 2 else (text, "")
    declarations = [(set(map(str.strip, match[2].split(","))),
                     {option.strip() for option in (match[1] or "").split(",") if option.strip()})
                    for match in PACKAGE.finditer(preamble)]
    options = [parsed_options for names, parsed_options in declarations if package in names]
    other_icml_style = venue == "icml" and any(
        re.fullmatch(r"icml\d{4}", name) and name != package
        for names, _ in declarations for name in names
    )
    expected_modes = ({"review"},) if venue == "acl" else (set(),)
    if stage == "camera-ready":
        expected_modes = (set(), {"final"}) if venue == "acl" else ({"accepted"},)
    allowed_non_modes = {"hyperref", "nohyperref"} if venue == "acl" else {"nohyperref"}
    if not options and not other_icml_style:
        add("template_mode", "not_verified", f"No direct {package} declaration found. Check active class/package and options.")
    else:
        mode_ok = len(options) == 1 and (options[0] - allowed_non_modes) in expected_modes and not other_icml_style
        add("template_mode", "pass" if mode_ok else "fail", f"Expected one {package} declaration with options for {stage}")

    kit = TEMPLATES / venue.upper() / "2027"
    try:
        manifest = json.loads((kit / "template-source.json").read_text(encoding="utf-8"))
        hashes = {item["path"]: item["sha256"] for item in manifest["files"]}
    except (OSError, ValueError, KeyError):
        hashes = {}
    for name in (f"{package}.sty", f"{bibliography_style}.bst"):
        file = next((base / name for base in (tex.parent, project) if (base / name).is_file()), None)
        expected = hashes.get(name)
        if file and expected:
            data = file.read_bytes()
            valid = expected in {hashlib.sha256(data).hexdigest(),
                                 hashlib.sha256(data.replace(b"\r\n", b"\n")).hexdigest()}
            add(name, "pass" if valid else "not_verified", "Official snapshot matched" if valid else
                "Snapshot differs. Compare the style selected by the build with the current official file.")
        else:
            add(name, "not_verified", "Inspect the file source and TeX search path")

    if not body:
        add("required_sections", "not_verified", "Document body not found in standard form. Inspect the compiled paper.")
        return

    check_sections(body, venue, add)


def check_sections(body, venue, add):
    references = REFERENCES.search(body)
    appendix = APPENDIX.search(body)
    headings = list(SECTION.finditer(body))
    boundaries = [match.start() for match in [*headings, references, appendix] if match]

    titles = SECTION_TITLES[venue]
    end_matter = set().union(*titles.values(), {
        "acknowledgments", "acknowledgements", "ethical considerations", "ethics statement",
    })
    tail_offsets = [match.start() for match in (references, appendix) if match]
    text_end = min(tail_offsets, default=len(body))
    main_headings = [heading for heading in headings
                     if heading.start() < text_end and heading[1].casefold() not in end_matter]
    for key, names in titles.items():
        sections = [heading for heading in headings if heading[1].casefold() in names]
        if not sections:
            add(key, "not_verified", "Required heading not detected. Inspect the PDF, including macro-defined headings.")
            continue
        heading = sections[0]
        end = min((offset for offset in boundaries if offset > heading.start()), default=len(body))
        content = body[heading.end():end].split(r"\end{document}", 1)[0].strip()
        visible = re.sub(r"\\(?:label|index)\s*\{[^{}]*\}", "", content).strip()
        if len(sections) != 1 or not visible or re.match(r"^(?:\[\s*)?(?:todo|tbd|placeholder)\b", visible, re.I):
            add(key, "fail", "Expected one populated section")
        elif main_headings and heading.start() < main_headings[-1].start():
            add(key, "fail", "Required section appears before the last main-text section")
        elif key != "ai_disclosure" and references is None:
            add(key, "not_verified", "Heading found. Verify its placement before references in the PDF.")
        elif key != "ai_disclosure" and heading.start() > references.start():
            add(key, "fail", "Required section appears after references")
        elif appendix and heading.start() > appendix.start():
            add(key, "not_verified", "Section appears inside appendix source. Verify required placement in the PDF.")
        else:
            add(key, "pass", "Matching source section found. Verify rendered placement and content.")

    if appendix and references:
        proper_order = references.start() < appendix.start()
        add("appendix_order", "pass" if proper_order else "fail", "Appendices follow references")


def check_pdf(pdf, main_pages, limit, venue, stage, add):
    if pdf is None:
        add("pdf", "not_verified", "Supply the compiled PDF to inspect the package")
        return
    if not pdf.is_file():
        add("pdf", "fail", "PDF not found")
        return
    pdfinfo = shutil.which("pdfinfo")
    if not pdfinfo:
        add("pdf", "not_verified", "pdfinfo unavailable")
        return
    try:
        result = subprocess.run([pdfinfo, "-enc", "UTF-8", "-isodates", "-f", "1", "-l", "1000000",
                                 str(pdf.resolve())],
                                capture_output=True, check=True, timeout=20)
        info = result.stdout.decode("utf-8")
        pages_match = re.search(r"^Pages:[ \t]+(\d+)", info, re.M)
        if not pages_match:
            raise ValueError("Page count unavailable")
        pages = int(pages_match[1])
    except (OSError, subprocess.SubprocessError, ValueError) as error:
        add("pdf", "not_verified", f"Could not inspect PDF: {type(error).__name__}")
        return
    add("pdf", "pass", f"Readable PDF, {pages} total pages")

    _, _, dimensions, size_name, _ = GUIDE[venue]
    sizes = {int(number): (float(width), float(height)) for number, width, height in re.findall(
        r"^Page[ \t]+(\d+)[ \t]+size:[ \t]+([\d.]+) x ([\d.]+) pts", info, re.M
    )}
    if not sizes:
        first_size = re.search(r"^Page size:[ \t]+([\d.]+) x ([\d.]+) pts", info, re.M)
        if first_size:
            sizes[1] = (float(first_size[1]), float(first_size[2]))
    wrong = [page for page, size in sizes.items()
             if any(abs(value - target) >= 1 for value, target in zip(size, dimensions))]
    first_page_status = "fail" if 1 in wrong else "pass" if 1 in sizes else "not_verified"
    add("first_page_size", first_page_status, f"Expected {size_name} on the first page")
    if wrong:
        numbers = ", ".join(map(str, wrong[:10]))
        add("page_geometry", "fail", f"Expected {size_name}. Off-size pages: {numbers} ({len(wrong)} total)")
    elif len(sizes) == pages:
        add("page_geometry", "pass", f"All {pages} pages use {size_name}")
    else:
        add("page_geometry", "not_verified", f"Measured {len(sizes)} of {pages} pages")

    if main_pages is not None:
        valid = 0 < main_pages <= min(pages, limit)
        add("main_pages", "pass" if valid else "fail", f"Inspected main pages: {main_pages}, limit: {limit}, total: {pages}")
    elif pages <= limit:
        add("main_pages", "pass", f"The entire PDF fits the {limit}-page main-text cap")
    else:
        add("main_pages", "not_verified", f"Inspect the body boundary before comparing with the {limit}-page cap")

    if stage == "review":
        author = re.search(r"^Author:[ \t]*([^\r\n]*)", info, re.M)
        value = author[1].strip().casefold() if author else ""
        accepted = {"", "anonymous", "anonymous authors", "anonymous submission", "anonymous acl submission",
                    "anonymous author(s)"}
        add("pdf_author_metadata", "pass" if value in accepted else "fail",
            "Author metadata is empty or anonymous" if value in accepted else
            "Non-anonymous Author metadata. Inspect it and remove identifying information.")


def audit(venue, track, stage, tex, pdf=None, main_pages=None, project_root=None):
    checks = []

    def add(key, status, detail):
        checks.append({"id": key, "status": status, "detail": detail})

    tex = tex.resolve()
    project = project_root.resolve() if project_root else tex.parent
    limit = (4 if track == "short" else 8) + (stage == "camera-ready")
    check_source(tex, project, venue, stage, add)
    check_pdf(pdf, main_pages, limit, venue, stage, add)
    add("manual", "not_verified", "Verify live rules, build logs, rendered submission, supplements and required section content")
    if venue == "acl" and stage == "review":
        add("arr_submission", "not_verified", "Check cycle, Responsible NLP answers, AI assistance, profiles and service registration")
    elif venue == "acl":
        add("acl_commitment", "not_verified", "Check the committed ARR version, decision, author list and final instructions")
    elif stage == "review":
        add("icml_submission", "not_verified", "Check contribution type, actual AI use, profiles, quotas and reviewer eligibility")
    else:
        add("icml_camera_ready", "not_verified", "Check final limits and metadata, registration and lay summary")
    failures = sum(check["status"] == "fail" for check in checks)
    return {"venue": venue.upper(), "year": 2027, "track": track, "stage": stage,
            "policy_records": GUIDE[venue][-1], "policy_checked": "2026-11-25",
            "status": "fail" if failures else "not_verified", "failures": failures, "checks": checks}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--venue", choices=("acl", "icml"), required=True)
    parser.add_argument("--track", choices=("long", "short", "research"), help="Required for ACL; ICML supports research")
    parser.add_argument("--stage", choices=("review", "camera-ready"), default="review")
    parser.add_argument("--tex", type=Path, required=True)
    parser.add_argument("--project-root", type=Path)
    parser.add_argument("--pdf", type=Path)
    parser.add_argument("--main-pages", type=int, help="Main-text pages after inspecting the compiled PDF")
    args = parser.parse_args()
    if args.venue == "acl" and args.track not in {"long", "short"}:
        parser.error("ACL requires --track long or --track short")
    if args.venue == "icml" and args.track not in {None, "research"}:
        parser.error("ICML supports --track research")
    if args.main_pages is not None and (args.pdf is None or args.main_pages < 1):
        parser.error("--main-pages must be positive and requires --pdf")
    report = audit(args.venue, args.track or "research", args.stage, args.tex, args.pdf,
                   args.main_pages, args.project_root)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 1 if report["failures"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
