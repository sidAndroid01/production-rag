"""Build paired Markdown and self-contained HTML study guides from the question bank."""

# ruff: noqa: E501 -- Keep authored prose and generated HTML intact.
import base64
import html
import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent
QUICK_URL = (
    "https://docs.google.com/document/d/1SZEuldphejmk55V2Q3Gwxgb076uMTJZRP5obBGvJuwU/edit?tab=t.0"
)
DETAIL_URL = (
    "https://docs.google.com/document/d/19JF4nCVackD17nZpXFZ8xQ4Cp3kC96kDndTJ7rxcLV0/edit?tab=t.0"
)
sections = json.loads((ROOT / "question-bank.json").read_text())
questions = [q for section in sections for q in section["questions"]]
assert len(questions) == 220
assert [q["id"] for q in questions] == [f"Q{i:03}" for i in range(1, 221)]

quick = [
    "# RAG interview quick revision for an Android developer",
    "Use these 220 question and answer pairs for quick recall. The detailed guide uses the same question IDs and includes explanations, examples, code references, and primary-source links.",
    "Current scope: an offline FastAPI service with UTF-8 ingestion, in-memory lexical cosine retrieval, an extractive generator, citations, a shared API key, and engineering scaffolding. Embeddings, a persistent database, a hosted LLM, a durable queue, and a RAG evaluation benchmark are not built.",
    "Code re-inspected 6 September 2026. The recorded 5 September verification passed 8 tests with 94.76% statement coverage plus lint, formatting, and typing checks. Coverage is not answer accuracy.",
    "Android bridges: Retrofit → HTTP contract; Kotlin DTOs → Pydantic schemas; Hilt → dependency injection; repository interface → Protocol; application singleton → per-process app.state; Room → future durable storage; suspend → async with no automatic CPU parallelism.",
    "Numbers to remember for this code: 5,000,000 upload bytes; 2,000 stripped query characters; 900-character chunks; 120-character intended overlap; top-k 5; minimum score 0.10; generation uses up to 3 hits; citation excerpts use 240 characters; document IDs use 24 hex characters.",
    "Important defects and limits: shared default API tenant, duplicate reuploads, non-advancing chunk-loop edge case, volatile storage, constant readiness, and grounding that only means a hit passed the score gate.",
    "Each answer below is one sentence. Use the matching detailed entry whenever you cannot explain the mechanism or tradeoff.",
]
detail = [(ROOT / "detailed-introduction.md").read_text().rstrip()]
for i, section in enumerate(sections, 1):
    heading = f"## {i} {section['title']}"
    quick.append(heading)
    detail.extend([heading, section["overview"]])
    for q in section["questions"]:
        quick.append(f"- **{q['id']} {q['question']}** {q['quick']}")
        detail.extend(
            [
                f"### {q['id']} {q['question']}",
                f"**Short answer:** {q['quick']}",
                q["detail"],
                f"**Self-test follow-up:** {q['follow']}",
            ]
        )

source_links = []
for q in questions:
    for label, url in re.findall(r"\[([^]]+)\]\((https?://[^)]+)\)", q["detail"]):
        if url not in [u for _, u in source_links]:
            source_links.append((label, url))
detail.extend(
    [
        "## Primary references",
        "The implementation claims come from the repository files listed in the tutorial. The following official documentation and research papers support the general backend and RAG concepts; links also appear beside the relevant explanations. Design proposals and numerical practice assumptions are labeled separately.",
        *[f"- [{label}]({url})" for label, url in source_links],
        "## Completion checklist for your preparation",
        "- Explain every current module and API field without claiming future features are built.\n- Calculate the handbook score and toy retrieval metrics.\n- Demonstrate upload, a supported result, abstention, duplicate ingestion, and restart data loss.\n- Explain chunk termination and idempotency invariants.\n- Compare lexical, dense, hybrid, and reranked retrieval using a concrete question.\n- Design tenant-safe persistence, jobs, generation, and caching.\n- Separate unit tests, evaluation quality, security, and operational metrics.\n- Practice at least one latency/cost estimate and one five-minute system-design walkthrough.\n- Revisit questions you cannot answer with an example, failure case, and test.",
    ]
)
quick.extend(
    [
        "## Final interview reminders",
        "- Similarity is not confidence; code coverage is not answer accuracy.\n- Authentication is not authorization; a tenant field is not complete isolation.\n- Async is not CPU parallelism; a background callback is not a durable queue.\n- Hashing is not encryption or complete idempotency.\n- Citations are not proof of entailment; valid JSON is not factual correctness.\n- More chunks, larger models, and advanced patterns are experiments, not automatic improvements.\n- State assumptions, measure tradeoffs, and describe only work actually implemented.",
    ]
)
(ROOT / "quick-revision.md").write_text("\n\n".join(quick) + "\n")
(ROOT / "detailed-explanation.md").write_text("\n\n".join(detail) + "\n")


def inline(text):
    text = html.escape(text)
    text = re.sub(r"`([^`]+)`", r"<code>\1</code>", text)
    text = re.sub(r"\*\*([^*]+)\*\*", r"<strong>\1</strong>", text)
    text = re.sub(r"\[([^]]+)\]\(([^)]+)\)", r'<a href="\2">\1</a>', text)
    return text


