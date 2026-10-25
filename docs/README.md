# AI Pipeline Documentation

- [Architecture Overview](architecture.md) — system design, 6 pipelines, data flow, external tools
- [Configuration](configuration.md) — `.env` settings and per-pipeline defaults in `config/pipeline.defaults.yaml`
- [Script Generation](script-generation.md) — policy-driven AI/finance narration, validation, repair, and audit boundaries
- [Agent Interface](agent-interface.md) — MCP, REST, and CLI contracts for agents and maintainers
- [Browser Automation Strategy](browser-automation-strategy.md) — Photo-Process browser backend decisions and constraints
- [Web Operator Console](web-console.md) — login, dashboard, task forms, preflight, polling, artifacts, and publishing workflow
- [Web Access Boundary](web-access-boundary.md) — loopback and ZeroTier trust boundary, exposure limits, and remote-access gate
- [ZeroTier Web Access Runbook](zerotier-web-access.md) — authentication, TLS, firewall, startup, verification, and emergency disable steps
- [Verification Baselines](verification/) — frozen failure baselines and diagnostic evidence
- [Smoke Testing](smoke-testing.md) — production quality gate, installed-wheel smoke tests, and external tool troubleshooting
- [Publishing Runbook](publishing-runbook.md) — release-gate checklist, dry-run review, and step-by-step guide for each platform
