---
title: Production RAG
emoji: 📚
colorFrom: green
colorTo: gray
sdk: docker
app_port: 8000
pinned: false
short_description: Grounded, cited answers over your own documents
---

# Production RAG

Upload documents, ask questions, get answers that cite the exact passage, or an
honest "I do not have enough evidence" when the documents do not say.

You need an API key from the owner of this Space to use it. For written answers,
add your own LLM key (Groq, OpenAI, Gemini, …) under **Model**; it stays in your
browser and is never stored. Without one, answers are cited quotes from the documents. Source code,
architecture, and the phase-by-phase build: <https://github.com/sidAndroid01/production-rag>.
