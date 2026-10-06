# Names and duplicate rules

Canonicalisation makes search tolerant without rewriting extracted facts. It is
local, explainable and reversible.

For every candidate the system keeps the extracted `observed_name`, its
`evidence_json`, an optional `canonical_name`, and a `resolved` or `abstained`
decision with method and explanation. The catalogue can show a resolved
canonical name while retaining the observed name and aliases.

A resolution is allowed only when the proposed spelling appears literally in a
caption, tagged account, reliable OCR or a locally attested candidate. Case,
accents and spaces are normalised for `normalised_key` search, never in source
evidence. Character distance, translation and phonetic resemblance are not
evidence. In those cases, the system abstains and keeps both names.

Implementation: `src/domain/canonicalization.py`,
`src/pipeline/catalogue/resolve.py`, `src/domain/reel_library.py`,
`src/agent/search.py`, and `candidate_name_resolution` in `storage/schema.sql`.
