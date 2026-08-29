Add a pure `normalize_label(value: str) -> str` helper in the repository's most
appropriate existing module. It must trim leading and trailing whitespace,
replace each internal run of whitespace with one ASCII space, and preserve all
other Unicode scalar values. Add focused tests that match the repository's test
conventions. Do not change dependencies, public behavior outside this helper, or
generated files.
