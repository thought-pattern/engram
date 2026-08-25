# Section 11 corpus provenance and licensing review

Date: 2026-08-20  
Decision: acceptable for component engineering use

The 23 version-1 rules in `engram/data/rewrite-rules-v1.json` were authored for Engram from ordinary English and technical phrasing examples. They were not copied, translated, mechanically derived, or transcribed from historical ALICE, AIML implementation source, chatbot response content, or another rule corpus.

Historical chatbot systems informed only the high-level taxonomy already named by `ENGRAM-DEVELOPMENT.md`: contractions, question normalization, paraphrase reduction, pronoun transformation, synonym classes, conversational repair, contextual reduction, and technical phrasing. The expressions and outputs in this repository are independently authored, narrow retrieval reductions.

Every rule declares `Engram project` as author, an independently authored origin, the repository Apache-2.0 license, and its authoring date. The corpus contains no response prose and grants no response authority. The only runtime template field is the bounded inherited `{subject}` retrieval value; no AIML tags or executable expressions are accepted.

The repository-visible evaluation corpus is also Apache-2.0 and is an engineering holdout, not a Section 16 release partition. It was frozen separately from the production corpus and is not loaded by the runtime. No external model, network resource, downloaded corpus, or runtime license is involved.
