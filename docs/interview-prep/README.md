# Android to RAG interview preparation

Prepared on 6 September 2026 with 220 matched questions and answers across 22 topics.

**Google Docs delivery is pending. Neither destination document has been edited.** The session has no connected browser or Google Docs editing tool, and the existing document contents could not be inspected. Once access is available, inspect each destination, preserve existing material, add the appropriate guide, and verify the saved content.

| Requested destination | Prepared content |
| --- | --- |
| [Quick revision Google Doc](https://docs.google.com/document/d/1SZEuldphejmk55V2Q3Gwxgb076uMTJZRP5obBGvJuwU/edit?tab=t.0) | [Quick revision Markdown](quick-revision.md) and [formatted HTML](quick-revision.html) |
| [Detailed explanation Google Doc](https://docs.google.com/document/d/19JF4nCVackD17nZpXFZ8xQ4Cp3kC96kDndTJ7rxcLV0/edit?tab=t.0) | [Detailed explanation Markdown](detailed-explanation.md) and [formatted HTML](detailed-explanation.html) |

The detailed guide contains the tutorial, every source component, exact defaults, ingestion and query walkthroughs, rendered mind maps, mathematics, sizing and cost practice, API examples, interview answers, and primary references. The revision guide uses matching question IDs and one-sentence answers.

Self-test follow-ups are practice prompts; the 220 numbered questions each have an explicit answer. The guide is broad preparation, not a guarantee of every possible interview question. It distinguishes implemented behavior, observed defects, and proposed production components. Application source and existing tests have not been modified.

The HTML versions contain the images directly and have no network-dependent scripts. They are formatting-ready local copies, not evidence of a Google Docs upload.

## Updating the content

Edit [question-bank.json](question-bank.json) for the paired questions or [detailed-introduction.md](detailed-introduction.md) for the tutorial, then run:

```bash
python3 docs/interview-prep/build_guides.py
```

The generator creates both Markdown and HTML versions and a delivery-status manifest. That manifest records preparation, not a completed external write.
