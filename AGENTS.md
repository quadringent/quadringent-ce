# Quadringent contributions

- English for documentation, comments and user-facing text.
- Desktop only: 1440 px, keyboard and mouse. Preserve existing mobile
  adaptations without extending qualification to those devices.
- Never invent data: missing measurements remain missing. Preserve simulated
  or historical provenance and freshness through to the browser.
- Operator copy belongs in `ui/src/domain/operator.ts`. Offer buttons only
  when the service reports `available`. Confirm actions and read back effects.
- Write behavior tests before changing behavior. Use `pytest` as the single
  Python test runner.
- Preserve unrelated changes. Never commit secrets, private reports or customer
  identifiers. See CONTRIBUTING.md and SECURITY.md.