def markdown_to_html(text):
    lines = text.splitlines()
    result = []
    i = 0
    while i < len(lines):
        line = lines[i]
        if not line.strip():
            i += 1
            continue
        if line.startswith("```"):
            code = []
            i += 1
            while i < len(lines) and not lines[i].startswith("```"):
                code.append(lines[i])
                i += 1
            result.append("<pre><code>" + html.escape("\n".join(code)) + "</code></pre>")
            i += 1
            continue
        image_match = re.fullmatch(r"!\[([^]]*)\]\(([^)]+)\)", line)
        if image_match:
            image_path = (ROOT / image_match[2]).resolve()
            data = base64.b64encode(image_path.read_bytes()).decode()
            result.append(
                f'<figure><img alt="{html.escape(image_match[1], quote=True)}" src="data:image/png;base64,{data}"></figure>'
            )
            i += 1
            continue
        heading = re.match(r"^(#{1,6}) (.*)", line)
        if heading:
            level = len(heading[1])
            label = heading[2]
            anchor = re.match(r"Q\d{3}\b", label)
            ident = f' id="{anchor[0]}"' if anchor else ""
            result.append(f"<h{level}{ident}>{inline(label)}</h{level}>")
            i += 1
            continue
        if line.startswith("|"):
            rows = []
            while i < len(lines) and lines[i].startswith("|"):
                cells = [c.strip() for c in lines[i].strip("|").split("|")]
                if not all(re.fullmatch(r":?-+:?", c) for c in cells):
                    rows.append(cells)
                i += 1
            table = [
                "<table><thead><tr>"
                + "".join("<th>" + inline(c) + "</th>" for c in rows[0])
                + "</tr></thead><tbody>"
            ]
            for row in rows[1:]:
                table.append("<tr>" + "".join("<td>" + inline(c) + "</td>" for c in row) + "</tr>")
            result.append("".join(table) + "</tbody></table>")
            continue
        if re.match(r"^(- |\d+\. )", line):
            ordered = not line.startswith("- ")
            tag = "ol" if ordered else "ul"
            items = []
            pattern = r"^\d+\. " if ordered else r"^- "
            while i < len(lines):
                if not lines[i].strip() and i + 1 < len(lines) and re.match(pattern, lines[i + 1]):
                    i += 1
                    continue
                if not re.match(pattern, lines[i]):
                    break
                items.append("<li>" + inline(re.sub(pattern, "", lines[i])) + "</li>")
                i += 1
            result.append(f"<{tag}>" + "".join(items) + f"</{tag}>")
            continue
        paragraph = [line]
        i += 1
        while (
            i < len(lines)
            and lines[i].strip()
            and not re.match(r"^(#|\||```|!\[|- |\d+\. )", lines[i])
        ):
            paragraph.append(lines[i])
            i += 1
        result.append("<p>" + inline(" ".join(paragraph)) + "</p>")
    return "\n".join(result)


css = """
body { font-family: Arial, sans-serif; color: #111; background: #fff; margin: 0; font-size: 16px; line-height: 1.65; }
main { max-width: 1000px; margin: 40px auto; padding: 0 28px 70px; }
h1,h2,h3,h4 { color: #000; line-height: 1.25; break-after: avoid; }
h1 { font-size: 34px; margin-bottom: 28px; }
h2 { font-size: 25px; margin-top: 44px; }
h3 { font-size: 19px; margin-top: 30px; }
p { margin: 12px 0; }
a { color: #174ea6; text-decoration: underline; overflow-wrap: anywhere; }
li { margin: 10px 0; }
table { border-collapse: collapse; width: 100%; margin: 24px 0; font-size: 14px; }
th,td { border: 1px solid #d9d9d9; padding: 11px 13px; text-align: left; vertical-align: middle; overflow-wrap: anywhere; }
th { background: #e9eef5; color: #000; }
tr:nth-child(even) td { background: #f8f9fb; }
pre { background: #f5f6f8; padding: 18px; white-space: pre-wrap; overflow-wrap: anywhere; font-size: 13px; }
code { font-family: ui-monospace, Menlo, monospace; }
figure { margin: 28px 0; }
img { width: 100%; height: auto; }
@media print { main { max-width: none; margin: 0; padding: 0; } body { font-size: 10.5pt; } h1 { font-size: 24pt; } h2 { font-size: 17pt; } h3 { font-size: 12pt; } table { font-size: 9pt; } thead { display: table-header-group; } tr,figure { break-inside: avoid; } }
"""
for stem in ["quick-revision", "detailed-explanation"]:
    md = (ROOT / (stem + ".md")).read_text()
    title = md.splitlines()[0].removeprefix("# ")
    document = f'<!doctype html>\n<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1"><title>{html.escape(title)}</title><style>{css}</style></head><body><main>\n{markdown_to_html(md)}\n</main></body></html>\n'
    (ROOT / (stem + ".html")).write_text(document)

manifest = {
    "question_count": len(questions),
    "section_count": len(sections),
    "quick_revision": {
        "destination": QUICK_URL,
        "markdown": "quick-revision.md",
        "formatted_html": "quick-revision.html",
    },
    "detailed_explanation": {
        "destination": DETAIL_URL,
        "markdown": "detailed-explanation.md",
        "formatted_html": "detailed-explanation.html",
    },
    "google_docs_status": "Not updated. No browser or Google Docs editing connection is available.",
    "existing_document_contents": "Not accessible; preserve and inspect before adding content.",
    "prepared_date": "2026-09-06",
}
(ROOT / "delivery-status.json").write_text(json.dumps(manifest, indent=2) + "\n")
for stem in ["quick-revision", "detailed-explanation"]:
    print(stem, len((ROOT / (stem + ".md")).read_text().split()), "words")
print("Generated matching Markdown and self-contained HTML guides; Google Docs not updated.")
