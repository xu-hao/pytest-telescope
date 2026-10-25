# Changelog

## 0.1.0a6

- Moving a checkout with pytest assertion-rewrite bytecode no longer refuses valid
  literal telescope declarations. Declaration validation uses the module's current
  project location while preserving source-position, literal-argument and provenance
  checks.
